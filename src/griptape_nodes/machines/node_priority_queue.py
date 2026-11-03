"""Priority queue for managing node execution order."""

from __future__ import annotations

from typing import TYPE_CHECKING

from griptape_nodes.machines.heuristics import DistanceToNode, HasConnectionFromPrevious, TopLeftToBottomRight

if TYPE_CHECKING:
    from griptape_nodes.machines.dag_builder import DagNode
    from griptape_nodes.machines.heuristics import NodeHeuristic
    from griptape_nodes.machines.parallel_resolution import ParallelResolutionContext


class NodePriorityQueue:
    """Manages priority-based ordering of nodes ready for execution.

    This is a simple framework that stores nodes and returns them in priority order.
    The _calculate_priority() method is the extension point for future heuristics.
    """

    def __init__(self, context: ParallelResolutionContext) -> None:
        """Initialize the priority queue.

        Args:
            context: The execution context containing node references and DAG state
        """
        self._context = context
        self._queued_nodes: list[str] = []  # Node names, sorted by priority (highest first)
        self._blocked_nodes: list[str] = []  # Node names blocked from queuing (not ready yet)
        self._needs_reorder = False  # Lazy reorder flag
        self._last_resolved_successors: set[str] = set()  # Nodes connected to last resolved node

        # Initialize heuristics with fixed weights
        self._heuristics: list[NodeHeuristic] = [
            HasConnectionFromPrevious(context, weight=1.0),
            DistanceToNode(context, weight=0.75),
            TopLeftToBottomRight(context, weight=1.0),
        ]

    def add_node(self, dag_node: DagNode) -> str:
        """Add a node to the priority queue or blocked list based on readiness.

        Checks if the node is ready for execution using can_queue_for_execution().
        If ready, adds to the queue. If not ready, adds to blocked list.

        Args:
            dag_node: The DagNode to add

        Returns:
            The name of the node that was added
        """
        node_name = dag_node.node_reference.name

        if node_name in self._queued_nodes or node_name in self._blocked_nodes:
            return node_name

        if dag_node.node_reference.can_queue_for_execution():
            self._queued_nodes.append(node_name)
            self._needs_reorder = True
        else:
            self._blocked_nodes.append(node_name)

        return node_name

    def get_next_node(self) -> str | None:
        """Get and remove the highest priority node from the queue.

        Returns:
            The name of the highest priority node, or None if queue is empty
        """
        self._drop_nodes_missing_from_dag()

        if self._needs_reorder:
            self._reorder()
            self._needs_reorder = False

        if self._queued_nodes:
            return self._queued_nodes.pop(0)
        return None

    def remove_node(self, node_name: str) -> None:
        """Remove a node from the priority queue or blocked list.

        Args:
            node_name: Name of the node to remove
        """
        if node_name in self._queued_nodes:
            self._queued_nodes.remove(node_name)
        if node_name in self._blocked_nodes:
            self._blocked_nodes.remove(node_name)

    def mark_priorities_stale(self) -> None:
        """Mark priorities as needing recalculation.

        Called when context.last_resolved_node changes and existing
        queued nodes need reprioritization based on the new context.
        """
        if self._queued_nodes:
            self._needs_reorder = True

    def check_blocked_nodes(self) -> int:
        """Check blocked nodes and promote any that are now ready to the queue.

        Returns:
            The number of nodes that were promoted from blocked to queued
        """
        if not self._blocked_nodes:
            return 0

        promoted_count = 0
        nodes_still_blocked = []

        for node_name in self._blocked_nodes:
            if node_name in self._context.node_to_reference:
                dag_node = self._context.node_to_reference[node_name]
                if dag_node.node_reference.can_queue_for_execution():
                    self._queued_nodes.append(node_name)
                    self._needs_reorder = True
                    promoted_count += 1
                else:
                    nodes_still_blocked.append(node_name)
            else:
                nodes_still_blocked.append(node_name)

        self._blocked_nodes = nodes_still_blocked
        return promoted_count

    def _drop_nodes_missing_from_dag(self) -> None:
        """Forget queued and blocked nodes the DAG no longer has.

        A node can leave the DAG while it waits here: packaging a loop body pulls in data nodes
        from outside the loop and removes them from the DAG the loop runs in, without reaching
        this queue. Every lookup below assumes a queued name is still in the DAG.
        """
        if not self._queued_nodes and not self._blocked_nodes:
            return

        node_to_reference = self._context.node_to_reference
        self._queued_nodes = [name for name in self._queued_nodes if name in node_to_reference]
        self._blocked_nodes = [name for name in self._blocked_nodes if name in node_to_reference]

    def _reorder(self) -> None:
        """Reorder the queue based on current node priorities."""
        # Check if any blocked nodes have become unblocked
        if len(self._blocked_nodes) > 0:
            self.check_blocked_nodes()

        if not self._queued_nodes:
            return

        if len(self._queued_nodes) == 1:
            return

        # Look up DagNodes from context only during reorder
        dag_nodes = [self._context.node_to_reference[name] for name in self._queued_nodes]

        previous_executed_node = None
        if self._context.last_resolved_node is not None:
            node_name = self._context.last_resolved_node.name
            if node_name in self._context.node_to_reference:
                previous_executed_node = self._context.node_to_reference[node_name]

        combined_priorities: dict[str, float] = {node.node_reference.name: 0.0 for node in dag_nodes}

        for heuristic in self._heuristics:
            scores = heuristic.calculate_priorities_batch(
                dag_nodes,
                previous_executed_node=previous_executed_node,
                last_resolved_successors=self._last_resolved_successors,
            )

            for node_name, score in scores.items():
                combined_priorities[node_name] += score * heuristic.weight

        # Sort the string names directly
        self._queued_nodes.sort(key=lambda name: combined_priorities[name], reverse=True)
