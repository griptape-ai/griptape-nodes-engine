"""Tests for the transitive containment a node group needs once groups can nest.

``node.parent_group`` names only the group a node sits in directly. Once a group can live inside
another group, "is this node in my group?" stops being a single lookup: the answer has to walk the
whole chain of enclosing groups. Code that checked only the direct parent treated a node nested one
level deeper as external, which is what made a group re-route connections that never left it.
"""

from __future__ import annotations

from typing import Any

import pytest

from griptape_nodes.exe_types.node_groups.base_node_group import BaseNodeGroup
from griptape_nodes.exe_types.node_types import BaseNode


class _MiniGroup(BaseNodeGroup):
    """Concrete group with no subflow, so membership can be tested on its own."""

    def process(self) -> Any:  # pragma: no cover - execution not exercised here
        return None


class _MiniNode(BaseNode):
    """Plain node used as group member."""

    def process(self) -> Any:  # pragma: no cover - execution not exercised here
        return None


class TestGetEnclosingGroups:
    def test_returns_empty_for_ungrouped_node(self) -> None:
        assert BaseNodeGroup.get_enclosing_groups(_MiniNode(name="loose")) == []

    def test_returns_every_ancestor_innermost_first(self) -> None:
        outer = _MiniGroup(name="outer")
        inner = _MiniGroup(name="inner")
        leaf = _MiniNode(name="leaf")
        inner.add_nodes_to_group([leaf])
        outer.add_nodes_to_group([inner])

        assert BaseNodeGroup.get_enclosing_groups(leaf) == [inner, outer]

    def test_stops_instead_of_looping_on_a_cycle(self) -> None:
        """A malformed parent_group cycle must terminate rather than hang the engine."""
        first = _MiniGroup(name="first")
        second = _MiniGroup(name="second")
        # Force the cycle the validation in add_nodes_to_group exists to prevent.
        first.parent_group = second
        second.parent_group = first

        # Terminates, and stops rather than walking back into a group it has already seen.
        assert BaseNodeGroup.get_enclosing_groups(first) == [second]


class TestContainsNode:
    def test_contains_direct_member(self) -> None:
        group = _MiniGroup(name="group")
        leaf = _MiniNode(name="leaf")
        group.add_nodes_to_group([leaf])

        assert group.contains_node(leaf)

    def test_contains_node_nested_two_levels_deep(self) -> None:
        """The outer group must own a node held by a group nested inside it."""
        outer = _MiniGroup(name="outer")
        inner = _MiniGroup(name="inner")
        leaf = _MiniNode(name="leaf")
        inner.add_nodes_to_group([leaf])
        outer.add_nodes_to_group([inner])

        assert outer.contains_node(leaf)
        assert outer.contains_node(inner)

    def test_does_not_contain_outside_node(self) -> None:
        group = _MiniGroup(name="group")

        assert not group.contains_node(_MiniNode(name="stranger"))

    def test_does_not_contain_a_sibling_groups_member(self) -> None:
        left = _MiniGroup(name="left")
        right = _MiniGroup(name="right")
        leaf = _MiniNode(name="leaf")
        right.add_nodes_to_group([leaf])

        assert not left.contains_node(leaf)


class TestNestingValidation:
    def test_rejects_adding_a_group_to_itself(self) -> None:
        group = _MiniGroup(name="group")

        with pytest.raises(ValueError, match="itself"):
            group.add_nodes_to_group([group])

    def test_rejects_adding_an_enclosing_group_as_a_child(self) -> None:
        """Nesting a group inside its own descendant would make the ancestor walk endless."""
        outer = _MiniGroup(name="outer")
        inner = _MiniGroup(name="inner")
        outer.add_nodes_to_group([inner])

        with pytest.raises(ValueError, match="already inside"):
            inner.add_nodes_to_group([outer])

    def test_rejects_the_whole_batch_before_mutating_anything(self) -> None:
        """A rejected add must not leave half the batch attached."""
        outer = _MiniGroup(name="outer")
        inner = _MiniGroup(name="inner")
        outer.add_nodes_to_group([inner])
        innocent = _MiniNode(name="innocent")

        with pytest.raises(ValueError, match="already inside"):
            inner.add_nodes_to_group([innocent, outer])

        assert innocent.name not in inner.nodes
        assert innocent.parent_group is None
