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
from typing import Any


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
    return _closure_over_paths(clusters, nodes, edges)


def exec_dependencies_for_library(library_name: str) -> frozenset[str]:
    """The execution-dependency set a library declares, empty when it declares none.

    This is the placement signal: a node whose library has execution dependencies cannot
    run its ``process`` where those dependencies are not installed.
    """
    # Deferred import: this module is otherwise pure, and library_registry imports widely.
    from griptape_nodes.node_library.library_registry import LibraryRegistry

    library = LibraryRegistry.get_library(library_name)
    dependencies = library.get_library_data().metadata.dependencies
    if dependencies is None or not dependencies.pip_dependencies_exec:
        return frozenset()
    return frozenset(dependencies.pip_dependencies_exec)


@dataclass(frozen=True)
class NodeGraphEdge:
    """A dataflow edge between two live nodes, as the graph layer describes it."""

    source_node_name: str
    source_parameter: str
    target_node_name: str
    target_parameter: str


def clusters_for_nodes(
    nodes: list[Any],
    edges: list[NodeGraphEdge],
) -> list[ExecutionCluster]:
    """Compute execution clusters from live BaseNode instances and their dataflow edges.

    The adapter between real graphs and the pure computation: each node's
    execution-dependency set comes from its library (``metadata["library"]``), and an
    edge is unserializable when the source node's output parameter is marked
    ``serializable=False`` -- the producer's declaration governs, since it is the
    producer's value that would have to cross a boundary.

    Args:
        nodes: Live BaseNode instances (typed as Any to keep this module import-light;
            they need ``.name``, ``.metadata``, and ``.get_parameter_by_name``).
        edges: Dataflow edges between those nodes.
    """
    cluster_nodes = []
    for node in nodes:
        library_name = node.metadata.get("library", "")
        exec_dependencies = exec_dependencies_for_library(library_name) if library_name else frozenset()
        cluster_nodes.append(ClusterNode(name=node.name, exec_dependencies=exec_dependencies))

    nodes_by_name = {node.name: node for node in nodes}
    cluster_edges = []
    for edge in edges:
        source = nodes_by_name.get(edge.source_node_name)
        if source is None:
            msg = (
                f"Attempted to compute execution clusters, but edge source "
                f"{edge.source_node_name!r} is not among the given nodes."
            )
            raise ValueError(msg)
        parameter = source.get_parameter_by_name(edge.source_parameter)
        serializable = parameter.serializable if parameter is not None else True
        cluster_edges.append(
            ClusterEdge(
                source=edge.source_node_name,
                target=edge.target_node_name,
                serializable=serializable,
            )
        )

    return compute_execution_clusters(cluster_nodes, cluster_edges)


def _closure_over_paths(
    clusters: list[ExecutionCluster],
    nodes: list[ClusterNode],
    edges: list[ClusterEdge],
) -> list[ExecutionCluster]:
    """Absorb any node lying on a path between two members of the same cluster.

    A cluster executes atomically, so it must be convex in the DAG: if member U feeds
    external node X and X feeds member D, then X's value is needed mid-cluster and X's
    input only exists once the cluster runs. Leaving X outside would deadlock the
    dispatch, so X is absorbed and the cluster's dependency union grows accordingly.
    """
    deps_by_name = {node.name: node.exec_dependencies for node in nodes}
    forward: dict[str, list[str]] = {node.name: [] for node in nodes}
    backward: dict[str, list[str]] = {node.name: [] for node in nodes}
    for edge in edges:
        forward[edge.source].append(edge.target)
        backward[edge.target].append(edge.source)

    def descendants(seeds: frozenset[str]) -> set[str]:
        return _reachable(seeds, forward)

    def ancestors(seeds: frozenset[str]) -> set[str]:
        return _reachable(seeds, backward)

    current = clusters
    changed = True
    while changed:
        changed = False
        absorbed_by_cluster: dict[int, set[str]] = {}
        for index, cluster in enumerate(current):
            if cluster.runs_in_orchestrator or len(cluster.node_names) == 1:
                continue
            # Nodes both downstream of some member and upstream of another lie on a
            # member -> member path.
            on_paths = (descendants(cluster.node_names) & ancestors(cluster.node_names)) - cluster.node_names
            if on_paths:
                absorbed_by_cluster[index] = on_paths
        if not absorbed_by_cluster:
            return current

        changed = True
        grown = [cluster.node_names | absorbed_by_cluster.get(index, set()) for index, cluster in enumerate(current)]
        current = [
            ExecutionCluster(
                node_names=members,
                exec_dependencies=frozenset().union(*(deps_by_name[name] for name in members)),
            )
            for members in _merge_overlapping(grown)
        ]
    return current


def _merge_overlapping(member_sets: list[frozenset[str]]) -> list[frozenset[str]]:
    """Merge any member sets that share a node, keeping every node in exactly one set.

    Absorption can pull the same node into two clusters (it sits between members of both)
    or steal a node that was another cluster's member. Either way the affected clusters
    cannot execute separately.
    """
    merged: list[frozenset[str]] = []
    for members in member_sets:
        combined = members
        for existing in [candidate for candidate in merged if candidate & combined]:
            merged.remove(existing)
            combined = combined | existing
        merged.append(combined)
    return merged


def _reachable(seeds: frozenset[str], adjacency: dict[str, list[str]]) -> set[str]:
    """Every node reachable from ``seeds`` (inclusive) over ``adjacency``."""
    seen = set(seeds)
    stack = list(seeds)
    while stack:
        for neighbor in adjacency[stack.pop()]:
            if neighbor not in seen:
                seen.add(neighbor)
                stack.append(neighbor)
    return seen
