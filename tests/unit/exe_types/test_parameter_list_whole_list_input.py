"""A ParameterList fed by a connection to the list itself yields the incoming list, then its own rows.

Without a connection the list keeps no value of its own: it is built from its child rows, and a whole
list set on it becomes rows.
"""

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

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


@pytest.fixture
def connected() -> Iterator[None]:
    """Report every parameter on the node as having an incoming connection."""
    with patch.object(MockNode, "_param_has_incoming_connection", return_value=True):
        yield


@pytest.fixture
def disconnected() -> Iterator[None]:
    """Report every parameter on the node as having no incoming connection."""
    with patch.object(MockNode, "_param_has_incoming_connection", return_value=False):
        yield


class TestWholeListInput:
    @pytest.mark.usefixtures("disconnected")
    def test_without_connection_the_value_becomes_rows(self, node: MockNode) -> None:
        node.set_parameter_value("refs", ["a", "b", "c"])

        refs = node.get_parameter_by_name("refs")
        assert isinstance(refs, ParameterList)
        assert [node.get_parameter_value(row.name) for row in refs.get_child_parameters()] == ["a", "b", "c"]
        assert "refs" not in node.parameter_values

    @pytest.mark.usefixtures("connected")
    def test_connection_delivers_the_whole_list(self, node: MockNode) -> None:
        node.set_parameter_value("refs", ["a", "b", "c"])

        assert node.get_parameter_value("refs") == ["a", "b", "c"]
        assert node.get_parameter_list_value("refs") == ["a", "b", "c"]

    @pytest.mark.usefixtures("disconnected")
    def test_child_rows_still_build_the_list(self, node: MockNode) -> None:
        """The pre-existing per-row path is untouched."""
        refs = node.get_parameter_by_name("refs")
        assert isinstance(refs, ParameterList)
        first = refs.add_child_parameter()
        second = refs.add_child_parameter()
        node.set_parameter_value(first.name, "one")
        node.set_parameter_value(second.name, "two")

        assert node.get_parameter_value("refs") == ["one", "two"]

    @pytest.mark.usefixtures("connected")
    def test_connection_is_followed_by_child_rows(self, node: MockNode) -> None:
        refs = node.get_parameter_by_name("refs")
        assert isinstance(refs, ParameterList)
        child = refs.add_child_parameter()
        node.set_parameter_value(child.name, "manual")
        node.set_parameter_value("refs", ["a", "b"])

        assert node.get_parameter_value("refs") == ["a", "b", "manual"]

    def test_connection_then_mixed_rows_keep_their_order(self) -> None:
        node = MockNode()
        node.add_parameter(
            ParameterList(name="nums", tooltip="t", input_types=["int"], allowed_modes={ParameterMode.INPUT})
        )
        nums = node.get_parameter_by_name("nums")
        assert isinstance(nums, ParameterList)
        rows = [nums.add_child_parameter() for _ in range(4)]
        node.set_parameter_value(rows[0].name, 1)
        node.set_parameter_value(rows[1].name, [2, 3])
        node.set_parameter_value(rows[2].name, 4)
        node.set_parameter_value(rows[3].name, [5, 6])
        with patch.object(MockNode, "_param_has_incoming_connection", return_value=True):
            node.set_parameter_value("nums", [9, 8])

            assert node.get_parameter_value("nums") == [9, 8, 1, 2, 3, 4, 5, 6]

    @pytest.mark.usefixtures("connected")
    def test_dispatching_the_list_as_read_does_not_store_the_rows_again(self, node: MockNode) -> None:
        refs = node.get_parameter_by_name("refs")
        assert isinstance(refs, ParameterList)
        row = refs.add_child_parameter()
        node.set_parameter_value(row.name, "manual")
        node.set_parameter_value("refs", ["a", "b"])
        dispatched = node._list_rows.values_for_dispatch()["refs"]

        assert node._list_rows.set_as_rows_if_unconnected(refs, dispatched)

        assert node.parameter_values["refs"] == ["a", "b"]
        assert node.get_parameter_value("refs") == ["a", "b", "manual"]

    @pytest.mark.usefixtures("connected")
    @pytest.mark.parametrize("upstream", [[], None])
    def test_dispatching_an_empty_connected_list_does_not_store_the_rows(self, node: MockNode, upstream: Any) -> None:
        refs = node.get_parameter_by_name("refs")
        assert isinstance(refs, ParameterList)
        row = refs.add_child_parameter()
        node.set_parameter_value(row.name, "manual")
        node.parameter_values["refs"] = upstream
        dispatched = node._list_rows.values_for_dispatch()["refs"]

        assert node._list_rows.set_as_rows_if_unconnected(refs, dispatched)

        assert node.parameter_values["refs"] == upstream
        assert node.get_parameter_value("refs") == ["manual"]

    @pytest.mark.usefixtures("connected")
    def test_row_edits_on_a_connected_list_run_validators_and_hooks_with_the_combined_list(
        self, node: MockNode
    ) -> None:
        refs = node.get_parameter_by_name("refs")
        assert isinstance(refs, ParameterList)
        validated: list[Any] = []
        refs._validators.append(lambda _param, value: validated.append(value))
        node.set_parameter_value("refs", ["a", "b"])
        row = refs.add_child_parameter()

        node.set_parameter_value(row.name, "manual")

        assert validated[-1] == ["a", "b", "manual"]

        refs.remove_child(row)

        assert validated[-1] == ["a", "b"]

    def test_removed_row_value_does_not_resurrect(self, node: MockNode) -> None:
        """An unconnected list keeps no value of its own, so nothing is left behind to read."""
        refs = node.get_parameter_by_name("refs")
        assert isinstance(refs, ParameterList)
        with patch.object(MockNode, "_param_has_incoming_connection", return_value=False):
            child = refs.add_child_parameter()
            node.set_parameter_value(child.name, "manual")
            assert "refs" not in node.parameter_values

            refs.remove_child(child)

            assert node.get_parameter_value("refs") == []

    @pytest.mark.usefixtures("connected")
    def test_non_list_value_falls_through(self, node: MockNode) -> None:
        """A scalar cannot be a whole-list payload, so it must not take the whole-list path."""
        node.set_parameter_value("refs", "not-a-list")

        assert node.get_parameter_value("refs") == []


class TestWholeListWarnings:
    @pytest.mark.usefixtures("connected")
    def test_warns_when_incoming_list_exceeds_max_items(self) -> None:
        node = MockNode()
        node.add_parameter(
            ParameterList(
                name="capped",
                tooltip="t",
                input_types=["str"],
                allowed_modes={ParameterMode.INPUT},
                max_items=2,
            )
        )
        node.set_parameter_value("capped", ["a", "b", "c"])

        with patch("griptape_nodes.exe_types.node_types.logger") as mock_logger:
            value = node.get_parameter_value("capped")

        # The node decides how to truncate; the engine only surfaces the mismatch.
        assert value == ["a", "b", "c"]
        assert mock_logger.warning.called
