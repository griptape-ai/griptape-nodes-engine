from unittest.mock import patch

import pytest

from griptape_nodes.exe_types.core_types import ParameterList, ParameterMode

from .mocks import MockNode


@pytest.fixture
def node() -> MockNode:
    """A node with an input list `items`."""
    node = MockNode()
    node.add_parameter(ParameterList(name="items", tooltip="t", type="str", allowed_modes={ParameterMode.INPUT}))
    return node


def _items(node: MockNode) -> ParameterList:
    items = node.get_parameter_by_name("items")
    assert isinstance(items, ParameterList)
    return items


class TestAddChildParameterName:
    def test_named_row_has_exactly_that_name_in_its_only_lifecycle_event(self, node: MockNode) -> None:
        with patch.object(MockNode, "_emit_parameter_lifecycle_event") as emit:
            row = _items(node).add_child_parameter(name="items_case_1")

        assert row.name == "items_case_1"
        assert emit.call_count == 1
        assert emit.call_args.args[0].name == "items_case_1"

    def test_unnamed_row_gets_generated_name(self, node: MockNode) -> None:
        row = _items(node).add_child_parameter()

        assert row.name.startswith("items_ParameterListUniqueParamID_")

    def test_duplicate_name_raises(self, node: MockNode) -> None:
        items = _items(node)
        items.add_child_parameter(name="items_case_1")

        with pytest.raises(ValueError, match="items_case_1"):
            items.add_child_parameter(name="items_case_1")

        assert len(items.get_child_parameters()) == 1

    def test_name_of_another_node_parameter_raises(self, node: MockNode) -> None:
        node.add_parameter(ParameterList(name="other", tooltip="t", type="str"))

        with pytest.raises(ValueError, match="other"):
            _items(node).add_child_parameter(name="other")

    def test_named_row_keeps_its_name_after_move_child(self, node: MockNode) -> None:
        items = _items(node)
        first = items.add_child_parameter(name="items_a")
        items.add_child_parameter(name="items_b")

        items.move_child(1, 0)

        assert [p.name for p in items.get_child_parameters()] == ["items_b", "items_a"]
        assert first.name == "items_a"

    def test_auto_increment_numbers_rows_from_1(self, node: MockNode) -> None:
        items = _items(node)

        names = [items.add_child_parameter(name="items_a", auto_increment=True).name for _ in range(3)]

        assert names == ["items_a_1", "items_a_2", "items_a_3"]

    def test_auto_increment_with_a_numbered_name_does_not_nest(self, node: MockNode) -> None:
        items = _items(node)

        names = [
            items.add_child_parameter(name=n, auto_increment=True).name for n in ["items_a_1", "items_a", "items_a_1"]
        ]

        assert names == ["items_a_1", "items_a_2", "items_a_3"]
