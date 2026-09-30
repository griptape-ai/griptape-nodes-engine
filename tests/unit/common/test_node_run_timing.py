"""Tests for the run summary logged by the node_run_timing beta feature."""

import logging

import pytest

from griptape_nodes.common.node_run_timing import (
    NodeRunRecord,
    NodeRunStatus,
    NodeRunTimer,
    RunOutcome,
    build_run_summary,
)


def _record(
    name: str,
    started_at: float,
    finished_at: float,
    *,
    parent_name: str | None = None,
    status: NodeRunStatus = NodeRunStatus.SUCCEEDED,
) -> NodeRunRecord:
    return NodeRunRecord(
        node_name=name,
        node_type=f"{name}Type",
        parent_name=parent_name,
        started_at=started_at,
        finished_at=finished_at,
        status=status,
    )


class TestBuildRunSummary:
    def test_sequential_nodes_are_separate_stages(self) -> None:
        records = [_record("A", 0.0, 1.0), _record("B", 1.5, 3.5)]

        summary = build_run_summary(records, RunOutcome.COMPLETED, 0.0, 4.0)

        assert summary.splitlines() == [
            "RUN SUMMARY: completed in 4.000 s, 2 nodes ran",
            "├── Stage 1 at 0.000 s, 1.000 s",
            "│   └── 1.000 s  'A' (AType)",
            "└── Stage 2 at 1.500 s, 2.000 s",
            "    └── 2.000 s  'B' (BType)",
        ]

    def test_overlapping_nodes_share_a_stage_slowest_first(self) -> None:
        records = [_record("A", 0.0, 1.0), _record("B", 0.5, 4.0), _record("C", 0.2, 2.0)]

        summary = build_run_summary(records, RunOutcome.COMPLETED, 0.0, 4.0)

        assert summary.splitlines() == [
            "RUN SUMMARY: completed in 4.000 s, 3 nodes ran",
            "└── Stage 1 at 0.000 s, 4.000 s, 3 nodes in parallel",
            "    ├── 3.500 s  'B' (BType)",
            "    ├── 1.800 s  'C' (CType)",
            "    └── 1.000 s  'A' (AType)",
        ]

    def test_nodes_inside_a_loop_are_combined_under_it(self) -> None:
        records = [
            _record("Body", 0.1, 0.4, parent_name="Loop"),
            _record("Body", 0.4, 0.9, parent_name="Loop", status=NodeRunStatus.FAILED),
            _record("Loop", 0.0, 1.0),
        ]

        summary = build_run_summary(records, RunOutcome.FAILED, 0.0, 1.0)

        assert summary.splitlines() == [
            "RUN SUMMARY: failed in 1.000 s, 2 nodes ran",
            "└── Stage 1 at 0.000 s, 1.000 s",
            "    └── 1.000 s  'Loop' (LoopType)",
            "        └── 0.800 s  'Body' (BodyType) x2  1 did not finish",
        ]

    def test_failed_and_cancelled_nodes_are_marked(self) -> None:
        records = [
            _record("A", 0.0, 1.0, status=NodeRunStatus.FAILED),
            _record("B", 0.0, 0.5, status=NodeRunStatus.CANCELLED),
        ]

        summary = build_run_summary(records, RunOutcome.FAILED, 0.0, 1.0)

        assert "'A' (AType)  FAILED" in summary
        assert "'B' (BType)  CANCELLED" in summary

    def test_a_child_whose_parent_was_not_recorded_is_listed_at_the_top_level(self) -> None:
        records = [_record("Body", 0.1, 0.4, parent_name="Loop")]

        summary = build_run_summary(records, RunOutcome.CANCELLED, 0.0, 1.0)

        assert summary.splitlines()[2] == "    └── 0.300 s  'Body' (BodyType)"

    def test_brackets_in_node_names_are_not_read_as_markup(self) -> None:
        summary = build_run_summary([_record("Describe [v2]", 0.0, 1.0)], RunOutcome.COMPLETED, 0.0, 1.0)

        assert "'Describe [v2]'" in summary

    def test_a_child_named_like_its_parent_does_not_recurse_forever(self) -> None:
        records = [_record("Loop", 0.1, 0.2, parent_name="Loop"), _record("Loop", 0.0, 1.0)]

        summary = build_run_summary(records, RunOutcome.COMPLETED, 0.0, 1.0)

        assert summary.count("'Loop'") == 2  # noqa: PLR2004


class TestNodeRunTimer:
    def test_records_are_ignored_outside_a_run(self, caplog: pytest.LogCaptureFixture) -> None:
        timer = NodeRunTimer()
        timer.record(_record("A", 0.0, 1.0))

        with caplog.at_level(logging.INFO, logger="griptape_nodes"):
            timer.finish_run(RunOutcome.COMPLETED)

        assert caplog.records == []

    def test_only_the_first_finish_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        timer = NodeRunTimer()
        timer.start_run()
        timer.record(_record("A", 0.0, 1.0))

        with caplog.at_level(logging.INFO, logger="griptape_nodes"):
            timer.finish_run(RunOutcome.FAILED)
            timer.finish_run(RunOutcome.CANCELLED)

        summaries = [record.getMessage() for record in caplog.records if "RUN SUMMARY" in record.getMessage()]
        assert len(summaries) == 1
        assert summaries[0].startswith("RUN SUMMARY: failed")

    def test_starting_a_run_drops_the_previous_records(self, caplog: pytest.LogCaptureFixture) -> None:
        timer = NodeRunTimer()
        timer.start_run()
        timer.record(_record("Old", 0.0, 1.0))
        timer.start_run()
        timer.record(_record("New", 0.0, 1.0))

        with caplog.at_level(logging.INFO, logger="griptape_nodes"):
            timer.finish_run(RunOutcome.COMPLETED)

        assert "'New'" in caplog.text
        assert "'Old'" not in caplog.text
