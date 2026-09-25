"""Unit tests for BaseWhileNodeGroup."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.node_groups.base_while_node_group import BaseWhileNodeGroup
from griptape_nodes.exe_types.node_groups.subflow_node_group import (
    LEFT_PARAMETERS_KEY,
    RIGHT_PARAMETERS_KEY,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine


class MockWhileGroup(BaseWhileNodeGroup):
    """Minimal concrete subclass for testing BaseWhileNodeGroup."""


@pytest.fixture
def while_group(engine: Engine) -> MockWhileGroup:  # noqa: ARG001 - initialises the engine singleton for construction
    """Return a freshly constructed MockWhileGroup."""
    return MockWhileGroup(name="test_while_group")


class TestBaseWhileNodeGroupRails:
    """The rail metadata drives what the editor draws down each side of the group."""

    def test_fresh_group_names_each_rail_parameter_once(self, while_group: MockWhileGroup) -> None:
        """The rails are membership sets, so this pins which names are there and that none repeats."""
        expected_left = {"group_exec_in", "iteration"}
        expected_right = {"group_exec_out", "done", "continue_loop", "total_iterations"}
        assert set(while_group.metadata[LEFT_PARAMETERS_KEY]) == expected_left
        assert len(while_group.metadata[LEFT_PARAMETERS_KEY]) == len(expected_left)
        assert set(while_group.metadata[RIGHT_PARAMETERS_KEY]) == expected_right
        assert len(while_group.metadata[RIGHT_PARAMETERS_KEY]) == len(expected_right)


class TestBaseWhileNodeGroupRestoreRoundTrip:
    """Rebuilding a group from its saved metadata must reproduce its rails, not grow them.

    Load hands the saved metadata straight to the constructor, and the group holds on to that very
    list, so any port the constructor re-records is a port the artist sees twice.
    """

    def test_restored_group_has_the_same_rail_parameters(self, while_group: MockWhileGroup) -> None:
        """A group rebuilt from what it was saved with names the same ports, once each."""
        restored = MockWhileGroup(name="restored", metadata=copy.deepcopy(while_group.metadata))

        assert set(restored.metadata[LEFT_PARAMETERS_KEY]) == set(while_group.metadata[LEFT_PARAMETERS_KEY])
        assert len(restored.metadata[LEFT_PARAMETERS_KEY]) == len(while_group.metadata[LEFT_PARAMETERS_KEY])
        assert set(restored.metadata[RIGHT_PARAMETERS_KEY]) == set(while_group.metadata[RIGHT_PARAMETERS_KEY])
        assert len(restored.metadata[RIGHT_PARAMETERS_KEY]) == len(while_group.metadata[RIGHT_PARAMETERS_KEY])

    def test_a_third_generation_still_has_the_same_rail_parameters(self, while_group: MockWhileGroup) -> None:
        """Duplicates compound across save/load cycles, so a second rebuild has to stay stable too."""
        restored = MockWhileGroup(name="restored", metadata=copy.deepcopy(while_group.metadata))
        restored_again = MockWhileGroup(name="restored_again", metadata=copy.deepcopy(restored.metadata))

        assert set(restored_again.metadata[LEFT_PARAMETERS_KEY]) == set(while_group.metadata[LEFT_PARAMETERS_KEY])
        assert len(restored_again.metadata[LEFT_PARAMETERS_KEY]) == len(while_group.metadata[LEFT_PARAMETERS_KEY])
        assert set(restored_again.metadata[RIGHT_PARAMETERS_KEY]) == set(while_group.metadata[RIGHT_PARAMETERS_KEY])
        assert len(restored_again.metadata[RIGHT_PARAMETERS_KEY]) == len(while_group.metadata[RIGHT_PARAMETERS_KEY])
