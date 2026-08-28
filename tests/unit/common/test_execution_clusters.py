"""Unit tests for execution-cluster computation.

The shapes here mirror the shipped diffusers templates: latent chains that must co-locate,
light passthrough nodes absorbed into heavy clusters, and multi-stage graphs whose stages
communicate through serializable values.
"""

from __future__ import annotations

import pytest

from griptape_nodes.common.execution_clusters import (
    ClusterEdge,
    ClusterNode,
    ExecutionCluster,
    compute_execution_clusters,
)

TORCH = frozenset({"torch==2.7.0", "diffusers==0.39.0"})
OTHER = frozenset({"torch==2.4.0"})


def _cluster_of(clusters: list[ExecutionCluster], name: str) -> ExecutionCluster:
    matches = [c for c in clusters if name in c.node_names]
    assert len(matches) == 1
    return matches[0]


class TestPlacement:
    def test_light_nodes_run_in_orchestrator(self) -> None:
        clusters = compute_execution_clusters(
            [ClusterNode("a"), ClusterNode("b")],
            [ClusterEdge("a", "b", serializable=True)],
        )
        assert all(c.runs_in_orchestrator for c in clusters)
        assert {c.node_names for c in clusters} == {frozenset({"a"}), frozenset({"b"})}  # nothing forces them together

    def test_lone_heavy_node_is_a_single_node_cluster(self) -> None:
        clusters = compute_execution_clusters([ClusterNode("gen", TORCH)], [])
        assert clusters == [ExecutionCluster(frozenset({"gen"}), TORCH)]
        assert not clusters[0].runs_in_orchestrator


class TestUnserializableEdges:
    def test_latent_chain_merges(self) -> None:
        """Loader -> generate -> decode over live values is one cluster."""
        nodes = [ClusterNode("loader", TORCH), ClusterNode("generate", TORCH), ClusterNode("decode", TORCH)]
        edges = [
            ClusterEdge("loader", "generate", serializable=False),
            ClusterEdge("generate", "decode", serializable=False),
        ]
        clusters = compute_execution_clusters(nodes, edges)
        assert len(clusters) == 1
        assert clusters[0].node_names == {"loader", "generate", "decode"}

    def test_light_passthrough_is_absorbed_into_the_heavy_cluster(self) -> None:
        """A base-only node in a latent chain runs in the venue with everyone else."""
        nodes = [ClusterNode("gen", TORCH), ClusterNode("note", frozenset()), ClusterNode("decode", TORCH)]
        edges = [
            ClusterEdge("gen", "note", serializable=False),
            ClusterEdge("note", "decode", serializable=False),
        ]
        clusters = compute_execution_clusters(nodes, edges)
        assert len(clusters) == 1
        assert clusters[0].exec_dependencies == TORCH

    def test_two_libraries_joined_by_live_value_union_their_deps(self) -> None:
        """The merged cluster carries both dependency sets.

        Whether they co-resolve is the environment builder's question, not ours.
        """
        clusters = compute_execution_clusters(
            [ClusterNode("a", TORCH), ClusterNode("b", OTHER)],
            [ClusterEdge("a", "b", serializable=False)],
        )
        assert len(clusters) == 1
        assert clusters[0].exec_dependencies == TORCH | OTHER


class TestSerializableBoundaries:
    def test_serializable_edge_between_different_dep_sets_does_not_merge(self) -> None:
        clusters = compute_execution_clusters(
            [ClusterNode("a", TORCH), ClusterNode("b", OTHER)],
            [ClusterEdge("a", "b", serializable=True)],
        )
        assert {c.node_names for c in clusters} == {frozenset({"a"}), frozenset({"b"})}

    def test_same_heavy_deps_across_serializable_edge_merge_to_avoid_a_round_trip(self) -> None:
        clusters = compute_execution_clusters(
            [ClusterNode("stage1", TORCH), ClusterNode("stage2", TORCH)],
            [ClusterEdge("stage1", "stage2", serializable=True)],
        )
        assert len(clusters) == 1

    def test_light_nodes_across_serializable_edges_never_merge(self) -> None:
        """The same-deps optimization only applies to non-empty dependency sets.

        Light nodes stay individually placeable in the orchestrator.
        """
        clusters = compute_execution_clusters(
            [ClusterNode("a"), ClusterNode("b")],
            [ClusterEdge("a", "b", serializable=True)],
        )
        assert {c.node_names for c in clusters} == {frozenset({"a"}), frozenset({"b"})}


class TestGraphShapes:
    def test_multi_stage_template_shape(self) -> None:
        """Two latent stages joined by a serializable image dispatch as one cluster.

        Same library on both stages, so the same-deps rule merges them -- matching how
        MultistageText2Image should dispatch.
        """
        nodes = [
            ClusterNode("s1_gen", TORCH),
            ClusterNode("s1_decode", TORCH),
            ClusterNode("s2_encode", TORCH),
            ClusterNode("s2_gen", TORCH),
        ]
        edges = [
            ClusterEdge("s1_gen", "s1_decode", serializable=False),
            ClusterEdge("s1_decode", "s2_encode", serializable=True),
            ClusterEdge("s2_encode", "s2_gen", serializable=False),
        ]
        clusters = compute_execution_clusters(nodes, edges)
        assert len(clusters) == 1

    def test_diamond_with_mixed_edges(self) -> None:
        nodes = [
            ClusterNode("src"),
            ClusterNode("left", TORCH),
            ClusterNode("right", OTHER),
            ClusterNode("sink"),
        ]
        edges = [
            ClusterEdge("src", "left", serializable=True),
            ClusterEdge("src", "right", serializable=True),
            ClusterEdge("left", "sink", serializable=True),
            ClusterEdge("right", "sink", serializable=True),
        ]
        clusters = compute_execution_clusters(nodes, edges)
        assert {c.node_names for c in clusters} == {
            frozenset({"src"}),
            frozenset({"left"}),
            frozenset({"right"}),
            frozenset({"sink"}),
        }
        assert _cluster_of(clusters, "left").exec_dependencies == TORCH
        assert _cluster_of(clusters, "right").exec_dependencies == OTHER
        assert _cluster_of(clusters, "src").runs_in_orchestrator

    def test_every_node_lands_in_exactly_one_cluster(self) -> None:
        nodes = [ClusterNode(f"n{i}", TORCH if i % 2 else frozenset()) for i in range(10)]
        edges = [ClusterEdge(f"n{i}", f"n{i + 1}", serializable=(i % 3 != 0)) for i in range(9)]
        clusters = compute_execution_clusters(nodes, edges)
        seen: set[str] = set()
        for cluster in clusters:
            assert not (cluster.node_names & seen)
            seen |= cluster.node_names
        assert seen == {node.name for node in nodes}


class TestValidation:
    def test_edge_referencing_unknown_node_raises(self) -> None:
        with pytest.raises(ValueError, match="references a node not in the graph"):
            compute_execution_clusters([ClusterNode("a")], [ClusterEdge("a", "ghost")])


class TestConvexity:
    """A dispatched cluster must be convex in the DAG.

    Nodes on member->member paths are absorbed, or the dispatch would need a value that
    only exists mid-cluster.
    """

    def test_external_node_on_a_member_to_member_path_is_absorbed(self) -> None:
        """U -> X -> D with U,D pinned by a live edge: X joins the cluster."""
        nodes = [ClusterNode("u", TORCH), ClusterNode("x"), ClusterNode("d", TORCH)]
        edges = [
            ClusterEdge("u", "d", serializable=False),
            ClusterEdge("u", "x", serializable=True),
            ClusterEdge("x", "d", serializable=True),
        ]
        clusters = compute_execution_clusters(nodes, edges)
        assert {c.node_names for c in clusters} == {frozenset({"u", "x", "d"})}

    def test_pure_upstream_and_downstream_nodes_are_not_absorbed(self) -> None:
        """Nodes before or after the cluster stay outside; only in-between nodes join."""
        nodes = [
            ClusterNode("before"),
            ClusterNode("u", TORCH),
            ClusterNode("d", TORCH),
            ClusterNode("after"),
        ]
        edges = [
            ClusterEdge("before", "u", serializable=True),
            ClusterEdge("u", "d", serializable=False),
            ClusterEdge("d", "after", serializable=True),
        ]
        clusters = compute_execution_clusters(nodes, edges)
        assert {c.node_names for c in clusters} == {
            frozenset({"before"}),
            frozenset({"u", "d"}),
            frozenset({"after"}),
        }

    def test_absorbed_heavy_node_unions_its_dependencies(self) -> None:
        """An in-between node from another heavy library grows the cluster's dep union."""
        nodes = [ClusterNode("u", TORCH), ClusterNode("x", OTHER), ClusterNode("d", TORCH)]
        edges = [
            ClusterEdge("u", "d", serializable=False),
            ClusterEdge("u", "x", serializable=True),
            ClusterEdge("x", "d", serializable=True),
        ]
        clusters = compute_execution_clusters(nodes, edges)
        assert len(clusters) == 1
        assert clusters[0].exec_dependencies == TORCH | OTHER

    def test_chained_absorption_reaches_a_fixed_point(self) -> None:
        """U -> x1 -> x2 -> D: the whole in-between chain is absorbed."""
        nodes = [ClusterNode("u", TORCH), ClusterNode("x1"), ClusterNode("x2"), ClusterNode("d", TORCH)]
        edges = [
            ClusterEdge("u", "d", serializable=False),
            ClusterEdge("u", "x1", serializable=True),
            ClusterEdge("x1", "x2", serializable=True),
            ClusterEdge("x2", "d", serializable=True),
        ]
        clusters = compute_execution_clusters(nodes, edges)
        assert {c.node_names for c in clusters} == {frozenset({"u", "x1", "x2", "d"})}

    def test_light_clusters_are_never_closed(self) -> None:
        """Convexity only matters for dispatched clusters; orchestrator nodes run per-node."""
        nodes = [ClusterNode("a"), ClusterNode("x"), ClusterNode("b")]
        edges = [
            ClusterEdge("a", "b", serializable=False),
            ClusterEdge("a", "x", serializable=True),
            ClusterEdge("x", "b", serializable=True),
        ]
        clusters = compute_execution_clusters(nodes, edges)
        # a-b cluster is light (runs in orchestrator), so x stays outside it.
        assert frozenset({"x"}) in {c.node_names for c in clusters}

    def test_two_clusters_sharing_an_intermediate_node_merge(self) -> None:
        """One node between members of TWO clusters merges everything into one.

        The node cannot execute in two places, and every node must land in exactly one
        cluster.
        """
        nodes = [
            ClusterNode("u1", TORCH),
            ClusterNode("d1", TORCH),
            ClusterNode("u2", OTHER),
            ClusterNode("d2", OTHER),
            ClusterNode("x"),
        ]
        edges = [
            ClusterEdge("u1", "d1", serializable=False),
            ClusterEdge("u2", "d2", serializable=False),
            ClusterEdge("u1", "x", serializable=True),
            ClusterEdge("x", "d1", serializable=True),
            ClusterEdge("u2", "x", serializable=True),
            ClusterEdge("x", "d2", serializable=True),
        ]
        clusters = compute_execution_clusters(nodes, edges)
        seen: set[str] = set()
        for cluster in clusters:
            assert not (cluster.node_names & seen), "a node appeared in two clusters"
            seen |= cluster.node_names
        assert {c.node_names for c in clusters} == {frozenset({"u1", "d1", "u2", "d2", "x"})}
