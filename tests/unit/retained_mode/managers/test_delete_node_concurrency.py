"""A node delete that waits on the running flow must not hold its node on the shared context stack."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.node_events import (
    DeleteNodeRequest,
    DeleteNodeResultFailure,
    DeleteNodeResultSuccess,
)
from griptape_nodes.retained_mode.managers.node_manager import _FlowCancelOutcome
from tests.unit.exe_types.mocks import MockNode

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.exe_types.flow import ControlFlow
    from griptape_nodes.exe_types.node_types import BaseNode
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

TIMEOUT = 5.0


class _RecordingNode(MockNode):
    """Records which node the workflow's context calls current when the engine tells it it is leaving."""

    def __init__(self, name: str, workflow: WorkflowRuntime, current_when_deleted: dict[str, str | None]) -> None:
        super().__init__(name)
        self._runtime = workflow
        self._current_when_deleted = current_when_deleted

    def after_node_deleted(self) -> None:
        context = self._runtime.context
        if context.has_current_node():
            self._current_when_deleted[self.name] = context.get_current_node().name
        else:
            self._current_when_deleted[self.name] = None


class _DeleteNodeTestBase:
    @pytest.fixture
    def workflow(self, engine: Engine) -> Iterator[WorkflowRuntime]:
        engine.context_manager.push_workflow("delete_concurrency_wf")
        yield engine.current_workflow()
        engine.context_manager.pop_workflow()

    @pytest.fixture
    def current_when_deleted(self) -> dict[str, str | None]:
        """The current node `after_node_deleted` saw, by deleted node name."""
        return {}

    @pytest.fixture
    def flow_with_nodes(
        self, engine: Engine, workflow: WorkflowRuntime, current_when_deleted: dict[str, str | None]
    ) -> ControlFlow:
        """A current flow holding two nodes registered the way `on_create_node_request` registers them."""
        result = engine.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="DeleteFlow", set_as_new_context=True)
        )
        assert isinstance(result, CreateFlowResultSuccess)
        flow = engine.flow_manager.get_flow_by_name_in(workflow, result.flow_name)
        for name in ("NodeA", "NodeB"):
            node = _RecordingNode(name, workflow, current_when_deleted)
            flow.add_node(node)
            engine.object_manager.add_object_by_name_in(workflow, name, node)
            workflow.nodes.name_to_parent_flow_name[name] = result.flow_name
        return flow


class TestDeleteNodeWhileAwaitingCancel(_DeleteNodeTestBase):
    @pytest.fixture
    def cancel_gates(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, asyncio.Event]]:
        """Park each delete inside `cancel_conditionally` until its gate's `release` event is set."""
        gates = {name: {"entered": asyncio.Event(), "release": asyncio.Event()} for name in ("NodeA", "NodeB")}

        async def cancel_conditionally(
            _workflow: WorkflowRuntime, _parent_flow: ControlFlow, _parent_flow_name: str, node: BaseNode
        ) -> _FlowCancelOutcome:
            gates[node.name]["entered"].set()
            await asyncio.wait_for(gates[node.name]["release"].wait(), timeout=TIMEOUT)
            return _FlowCancelOutcome()

        monkeypatch.setattr(engine.node_manager, "cancel_conditionally", cancel_conditionally)
        return gates

    @pytest.mark.asyncio
    async def test_a_flow_pushed_while_the_delete_waits_does_not_break_the_delete(
        self,
        engine: Engine,
        workflow: WorkflowRuntime,
        flow_with_nodes: ControlFlow,
        cancel_gates: dict[str, dict[str, asyncio.Event]],
    ) -> None:
        """Guard the original `EmptyStackError`, which the context stack now avoids by itself.

        It passes without the delete fix and does not test the remaining hazards, which the next two tests cover.
        """
        delete = asyncio.create_task(
            engine.node_manager.on_delete_node_request(DeleteNodeRequest(node_name="NodeA"), workflow)
        )
        await asyncio.wait_for(cancel_gates["NodeA"]["entered"].wait(), timeout=TIMEOUT)

        # Another request makes a new flow the current one while the delete is parked.
        pushed = engine.handle_request(
            CreateFlowRequest(parent_flow_name=flow_with_nodes.name, flow_name="Other", set_as_new_context=True)
        )
        assert isinstance(pushed, CreateFlowResultSuccess)
        cancel_gates["NodeA"]["release"].set()
        result = await asyncio.wait_for(delete, timeout=TIMEOUT)

        assert isinstance(result, DeleteNodeResultSuccess)
        assert not workflow.context.has_current_node()
        assert not workflow.context._flow_stack[0].has_current_node()

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("flow_with_nodes")
    async def test_another_task_sees_no_current_node_while_the_delete_waits(
        self,
        engine: Engine,
        workflow: WorkflowRuntime,
        cancel_gates: dict[str, dict[str, asyncio.Event]],
    ) -> None:
        delete = asyncio.create_task(
            engine.node_manager.on_delete_node_request(DeleteNodeRequest(node_name="NodeA"), workflow)
        )
        await asyncio.wait_for(cancel_gates["NodeA"]["entered"].wait(), timeout=TIMEOUT)

        seen_while_parked = workflow.context.has_current_node()
        cancel_gates["NodeA"]["release"].set()
        await asyncio.wait_for(delete, timeout=TIMEOUT)

        assert not seen_while_parked

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("flow_with_nodes")
    async def test_concurrent_deletes_each_run_with_their_own_node_current(
        self,
        engine: Engine,
        workflow: WorkflowRuntime,
        current_when_deleted: dict[str, str | None],
        cancel_gates: dict[str, dict[str, asyncio.Event]],
    ) -> None:
        delete_a = asyncio.create_task(
            engine.node_manager.on_delete_node_request(DeleteNodeRequest(node_name="NodeA"), workflow)
        )
        delete_b = asyncio.create_task(
            engine.node_manager.on_delete_node_request(DeleteNodeRequest(node_name="NodeB"), workflow)
        )
        await asyncio.wait_for(cancel_gates["NodeA"]["entered"].wait(), timeout=TIMEOUT)
        await asyncio.wait_for(cancel_gates["NodeB"]["entered"].wait(), timeout=TIMEOUT)

        cancel_gates["NodeA"]["release"].set()
        result_a = await asyncio.wait_for(delete_a, timeout=TIMEOUT)
        cancel_gates["NodeB"]["release"].set()
        result_b = await asyncio.wait_for(delete_b, timeout=TIMEOUT)

        assert isinstance(result_a, DeleteNodeResultSuccess)
        assert isinstance(result_b, DeleteNodeResultSuccess)
        assert current_when_deleted == {"NodeA": "NodeA", "NodeB": "NodeB"}
        assert not workflow.context.has_current_node()


class TestDeleteNodeBeforeTheNodeContext(_DeleteNodeTestBase):
    """The lookups and the cancel run before the node is put on the context stack, so a failure leaves nothing on it."""

    @pytest.fixture
    def cancel_conditionally(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
        mock = AsyncMock(spec=engine.node_manager.cancel_conditionally, return_value=_FlowCancelOutcome())
        monkeypatch.setattr(engine.node_manager, "cancel_conditionally", mock)
        return mock

    @pytest.mark.asyncio
    async def test_fails_without_cancelling_when_the_parent_flow_is_missing(
        self,
        engine: Engine,
        workflow: WorkflowRuntime,
        flow_with_nodes: ControlFlow,
        cancel_conditionally: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setitem(workflow.nodes.name_to_parent_flow_name, "NodeA", "NoSuchFlow")

        result = await engine.node_manager.on_delete_node_request(DeleteNodeRequest(node_name="NodeA"), workflow)

        assert isinstance(result, DeleteNodeResultFailure)
        assert result.result_details is not None
        assert "NodeA" in str(result.result_details)
        assert "NoSuchFlow" in str(result.result_details)
        cancel_conditionally.assert_not_awaited()
        assert "NodeA" in flow_with_nodes.nodes
        assert not workflow.context.has_current_node()

    @pytest.mark.asyncio
    async def test_returns_the_cancel_failure_and_keeps_the_node(
        self, engine: Engine, workflow: WorkflowRuntime, flow_with_nodes: ControlFlow, cancel_conditionally: AsyncMock
    ) -> None:
        failure = DeleteNodeResultFailure(result_details="running flow could not cancel")
        cancel_conditionally.return_value = _FlowCancelOutcome(failure=failure)

        result = await engine.node_manager.on_delete_node_request(DeleteNodeRequest(node_name="NodeA"), workflow)

        assert result is failure
        cancel_conditionally.assert_awaited_once_with(
            workflow, flow_with_nodes, flow_with_nodes.name, flow_with_nodes.nodes["NodeA"]
        )
        assert "NodeA" in flow_with_nodes.nodes
        assert engine.object_manager.attempt_get_object_by_name("NodeA") is not None
        assert not workflow.context.has_current_node()
