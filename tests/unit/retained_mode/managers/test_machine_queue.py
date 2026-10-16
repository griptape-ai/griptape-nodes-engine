"""Tests for MachineQueue: one run at a time, its nodes together, and every way a run lets go."""

from __future__ import annotations

import asyncio

import pytest

from griptape_nodes.retained_mode.managers.machine_queue import MachineQueue, NodeNotAdmittedError

_A = "session-a"
_B = "session-b"


class _Recorder:
    """Records the queue's callbacks."""

    def __init__(self) -> None:
        self.handoffs: list[str] = []
        self.positions: list[tuple[str, str, int]] = []

    async def on_handoff(self, incoming: str) -> None:
        self.handoffs.append(incoming)

    async def on_position(self, originator: str, node_name: str, position: int) -> None:
        self.positions.append((originator, node_name, position))


def _queue(recorder: _Recorder, *, idle_timeout_s: float = 60.0) -> MachineQueue:
    return MachineQueue(idle_timeout_s=idle_timeout_s, on_handoff=recorder.on_handoff, on_position=recorder.on_position)


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


def _admit(queue: MachineQueue, admission_id: str, originator: str, run_id: str | None) -> asyncio.Task[str]:
    return asyncio.ensure_future(
        queue.admit(admission_id=admission_id, originator=originator, run_id=run_id, node_name=f"node-{admission_id}")
    )


class TestMachineQueue:
    @pytest.mark.asyncio
    async def test_a_free_machine_admits_at_once(self) -> None:
        queue = _queue(_Recorder())

        run = await queue.admit(admission_id="1", originator=_A, run_id="r1", node_name="n")

        assert run == "r1"
        assert queue.holder == (_A, "r1")

    @pytest.mark.asyncio
    async def test_another_run_waits_until_the_run_holding_the_machine_ends(self) -> None:
        recorder = _Recorder()
        queue = _queue(recorder)
        await queue.admit(admission_id="1", originator=_A, run_id="r1", node_name="n")

        waiting = _admit(queue, "2", _B, "r9")
        await _settle()
        assert not waiting.done()
        assert recorder.positions == [(_B, "node-2", 1)]

        queue.end_run(_A, "r1")
        await _settle()

        assert await waiting == "r9"
        assert queue.holder == (_B, "r9")
        assert recorder.positions[-1] == (_B, "node-2", 0), "B was not told its wait was over"

    @pytest.mark.asyncio
    async def test_the_holding_runs_later_nodes_run_while_another_run_waits(self) -> None:
        queue = _queue(_Recorder())
        await queue.admit(admission_id="1", originator=_A, run_id="r1", node_name="n")
        waiting = _admit(queue, "2", _B, "r9")
        await _settle()

        run = await queue.admit(admission_id="3", originator=_A, run_id="r1", node_name="later")

        assert run == "r1"
        assert not waiting.done()
        queue.end_run(_A, "r1")
        await waiting

    @pytest.mark.asyncio
    async def test_every_waiting_node_of_the_next_run_is_admitted_together(self) -> None:
        queue = _queue(_Recorder())
        await queue.admit(admission_id="1", originator=_A, run_id="r1", node_name="n")
        first = _admit(queue, "2", _B, "r9")
        second = _admit(queue, "3", _B, "r9")
        await _settle()

        queue.end_run(_A, "r1")
        await _settle()

        assert first.done()
        assert second.done()

    @pytest.mark.asyncio
    async def test_runs_take_the_machine_in_the_order_they_first_asked(self) -> None:
        recorder = _Recorder()
        queue = _queue(recorder)
        await queue.admit(admission_id="1", originator=_A, run_id="r1", node_name="n")
        b_waits = _admit(queue, "2", _B, "r9")
        c_waits = _admit(queue, "3", "session-c", "r5")
        await _settle()
        assert (_B, "node-2", 1) in recorder.positions
        assert ("session-c", "node-3", 2) in recorder.positions

        queue.end_run(_A, "r1")
        await _settle()

        assert b_waits.done()
        assert not c_waits.done()
        assert ("session-c", "node-3", 1) in recorder.positions

    @pytest.mark.asyncio
    async def test_the_machine_is_freed_only_when_it_passes_to_another_orchestrator(self) -> None:
        recorder = _Recorder()
        queue = _queue(recorder)
        await queue.admit(admission_id="1", originator=_A, run_id="r1", node_name="n")
        queue.end_run(_A, "r1")
        await _settle()

        await queue.admit(admission_id="2", originator=_A, run_id="r2", node_name="n")
        assert recorder.handoffs == [], "the same artist's next run must not free its own models"

        waiting = _admit(queue, "3", _B, "r9")
        queue.end_run(_A, "r2")
        await waiting

        assert recorder.handoffs == [_B]

    @pytest.mark.asyncio
    async def test_a_handoff_that_fails_still_hands_the_machine_over(self) -> None:
        recorder = _Recorder()

        async def fail_to_free(incoming: str) -> None:
            recorder.handoffs.append(incoming)
            msg = "the connection is re-establishing"
            raise ConnectionError(msg)

        queue = MachineQueue(idle_timeout_s=60.0, on_handoff=fail_to_free, on_position=recorder.on_position)
        await queue.admit(admission_id="1", originator=_A, run_id="r1", node_name="n")
        waiting = _admit(queue, "2", _B, "r9")
        await _settle()

        queue.end_run(_A, "r1")

        assert await asyncio.wait_for(waiting, timeout=1.0) == "r9"
        assert recorder.handoffs == [_B]
        assert queue.holder == (_B, "r9")

    @pytest.mark.asyncio
    async def test_a_run_with_no_node_running_lets_go_after_the_idle_timeout(self) -> None:
        queue = _queue(_Recorder(), idle_timeout_s=0.05)
        await queue.admit(admission_id="1", originator=_A, run_id="r1", node_name="n")
        waiting = _admit(queue, "2", _B, "r9")

        queue.node_ended("1")
        await asyncio.wait_for(waiting, timeout=1)

        assert queue.holder == (_B, "r9")

    @pytest.mark.asyncio
    async def test_a_running_node_keeps_the_machine_past_the_idle_timeout(self) -> None:
        """A node that runs longer than the timeout is not idle: the timeout counts from the last one ending."""
        queue = _queue(_Recorder(), idle_timeout_s=0.05)
        await queue.admit(admission_id="1", originator=_A, run_id="r1", node_name="n")
        waiting = _admit(queue, "2", _B, "r9")

        await asyncio.sleep(0.15)

        assert not waiting.done()
        queue.end_run(_A, "r1")
        await waiting

    @pytest.mark.asyncio
    async def test_a_node_sent_without_a_run_holds_the_machine_only_while_it_runs(self) -> None:
        queue = _queue(_Recorder())
        run = await queue.admit(admission_id="1", originator=_A, run_id=None, node_name="n")
        waiting = _admit(queue, "2", _B, "r9")
        await _settle()
        assert not waiting.done()

        queue.node_ended("1")
        await asyncio.wait_for(waiting, timeout=1)

        assert run.startswith("one-off:")

    @pytest.mark.asyncio
    async def test_two_nodes_sent_without_a_run_are_separate_runs(self) -> None:
        queue = _queue(_Recorder())
        await queue.admit(admission_id="1", originator=_A, run_id=None, node_name="n")

        second = _admit(queue, "2", _A, None)
        await _settle()

        assert not second.done()
        queue.node_ended("1")
        await asyncio.wait_for(second, timeout=1)

    @pytest.mark.asyncio
    async def test_an_orchestrator_going_away_frees_the_machine_and_turns_its_waiting_nodes_away(self) -> None:
        queue = _queue(_Recorder())
        await queue.admit(admission_id="1", originator=_A, run_id="r1", node_name="n")
        b_waits = _admit(queue, "2", _B, "r9")
        another_a = _admit(queue, "3", _A, "r2")
        await _settle()

        queue.originator_gone(_A)
        await _settle()

        assert b_waits.done()
        with pytest.raises(NodeNotAdmittedError):
            await another_a

    @pytest.mark.asyncio
    async def test_a_waiting_node_cancelled_stops_waiting_and_moves_the_line_up(self) -> None:
        recorder = _Recorder()
        queue = _queue(recorder)
        await queue.admit(admission_id="1", originator=_A, run_id="r1", node_name="n")
        b_waits = _admit(queue, "2", _B, "r9")
        c_waits = _admit(queue, "3", "session-c", "r5")
        await _settle()

        queue.node_ended("2")
        await _settle()

        with pytest.raises(NodeNotAdmittedError):
            await b_waits
        assert ("session-c", "node-3", 1) in recorder.positions
        queue.end_run(_A, "r1")
        await c_waits
