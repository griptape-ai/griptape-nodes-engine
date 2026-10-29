"""Tests for RunQueue: the runs waiting to start, first submitted first."""

from __future__ import annotations

import asyncio
import contextvars
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from griptape_nodes.retained_mode.managers.run_queue import Run, RunQueue, RunState


def _run(run_id: str) -> Run:
    return Run(
        run_id=run_id,
        spec=MagicMock(),
        flow_name="flow",
        queued_at=datetime.now(tz=UTC),
        state=RunState.QUEUED,
        outcome=asyncio.get_running_loop().create_future(),
        context=contextvars.copy_context(),
    )


class TestRunQueue:
    @pytest.mark.asyncio
    async def test_runs_come_out_in_the_order_they_went_in(self) -> None:
        queue = RunQueue()
        queue.enqueue(_run("first"))
        queue.enqueue(_run("second"))

        first = queue.pop_next()
        second = queue.pop_next()

        assert first is not None
        assert first.run_id == "first"
        assert second is not None
        assert second.run_id == "second"
        assert queue.pop_next() is None

    @pytest.mark.asyncio
    async def test_a_removed_run_leaves_the_line_and_the_rest_keep_their_order(self) -> None:
        queue = RunQueue()
        for run_id in ("a", "b", "c"):
            queue.enqueue(_run(run_id))

        removed = queue.remove("b")

        assert removed is not None
        assert removed.run_id == "b"
        assert [run.run_id for run in queue.snapshot()] == ["a", "c"]

    @pytest.mark.asyncio
    async def test_removing_a_run_that_is_not_waiting_changes_nothing(self) -> None:
        queue = RunQueue()
        queue.enqueue(_run("a"))

        removed = queue.remove("missing")

        assert removed is None
        assert [run.run_id for run in queue.snapshot()] == ["a"]

    @pytest.mark.asyncio
    async def test_a_snapshot_is_a_copy(self) -> None:
        queue = RunQueue()
        queue.enqueue(_run("a"))

        queue.snapshot().clear()

        assert [run.run_id for run in queue.snapshot()] == ["a"]
