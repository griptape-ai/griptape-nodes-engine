"""The engine reports how long each node took to run on its ``NodeResolvedEvent``."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from griptape_nodes.machines.dag_builder import DagNode
from griptape_nodes.machines.parallel_resolution import ExecuteDagState, ParallelResolutionContext
from griptape_nodes.retained_mode.events.execution_events import NodeResolvedEvent
from tests.unit.machines.scheduler_stubs import node_stub

NODE_RUN_SECONDS = 0.05


class TestNodeDuration:
    @pytest.mark.asyncio
    async def test_execute_node_records_how_long_the_node_ran(self) -> None:
        async def _slow_execute(_node: object) -> None:
            await asyncio.sleep(NODE_RUN_SECONDS)

        engine = MagicMock()
        engine.flow_manager.node_executor.execute = _slow_execute
        dag_node = DagNode(node_reference=node_stub("slow"))

        await ExecuteDagState.execute_node(engine, dag_node)

        assert dag_node.execution_duration_ms is not None
        assert dag_node.execution_duration_ms >= NODE_RUN_SECONDS * 1000 * 0.9

    @pytest.mark.asyncio
    async def test_execute_node_leaves_no_duration_when_the_node_fails(self) -> None:
        engine = MagicMock()
        engine.flow_manager.node_executor.execute = AsyncMock(side_effect=RuntimeError("boom"))
        dag_node = DagNode(node_reference=node_stub("broken"), execution_duration_ms=12.0)

        with pytest.raises(RuntimeError):
            await ExecuteDagState.execute_node(engine, dag_node)

        assert dag_node.execution_duration_ms is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("duration_ms", [312.5, None])
    async def test_node_resolved_event_carries_the_recorded_duration(
        self, monkeypatch: pytest.MonkeyPatch, duration_ms: float | None
    ) -> None:
        engine = MagicMock()
        engine.event_manager.aput_event = AsyncMock()
        context = ParallelResolutionContext("flow", max_nodes_in_parallel=5, dag_builder=MagicMock(), engine=engine)
        node = node_stub("done")
        node.parameter_values = {}
        dag_node = DagNode(node_reference=node, execution_duration_ms=duration_ms)
        monkeypatch.setattr(ExecuteDagState, "get_next_control_graph", MagicMock())
        monkeypatch.setattr(ExecuteDagState, "check_for_new_start_nodes", MagicMock())

        await ExecuteDagState.handle_done_nodes(context, dag_node, "flow")

        payloads = [call.args[0].wrapped_event.payload for call in engine.event_manager.aput_event.await_args_list]
        resolved = [payload for payload in payloads if isinstance(payload, NodeResolvedEvent)]
        assert len(resolved) == 1
        assert resolved[0].duration_ms == duration_ms
