"""Finding file sequences, such as `render.0001.exr` through `render.0100.exr`."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from fileseq.exceptions import FileSeqException

from griptape_nodes.common.macro_parser import MacroSyntaxError, ParsedMacro
from griptape_nodes.common.sequences import (
    InvalidSubsetBoundsError,
    InvalidTemplateError,
    MissingItemError,
    Sequence,
    SequenceScanOptions,
)
from griptape_nodes.common.sequences.scan import (
    DirectoryListingError,
    PathMapping,
    scan_sequences,
    scan_sequences_from_filenames,
)
from griptape_nodes.retained_mode.engine import Engine, EngineScoped
from griptape_nodes.retained_mode.events.os_events import (
    DeduceSequencesFromFileListRequest,
    DeduceSequencesFromFileListResultFailure,
    DeduceSequencesFromFileListResultSuccess,
    FileIOFailureReason,
    ListDirectoryRequest,
    ListDirectoryResultFailure,
    ListDirectoryResultSuccess,
    ListDirectorySequencesRequest,
    ListDirectorySequencesResultFailure,
    ListDirectorySequencesResultSuccess,
    ScanSequencesRequest,
    ScanSequencesResultFailure,
    ScanSequencesResultSuccess,
    SequenceScanFailureReason,
)
from griptape_nodes.retained_mode.events.project_events import GetPathForMacroRequest, GetPathForMacroResultSuccess
from griptape_nodes.retained_mode.request_handlers import handles

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

# How many gap numbers to show inline in the ABORTED_AT_GAP `result_details`
# string before truncating the rest with "(+N more)". Larger lists overwhelm
# the artist's status panel; the full list lives on `missing_item_numbers`.
ABORTED_AT_GAP_PREVIEW_COUNT = 5


def describe_missing_items(numbers: list[int]) -> str:
    """Summarize a sequence's gaps for an ABORTED_AT_GAP message, such as "the sequence has a gap at item 7"."""
    gap_count = len(numbers)
    if gap_count == 1:
        return f"the sequence has a gap at item {numbers[0]}"
    sample = ", ".join(str(n) for n in numbers[:ABORTED_AT_GAP_PREVIEW_COUNT])
    if gap_count <= ABORTED_AT_GAP_PREVIEW_COUNT:
        suffix = ""
    else:
        suffix = f" (+ {gap_count - ABORTED_AT_GAP_PREVIEW_COUNT} more)"
    return f"the sequence has {gap_count} gaps: items {sample}{suffix}"


class FileSequences(EngineScoped):
    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        event_manager.register_request_handlers(self)

    @handles(ListDirectorySequencesRequest)
    def on_list_directory_sequences_request(
        self, request: ListDirectorySequencesRequest, workflow: WorkflowRuntime | None = None
    ) -> ResultPayload:
        """Handle a request to list only file sequences in a directory.

        Delegates to `on_list_directory_request` with `group_sequences=True` and
        re-wraps the result to expose only the detected sequences.
        """
        inner = ListDirectoryRequest(
            directory_path=request.directory_path,
            show_hidden=request.show_hidden,
            workspace_only=request.workspace_only,
            pattern=request.pattern,
            include_size=request.include_size,
            include_modified_time=request.include_modified_time,
            include_mime_type=request.include_mime_type,
            include_absolute_path=request.include_absolute_path,
            group_sequences=True,
            sequence_options=request.sequence_options,
        )
        result = self.engine.os_manager.on_list_directory_request(inner, workflow)
        if isinstance(result, ListDirectoryResultSuccess):
            return ListDirectorySequencesResultSuccess(
                sequences=result.sequences,
                current_path=result.current_path,
                is_workspace_path=result.is_workspace_path,
                result_details=result.result_details,
            )
        if isinstance(result, ListDirectoryResultFailure):
            return ListDirectorySequencesResultFailure(
                failure_reason=result.failure_reason,
                missing_item_numbers=result.missing_item_numbers,
                result_details=str(result.result_details),
            )
        return ListDirectorySequencesResultFailure(
            failure_reason=FileIOFailureReason.UNKNOWN,
            result_details="Unexpected result type from on_list_directory_request.",
        )

    @handles(DeduceSequencesFromFileListRequest)
    def on_deduce_sequences_from_file_list_request(self, request: DeduceSequencesFromFileListRequest) -> ResultPayload:
        """Handle a request to detect sequences from a caller-supplied file list.

        Groups input paths by parent directory, then calls
        `scan_sequences_from_filenames` per group. No directory I/O is
        performed.
        """
        try:
            options = request.sequence_options or SequenceScanOptions()
            dir_groups: dict[str, list[str]] = {}
            for fp in request.file_paths:
                p = Path(fp)
                raw_parent = str(p.parent)
                parent = "" if raw_parent == "." else raw_parent
                if parent not in dir_groups:
                    dir_groups[parent] = []
                dir_groups[parent].append(p.name)

            all_sequences: list[Sequence] = []
            for parent_dir, bare_names in dir_groups.items():
                seqs, _ = scan_sequences_from_filenames(bare_names, parent_dir, options)
                all_sequences.extend(seqs)
        except InvalidSubsetBoundsError as e:
            return DeduceSequencesFromFileListResultFailure(
                failure_reason=SequenceScanFailureReason.INVALID_BOUNDS,
                result_details=str(e),
            )
        except MissingItemError as e:
            summary = describe_missing_items(e.numbers)
            return DeduceSequencesFromFileListResultFailure(
                failure_reason=SequenceScanFailureReason.ABORTED_AT_GAP,
                missing_item_numbers=e.numbers,
                result_details=(
                    f"Attempted to deduce sequences from file list with policy=ABORT. Failed because {summary}."
                ),
            )
        except Exception as e:
            msg = f"Attempted to deduce sequences from file list. Failed with {type(e).__name__}: {e}"
            return DeduceSequencesFromFileListResultFailure(
                failure_reason=FileIOFailureReason.UNKNOWN,
                result_details=msg,
            )

        return DeduceSequencesFromFileListResultSuccess(
            sequences=all_sequences,
            result_details=(f"Deduced {len(all_sequences)} sequence(s) from {len(request.file_paths)} path(s)."),
        )

    @handles(ScanSequencesRequest)
    async def on_scan_sequences_request(  # noqa: PLR0911
        self, request: ScanSequencesRequest, workflow: WorkflowRuntime | None = None
    ) -> ResultPayload:
        """Handle a request to scan a path or pattern for file sequences.

        The handler does macro resolution itself, builds a `PathMapping`, and
        runs `scan_sequences` in a worker thread (`asyncio.to_thread`) so
        neither the directory listing (via `ListDirectoryRequest`, performed
        inside `scan_sequences`) nor fileseq parsing blocks the event loop.

        Routes failures to the appropriate taxonomy:
        - Macro syntax / resolution / shape problems → `INVALID_TEMPLATE`
          (sequence-semantic).
        - Subset bound problems → `INVALID_BOUNDS`.
        - ABORT-policy gaps → `ABORTED_AT_GAP` listing every offending item number.
        - OS-layer listing failures (directory not found, permission denied)
          propagate via `DirectoryListingError` and surface their original
          `FileIOFailureReason` so the underlying diagnostic isn't lost.
        """
        mapping_or_failure = self._build_scan_path_mapping(request.path, workflow)
        if isinstance(mapping_or_failure, ScanSequencesResultFailure):
            return mapping_or_failure
        mapping = mapping_or_failure

        try:
            outcome = await asyncio.to_thread(
                scan_sequences,
                mapping,
                mapping.filename_pattern,
                engine=self.engine,
                policy=request.policy,
                no_token_behavior=request.no_token_behavior,
                start=request.start_number,
                end=request.end_number,
            )
        except DirectoryListingError as e:
            return ScanSequencesResultFailure(
                failure_reason=e.failure_reason,
                result_details=e.result_details,
            )
        except InvalidSubsetBoundsError as e:
            return ScanSequencesResultFailure(
                failure_reason=SequenceScanFailureReason.INVALID_BOUNDS,
                result_details=str(e),
            )
        except InvalidTemplateError as e:
            return ScanSequencesResultFailure(
                failure_reason=SequenceScanFailureReason.INVALID_TEMPLATE,
                result_details=str(e),
            )
        except FileSeqException as e:
            return ScanSequencesResultFailure(
                failure_reason=SequenceScanFailureReason.INVALID_TEMPLATE,
                result_details=(
                    f"Attempted to scan sequences with path={request.path!r}. "
                    f"Failed because fileseq could not parse the path: {e}"
                ),
            )
        except MissingItemError as e:
            summary = describe_missing_items(e.numbers)
            return ScanSequencesResultFailure(
                failure_reason=SequenceScanFailureReason.ABORTED_AT_GAP,
                missing_item_numbers=e.numbers,
                result_details=(
                    f"Attempted to scan sequences with path={request.path!r}, policy=ABORT. Failed because {summary}."
                ),
            )

        # An empty result is a successful scan that simply found nothing —
        # not a failure. Callers that need to fail-fast can check `has_entries`.
        has_entries = any(seq.entries for seq in outcome.sequences)
        if has_entries:
            details = f"Found {len(outcome.sequences)} sequence(s)."
        else:
            details = f"Scanned path={request.path!r}; no matching sequence entries found."
        return ScanSequencesResultSuccess(
            sequences=outcome.sequences,
            has_entries=has_entries,
            directory_had_matching_files=outcome.directory_had_matching_files,
            discovered_first=outcome.discovered_first,
            discovered_last=outcome.discovered_last,
            result_details=details,
        )

    def _build_scan_path_mapping(  # noqa: PLR0911
        self, path: str, workflow: WorkflowRuntime | None
    ) -> PathMapping | ScanSequencesResultFailure:
        """Parse `path`, resolve any macro head, and build a `PathMapping`.

        The path is split into a directory portion and a filename portion at
        the last separator. The directory portion is resolved through
        `GetPathForMacroRequest` if it carries macros; the filename portion
        (which holds any sequence token) is preserved verbatim so the macro
        head survives the round trip.

        Returns either the assembled `PathMapping` or a `ScanSequencesResultFailure`
        ready to return up the stack.
        """
        if not path:
            return ScanSequencesResultFailure(
                failure_reason=SequenceScanFailureReason.INVALID_TEMPLATE,
                result_details="No path or pattern provided.",
            )

        sep_index = max(path.rfind("/"), path.rfind("\\"))
        if sep_index < 0:
            return ScanSequencesResultFailure(
                failure_reason=SequenceScanFailureReason.INVALID_TEMPLATE,
                result_details=(
                    f"`{path}` has no directory portion — point at a file or pattern "
                    "(e.g. `/work/render.####.png` or `{inputs}/render.####.png`)."
                ),
            )
        original_directory = path[:sep_index]
        filename_pattern = path[sep_index + 1 :]
        if not filename_pattern:
            return ScanSequencesResultFailure(
                failure_reason=SequenceScanFailureReason.INVALID_TEMPLATE,
                result_details=(f"`{path}` has no filename to scan — point at a file or pattern, not a directory."),
            )

        try:
            parsed_directory = ParsedMacro(original_directory)
        except MacroSyntaxError as e:
            return ScanSequencesResultFailure(
                failure_reason=SequenceScanFailureReason.INVALID_TEMPLATE,
                result_details=f"Invalid path or pattern `{path}`: {e}",
            )

        if not parsed_directory.get_variables():
            # No macros in the directory portion — treat it as a plain
            # absolute (or relative) path. Round-trip is a no-op.
            return PathMapping(
                original_directory=original_directory,
                resolved_directory=original_directory,
                filename_pattern=filename_pattern,
            )

        resolve_result = self.engine.project_manager.get_path_for_macro_in(
            workflow, GetPathForMacroRequest(parsed_macro=parsed_directory, variables={})
        )
        if not isinstance(resolve_result, GetPathForMacroResultSuccess):
            return ScanSequencesResultFailure(
                failure_reason=SequenceScanFailureReason.INVALID_TEMPLATE,
                result_details=(f"Couldn't resolve project variables in `{path}`: {resolve_result.result_details}"),
            )
        return PathMapping(
            original_directory=original_directory,
            resolved_directory=str(resolve_result.absolute_path),
            filename_pattern=filename_pattern,
        )
