from unittest.mock import patch

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterList, ParameterMode
from griptape_nodes.exe_types.list_rows import ListRows
from griptape_nodes.exe_types.node_loading import node_loading

from .mocks import MockNode


def _list(name: str, **kwargs: object) -> ParameterList:
    return ParameterList(name=name, tooltip="t", type="str", allowed_modes={ParameterMode.INPUT}, **kwargs)  # type: ignore[arg-type]


@pytest.fixture
def node() -> MockNode:
    """A node where `values` and `notes` are driven by `cases`, and `extras` by `values`."""
    node = MockNode()
    node.add_parameter(_list("cases"))
    node.add_parameter(_list("values", driven_by="cases"))
    node.add_parameter(_list("notes", driven_by="cases"))
    node.add_parameter(_list("extras", driven_by="values"))
    return node


def _rows(node: MockNode, name: str) -> list[str]:
    lst = node.get_parameter_by_name(name)
    assert isinstance(lst, ParameterList)
    return [row.name for row in lst.get_child_parameters()]


def _cases(node: MockNode) -> ParameterList:
    cases = node.get_parameter_by_name("cases")
    assert isinstance(cases, ParameterList)
    return cases


def _driven_names(list_name: str, cases: list[str]) -> list[str]:
    return [f"{list_name}_for_{case}" for case in cases]


class TestDrivenRows:
    def test_driver_row_without_a_value_gets_a_row_in_each_driven_list(self, node: MockNode) -> None:
        row = _cases(node).add_child_parameter()

        assert _rows(node, "values") == [f"values_for_{row.name}"]
        assert _rows(node, "notes") == [f"notes_for_{row.name}"]

    def test_rows_follow_driver_through_add_remove_and_reorder(self, node: MockNode) -> None:
        cases = _cases(node)
        a, b, c = (cases.add_child_parameter() for _ in range(3))

        cases.remove_child(b)
        cases.move_child(1, 0)

        order = [c.name, a.name]
        assert [row.name for row in cases.get_child_parameters()] == order
        assert _rows(node, "values") == _driven_names("values", order)
        assert _rows(node, "notes") == _driven_names("notes", order)

    def test_chain_keeps_every_list_in_step_and_removal_reaches_the_end(self, node: MockNode) -> None:
        cases = _cases(node)
        a = cases.add_child_parameter()
        b = cases.add_child_parameter()
        assert _rows(node, "extras") == [f"extras_for_values_for_{row.name}" for row in (a, b)]

        cases.remove_child(a)

        assert _rows(node, "values") == [f"values_for_{b.name}"]
        assert _rows(node, "extras") == [f"extras_for_values_for_{b.name}"]

    def test_driven_row_value_survives_driver_reorder(self, node: MockNode) -> None:
        cases = _cases(node)
        a = cases.add_child_parameter()
        cases.add_child_parameter()
        row_name = f"values_for_{a.name}"
        node.set_parameter_value(row_name, "kept")

        cases.move_child(0, 1)

        assert node.get_parameter_value(row_name) == "kept"

    def test_undeclared_driver_is_rejected(self) -> None:
        node = MockNode()

        with pytest.raises(ValueError, match="not declared on the node yet"):
            node.add_parameter(_list("values", driven_by="cases"))


class TestDrivenLabels:
    def test_rows_use_their_own_numbering_by_default(self, node: MockNode) -> None:
        row = _cases(node).add_child_parameter()
        node.set_parameter_value(row.name, "Lighting")

        values = node.get_parameter_by_name("values")
        assert isinstance(values, ParameterList)
        assert values.get_child_parameters()[0].display_name is None

    def test_label_from_driver_value_follows_the_value_and_falls_back_when_empty(self) -> None:
        node = MockNode()
        node.add_parameter(_list("cases"))
        node.add_parameter(_list("values", driven_by="cases", label_from_driver_value=True))
        cases = _cases(node)
        row = cases.add_child_parameter()
        values = node.get_parameter_by_name("values")
        assert isinstance(values, ParameterList)
        driven = values.get_child_parameters()[0]
        assert driven.display_name is None

        node.set_parameter_value(row.name, "Lighting")
        assert driven.display_name == "Lighting"

        node.set_parameter_value(row.name, "Camera")
        assert driven.display_name == "Camera"

        node.set_parameter_value(row.name, "")
        assert driven.display_name is None


class TestDrivenReorderEvent:
    def test_reordering_the_driver_tells_the_editor_the_driven_lists_new_order(self, node: MockNode) -> None:
        cases = _cases(node)
        a = cases.add_child_parameter()
        b = cases.add_child_parameter()
        values = node.get_parameter_by_name("values")
        assert isinstance(values, ParameterList)

        with patch.object(ParameterList, "_emit_alter_element_event_if_possible") as emit:
            cases.move_child(1, 0)

        emit.assert_called()
        assert [child["name"] for child in values._changes["children"]] == [
            f"values_for_{b.name}",
            f"values_for_{a.name}",
        ]


class TestDrivenListsWhileLoading:
    def test_saved_rows_restored_while_loading_do_not_get_extra_driven_rows(self, node: MockNode) -> None:
        cases = _cases(node)
        values = node.get_parameter_by_name("values")
        assert isinstance(values, ParameterList)

        with node_loading(node.name):
            cases.add_child(Parameter(name="cases_saved", tooltip="t", type="str", parent_container_name="cases"))
            values.add_child(
                Parameter(name="values_for_cases_saved", tooltip="t", type="str", parent_container_name="values")
            )

        assert _rows(node, "values") == ["values_for_cases_saved"]
        assert _rows(node, "notes") == []


class TestReplacingTheDriver:
    def test_setting_the_driver_as_a_whole_list_syncs_once_with_the_result(self, node: MockNode) -> None:
        node.set_parameter_value("cases", ["a", "b"])

        names = _rows(node, "cases")
        assert len(names) == 2  # noqa: PLR2004
        assert _rows(node, "values") == _driven_names("values", names)
        assert _rows(node, "notes") == _driven_names("notes", names)

    def test_a_wire_into_a_driven_row_blocks_replacing_the_driver(self, node: MockNode) -> None:
        node.set_parameter_value("cases", ["a"])
        before = _rows(node, "values")

        with (
            patch.object(ListRows, "_connected_parameter_names", side_effect=lambda names: set(names) & set(before)),
            pytest.raises(ValueError, match="connected"),
        ):
            node.set_parameter_value("cases", ["a", "b"])

        assert _rows(node, "values") == before

    def test_replacing_the_driver_keeps_the_driven_values_of_rows_that_stay(self, node: MockNode) -> None:
        node.set_parameter_value("cases", ["a", "b"])
        first, second = _rows(node, "values")
        node.set_parameter_value(first, "one")
        node.set_parameter_value(second, "two")

        node.set_parameter_value("cases", ["a", "b", "c"])

        values = _rows(node, "values")
        assert values[:2] == [first, second]
        assert [node.get_parameter_value(name) for name in values[:2]] == ["one", "two"]
        assert len(values) == 3  # noqa: PLR2004

    def test_replacing_the_driver_with_fewer_rows_drops_the_extra_driven_rows(self, node: MockNode) -> None:
        node.set_parameter_value("cases", ["a", "b", "c"])
        first = _rows(node, "values")[0]

        node.set_parameter_value("cases", ["a"])

        assert _rows(node, "values") == [first]
