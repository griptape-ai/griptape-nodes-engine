"""Directory scanning for sequences.

Worker behind `ScanSequencesRequest`. Takes a `PathMapping` (the macro-form
directory paired with its resolved-on-disk twin) plus a fileseq pattern,
lists the resolved directory via `ListDirectoryRequest`, hands the filenames
to `fileseq.findSequencesInList`, applies subset clipping and the chosen
missing-item policy, and returns a `ScanOutcome` carrying the inferred
sequences plus diagnostic flags. The intended caller is
`OSManager.on_scan_sequences_request`; other callers are welcome but should
prefer dispatching the bus request unless they have a strong reason to
bypass the worker-thread / async-dispatch path.

The `PathMapping` lets the scanner do all I/O against the resolved absolute
directory while emitting Sequence objects whose `directory` and entry
`path` fields stay in the caller's macro form. That preserves portability
across machines where `{inputs}` resolves to different absolute roots.

All filesystem I/O is routed through the engine's request bus — this module
never calls `os.scandir`, `os.walk`, or `pathlib.Path.glob` directly.
fileseq's filesystem-touching helpers (`findSequencesOnDisk`,
`findSequenceOnDisk`) are not used.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import NamedTuple, Protocol, runtime_checkable

from fileseq.constants import PAD_STYLE_HASH1
from fileseq.filesequence import FileSequence

from griptape_nodes.common.sequences.models import (
    InvalidSubsetBoundsError,
    InvalidTemplateError,
    MissingItemPolicy,
    NoTokenBehavior,
    Sequence,
    SequenceScanOptions,
)
from griptape_nodes.common.sequences.policies import PolicyContext, apply_policy
from griptape_nodes.retained_mode.events.os_events import (
    FileIOFailureReason,
    ListDirectoryRequest,
    ListDirectoryResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

logger = logging.getLogger("griptape_nodes")

# Mandatory pad style: each `#` = 1 zero, matching Nuke. fileseq's default
# (HASH4) treats each `#` as 4 zeros, which would silently break templates.
PAD_STYLE = PAD_STYLE_HASH1


class ScanOutcome(NamedTuple):
    """The full result of a sequence scan, including diagnostics for empty results.

    `directory_had_matching_files` and `discovered_first/last` let callers
    distinguish "wrong path / template" from "right path, padding mismatch /
    subset clipped out everything" without inspecting log strings.
    """

    sequences: list[Sequence]
    directory_had_matching_files: bool
    discovered_first: int | None
    discovered_last: int | None


class DirectoryListingError(Exception):
    """Raised when the inner `ListDirectoryRequest` returns a failure.

    Carries the listing's own `FileIOFailureReason` and `result_details` so
    the request handler can surface them through `ScanSequencesResultFailure`
    without losing the OS-level diagnostic.
    """

    def __init__(self, failure_reason: FileIOFailureReason, result_details: str) -> None:
        super().__init__(result_details)
        self.failure_reason = failure_reason
        self.result_details = result_details


@dataclass(frozen=True)
class _PresentNumbers:
    """Map of item numbers to their (caller-shaped) paths, plus the dropped count.

    `by_number[N]` is a string in the *caller's* path shape — macro-form when
    the caller supplied a macro, plain absolute otherwise. The mapping back
    to the resolved on-disk path is done up front by `PathMapping` and is
    not needed past the listing step.
    """

    by_number: dict[int, str]
    dropped_negatives: int


class _ActiveRange(NamedTuple):
    """The [first, last] bounds after subset clipping is applied."""

    first: int
    last: int


@dataclass(frozen=True)
class PathMapping:
    """Pairs a macro-form directory with its resolved-on-disk twin.

    The scanner does all I/O against `resolved_directory` (an absolute path
    fileseq and `ListDirectoryRequest` can use) but emits paths through
    `to_caller_path` so the artist's macro shape survives the round trip.

    For plain absolute inputs (no macros), `original_directory` and
    `resolved_directory` are equal and `to_caller_path` is effectively a
    pass-through.

    Attributes:
        original_directory: Directory portion as the caller wrote it. May be
            macro-form (`{inputs}/xyz`) or plain absolute.
        resolved_directory: Absolute on-disk directory after macro resolution.
        filename_pattern: The fileseq filename component (e.g. `render.####.png`).
            Joined onto `original_directory` to produce the original `path`.
    """

    original_directory: str
    resolved_directory: str
    filename_pattern: str

    def to_caller_path(self, filename: str) -> str:
        """Return `original_directory + sep + filename`, preserving macro form."""
        if not self.original_directory:
            return filename
        # Use forward slash unconditionally — macros and engine-resolved paths
        # both speak `/` internally; the OS layer canonicalizes when it does
        # I/O. This avoids `pathlib.Path` mishandling `{inputs}` segments on
        # Windows.
        return f"{self.original_directory}/{filename}"


def scan_sequences(  # noqa: PLR0913
    mapping: PathMapping,
    pattern: str | FileSequence,
    *,
    policy: MissingItemPolicy = MissingItemPolicy.SPLIT,
    no_token_behavior: NoTokenBehavior = NoTokenBehavior.SINGLE_FILE,
    start: int | None = None,
    end: int | None = None,
) -> ScanOutcome:
    """Find sequences matching `pattern` inside `mapping.resolved_directory`.

    The intended entry point is `ScanSequencesRequest` on the engine's event
    bus; that request's handler invokes this function via `asyncio.to_thread()`
    so neither the directory listing nor fileseq parsing blocks the event loop.
    Callers that bypass the request lose the worker-thread offload — only do so
    if you've already taken the I/O off the event loop yourself.

    Args:
        mapping: The directory pair to scan. The scanner reads from
            `mapping.resolved_directory` and emits paths in the shape of
            `mapping.original_directory` (so macros stay macros).
        pattern: Either a fileseq pattern string (e.g. "render.####.exr") or
            a pre-constructed `FileSequence` whose basename + padding +
            extension act as the filter. Sequence tokens are interpreted in
            HASH1 mode regardless of pattern syntax.
        policy: How to handle gaps within the matched range. SPLIT yields one
            Sequence per contiguous run; the others yield exactly one
            Sequence with policy-driven gap fills (or omissions for SKIP, or
            a `MissingItemError` for ABORT).
        no_token_behavior: How to handle a `pattern` with zero sequence
            tokens. `SINGLE_FILE` (default) treats the whole filename as a
            literal — one-item sequence if the file exists, empty otherwise.
            `EXPLORE_SEQUENCE` lets fileseq read digits in the filename as an
            implicit sequence token (`render.0002.png` → one frame of a
            `render.####.png` sequence; the scan walks every matching
            sibling). `REJECT` raises `InvalidTemplateError` when the
            pattern has no token, useful for strict workflows.
        start: Optional lower bound (inclusive) for the active subset. Items
            below this are dropped from output. Must be >= 0 if supplied.
        end: Optional upper bound (inclusive) for the active subset. Items
            above this are dropped from output. Must be >= start if both
            supplied.

    Returns:
        `ScanOutcome` carrying the inferred sequences plus diagnostic flags
        (whether the directory had any files matching the basename/extension
        shape, and the on-disk discovered range when sequences were inferred).
        `sequences` is empty if the directory contains no matching files, the
        padding doesn't line up, or the active subset clipped everything out;
        the diagnostic fields tell the caller which case fired.

    Raises:
        DirectoryListingError: If the inner `ListDirectoryRequest` returns
            a failure (directory not found, permission denied, etc.). Carries
            the original `FileIOFailureReason`.
        InvalidSubsetBoundsError: If `start` < 0 or `end` < `start`.
        InvalidTemplateError: If `pattern` contains more than one sequence
            token, or has zero tokens and `no_token_behavior` is `REJECT`.
        MissingItemError: If `policy` is ABORT and a gap is found inside the
            active range.

    Negative numbers on disk are filtered out before policy is applied;
    `Sequence.dropped_negative_number_count` records how many were skipped.
    """
    _validate_subset_bounds(start, end)
    target = _coerce_target_pattern(pattern, no_token_behavior=no_token_behavior)

    relevant = _list_pattern_matching_filenames(mapping.resolved_directory, target)
    directory_had_matching_files = bool(relevant)
    if not relevant:
        return ScanOutcome(
            sequences=[],
            directory_had_matching_files=False,
            discovered_first=None,
            discovered_last=None,
        )

    present = _collect_present_numbers(mapping, target, relevant)
    if not present.by_number:
        # Directory had files matching the basename/extension shape, but
        # fileseq grouped them at a different padding than the target's
        # zfill. Surface the diagnostic flag so the caller can say "the
        # padding is wrong" instead of a generic "no matches".
        return ScanOutcome(
            sequences=[],
            directory_had_matching_files=directory_had_matching_files,
            discovered_first=None,
            discovered_last=None,
        )

    discovered_first = min(present.by_number)
    discovered_last = max(present.by_number)
    active = _compute_active_range(start, end, discovered_first, discovered_last)
    if active.first > active.last:
        # Active subset clipped every present item out. Surface the
        # discovered range so the caller can show "asked for 90..100 but
        # disk has 1..7".
        return ScanOutcome(
            sequences=[],
            directory_had_matching_files=directory_had_matching_files,
            discovered_first=discovered_first,
            discovered_last=discovered_last,
        )

    sequences = apply_policy(
        PolicyContext(
            fseq=target,
            present_numbers=present.by_number,
            directory=mapping.original_directory,
            policy=policy,
            first=active.first,
            last=active.last,
            discovered_first=discovered_first,
            discovered_last=discovered_last,
            dropped_negative_number_count=present.dropped_negatives,
        )
    )
    return ScanOutcome(
        sequences=sequences,
        directory_had_matching_files=directory_had_matching_files,
        discovered_first=discovered_first,
        discovered_last=discovered_last,
    )


def _validate_subset_bounds(start: int | None, end: int | None) -> None:
    """Reject negative start values and end < start ranges before any work."""
    if start is not None and start < 0:
        msg = f"Attempted to validate sequence subset bounds with start={start}. Failed because start must be >= 0."
        raise InvalidSubsetBoundsError(msg)
    if start is not None and end is not None and end < start:
        msg = (
            f"Attempted to validate sequence subset bounds with start={start}, end={end}. "
            f"Failed because end must be >= start."
        )
        raise InvalidSubsetBoundsError(msg)


@runtime_checkable
class TargetPattern(Protocol):
    """The slice of `fileseq.FileSequence`'s API that the scanner actually uses.

    A real `FileSequence` already satisfies this protocol; `_LiteralTarget`
    implements it for token-less inputs where fileseq's parse misreads digits
    in the filename as a sequence (e.g. `render.0002.png` parses with zfill=4
    even though the user typed it as a literal name).
    """

    def basename(self) -> str: ...
    def extension(self) -> str: ...
    def padding(self) -> str: ...
    def zfill(self) -> int: ...


@dataclass(frozen=True)
class _LiteralTarget:
    """Literal-filename target for inputs with zero sequence tokens.

    fileseq parses `render.0002.png` as a sequence with zfill=4 — its view of
    the basename and extension is wrong for our purposes (we want the entire
    filename treated as one literal). `_LiteralTarget` keeps the full filename
    in `_filename` and reports `basename=_filename, extension="", zfill=0`.
    The downstream literal-file branch in `_collect_present_numbers` matches
    on `basename + extension` (== `_filename`), so the shim slots in cleanly.
    """

    _filename: str

    def basename(self) -> str:
        return self._filename

    def extension(self) -> str:
        return ""

    def padding(self) -> str:
        return ""

    def zfill(self) -> int:
        return 0


def _coerce_target_pattern(
    pattern: str | FileSequence,
    *,
    no_token_behavior: NoTokenBehavior = NoTokenBehavior.SINGLE_FILE,
) -> TargetPattern:
    """Construct a fileseq-or-literal target from the caller's pattern.

    For string inputs, also rejects multi-token templates up-front. fileseq's
    own behavior here is uneven: it raises on some forms (`v##_f####.exr`)
    but silently accepts others (`render.##.##.exr` parses as a single
    `##.##` padding) — neither produces what the user meant. Catching it
    here gives a clear error before any work is done.

    Token-less inputs are dispatched on `no_token_behavior`:
    - `SINGLE_FILE` returns a `_LiteralTarget`. The whole filename is treated
      as one literal name; sibling files in the directory are ignored.
    - `EXPLORE_SEQUENCE` falls through to `FileSequence(pattern)`, letting
      fileseq read digits in the filename as an implicit sequence (so
      `render.0002.png` is one frame of an inferred `render.####.png`).
    - `REJECT` raises `InvalidTemplateError` so the caller learns to add an
      explicit token.

    A pre-constructed `FileSequence` short-circuits all of this and is
    returned as-is.
    """
    if isinstance(pattern, FileSequence):
        return pattern
    token_count = _count_sequence_tokens(pattern)
    if token_count > 1:
        msg = (
            f"Attempted to parse fileseq template {pattern!r}. "
            f"Failed because it contains {token_count} sequence tokens; only one is supported. "
            f"Multi-token templates like 'v##_f####.exr' are not handled correctly by fileseq."
        )
        raise InvalidTemplateError(msg)
    if token_count == 0:
        match no_token_behavior:
            case NoTokenBehavior.SINGLE_FILE:
                return _LiteralTarget(_filename=pattern)
            case NoTokenBehavior.REJECT:
                msg = (
                    f"Attempted to parse fileseq template {pattern!r}. "
                    f"Failed because it has no sequence token (`####`, `%04d`, `@@@`, or `$F4`); "
                    f"add a token to scan the surrounding sequence, or set `no_token_behavior` "
                    f"to `SINGLE_FILE` to scan this exact filename."
                )
                raise InvalidTemplateError(msg)
            case NoTokenBehavior.EXPLORE_SEQUENCE:
                pass  # fall through to FileSequence parse below
    return FileSequence(pattern, pad_style=PAD_STYLE)


# Recognized sequence-token forms, in fileseq's HASH1 idiom:
# - hash runs: `#`, `##`, `####`, ...
# - printf:    `%d`, `%4d`, `%04d`
# - at runs:   `@`, `@@`, ... (Houdini/RV)
# - $F tokens: `$F`, `$F4` (Houdini variable form)
_TOKEN_PATTERN = re.compile(r"#+|%0?\d*d|@+|\$F\d*")


def _count_sequence_tokens(pattern: str) -> int:
    """Return the number of sequence tokens in `pattern`.

    Used to reject multi-token templates before they reach fileseq.
    """
    return len(_TOKEN_PATTERN.findall(pattern))


def _list_pattern_matching_filenames(directory: str, target: TargetPattern) -> list[str]:
    """List `directory` and keep only files whose name matches the target shape.

    Filters by basename prefix and extension suffix before fileseq sees the
    list — avoids polluting fileseq's grouping with unrelated files (which
    would produce noise sequences). For a `_LiteralTarget`, basename is the
    full filename and extension is empty, so this collapses to an exact-name
    match — same behavior `_collect_literal_single_file` then applies.
    """
    filenames = _list_directory_filenames(directory)
    if not filenames:
        return []
    target_basename = target.basename()
    target_extension = target.extension()
    return [name for name in filenames if name.startswith(target_basename) and name.endswith(target_extension)]


def _collect_present_numbers(
    mapping: PathMapping,
    target: TargetPattern,
    relevant_filenames: list[str],
) -> _PresentNumbers:
    """Run fileseq inference on `relevant_filenames` and collect number->caller-path entries.

    Drops negatives, filters to sequences whose padding matches `target`, and
    rebuilds each entry's path through `mapping.to_caller_path` so the macro
    head (when supplied) survives end-to-end.

    Token-less inputs route through `_collect_literal_single_file` instead.
    The dispatch keys off `target.zfill() == 0`; a `_LiteralTarget` reports
    zero deliberately so this branch fires for `render.0002.png`-style names.
    """
    if target.zfill() == 0:
        return _collect_literal_single_file(mapping, target, relevant_filenames)

    inferred = FileSequence.findSequencesInList(relevant_filenames, pad_style=PAD_STYLE)
    matching = [s for s in inferred if s.zfill() == target.zfill()]
    if not matching:
        return _PresentNumbers(by_number={}, dropped_negatives=0)

    present: dict[int, str] = {}
    dropped = 0
    for seq in matching:
        frame_set = seq.frameSet()
        if frame_set is None:
            continue
        for number in frame_set:
            # Subframes (Decimal/float) aren't enabled (allow_subframes is
            # False by default) so this is always an int in practice;
            # narrow the type for pyright.
            if not isinstance(number, int):
                continue
            if number < 0:
                dropped += 1
                continue
            present[number] = mapping.to_caller_path(seq.frame(number))

    if dropped:
        logger.warning(
            "scan_sequences: dropped %d negative number(s) from %r",
            dropped,
            f"{target.basename()}{target.padding()}{target.extension()}",
        )
    return _PresentNumbers(by_number=present, dropped_negatives=dropped)


def _collect_literal_single_file(
    mapping: PathMapping,
    target: TargetPattern,
    relevant_filenames: list[str],
) -> _PresentNumbers:
    """Treat a token-less target as a one-item sequence.

    When `target` has no sequence token (zfill = 0), the only "match" is the
    literal `basename + extension`. For `_LiteralTarget` that's the full
    filename the user typed; for a real `FileSequence` it's the basename and
    empty extension fileseq inferred. If that filename appears in the listing,
    return it as item #1 so downstream code emits a 1-item Sequence; otherwise
    return an empty mapping and let `_scan_sequences` route to its normal
    empty-result diagnostics.
    """
    literal_name = f"{target.basename()}{target.extension()}"
    if literal_name not in relevant_filenames:
        return _PresentNumbers(by_number={}, dropped_negatives=0)
    return _PresentNumbers(
        by_number={1: mapping.to_caller_path(literal_name)},
        dropped_negatives=0,
    )


def _compute_active_range(
    start: int | None,
    end: int | None,
    discovered_first: int,
    discovered_last: int,
) -> _ActiveRange:
    """Clip the discovered range to the optional [start, end] subset bounds."""
    if start is None:
        active_first = discovered_first
    else:
        active_first = max(start, discovered_first)
    if end is None:
        active_last = discovered_last
    else:
        active_last = min(end, discovered_last)
    return _ActiveRange(first=active_first, last=active_last)


def scan_sequences_from_filenames(
    filenames: list[str],
    directory: str,
    options: SequenceScanOptions | None = None,
) -> tuple[list[Sequence], set[str]]:
    """Detect all sequences within a pre-existing list of bare filenames.

    No filesystem I/O is performed — callers are responsible for supplying
    the filenames themselves (e.g. from a prior directory listing). The
    second return value is the set of bare filenames that were grouped into
    at least one ``Sequence``; callers can use it to filter those entries
    out of a directory listing.

    Args:
        filenames: Bare filenames (no directory prefix) to inspect.
        directory: The directory string stored in emitted ``Sequence.directory``
            and ``SequenceEntry.path`` fields. May be macro-form or absolute.
        options: Detection and policy options. Defaults to
            ``SequenceScanOptions()`` (SKIP policy, REJECT token-less files).

    Returns:
        ``(sequences, consumed_filenames)`` where ``consumed_filenames`` is
        the set of bare filenames that belong to at least one returned
        ``Sequence``.

    Raises:
        InvalidSubsetBoundsError: If ``options.start_number`` < 0 or
            ``options.end_number`` < ``options.start_number``.
        MissingItemError: If ``options.policy`` is ABORT and a gap is found
            inside the active range.
    """
    if options is None:
        options = SequenceScanOptions()

    _validate_subset_bounds(options.start_number, options.end_number)

    all_detected = FileSequence.findSequencesInList(filenames, pad_style=PAD_STYLE)

    result_sequences: list[Sequence] = []
    consumed_filenames: set[str] = set()

    for fseq in all_detected:
        frame_set = fseq.frameSet()
        if frame_set is None or not list(frame_set):
            continue
        if options.no_token_behavior == NoTokenBehavior.REJECT and fseq.zfill() == 0:
            continue
        if options.padding is not None and fseq.zfill() != options.padding:
            continue

        present_numbers, dropped, bare_names = _collect_present_numbers_from_fseq(fseq, directory)
        if not present_numbers:
            continue

        consumed_filenames.update(bare_names)

        discovered_first = min(present_numbers)
        discovered_last = max(present_numbers)
        active = _compute_active_range(options.start_number, options.end_number, discovered_first, discovered_last)
        if active.first > active.last:
            continue

        result_sequences.extend(
            apply_policy(
                PolicyContext(
                    fseq=fseq,
                    present_numbers=present_numbers,
                    directory=directory,
                    policy=options.policy,
                    first=active.first,
                    last=active.last,
                    discovered_first=discovered_first,
                    discovered_last=discovered_last,
                    dropped_negative_number_count=dropped,
                )
            )
        )

    return result_sequences, consumed_filenames


def _collect_present_numbers_from_fseq(
    fseq: FileSequence,
    directory: str,
) -> tuple[dict[int, str], int, set[str]]:
    """Build a present-numbers map from a ``FileSequence``'s frame set.

    Returns ``(present_numbers, dropped_negative_count, bare_filenames)``
    where ``present_numbers`` maps frame number to the full path string
    (using ``directory`` as the prefix) and ``bare_filenames`` is the set
    of bare names that were consumed.
    """
    present_numbers: dict[int, str] = {}
    bare_filenames: set[str] = set()
    dropped = 0
    frame_set = fseq.frameSet()
    if frame_set is None:
        return present_numbers, dropped, bare_filenames
    for n in frame_set:
        if not isinstance(n, int):
            continue
        if n < 0:
            dropped += 1
            continue
        bare = fseq.frame(n)
        full = f"{directory}/{bare}" if directory else bare
        present_numbers[n] = full
        bare_filenames.add(bare)
    return present_numbers, dropped, bare_filenames


def _list_directory_filenames(directory: str) -> list[str]:
    """List `directory` via ListDirectoryRequest, returning bare filenames.

    Raises `DirectoryListingError` on any listing failure, carrying the
    underlying `FileIOFailureReason` so the request handler can surface
    "directory not found" / "permission denied" / etc. through
    `ScanSequencesResultFailure` without losing the OS-level diagnostic.

    Suppresses client toasts via `broadcast_result=False` since "directory
    not found" is a normal outcome of a user-supplied template.
    """
    result = GriptapeNodes.handle_request(
        ListDirectoryRequest(
            directory_path=directory,
            workspace_only=False,
            show_hidden=False,
            include_size=False,
            include_modified_time=False,
            include_mime_type=False,
            include_absolute_path=False,
            broadcast_result=False,
            group_sequences=False,
        )
    )
    if not isinstance(result, ListDirectoryResultSuccess):
        raise DirectoryListingError(
            failure_reason=result.failure_reason,  # pyright: ignore[reportAttributeAccessIssue]
            result_details=str(result.result_details),
        )
    # Skip directories — we want files only.
    return [entry.name for entry in result.entries if not entry.is_dir]
