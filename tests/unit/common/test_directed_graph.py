"""Tests for DirectedGraph class."""

# ruff: noqa: PLR2004

from griptape_nodes.common.directed_graph import DirectedGraph


class TestDirectedGraph:
    """Test cases for DirectedGraph functionality."""

    def test_init_creates_empty_graph(self) -> None:
        """Test that initialization creates an empty graph."""
        graph = DirectedGraph()
        assert graph.nodes() == set()

    def test_add_node_single(self) -> None:
        """Test adding a single node to the graph."""
        graph = DirectedGraph()
        graph.add_node("node1")

        assert "node1" in graph.nodes()
        assert len(graph.nodes()) == 1

    def test_add_node_multiple(self) -> None:
        """Test adding multiple nodes to the graph."""
        graph = DirectedGraph()
        graph.add_node("node1")
        graph.add_node("node2")
        graph.add_node("node3")

        nodes = graph.nodes()
        assert nodes == {"node1", "node2", "node3"}
        assert len(nodes) == 3

    def test_add_node_duplicate(self) -> None:
        """Test that adding the same node twice doesn't create duplicates."""
        graph = DirectedGraph()
        graph.add_node("node1")
        graph.add_node("node1")

        assert len(graph.nodes()) == 1
        assert "node1" in graph.nodes()

    def test_add_edge_creates_nodes(self) -> None:
        """Test that adding an edge automatically creates nodes if they don't exist."""
        graph = DirectedGraph()
        graph.add_edge("from_node", "to_node")

        nodes = graph.nodes()
        assert "from_node" in nodes
        assert "to_node" in nodes
        assert len(nodes) == 2

    def test_add_edge_existing_nodes(self) -> None:
        """Test adding edges between existing nodes."""
        graph = DirectedGraph()
        graph.add_node("node1")
        graph.add_node("node2")
        graph.add_edge("node1", "node2")

        assert len(graph.nodes()) == 2
        assert graph.in_degree("node2") == 1
        assert graph.in_degree("node1") == 0

    def test_in_degree_zero_for_new_nodes(self) -> None:
        """Test that newly added nodes have in-degree of 0."""
        graph = DirectedGraph()
        graph.add_node("node1")

        assert graph.in_degree("node1") == 0

    def test_in_degree_nonexistent_node(self) -> None:
        """Test that in_degree raises KeyError for nodes that don't exist."""
        graph = DirectedGraph()

        import pytest

        with pytest.raises(KeyError, match="Node nonexistent not found in graph"):
            graph.in_degree("nonexistent")

    def test_in_degree_with_edges(self) -> None:
        """Test in_degree calculation with various edge configurations."""
        graph = DirectedGraph()

        # Create a simple DAG: A -> B -> C, A -> C
        graph.add_edge("A", "B")
        graph.add_edge("B", "C")
        graph.add_edge("A", "C")

        assert graph.in_degree("A") == 0  # Root node
        assert graph.in_degree("B") == 1  # One incoming edge
        assert graph.in_degree("C") == 2  # Two incoming edges

    def test_in_degree_complex_graph(self) -> None:
        """Test in_degree calculation in a more complex graph."""
        graph = DirectedGraph()

        # Create a more complex DAG
        graph.add_edge("root", "node1")
        graph.add_edge("root", "node2")
        graph.add_edge("node1", "node3")
        graph.add_edge("node2", "node3")
        graph.add_edge("node3", "leaf")

        assert graph.in_degree("root") == 0
        assert graph.in_degree("node1") == 1
        assert graph.in_degree("node2") == 1
        assert graph.in_degree("node3") == 2
        assert graph.in_degree("leaf") == 1

    def test_remove_node_existing(self) -> None:
        """Test removing an existing node and all its edges."""
        graph = DirectedGraph()
        graph.add_edge("A", "B")
        graph.add_edge("B", "C")
        graph.add_edge("A", "C")

        # Remove node B
        graph.remove_node("B")

        nodes = graph.nodes()
        assert "B" not in nodes
        assert "A" in nodes
        assert "C" in nodes
        assert len(nodes) == 2

        # Check that edges involving B are removed
        assert graph.in_degree("C") == 1  # Only A -> C remains

    def test_remove_node_nonexistent(self) -> None:
        """Test that removing a nonexistent node doesn't affect the graph."""
        graph = DirectedGraph()
        graph.add_node("node1")

        graph.remove_node("nonexistent")

        assert len(graph.nodes()) == 1
        assert "node1" in graph.nodes()

    def test_remove_node_all_edge_types(self) -> None:
        """Test removing a node removes both incoming and outgoing edges."""
        graph = DirectedGraph()

        # Create graph where B has both incoming and outgoing edges
        graph.add_edge("A", "B")  # B has incoming edge
        graph.add_edge("B", "C")  # B has outgoing edge
        graph.add_edge("B", "D")  # B has another outgoing edge

        graph.remove_node("B")

        # B should be removed
        assert "B" not in graph.nodes()

        # Other nodes should remain but have correct in_degrees
        assert graph.in_degree("A") == 0
        assert graph.in_degree("C") == 0  # No longer receives from B
        assert graph.in_degree("D") == 0  # No longer receives from B

    def test_clear_empty_graph(self) -> None:
        """Test clearing an already empty graph."""
        graph = DirectedGraph()
        graph.clear()

        assert graph.nodes() == set()

    def test_clear_graph_with_nodes(self) -> None:
        """Test clearing a graph with nodes and edges."""
        graph = DirectedGraph()
        graph.add_edge("A", "B")
        graph.add_edge("B", "C")
        graph.add_node("isolated")

        graph.clear()

        assert graph.nodes() == set()

    def test_nodes_returns_copy(self) -> None:
        """Test that nodes() returns a copy, not the original set."""
        graph = DirectedGraph()
        graph.add_node("node1")

        nodes1 = graph.nodes()
        nodes2 = graph.nodes()

        # Should be equal but not the same object
        assert nodes1 == nodes2
        assert nodes1 is not nodes2

        # Modifying returned set shouldn't affect graph
        nodes1.add("external_node")
        assert "external_node" not in graph.nodes()

    def test_leaf_nodes_identification(self) -> None:
        """Test identifying leaf nodes (nodes with in_degree 0)."""
        graph = DirectedGraph()

        # Create a DAG with multiple roots and leaves
        graph.add_edge("root1", "middle")
        graph.add_edge("root2", "middle")
        graph.add_edge("middle", "leaf1")
        graph.add_edge("middle", "leaf2")
        graph.add_node("isolated")  # Isolated node (also a leaf)

        # Find leaf nodes (in_degree == 0)
        leaf_nodes = [n for n in graph.nodes() if graph.in_degree(n) == 0]

        assert set(leaf_nodes) == {"root1", "root2", "isolated"}

    def test_multiple_edges_same_direction(self) -> None:
        """Test that multiple edges in the same direction don't increase in_degree."""
        graph = DirectedGraph()

        # Adding the same edge multiple times shouldn't increase in_degree
        graph.add_edge("A", "B")
        graph.add_edge("A", "B")
        graph.add_edge("A", "B")

        assert graph.in_degree("B") == 1

    def test_empty_graph_operations(self) -> None:
        """Test various operations on an empty graph."""
        graph = DirectedGraph()

        assert graph.nodes() == set()

        # in_degree should raise KeyError for nonexistent nodes
        import pytest

        with pytest.raises(KeyError):
            graph.in_degree("any_node")

        # These operations should not raise errors
        graph.remove_node("nonexistent")
        graph.clear()

    def test_get_successors_returns_direct_targets_only(self) -> None:
        """get_successors names the nodes one edge downstream, not the whole reachable set."""
        graph = DirectedGraph()
        graph.add_edge("a", "b")
        graph.add_edge("a", "c")
        graph.add_edge("b", "d")

        assert graph.get_successors("a") == {"b", "c"}
        assert graph.get_successors("b") == {"d"}

    def test_get_successors_empty_for_leaf_or_unknown_node(self) -> None:
        """A node with no outgoing edges, or one not in the graph, has no successors."""
        graph = DirectedGraph()
        graph.add_edge("a", "b")

        assert graph.get_successors("b") == set()
        assert graph.get_successors("missing") == set()
