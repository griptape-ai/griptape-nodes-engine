"""A node delete that waits on the running flow must not leave its node on the shared context stack."""

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

TIMEOUT = 5.0


class _DeleteNodeTestBase:
    @pytest.fixture
    def flow_with_nodes(self, engine: Engine) -> Iterator[ControlFlow]:
        """A current flow holding two nodes registered the way `on_create_node_request` registers them."""
        engine.context_manager.push_workflow("delete_concurrency_wf")
        result = engine.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="DeleteFlow", set_as_new_context=True)
        )
        assert isinstance(result, CreateFlowResultSuccess)
        flow = engine.flow_manager.get_flow_by_name(result.flow_name)
        for name in ("NodeA", "NodeB"):
            node = MockNode(name)
            flow.add_node(node)
            engine.object_manager.add_object_by_name(name, node)
            engine.node_manager._name_to_parent_flow_name[name] = result.flow_name
        yield flow
        engine.context_manager.pop_workflow()


class TestDeleteNodeWhileAwaitingCancel(_DeleteNodeTestBase):
    @pytest.fixture
    def cancel_started(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> dict[str, asyncio.Event]:
        """Park every delete inside `cancel_conditionally` until `cancel_started['release']` is set."""
        events = {"entered": asyncio.Event(), "release": asyncio.Event()}

        async def cancel_conditionally(
            _parent_flow: ControlFlow, _parent_flow_name: str, _node: BaseNode
        ) -> _FlowCancelOutcome:
            events["entered"].set()
            await asyncio.wait_for(events["release"].wait(), timeout=TIMEOUT)
            return _FlowCancelOutcome()

        monkeypatch.setattr(engine.node_manager, "cancel_conditionally", cancel_conditionally)
        return events

    @pytest.mark.asyncio
    async def test_a_flow_pushed_while_the_delete_waits_does_not_break_the_delete(
        self, engine: Engine, flow_with_nodes: ControlFlow, cancel_started: dict[str, asyncio.Event]
    ) -> None:
        context_manager = engine.context_manager
        delete = asyncio.create_task(engine.node_manager.on_delete_node_request(DeleteNodeRequest(node_name="NodeA")))
        await asyncio.wait_for(cancel_started["entered"].wait(), timeout=TIMEOUT)

        # Another request makes a new flow the current one while the delete is parked.
        pushed = engine.handle_request(
            CreateFlowRequest(parent_flow_name=flow_with_nodes.name, flow_name="Other", set_as_new_context=True)
        )
        assert isinstance(pushed, CreateFlowResultSuccess)
        cancel_started["release"].set()
        result = await asyncio.wait_for(delete, timeout=TIMEOUT)

        assert isinstance(result, DeleteNodeResultSuccess)
        assert not context_manager.has_current_node()
        assert not context_manager._workflow_stack[-1]._flow_stack[0].has_current_node()


class TestDeleteNodeBeforeTheNodeContext(_DeleteNodeTestBase):
    """The lookups and the cancel run before the node is put on the context stack, so a failure leaves nothing on it."""

    @pytest.fixture
    def cancel_conditionally(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
        mock = AsyncMock(spec=engine.node_manager.cancel_conditionally, return_value=_FlowCancelOutcome())
        monkeypatch.setattr(engine.node_manager, "cancel_conditionally", mock)
        return mock

    @pytest.mark.asyncio
    async def test_fails_without_cancelling_when_the_parent_flow_is_missing(
        self, engine: Engine, flow_with_nodes: ControlFlow, cancel_conditionally: AsyncMock
    ) -> None:
        engine.node_manager._name_to_parent_flow_name["NodeA"] = "NoSuchFlow"

        result = await engine.node_manager.on_delete_node_request(DeleteNodeRequest(node_name="NodeA"))

        assert isinstance(result, DeleteNodeResultFailure)
        assert result.result_details is not None
        assert "NodeA" in str(result.result_details)
        assert "NoSuchFlow" in str(result.result_details)
        cancel_conditionally.assert_not_awaited()
        assert "NodeA" in flow_with_nodes.nodes
        assert not engine.context_manager.has_current_node()

    @pytest.mark.asyncio
    async def test_returns_the_cancel_failure_and_keeps_the_node(
        self, engine: Engine, flow_with_nodes: ControlFlow, cancel_conditionally: AsyncMock
    ) -> None:
        failure = DeleteNodeResultFailure(result_details="running flow could not cancel")
        cancel_conditionally.return_value = _FlowCancelOutcome(failure=failure)

        result = await engine.node_manager.on_delete_node_request(DeleteNodeRequest(node_name="NodeA"))

        assert result is failure
        cancel_conditionally.assert_awaited_once_with(
            flow_with_nodes, flow_with_nodes.name, flow_with_nodes.nodes["NodeA"]
        )
        assert "NodeA" in flow_with_nodes.nodes
        assert engine.object_manager.attempt_get_object_by_name("NodeA") is not None
        assert not engine.context_manager.has_current_node()
