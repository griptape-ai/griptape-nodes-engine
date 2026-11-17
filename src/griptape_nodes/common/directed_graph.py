from __future__ import annotations

import logging

logger = logging.getLogger("griptape_nodes")


class DirectedGraph:
    """Directed graph implementation using Python's graphlib for DAG operations."""

    def __init__(self) -> None:
        self._nodes: set[str] = set()
        self._predecessors: dict[str, set[str]] = {}

    def __len__(self) -> int:
        """Return the number of nodes in the graph."""
        return len(self._nodes)

    def add_node(self, node_for_adding: str) -> None:
        """Add a node to the graph."""
        self._nodes.add(node_for_adding)
        if node_for_adding not in self._predecessors:
            self._predecessors[node_for_adding] = set()

    def add_edge(self, from_node: str, to_node: str) -> None:
        """Add a directed edge from from_node to to_node."""
        self.add_node(from_node)
        self.add_node(to_node)
        self._predecessors[to_node].add(from_node)

    def nodes(self) -> set[str]:
        """Return all nodes in the graph."""
        return self._nodes.copy()

    def in_degree(self, node: str) -> int:
        """Return the in-degree of a node (number of incoming edges)."""
        if node not in self._nodes:
            msg = f"Node {node} not found in graph"
            raise KeyError(msg)
        return len(self._predecessors.get(node, set()))

    def out_degree(self, node: str) -> int:
        """Return the out-degree of a node (number of outgoing edges)."""
        if node not in self._nodes:
            msg = f"Node {node} not found in graph"
            raise KeyError(msg)
        count = 0
        for predecessors in self._predecessors.values():
            if node in predecessors:
                count += 1
        return count

    def get_successors(self, node: str) -> set[str]:
        """Return the names of nodes with an edge coming directly from `node`."""
        return {other for other, predecessors in self._predecessors.items() if node in predecessors}

    def remove_node(self, node: str) -> None:
        """Remove a node and all its edges from the graph."""
        if node not in self._nodes:
            return

        self._nodes.remove(node)

        # Remove this node from all predecessor lists
        for predecessors in self._predecessors.values():
            predecessors.discard(node)

        # Remove this node's predecessor entry
        if node in self._predecessors:
            del self._predecessors[node]

    def clear(self) -> None:
        """Clear all nodes and edges from the graph."""
        self._nodes.clear()
        self._predecessors.clear()
