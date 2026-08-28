"""Compute execution clusters: which nodes must run together, and where.

Nodes connected by an unserializable value MUST run in the same process, because that value
cannot cross a process boundary (a live tensor, an open pipeline). That is a correctness
constraint the user cannot see, so clusters are computed from the graph rather than authored:

1. Union two nodes across any edge whose value cannot serialize.
2. Union adjacent nodes that require the same non-empty execution-dependency set, so a chain
   from one heavy library does not pay a boundary crossing between every pair of nodes.
3. A cluster whose execution-dependency union is empty runs in the orchestrator. A cluster
   with execution dependencies is a dispatch unit for a venue that has them installed.

The inputs are deliberately primitive (names, dependency sets, edge flags) so this module
stays pure: the DAG builder describes its graph in these terms, and dispatch consumes the
resulting clusters, but neither is imported here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ClusterNode:
    """A node as the cluster computation sees it.

    exec_dependencies is the execution-dependency set of the node's library
    (``pip_dependencies_exec``), empty for a library that declares none.
    """

    name: str
    exec_dependencies: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ClusterEdge:
    """A dataflow edge between two nodes.

    serializable is False when the value on this edge cannot cross a process boundary
    (``Parameter.serializable`` is static metadata, so this is known without running).
    """

    source: str
    target: str
    serializable: bool = True


@dataclass(frozen=True)
class ExecutionCluster:
    """A set of nodes that execute together, and the execution deps their venue needs."""

    node_names: frozenset[str]
    exec_dependencies: frozenset[str]

    @property
    def runs_in_orchestrator(self) -> bool:
        """True when no member needs execution dependencies, so no venue is required."""
        return not self.exec_dependencies


class _UnionFind:
    """Union-find over node names, path-compressed."""

    def __init__(self, names: list[str]) -> None:
        self._parent: dict[str, str] = {name: name for name in names}

    def find(self, name: str) -> str:
        root = name
        while self._parent[root] != root:
            root = self._parent[root]
        # Path compression: point every node on the walk directly at the root.
        while self._parent[name] != root:
            self._parent[name], name = root, self._parent[name]
        return root

    def union(self, a: str, b: str) -> None:
        self._parent[self.find(a)] = self.find(b)


def compute_execution_clusters(
    nodes: list[ClusterNode],
    edges: list[ClusterEdge],
) -> list[ExecutionCluster]:
    """Partition a dataflow graph into execution clusters.

    Args:
        nodes: Every node in the control-flow step's DAG.
        edges: Every dataflow edge between those nodes.

    Returns:
        Clusters covering every input node, each with the union of its members'
        execution dependencies. Order follows first appearance in ``nodes``.

    Raises:
        ValueError: If an edge references a node that is not in ``nodes``; the DAG
            builder handing us an edge for a missing node is a caller bug, not a
            graph shape.
    """
    known_names = {node.name for node in nodes}
    for edge in edges:
        if edge.source not in known_names or edge.target not in known_names:
            msg = (
                f"Attempted to compute execution clusters, but edge "
                f"{edge.source!r} -> {edge.target!r} references a node not in the graph."
            )
            raise ValueError(msg)

    deps_by_name = {node.name: node.exec_dependencies for node in nodes}
    union_find = _UnionFind([node.name for node in nodes])

    for edge in edges:
        if not edge.serializable:
            # An unserializable value pins producer and consumer into one process.
            union_find.union(edge.source, edge.target)
            continue
        source_deps = deps_by_name[edge.source]
        if source_deps and source_deps == deps_by_name[edge.target]:
            # Same heavy library on both ends: crossing a boundary here would serialize a
            # value just to land in an identical environment. Merge to avoid the round trip.
            union_find.union(edge.source, edge.target)

    members_by_root: dict[str, list[str]] = {}
    for node in nodes:
        members_by_root.setdefault(union_find.find(node.name), []).append(node.name)

    clusters = []
    for members in members_by_root.values():
        exec_dependencies: frozenset[str] = frozenset().union(*(deps_by_name[name] for name in members))
        clusters.append(ExecutionCluster(node_names=frozenset(members), exec_dependencies=exec_dependencies))
    return clusters
