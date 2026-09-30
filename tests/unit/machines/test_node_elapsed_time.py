"""The engine reports how long each node took to execute on its NodeResolvedEvent."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from griptape_nodes.exe_types.node_types import BaseNode, NodeResolutionState
from griptape_nodes.machines.dag_builder import DagNode
from griptape_nodes.machines.parallel_resolution import ExecuteDagState
from griptape_nodes.node_library.library_registry import LibraryRegistry
from griptape_nodes.retained_mode.events.execution_events import NodeResolvedEvent

ELAPSED_MS = 312.5


def _dag_node(name: str) -> DagNode:
    node = MagicMock(spec=BaseNode)
    node.name = name
    node.lock = False
    node.state = NodeResolutionState.RESOLVING
    node.parameter_values = {}
    node.parameter_output_values = {}
    return DagNode(node_reference=node)


class TestExecuteNodeElapsedTime:
    @pytest.mark.asyncio
    async def test_records_elapsed_milliseconds_on_the_dag_node(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = iter([10.0, 10.25])
        monkeypatch.setattr("griptape_nodes.machines.parallel_resolution.time.perf_counter", lambda: next(clock))
        engine = MagicMock()
        engine.flow_manager.node_executor.execute = AsyncMock()
        dag_node = _dag_node("Blur")

        await ExecuteDagState.execute_node(engine, dag_node)

        assert dag_node.elapsed_ms == pytest.approx(250.0)

    def test_is_unset_until_the_node_has_executed(self) -> None:
        assert _dag_node("Blur").elapsed_ms is None


class TestNodeResolvedEventElapsedTime:
    @pytest.mark.asyncio
    async def test_carries_the_elapsed_milliseconds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(LibraryRegistry, "get_libraries_with_node_type", MagicMock(return_value=["Library"]))
        monkeypatch.setattr(ExecuteDagState, "get_next_control_graph", MagicMock())
        monkeypatch.setattr(ExecuteDagState, "check_for_new_start_nodes", MagicMock())
        monkeypatch.setattr(ExecuteDagState, "_unresolve_if_an_input_was_torn_down", MagicMock())
        context = MagicMock()
        context.engine.event_manager.aput_event = AsyncMock()
        dag_node = _dag_node("Blur")
        dag_node.elapsed_ms = ELAPSED_MS

        await ExecuteDagState.handle_done_nodes(context, dag_node, "network")

        sent_events = [call.args[0] for call in context.engine.event_manager.aput_event.await_args_list]
        resolved = [
            e.wrapped_event.payload for e in sent_events if isinstance(e.wrapped_event.payload, NodeResolvedEvent)
        ]
        assert len(resolved) == 1
        assert resolved[0].elapsed_ms == ELAPSED_MS

    def test_defaults_to_no_timing(self) -> None:
        event = NodeResolvedEvent(node_name="Blur", parameter_output_values={}, node_type="Blur")
        assert event.elapsed_ms is None
