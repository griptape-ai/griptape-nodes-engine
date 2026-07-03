"""Gap-handling policy logic for sequences.

Pure functions that take a `fileseq.FileSequence` plus the present-numbers
map and produce a list of `Sequence` objects shaped according to the chosen
policy. No I/O, no fileseq state mutation — just transformation.

Path values flowing through this module are *strings in the caller's shape*
(macro-form when the caller supplied a macro, plain absolute otherwise). The
scanner's `PathMapping` does the resolved↔caller conversion before handing
the present-numbers dict in here, so policies don't touch macros directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from griptape_nodes.common.sequences.models import (
    MissingItemError,
    MissingItemPolicy,
    Sequence,
    SequenceEntry,
)

if TYPE_CHECKING:
    from griptape_nodes.common.sequences.scan import TargetPattern


@dataclass(frozen=True)
class PolicyContext:
    """Bundle of inputs for `apply_policy`.

    Groups the unchanging context (range, discovered range, fileseq-or-literal
    target, drop count) so callers and helpers can pass one object instead of
    nine keyword arguments. `fseq` is the structural protocol satisfied by
    both `fileseq.FileSequence` and `_LiteralTarget`; only its metadata
    accessors (basename / padding / extension / zfill) are read here.
    """

    fseq: TargetPattern
    present_numbers: dict[int, str]
    directory: str
    policy: MissingItemPolicy
    first: int
    last: int
    discovered_first: int
    discovered_last: int
    dropped_negative_number_count: int


def apply_policy(context: PolicyContext) -> list[Sequence]:
    """Build the final list of Sequences according to `context.policy`.

    `context.present_numbers` maps each present integer key to its
    caller-shaped path string. Numbers may sit inside or outside [first,
    last]; only those inside the active range are surfaced. SPLIT returns
    multiple sequences (one per contiguous run). All other policies return
    exactly one sequence.

    `context.fseq` is used only to read its formatting metadata (basename,
    padding, extension, zfill); it is not mutated.
    """
    if context.policy is MissingItemPolicy.SPLIT:
        return _apply_split(context)
    return [_apply_single(context)]


def _apply_split(context: PolicyContext) -> list[Sequence]:
    """SPLIT: emit one Sequence per contiguous run of present numbers in [first, last]."""
    in_range_numbers = sorted(n for n in context.present_numbers if context.first <= n <= context.last)
    if not in_range_numbers:
        return []

    runs = _contiguous_runs(in_range_numbers)
    return [_build_split_sequence(run, context) for run in runs]


def _build_split_sequence(run: list[int], context: PolicyContext) -> Sequence:
    """Build one Sequence from a contiguous run of present numbers."""
    entries = [_present_entry(context.fseq, number, context.present_numbers[number]) for number in run]
    return Sequence(
        entries=entries,
        first=run[0],
        last=run[-1],
        discovered_first=context.discovered_first,
        discovered_last=context.discovered_last,
        padding=context.fseq.zfill(),
        pattern=_canonical_pattern(context.fseq),
        directory=context.directory,
        policy=MissingItemPolicy.SPLIT,
        dropped_negative_number_count=context.dropped_negative_number_count,
        present_numbers=set(run),
    )


def _apply_single(context: PolicyContext) -> Sequence:
    """SKIP / FILL_NEAREST: emit one Sequence over [first, last]; ABORT raises with every gap."""
    in_range_present = {n: p for n, p in context.present_numbers.items() if context.first <= n <= context.last}

    if context.policy is MissingItemPolicy.ABORT:
        # Collect all gaps in one pass so the failure payload can surface every
        # missing item at once. Letting `_gap_entry` raise on the first gap (the
        # old behavior) made artists fix gaps one-re-run-at-a-time.
        missing = [n for n in range(context.first, context.last + 1) if n not in in_range_present]
        if missing:
            raise MissingItemError(missing)

    entries: list[SequenceEntry] = []
    for number in range(context.first, context.last + 1):
        if number in in_range_present:
            entries.append(_present_entry(context.fseq, number, in_range_present[number]))
            continue
        gap_entry = _gap_entry(number, context.policy, context.fseq, in_range_present)
        if gap_entry is not None:
            entries.append(gap_entry)

    return Sequence(
        entries=entries,
        first=context.first,
        last=context.last,
        discovered_first=context.discovered_first,
        discovered_last=context.discovered_last,
        padding=context.fseq.zfill(),
        pattern=_canonical_pattern(context.fseq),
        directory=context.directory,
        policy=context.policy,
        dropped_negative_number_count=context.dropped_negative_number_count,
        present_numbers=set(in_range_present.keys()),
    )


def _gap_entry(
    number: int,
    policy: MissingItemPolicy,
    fseq: TargetPattern,
    in_range_present: dict[int, str],
) -> SequenceEntry | None:
    """Build the SequenceEntry for a missing item, or None to omit it.

    None is returned for SKIP (which drops gaps from `entries`) and for
    FILL_NEAREST when there's no neighbor at all (empty in-range present set).

    ABORT is not handled here — `_apply_single` collects all gaps in a single
    pass and raises `MissingItemError` itself before this function is called.
    """
    match policy:
        case MissingItemPolicy.SKIP:
            return None
        case MissingItemPolicy.FILL_NEAREST:
            neighbor_path = _nearest_path(number, in_range_present)
            if neighbor_path is None:
                return None
            return SequenceEntry(
                number=number,
                padded_number=_format_number(fseq, number),
                path=neighbor_path,
            )
        case _:
            msg = f"Unknown missing-item policy: {policy}"
            raise ValueError(msg)


def _contiguous_runs(sorted_numbers: list[int]) -> list[list[int]]:
    """Group an already-sorted number list into contiguous integer runs."""
    if not sorted_numbers:
        return []
    runs: list[list[int]] = [[sorted_numbers[0]]]
    for number in sorted_numbers[1:]:
        if number == runs[-1][-1] + 1:
            runs[-1].append(number)
        else:
            runs.append([number])
    return runs


def _nearest_path(number: int, present: dict[int, str]) -> str | None:
    """Find the nearest present number's path. Backward-first, then forward.

    Per the spec: when a missing item needs a NEAREST fill, we prefer the
    closest *earlier* present number. Only if no earlier number exists do we
    look forward. Returns None only if `present` is empty.
    """
    if not present:
        return None
    earlier = max((n for n in present if n < number), default=None)
    if earlier is not None:
        return present[earlier]
    later = min((n for n in present if n > number), default=None)
    if later is not None:
        return present[later]
    return None


def _present_entry(fseq: TargetPattern, number: int, path: str) -> SequenceEntry:
    """Build a SequenceEntry for a present-on-disk item."""
    return SequenceEntry(number=number, padded_number=_format_number(fseq, number), path=path)


def _format_number(fseq: TargetPattern, number: int) -> str:
    """Render an integer key with the sequence's declared zero-padding.

    `fseq.frame(N)` returns the full filename (basename + padded number +
    extension). We want just the padded number (e.g. "0005" for number 5
    against `####`), so we compute it directly from `zfill()`.
    """
    width = fseq.zfill()
    if width <= 0:
        return str(number)
    if number < 0:
        return f"-{abs(number):0{width}d}"
    return f"{number:0{width}d}"


def _canonical_pattern(fseq: TargetPattern) -> str:
    """Reconstruct the basename + padding + extension form (no number range)."""
    return f"{fseq.basename()}{fseq.padding()}{fseq.extension()}"
