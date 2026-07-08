from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, NamedTuple

from griptape_nodes.common.directed_graph import DirectedGraph
from griptape_nodes.exe_types.base_iterative_nodes import BaseIterativeEndNode, BaseIterativeStartNode
from griptape_nodes.exe_types.connections import Direction
from griptape_nodes.exe_types.core_types import ParameterTypeBuiltin
from griptape_nodes.exe_types.node_types import NodeResolutionState

if TYPE_CHECKING:
    import asyncio

    from griptape_nodes.exe_types.connections import Connections
    from griptape_nodes.exe_types.node_types import BaseNode

logger = logging.getLogger("griptape_nodes")


class DagNodeCategories(NamedTuple):
    """Nodes bucketed by their role when seeding a DAG.

    Produced by classifying a scope of nodes (a whole flow or a single subflow) and consumed
    by the DAG-seeding routine. Keeping this scope-agnostic lets top-level flows and isolated
    subflows share one classification + seeding implementation.

    Attributes:
        start_nodes: StartNode instances (workflow entry points).
        control_nodes: Control-flow entry nodes that have no external incoming control connection.
        data_sink_nodes: Data nodes with no external outgoing connection (terminal/leaf nodes);
            seeding these resolves their dependencies backward.
    """

    start_nodes: list[BaseNode]
    control_nodes: list[BaseNode]
    data_sink_nodes: list[BaseNode]


class NodeState(StrEnum):
    """Individual node execution states."""

    QUEUED = "queued"
    PROCESSING = "processing"
    DONE = "done"
    CANCELED = "canceled"
    ERRORED = "errored"
    WAITING = "waiting"


@dataclass(kw_only=True)
class DagNode:
    """Represents a node in the DAG with runtime references."""

    task_reference: asyncio.Task | None = field(default=None)
    node_state: NodeState = field(default=NodeState.WAITING)
    node_reference: BaseNode


class DagBuilder:
    """Handles DAG construction independently of execution state machine."""

    graphs: dict[str, DirectedGraph]  # Str is the name of the start node associated here.
    node_to_reference: dict[str, DagNode]
    graph_to_nodes: dict[str, set[str]]  # Track which nodes belong to which graph
    start_node_candidates: dict[str, dict[str, set[str]]]  # {data_node: {graph: {boundary_nodes}}}

    def __init__(self) -> None:
        self.graphs = {}
        self.node_to_reference: dict[str, DagNode] = {}
        self.graph_to_nodes = {}
        self.start_node_candidates = {}

    def _get_nodes_to_exclude_for_iterative_end(self, node: BaseNode, connections: Connections) -> set[str]:
        """Get nodes to exclude when collecting dependencies for BaseIterativeEndNode.

        Args:
            node: The node being processed
            connections: The connections manager

        Returns:
            Set of node names to exclude (empty if not BaseIterativeEndNode)

        Raises:
            ValueError: If node is BaseIterativeEndNode but start_node is None
        """
        if not isinstance(node, BaseIterativeEndNode):
            return set()

        if node.start_node is None:
            error_msg = f"Error: {node.name} is not properly connected to a start node"
            logger.error(error_msg)
            raise ValueError(error_msg)

        return self.collect_loop_body_nodes(node.start_node, node, connections)

    # Complex with the inner recursive method, but it needs connections and added_nodes.
    def add_node_with_dependencies(self, node: BaseNode, graph_name: str = "default") -> list[BaseNode]:  # noqa: C901
        """Add node and all its dependencies to DAG. Returns list of added nodes."""
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        connections = GriptapeNodes.FlowManager().get_connections()
        added_nodes = []
        graph = self.graphs.get(graph_name, None)
        if graph is None:
            graph = DirectedGraph()
            self.graphs[graph_name] = graph
            self.graph_to_nodes[graph_name] = set()

        # Get nodes to exclude for BaseIterativeEndNode (loop body nodes)
        nodes_to_exclude = self._get_nodes_to_exclude_for_iterative_end(node, connections)

        def _add_node_recursive(current_node: BaseNode, visited: set[str], graph: DirectedGraph) -> None:
            # Skip if already visited or already in DAG
            if current_node.name in visited:
                return
            visited.add(current_node.name)

            if current_node.name in self.node_to_reference:
                return
            # Add current node to tracking
            dag_node = DagNode(node_reference=current_node, node_state=NodeState.WAITING)
            self.node_to_reference[current_node.name] = dag_node
            added_nodes.append(current_node)

            # Add to graph
            graph.add_node(node_for_adding=current_node.name)
            self.graph_to_nodes[graph_name].add(current_node.name)

            # Check if we should ignore dependencies (for special nodes like output_selector)
            ignore_data_dependencies = hasattr(current_node, "ignore_dependencies")

            # Process all upstream dependencies first (depth-first traversal)
            for param in current_node.parameters:
                # Skip control flow parameters
                if param.type == ParameterTypeBuiltin.CONTROL_TYPE:
                    continue

                # Skip if node ignores dependencies
                if ignore_data_dependencies:
                    continue

                upstream_connection = connections.get_connected_node(current_node, param, include_internal=False)
                if not upstream_connection:
                    continue

                upstream_node, _ = upstream_connection

                # Skip nodes in exclusion set (for BaseIterativeEndNode loop body)
                if upstream_node.name in nodes_to_exclude:
                    continue

                # Skip already resolved nodes
                if upstream_node.state == NodeResolutionState.RESOLVED:
                    continue

                # Recursively add upstream node
                _add_node_recursive(upstream_node, visited, graph)

                # Add edge from upstream to current
                graph.add_edge(upstream_node.name, current_node.name)

        _add_node_recursive(node, set(), graph)

        # Special handling for BaseIterativeEndNode: add start_node as dependency
        if isinstance(node, BaseIterativeEndNode) and node.start_node is not None:
            # Add start_node and its dependencies
            _add_node_recursive(node.start_node, set(), graph)
            # Add edge from start_node to end_node
            if node.start_node.name in graph.nodes() and node.name in graph.nodes():
                graph.add_edge(node.start_node.name, node.name)

        return added_nodes

    def add_node(self, node: BaseNode, graph_name: str = "default") -> DagNode:
        """Add just one node to DAG without dependencies (assumes dependencies already exist)."""
        if node.name in self.node_to_reference:
            return self.node_to_reference[node.name]

        dag_node = DagNode(node_reference=node, node_state=NodeState.WAITING)
        self.node_to_reference[node.name] = dag_node
        graph = self.graphs.get(graph_name, None)
        if graph is None:
            graph = DirectedGraph()
            self.graphs[graph_name] = graph
        graph.add_node(node_for_adding=node.name)

        # Track which nodes belong to this graph
        if graph_name not in self.graph_to_nodes:
            self.graph_to_nodes[graph_name] = set()
        self.graph_to_nodes[graph_name].add(node.name)

        return dag_node

    def clear(self) -> None:
        """Clear all nodes and references from the DAG builder."""
        self.graphs.clear()
        self.node_to_reference.clear()
        self.graph_to_nodes.clear()
        self.start_node_candidates.clear()

    def _has_active_control_path_to_node(self, node: DagNode, connections: Connections) -> bool:
        """Check if any active graph has a control path that can reach the target node.

        Args:
            node: The target node to check
            connections: The connections manager

        Returns:
            True if any graph could still reach the target node via control flow
        """
        for graph in self.graphs.values():
            # If the length of the graph is 0, skip it. it's either reached it or it's a dead end.
            if len(graph.nodes()) == 0:
                continue

            # If graph has nodes, the root node (not the leaf, the root), check forward path from that
            root_nodes = [n for n in graph.nodes() if graph.out_degree(n) == 0]
            for root_node_name in root_nodes:
                if root_node_name in self.node_to_reference:
                    root_node = self.node_to_reference[root_node_name].node_reference

                    # Skip if the root node is the same as the target node - it can't reach itself
                    if root_node == node.node_reference:
                        continue

                    # Skip if the root node is the end node of a start node. It's technically not downstream.
                    if (
                        isinstance(node.node_reference, BaseIterativeStartNode)
                        and root_node == node.node_reference.end_node
                    ):
                        continue

                    # Check if the target node is in the forward path from this root
                    if connections.is_node_in_forward_control_path(root_node, node.node_reference):
                        return True  # This graph could still reach the target node

        return False

    def can_queue_control_node(self, node: DagNode) -> bool:
        # Check if node has any DAG predecessors (dependencies) still in the graph
        node_name = node.node_reference.name
        for graph in self.graphs.values():
            if node_name in graph.nodes():
                # If any predecessors exist, they haven't completed yet (completed nodes are removed)
                predecessors = graph._predecessors.get(node_name, set())
                if predecessors:
                    return False

        if len(self.graphs) == 1:
            # If there's only one graph, we aren't looking for a control flow connection from elsewhere! We can queue. We've already looked at data dependencies.
            return True

        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        connections = GriptapeNodes.FlowManager().get_connections()

        control_connections = self.get_number_incoming_control_connections(node.node_reference, connections)
        # If no control connections, we can queue this! Don't worry about this.
        if control_connections == 0:
            return True

        # Check if any active graph has a control path to this node
        return not self._has_active_control_path_to_node(node, connections)

    def get_number_incoming_control_connections(self, node: BaseNode, connections: Connections) -> int:
        if node.name not in connections.incoming_index:
            return 0

        control_connection_count = 0
        node_connections = connections.incoming_index[node.name]

        for param_name, connection_ids in node_connections.items():
            # Find the parameter to check if it's a control type
            param = node.get_parameter_by_name(param_name)
            if param and ParameterTypeBuiltin.CONTROL_TYPE.value in param.input_types:
                # Skip connections from end node or itself if this is a BaseIterativeStartNode
                if isinstance(node, BaseIterativeStartNode):
                    for connection_id in connection_ids:
                        if connection_id in connections.connections:
                            connection = connections.connections[connection_id]
                            source_node = connection.source_node
                            # Skip if connection is from end node or itself
                            if source_node in (node.end_node, node):
                                continue
                            control_connection_count += 1
                else:
                    control_connection_count += len(connection_ids)

        return control_connection_count

    @staticmethod
    def collect_nodes_in_forward_control_path(
        start_node: BaseNode, end_node: BaseNode, connections: Connections
    ) -> set[str]:
        """Collect all nodes in the forward control path from start_node to end_node.

        Args:
            start_node: The node to start traversal from
            end_node: The node to stop traversal at (inclusive)
            connections: The connections manager

        Returns:
            Set of node names in the control path from start to end (inclusive)
        """
        nodes_in_path: set[str] = set()
        to_visit = [start_node]
        visited = set()

        while to_visit:
            current_node = to_visit.pop(0)

            if current_node.name in visited:
                continue
            visited.add(current_node.name)

            # Add to our collection
            nodes_in_path.add(current_node.name)

            # Stop if we've reached the end node
            if current_node == end_node:
                continue

            # Find all outgoing control connections
            if current_node.name in connections.outgoing_index:
                for param_name, connection_ids in connections.outgoing_index[current_node.name].items():
                    param = current_node.get_parameter_by_name(param_name)
                    if param and param.output_type == ParameterTypeBuiltin.CONTROL_TYPE.value:
                        for connection_id in connection_ids:
                            if connection_id in connections.connections:
                                connection = connections.connections[connection_id]
                                next_node = connection.target_node
                                if next_node.name not in visited and not connection.is_node_group_internal:
                                    to_visit.append(next_node)

        return nodes_in_path

    @staticmethod
    def collect_loop_body_nodes(
        start_node: BaseIterativeStartNode, end_node: BaseIterativeEndNode, connections: Connections
    ) -> set[str]:
        """Collect all nodes in the loop body between start_node and end_node.

        This walks through all outgoing connections from start_node and stops when
        reaching end_node, collecting all intermediate nodes.

        Args:
            start_node: The iterative start node
            end_node: The iterative end node
            connections: The connections manager

        Returns:
            Set of node names in the loop body (between start and end, exclusive)
        """
        loop_body_nodes: set[str] = set()
        to_visit = []
        to_visit.append(start_node)
        visited = set()
        while to_visit:
            current_node = to_visit.pop(0)

            if current_node.name in visited:
                continue
            visited.add(current_node.name)
            # Don't add start or end nodes themselves
            if current_node not in (start_node, end_node):
                loop_body_nodes.add(current_node.name)
            # Stop traversal if we've reached the end node
            if current_node == end_node:
                continue
            for param in current_node.parameters:
                downstream_connection = connections.get_connected_node(
                    current_node, param, direction=Direction.DOWNSTREAM, include_internal=False
                )
                if downstream_connection:
                    next_node = downstream_connection.node
                    if next_node.name not in visited:
                        to_visit.append(next_node)
        return loop_body_nodes

    @staticmethod
    def collect_data_dependencies_for_node(
        node: BaseNode, connections: Connections, nodes_to_exclude: set[str], visited: set[str]
    ) -> set[str]:
        """Collect data dependencies for a node recursively.

        Args:
            node: The node to collect dependencies for
            connections: The connections manager
            nodes_to_exclude: Set of nodes to exclude (e.g., nodes already in control flow)
            visited: Set of already visited dependency nodes (modified in place)

        Returns:
            Set of dependency node names
        """
        if node.name in visited:
            return set()

        visited.add(node.name)
        dependencies: set[str] = set()

        # Check for ignore_dependencies attribute (like output_selector)
        ignore_data_dependencies = hasattr(node, "ignore_dependencies")
        if ignore_data_dependencies:
            return dependencies

        # Process each parameter looking for data dependencies
        for param in node.parameters:
            # Skip control parameters
            if param.type == ParameterTypeBuiltin.CONTROL_TYPE:
                continue

            # Get upstream data connection
            upstream_connection = connections.get_connected_node(node, param, include_internal=False)
            if upstream_connection:
                upstream_node, _ = upstream_connection

                # Skip if already resolved
                if upstream_node.state == NodeResolutionState.RESOLVED:
                    continue

                # Only add if it's not in the exclusion set
                if upstream_node.name not in nodes_to_exclude:
                    dependencies.add(upstream_node.name)
                    # Recursively collect dependencies of this dependency
                    sub_deps = DagBuilder.collect_data_dependencies_for_node(
                        upstream_node, connections, nodes_to_exclude, visited
                    )
                    dependencies.update(sub_deps)

        return dependencies

    def cleanup_empty_graph_nodes(self, graph_name: str) -> None:
        """Remove nodes from node_to_reference when their graph becomes empty (only in single node resolution)."""
        if graph_name in self.graph_to_nodes:
            for node_name in self.graph_to_nodes[graph_name]:
                self.node_to_reference.pop(node_name, None)
            self.graph_to_nodes.pop(graph_name, None)

    def remove_node_from_dependencies(self, completed_node: str, graph_name: str) -> list[str]:
        """Remove completed node from all dependencies, return nodes ready to execute.

        Args:
            completed_node: Name of the node that just completed
            graph_name: Name of the graph the node completed in

        Returns:
            List of data node names that are now ready to execute
        """
        newly_available = []

        for data_node, graph_deps in copy.deepcopy(list(self.start_node_candidates.items())):
            if graph_name in graph_deps:
                # Remove the completed node from this graph's boundary nodes
                graph_deps[graph_name].discard(completed_node)

                # If all boundary nodes from this graph have completed, remove the graph dependency
                if len(graph_deps[graph_name]) == 0:
                    del graph_deps[graph_name]

                # If all graph dependencies are satisfied, the data node is ready
                if len(graph_deps) == 0:
                    del self.start_node_candidates[data_node]
                    newly_available.append(data_node)

        return newly_available
