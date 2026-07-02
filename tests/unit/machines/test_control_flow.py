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
    from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes


def _mock_node(name: str) -> MagicMock:
    """A BaseNode stand-in with the surface the seeding helpers touch."""
    node = MagicMock(spec=BaseNode)
    node.name = name
    node.parameters = []
    node.state = NodeResolutionState.RESOLVED
    return node


class TestDrainGlobalFlowQueue:
    """Tests for ControlFlowMachine._drain_global_flow_queue."""

    def test_buckets_items_by_type_and_empties_queue(self, griptape_nodes: GriptapeNodes) -> None:
        flow_manager = griptape_nodes.FlowManager()
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

    def test_empty_queue_yields_empty_categories(self, griptape_nodes: GriptapeNodes) -> None:
        flow_manager = griptape_nodes.FlowManager()
        flow_manager.global_flow_queue.queue.clear()

        categories = ControlFlowMachine._drain_global_flow_queue(flow_manager)

        assert categories.start_nodes == []
        assert categories.control_nodes == []
        assert categories.data_sink_nodes == []


class TestSeedDagFromCategories:
    """Tests for ControlFlowMachine._seed_dag_from_categories."""

    def test_pass1_adds_start_and_control_entries(self) -> None:
        start = _mock_node("Start")
        control = _mock_node("Ctrl")
        categories = DagNodeCategories(start_nodes=[start], control_nodes=[control], data_sink_nodes=[])
        flow_manager = MagicMock()
        node_manager = MagicMock()

        with patch("griptape_nodes.retained_mode.griptape_nodes.GriptapeNodes.FlowManager") as mock_flow_manager:
            connections = MagicMock()
            connections.get_connected_node.return_value = None
            mock_flow_manager.return_value.get_connections.return_value = connections

            dag_builder = DagBuilder()
            entry_nodes = ControlFlowMachine._seed_dag_from_categories(
                start, categories, dag_builder, flow_manager, node_manager
            )

        # The start node plus every entry node come back so the control flow can begin.
        assert [node.name for node in entry_nodes] == ["Start", "Ctrl"]
        assert "Start" in dag_builder.node_to_reference
        assert "Ctrl" in dag_builder.node_to_reference
        # Entry nodes are reset so they re-resolve on this run.
        assert start.state == NodeResolutionState.UNRESOLVED
        assert control.state == NodeResolutionState.UNRESOLVED

    def test_pass2_disconnected_sink_gets_its_own_graph(self) -> None:
        start = _mock_node("Start")
        sink = _mock_node("Sink")
        categories = DagNodeCategories(start_nodes=[start], control_nodes=[], data_sink_nodes=[sink])
        # is_node_connected returns [] -> the sink is not gated by any control graph.
        flow_manager = MagicMock()
        flow_manager.is_node_connected.return_value = []
        node_manager = MagicMock()

        with patch("griptape_nodes.retained_mode.griptape_nodes.GriptapeNodes.FlowManager") as mock_flow_manager:
            connections = MagicMock()
            connections.get_connected_node.return_value = None
            mock_flow_manager.return_value.get_connections.return_value = connections

            dag_builder = DagBuilder()
            ControlFlowMachine._seed_dag_from_categories(start, categories, dag_builder, flow_manager, node_manager)

        # A data-only sink is seeded directly so its dependencies resolve unconditionally.
        assert "Sink" in dag_builder.node_to_reference
        assert dag_builder.start_node_candidates == {}

    def test_pass2_gated_sink_becomes_control_candidate(self) -> None:
        start = _mock_node("Start")
        sink = _mock_node("Sink")
        categories = DagNodeCategories(start_nodes=[start], control_nodes=[], data_sink_nodes=[sink])
        # is_node_connected returns boundary nodes -> the sink is gated by Start's control graph.
        flow_manager = MagicMock()
        flow_manager.is_node_connected.return_value = ["Start"]
        node_manager = MagicMock()
        node_manager.get_node_by_name.side_effect = lambda name: start if name == "Start" else None

        with patch("griptape_nodes.retained_mode.griptape_nodes.GriptapeNodes.FlowManager") as mock_flow_manager:
            connections = MagicMock()
            connections.get_connected_node.return_value = None
            mock_flow_manager.return_value.get_connections.return_value = connections

            dag_builder = DagBuilder()
            ControlFlowMachine._seed_dag_from_categories(start, categories, dag_builder, flow_manager, node_manager)

        # A gated sink is not seeded directly; it is registered as a candidate keyed by the graph
        # start so the branch stays control-gated.
        assert "Sink" not in dag_builder.node_to_reference
        assert dag_builder.start_node_candidates["Sink"]["Start"] == {"Start"}

    def test_sink_already_in_dag_is_skipped(self) -> None:
        start = _mock_node("Start")
        sink = _mock_node("Sink")
        categories = DagNodeCategories(start_nodes=[start], control_nodes=[], data_sink_nodes=[sink])
        flow_manager = MagicMock()
        node_manager = MagicMock()

        with patch("griptape_nodes.retained_mode.griptape_nodes.GriptapeNodes.FlowManager") as mock_flow_manager:
            connections = MagicMock()
            connections.get_connected_node.return_value = None
            mock_flow_manager.return_value.get_connections.return_value = connections

            dag_builder = DagBuilder()
            # Pre-seed the sink so PASS 2 sees it already present.
            dag_builder.add_node(sink)

            ControlFlowMachine._seed_dag_from_categories(start, categories, dag_builder, flow_manager, node_manager)

        # Already-present sinks short-circuit before any connectivity check.
        flow_manager.is_node_connected.assert_not_called()
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
    async def test_isolated_scope_keeps_group_children(self, griptape_nodes: GriptapeNodes) -> None:
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

        flow_manager = griptape_nodes.FlowManager()
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
