"""The engine reports how long each node took, on the `NodeResolvedEvent` that announces it finished."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from griptape_nodes.exe_types.node_types import BaseNode, NodeResolutionState
from griptape_nodes.machines.dag_builder import DagNode, NodeState
from griptape_nodes.machines.parallel_resolution import ExecuteDagState, ParallelResolutionContext
from griptape_nodes.retained_mode.events.execution_events import NodeResolvedEvent


def _node(name: str = "n") -> MagicMock:
    node = MagicMock(spec=BaseNode)
    node.name = name
    node.lock = False
    node.state = NodeResolutionState.RESOLVING
    node.parameter_values = {}
    node.parameter_output_values = {}
    return node


class TestNodeDuration:
    @pytest.mark.asyncio
    async def test_execute_node_records_how_long_the_node_took(self) -> None:
        async def _slow_execute(_node: BaseNode) -> None:
            await asyncio.sleep(0.05)

        engine = MagicMock()
        engine.flow_manager.node_executor.execute = _slow_execute
        dag_node = DagNode(node_reference=_node(), node_state=NodeState.PROCESSING)

        assert dag_node.duration_ms is None

        await ExecuteDagState.execute_node(engine, dag_node)

        assert dag_node.duration_ms is not None
        assert dag_node.duration_ms >= 40  # noqa: PLR2004 (sleep of 50 ms, minus timer slack)

    @pytest.mark.asyncio
    async def test_node_resolved_event_carries_the_recorded_duration(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ExecuteDagState, "get_next_control_graph", MagicMock())
        monkeypatch.setattr(ExecuteDagState, "check_for_new_start_nodes", MagicMock())
        engine = MagicMock()
        engine.event_manager.aput_event = AsyncMock()
        context = ParallelResolutionContext("flow", engine=engine)
        dag_node = DagNode(node_reference=_node(), node_state=NodeState.DONE, duration_ms=312.5)

        await ExecuteDagState.handle_done_nodes(context, dag_node, "network")

        payloads = [call.args[0].wrapped_event.payload for call in engine.event_manager.aput_event.await_args_list]
        resolved = [payload for payload in payloads if isinstance(payload, NodeResolvedEvent)]
        assert [event.duration_ms for event in resolved] == [312.5]

    @pytest.mark.asyncio
    async def test_node_resolved_event_has_no_duration_for_a_node_that_was_not_executed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ExecuteDagState, "get_next_control_graph", MagicMock())
        monkeypatch.setattr(ExecuteDagState, "check_for_new_start_nodes", MagicMock())
        engine = MagicMock()
        engine.event_manager.aput_event = AsyncMock()
        context = ParallelResolutionContext("flow", engine=engine)
        dag_node = DagNode(node_reference=_node(), node_state=NodeState.DONE)

        await ExecuteDagState.handle_done_nodes(context, dag_node, "network")

        payloads = [call.args[0].wrapped_event.payload for call in engine.event_manager.aput_event.await_args_list]
        resolved = [payload for payload in payloads if isinstance(payload, NodeResolvedEvent)]
        assert [event.duration_ms for event in resolved] == [None]
