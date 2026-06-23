"""File path-like object for simplified file reading via the retained mode API."""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, Protocol, cast, runtime_checkable

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.events.base_events import ResultPayload

from griptape_nodes.common.macro_parser import MacroSyntaxError, NumericPaddingFormat, ParsedMacro, ParsedVariable
from griptape_nodes.retained_mode.events.os_events import (
    ExistingFilePolicy,
    FileIOFailureReason,
    GetNextVersionIndexRequest,
    GetNextVersionIndexResultSuccess,
    ReadFileRequest,
    ReadFileResultFailure,
    ReadFileResultSuccess,
    WriteFileRequest,
    WriteFileResultFailure,
    WriteFileResultSuccess,
)
from griptape_nodes.retained_mode.events.project_events import (
    GetPathForMacroRequest,
    GetPathForMacroResultFailure,
    GetPathForMacroResultSuccess,
    MacroPath,
    PathResolutionFailureReason,
)
from griptape_nodes.retained_mode.file_metadata.sidecar_metadata import (
    SidecarContent,
    SituationMetadata,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

logger = logging.getLogger("griptape_nodes")


class FileLoadError(Exception):
    """Raised when a file load operation fails.

    Attributes:
        failure_reason: Classification of why the load failed.
        result_details: Human-readable error message.
    """

    def __init__(
        self,
        failure_reason: FileIOFailureReason,
        result_details: str,
        missing_variables: set[str] | None = None,
        conflicting_variables: set[str] | None = None,
    ) -> None:
        self.failure_reason = failure_reason
        self.result_details = result_details
        self.missing_variables = missing_variables
        self.conflicting_variables = conflicting_variables
        super().__init__(result_details)


class FileWriteError(Exception):
    """Raised when a file write operation fails.

    Attributes:
        failure_reason: Classification of why the write failed.
        result_details: Human-readable error message.
    """

    def __init__(
        self,
        failure_reason: FileIOFailureReason,
        result_details: str,
        missing_variables: set[str] | None = None,
    ) -> None:
        self.failure_reason = failure_reason
        self.result_details = result_details
        self.missing_variables = missing_variables
        super().__init__(result_details)


class FileContent(NamedTuple):
    """Result of reading a file, containing content and metadata."""

    content: str | bytes
    mime_type: str
    encoding: str | None
    size: int


_PATH_FAILURE_TO_FILE_IO: dict[PathResolutionFailureReason, FileIOFailureReason] = {
    PathResolutionFailureReason.MISSING_REQUIRED_VARIABLES: FileIOFailureReason.MISSING_MACRO_VARIABLES,
    PathResolutionFailureReason.MACRO_RESOLUTION_ERROR: FileIOFailureReason.INVALID_PATH,
    PathResolutionFailureReason.RESERVED_NAME_COLLISION: FileIOFailureReason.INVALID_PATH,
}


def _make_file_load_error(result: ResultPayload) -> FileLoadError:
    if isinstance(result, GetPathForMacroResultFailure):
        return FileLoadError(
            failure_reason=_PATH_FAILURE_TO_FILE_IO[result.failure_reason],
            result_details=str(result.result_details),
            missing_variables=result.missing_variables,
            conflicting_variables=result.conflicting_variables,
        )
    return FileLoadError(
        failure_reason=FileIOFailureReason.UNKNOWN,
        result_details=str(result.result_details),
    )


def _find_padded_unresolved_required(macro_path: MacroPath, missing_required: set[str]) -> ParsedVariable | None:
    """Return the unresolved required variable that opts into auto-index seeding, else None.

    A macro author opts in by writing exactly one unresolved required variable with a
    ``NumericPaddingFormat`` (``{x:NN}``). The padding spec is the signal that this slot
    holds an auto-allocated zero-padded number — the only realistic shape an index takes.
    Without padding, an unresolved ``{shot}`` could just as plausibly be a variable the
    user forgot to bind, and silently filling it with ``1, 2, 3, …`` would write data
    under a name the user never intended.

    Debugging note: a ``None`` return from this function is the most common reason the
    seed-on-retry path silently falls through to ``MISSING_REQUIRED_VARIABLES``. If a
    caller expects auto-allocation and gets a missing-variables error instead, check
    each gate below in order.
    """
    # Gate 1: the heuristic only fires when there is exactly ONE missing required
    # variable. Two or more missing → ambiguous which one is the index slot, so we
    # refuse to guess and let MISSING_REQUIRED_VARIABLES surface every unbound name.
    if len(missing_required) != 1:
        return None
    [name] = missing_required

    # Gate 2: walk the parsed segments to find the variable's full ParsedVariable
    # (we need its format_specs, which the caller doesn't have — they only have the
    # name string from result.missing_variables). The same name can appear multiple
    # times in a template; we use the first occurrence because partial_resolve treats
    # repeated variable names as a single binding.
    matching_variables: list[ParsedVariable] = []
    for segment in macro_path.parsed_macro.segments:
        if isinstance(segment, ParsedVariable) and segment.info.name == name:
            matching_variables.append(segment)  # noqa: PERF401  # explicit loop for breakpoint debugging
    if not matching_variables:
        # Shouldn't happen in practice — the name came from the parser's own
        # missing-variables set — but guard so a corrupt failure result can't crash.
        return None
    candidate = matching_variables[0]

    # Gate 3: the macro author must have opted in via padding (`:NN`). This is the
    # safety contract — without it, an unbound `{shot}` would be auto-allocated and
    # we'd write `1_render.png`, `2_render.png`, … under a name the user did not
    # intend. The padding spec is what distinguishes "auto-index slot" from
    # "configuration mistake."
    if not any(isinstance(spec, NumericPaddingFormat) for spec in candidate.format_specs):
        return None

    return candidate


def _seed_index_for_create_new(macro_path: MacroPath, var: ParsedVariable) -> MacroPath | None:
    """Ask OSManager for the next free index for ``var`` and return a seeded MacroPath.

    Returns ``None`` when the OS-level scan can't compute a value (e.g. ambiguous index
    variable, glob failure, no current project) so the caller falls through to the
    standard ``MISSING_REQUIRED_VARIABLES`` error rather than silently writing.

    Debugging note: if seeding seems to be returning index=1 every time when files
    already exist on disk, the scan is most likely globbing the wrong directory —
    see ``_inject_referenced_project_directories`` in os_manager.py and the workspace
    anchor in ``_scan_for_next_available_index``. The latter was the root cause of the
    Windows-CI regression caught while implementing
    https://github.com/griptape-ai/griptape-nodes-engine/issues/4875.
    """
    # Hand the macro to the OS-level scan handler. It absolutizes any project
    # directory variables the macro references (so `{outputs}/...` becomes a concrete
    # workspace-anchored glob), walks the filesystem once, and returns the next gap-fill
    # index — or fails (e.g. multiple unresolved variables visible to it).
    scan_result = GriptapeNodes.handle_request(GetNextVersionIndexRequest(macro_path=macro_path))
    if not isinstance(scan_result, GetNextVersionIndexResultSuccess):
        return None

    # An optional `{_index?:NN}` slot can return None when the un-indexed base path
    # is free; for the seed-on-retry caller we always need a concrete integer (the
    # whole point of the retry is that the index was REQUIRED), so default to 1.
    next_index = scan_result.index if scan_result.index is not None else 1

    # Build a fresh MacroPath with the seeded value layered ON TOP of the user's
    # original variables — we only add the index, never strip or mutate caller data.
    # The parsed_macro is reused as-is because the seed retry re-issues
    # GetPathForMacroRequest, which resolves project directories itself.
    seeded_vars = {**macro_path.variables, var.info.name: next_index}
    return MacroPath(parsed_macro=macro_path.parsed_macro, variables=seeded_vars)


async def _aseed_index_for_create_new(macro_path: MacroPath, var: ParsedVariable) -> MacroPath | None:
    """Async sibling of ``_seed_index_for_create_new``. See its docstring for breadcrumbs."""
    scan_result = await GriptapeNodes.ahandle_request(GetNextVersionIndexRequest(macro_path=macro_path))
    if not isinstance(scan_result, GetNextVersionIndexResultSuccess):
        return None
    next_index = scan_result.index if scan_result.index is not None else 1
    seeded_vars = {**macro_path.variables, var.info.name: next_index}
    return MacroPath(parsed_macro=macro_path.parsed_macro, variables=seeded_vars)


def _resolve_macro_path(macro_path: MacroPath) -> str:
    """Dispatch GetPathForMacroRequest and return the resolved absolute path string.

    Pure resolver — no policy-dependent behavior, no filesystem-aware retry. The CREATE_NEW
    seed-on-retry-and-write loop lives in ``_seeded_create_new_write`` instead.

    Raises:
        FileLoadError: If macro resolution fails.
    """
    result = GriptapeNodes.handle_request(
        GetPathForMacroRequest(parsed_macro=macro_path.parsed_macro, variables=macro_path.variables)
    )
    if not isinstance(result, GetPathForMacroResultSuccess):
        raise _make_file_load_error(result)
    return str(result.absolute_path)


async def _aresolve_macro_path(macro_path: MacroPath) -> str:
    """Async sibling of ``_resolve_macro_path``."""
    result = await GriptapeNodes.ahandle_request(
        GetPathForMacroRequest(parsed_macro=macro_path.parsed_macro, variables=macro_path.variables)
    )
    if not isinstance(result, GetPathForMacroResultSuccess):
        raise _make_file_load_error(result)
    return str(result.absolute_path)


def _resolve_file_path(file_path: str | MacroPath) -> str:
    """Resolve a file path, handling MacroPath resolution if needed.

    Args:
        file_path: A plain path string or a MacroPath.

    Returns:
        A resolved path string.

    Raises:
        FileLoadError: If macro resolution fails.
    """
    if isinstance(file_path, str):
        return file_path
    return _resolve_macro_path(file_path)


async def _aresolve_file_path(file_path: str | MacroPath) -> str:
    """Async version of ``_resolve_file_path``."""
    if isinstance(file_path, str):
        return file_path
    return await _aresolve_macro_path(file_path)


# Race-loss recovery uses these failure reasons to distinguish "another writer took
# our slot — re-seed and try the next index" from "real error — propagate." Centralized
# so the sync and async write loops stay in lockstep.
_RACE_LOSS_REASONS: frozenset[FileIOFailureReason] = frozenset(
    {FileIOFailureReason.POLICY_NO_OVERWRITE, FileIOFailureReason.FILE_LOCKED}
)


@dataclass(frozen=True)
class _SeededWriteParams:
    """Knobs for the seeded CREATE_NEW write loop other than the macro and content.

    Bundled into a frozen dataclass so the helper signature stays narrow and the loop
    body doesn't accidentally drop a parameter on retry. Mirrors the ``WriteFileRequest``
    fields the caller controls; the helper sets ``existing_file_policy`` itself (always
    ``FAIL`` — see the helper docstring for why).
    """

    encoding: str
    append: bool
    create_parents: bool
    file_metadata: SidecarContent | None
    coerce_extension_to_match_bytes: bool


def _seeded_create_new_write(
    macro_path: MacroPath,
    content: str | bytes,
    params: _SeededWriteParams,
) -> Path | None:
    """Atomically resolve+write a CREATE_NEW MacroPath whose only unresolved var is a padded index.

    Why this exists: the user-visible contract for ``{x:NN}`` saves is "every save in the
    sequence has consistent zero-padded width" (#4875). The naive flow — seed once, return
    a string, write with policy=CREATE_NEW — corrupts that contract under contention: if a
    racing writer takes the seeded slot before our open, OSManager's CREATE_NEW match arm
    falls into ``_convert_str_path_to_macro_with_index`` which suffix-injects an unpadded
    ``_1`` onto the resolved path (e.g. ``render_v003_1.png``). We need ``render_v004.png``.

    The fix: do seed+resolve+write atomically in a loop, using ``ExistingFilePolicy.FAIL``
    (no fallback) for the write so we can OBSERVE the race-loss and re-seed. Each iteration
    re-runs the scan, which now sees the racer's win and returns the next free index.

    Returns:
      Path on success.
      None if the macro doesn't qualify for seeding (caller does the normal write through
        ``_resolve_file_path`` + ``WriteFileRequest(CREATE_NEW)``). Three sub-cases produce
        None: (a) initial resolve succeeded — no seed needed; (b) initial failure isn't
        MISSING_REQUIRED_VARIABLES — caller bubbles the failure; (c) heuristic refused
        (no padding / multiple missing) — caller bubbles the failure. In (b) and (c) we
        return None because surfacing the original ``GetPathForMacroResultFailure`` as a
        ``FileLoadError`` is the caller's job at its own raise site, not ours.

    Raises:
      FileWriteError: on race-budget exhaustion, or on a non-recoverable write failure
        (anything other than POLICY_NO_OVERWRITE / FILE_LOCKED).
      FileLoadError: when the seeded retry's macro resolution fails. Surfaces the deeper
        upstream failure to the caller.

    Debugging:
      • ``FileWriteError`` with "race-loss exhausted" → either real contention beyond our
        budget (rare; ``MAX_INDEXED_CANDIDATES`` defaults to 1000) or the scan keeps
        returning the same index. Common cause for the latter: the project directory the
        macro references doesn't actually exist on disk yet, so the glob in
        ``_scan_for_next_available_index`` walks an empty parent and returns 1 every time.
      • Returns None when you expected a write → the macro didn't have an unresolved
        padded slot. Check the macro's ``parsed_macro.segments`` for a ``ParsedVariable``
        with a ``NumericPaddingFormat`` in its ``format_specs``.
    """
    # Lazy import: os_manager imports from this module (file.py defines File and
    # FileLoadError that os_manager uses), so we can't import MAX_INDEXED_CANDIDATES
    # at module load without a circular import. Resolved at first call instead.
    from griptape_nodes.retained_mode.managers.os_manager import MAX_INDEXED_CANDIDATES

    # PROBE: does this macro need seeding at all? An initial resolve tells us. Three
    # outcomes from this single dispatch are possible:
    #   • Success            → all vars bound; no seed needed; signal "not for us."
    #   • MISSING_REQUIRED   → maybe seedable; check the heuristic next.
    #   • Other failure      → not a seedable shape; signal "not for us" so the caller's
    #                          normal raise pathway surfaces the original failure.
    initial = GriptapeNodes.handle_request(
        GetPathForMacroRequest(parsed_macro=macro_path.parsed_macro, variables=macro_path.variables)
    )
    if isinstance(initial, GetPathForMacroResultSuccess):
        return None
    if not (
        isinstance(initial, GetPathForMacroResultFailure)
        and initial.failure_reason == PathResolutionFailureReason.MISSING_REQUIRED_VARIABLES
        and initial.missing_variables
    ):
        return None
    var = _find_padded_unresolved_required(macro_path, initial.missing_variables)
    if var is None:
        return None

    # SEEDABLE. Loop with FAIL writes; on race-loss re-seed.
    last_attempted_path: str | None = None
    for _attempt in range(MAX_INDEXED_CANDIDATES):
        # Each iteration re-runs the scan so the racer's recently-written file is visible
        # and the next index is gap-filled forward. ``_seed_index_for_create_new`` returns
        # None only if OSManager's scan itself fails (ambiguous index, glob error). In
        # that case we abandon the loop and surface the original missing-vars failure.
        seeded = _seed_index_for_create_new(macro_path, var)
        if seeded is None:
            raise _make_file_load_error(initial)

        # Resolve the seeded macro to a concrete absolute path. ProjectManager fills in
        # any project directories the same way it would have on a first call; we don't
        # carry forward any helper-side absolutization.
        resolved_path = _resolve_macro_path(seeded)
        last_attempted_path = resolved_path

        # FAIL write: atomic, no fallback. If another process has the slot, we'll get
        # POLICY_NO_OVERWRITE and re-seed. The user's *outer* policy is CREATE_NEW; we
        # internalize the index allocation, so FAIL is the right primitive here.
        write_request = WriteFileRequest(
            file_path=resolved_path,
            content=content,
            encoding=params.encoding,
            existing_file_policy=ExistingFilePolicy.FAIL,
            append=params.append,
            create_parents=params.create_parents,
            file_metadata=params.file_metadata,
            coerce_extension_to_match_bytes=params.coerce_extension_to_match_bytes,
        )
        write_result = GriptapeNodes.handle_request(write_request)
        if isinstance(write_result, WriteFileResultSuccess):
            return Path(write_result.final_file_path)

        # Failure. Race-loss reasons continue the loop; everything else propagates.
        if not isinstance(write_result, WriteFileResultFailure):
            # Defensive: should never happen, but if some future handler emits an
            # unexpected payload type we'd rather raise than busy-loop.
            msg = f"Unexpected response type {type(write_result).__name__} from WriteFileRequest"
            raise FileWriteError(failure_reason=FileIOFailureReason.UNKNOWN, result_details=msg)
        if write_result.failure_reason not in _RACE_LOSS_REASONS:
            raise FileWriteError(
                failure_reason=write_result.failure_reason,
                result_details=str(write_result.result_details),
                missing_variables=write_result.missing_variables,
            )
        # Fall through to next loop iteration: race-loss → re-seed against the racer's
        # newly-written file and try the next free index.

    # Budget exhausted. Per #4875 contract, callers see a real error (not a degraded
    # suffix-injected path) when contention overwhelms the retry budget.
    msg = (
        f"Attempted to write to seeded CREATE_NEW path. Failed because race-loss exhausted "
        f"after {MAX_INDEXED_CANDIDATES} attempts; last attempted path: {last_attempted_path!r}."
    )
    raise FileWriteError(failure_reason=FileIOFailureReason.IO_ERROR, result_details=msg)


async def _aseeded_create_new_write(
    macro_path: MacroPath,
    content: str | bytes,
    params: _SeededWriteParams,
) -> Path | None:
    """Async sibling of ``_seeded_create_new_write``.

    See that function's docstring for the per-stage flow and debugging notes. Keep the
    two in lockstep.
    """
    # Lazy import — see _seeded_create_new_write for the circular-import rationale.
    from griptape_nodes.retained_mode.managers.os_manager import MAX_INDEXED_CANDIDATES

    initial = await GriptapeNodes.ahandle_request(
        GetPathForMacroRequest(parsed_macro=macro_path.parsed_macro, variables=macro_path.variables)
    )
    if isinstance(initial, GetPathForMacroResultSuccess):
        return None
    if not (
        isinstance(initial, GetPathForMacroResultFailure)
        and initial.failure_reason == PathResolutionFailureReason.MISSING_REQUIRED_VARIABLES
        and initial.missing_variables
    ):
        return None
    var = _find_padded_unresolved_required(macro_path, initial.missing_variables)
    if var is None:
        return None

    last_attempted_path: str | None = None
    for _attempt in range(MAX_INDEXED_CANDIDATES):
        seeded = await _aseed_index_for_create_new(macro_path, var)
        if seeded is None:
            raise _make_file_load_error(initial)
        resolved_path = await _aresolve_macro_path(seeded)
        last_attempted_path = resolved_path

        write_request = WriteFileRequest(
            file_path=resolved_path,
            content=content,
            encoding=params.encoding,
            existing_file_policy=ExistingFilePolicy.FAIL,
            append=params.append,
            create_parents=params.create_parents,
            file_metadata=params.file_metadata,
            coerce_extension_to_match_bytes=params.coerce_extension_to_match_bytes,
        )
        write_result = await GriptapeNodes.ahandle_request(write_request)
        if isinstance(write_result, WriteFileResultSuccess):
            return Path(write_result.final_file_path)

        if not isinstance(write_result, WriteFileResultFailure):
            msg = f"Unexpected response type {type(write_result).__name__} from WriteFileRequest"
            raise FileWriteError(failure_reason=FileIOFailureReason.UNKNOWN, result_details=msg)
        if write_result.failure_reason not in _RACE_LOSS_REASONS:
            raise FileWriteError(
                failure_reason=write_result.failure_reason,
                result_details=str(write_result.result_details),
                missing_variables=write_result.missing_variables,
            )

    msg = (
        f"Attempted to write to seeded CREATE_NEW path. Failed because race-loss exhausted "
        f"after {MAX_INDEXED_CANDIDATES} attempts; last attempted path: {last_attempted_path!r}."
    )
    raise FileWriteError(failure_reason=FileIOFailureReason.IO_ERROR, result_details=msg)


# Pairs of suffixes that should be treated as equivalent when comparing a
# user-supplied filename extension against the canonical extension reported
# by ArtifactManager.sniff_extension. Keys and values are lowercase, no
# leading dot.
_EXTENSION_ALIASES: dict[str, str] = {
    "jpg": "jpeg",
    "jpeg": "jpeg",
    "tif": "tiff",
    "tiff": "tiff",
    "m4v": "mp4",
    "mp4": "mp4",
    "m4a": "m4a",
    "m4b": "m4a",
}


def canonical_extension(ext: str) -> str:
    """Return the canonical form of an on-disk extension for equivalence checks."""
    lowered = ext.lstrip(".").lower()
    return _EXTENSION_ALIASES.get(lowered, lowered)


class File:
    """Path-like object for reading and writing files via the retained mode API.

    The constructor stores a file reference without performing any I/O.
    Call instance methods like ``read_bytes()``, ``read_text()``,
    ``read_data_uri()``, ``write_bytes()``, or ``write_text()`` to perform
    the actual I/O.

    Supports MacroPath resolution: pass a MacroPath (which contains variables)
    or a plain string path.

    For a pre-configured write handle with baked-in write policy, use
    ``FileDestination`` instead.
    """

    def __init__(
        self,
        file_path: str | MacroPath,
        *,
        file_metadata: SidecarContent | None = None,
    ) -> None:
        """Store file reference. No I/O is performed.

        Plain strings containing macro variables (e.g. ``"{outputs}/file.png"``) are
        automatically wrapped in a MacroPath so they are resolved against the current
        project at read time.  Strings with no macro variables and already-constructed
        MacroPath objects are stored as-is.

        Args:
            file_path: Path to the file. Can be a plain string or a MacroPath
                (which contains macro variables).
            file_metadata: Optional caller-provided context to include in the sidecar
                metadata file alongside auto-collected workflow metadata.
        """
        self._file_metadata = file_metadata
        if isinstance(file_path, str):
            try:
                parsed = ParsedMacro(file_path)
            except MacroSyntaxError:
                self._file_path: str | MacroPath = file_path
            else:
                if parsed.get_variables():
                    self._file_path = MacroPath(parsed, {})
                else:
                    self._file_path = file_path
        else:
            self._file_path = file_path

    def resolve(self) -> str:
        """Resolve and return the absolute path string for this file.

        Useful when a caller needs the path for writing (not reading). Macro
        variables in the path are resolved against the current project at call time.

        Returns:
            Absolute path string.

        Raises:
            FileLoadError: If macro resolution fails (e.g. no project loaded).
        """
        return _resolve_file_path(self._file_path)

    @property
    def location(self) -> str:
        """Return the most portable string representation of this file's location.

        Returns the macro template (e.g. ``"{outputs}/image.png"``) when the file
        holds a macro path, otherwise the plain path string.  No I/O is performed.
        """
        if isinstance(self._file_path, MacroPath):
            return self._file_path.parsed_macro.template
        return self._file_path

    @property
    def name(self) -> str:
        """Return the filename component of this file's location.

        For example, a File holding ``"{outputs}/image.png"`` returns ``"image.png"``.
        """
        return Path(self.location).name

    def write_bytes(
        self,
        content: bytes,
        *,
        existing_file_policy: ExistingFilePolicy = ExistingFilePolicy.OVERWRITE,
        append: bool = False,
        create_parents: bool = True,
        coerce_extension_to_match_bytes: bool = True,
    ) -> Path:
        """Write bytes to the file.

        After the write, the bytes are sniffed for a known media format. If
        the sniffed format disagrees with the file's extension and
        ``coerce_extension_to_match_bytes`` is True (the default), the
        on-disk file is renamed so its suffix matches the sniffed format
        and a single warning is logged. If the flag is False, the write
        fails with ``FileWriteError(failure_reason=EXTENSION_MISMATCH)`` and
        no file is left on disk.

        Args:
            content: The bytes to write.
            existing_file_policy: How to handle an existing file. Ignored when
                append=True. Defaults to OVERWRITE.
            append: If True, append to an existing file. Defaults to False.
            create_parents: If True, create parent directories if missing.
                Defaults to True.
            coerce_extension_to_match_bytes: If True, rewrite the suffix to
                match the sniffed bytes; if False, fail on mismatch. Defaults
                to True.

        Returns:
            The actual path where the file was written.

        Raises:
            FileWriteError: If the file cannot be written, including the
                ``EXTENSION_MISMATCH`` failure when coercion is disabled.
        """
        return self._write_content(
            content,
            existing_file_policy=existing_file_policy,
            append=append,
            create_parents=create_parents,
            coerce_extension_to_match_bytes=coerce_extension_to_match_bytes,
        )

    async def awrite_bytes(
        self,
        content: bytes,
        *,
        existing_file_policy: ExistingFilePolicy = ExistingFilePolicy.OVERWRITE,
        append: bool = False,
        create_parents: bool = True,
        coerce_extension_to_match_bytes: bool = True,
    ) -> Path:
        """Async version of write_bytes().

        Args:
            content: The bytes to write.
            existing_file_policy: How to handle an existing file. Ignored when
                append=True. Defaults to OVERWRITE.
            append: If True, append to an existing file. Defaults to False.
            create_parents: If True, create parent directories if missing.
                Defaults to True.
            coerce_extension_to_match_bytes: If True, rewrite the suffix to
                match the sniffed bytes; if False, fail on mismatch. Defaults
                to True.

        Returns:
            The actual path where the file was written.

        Raises:
            FileWriteError: If the file cannot be written, including the
                ``EXTENSION_MISMATCH`` failure when coercion is disabled.
        """
        return await self._awrite_content(
            content,
            existing_file_policy=existing_file_policy,
            append=append,
            create_parents=create_parents,
            coerce_extension_to_match_bytes=coerce_extension_to_match_bytes,
        )

    def write_text(
        self,
        content: str,
        encoding: str = "utf-8",
        *,
        existing_file_policy: ExistingFilePolicy = ExistingFilePolicy.OVERWRITE,
        append: bool = False,
        create_parents: bool = True,
    ) -> Path:
        """Write text to the file.

        Args:
            content: The text to write.
            encoding: Text encoding to use when writing.
            existing_file_policy: How to handle an existing file. Ignored when
                append=True. Defaults to OVERWRITE.
            append: If True, append to an existing file. Defaults to False.
            create_parents: If True, create parent directories if missing.
                Defaults to True.

        Returns:
            The actual path where the file was written.

        Raises:
            FileWriteError: If the file cannot be written.
        """
        return self._write_content(
            content,
            encoding=encoding,
            existing_file_policy=existing_file_policy,
            append=append,
            create_parents=create_parents,
        )

    async def awrite_text(
        self,
        content: str,
        encoding: str = "utf-8",
        *,
        existing_file_policy: ExistingFilePolicy = ExistingFilePolicy.OVERWRITE,
        append: bool = False,
        create_parents: bool = True,
    ) -> Path:
        """Async version of write_text().

        Args:
            content: The text to write.
            encoding: Text encoding to use when writing.
            existing_file_policy: How to handle an existing file. Ignored when
                append=True. Defaults to OVERWRITE.
            append: If True, append to an existing file. Defaults to False.
            create_parents: If True, create parent directories if missing.
                Defaults to True.

        Returns:
            The actual path where the file was written.

        Raises:
            FileWriteError: If the file cannot be written.
        """
        return await self._awrite_content(
            content,
            encoding=encoding,
            existing_file_policy=existing_file_policy,
            append=append,
            create_parents=create_parents,
        )

    def read(self, encoding: str = "utf-8") -> FileContent:
        """Read the file and return a FileContent with content and metadata.

        Args:
            encoding: Text encoding to use if file is detected as text.

        Returns:
            A FileContent named tuple with content, mime_type, encoding, and size.

        Raises:
            FileLoadError: If the file cannot be read.
        """
        return self._read(encoding=encoding)

    async def aread(self, encoding: str = "utf-8") -> FileContent:
        """Async version of read().

        Args:
            encoding: Text encoding to use if file is detected as text.

        Returns:
            A FileContent named tuple with content, mime_type, encoding, and size.

        Raises:
            FileLoadError: If the file cannot be read.
        """
        return await self._aread(encoding=encoding)

    def read_bytes(self) -> bytes:
        """Read the file and return its content as bytes.

        If the content is a string, it is encoded using the file's encoding
        (falling back to utf-8 if encoding is None).

        Returns:
            The file content as bytes.

        Raises:
            FileLoadError: If the file cannot be read.
        """
        fc = self._read()
        return _to_bytes(fc)

    async def aread_bytes(self) -> bytes:
        """Async version of read_bytes().

        Returns:
            The file content as bytes.

        Raises:
            FileLoadError: If the file cannot be read.
        """
        fc = await self._aread()
        return _to_bytes(fc)

    def read_text(self, encoding: str = "utf-8") -> str:
        """Read the file and return its content as a string.

        Args:
            encoding: Text encoding to use for decoding the file.

        Returns:
            The file content as a string.

        Raises:
            FileLoadError: If the file cannot be read.
            TypeError: If the file content is binary (bytes).
        """
        fc = self._read(encoding=encoding)
        return _to_text(fc)

    async def aread_text(self, encoding: str = "utf-8") -> str:
        """Async version of read_text().

        Args:
            encoding: Text encoding to use for decoding the file.

        Returns:
            The file content as a string.

        Raises:
            FileLoadError: If the file cannot be read.
            TypeError: If the file content is binary (bytes).
        """
        fc = await self._aread(encoding=encoding)
        return _to_text(fc)

    def read_data_uri(self, fallback_mime: str = "application/octet-stream") -> str:
        """Read the file and return its content as a ``data:MIME;base64,...`` URI.

        Args:
            fallback_mime: MIME type to use when the file has no mime_type.

        Returns:
            A ``data:<mime>;base64,<b64>`` string.

        Raises:
            FileLoadError: If the file cannot be read.
        """
        fc = self._read()
        return _to_data_uri(fc, fallback_mime)

    async def aread_data_uri(self, fallback_mime: str = "application/octet-stream") -> str:
        """Async version of read_data_uri().

        Args:
            fallback_mime: MIME type to use when the file has no mime_type.

        Returns:
            A ``data:<mime>;base64,<b64>`` string.

        Raises:
            FileLoadError: If the file cannot be read.
        """
        fc = await self._aread()
        return _to_data_uri(fc, fallback_mime)

    def _read(self, encoding: str = "utf-8") -> FileContent:
        """Perform the sync file read and return a FileContent.

        Args:
            encoding: Text encoding to use if file is detected as text.

        Raises:
            FileLoadError: If the file cannot be read.
        """
        request = ReadFileRequest(
            file_path=_resolve_file_path(self._file_path),
            encoding=encoding,
            should_transform_image_content_to_thumbnail=False,
        )
        result = GriptapeNodes.handle_request(request)

        if isinstance(result, ReadFileResultFailure):
            raise FileLoadError(
                failure_reason=result.failure_reason,
                result_details=str(result.result_details),
            )

        success = cast("ReadFileResultSuccess", result)
        return FileContent(
            content=success.content,
            mime_type=success.mime_type,
            encoding=success.encoding,
            size=success.file_size,
        )

    async def _aread(self, encoding: str = "utf-8") -> FileContent:
        """Perform the async file read and return a FileContent.

        Args:
            encoding: Text encoding to use if file is detected as text.

        Raises:
            FileLoadError: If the file cannot be read.
        """
        request = ReadFileRequest(
            file_path=await _aresolve_file_path(self._file_path),
            encoding=encoding,
            should_transform_image_content_to_thumbnail=False,
        )
        result = await GriptapeNodes.ahandle_request(request)

        if isinstance(result, ReadFileResultFailure):
            raise FileLoadError(
                failure_reason=result.failure_reason,
                result_details=str(result.result_details),
            )

        success = cast("ReadFileResultSuccess", result)
        return FileContent(
            content=success.content,
            mime_type=success.mime_type,
            encoding=success.encoding,
            size=success.file_size,
        )

    def _write_content(  # noqa: PLR0913
        self,
        content: str | bytes,
        encoding: str = "utf-8",
        *,
        existing_file_policy: ExistingFilePolicy = ExistingFilePolicy.OVERWRITE,
        append: bool = False,
        create_parents: bool = True,
        coerce_extension_to_match_bytes: bool = True,
    ) -> Path:
        """Perform the sync file write.

        Args:
            content: Content to write (str or bytes).
            encoding: Text encoding to use when writing text content.
            existing_file_policy: How to handle an existing file.
            append: If True, append to an existing file.
            create_parents: If True, create parent directories if missing.
            coerce_extension_to_match_bytes: If True, the OSManager rewrites
                the on-disk suffix to match the sniffed bytes; if False, an
                ``EXTENSION_MISMATCH`` failure is returned on mismatch.

        Returns:
            The actual path where the file was written (may differ from the
            requested path if CREATE_NEW policy is in effect, or if the
            extension was coerced to match the byte content).

        Raises:
            FileWriteError: If the file cannot be written.
        """
        # Try the seeded CREATE_NEW write loop first when both conditions hold:
        #   • file_path is a MacroPath (the only shape where seeding ever fires)
        #   • policy is CREATE_NEW (the only policy that opts in — see #4875)
        # The helper returns None when the macro doesn't qualify (no padded slot, etc.),
        # in which case we fall through to the standard resolve+write path below. On
        # success it returns the written Path; on failure it raises directly.
        if isinstance(self._file_path, MacroPath) and existing_file_policy is ExistingFilePolicy.CREATE_NEW:
            seeded_result = _seeded_create_new_write(
                self._file_path,
                content,
                _SeededWriteParams(
                    encoding=encoding,
                    append=append,
                    create_parents=create_parents,
                    file_metadata=self._build_file_metadata(),
                    coerce_extension_to_match_bytes=coerce_extension_to_match_bytes,
                ),
            )
            if seeded_result is not None:
                return seeded_result

        resolved_path = _resolve_file_path(self._file_path)
        request = WriteFileRequest(
            file_path=resolved_path,
            content=content,
            encoding=encoding,
            existing_file_policy=existing_file_policy,
            append=append,
            create_parents=create_parents,
            file_metadata=self._build_file_metadata(),
            coerce_extension_to_match_bytes=coerce_extension_to_match_bytes,
        )
        result = GriptapeNodes.handle_request(request)

        if isinstance(result, WriteFileResultFailure):
            raise FileWriteError(
                failure_reason=result.failure_reason,
                result_details=str(result.result_details),
                missing_variables=result.missing_variables,
            )

        return Path(cast("WriteFileResultSuccess", result).final_file_path)

    async def _awrite_content(  # noqa: PLR0913
        self,
        content: str | bytes,
        encoding: str = "utf-8",
        *,
        existing_file_policy: ExistingFilePolicy = ExistingFilePolicy.OVERWRITE,
        append: bool = False,
        create_parents: bool = True,
        coerce_extension_to_match_bytes: bool = True,
    ) -> Path:
        """Async version of _write_content.

        Args:
            content: Content to write (str or bytes).
            encoding: Text encoding to use when writing text content.
            existing_file_policy: How to handle an existing file.
            append: If True, append to an existing file.
            create_parents: If True, create parent directories if missing.
            coerce_extension_to_match_bytes: If True, the OSManager rewrites
                the on-disk suffix to match the sniffed bytes; if False, an
                ``EXTENSION_MISMATCH`` failure is returned on mismatch.

        Returns:
            The actual path where the file was written (may differ from the
            requested path if CREATE_NEW policy is in effect, or if the
            extension was coerced to match the byte content).

        Raises:
            FileWriteError: If the file cannot be written.
        """
        # See ``_write_content`` above for the full rationale; this is the async mirror.
        if isinstance(self._file_path, MacroPath) and existing_file_policy is ExistingFilePolicy.CREATE_NEW:
            seeded_result = await _aseeded_create_new_write(
                self._file_path,
                content,
                _SeededWriteParams(
                    encoding=encoding,
                    append=append,
                    create_parents=create_parents,
                    file_metadata=self._build_file_metadata(),
                    coerce_extension_to_match_bytes=coerce_extension_to_match_bytes,
                ),
            )
            if seeded_result is not None:
                return seeded_result

        resolved_path = await _aresolve_file_path(self._file_path)
        request = WriteFileRequest(
            file_path=resolved_path,
            content=content,
            encoding=encoding,
            existing_file_policy=existing_file_policy,
            append=append,
            create_parents=create_parents,
            file_metadata=self._build_file_metadata(),
            coerce_extension_to_match_bytes=coerce_extension_to_match_bytes,
        )
        result = await GriptapeNodes.ahandle_request(request)

        if isinstance(result, WriteFileResultFailure):
            raise FileWriteError(
                failure_reason=result.failure_reason,
                result_details=str(result.result_details),
                missing_variables=result.missing_variables,
            )

        return Path(cast("WriteFileResultSuccess", result).final_file_path)

    def _build_file_metadata(self) -> SidecarContent | None:
        """Build SidecarContent from MacroPath variables and caller-provided metadata.

        Caller-provided metadata takes full precedence. If only a MacroPath is present
        (no caller metadata), the macro template and variables are captured as a minimal
        SituationMetadata.
        """
        if self._file_metadata is not None:
            return self._file_metadata
        if isinstance(self._file_path, MacroPath):
            return SidecarContent(
                situation=SituationMetadata(
                    macro=self._file_path.parsed_macro.template,
                    variables={k: str(v) for k, v in self._file_path.variables.items()},
                ),
            )
        return None


class FileDestination:
    """A pre-configured write handle for a file path.

    Bundles a file path with write policy so it can be passed around as a
    self-contained object. The consumer calls ``write_bytes()`` or
    ``write_text()`` without needing to know the policy details.

    For a lean path reference that also supports reading, use ``File`` instead.
    """

    def __init__(  # noqa: PLR0913
        self,
        file_path: str | MacroPath,
        *,
        existing_file_policy: ExistingFilePolicy = ExistingFilePolicy.OVERWRITE,
        append: bool = False,
        create_parents: bool = True,
        file_metadata: SidecarContent | None = None,
        coerce_extension_to_match_bytes: bool = True,
    ) -> None:
        """Store file path and write configuration. No I/O is performed.

        Args:
            file_path: Path to the file. Can be a plain string or a MacroPath
                (which contains macro variables).
            existing_file_policy: How to handle an existing file. Ignored when
                append=True. Defaults to OVERWRITE.
            append: If True, append to an existing file. Defaults to False.
            create_parents: If True, create parent directories if missing.
                Defaults to True.
            file_metadata: Optional caller-provided context to include in the sidecar
                metadata file alongside auto-collected workflow metadata.
            coerce_extension_to_match_bytes: If True (default), the OSManager
                rewrites the on-disk suffix to match the sniffed bytes when
                they disagree. If False, the write fails with an
                ``EXTENSION_MISMATCH`` error and no file is left on disk.
        """
        self._file = File(file_path, file_metadata=file_metadata)
        self._existing_file_policy = existing_file_policy
        self._append = append
        self._create_parents = create_parents
        self._coerce_extension_to_match_bytes = coerce_extension_to_match_bytes

    def resolve(self) -> str:
        """Resolve and return the absolute path string for this destination.

        Returns:
            Absolute path string.

        Raises:
            FileLoadError: If macro resolution fails (e.g. no project loaded).
        """
        return self._file.resolve()

    @property
    def location(self) -> str:
        return self._file.location

    @property
    def name(self) -> str:
        return self._file.name

    def write_bytes(self, content: bytes) -> File:
        """Write bytes to the file using the configured write policy.

        Args:
            content: The bytes to write.

        Returns:
            A File referencing the path where the content was written.

        Raises:
            FileWriteError: If the file cannot be written.
        """
        path = self._file.write_bytes(
            content,
            existing_file_policy=self._existing_file_policy,
            append=self._append,
            create_parents=self._create_parents,
            coerce_extension_to_match_bytes=self._coerce_extension_to_match_bytes,
        )
        return File(str(path))

    async def awrite_bytes(self, content: bytes) -> File:
        """Async version of write_bytes().

        Args:
            content: The bytes to write.

        Returns:
            A File referencing the path where the content was written.

        Raises:
            FileWriteError: If the file cannot be written.
        """
        path = await self._file.awrite_bytes(
            content,
            existing_file_policy=self._existing_file_policy,
            append=self._append,
            create_parents=self._create_parents,
            coerce_extension_to_match_bytes=self._coerce_extension_to_match_bytes,
        )
        return File(str(path))

    def write_text(self, content: str, encoding: str = "utf-8") -> File:
        """Write text to the file using the configured write policy.

        Args:
            content: The text to write.
            encoding: Text encoding to use when writing.

        Returns:
            A File referencing the path where the content was written.

        Raises:
            FileWriteError: If the file cannot be written.
        """
        path = self._file.write_text(
            content,
            encoding,
            existing_file_policy=self._existing_file_policy,
            append=self._append,
            create_parents=self._create_parents,
        )
        return File(str(path))

    async def awrite_text(self, content: str, encoding: str = "utf-8") -> File:
        """Async version of write_text().

        Args:
            content: The text to write.
            encoding: Text encoding to use when writing.

        Returns:
            A File referencing the path where the content was written.

        Raises:
            FileWriteError: If the file cannot be written.
        """
        path = await self._file.awrite_text(
            content,
            encoding,
            existing_file_policy=self._existing_file_policy,
            append=self._append,
            create_parents=self._create_parents,
        )
        return File(str(path))


@runtime_checkable
class FileDestinationProvider(Protocol):
    """Protocol for nodes that provide a FileDestination without serializing it over the wire."""

    @property
    def file_destination(self) -> FileDestination | None: ...


def _to_bytes(fc: FileContent) -> bytes:
    """Convert FileContent to bytes."""
    if isinstance(fc.content, bytes):
        return fc.content

    encode_with = fc.encoding if fc.encoding is not None else "utf-8"
    return fc.content.encode(encode_with)


def _to_text(fc: FileContent) -> str:
    """Convert FileContent to str.

    Raises:
        TypeError: If the content is binary.
    """
    if isinstance(fc.content, bytes):
        msg = f"Expected text content but got binary content (mime_type={fc.mime_type})."
        raise TypeError(msg)

    return fc.content


def _to_data_uri(fc: FileContent, fallback_mime: str) -> str:
    """Convert FileContent to a data URI string."""
    mime = fc.mime_type or fallback_mime
    raw_bytes = _to_bytes(fc)
    b64 = base64.b64encode(raw_bytes).decode("utf-8")
    return f"data:{mime};base64,{b64}"
