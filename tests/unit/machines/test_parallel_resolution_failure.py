"""A node failing in a parallel run must not take down the nodes running alongside it.

The failure stops new nodes from starting, but nodes already running finish and publish
their results before the run reports the failure. Only a cancelled run stops them.
"""

import asyncio
from typing import cast
from unittest.mock import AsyncMock

import pytest

from griptape_nodes.exe_types.node_types import NodeResolutionState
from griptape_nodes.machines.dag_builder import DagNode, NodeState
from griptape_nodes.machines.fsm import WorkflowState
from griptape_nodes.machines.parallel_resolution import (
    DagCompleteState,
    ErrorState,
    ExecuteDagState,
    ParallelResolutionContext,
    ParallelResolutionMachine,
)
from griptape_nodes.retained_mode.events.execution_events import NodeErrorEvent
from tests.unit.machines.scheduler_stubs import add_waiting_node, machine_running_one_node, node_stub


def _seed_task(machine: ParallelResolutionMachine, dag_node: DagNode, task: asyncio.Task) -> None:
    dag_node.node_state = NodeState.PROCESSING
    machine.context.task_to_node[task] = dag_node
    dag_node.task_reference = task
    machine.context.running_tasks_count += 1


async def _raise_after_a_tick() -> None:
    await asyncio.sleep(0)
    msg = "boom"
    raise RuntimeError(msg)


async def _wait_for_the_failure(machine: ParallelResolutionMachine) -> None:
    for _ in range(50):
        if machine.current_state in (ErrorState, DagCompleteState):
            return
        await asyncio.sleep(0)
    pytest.fail("the run never handled the failure")


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Publishing reaches into the library registry and events; keep only its state change."""

    async def _resolve(_context: ParallelResolutionContext, dag_node: DagNode) -> None:
        dag_node.node_reference.state = NodeResolutionState.RESOLVED

    publish = AsyncMock(side_effect=_resolve)
    monkeypatch.setattr(ExecuteDagState, "publish_resolved_node", publish)
    return publish


class TestAFailureLetsRunningNodesFinish:
    @pytest.mark.asyncio
    async def test_a_running_node_is_not_cancelled_when_a_sibling_fails(self, published: AsyncMock) -> None:
        machine, release, dag_builder = machine_running_one_node(max_nodes_in_parallel=5)
        running_task = dag_builder.node_to_reference["running"].task_reference
        running_node = dag_builder.node_to_reference["running"].node_reference
        failing = add_waiting_node(dag_builder, "failing", "failing")
        _seed_task(machine, failing, asyncio.create_task(_raise_after_a_tick()))

        driver = asyncio.create_task(machine.resolve_node())
        await _wait_for_the_failure(machine)

        assert running_task is not None
        assert not running_task.cancelled()
        assert not driver.done(), "the run must wait for the running node before it ends"

        release.set()
        await asyncio.wait_for(driver, timeout=1)

        published.assert_awaited_once()
        assert published.await_args is not None
        assert published.await_args.args[1].node_reference.name == "running"
        assert running_node.state is NodeResolutionState.RESOLVED, "its result must survive the run ending"
        assert machine.context.running_tasks_count == 0
        assert machine.is_complete()
        assert machine.context.workflow_state == WorkflowState.ERRORED
        assert machine.get_error_message() == "Node 'failing' encountered a problem: boom"

    @pytest.mark.asyncio
    async def test_a_node_finishing_alongside_the_failure_keeps_its_result(self, published: AsyncMock) -> None:
        """Both tasks come back from one wait; the success must not be dropped whichever is reaped first."""
        machine, release, dag_builder = machine_running_one_node(max_nodes_in_parallel=5)
        running_node = dag_builder.node_to_reference["running"].node_reference
        failing = add_waiting_node(dag_builder, "failing", "failing")

        async def _raise_on_release() -> None:
            await release.wait()
            msg = "boom"
            raise RuntimeError(msg)

        _seed_task(machine, failing, asyncio.create_task(_raise_on_release()))
        release.set()

        await asyncio.wait_for(machine.resolve_node(), timeout=1)

        published.assert_awaited_once()
        assert running_node.state is NodeResolutionState.RESOLVED
        assert machine.context.workflow_state == WorkflowState.ERRORED
        assert machine.context.running_tasks_count == 0

    @pytest.mark.asyncio
    async def test_a_node_cannot_join_a_run_that_is_ending_on_a_failure(self, published: AsyncMock) -> None:  # noqa: ARG002
        machine, release, dag_builder = machine_running_one_node(max_nodes_in_parallel=5)
        failing = add_waiting_node(dag_builder, "failing", "failing")
        _seed_task(machine, failing, asyncio.create_task(_raise_after_a_tick()))

        driver = asyncio.create_task(machine.resolve_node())
        await _wait_for_the_failure(machine)

        with pytest.raises(ValueError, match="a node in that run failed"):
            machine.inject_node(node_stub("late"))
        assert "late" not in dag_builder.node_to_reference

        release.set()
        await asyncio.wait_for(driver, timeout=1)

    @pytest.mark.asyncio
    async def test_a_queued_node_does_not_start_after_a_failure(self, published: AsyncMock) -> None:  # noqa: ARG002
        machine, release, dag_builder = machine_running_one_node(max_nodes_in_parallel=5)
        failing = add_waiting_node(dag_builder, "failing", "failing")
        _seed_task(machine, failing, asyncio.create_task(_raise_after_a_tick()))
        machine.context.max_nodes_in_parallel = 2
        queued = add_waiting_node(dag_builder, "queued", "queued")
        queued.node_state = NodeState.QUEUED
        machine.context.node_priority_queue.add_node(queued)

        driver = asyncio.create_task(machine.resolve_node())
        await _wait_for_the_failure(machine)

        assert queued.node_state is NodeState.CANCELED

        release.set()
        await asyncio.wait_for(driver, timeout=1)
        assert queued.task_reference is None

    @pytest.mark.asyncio
    async def test_a_second_failure_still_reaches_its_node(self, published: AsyncMock) -> None:  # noqa: ARG002
        machine, release, dag_builder = machine_running_one_node(max_nodes_in_parallel=5)
        context = machine.context
        context.error_message = "Node 'first' encountered a problem: boom"
        running = dag_builder.node_to_reference["running"]
        context.task_to_node.clear()

        async def _fail_on_release() -> None:
            await release.wait()
            msg = "second"
            raise RuntimeError(msg)

        context.running_tasks_count = 0
        _seed_task(machine, running, asyncio.create_task(_fail_on_release()))

        await ErrorState.on_enter(context)
        release.set()
        state = await ErrorState.on_update(context)
        while state is ErrorState:
            state = await ErrorState.on_update(context)

        assert state is DagCompleteState
        aput_event = cast("AsyncMock", context.engine.event_manager.aput_event)
        payloads = [call.args[0].wrapped_event.payload for call in aput_event.await_args_list]
        assert any(isinstance(p, NodeErrorEvent) and p.node_name == "running" for p in payloads)
        assert running.node_state is NodeState.ERRORED
        # The run keeps reporting the failure that ended it.
        assert machine.get_error_message() == "Node 'first' encountered a problem: boom"


class TestACancelStillStopsEverything:
    @pytest.mark.asyncio
    async def test_a_cancelled_node_cancels_the_rest_of_the_run(self) -> None:
        machine, _release, dag_builder = machine_running_one_node(max_nodes_in_parallel=5)
        running_task = dag_builder.node_to_reference["running"].task_reference
        cancelled = add_waiting_node(dag_builder, "cancelled", "cancelled")
        cancelled_task = asyncio.create_task(asyncio.sleep(3600))
        _seed_task(machine, cancelled, cancelled_task)
        cancelled_task.cancel()

        await asyncio.wait_for(machine.resolve_node(), timeout=1)

        assert running_task is not None
        assert running_task.cancelled()
        assert machine.context.workflow_state == WorkflowState.CANCELED


class TestTeardownWhileWaitingOnRunningNodes:
    @pytest.mark.asyncio
    async def test_reset_abandons_the_failed_run_without_waiting(self, published: AsyncMock) -> None:
        machine, release, dag_builder = machine_running_one_node(max_nodes_in_parallel=5)
        failing = add_waiting_node(dag_builder, "failing", "failing")
        _seed_task(machine, failing, asyncio.create_task(_raise_after_a_tick()))

        driver = asyncio.create_task(machine.resolve_node())
        await _wait_for_the_failure(machine)

        # The running node is never released: the reset alone has to end the run.
        machine.reset_machine(cancel=True)

        await asyncio.wait_for(driver, timeout=1)
        published.assert_not_awaited()
        release.set()
