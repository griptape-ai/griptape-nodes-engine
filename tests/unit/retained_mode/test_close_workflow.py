"""Closing a workflow owns its whole teardown, whichever path closes it.

`Engine.close_workflow` (sync: `pop_workflow`, a `workflow()` block, the EnsureWorkflowAndFlow rollback)
and `Engine.aclose_workflow` (ClearAll, DeleteWorkflow) both cancel the run, reset its run state,
delete its flows and nodes, and tell the workers to release the workflow's objects. A teardown that
fails leaves the workflow open, rather than spinning or dropping live nodes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from griptape_nodes.exe_types.node_types import DataNode
from griptape_nodes.node_library.library_registry import LibraryMetadata, LibraryRegistry, LibrarySchema, NodeMetadata
from griptape_nodes.retained_mode.events.execution_events import StartFlowResultFailure, StartFlowResultSuccess
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.generic_events import WorkflowNotOpenResultFailure
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.runtime.binding import WorkflowNotOpenError
from griptape_nodes.retained_mode.runtime.open_workflow_set import OpenWorkflowError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

_LIBRARY_NAME = "Close Workflow Test Library"


class _UndeletableNode(DataNode):
    def after_node_deleted(self) -> None:
        msg = "This node refuses to be deleted."
        raise ValueError(msg)


@pytest.fixture
def undeletable_library() -> Iterator[None]:
    """Register `_UndeletableNode`. LibraryRegistry is process-global, so it is cleared around the test."""
    LibraryRegistry._clear()
    schema = LibrarySchema(
        name=_LIBRARY_NAME,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="test", description="test", library_version="0.1.0", engine_version="0.0.0", tags=[]
        ),
        categories=[],
        nodes=[],
    )
    library = LibraryRegistry.generate_new_library(schema)
    library.register_new_node_type(
        _UndeletableNode, NodeMetadata(category="test", description="d", display_name="Undeletable")
    )
    try:
        yield
    finally:
        LibraryRegistry._clear()


def _open_with_canvas(engine: Engine) -> WorkflowRuntime:
    engine.context_manager.push_workflow(workflow_name="closing")
    created = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas"))
    assert isinstance(created, CreateFlowResultSuccess), created.result_details
    return engine.current_workflow()


def _add_undeletable_node(engine: Engine) -> None:
    created = engine.handle_request(
        CreateNodeRequest(node_type="_UndeletableNode", node_name="stuck", override_parent_flow_name="canvas")
    )
    assert isinstance(created, CreateNodeResultSuccess), created.result_details


@pytest.mark.usefixtures("undeletable_library")
class TestAFailedTeardownLeavesTheWorkflowOpen:
    def test_popping_a_workflow_whose_node_fails_to_delete_raises_and_keeps_it_open(self, engine: Engine) -> None:
        workflow = _open_with_canvas(engine)
        _add_undeletable_node(engine)

        with pytest.raises(OpenWorkflowError, match="flow 'canvas' could not be deleted"):
            engine.context_manager.pop_workflow()

        assert engine.open_workflows.all_open() == [workflow]
        assert workflow.objects.has("stuck")

    @pytest.mark.asyncio
    async def test_clearing_a_workflow_whose_node_fails_to_delete_fails_and_keeps_it_open(self, engine: Engine) -> None:
        workflow = _open_with_canvas(engine)
        _add_undeletable_node(engine)

        result = await engine.ahandle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

        assert result.failed()
        assert "could not be deleted" in str(result.result_details)
        assert engine.open_workflows.all_open() == [workflow]


class TestACloseLeavesNothingBehind:
    def test_a_workflow_still_holding_objects_after_its_flows_are_deleted_stays_open(self, engine: Engine) -> None:
        workflow = _open_with_canvas(engine)
        # An object no flow owns, so deleting the flows leaves it behind.
        workflow.objects.add("loose", object())

        with pytest.raises(OpenWorkflowError, match="1 object\\(s\\) remained"):
            engine.context_manager.pop_workflow()

        assert engine.open_workflows.all_open() == [workflow]


class TestWorkStillBoundToAClosedWorkflow:
    @pytest.mark.asyncio
    async def test_a_task_bound_to_a_workflow_clear_all_closed_cannot_write_or_park(self, engine: Engine) -> None:
        workflow = _open_with_canvas(engine)
        release = asyncio.Event()

        async def late_work() -> list[object]:
            await release.wait()
            outcomes: list[object] = [engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="late"))]
            for write in (
                engine.current_workflow,
                lambda: engine.resource_manager.put_local_object(object(), owner="Lib", source="N", key="late"),
            ):
                try:
                    write()
                except WorkflowNotOpenError as err:
                    outcomes.append(err)
            return outcomes

        with engine.bind_workflow(workflow):
            task = asyncio.create_task(late_work())
        cleared = await engine.ahandle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        release.set()
        outcomes = await task

        assert cleared.succeeded(), cleared.result_details
        assert workflow.closed
        assert isinstance(outcomes[0], WorkflowNotOpenResultFailure)
        assert [type(outcome) for outcome in outcomes[1:]] == [WorkflowNotOpenError, WorkflowNotOpenError]
        assert len(workflow.objects) == 0
        assert engine.resource_manager.drop_all_local_objects() == 0


class TestASyncCloseStopsTheRun:
    def test_popping_a_running_workflow_cancels_its_run_before_resetting_it(self, engine: Engine) -> None:
        _open_with_canvas(engine)
        calls: list[str] = []
        flow_manager = engine.flow_manager

        async def cancel() -> None:
            calls.append("cancel")

        with (
            patch.object(flow_manager, "cancel_all_runs", AsyncMock(side_effect=cancel)),
            patch.object(flow_manager, "reset_global_execution_state", side_effect=lambda: calls.append("reset")),
        ):
            engine.context_manager.pop_workflow()

        assert calls == ["cancel", "reset"]
        assert engine.open_workflows.all_open() == []


@dataclass
class _RunSpec:
    """A queued run that executes `body`, standing in for a flow run."""

    flow_name: str
    body: Callable[[], Awaitable[ResultPayload]]

    async def execute(self, engine: Engine) -> ResultPayload:  # noqa: ARG002
        return await self.body()

    def failure(self, details: str) -> ResultPayload:
        return StartFlowResultFailure(validation_exceptions=[], result_details=details)


class TestACloseTurnsAwayItsWaitingRuns:
    """A run waiting in a closing workflow's line never starts against the flows being deleted."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("close", ["clear_all", "pop_workflow"])
    async def test_closing_a_workflow_turns_away_the_runs_waiting_in_it(self, engine: Engine, close: str) -> None:
        workflow = _open_with_canvas(engine)
        first_may_end = asyncio.Event()
        executed: list[str] = []

        async def first() -> ResultPayload:
            await first_may_end.wait()
            return StartFlowResultSuccess(result_details="first ran")

        async def second() -> ResultPayload:
            executed.append("second")
            return StartFlowResultSuccess(result_details="second ran")

        running = asyncio.ensure_future(engine.flow_manager.start_run(_RunSpec("canvas", first)))
        await asyncio.sleep(0)
        waiting = engine.flow_manager.submit_run(_RunSpec("canvas", second))

        if close == "clear_all":
            cleared = await engine.ahandle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
            assert cleared.succeeded(), cleared.result_details
        else:
            engine.context_manager.pop_workflow()
        turned_away = await asyncio.wait_for(waiting.outcome, timeout=5)
        first_may_end.set()
        await asyncio.wait_for(running, timeout=5)

        assert workflow.closed
        assert "did not start: it was cancelled" in str(turned_away.result_details)
        assert executed == []
        assert workflow.flows.run_queue.snapshot() == []


class TestEveryCloseTellsTheWorkers:
    """A sync close cannot await the workers' release, so it is sent on the engine loop."""

    @pytest.mark.asyncio
    async def test_a_sync_close_on_the_engine_loop_tells_the_workers(self, engine: Engine) -> None:
        workflow = _open_with_canvas(engine)
        worker_manager = engine.worker_manager

        with (
            patch.object(worker_manager, "_transport", object()),
            patch.object(worker_manager, "_workers", {"w1": object()}),
            patch.object(engine.event_manager, "_event_loop", asyncio.get_running_loop()),
            patch.object(worker_manager, "broadcast_drop_all_local_objects", AsyncMock()) as drop_all,
        ):
            engine.context_manager.pop_workflow()
            await asyncio.wait_for(asyncio.gather(*worker_manager._inflight_broadcast_tasks), timeout=5)

        drop_all.assert_awaited_once_with(workflow_id=workflow.workflow_id)

    @pytest.mark.asyncio
    async def test_a_workflow_block_closed_on_another_thread_tells_the_workers_on_the_engine_loop(
        self, engine: Engine
    ) -> None:
        worker_manager = engine.worker_manager
        told = asyncio.Event()
        closed_ids: list[str | None] = []

        async def drop_all(*, workflow_id: str | None = None) -> None:
            closed_ids.append(workflow_id)
            told.set()

        def run_block() -> str:
            with engine.context_manager.workflow("block"):
                return engine.context_manager.get_current_workflow_id()

        with (
            patch.object(worker_manager, "_transport", object()),
            patch.object(worker_manager, "_workers", {"w1": object()}),
            patch.object(engine.event_manager, "_event_loop", asyncio.get_running_loop()),
            patch.object(worker_manager, "broadcast_drop_all_local_objects", side_effect=drop_all),
        ):
            block_id = await asyncio.to_thread(run_block)
            await asyncio.wait_for(told.wait(), timeout=5)

        assert closed_ids == [block_id]
