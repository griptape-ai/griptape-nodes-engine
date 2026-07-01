"""Tests for DagBuilder class."""

# ruff: noqa: PLR2004

from typing import Any
from unittest.mock import MagicMock, patch

from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.machines.dag_builder import DagBuilder, DagNode, DagNodeCategories, NodeState


class TestDagNodeCategories:
    """Test cases for the DagNodeCategories seeding contract.

    The classifier (FlowManager.classify_nodes_for_dag) and the queue drainer
    (ControlFlowMachine._drain_global_flow_queue) both produce this tuple, and the DAG-seeding
    routine consumes it positionally, so the field order/names are part of the shared contract.
    """

    def test_fields_are_accessible_by_name_and_position(self) -> None:
        start = MagicMock(spec=BaseNode)
        control = MagicMock(spec=BaseNode)
        sink = MagicMock(spec=BaseNode)

        categories = DagNodeCategories(start_nodes=[start], control_nodes=[control], data_sink_nodes=[sink])

        assert categories.start_nodes == [start]
        assert categories.control_nodes == [control]
        assert categories.data_sink_nodes == [sink]
        # Positional order backs the tuple-unpacking consumers.
        assert tuple(categories) == ([start], [control], [sink])


class TestDagBuilder:
    """Test cases for DagBuilder functionality."""

    def test_init_creates_empty_dag_builder(self) -> None:
        """Test that initialization creates an empty DAG builder."""
        dag_builder = DagBuilder()

        assert len(dag_builder.graphs) == 0
        assert dag_builder.node_to_reference == {}

    def test_add_node_creates_dag_node(self) -> None:
        """Test that add_node creates a DagNode with correct initial state."""
        dag_builder = DagBuilder()
        mock_node = MagicMock(spec=BaseNode)
        mock_node.name = "test_node"

        dag_node = dag_builder.add_node(mock_node)

        assert isinstance(dag_node, DagNode)
        assert dag_node.node_reference is mock_node
        assert dag_node.node_state == NodeState.WAITING
        assert dag_node.task_reference is None

    def test_add_node_adds_to_graph(self) -> None:
        """Test that add_node adds node to the internal graph."""
        dag_builder = DagBuilder()
        mock_node = MagicMock(spec=BaseNode)
        mock_node.name = "test_node"

        dag_builder.add_node(mock_node)

        default_graph = dag_builder.graphs.get("default")
        assert default_graph is not None
        assert "test_node" in default_graph.nodes()
        assert "test_node" in dag_builder.node_to_reference

    def test_add_node_duplicate_returns_existing(self) -> None:
        """Test that adding the same node twice returns the existing DagNode."""
        dag_builder = DagBuilder()
        mock_node = MagicMock(spec=BaseNode)
        mock_node.name = "test_node"

        dag_node1 = dag_builder.add_node(mock_node)
        dag_node2 = dag_builder.add_node(mock_node)

        assert dag_node1 is dag_node2
        default_graph = dag_builder.graphs.get("default")
        assert default_graph is not None
        assert len(default_graph.nodes()) == 1
        assert len(dag_builder.node_to_reference) == 1

    def test_add_node_with_dependencies_no_connections(self) -> None:
        """Test adding a node with no upstream dependencies."""
        dag_builder = DagBuilder()
        mock_node = MagicMock(spec=BaseNode)
        mock_node.name = "test_node"
        mock_node.parameters = []

        # Mock the FlowManager to return no connections
        with patch("griptape_nodes.retained_mode.griptape_nodes.GriptapeNodes.FlowManager") as mock_flow_manager:
            mock_connections = MagicMock()
            mock_connections.get_connected_node.return_value = None
            mock_flow_manager.return_value.get_connections.return_value = mock_connections

            added_nodes = dag_builder.add_node_with_dependencies(mock_node)

            assert len(added_nodes) == 1
            assert added_nodes[0] is mock_node
            default_graph = dag_builder.graphs.get("default")
            assert default_graph is not None
            assert "test_node" in default_graph.nodes()

    def test_add_node_with_dependencies_with_upstream_nodes(self) -> None:
        """Test adding a node with upstream dependencies."""
        dag_builder = DagBuilder()

        # Create mock nodes
        upstream_node = MagicMock(spec=BaseNode)
        upstream_node.name = "upstream_node"
        upstream_node.parameters = []
        upstream_node.state = MagicMock()
        upstream_node.initialize_spotlight.return_value = MagicMock()  # Non-None return

        downstream_node = MagicMock(spec=BaseNode)
        downstream_node.name = "downstream_node"
        downstream_node.state = MagicMock()
        downstream_node.initialize_spotlight.return_value = MagicMock()  # Non-None return

        # Create mock parameter
        mock_param = MagicMock()
        mock_param.type = "str"  # Not CONTROL_TYPE
        downstream_node.parameters = [mock_param]

        # Mock the FlowManager connections
        with patch("griptape_nodes.retained_mode.griptape_nodes.GriptapeNodes.FlowManager") as mock_flow_manager:
            mock_connections = MagicMock()
            # First call (for downstream_node): return upstream connection
            # Second call (for upstream_node): return None
            mock_connections.get_connected_node.side_effect = [
                (upstream_node, MagicMock()),  # downstream_node has upstream dependency
                None,  # upstream_node has no dependencies
            ]
            mock_flow_manager.return_value.get_connections.return_value = mock_connections

            added_nodes = dag_builder.add_node_with_dependencies(downstream_node)

            assert len(added_nodes) == 2
            assert upstream_node in added_nodes
            assert downstream_node in added_nodes
            default_graph = dag_builder.graphs.get("default")
            assert default_graph is not None
            assert "upstream_node" in default_graph.nodes()
            assert "downstream_node" in default_graph.nodes()

            # Check that edge was added
            assert default_graph.in_degree("downstream_node") == 1
            assert default_graph.in_degree("upstream_node") == 0

    def test_add_node_with_dependencies_existing_upstream_node(self) -> None:
        """Test adding a node when upstream dependency already exists in DAG."""
        dag_builder = DagBuilder()

        # Create mock nodes
        upstream_node = MagicMock(spec=BaseNode)
        upstream_node.name = "upstream_node"
        upstream_node.parameters = []
        upstream_node.state = MagicMock()
        upstream_node.initialize_spotlight.return_value = MagicMock()  # Non-None return

        downstream_node = MagicMock(spec=BaseNode)
        downstream_node.name = "downstream_node"
        downstream_node.state = MagicMock()
        downstream_node.initialize_spotlight.return_value = MagicMock()  # Non-None return

        # Create mock parameter
        mock_param = MagicMock()
        mock_param.type = "str"  # Not CONTROL_TYPE
        downstream_node.parameters = [mock_param]

        # Add upstream node first
        dag_builder.add_node(upstream_node)

        # Mock the FlowManager connections
        with patch("griptape_nodes.retained_mode.griptape_nodes.GriptapeNodes.FlowManager") as mock_flow_manager:
            mock_connections = MagicMock()
            mock_connections.get_connected_node.return_value = (upstream_node, MagicMock())
            mock_flow_manager.return_value.get_connections.return_value = mock_connections

            added_nodes = dag_builder.add_node_with_dependencies(downstream_node)

            # Only downstream_node should be added (upstream already existed)
            assert len(added_nodes) == 1
            assert added_nodes[0] is downstream_node

            # Both nodes should be in the DAG with proper edge
            default_graph = dag_builder.graphs.get("default")
            assert default_graph is not None
            assert len(default_graph.nodes()) == 2
            assert default_graph.in_degree("downstream_node") == 1
            assert default_graph.in_degree("upstream_node") == 0

    def test_add_node_with_dependencies_prevents_cycles(self) -> None:
        """Test that add_node_with_dependencies handles potential cycles using visited set."""
        dag_builder = DagBuilder()

        # Create mock nodes that could create a cycle
        node_a = MagicMock(spec=BaseNode)
        node_a.name = "node_a"
        node_a.state = MagicMock()
        node_a.initialize_spotlight.return_value = MagicMock()  # Non-None return

        node_b = MagicMock(spec=BaseNode)
        node_b.name = "node_b"
        node_b.state = MagicMock()
        node_b.initialize_spotlight.return_value = MagicMock()  # Non-None return

        # Create mock parameters
        param_a = MagicMock()
        param_a.type = "str"  # Not CONTROL_TYPE
        param_b = MagicMock()
        param_b.type = "str"  # Not CONTROL_TYPE
        node_a.parameters = [param_a]
        node_b.parameters = [param_b]

        # Mock the FlowManager connections to simulate a cycle - but limit recursion depth
        with patch("griptape_nodes.retained_mode.griptape_nodes.GriptapeNodes.FlowManager") as mock_flow_manager:
            mock_connections = MagicMock()
            call_count = {"count": 0}  # Track calls to prevent infinite recursion in test

            def mock_get_connected_node(
                current_node: Any,
                param: Any,  # noqa: ARG001
                direction: Any = None,  # noqa: ARG001
                *,
                include_internal: bool = True,  # noqa: ARG001
            ) -> Any:
                call_count["count"] += 1
                # Safety limit to prevent infinite recursion in test environment
                if call_count["count"] > 10:
                    return None

                if current_node.name == "node_a":
                    return (node_b, MagicMock())
                if current_node.name == "node_b":
                    return (node_a, MagicMock())
                return None

            mock_connections.get_connected_node.side_effect = mock_get_connected_node
            mock_flow_manager.return_value.get_connections.return_value = mock_connections

            # This should not cause infinite recursion due to visited set
            added_nodes = dag_builder.add_node_with_dependencies(node_a)

            # Both nodes should be added - the visited set should prevent infinite recursion
            assert len(added_nodes) >= 1  # At least the starting node
            assert node_a in added_nodes
            # node_b may or may not be added depending on how the visited set handles the cycle

    def test_clear_removes_all_nodes_and_references(self) -> None:
        """Test that clear() removes all nodes and references from the DAG builder."""
        dag_builder = DagBuilder()

        # Add some nodes
        mock_node1 = MagicMock(spec=BaseNode)
        mock_node1.name = "node1"
        mock_node2 = MagicMock(spec=BaseNode)
        mock_node2.name = "node2"

        dag_builder.add_node(mock_node1)
        dag_builder.add_node(mock_node2)

        # Verify nodes were added
        default_graph = dag_builder.graphs.get("default")
        assert default_graph is not None
        assert len(default_graph.nodes()) == 2
        assert len(dag_builder.node_to_reference) == 2

        # Clear the DAG builder
        dag_builder.clear()

        # Verify everything is cleared
        assert len(dag_builder.graphs) == 0
        assert dag_builder.node_to_reference == {}

    def test_clear_on_empty_dag_builder(self) -> None:
        """Test that clear() works correctly on an empty DAG builder."""
        dag_builder = DagBuilder()

        # Clear should not raise any errors
        dag_builder.clear()

        assert len(dag_builder.graphs) == 0
        assert dag_builder.node_to_reference == {}

    def test_clear_removes_edges(self) -> None:
        """Test that clear() also removes all edges from the graph."""
        dag_builder = DagBuilder()

        # Create nodes with a dependency
        upstream_node = MagicMock(spec=BaseNode)
        upstream_node.name = "upstream_node"
        upstream_node.parameters = []
        upstream_node.state = MagicMock()
        upstream_node.initialize_spotlight.return_value = MagicMock()  # Non-None return

        downstream_node = MagicMock(spec=BaseNode)
        downstream_node.name = "downstream_node"
        downstream_node.state = MagicMock()
        downstream_node.initialize_spotlight.return_value = MagicMock()  # Non-None return

        # Create mock parameter
        mock_param = MagicMock()
        mock_param.type = "str"  # Not CONTROL_TYPE
        downstream_node.parameters = [mock_param]

        # Mock the FlowManager to create an edge
        with patch("griptape_nodes.retained_mode.griptape_nodes.GriptapeNodes.FlowManager") as mock_flow_manager:
            mock_connections = MagicMock()
            mock_connections.get_connected_node.side_effect = [
                (upstream_node, MagicMock()),  # downstream has upstream dependency
                None,  # upstream has no dependencies
            ]
            mock_flow_manager.return_value.get_connections.return_value = mock_connections

            dag_builder.add_node_with_dependencies(downstream_node)

            # Verify edge was created
            default_graph = dag_builder.graphs.get("default")
            assert default_graph is not None
            assert default_graph.in_degree("downstream_node") == 1

            # Clear the DAG
            dag_builder.clear()

            # Verify everything is cleared including edges
            assert len(dag_builder.graphs) == 0
            assert dag_builder.node_to_reference == {}

    def test_node_state_preservation(self) -> None:
        """Test that DagNode state is preserved correctly."""
        dag_builder = DagBuilder()
        mock_node = MagicMock(spec=BaseNode)
        mock_node.name = "test_node"

        dag_node = dag_builder.add_node(mock_node)

        # Initial state should be WAITING
        assert dag_node.node_state == NodeState.WAITING

        # Change state and verify it's preserved
        dag_node.node_state = NodeState.PROCESSING
        assert dag_node.node_state == NodeState.PROCESSING

        # Retrieving the same node should return the same object with preserved state
        retrieved_dag_node = dag_builder.node_to_reference["test_node"]
        assert retrieved_dag_node is dag_node
        assert retrieved_dag_node.node_state == NodeState.PROCESSING
