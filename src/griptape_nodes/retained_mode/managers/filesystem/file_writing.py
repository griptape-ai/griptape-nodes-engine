"""Writing files: the `WriteFile` and `WriteTempFile` handlers and the locking, staging, and vetting behind them."""

import contextlib
import errno
import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import portalocker

from griptape_nodes.common.macro_parser import (
    MacroResolutionError,
    MacroResolutionFailure,
    MacroSyntaxError,
    MacroVariables,
    ParsedMacro,
)
from griptape_nodes.common.macro_parser.segments import ParsedVariable
from griptape_nodes.common.project_templates.situation import BuiltInSituation
from griptape_nodes.files.file import canonical_extension
from griptape_nodes.files.path_utils import (
    normalize_path_for_platform,
    sanitize_path_string,
)
from griptape_nodes.retained_mode.engine import Engine, EngineScoped
from griptape_nodes.retained_mode.events.base_events import ResultDetails, ResultPayload
from griptape_nodes.retained_mode.events.os_events import (
    DeleteFileRequest,
    DeleteFileResultSuccess,
    DeletionBehavior,
    ExistingFilePolicy,
    FileIOFailureReason,
    WriteFileRequest,
    WriteFileResultFailure,
    WriteFileResultSuccess,
    WriteTempFileRequest,
    WriteTempFileResultFailure,
    WriteTempFileResultSuccess,
)
from griptape_nodes.retained_mode.events.project_events import (
    GetPathForMacroRequest,
    GetPathForMacroResultSuccess,
    GetSituationRequest,
    GetSituationResultSuccess,
    MacroPath,
)
from griptape_nodes.retained_mode.file_metadata.sidecar_metadata import write_sidecar
from griptape_nodes.retained_mode.managers.artifact_providers import WriteVettingPolicy
from griptape_nodes.retained_mode.managers.event_manager import EventManager
from griptape_nodes.retained_mode.request_handlers import handles
from griptape_nodes.utils.async_utils import to_thread
from griptape_nodes.utils.file_utils import atomic_write_bytes

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime


logger = logging.getLogger("griptape_nodes")

# Maximum number of indexed candidates to try when CREATE_NEW policy is used
MAX_INDEXED_CANDIDATES = 1000


class FileWriteAttemptResult(NamedTuple):
    """Result of attempting to write a file.

    Possible outcomes:
    - Success: bytes_written is set, failure_reason and error_message are None
    - Continue: all fields are None (file exists/locked but caller wants to continue)
    - Failure: failure_reason and error_message are set, bytes_written is None
    """

    bytes_written: int | None
    failure_reason: FileIOFailureReason | None
    error_message: str | None


class ExtensionAlignment(NamedTuple):
    """Result of aligning a destination path's suffix to the sniffed byte format.

    Attributes:
        aligned_path: The path the write should target. Equal to the requested path
            when the suffix already matches, when bytes were unrecognizable, or for
            non-bytes content. Otherwise the requested path with its suffix replaced
            by the sniffed extension.
        sniffed_ext: The sniffed extension (e.g. ``"jpg"``) when a swap occurred,
            None otherwise. Downstream code carries this forward so the indexed
            walk and the sidecar update agree on the final extension.
    """

    aligned_path: Path
    sniffed_ext: str | None


class StagingFailedError(Exception):
    """Raised when ``FileWriter._stage_bytes_at_temp`` cannot land bytes on disk.

    The on-disk vet path catches this to fail closed: if the bytes cannot even
    be staged for inspection, the write cannot be verified as compliant and
    must be refused. Carries the ``FileIOFailureReason`` that would have
    appeared in a ``WriteTempFileResultFailure`` so the public
    ``on_write_temp_file_request`` handler can preserve the failure reason
    when translating the exception back into a request result.
    """

    def __init__(self, message: str, failure_reason: FileIOFailureReason) -> None:
        super().__init__(message)
        self.failure_reason = failure_reason


@dataclass(frozen=True)
class _StagedTempOutcome:
    """Result of ``FileWriter._stage_bytes_at_temp`` on the success path."""

    staged_path: str
    bytes_written: int


class _WriteTarget(NamedTuple):
    """The file a write first targets, and how to name the requested path in messages."""

    file_path: Path
    path_display: str


class _IndexWalk(NamedTuple):
    """How the CREATE_NEW walk steps through candidate names."""

    macro_path: MacroPath
    index_variable: ParsedVariable
    start_index: int
    # True when walking the caller's own padded slot rather than a synthesized `{stem}_{_index}`.
    walking_original: bool


class _WrittenFile(NamedTuple):
    """Where a write landed."""

    file_path: Path
    bytes_written: int
    used_indexed_fallback: bool


class FileWriter(EngineScoped):
    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        event_manager.register_request_handlers(self)

    def _handle_parent_directory_failure(
        self,
        parent_failure_reason: FileIOFailureReason,
        candidate_path: Path,
    ) -> WriteFileResultFailure:
        """Create failure result for parent directory errors.

        Args:
            parent_failure_reason: The failure reason from _ensure_parent_directory_ready
            candidate_path: The file path that failed

        Returns:
            WriteFileResultFailure with appropriate error message
        """
        match parent_failure_reason:
            case FileIOFailureReason.PERMISSION_DENIED:
                msg = f"Attempted to write to file '{candidate_path}'. Failed due to permission denied creating parent directory {candidate_path.parent}"
            case FileIOFailureReason.POLICY_NO_CREATE_PARENT_DIRS:
                msg = f"Attempted to write to file '{candidate_path}'. Failed due to the parent directory not existing, and a policy was specified to NOT create parent directories: {candidate_path.parent}"
            case _:
                msg = f"Attempted to write to file '{candidate_path}'. Failed due to error creating parent directory {candidate_path.parent}"
        return WriteFileResultFailure(
            failure_reason=parent_failure_reason,
            result_details=msg,
        )

    @handles(WriteFileRequest)
    async def on_write_file_request(
        self, request: WriteFileRequest, workflow: "WorkflowRuntime | None" = None
    ) -> ResultPayload:
        """Write content to a file with exclusive locking, returning once it is on disk.

        A macro path, the codec-vet staging path and the sidecar path resolve for `workflow`.

        Everything that reads engine state runs on the thread driving this coroutine: macro
        resolution, the config read, the codec vet, the index scan, and the collision policy.
        Manager state has no locks, so none of that may move to a worker thread. Only the two
        steps that touch no engine state and take seconds for a large file are handed to a worker
        thread: re-encoding image bytes with workflow metadata, and writing the bytes to disk.
        """
        target = self._resolve_write_target(request, workflow)
        if isinstance(target, WriteFileResultFailure):
            return target
        path_display = target.path_display

        # Sniff bytes ONCE up front. The sniffed format drives two independent
        # decisions -- the codec-permission vet (which must run even for
        # extension-less destinations) and the extension-coercion planner
        # (only relevant when there IS a destination extension to reconcile).
        # ``sniff_extension`` returns None for non-bytes content, so a non-None
        # sniffed_ext also implies ``request.content`` is bytes; downstream code
        # relies on that.
        sniffed_ext = (
            self.engine.artifact_manager.sniff_extension(request.content)
            if isinstance(request.content, bytes)
            else None
        )

        # Write vet. Runs BEFORE extension coercion so an extension-less
        # destination (e.g. "movie") does not silently bypass the gate --
        # sniff on bytes is independent of the destination suffix. Appends
        # skip the vet: the tail alone has no container header to classify.
        # Whether the vet actually does any work is the provider's call:
        # opting out is a single ``None`` return in ``get_write_vetting_policy``,
        # short-circuited inside ``_run_write_vet``.
        if sniffed_ext is not None and not request.append:
            vet_failure = self._run_write_vet(
                workflow,
                content=request.content,  # type: ignore[arg-type]
                sniffed_ext=sniffed_ext,
                file_path=target.file_path,
                caller_variables=request.file_path.variables if isinstance(request.file_path, MacroPath) else None,
            )
            if vet_failure is not None:
                return vet_failure

        # Align the destination suffix to the sniffed format before any scan,
        # write, or candidate generation runs. Without this, the scan globs
        # the template's suffix while the post-write rename moves the file to
        # the sniffed suffix; the next CREATE_NEW save with the same bytes
        # globs the wrong family, returns an already-used index, writes, and
        # the rename clobbers the prior save.
        # (https://github.com/griptape-ai/griptape-nodes-engine/issues/4924)
        # Strict mode fails here before touching the disk.
        alignment = self._apply_extension_coercion(request, target.file_path, sniffed_ext)
        if isinstance(alignment, WriteFileResultFailure):
            return alignment
        file_path = alignment.aligned_path
        # ``alignment.sniffed_ext`` is the *swap-only* signal: non-None only
        # when the on-disk suffix was actually rewritten. Do NOT reuse the raw
        # ``sniffed_ext`` local for on-disk-name-sensitive downstream code
        # (indexed-fallback walk, sidecar update). The raw sniff is truthy
        # for alias pairs too (jpg/jpeg, tif/tiff, m4v/mp4), where the file
        # lands at the caller's requested suffix and downstream consumers
        # would otherwise disagree with the on-disk name.
        swapped_ext = alignment.sniffed_ext

        content = await self._inject_workflow_metadata(request, file_path)

        if request.existing_file_policy is ExistingFilePolicy.CREATE_NEW:
            written = await self._write_create_new(
                request,
                workflow,
                file_path=file_path,
                path_display=path_display,
                content=content,
                swapped_ext=swapped_ext,
            )
        else:
            written = await self._write_in_place(request, file_path, content)
        if isinstance(written, WriteFileResultFailure):
            return written

        if request.file_metadata is not None:
            self._write_sidecar(request, written.file_path, swapped_ext, workflow)

        if written.used_indexed_fallback:
            msg = f"File written to indexed path: {written.file_path} (original path '{path_display}' already existed)"
            result_details = ResultDetails(message=msg, level=logging.DEBUG)
        else:
            result_details = f"File written successfully: {written.file_path}"

        return WriteFileResultSuccess(
            final_file_path=str(written.file_path),
            bytes_written=written.bytes_written,
            result_details=result_details,
        )

    def _write_sidecar(
        self,
        request: WriteFileRequest,
        file_path: Path,
        swapped_ext: str | None,
        workflow: "WorkflowRuntime | None",
    ) -> None:
        """Write the sidecar metadata file the caller opted into by providing `file_metadata`."""
        file_metadata = request.file_metadata
        if file_metadata is None:
            return
        # Sidecar provenance must reflect the on-disk extension, not the requested one.
        # The reconciliation already happened in the sniff-and-swap; this just keeps the
        # sidecar's file_extension variable (when present) consistent with what the bytes
        # turned out to be. ``swapped_ext`` is non-None only when a genuine swap happened:
        # for alias pairs (jpg/jpeg, tif/tiff, m4v/mp4) the file lands at the caller's
        # requested suffix, and the sidecar's ``file_extension`` stays what the caller supplied.
        if swapped_ext is not None and file_metadata.situation is not None:
            sidecar_variables = file_metadata.situation.variables
            if sidecar_variables is not None and "file_extension" in sidecar_variables:
                sidecar_variables["file_extension"] = swapped_ext
        write_sidecar(file_path, file_metadata, self.engine, workflow)

    def _resolve_write_target(
        self, request: WriteFileRequest, workflow: "WorkflowRuntime | None"
    ) -> "_WriteTarget | WriteFileResultFailure":
        """Resolve the requested path to the file the write first targets, and ready its parent directory.

        For a MacroPath, a single padded missing-required slot is seeded with 1, but only
        for CREATE_NEW writes, and only if the macro author opted in via `:NN` padding.
        """
        if isinstance(request.file_path, MacroPath):
            macro_path = request.file_path
            # The readable template string, not the ParsedMacro dataclass repr, which dumps
            # the whole segment tree into every error message below.
            path_display = macro_path.parsed_macro.template
            # First-attempt resolve: for CREATE_NEW, a missing sequence slot is EXPECTED —
            # the failure is the signal the seed-and-retry logic below uses to pick which
            # slot to auto-allocate. Demote the log level so the probing miss doesn't
            # surface as a user-facing ERROR when the retry succeeds. For any other
            # policy the failure IS terminal, so it keeps the default ERROR level.
            if request.existing_file_policy is ExistingFilePolicy.CREATE_NEW:
                first_attempt_log_level = logging.DEBUG
            else:
                first_attempt_log_level = logging.ERROR
            resolution_result = self.engine.os_manager.resolve_macro_path_to_string(
                macro_path, workflow, failure_log_level=first_attempt_log_level
            )

            # Seed-and-retry: ONLY for CREATE_NEW + a single padded missing-required
            # slot. Anything else (other policies, multiple missing, no padding) falls
            # through to the failure return below.
            # https://github.com/griptape-ai/griptape-nodes-engine/issues/4875
            if (
                isinstance(resolution_result, MacroResolutionFailure)
                and resolution_result.missing_variables
                and request.existing_file_policy is ExistingFilePolicy.CREATE_NEW
            ):
                candidate = self.engine.os_manager.indexed_filenames.find_padded_unresolved_required(
                    macro_path.parsed_macro, resolution_result.missing_variables
                )
                if candidate is not None:
                    seeded_vars = {**macro_path.variables, candidate.info.name: 1}
                    seeded_macro = MacroPath(parsed_macro=macro_path.parsed_macro, variables=seeded_vars)
                    # Second-attempt resolve: if seeding didn't fix it, that's genuinely
                    # broken — keep the ERROR default so the user sees what went wrong.
                    resolution_result = self.engine.os_manager.resolve_macro_path_to_string(seeded_macro, workflow)

            if isinstance(resolution_result, MacroResolutionFailure):
                msg = f"Attempted to write to file '{path_display}'. Failed due to missing variables: {resolution_result.error_details}"
                return WriteFileResultFailure(
                    failure_reason=FileIOFailureReason.MISSING_MACRO_VARIABLES,
                    missing_variables=resolution_result.missing_variables,
                    result_details=msg,
                )
            resolved_path_str = resolution_result
        else:
            # Sanitize string path (removes shell escapes, quotes, etc.)
            resolved_path_str = sanitize_path_string(request.file_path)
            path_display = resolved_path_str

        file_path = self._resolve_candidate_path(resolved_path_str, path_display)
        if isinstance(file_path, WriteFileResultFailure):
            return file_path

        parent_failure_reason = self._ensure_parent_directory_ready(file_path, create_parents=request.create_parents)
        if parent_failure_reason is not None:
            return self._handle_parent_directory_failure(parent_failure_reason, file_path)

        return _WriteTarget(file_path=file_path, path_display=path_display)

    def _resolve_candidate_path(self, path_str: str, path_display: str) -> "Path | WriteFileResultFailure":
        """Convert a resolved path string to a Path, naming `path_display` in any failure."""
        try:
            return self.engine.os_manager.resolve_file_path(path_str, workspace_only=False)
        except (ValueError, RuntimeError) as e:
            msg = f"Attempted to write to file '{path_display}'. Failed due to invalid path: {e}"
            return WriteFileResultFailure(
                failure_reason=FileIOFailureReason.INVALID_PATH,
                result_details=msg,
            )
        except Exception as e:
            msg = f"Attempted to write to file '{path_display}'. Failed due to unexpected error: {e}"
            return WriteFileResultFailure(
                failure_reason=FileIOFailureReason.IO_ERROR,
                result_details=msg,
            )

    async def _inject_workflow_metadata(self, request: WriteFileRequest, file_path: Path) -> str | bytes:
        """Return the content to write, with workflow metadata embedded when the config asks for it."""
        content = request.content
        if not isinstance(content, bytes):
            return content
        if request.skip_metadata_injection:
            return content
        if not self.engine.config_manager.get_config_value("auto_inject_workflow_metadata"):
            return content
        # The provider lookup and metadata collection read engine state, so they stay on the
        # loop; only the re-encode with the collected metadata goes to a worker thread.
        provider = self.engine.artifact_manager.provider_for_write(file_path.name)
        if provider is None:
            return content
        # Providers read the bound workflow, which dispatch bound to the one the file's sidecar records.
        metadata = provider.collect_metadata_for_write(file_path.name)
        if not metadata:
            return content
        return await to_thread(provider.prepare_content_for_write, content, file_path.name, metadata)

    async def _write_in_place(
        self, request: WriteFileRequest, file_path: Path, content: str | bytes
    ) -> "_WrittenFile | WriteFileResultFailure":
        """Write for the FAIL and OVERWRITE policies, which only ever target `file_path`."""
        normalized_path = Path(normalize_path_for_platform(file_path))
        if request.existing_file_policy == ExistingFilePolicy.FAIL:
            mode = "x"  # Exclusive creation (fail if exists)
        elif request.append:
            mode = "a"
        else:
            mode = "w"

        if mode == "w":
            # Whole-file overwrites go through a sibling temp file + rename
            # instead of truncating in place, so a concurrent reader (e.g. the
            # static server streaming a preview to a browser) never observes a
            # zero-length or partially-written destination. The rename replaces
            # portalocker as the concurrency mechanism here: concurrent
            # overwrites are last-rename-wins, and writer interleaving is
            # impossible because each writer fills a private sibling.
            result = await to_thread(
                self._attempt_atomic_file_write,
                normalized_path=normalized_path,
                content=content,
                encoding=request.encoding,
                file_path_display=file_path,
            )
        else:
            # The other modes cannot use rename semantics and keep the
            # portalocker-locked in-place write: append ("a") mutates the
            # existing file, so writer mutual exclusion is the only thing
            # keeping two appenders from interleaving; exclusive create ("x",
            # the FAIL policy) gets its atomicity from O_EXCL, and its
            # FileExistsError signal plus the debris cleanup around it are
            # load-bearing for the CREATE_NEW candidate walk.
            result = await to_thread(
                self._attempt_file_write,
                normalized_path=normalized_path,
                content=content,
                encoding=request.encoding,
                mode=mode,
                file_path_display=file_path,
                fail_if_file_exists=True,  # FAIL policy always fails on file exists
                fail_if_file_locked=True,
            )
        if result.failure_reason is not None:
            # error_message is guaranteed to be set when failure_reason is set
            return WriteFileResultFailure(
                failure_reason=result.failure_reason,
                result_details=result.error_message,  # type: ignore[arg-type]
            )
        if result.bytes_written is None:
            msg = "Internal error: write reported neither failure nor bytes written"
            raise RuntimeError(msg)
        return _WrittenFile(file_path=file_path, bytes_written=result.bytes_written, used_indexed_fallback=False)

    async def _write_create_new(  # noqa: PLR0913
        self,
        request: WriteFileRequest,
        workflow: "WorkflowRuntime | None",
        *,
        file_path: Path,
        path_display: str,
        content: str | bytes,
        swapped_ext: str | None,
    ) -> "_WrittenFile | WriteFileResultFailure":
        """Write for the CREATE_NEW policy: try `file_path`, then the next free indexed name."""
        result = await to_thread(
            self._attempt_file_write,
            normalized_path=Path(normalize_path_for_platform(file_path)),
            content=content,
            encoding=request.encoding,
            mode="x",
            file_path_display=file_path,
            fail_if_file_exists=False,  # Fall back to indexed
            fail_if_file_locked=False,  # Fall back to indexed
        )
        if result.failure_reason is not None:
            # error_message is guaranteed to be set when failure_reason is set
            return WriteFileResultFailure(
                failure_reason=result.failure_reason,
                result_details=result.error_message,  # type: ignore[arg-type]
            )
        if result.bytes_written is not None:
            return _WrittenFile(file_path=file_path, bytes_written=result.bytes_written, used_indexed_fallback=False)
        # The file exists or is locked, so walk forward to the next free name.
        return await self._write_next_free_index(
            request,
            workflow,
            file_path=file_path,
            path_display=path_display,
            content=content,
            swapped_ext=swapped_ext,
        )

    async def _write_next_free_index(  # noqa: PLR0913
        self,
        request: WriteFileRequest,
        workflow: "WorkflowRuntime | None",
        *,
        file_path: Path,
        path_display: str,
        content: str | bytes,
        swapped_ext: str | None,
    ) -> "_WrittenFile | WriteFileResultFailure":
        """Write to the first free indexed candidate after `file_path` was taken.

        Two ways to discover the index variable to walk:

        1. If the caller passed a MacroPath that already opted into the auto-index seed
           (one unresolved required `{x:NN}` slot bound to `1` by the seed gate), walk
           THAT slot. Incrementing `_index` against the user's original macro produces
           consistent zero-padded width across the sequence (`v001 → v002 → v003`).
        2. Otherwise, synthesize an `{stem}_{_index}{ext}` macro from the resolved string.
           This handles plain string paths (`output.png` → `output_1.png`). For seeded MacroPaths it would lose padding (`v003 → v003_1`),
           which is why path 1 catches them first.
        """
        walk = self._plan_index_walk(request, file_path, path_display)
        if isinstance(walk, WriteFileResultFailure):
            return walk
        for idx in range(walk.start_index, walk.start_index + MAX_INDEXED_CANDIDATES):
            candidate_path = self._prepare_indexed_candidate(
                request,
                workflow,
                walk,
                idx=idx,
                path_display=path_display,
                swapped_ext=swapped_ext,
            )
            if isinstance(candidate_path, WriteFileResultFailure):
                return candidate_path

            result = await to_thread(
                self._attempt_file_write,
                normalized_path=Path(normalize_path_for_platform(candidate_path)),
                content=content,
                encoding=request.encoding,
                mode="x",
                file_path_display=candidate_path,
                fail_if_file_exists=False,  # Try next candidate
                fail_if_file_locked=False,  # Try next candidate
            )
            if result.failure_reason is not None:
                # error_message is guaranteed to be set when failure_reason is set
                return WriteFileResultFailure(
                    failure_reason=result.failure_reason,
                    result_details=result.error_message,  # type: ignore[arg-type]
                )
            if result.bytes_written is not None:
                return _WrittenFile(
                    file_path=candidate_path, bytes_written=result.bytes_written, used_indexed_fallback=True
                )

        msg = f"Attempted to write to file '{path_display}'. Failed due to could not find available filename after trying {MAX_INDEXED_CANDIDATES} candidates"
        return WriteFileResultFailure(
            failure_reason=FileIOFailureReason.IO_ERROR,
            result_details=msg,
        )

    def _prepare_indexed_candidate(  # noqa: PLR0913
        self,
        request: WriteFileRequest,
        workflow: "WorkflowRuntime | None",
        walk: "_IndexWalk",
        *,
        idx: int,
        path_display: str,
        swapped_ext: str | None,
    ) -> "Path | WriteFileResultFailure":
        """Resolve the candidate at `idx` to a path whose parent directory is ready."""
        index_vars = {**walk.macro_path.variables, walk.index_variable.info.name: idx}
        candidate_str = self._resolve_indexed_candidate(
            MacroPath(parsed_macro=walk.macro_path.parsed_macro, variables=index_vars),
            workflow,
            idx=idx,
            path_display=path_display,
            walking_original=walk.walking_original,
        )
        if isinstance(candidate_str, WriteFileResultFailure):
            return candidate_str

        candidate_path = self._resolve_candidate_path(candidate_str, candidate_str)
        if isinstance(candidate_path, WriteFileResultFailure):
            return candidate_path

        # Align the candidate suffix with the sniffed extension so the walked
        # candidate ends up on disk at the same suffix as the try-first attempt.
        # The synthesized-macro path inherits this because its template comes from
        # the already-swapped file_path; the walking-original path uses the user's
        # original suffix and needs this swap every iteration. Use ``swapped_ext``
        # (non-None only when a genuine swap happened) rather than the raw sniff:
        # alias pairs (jpg/jpeg, tif/tiff, m4v/mp4) leave the on-disk suffix at the
        # caller's request, and the walked candidate must follow suit.
        if swapped_ext is not None:
            candidate_path = candidate_path.with_suffix(f".{swapped_ext}")

        parent_failure_reason = self._ensure_parent_directory_ready(
            candidate_path,
            create_parents=request.create_parents,
        )
        if parent_failure_reason is not None:
            return self._handle_parent_directory_failure(parent_failure_reason, candidate_path)

        return candidate_path

    def _plan_index_walk(
        self, request: WriteFileRequest, file_path: Path, path_display: str
    ) -> "_IndexWalk | WriteFileResultFailure":
        """Pick the macro, slot, and starting index the CREATE_NEW walk steps through."""
        indexed_filenames = self.engine.os_manager.indexed_filenames
        macro_path, padded_index_var = indexed_filenames.select_collision_walk_macro(request, file_path)
        parsed_macro = macro_path.parsed_macro
        variables = macro_path.variables

        # For the synthesized-macro case (padded_index_var is None) we still use
        # identify_index_variable to find the slot — its variables dict is empty by
        # construction so the call is unambiguous. For the original-macro case the
        # caller already knows the slot from select_collision_walk_macro, so we skip the
        # call entirely (running it against `{outputs}/render_v{_index:NN}.png` with
        # empty variables would falsely report ambiguity since `{outputs}` is also
        # unresolved at that level — it gets substituted by ProjectManager during the
        # per-iteration resolve).
        if padded_index_var is not None:
            index_info = padded_index_var
            start_idx = self._collision_walk_start_index(request, padded_index_var)
        else:
            try:
                identified = indexed_filenames.identify_index_variable(parsed_macro, variables)
            except ValueError as e:
                msg = f"Attempted to write to file '{path_display}'. Failed due to {e}"
                return WriteFileResultFailure(
                    failure_reason=FileIOFailureReason.INVALID_PATH,
                    result_details=msg,
                )
            except Exception as e:
                msg = f"Attempted to write to file '{path_display}'. Failed due to unexpected error: {e}"
                return WriteFileResultFailure(
                    failure_reason=FileIOFailureReason.IO_ERROR,
                    result_details=msg,
                )
            if identified is None:
                # This should not happen since the synthesized macro always carries {_index}
                msg = f"Attempted to write to file '{path_display}'. Failed due to missing index variable after conversion"
                return WriteFileResultFailure(
                    failure_reason=FileIOFailureReason.INVALID_PATH,
                    result_details=msg,
                )
            index_info = identified
            # The synthesized macro is fully static except `{_index}`, so scan for a
            # starting index (`output.png` exists, the scan finds `output_1.png`, …,
            # `output_4.png`, and returns 5).
            starting_index = indexed_filenames.scan_for_next_available_index(parsed_macro, variables, index_info)
            start_idx = starting_index if starting_index is not None else 1

        return _IndexWalk(
            macro_path=macro_path,
            index_variable=index_info,
            start_index=start_idx,
            walking_original=padded_index_var is not None,
        )

    def _resolve_indexed_candidate(
        self,
        macro_path: MacroPath,
        workflow: "WorkflowRuntime | None",
        *,
        idx: int,
        path_display: str,
        walking_original: bool,
    ) -> "str | WriteFileResultFailure":
        """Resolve one indexed candidate's path string.

        The caller's original MacroPath routes through ProjectManager so project
        directories (`{outputs}`, …) get substituted along with the index. A synthesized
        macro is fully static apart from the index, so it resolves directly.
        """
        if walking_original:
            resolution = self.engine.os_manager.resolve_macro_path_to_string(macro_path, workflow)
            if isinstance(resolution, MacroResolutionFailure):
                msg = f"Attempted to write to file '{path_display}'. Failed due to unable to resolve path template with index {idx}: {resolution.error_details}"
                return WriteFileResultFailure(
                    failure_reason=FileIOFailureReason.MISSING_MACRO_VARIABLES,
                    result_details=msg,
                )
            return resolution
        try:
            return macro_path.parsed_macro.resolve(macro_path.variables, self.engine.secrets_manager)
        except MacroResolutionError as e:
            msg = f"Attempted to write to file '{path_display}'. Failed due to unable to resolve path template with index {idx}: {e}"
            return WriteFileResultFailure(
                failure_reason=FileIOFailureReason.MISSING_MACRO_VARIABLES,
                result_details=msg,
            )
        except Exception as e:
            msg = f"Attempted to write to file '{path_display}'. Failed due to unexpected error: {e}"
            return WriteFileResultFailure(
                failure_reason=FileIOFailureReason.IO_ERROR,
                result_details=msg,
            )

    @handles(WriteTempFileRequest)
    def on_write_temp_file_request(
        self, request: WriteTempFileRequest, workflow: "WorkflowRuntime | None" = None
    ) -> ResultPayload:
        """Write a temp file at the project-scoped ``SAVE_TEMP_FILE`` situation path, resolved for `workflow`.

        Distinct from ``on_write_file_request``: callers do not choose the
        destination -- the ``SAVE_TEMP_FILE`` situation's macro decides. Thin
        wrapper around ``_stage_bytes_at_temp``; the shared helper is what the
        internal codec-vet path calls without re-entering ``handle_request``.

        The handler does NOT synthesize any variable bindings on the caller's
        behalf. Callers are responsible for supplying enough variables to
        fully resolve the macro (unresolved required slots surface as a
        Failure); if a caller wants collision-safe filenames they must include
        a uuid or similar in ``variables["file_name_base"]``.
        """
        try:
            outcome = self._stage_bytes_at_temp(request.content, request.variables, workflow)
        except StagingFailedError as exc:
            return WriteTempFileResultFailure(
                failure_reason=exc.failure_reason,
                result_details=str(exc),
            )
        return WriteTempFileResultSuccess(
            staged_path=outcome.staged_path,
            bytes_written=outcome.bytes_written,
            result_details=f"Temp file written at {outcome.staged_path}",
        )

    def _stage_bytes_at_temp(
        self, content: bytes, variables: MacroVariables, workflow: "WorkflowRuntime | None"
    ) -> _StagedTempOutcome:
        """Land ``content`` at the SAVE_TEMP_FILE path resolved with ``variables``.

        Resolves the SAVE_TEMP_FILE situation, resolves its macro against the
        caller's variables (merged with project builtins by
        ``GetPathForMacroRequest``), ensures the parent dir, and delegates the
        write to ``_attempt_file_write``. Called by both the public
        ``on_write_temp_file_request`` handler and the internal codec-vet path
        in ``on_write_file_request``. The vet path calls this directly rather
        than round-tripping through ``handle_request`` so a provider vetting a
        path never re-enters the request dispatcher.

        Raises ``StagingFailedError`` on any failure, tagged with the
        ``FileIOFailureReason`` that would have appeared in the request result.
        """
        situation_result = self.engine.handle_request(
            GetSituationRequest(situation_name=BuiltInSituation.SAVE_TEMP_FILE)
        )
        if not isinstance(situation_result, GetSituationResultSuccess):
            msg = (
                f"Attempted to write temp file. Failed because the '{BuiltInSituation.SAVE_TEMP_FILE}' "
                f"situation is not registered in the current project template."
            )
            raise StagingFailedError(msg, FileIOFailureReason.INVALID_PATH)

        try:
            parsed_macro = ParsedMacro(situation_result.situation.macro)
        except MacroSyntaxError as exc:
            msg = f"Attempted to write temp file. Failed to parse SAVE_TEMP_FILE macro: {exc}"
            raise StagingFailedError(msg, FileIOFailureReason.INVALID_PATH) from exc

        path_result = self.engine.project_manager.get_path_for_macro_in(
            workflow, GetPathForMacroRequest(parsed_macro=parsed_macro, variables=variables)
        )
        if not isinstance(path_result, GetPathForMacroResultSuccess):
            msg = f"Attempted to write temp file. Failed to resolve SAVE_TEMP_FILE macro: {path_result.result_details}"
            raise StagingFailedError(msg, FileIOFailureReason.INVALID_PATH)

        staged_path = path_result.absolute_path

        parent_failure_reason = self._ensure_parent_directory_ready(staged_path, create_parents=True)
        if parent_failure_reason is not None:
            msg = f"Attempted to write temp file at '{staged_path}'. Failed to prepare parent directory."
            raise StagingFailedError(msg, parent_failure_reason)

        normalized_path = normalize_path_for_platform(staged_path)
        attempt = self._attempt_file_write(
            normalized_path=Path(normalized_path),
            content=content,
            encoding="utf-8",  # ignored for bytes content
            mode="w",
            file_path_display=staged_path,
            fail_if_file_exists=False,  # uuid stem makes this unreachable, but be explicit
            fail_if_file_locked=True,
        )
        if attempt.failure_reason is not None:
            # ``error_message`` is guaranteed set when ``failure_reason`` is set.
            raise StagingFailedError(attempt.error_message or "unknown write failure", attempt.failure_reason)
        # ``bytes_written`` is guaranteed set on the success path.
        return _StagedTempOutcome(staged_path=str(staged_path), bytes_written=attempt.bytes_written)  # type: ignore[arg-type]

    def _truncate_and_delete_staged(self, staged_path: str) -> None:
        r"""Neutralize and remove a staged codec-vet temp file.

        Truncate the file to a single ``\x00`` byte, then delete it. Truncate
        first so that even if the delete fails, whatever remains on disk
        cannot be interpreted as a video: ffprobe on a 1-byte file finds no
        container header. Delete second so the normal case leaves nothing
        behind.

        The truncate calls ``_attempt_file_write`` directly rather than
        dispatching ``WriteFileRequest`` -- routing a single null byte back
        through ``on_write_file_request`` would re-run the sniff / vet /
        extension-coercion pipeline against the very cleanup path that just
        stripped the bytes we care about. Sniffing that byte is harmless
        today (returns None), but any future check bolted onto the write
        handler would fire on cleanup too.

        The delete dispatches ``DeleteFileRequest`` via ``handle_request``,
        specifically so it flows through ``on_delete_file_request``: that's
        where workspace containment, permanent-vs-trash policy, and audit
        logging live. Skipping the request layer here would silently opt
        codec-vet cleanup out of every one of those.

        Best-effort: logs on failure, never raises. A cleanup failure must
        not mask the vet's real result.
        """
        try:
            normalized = Path(normalize_path_for_platform(Path(staged_path)))
            attempt = self._attempt_file_write(
                normalized_path=normalized,
                content=b"\x00",
                encoding="utf-8",
                mode="w",
                file_path_display=staged_path,
                fail_if_file_exists=False,
                fail_if_file_locked=True,
            )
            if attempt.failure_reason is not None:
                logger.error(
                    "Attempted to truncate staged codec-vet temp at '%s'. Failed: %s",
                    staged_path,
                    attempt.error_message,
                )
        except OSError as exc:
            logger.error("Attempted to truncate staged codec-vet temp at '%s'. Failed: %s", staged_path, exc)

        delete_result = self.engine.handle_request(
            DeleteFileRequest(
                path=staged_path,
                workspace_only=False,
                deletion_behavior=DeletionBehavior.PERMANENTLY_DELETE,
            )
        )
        if not isinstance(delete_result, DeleteFileResultSuccess):
            logger.error(
                "Attempted to delete staged codec-vet temp at '%s'. Failed: %s (file has been truncated).",
                staged_path,
                delete_result.result_details,
            )

    def _run_write_vet(
        self,
        workflow: "WorkflowRuntime | None",
        *,
        content: bytes,
        sniffed_ext: str,
        file_path: Path,
        caller_variables: MacroVariables | None,
    ) -> WriteFileResultFailure | None:
        """Ask the format's provider to vet the pending write.

        Reads the provider's declared ``WriteVettingPolicy`` and dispatches
        accordingly:

        - ``None`` -- provider opts out. No staging, no dispatch, no cost.
        - ``FROM_BYTES`` -- hand the raw bytes to the provider directly.
        - ``FROM_PATH`` -- stage bytes at the SAVE_TEMP_FILE path, hand the
          resulting path to the provider, and truncate + delete the staged
          file in a ``finally`` regardless of outcome. Staging failure is
          treated as fail-closed: an unstageable vet cannot verify the write.

        Returns a ``WriteFileResultFailure`` when the vet refuses (or fails
        closed), or ``None`` when the write is permitted.

        An unrecognized ``WriteVettingPolicy`` raises loudly: silently
        falling through would let a policy variant added without updating
        this switch bless every write that reached it.
        """
        artifact_manager = self.engine.artifact_manager
        policy = artifact_manager.get_write_vetting_policy(sniffed_ext)
        denial: CheckpointDenial | None = None

        match policy:
            case None:
                return None
            case WriteVettingPolicy.FROM_BYTES:
                denial = artifact_manager.check_write_format_from_bytes(content, sniffed_ext)
            case WriteVettingPolicy.FROM_PATH:
                staging_variables: MacroVariables = dict(caller_variables) if caller_variables else {}
                # Vet's required overrides sit on top of caller variables --
                # ``file_name_base`` must be a uuid to keep concurrent vet
                # stagings from colliding under SAVE_TEMP_FILE's OVERWRITE
                # policy; ``file_extension`` must match the sniffed container
                # so ffprobe's extension-based demuxer dispatch picks correctly.
                staging_variables["file_name_base"] = uuid.uuid4().hex
                staging_variables["file_extension"] = sniffed_ext

                try:
                    outcome = self._stage_bytes_at_temp(content, staging_variables, workflow)
                except StagingFailedError as exc:
                    return WriteFileResultFailure(
                        failure_reason=FileIOFailureReason.CODEC_NOT_PERMITTED,
                        result_details=(f"Cannot save '{file_path.name}': staging for verification failed ({exc})."),
                    )
                try:
                    denial = artifact_manager.check_write_format_from_path(outcome.staged_path, sniffed_ext)
                finally:
                    self._truncate_and_delete_staged(outcome.staged_path)
            case _:
                msg = f"Unrecognized WriteVettingPolicy '{policy}' returned by provider for format '{sniffed_ext}'."
                raise RuntimeError(msg)

        if denial is None:
            return None
        return WriteFileResultFailure(
            failure_reason=FileIOFailureReason.CODEC_NOT_PERMITTED,
            result_details=f"Cannot save '{file_path.name}': {denial.reason()}",
        )

    def _apply_extension_coercion(
        self,
        request: WriteFileRequest,
        file_path: Path,
        sniffed_ext: str | None,
    ) -> ExtensionAlignment | WriteFileResultFailure:
        """Reconcile the destination suffix with the pre-sniffed byte format.

        ``on_write_file_request`` sniffs once up front and hands the result in,
        so this method never sniffs on its own; its single responsibility is
        deciding whether to rewrite the destination suffix.

        The three relevant signals are the pre-sniffed extension, the path's
        current suffix, and ``coerce_extension_to_match_bytes``:

        - No sniff / no bytes / empty destination suffix → no swap; pass the
          path through unchanged. Unrecognized bytes with a non-empty suffix
          log a warning so callers can spot it in logs.
        - Sniffed matches the path's canonical suffix → no swap.
        - Sniffed differs and ``coerce_extension_to_match_bytes=True`` (default)
          → swap to the sniffed suffix and return the new path.
        - Sniffed differs and ``coerce_extension_to_match_bytes=False`` →
          return ``WriteFileResultFailure`` with ``EXTENSION_MISMATCH`` so the
          caller can early-exit before touching the disk.
        """
        content = request.content
        if not isinstance(content, bytes):
            return ExtensionAlignment(aligned_path=file_path, sniffed_ext=None)

        destination_suffix = file_path.suffix.lstrip(".").lower()
        if not destination_suffix:
            return ExtensionAlignment(aligned_path=file_path, sniffed_ext=None)

        if sniffed_ext is None:
            logger.warning(
                "Attempted to identify the format of bytes destined for '%s'. "
                "Could not recognize the bytes as a known file format, so the file will be written "
                "with its requested '.%s' extension unchanged.",
                file_path,
                destination_suffix,
            )
            return ExtensionAlignment(aligned_path=file_path, sniffed_ext=None)

        if canonical_extension(destination_suffix) == canonical_extension(sniffed_ext):
            return ExtensionAlignment(aligned_path=file_path, sniffed_ext=None)

        if not request.coerce_extension_to_match_bytes:
            msg = (
                f"Attempted to write to file '{file_path}' with bytes that look like '.{sniffed_ext}'. "
                f"Failed because the requested extension '.{destination_suffix}' does not match the "
                f"byte content and coerce_extension_to_match_bytes=False. Either rename the destination "
                f"to '.{sniffed_ext}' or supply bytes that match '.{destination_suffix}'."
            )
            return WriteFileResultFailure(
                failure_reason=FileIOFailureReason.EXTENSION_MISMATCH,
                result_details=msg,
            )

        aligned_path = file_path.with_suffix(f".{sniffed_ext}")
        logger.warning(
            "Attempted to write '%s'. The bytes look like '.%s', so the destination has been "
            "adjusted to '%s' to match the byte content.",
            file_path,
            sniffed_ext,
            aligned_path,
        )
        return ExtensionAlignment(aligned_path=aligned_path, sniffed_ext=sniffed_ext)

    def _ensure_parent_directory_ready(
        self,
        file_path: Path,
        *,
        create_parents: bool,
    ) -> FileIOFailureReason | None:
        """Ensure parent directory exists or create it.

        Args:
            file_path: The file path whose parent should be validated/created
            create_parents: If True, create parent dirs; if False, validate they exist

        Returns:
            None on success, FileIOFailureReason if validation/creation fails
        """
        if create_parents:
            parent_normalized = normalize_path_for_platform(file_path.parent)
            try:
                if not Path(parent_normalized).exists():
                    Path(parent_normalized).mkdir(parents=True, exist_ok=True)
            except PermissionError:
                return FileIOFailureReason.PERMISSION_DENIED
            except OSError:
                return FileIOFailureReason.IO_ERROR
        elif not file_path.parent.exists():
            return FileIOFailureReason.POLICY_NO_CREATE_PARENT_DIRS

        return None

    def _attempt_atomic_file_write(  # noqa: PLR0911
        self,
        normalized_path: Path,
        content: str | bytes,
        encoding: str,
        file_path_display: str | Path,
    ) -> FileWriteAttemptResult:
        """Overwrite a file atomically via ``atomic_write_bytes``, mapping errors.

        The destination holds either its prior content or the full new content —
        readers never see a truncated file, and a failed write (including disk
        full) leaves the previous file intact instead of destroying it first the
        way an in-place truncate does.

        Args:
            normalized_path: The normalized destination path
            content: Content to write (str or bytes)
            encoding: Encoding for text content
            file_path_display: Path to use in error messages

        Returns:
            FileWriteAttemptResult with either bytes_written set (success) or
            failure_reason and error_message set (failure). Never returns the
            continue signal — overwrites have no fallback candidates.
        """
        if isinstance(content, bytes):
            data = content
        else:
            # Text mode translates every "\n" to os.linesep ("\r\n" on Windows);
            # this path writes raw bytes, so translate here or every text save
            # on Windows lands with bare LF.
            if os.linesep != "\n":
                content = content.replace("\n", os.linesep)
            try:
                data = content.encode(encoding)
            except UnicodeEncodeError as e:
                msg = f"Attempted to write to file '{file_path_display}'. Failed because the text could not be encoded as {encoding}: {e}"
                return FileWriteAttemptResult(
                    bytes_written=None,
                    failure_reason=FileIOFailureReason.ENCODING_ERROR,
                    error_message=msg,
                )

        try:
            atomic_write_bytes(normalized_path, data)
        except IsADirectoryError as e:
            msg = f"Attempted to write to file '{file_path_display}'. Failed due to path is a directory: {e}"
            return FileWriteAttemptResult(
                bytes_written=None,
                failure_reason=FileIOFailureReason.IS_DIRECTORY,
                error_message=msg,
            )
        except PermissionError as e:
            # On Windows, renaming onto a destination that another process holds
            # open (without FILE_SHARE_DELETE) raises PermissionError. That is
            # contention, not a permissions problem — retrying after the reader
            # closes succeeds — so report it as a lock. Classify off the error's
            # own winerror (sharing/lock violation) rather than probing the
            # filesystem afterward, which could observe a different state than
            # the one that caused the failure. winerror is None off-Windows.
            windows_contention_codes = (32, 33)  # ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION
            if getattr(e, "winerror", None) in windows_contention_codes:
                msg = f"Attempted to write to file '{file_path_display}'. Failed because the file is in use by another process: {e}"
                return FileWriteAttemptResult(
                    bytes_written=None,
                    failure_reason=FileIOFailureReason.FILE_LOCKED,
                    error_message=msg,
                )
            msg = f"Attempted to write to file '{file_path_display}'. Failed due to permission denied: {e}"
            return FileWriteAttemptResult(
                bytes_written=None,
                failure_reason=FileIOFailureReason.PERMISSION_DENIED,
                error_message=msg,
            )
        except OSError as e:
            if e.errno == errno.ENOSPC:
                # The swap needs the old and new file to coexist briefly, so a
                # nearly-full volume fails here — cleanly, with the previous
                # file untouched. Say so in terms an artist can act on.
                msg = (
                    f"Attempted to write to file '{file_path_display}'. "
                    f"Failed because the disk holding it is full. The previous version of the file was left unchanged. "
                    f"Free up space on that drive and try again."
                )
                return FileWriteAttemptResult(
                    bytes_written=None,
                    failure_reason=FileIOFailureReason.DISK_FULL,
                    error_message=msg,
                )
            msg = f"Attempted to write to file '{file_path_display}'. Failed due to I/O error: {e}"
            return FileWriteAttemptResult(
                bytes_written=None,
                failure_reason=FileIOFailureReason.IO_ERROR,
                error_message=msg,
            )

        return FileWriteAttemptResult(
            bytes_written=len(data),
            failure_reason=None,
            error_message=None,
        )

    def _attempt_file_write(  # noqa: PLR0911, PLR0913
        self,
        normalized_path: Path,
        content: str | bytes,
        encoding: str,
        mode: str,
        file_path_display: str | Path,
        *,
        fail_if_file_exists: bool,
        fail_if_file_locked: bool,
    ) -> FileWriteAttemptResult:
        """Attempt to write a file with unified exception handling.

        Args:
            normalized_path: The normalized path to write to
            content: Content to write (str or bytes)
            encoding: Encoding for text content
            mode: Write mode ("x", "w", "a")
            file_path_display: Path to use in error messages
            fail_if_file_exists: If True, return failure when file exists; if False, return continue signal
            fail_if_file_locked: If True, return failure when file is locked; if False, return continue signal

        Returns:
            FileWriteAttemptResult with one of:
            - Success: bytes_written is set, failure_reason and error_message are None
            - Continue: all fields are None (file exists/locked but caller wants to continue)
            - Failure: failure_reason and error_message are set, bytes_written is None
        """
        try:
            bytes_written = self._write_with_portalocker(
                str(normalized_path),
                content,
                encoding,
                mode=mode,
            )
            # Success!
            return FileWriteAttemptResult(
                bytes_written=bytes_written,
                failure_reason=None,
                error_message=None,
            )
        except FileExistsError:
            if fail_if_file_exists:
                msg = f"Attempted to write to file '{file_path_display}'. Failed due to file already exists (policy: fail if exists)"
                return FileWriteAttemptResult(
                    bytes_written=None,
                    failure_reason=FileIOFailureReason.POLICY_NO_OVERWRITE,
                    error_message=msg,
                )
            # Continue signal - caller should try next candidate or fallback
            return FileWriteAttemptResult(
                bytes_written=None,
                failure_reason=None,
                error_message=None,
            )
        except portalocker.LockException:
            if fail_if_file_locked:
                msg = f"Attempted to write to file '{file_path_display}'. Failed due to file locked by another process"
                return FileWriteAttemptResult(
                    bytes_written=None,
                    failure_reason=FileIOFailureReason.FILE_LOCKED,
                    error_message=msg,
                )
            # Continue signal - caller should try next candidate or fallback
            return FileWriteAttemptResult(
                bytes_written=None,
                failure_reason=None,
                error_message=None,
            )
        except PermissionError as e:
            msg = f"Attempted to write to file '{file_path_display}'. Failed due to permission denied: {e}"
            return FileWriteAttemptResult(
                bytes_written=None,
                failure_reason=FileIOFailureReason.PERMISSION_DENIED,
                error_message=msg,
            )
        except IsADirectoryError as e:
            msg = f"Attempted to write to file '{file_path_display}'. Failed due to path is a directory: {e}"
            return FileWriteAttemptResult(
                bytes_written=None,
                failure_reason=FileIOFailureReason.IS_DIRECTORY,
                error_message=msg,
            )
        except Exception as e:
            msg = f"Attempted to write to file '{file_path_display}'. Failed due to unexpected error: {e}"
            return FileWriteAttemptResult(
                bytes_written=None,
                failure_reason=FileIOFailureReason.IO_ERROR,
                error_message=msg,
            )

    def _write_with_portalocker(self, normalized_path: str, content: str | bytes, encoding: str, *, mode: str) -> int:
        """Write content to a file with exclusive lock using portalocker.

        Args:
            normalized_path: Normalized path string (with Windows long path prefix if needed)
            content: Content to write (str for text, bytes for binary)
            encoding: Text encoding (ignored for bytes)
            mode: File open mode ('x' for exclusive create, 'w' for overwrite, 'a' for append)

        Returns:
            Number of bytes written

        Raises:
            FileExistsError: If mode='x' and file already exists
            portalocker.LockException: If file is locked by another process
            PermissionError: If permission denied
            IsADirectoryError: If path is a directory
            UnicodeEncodeError: If encoding error occurs
            OSError: For other I/O errors
        """
        error_details = None

        try:
            self._write_locked_discarding_debris(normalized_path, content, encoding, mode=mode)

            # Calculate bytes written
            if isinstance(content, bytes):
                return len(content)
            return len(content.encode(encoding))

        except portalocker.LockException:
            raise
        except FileExistsError:
            raise
        except PermissionError:
            raise
        except IsADirectoryError:
            raise
        except UnicodeEncodeError:
            raise
        except OSError as e:
            # Check for disk full
            if "No space left" in str(e) or "Disk full" in str(e):
                error_details = f"Disk full: {e}"
                raise OSError(error_details) from e
            raise

    def _write_locked_discarding_debris(
        self, normalized_path: str, content: str | bytes, encoding: str, *, mode: str
    ) -> None:
        """Write under an exclusive lock, removing the file if the write fails.

        portalocker opens the file before it acquires the lock, so under ``mode="x"`` the
        file exists by the time a lock failure or a write/fsync error is raised. Leaving it
        there is what breaks the CREATE_NEW collision loop: that loop treats a lock failure
        as "try the next candidate," so debris accumulates one file per index until every
        candidate slot is taken and all later saves fail with "could not find available
        filename" (griptape-ai/internal#178).

        Removing it is safe because ``mode="x"`` maps to ``O_EXCL``, whose create is atomic:
        if anything already holds the name -- a file, a directory, or a symlink, live or
        dangling -- ``open()`` raises ``FileExistsError`` and the cleanup is never reached.
        The unlink is therefore reachable only after *our* create succeeded, which both
        proves the file is ours and rules out a concurrent writer sneaking a file in between
        (their create would have lost the race to ours). That proof rests entirely on the
        exclusive-create flag, so ``file_mode`` is derived here rather than accepted from the
        caller: a caller passing ``mode="x"`` alongside a non-exclusive open would arm the
        unlink against a pre-existing file.

        Args:
            normalized_path: Platform-normalized path string to write to
            content: Content to write (str or bytes)
            encoding: Text encoding (ignored for bytes content)
            mode: Logical write mode ("x", "w", or "a"). Only "x" enables cleanup.

        Raises:
            Exception: Whatever the write raised, after cleanup.
        """
        # Binary flag belongs to the payload, not the caller's policy.
        file_mode = mode + "b" if isinstance(content, bytes) else mode

        try:
            with portalocker.Lock(
                normalized_path,
                mode=file_mode,  # type: ignore[arg-type]
                encoding=encoding if isinstance(content, str) else None,
                timeout=0,  # Non-blocking
                flags=portalocker.LockFlags.EXCLUSIVE | portalocker.LockFlags.NON_BLOCKING,
            ) as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
        # LOAD-BEARING, despite the blanket handler below also re-raising: this arm is what
        # keeps the unlink away from a file we did not create. O_EXCL raises FileExistsError
        # when the name is already taken, so returning here is the only thing standing
        # between a lost race and deleting the winner's file. Do not merge it into the
        # handler below.
        except FileExistsError:
            raise
        except Exception:
            if mode == "x":
                with contextlib.suppress(OSError):
                    Path(normalized_path).unlink()
            raise

    @staticmethod
    def _collision_walk_start_index(request: WriteFileRequest, padded_index_var: ParsedVariable) -> int:
        """Pick the first index to try when walking the caller's own padded slot.

        - Bound to value N (caller reverse-matched an existing file, e.g. `_index=7` from
          `my_file_v007.py`): start at N+1. The slot's value is the "current version";
          we want the next.
        - Required `{x:NN}` unbound: the seed in `_resolve_write_target` already tried 1,
          so start at 2.
        - Optional `{x?:NN}` unbound: the seed didn't fire (it's gated on required), and
          the first attempt resolved with the slot omitted, so start at 1.
        """
        bound_value = None
        if isinstance(request.file_path, MacroPath):
            bound_value = request.file_path.variables.get(padded_index_var.info.name)
        if isinstance(bound_value, int):
            return bound_value + 1
        if padded_index_var.info.is_required:
            return 2
        return 1
