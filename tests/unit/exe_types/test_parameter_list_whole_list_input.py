"""A ParameterList fed by a connection to the list itself yields the whole incoming list.

Without a connection the list is still built from its child rows, which is what keeps a stale
`parameter_values` entry (not cleared when a row is removed) from becoming visible.
"""

from collections.abc import Iterator
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
    def test_without_connection_the_child_rows_win(self, node: MockNode) -> None:
        """A value set with no connection is ignored, because it may be a stale row cache."""
        node.set_parameter_value("refs", ["a", "b", "c"])

        assert node.get_parameter_value("refs") == []

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
    def test_connection_wins_over_child_rows(self, node: MockNode) -> None:
        """Ordering between a whole list and manual rows is unrepresentable, so the list wins."""
        refs = node.get_parameter_by_name("refs")
        assert isinstance(refs, ParameterList)
        child = refs.add_child_parameter()
        node.set_parameter_value(child.name, "manual")
        node.set_parameter_value("refs", ["a", "b"])

        assert node.get_parameter_value("refs") == ["a", "b"]

    def test_removed_row_value_does_not_resurrect(self, node: MockNode) -> None:
        """The regression the connection gate exists to prevent.

        `parameter_values['refs']` is a write-through cache of the rows and is NOT cleared when a
        row is removed, so gating on "no children" would expose the deleted row's value.
        """
        refs = node.get_parameter_by_name("refs")
        assert isinstance(refs, ParameterList)
        with patch.object(MockNode, "_param_has_incoming_connection", return_value=False):
            child = refs.add_child_parameter()
            node.set_parameter_value(child.name, "manual")
            assert node.parameter_values["refs"] == ["manual"]

            refs.remove_child(child)

            assert node.get_parameter_value("refs") == []

    @pytest.mark.usefixtures("connected")
    def test_non_list_value_falls_through(self, node: MockNode) -> None:
        """A scalar cannot be a whole-list payload, so it must not take the whole-list path."""
        node.set_parameter_value("refs", "not-a-list")

        assert node.get_parameter_value("refs") == []


class TestWholeListWarnings:
    @pytest.mark.usefixtures("connected")
    def test_warns_when_manual_rows_are_ignored(self, node: MockNode) -> None:
        refs = node.get_parameter_by_name("refs")
        assert isinstance(refs, ParameterList)
        child = refs.add_child_parameter()
        node.set_parameter_value(child.name, "manual")
        node.set_parameter_value("refs", ["a"])

        with patch("griptape_nodes.exe_types.node_types.logger") as mock_logger:
            node.get_parameter_value("refs")

        assert mock_logger.warning.called

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
