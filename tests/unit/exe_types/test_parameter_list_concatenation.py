"""A ParameterList row takes one item or a whole list of its item type, and the rows concatenate."""

import pytest

from griptape_nodes.exe_types.core_types import ParameterList, ParameterMode

from .mocks import MockNode


@pytest.fixture
def node() -> MockNode:
    """A node with a single INPUT-only ParameterList named `refs`."""
    node = MockNode()
    node.add_parameter(
        ParameterList(
            name="refs",
            tooltip="t",
            input_types=["ImageUrlArtifact"],
            allowed_modes={ParameterMode.INPUT},
        )
    )
    return node


def _refs(node: MockNode) -> ParameterList:
    refs = node.get_parameter_by_name("refs")
    assert isinstance(refs, ParameterList)
    return refs


class TestRowInputTypes:
    def test_new_row_accepts_the_item_and_a_list_of_it(self, node: MockNode) -> None:
        row = _refs(node).add_child_parameter()

        assert row.input_types == ["ImageUrlArtifact", "list[ImageUrlArtifact]", "list"]

    def test_existing_rows_follow_a_change_of_input_types(self, node: MockNode) -> None:
        refs = _refs(node)
        row = refs.add_child_parameter()

        refs.input_types = ["VideoUrlArtifact"]

        assert row.input_types == ["VideoUrlArtifact", "list[VideoUrlArtifact]", "list"]

    def test_clearing_input_types_leaves_old_and_new_rows_alike(self, node: MockNode) -> None:
        refs = _refs(node)
        old_row = refs.add_child_parameter()

        refs.input_types = None
        new_row = refs.add_child_parameter()

        assert old_row.input_types == new_row.input_types


class TestConcatenation:
    def test_a_list_in_a_row_adds_its_items(self, node: MockNode) -> None:
        refs = _refs(node)
        first, second, third = (refs.add_child_parameter() for _ in range(3))

        node.set_parameter_value(first.name, "a")
        node.set_parameter_value(second.name, ["b", "c"])
        node.set_parameter_value(third.name, ["d"])

        assert node.get_parameter_list_value("refs") == ["a", "b", "c", "d"]
        assert node.get_parameter_value("refs") == ["a", "b", "c", "d"]

    def test_a_list_of_lists_keeps_its_rows_whole(self) -> None:
        node = MockNode()
        node.add_parameter(
            ParameterList(name="groups", tooltip="t", input_types=["list"], allowed_modes={ParameterMode.INPUT})
        )
        groups = node.get_parameter_by_name("groups")
        assert isinstance(groups, ParameterList)
        row = groups.add_child_parameter()

        node.set_parameter_value(row.name, ["b", "c"])

        assert node.get_parameter_value("groups") == [["b", "c"]]
