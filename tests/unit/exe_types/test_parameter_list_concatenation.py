import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterList, ParameterMode

from .mocks import MockNode


@pytest.fixture
def node() -> MockNode:
    """A node with an input-only list `refs` of `ImageUrlArtifact`."""
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

    def test_a_row_restored_with_narrow_input_types_is_widened(self, node: MockNode) -> None:
        refs = _refs(node)
        row = Parameter(name="saved_row", tooltip="t", input_types=["ImageUrlArtifact"], parent_container_name="refs")

        refs.add_child(row)

        assert row.input_types == ["ImageUrlArtifact", "list[ImageUrlArtifact]", "list"]

    def test_existing_rows_follow_a_change_of_input_types(self, node: MockNode) -> None:
        refs = _refs(node)
        row = refs.add_child_parameter()

        refs.input_types = ["VideoUrlArtifact"]

        assert row.input_types == ["VideoUrlArtifact", "list[VideoUrlArtifact]", "list"]

    def test_existing_rows_follow_a_change_of_type(self) -> None:
        node = MockNode()
        node.add_parameter(ParameterList(name="refs", tooltip="t", type="str", allowed_modes={ParameterMode.INPUT}))
        refs = node.get_parameter_by_name("refs")
        assert isinstance(refs, ParameterList)
        row = refs.add_child_parameter()

        refs.type = "int"

        assert row.input_types == ["int", "list[int]", "list"]

    def test_existing_rows_follow_a_change_of_output_type(self) -> None:
        node = MockNode()
        node.add_parameter(
            ParameterList(name="refs", tooltip="t", output_type="str", allowed_modes={ParameterMode.INPUT})
        )
        refs = node.get_parameter_by_name("refs")
        assert isinstance(refs, ParameterList)
        row = refs.add_child_parameter()

        refs.output_type = "int"

        assert row.input_types == ["int", "list[int]", "list"]

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

    def test_an_empty_item_in_a_row_is_dropped(self, node: MockNode) -> None:
        row = _refs(node).add_child_parameter()

        node.set_parameter_value(row.name, ["a", None, "b"])

        assert node.get_parameter_value("refs") == ["a", "b"]

    @pytest.mark.parametrize("item_type", ["list", "list[str]", "any", "all"])
    def test_a_list_whose_items_may_be_lists_keeps_its_rows_whole(self, item_type: str) -> None:
        node = MockNode()
        node.add_parameter(
            ParameterList(name="groups", tooltip="t", input_types=[item_type], allowed_modes={ParameterMode.INPUT})
        )
        groups = node.get_parameter_by_name("groups")
        assert isinstance(groups, ParameterList)
        row = groups.add_child_parameter()

        node.set_parameter_value(row.name, ["b", "c"])

        assert node.get_parameter_value("groups") == [["b", "c"]]
