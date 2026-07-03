"""Tests for `griptape_nodes.common.sequences` via the public `ScanSequencesRequest`.

The intended entry point is the bus request, not the underlying
`scan_sequences` function. Each test dispatches via
`GriptapeNodes.ahandle_request(...)` and asserts on the typed result payload.

Filesystem listings are stubbed by patching `GriptapeNodes.handle_request` so the
inner `ListDirectoryRequest` returns canned filenames; this leaves the real
`OSManager.on_scan_sequences_request` handler in place to exercise the full
async dispatch path.
"""

# ruff: noqa: PLR2004

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from griptape_nodes.common.sequences import MissingItemPolicy, NoTokenBehavior
from griptape_nodes.retained_mode.events.os_events import (
    FileIOFailureReason,
    FileSystemEntry,
    ListDirectoryRequest,
    ListDirectoryResultFailure,
    ListDirectoryResultSuccess,
    ScanSequencesRequest,
    ScanSequencesResultFailure,
    ScanSequencesResultSuccess,
    SequenceScanFailureReason,
)
from griptape_nodes.retained_mode.events.project_events import (
    GetPathForMacroRequest,
    GetPathForMacroResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes


def _stub_listing(directory: str, filenames: list[str]) -> Any:
    """Return a `handle_request` side_effect that lists `directory` with `filenames`.

    Any directory other than `directory` returns a listing failure. Any
    request type other than ListDirectoryRequest raises (no other request
    types should be hit by this code path).
    """

    def handle_request(request: object) -> object:
        if isinstance(request, ListDirectoryRequest):
            if request.directory_path == directory:
                entries = [
                    FileSystemEntry(name=name, path=str(Path(directory) / name), is_dir=False) for name in filenames
                ]
                return ListDirectoryResultSuccess(
                    entries=entries,
                    current_path=directory,
                    is_workspace_path=False,
                    result_details="ok",
                )
            return ListDirectoryResultFailure(
                failure_reason=FileIOFailureReason.FILE_NOT_FOUND,
                result_details=f"{request.directory_path} not stubbed",
            )
        msg = f"Unexpected request: {type(request).__name__}"
        raise AssertionError(msg)

    return handle_request


def _stub_listing_with_macro(
    *,
    macro_directory: str,
    resolved_directory: str,
    filenames: list[str],
) -> Any:
    """Stub both `GetPathForMacroRequest` and `ListDirectoryRequest`.

    Used by the macro-round-trip tests: the handler resolves the macro
    head via the project's macro resolver, then lists the resolved-on-disk
    directory. Both dispatches need to be intercepted since the test
    doesn't bring up a real ProjectManager.
    """

    def handle_request(request: object) -> object:
        if isinstance(request, GetPathForMacroRequest):
            assert request.parsed_macro.template == macro_directory, (
                f"unexpected macro: {request.parsed_macro.template!r} (expected {macro_directory!r})"
            )
            return GetPathForMacroResultSuccess(
                resolved_path=Path(resolved_directory),
                absolute_path=Path(resolved_directory),
                result_details="ok",
            )
        if isinstance(request, ListDirectoryRequest):
            # Path()-normalize both sides: on Windows the handler emits backslashes
            # via str(WindowsPath(...)), but the test fixture is POSIX-style.
            # `directory_path` is `str | None`; the macro tests always supply a
            # string but we narrow defensively so pyright is happy.
            if request.directory_path is not None and Path(request.directory_path) == Path(resolved_directory):
                entries = [
                    FileSystemEntry(name=name, path=str(Path(resolved_directory) / name), is_dir=False)
                    for name in filenames
                ]
                return ListDirectoryResultSuccess(
                    entries=entries,
                    current_path=resolved_directory,
                    is_workspace_path=False,
                    result_details="ok",
                )
            return ListDirectoryResultFailure(
                failure_reason=FileIOFailureReason.FILE_NOT_FOUND,
                result_details=f"{request.directory_path} not stubbed",
            )
        msg = f"Unexpected request: {type(request).__name__}"
        raise AssertionError(msg)

    return handle_request


async def _scan(  # noqa: PLR0913
    directory: str,
    pattern: str,
    *,
    policy: MissingItemPolicy = MissingItemPolicy.SPLIT,
    no_token_behavior: NoTokenBehavior = NoTokenBehavior.SINGLE_FILE,
    start_number: int | None = None,
    end_number: int | None = None,
) -> ScanSequencesResultSuccess | ScanSequencesResultFailure:
    """Dispatch a ScanSequencesRequest and narrow the result type for assertions.

    Test helper signature is unchanged for ergonomics — `directory` + `pattern`
    are concatenated into the new single `path=` field the request actually
    takes.
    """
    path = f"{directory}/{pattern}" if directory else pattern
    result = await GriptapeNodes.ahandle_request(
        ScanSequencesRequest(
            path=path,
            policy=policy,
            no_token_behavior=no_token_behavior,
            start_number=start_number,
            end_number=end_number,
        )
    )
    assert isinstance(result, (ScanSequencesResultSuccess, ScanSequencesResultFailure)), (
        f"unexpected result type: {type(result).__name__}"
    )
    return result


# --- Basic scanning -----------------------------------------------------


class TestBasicScanning:
    @pytest.mark.asyncio
    async def test_contiguous_sequence_split(self) -> None:
        """SPLIT on a contiguous sequence yields one sub-sequence."""
        directory = "/work/in"
        filenames = [f"render.{n:04d}.png" for n in [1, 2, 3, 4, 5]]
        with patch.object(GriptapeNodes, "handle_request", side_effect=_stub_listing(directory, filenames)):
            result = await _scan(directory, "render.####.png", policy=MissingItemPolicy.SPLIT)
        assert isinstance(result, ScanSequencesResultSuccess)
        assert result.has_entries is True
        assert result.directory_had_matching_files is True
        assert result.discovered_first == 1
        assert result.discovered_last == 5
        seqs = result.sequences
        assert len(seqs) == 1
        assert seqs[0].first == 1
        assert seqs[0].last == 5
        assert [e.number for e in seqs[0].entries] == [1, 2, 3, 4, 5]
        assert [e.padded_number for e in seqs[0].entries] == ["0001", "0002", "0003", "0004", "0005"]

    @pytest.mark.asyncio
    async def test_no_files_returns_empty_success(self) -> None:
        """An empty directory yields a success with no sequences and `has_entries=False`.

        The `directory_had_matching_files` flag is False because nothing in
        the listing matched the basename/extension shape.
        """
        with patch.object(GriptapeNodes, "handle_request", side_effect=_stub_listing("/work/in", [])):
            result = await _scan("/work/in", "render.####.png")
        assert isinstance(result, ScanSequencesResultSuccess)
        assert result.sequences == []
        assert result.has_entries is False
        assert result.directory_had_matching_files is False
        assert result.discovered_first is None
        assert result.discovered_last is None

    @pytest.mark.asyncio
    async def test_directory_listing_failure_surfaces_file_io_failure(self) -> None:
        """A failed listing propagates the OS-level `FileIOFailureReason` to the caller.

        `scan_sequences` raises `DirectoryListingError` when the inner
        `ListDirectoryRequest` fails; the handler maps that to a
        `ScanSequencesResultFailure` carrying the underlying reason. This
        replaces the prior behavior where listing failures were silently
        folded into empty success.
        """
        # Stub always returns failure for /work/in (only /other is recognized)
        with patch.object(GriptapeNodes, "handle_request", side_effect=_stub_listing("/other", [])):
            result = await _scan("/work/in", "render.####.png")
        assert isinstance(result, ScanSequencesResultFailure)
        assert result.failure_reason is FileIOFailureReason.FILE_NOT_FOUND

    @pytest.mark.asyncio
    async def test_unrelated_files_filtered_out(self) -> None:
        """Files not matching basename/extension are ignored."""
        directory = "/work/in"
        filenames = [
            "render.0001.png",
            "render.0002.png",
            "comp.0001.png",  # different basename
            "render.0001.exr",  # different extension
            "notes.txt",  # totally unrelated
        ]
        with patch.object(GriptapeNodes, "handle_request", side_effect=_stub_listing(directory, filenames)):
            result = await _scan(directory, "render.####.png", policy=MissingItemPolicy.SPLIT)
        assert isinstance(result, ScanSequencesResultSuccess)
        seqs = result.sequences
        assert len(seqs) == 1
        assert [e.number for e in seqs[0].entries] == [1, 2]


# --- Policy semantics ---------------------------------------------------


class TestSplitPolicy:
    @pytest.mark.asyncio
    async def test_three_runs(self) -> None:
        """Numbers 1-2, 4, 6-7 split into three sub-sequences."""
        directory = "/work/in"
        filenames = [f"render.{n:04d}.png" for n in [1, 2, 4, 6, 7]]
        with patch.object(GriptapeNodes, "handle_request", side_effect=_stub_listing(directory, filenames)):
            result = await _scan(directory, "render.####.png", policy=MissingItemPolicy.SPLIT)
        assert isinstance(result, ScanSequencesResultSuccess)
        seqs = result.sequences
        assert len(seqs) == 3
        assert (seqs[0].first, seqs[0].last) == (1, 2)
        assert (seqs[1].first, seqs[1].last) == (4, 4)
        assert (seqs[2].first, seqs[2].last) == (6, 7)

    @pytest.mark.asyncio
    async def test_split_records_discovered_range_on_each(self) -> None:
        """All sub-sequences carry the same discovered_first/discovered_last."""
        directory = "/work/in"
        filenames = [f"render.{n:04d}.png" for n in [1, 2, 4, 6, 7]]
        with patch.object(GriptapeNodes, "handle_request", side_effect=_stub_listing(directory, filenames)):
            result = await _scan(directory, "render.####.png", policy=MissingItemPolicy.SPLIT)
        assert isinstance(result, ScanSequencesResultSuccess)
        for s in result.sequences:
            assert s.discovered_first == 1
            assert s.discovered_last == 7


class TestSkipPolicy:
    @pytest.mark.asyncio
    async def test_skip_omits_gaps(self) -> None:
        """SKIP yields one sequence with only present numbers; gaps absent from entries."""
        directory = "/work/in"
        filenames = [f"render.{n:04d}.png" for n in [1, 2, 4, 6, 7]]
        with patch.object(GriptapeNodes, "handle_request", side_effect=_stub_listing(directory, filenames)):
            result = await _scan(directory, "render.####.png", policy=MissingItemPolicy.SKIP)
        assert isinstance(result, ScanSequencesResultSuccess)
        seqs = result.sequences
        assert len(seqs) == 1
        assert [e.number for e in seqs[0].entries] == [1, 2, 4, 6, 7]
        assert seqs[0].missing_numbers == {3, 5}


class TestFillNearestPolicy:
    @pytest.mark.asyncio
    async def test_fill_nearest_backward_first(self) -> None:
        """FILL_NEAREST fills gaps with the backward-first present number."""
        directory = "/work/in"
        filenames = [f"render.{n:04d}.png" for n in [1, 2, 4, 6, 7]]
        with patch.object(GriptapeNodes, "handle_request", side_effect=_stub_listing(directory, filenames)):
            result = await _scan(directory, "render.####.png", policy=MissingItemPolicy.FILL_NEAREST)
        assert isinstance(result, ScanSequencesResultSuccess)
        s = result.sequences[0]
        # Gap at 3 -> backward to 2
        entry_3 = next(e for e in s.entries if e.number == 3)
        assert Path(entry_3.path).name == "render.0002.png"
        # Gap at 5 -> backward to 4
        entry_5 = next(e for e in s.entries if e.number == 5)
        assert Path(entry_5.path).name == "render.0004.png"

    # Note: forward-fall is unreachable through scan_sequences with a single
    # subset clip — `active_first` is always clamped up to `discovered_first`,
    # so there's always at least one earlier present number for any in-range
    # gap. Forward-fall remains in the policy code as a defensive fallback
    # for direct callers of `apply_policy`, but isn't exercised here.


class TestAbortPolicy:
    @pytest.mark.asyncio
    async def test_abort_surfaces_all_gaps(self) -> None:
        """ABORT surfaces a ScanSequencesResultFailure listing every gap.

        With items 1, 2, 4, 5 on disk, the active range 1..5 has exactly one
        gap (item 3). The failure payload should expose it as a one-element
        list — no longer the bare integer the previous shape carried.
        """
        directory = "/work/in"
        filenames = [f"render.{n:04d}.png" for n in [1, 2, 4, 5]]
        with patch.object(GriptapeNodes, "handle_request", side_effect=_stub_listing(directory, filenames)):
            result = await _scan(directory, "render.####.png", policy=MissingItemPolicy.ABORT)
        assert isinstance(result, ScanSequencesResultFailure)
        assert result.failure_reason is SequenceScanFailureReason.ABORTED_AT_GAP
        assert result.missing_item_numbers == [3]

    @pytest.mark.asyncio
    async def test_abort_surfaces_multiple_gaps(self) -> None:
        """ABORT collects every gap in one pass, sorted ascending.

        Items 1, 2, 5, 7 on disk; active range 1..7. The expected gaps are 3,
        4, 6 — one continuous range and one singleton, surfaced together so a
        UI consumer can show the artist all the missing slots in one go
        instead of fixing them one re-run at a time.
        """
        directory = "/work/in"
        filenames = [f"render.{n:04d}.png" for n in [1, 2, 5, 7]]
        with patch.object(GriptapeNodes, "handle_request", side_effect=_stub_listing(directory, filenames)):
            result = await _scan(directory, "render.####.png", policy=MissingItemPolicy.ABORT)
        assert isinstance(result, ScanSequencesResultFailure)
        assert result.failure_reason is SequenceScanFailureReason.ABORTED_AT_GAP
        assert result.missing_item_numbers == [3, 4, 6]
        # Sanity: result_details summarises the count and shows the list.
        details = str(result.result_details or "")
        assert "3 gaps" in details
        assert "3, 4, 6" in details

    @pytest.mark.asyncio
    async def test_abort_succeeds_when_dense(self) -> None:
        """ABORT returns one Sequence with all entries when there are no gaps.

        The all-gaps pre-pass runs but finds nothing, so the scanner falls
        through to the normal entries-build loop and emits a contiguous
        sequence the same way SKIP would.
        """
        directory = "/work/in"
        filenames = [f"render.{n:04d}.png" for n in [1, 2, 3]]
        with patch.object(GriptapeNodes, "handle_request", side_effect=_stub_listing(directory, filenames)):
            result = await _scan(directory, "render.####.png", policy=MissingItemPolicy.ABORT)
        assert isinstance(result, ScanSequencesResultSuccess)
        seqs = result.sequences
        assert len(seqs) == 1
        assert [e.number for e in seqs[0].entries] == [1, 2, 3]


# --- Negative numbers ---------------------------------------------------


class TestNegativeNumbers:
    @pytest.mark.asyncio
    async def test_negatives_with_different_padding_filter_out_silently(self) -> None:
        """Negative numbers at a different padding width are filtered by the padding match.

        When `-0005.png` has 5 total chars (sign + 4 digits), fileseq groups
        it as a width-5 sequence — separate from the positive width-4 numbers.
        Our zfill filter discards it before we ever see the negative.
        """
        directory = "/work/in"
        filenames = ["render.-0005.png", "render.0001.png", "render.0002.png"]
        with patch.object(GriptapeNodes, "handle_request", side_effect=_stub_listing(directory, filenames)):
            result = await _scan(directory, "render.####.png", policy=MissingItemPolicy.SPLIT)
        assert isinstance(result, ScanSequencesResultSuccess)
        seqs = result.sequences
        assert len(seqs) == 1
        assert [e.number for e in seqs[0].entries] == [1, 2]
        # Negatives never entered our loop; the counter sees zero.
        assert seqs[0].dropped_negative_number_count == 0

    @pytest.mark.asyncio
    async def test_negatives_with_matching_padding_dropped_with_counter(self) -> None:
        """When padding matches, negatives DO enter the loop and get filtered out."""
        directory = "/work/in"
        # Width-5 pattern: `-0005` and `00005` both have 5 digits in their slot.
        filenames = ["render.-0005.png", "render.00005.png", "render.00010.png"]
        with patch.object(GriptapeNodes, "handle_request", side_effect=_stub_listing(directory, filenames)):
            result = await _scan(directory, "render.#####.png", policy=MissingItemPolicy.SPLIT)
        assert isinstance(result, ScanSequencesResultSuccess)
        seqs = result.sequences
        # The negative is dropped; positives 5 and 10 are in the same sequence
        # under SPLIT but they aren't contiguous, so they split into two runs.
        assert len(seqs) == 2
        assert {e.number for s in seqs for e in s.entries} == {5, 10}
        # The counter should have noted the dropped negative on every produced sequence.
        assert all(s.dropped_negative_number_count == 1 for s in seqs)


# --- Subset clipping ----------------------------------------------------


class TestSubsetClipping:
    @pytest.mark.asyncio
    async def test_start_only(self) -> None:
        directory = "/work/in"
        filenames = [f"render.{n:04d}.png" for n in [1, 2, 3, 4, 5]]
        with patch.object(GriptapeNodes, "handle_request", side_effect=_stub_listing(directory, filenames)):
            result = await _scan(directory, "render.####.png", policy=MissingItemPolicy.SPLIT, start_number=3)
        assert isinstance(result, ScanSequencesResultSuccess)
        seqs = result.sequences
        assert len(seqs) == 1
        assert [e.number for e in seqs[0].entries] == [3, 4, 5]
        assert seqs[0].discovered_first == 1
        assert seqs[0].first == 3

    @pytest.mark.asyncio
    async def test_end_only(self) -> None:
        directory = "/work/in"
        filenames = [f"render.{n:04d}.png" for n in [1, 2, 3, 4, 5]]
        with patch.object(GriptapeNodes, "handle_request", side_effect=_stub_listing(directory, filenames)):
            result = await _scan(directory, "render.####.png", policy=MissingItemPolicy.SPLIT, end_number=3)
        assert isinstance(result, ScanSequencesResultSuccess)
        seqs = result.sequences
        assert len(seqs) == 1
        assert [e.number for e in seqs[0].entries] == [1, 2, 3]
        assert seqs[0].discovered_last == 5
        assert seqs[0].last == 3

    @pytest.mark.asyncio
    async def test_start_and_end(self) -> None:
        directory = "/work/in"
        filenames = [f"render.{n:04d}.png" for n in [1, 2, 3, 4, 5]]
        with patch.object(GriptapeNodes, "handle_request", side_effect=_stub_listing(directory, filenames)):
            result = await _scan(
                directory, "render.####.png", policy=MissingItemPolicy.SPLIT, start_number=2, end_number=4
            )
        assert isinstance(result, ScanSequencesResultSuccess)
        seqs = result.sequences
        assert len(seqs) == 1
        assert [e.number for e in seqs[0].entries] == [2, 3, 4]

    @pytest.mark.asyncio
    async def test_subset_outside_discovered_range_reports_discovered_range(self) -> None:
        """Subset clip drops every present item — diagnostic flags expose the on-disk range.

        The directory has 1..3 on disk but the caller asked for 10..20.
        `directory_had_matching_files=True` and `discovered_first/last` carry
        the on-disk bounds so the caller can show "asked for 10..20 but disk
        has 1..3" instead of a generic "no matches".
        """
        directory = "/work/in"
        filenames = [f"render.{n:04d}.png" for n in [1, 2, 3]]
        with patch.object(GriptapeNodes, "handle_request", side_effect=_stub_listing(directory, filenames)):
            result = await _scan(
                directory, "render.####.png", policy=MissingItemPolicy.SPLIT, start_number=10, end_number=20
            )
        assert isinstance(result, ScanSequencesResultSuccess)
        assert result.sequences == []
        assert result.has_entries is False
        assert result.directory_had_matching_files is True
        assert result.discovered_first == 1
        assert result.discovered_last == 3

    @pytest.mark.asyncio
    async def test_negative_start_rejected(self) -> None:
        result = await _scan("/work/in", "render.####.png", start_number=-1)
        assert isinstance(result, ScanSequencesResultFailure)
        assert result.failure_reason is SequenceScanFailureReason.INVALID_BOUNDS

    @pytest.mark.asyncio
    async def test_inverted_bounds_rejected(self) -> None:
        result = await _scan("/work/in", "render.####.png", start_number=10, end_number=5)
        assert isinstance(result, ScanSequencesResultFailure)
        assert result.failure_reason is SequenceScanFailureReason.INVALID_BOUNDS


# --- Pattern variants ---------------------------------------------------


class TestPatternVariants:
    @pytest.mark.asyncio
    async def test_printf_pattern(self) -> None:
        """%04d works the same as ####."""
        directory = "/work/in"
        filenames = [f"render.{n:04d}.png" for n in [1, 2, 3]]
        with patch.object(GriptapeNodes, "handle_request", side_effect=_stub_listing(directory, filenames)):
            result = await _scan(directory, "render.%04d.png", policy=MissingItemPolicy.SPLIT)
        assert isinstance(result, ScanSequencesResultSuccess)
        seqs = result.sequences
        assert len(seqs) == 1
        assert [e.number for e in seqs[0].entries] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_mismatched_padding_reports_files_present(self) -> None:
        """Disk has 3-digit numbers; user declared #### (4 digits). No match — empty success.

        The basename/extension prefilter accepts these files (so
        `directory_had_matching_files=True`), but fileseq groups them at
        zfill=3 which doesn't match the target's zfill=4 — so no inferred
        numbers and `discovered_first/last` stay None. The flag combination
        tells the caller the cause is padding mismatch, not wrong path.
        """
        directory = "/work/in"
        filenames = [f"render.{n:03d}.png" for n in [1, 2, 3]]
        with patch.object(GriptapeNodes, "handle_request", side_effect=_stub_listing(directory, filenames)):
            result = await _scan(directory, "render.####.png", policy=MissingItemPolicy.SPLIT)
        assert isinstance(result, ScanSequencesResultSuccess)
        assert result.sequences == []
        assert result.has_entries is False
        assert result.directory_had_matching_files is True
        assert result.discovered_first is None
        assert result.discovered_last is None

    @pytest.mark.asyncio
    async def test_unpadded_printf_round_trip(self) -> None:
        """`%d` matches an unpadded directory and yields bare integer padded_numbers.

        fileseq treats `%d` as zfill=1 (same as a single `#`). The Sequence's
        canonical `pattern` preserves the user's input form (`%d`, not `#`).
        """
        directory = "/work/in"
        filenames = ["render.5.png", "render.42.png", "render.123.png"]
        with patch.object(GriptapeNodes, "handle_request", side_effect=_stub_listing(directory, filenames)):
            result = await _scan(directory, "render.%d.png", policy=MissingItemPolicy.SPLIT)
        assert isinstance(result, ScanSequencesResultSuccess)
        seqs = result.sequences
        # 5, 42, 123 aren't contiguous, so SPLIT yields three sequences.
        assert len(seqs) == 3
        all_entries = [e for s in seqs for e in s.entries]
        assert [e.number for e in all_entries] == [5, 42, 123]
        assert [e.padded_number for e in all_entries] == ["5", "42", "123"]
        for s in seqs:
            assert s.padding == 1
            assert s.pattern == "render.%d.png"


# --- Pattern validation -------------------------------------------------


class TestPatternValidation:
    """Multi-token templates surface as INVALID_TEMPLATE failures."""

    @pytest.mark.asyncio
    async def test_multi_token_dot_separator(self) -> None:
        """`render.##.##.exr` — fileseq accepts this and silently misparses."""
        result = await _scan("/work/in", "render.##.##.exr")
        assert isinstance(result, ScanSequencesResultFailure)
        assert result.failure_reason is SequenceScanFailureReason.INVALID_TEMPLATE
        assert "2 sequence tokens" in str(result.result_details or "")

    @pytest.mark.asyncio
    async def test_multi_token_underscore_separator(self) -> None:
        """`render.####_v####.exr` — two distinct hash tokens."""
        result = await _scan("/work/in", "render.####_v####.exr")
        assert isinstance(result, ScanSequencesResultFailure)
        assert result.failure_reason is SequenceScanFailureReason.INVALID_TEMPLATE
        assert "2 sequence tokens" in str(result.result_details or "")

    @pytest.mark.asyncio
    async def test_multi_token_mixed_syntax(self) -> None:
        """A printf token AND a hash token in the same template."""
        result = await _scan("/work/in", "foo_%04d_bar_####.exr")
        assert isinstance(result, ScanSequencesResultFailure)
        assert result.failure_reason is SequenceScanFailureReason.INVALID_TEMPLATE
        assert "2 sequence tokens" in str(result.result_details or "")

    @pytest.mark.asyncio
    async def test_zero_token_path_one_item_when_present(self) -> None:
        """A token-less path becomes a one-item sequence when the file exists.

        The artist pasted in `photo.png` and the file is on disk → produce a
        Sequence with `entries=[#1]`, `padding=0`, `first=last=1`. This is the
        path the artist actually traveled most of the time when typing in a
        single-file path.
        """
        directory = "/work/in"
        with patch.object(GriptapeNodes, "handle_request", side_effect=_stub_listing(directory, ["photo.png"])):
            result = await _scan(directory, "photo.png")
        assert isinstance(result, ScanSequencesResultSuccess)
        assert result.has_entries is True
        seqs = result.sequences
        assert len(seqs) == 1
        assert seqs[0].first == 1
        assert seqs[0].last == 1
        assert seqs[0].padding == 0
        assert [e.number for e in seqs[0].entries] == [1]
        # Path round-trips through the scanner — for a plain absolute input
        # the entry path matches the directory + filename verbatim.
        assert seqs[0].entries[0].path == "/work/in/photo.png"

    @pytest.mark.asyncio
    async def test_zero_token_path_empty_when_absent(self) -> None:
        """A token-less path returns empty success when the file isn't on disk.

        The literal filename is the only way a zero-token target can match;
        if it's absent, fall back to the same empty-result diagnostic any
        other miss produces.
        """
        directory = "/work/in"
        with patch.object(GriptapeNodes, "handle_request", side_effect=_stub_listing(directory, ["other.png"])):
            result = await _scan(directory, "photo.png")
        assert isinstance(result, ScanSequencesResultSuccess)
        assert result.sequences == []
        assert result.has_entries is False
        # `directory_had_matching_files` is False here because the prefilter
        # checks startswith(basename) + endswith(extension), and `other.png`
        # doesn't share `photo` as a basename prefix.
        assert result.directory_had_matching_files is False

    @pytest.mark.asyncio
    async def test_single_token_passes(self) -> None:
        """Sanity: a normal single-token pattern survives the validator."""
        directory = "/work/in"
        filenames = [f"render.{n:04d}.png" for n in [1, 2, 3]]
        with patch.object(GriptapeNodes, "handle_request", side_effect=_stub_listing(directory, filenames)):
            result = await _scan(directory, "render.####.png")
        assert isinstance(result, ScanSequencesResultSuccess)
        assert len(result.sequences) == 1


# --- Path round-trip ---------------------------------------------------


class TestPathRoundTrip:
    """Verify the engine emits paths in the same shape it received them.

    Macros stay macros, plain absolutes stay plain absolutes. This is what
    makes scan results portable between machines whose `{inputs}` resolve to
    different absolute roots.
    """

    @pytest.mark.asyncio
    async def test_macro_path_preserves_macro_head(self) -> None:
        """A macro-form input round-trips with the `{inputs}` head intact.

        The handler resolves the macro internally for I/O but rebuilds each
        emitted entry path off the original (macro) directory, so downstream
        consumers see the macro shape they supplied.
        """
        macro_directory = "{inputs}/xyz"
        resolved_directory = "/Users/me/project/inputs/xyz"
        filenames = [f"abc{n:03d}.png" for n in [1, 2, 3]]
        path = f"{macro_directory}/abc###.png"
        with patch.object(
            GriptapeNodes,
            "handle_request",
            side_effect=_stub_listing_with_macro(
                macro_directory=macro_directory,
                resolved_directory=resolved_directory,
                filenames=filenames,
            ),
        ):
            result = await GriptapeNodes.ahandle_request(
                ScanSequencesRequest(path=path, policy=MissingItemPolicy.SPLIT)
            )
        assert isinstance(result, ScanSequencesResultSuccess)
        assert len(result.sequences) == 1
        seq = result.sequences[0]
        # `directory` and every `entry.path` retain the macro head.
        assert seq.directory == macro_directory
        assert [e.path for e in seq.entries] == [
            "{inputs}/xyz/abc001.png",
            "{inputs}/xyz/abc002.png",
            "{inputs}/xyz/abc003.png",
        ]

    @pytest.mark.asyncio
    async def test_absolute_path_round_trips_unchanged(self) -> None:
        """A plain absolute path (no macros) round-trips identically.

        The macro-resolver dispatch is skipped entirely — `_build_scan_path_mapping`
        sees no macro variables in the directory portion and treats `original`
        and `resolved` as equal. The combined stub here would raise on a
        macro-resolve dispatch; getting through the test confirms the skip.
        """
        directory = "/work/in"
        filenames = [f"render.{n:04d}.png" for n in [1, 2]]
        with patch.object(GriptapeNodes, "handle_request", side_effect=_stub_listing(directory, filenames)):
            result = await _scan(directory, "render.####.png")
        assert isinstance(result, ScanSequencesResultSuccess)
        seq = result.sequences[0]
        assert seq.directory == directory
        assert [e.path for e in seq.entries] == [
            "/work/in/render.0001.png",
            "/work/in/render.0002.png",
        ]


# --- NoTokenBehavior dispatch ------------------------------------------


class TestNoTokenBehavior:
    """Verify the three-way dispatch on `no_token_behavior`.

    A path with zero sequence tokens (e.g. `render.0002.png`) is ambiguous —
    the user might mean a literal single file, or might be naming one frame of
    an implicit sequence, or might be expected to add an explicit token. The
    `no_token_behavior` field on `ScanSequencesRequest` picks which.
    """

    @pytest.mark.asyncio
    async def test_single_file_default_ignores_siblings(self) -> None:
        """SINGLE_FILE: `render.0002.png` returns just that one file.

        This is the regression pin for the bug where fileseq's parse of
        `render.0002.png` saw zfill=4 and grouped every sibling into a
        5-item sequence. Default behavior must produce a 1-item sequence
        containing only the named file.
        """
        directory = "/work/in"
        # Five renders on disk; only the named one should come back.
        filenames = [f"render.{n:04d}.png" for n in [1, 2, 3, 4, 5]]
        with patch.object(GriptapeNodes, "handle_request", side_effect=_stub_listing(directory, filenames)):
            result = await _scan(directory, "render.0002.png")
        assert isinstance(result, ScanSequencesResultSuccess)
        assert result.has_entries is True
        seqs = result.sequences
        assert len(seqs) == 1
        assert seqs[0].first == 1
        assert seqs[0].last == 1
        assert seqs[0].padding == 0
        assert [e.number for e in seqs[0].entries] == [1]
        # Crucially, only render.0002.png — not the other four siblings.
        assert [e.path for e in seqs[0].entries] == ["/work/in/render.0002.png"]
        assert seqs[0].discovered_first == 1
        assert seqs[0].discovered_last == 1

    @pytest.mark.asyncio
    async def test_explore_sequence_recovers_implicit_grouping(self) -> None:
        """EXPLORE_SEQUENCE: `render.0002.png` walks the full sibling sequence.

        Useful when a downstream tool gave the artist one filename but they
        want the whole take. fileseq parses the digits as a frame token and
        the scan returns every match (here, all five sibling renders).
        """
        directory = "/work/in"
        filenames = [f"render.{n:04d}.png" for n in [1, 2, 3, 4, 5]]
        with patch.object(GriptapeNodes, "handle_request", side_effect=_stub_listing(directory, filenames)):
            result = await _scan(
                directory,
                "render.0002.png",
                policy=MissingItemPolicy.SPLIT,
                no_token_behavior=NoTokenBehavior.EXPLORE_SEQUENCE,
            )
        assert isinstance(result, ScanSequencesResultSuccess)
        assert result.has_entries is True
        seqs = result.sequences
        assert len(seqs) == 1
        assert [e.number for e in seqs[0].entries] == [1, 2, 3, 4, 5]
        assert seqs[0].padding == 4
        assert seqs[0].discovered_first == 1
        assert seqs[0].discovered_last == 5

    @pytest.mark.asyncio
    async def test_reject_fails_with_invalid_template(self) -> None:
        """REJECT: a token-less path fails fast with INVALID_TEMPLATE.

        For workflows that should never silently widen the artist's intent
        (e.g. an automated pipeline that requires an explicit token).
        """
        result = await _scan(
            "/work/in",
            "render.0002.png",
            no_token_behavior=NoTokenBehavior.REJECT,
        )
        assert isinstance(result, ScanSequencesResultFailure)
        assert result.failure_reason is SequenceScanFailureReason.INVALID_TEMPLATE
        # Surface useful guidance, not just "rejected".
        assert "no sequence token" in str(result.result_details or "")

    @pytest.mark.asyncio
    async def test_single_file_with_no_digits_still_works(self) -> None:
        """SINGLE_FILE on `photo.png` (no digits at all) — sanity check."""
        directory = "/work/in"
        with patch.object(GriptapeNodes, "handle_request", side_effect=_stub_listing(directory, ["photo.png"])):
            result = await _scan(directory, "photo.png")
        assert isinstance(result, ScanSequencesResultSuccess)
        assert result.has_entries is True
        seqs = result.sequences
        assert len(seqs) == 1
        assert seqs[0].entries[0].path == "/work/in/photo.png"

    @pytest.mark.asyncio
    async def test_single_file_macro_path_round_trip(self) -> None:
        """SINGLE_FILE keeps macro paths macro-shaped on the way back out.

        Mirrors `test_macro_path_preserves_macro_head` for the literal-file
        branch: an `{inputs}` head supplied on a token-less path should
        survive into the entry's `path`.
        """
        macro_directory = "{inputs}/sequences/01_contiguous"
        resolved_directory = "/Users/me/project/inputs/sequences/01_contiguous"
        # Five siblings on disk; SINGLE_FILE should ignore all but the named one.
        filenames = [f"render.{n:04d}.png" for n in [1, 2, 3, 4, 5]]
        path = f"{macro_directory}/render.0002.png"
        with patch.object(
            GriptapeNodes,
            "handle_request",
            side_effect=_stub_listing_with_macro(
                macro_directory=macro_directory,
                resolved_directory=resolved_directory,
                filenames=filenames,
            ),
        ):
            result = await GriptapeNodes.ahandle_request(
                ScanSequencesRequest(path=path, policy=MissingItemPolicy.SPLIT)
            )
        assert isinstance(result, ScanSequencesResultSuccess)
        assert len(result.sequences) == 1
        seq = result.sequences[0]
        assert [e.path for e in seq.entries] == [f"{macro_directory}/render.0002.png"]
        assert seq.directory == macro_directory
