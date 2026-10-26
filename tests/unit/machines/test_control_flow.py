"""Tests for ControlFlowMachine DAG-seeding helpers.

These cover the two scope-agnostic helpers that back both the top-level run and the isolated
subflow run:

- ``_drain_global_flow_queue`` turns the already-scoped global flow queue into categorized node
  lists (and must empty the queue, preserving the contract that the queue is consumed during DAG
  construction).
- ``_seed_dag_from_categories`` seeds a DagBuilder from those categories: PASS 1 adds start/control
  entry nodes, PASS 2 adds data sinks, either as their own graph (data-only) or as control-gated
  candidates (reachable from a control graph's forward path).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from griptape_nodes.exe_types.node_groups.subflow_node_group import SubflowNodeGroup
from griptape_nodes.exe_types.node_types import BaseNode, NodeResolutionState
from griptape_nodes.machines.control_flow import ControlFlowMachine
from griptape_nodes.machines.dag_builder import DagBuilder, DagNodeCategories
from griptape_nodes.retained_mode.managers.flow_manager import DagExecutionType, QueueItem

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine


def _engine_with_connections(connections: MagicMock) -> MagicMock:
    """Build a stub engine whose flow_manager hands back `connections`.

    DagBuilder resolves connections through its injected engine, so a stub engine replaces what
    used to be a patch of the process-wide facade. Passing it explicitly also keeps the seeding
    helpers off the real engine a bare DagBuilder would otherwise fall back to.
    """
    engine = MagicMock()
    engine.flow_manager.get_connections.return_value = connections
    return engine


def _mock_node(name: str) -> MagicMock:
    """A BaseNode stand-in with the surface the seeding helpers touch."""
    node = MagicMock(spec=BaseNode)
    node.name = name
    node.parameters = []
    node.state = NodeResolutionState.RESOLVED
    return node


class TestDrainGlobalFlowQueue:
    """Tests for ControlFlowMachine._drain_global_flow_queue."""

    def test_buckets_items_by_type_and_empties_queue(self, engine: Engine) -> None:
        flow_manager = engine.flow_manager
        flow_manager.global_flow_queue.queue.clear()

        start = _mock_node("start")
        control = _mock_node("control")
        data = _mock_node("data")
        flow_manager.global_flow_queue.put(QueueItem(node=start, dag_execution_type=DagExecutionType.START_NODE))
        flow_manager.global_flow_queue.put(QueueItem(node=control, dag_execution_type=DagExecutionType.CONTROL_NODE))
        flow_manager.global_flow_queue.put(QueueItem(node=data, dag_execution_type=DagExecutionType.DATA_NODE))

        categories = ControlFlowMachine._drain_global_flow_queue(flow_manager)

        assert [node.name for node in categories.start_nodes] == ["start"]
        assert [node.name for node in categories.control_nodes] == ["control"]
        assert [node.name for node in categories.data_sink_nodes] == ["data"]
        # The queue must be fully drained.
        assert flow_manager.global_flow_queue.empty()

    def test_empty_queue_yields_empty_categories(self, engine: Engine) -> None:
        flow_manager = engine.flow_manager
        flow_manager.global_flow_queue.queue.clear()

        categories = ControlFlowMachine._drain_global_flow_queue(flow_manager)

        assert categories.start_nodes == []
        assert categories.control_nodes == []
        assert categories.data_sink_nodes == []


class TestSeedDagFromCategories:
    """Tests for ControlFlowMachine._seed_dag_from_categories."""

    @staticmethod
    def _connections(*, downstream: set[str] | None = None) -> MagicMock:
        """A Connections stand-in: `collect_downstream_data_nodes` returns `downstream` for any root."""
        connections = MagicMock()
        connections.get_connected_node.return_value = None
        connections.collect_downstream_data_nodes.return_value = set(downstream or ())
        return connections

    def test_pass1_adds_start_and_control_entries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        start = _mock_node("Start")
        control = _mock_node("Ctrl")
        categories = DagNodeCategories(start_nodes=[start], control_nodes=[control], data_sink_nodes=[])
        connections = self._connections()
        dag_builder = DagBuilder(_engine_with_connections(connections))
        monkeypatch.setattr(
            DagBuilder, "collect_nodes_in_forward_control_path", staticmethod(lambda n, _e, _c: {n.name})
        )

        entry_nodes = ControlFlowMachine._seed_dag_from_categories(start, categories, dag_builder, connections)

        # The start node plus every entry node come back so the control flow can begin.
        assert [node.name for node in entry_nodes] == ["Start", "Ctrl"]
        assert "Start" in dag_builder.node_to_reference
        assert "Ctrl" in dag_builder.node_to_reference
        # Entry nodes are reset so they re-resolve on this run.
        assert start.state == NodeResolutionState.UNRESOLVED
        assert control.state == NodeResolutionState.UNRESOLVED

    def test_pass2_defers_a_sink_fed_by_the_control_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        start = _mock_node("Start")
        sink = _mock_node("Sink")
        categories = DagNodeCategories(start_nodes=[start], control_nodes=[], data_sink_nodes=[sink])
        # Start's control path reaches Ctrl; Ctrl feeds Middle which feeds Sink (data edges).
        connections = self._connections(downstream={"Middle", "Sink"})
        dag_builder = DagBuilder(_engine_with_connections(connections))
        monkeypatch.setattr(
            DagBuilder, "collect_nodes_in_forward_control_path", staticmethod(lambda n, _e, _c: {n.name, "Ctrl"})
        )

        ControlFlowMachine._seed_dag_from_categories(start, categories, dag_builder, connections)

        # The sink is marked stale now but seeded later, by the driver, once control has run.
        assert sink.state == NodeResolutionState.UNRESOLVED
        assert "Sink" not in dag_builder.node_to_reference
        assert dag_builder.deferred_data_sinks == [sink]
        assert dag_builder.start_node_candidates == {}

    def test_pass2_seeds_a_sink_the_control_path_never_feeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        start = _mock_node("Start")
        sink = _mock_node("Sink")
        categories = DagNodeCategories(start_nodes=[start], control_nodes=[], data_sink_nodes=[sink])
        connections = self._connections(downstream={"Other"})
        dag_builder = DagBuilder(_engine_with_connections(connections))
        monkeypatch.setattr(
            DagBuilder, "collect_nodes_in_forward_control_path", staticmethod(lambda n, _e, _c: {n.name, "Ctrl"})
        )

        ControlFlowMachine._seed_dag_from_categories(start, categories, dag_builder, connections)

        # Nothing above the sink will re-run, so it starts alongside the control graph.
        assert "Sink" in dag_builder.node_to_reference
        assert dag_builder.deferred_data_sinks == []

    def test_sink_already_in_dag_is_not_deferred(self, monkeypatch: pytest.MonkeyPatch) -> None:
        start = _mock_node("Start")
        sink = _mock_node("Sink")
        categories = DagNodeCategories(start_nodes=[start], control_nodes=[], data_sink_nodes=[sink])
        connections = self._connections(downstream={"Sink"})
        dag_builder = DagBuilder(_engine_with_connections(connections))
        monkeypatch.setattr(
            DagBuilder, "collect_nodes_in_forward_control_path", staticmethod(lambda n, _e, _c: {n.name})
        )

        # Pre-seed the sink so PASS 2 sees it already present.
        dag_builder.add_node(sink)

        ControlFlowMachine._seed_dag_from_categories(start, categories, dag_builder, connections)

        assert dag_builder.deferred_data_sinks == []
        assert dag_builder.start_node_candidates == {}


class TestProcessNodesForDagIsolatedScope:
    """Tests for ControlFlowMachine._process_nodes_for_dag isolated-subflow scope selection.

    A SubflowNodeGroup's own subflow runs on the isolated path, and its direct nodes are the
    group's members, which all carry the owning group as their parent_group. The isolated scope
    must therefore be the subflow's nodes as-is: running them through the top-level
    exclude_subflow_group_children filter would drop every member and seed only the start node,
    so only one node would resolve.
    """

    @pytest.mark.asyncio
    async def test_isolated_scope_keeps_group_children(self, engine: Engine) -> None:
        machine = ControlFlowMachine("grp_subflow", is_isolated=True)
        # Swap the fresh DagBuilder for a stub: add_node_with_dependencies becomes a no-op, and the
        # is_isolated check (dag_builder is not the global one) still holds for a MagicMock.
        machine.context.resolution_machine.context.dag_builder = MagicMock()

        group = MagicMock(spec=SubflowNodeGroup)
        child_a = _mock_node("child_a")
        child_a.parent_group = group
        child_b = _mock_node("child_b")
        child_b.parent_group = group
        subflow = MagicMock()
        subflow.nodes = {"child_a": child_a, "child_b": child_b}

        flow_manager = engine.flow_manager
        captured: dict[str, list[BaseNode]] = {}

        def fake_classify(nodes: list[BaseNode]) -> DagNodeCategories:
            captured["scope"] = list(nodes)
            return DagNodeCategories(start_nodes=[], control_nodes=[], data_sink_nodes=list(nodes))

        with (
            patch.object(flow_manager, "get_flow_by_name", return_value=subflow),
            patch.object(flow_manager, "classify_nodes_for_dag", side_effect=fake_classify),
            patch.object(ControlFlowMachine, "_seed_dag_from_categories", return_value=[child_a]) as seed,
        ):
            entry_nodes = await machine._process_nodes_for_dag(child_a)

        # Both group members reach the classifier; the group-child filter is not applied here.
        assert {node.name for node in captured["scope"]} == {"child_a", "child_b"}
        # Applying the top-level filter to this scope would drop everything, which is exactly the
        # behavior the isolated path must avoid.
        assert flow_manager.exclude_subflow_group_children(captured["scope"]) == []
        # The categories built from the full scope are what get seeded.
        assert entry_nodes == [child_a]
        seed.assert_called_once()
