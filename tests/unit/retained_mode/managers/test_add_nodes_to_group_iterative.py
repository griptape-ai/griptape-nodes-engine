"""Regression tests for the interactive GUI path.

AddNodesToNodeGroupRequest must automatically pull the paired End node into the group
when a BaseIterativeStartNode is added (path #2 described in the bug report — the GUI
sends only the Start node name).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from griptape_nodes.exe_types.base_iterative_nodes import (
    BaseIterativeEndNode,
    BaseIterativeStartNode,
)
from griptape_nodes.exe_types.flow import ControlFlow
from griptape_nodes.exe_types.node_groups.base_node_group import BaseNodeGroup
from griptape_nodes.retained_mode.events.node_events import (
    AddNodesToNodeGroupRequest,
    AddNodesToNodeGroupResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from tests.unit.exe_types.mocks import MockNode

if TYPE_CHECKING:
    from griptape_nodes.exe_types.node_types import AsyncResult, BaseNode


# ---------------------------------------------------------------------------
# Minimal concrete mock classes
# ---------------------------------------------------------------------------


class _MockIterativeEndNode(BaseIterativeEndNode):
    """Minimal concrete end node for testing."""

    @classmethod
    def _get_compatible_start_classes(cls) -> set[type]:
        return {_MockIterativeStartNode}

    def run(self) -> None:
        pass

    def initialize(self) -> None:
        pass

    def process(self) -> AsyncResult | None:
        return None


class _MockIterativeStartNode(BaseIterativeStartNode):
    """Minimal concrete start node for testing."""

    @classmethod
    def _get_compatible_end_classes(cls) -> set[type]:
        return {_MockIterativeEndNode}

    def _get_parameter_group_name(self) -> str:
        return "Iteration Data"

    def _get_exec_out_display_name(self) -> str:
        return "On Each Item"

    def _get_exec_out_tooltip(self) -> str:
        return "Execute for each item"

    def _get_iteration_items(self) -> list[Any]:
        return []

    def _initialize_iteration_data(self) -> None:
        pass

    def _get_current_item_value(self) -> Any:
        return None

    def is_loop_finished(self) -> bool:
        return True

    def _get_total_iterations(self) -> int:
        return 0

    def _get_current_iteration_count(self) -> int:
        return 0

    def get_current_index(self) -> int:
        return 0

    def _advance_to_next_iteration(self) -> None:
        pass

    def run(self) -> None:
        pass

    def initialize(self) -> None:
        pass


class _ConcreteGroup(BaseNodeGroup):
    """Minimal concrete BaseNodeGroup for testing."""

    def run(self) -> None:
        pass

    def initialize(self) -> None:
        pass

    def process(self) -> None:
        return None


def _register(obj: BaseNode) -> None:
    """Register a node-like object in the ObjectManager so handle_request can find it."""
    GriptapeNodes.ObjectManager().add_object_by_name(obj.name, obj)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAddNodesToGroupIterativePath:
    """Verify that AddNodesToNodeGroupRequest auto-includes paired End nodes."""

    @pytest.fixture(autouse=True)
    def _setup_context(self, griptape_nodes: GriptapeNodes) -> None:  # noqa: ARG002
        """Push a workflow and flow context so group-operation helpers can find a current flow."""
        GriptapeNodes.ContextManager().push_workflow(workflow_name="test_workflow")
        flow = ControlFlow(name="test_flow")
        GriptapeNodes.ObjectManager().add_object_by_name(flow.name, flow)
        GriptapeNodes.ContextManager().push_flow(flow)

    def _build_paired_nodes(self) -> tuple[_MockIterativeStartNode, _MockIterativeEndNode]:
        """Build a Start/End pair with end_node wired up and both registered."""
        start = _MockIterativeStartNode("TestStartNode")
        end = _MockIterativeEndNode("TestEndNode")
        # Wire the pair the same way on_create_node_request does it after the
        # CreateConnectionRequest auto-connects them.
        start.end_node = end
        end.start_node = start
        _register(start)
        _register(end)
        return start, end

    def _build_group(self) -> _ConcreteGroup:
        """Build a group and register it in the ObjectManager."""
        group = _ConcreteGroup("TestGroup")
        _register(group)
        return group

    def test_end_node_is_pulled_into_group_automatically(self) -> None:
        """Interactive path: sending only the Start name must also group the End node.

        This is the core regression guard: before the fix, end_node.parent_group
        remained None and end_node.name was absent from group.nodes.
        """
        start, end = self._build_paired_nodes()
        group = self._build_group()

        result = GriptapeNodes.handle_request(
            AddNodesToNodeGroupRequest(
                node_names=[start.name],
                node_group_name=group.name,
            )
        )

        assert isinstance(result, AddNodesToNodeGroupResultSuccess), result
        assert start.parent_group is group
        assert end.parent_group is group
        assert start.name in group.nodes
        assert end.name in group.nodes
        assert start.name in group.metadata["node_names_in_group"]
        assert end.name in group.metadata["node_names_in_group"]

    def test_result_node_names_added_contains_both(self) -> None:
        """AddNodesToNodeGroupResultSuccess.node_names_added reports both nodes."""
        start, end = self._build_paired_nodes()
        group = self._build_group()

        result = GriptapeNodes.handle_request(
            AddNodesToNodeGroupRequest(
                node_names=[start.name],
                node_group_name=group.name,
            )
        )

        assert isinstance(result, AddNodesToNodeGroupResultSuccess)
        assert start.name in result.node_names_added
        assert end.name in result.node_names_added
        assert result.node_group_name == group.name

    def test_end_node_not_duplicated_if_already_in_request_list(self) -> None:
        """If the caller explicitly includes both Start and End, no duplication occurs."""
        start, end = self._build_paired_nodes()
        group = self._build_group()

        result = GriptapeNodes.handle_request(
            AddNodesToNodeGroupRequest(
                node_names=[start.name, end.name],
                node_group_name=group.name,
            )
        )

        assert isinstance(result, AddNodesToNodeGroupResultSuccess)
        # node_names_added should contain each name exactly once
        assert result.node_names_added.count(start.name) == 1
        assert result.node_names_added.count(end.name) == 1

    def test_end_node_not_duplicated_if_already_in_group(self) -> None:
        """If the End node is already in the group, it must not be added a second time."""
        start, end = self._build_paired_nodes()
        group = self._build_group()
        # Pre-populate the group with the end node only
        group.add_nodes_to_group([end])

        result = GriptapeNodes.handle_request(
            AddNodesToNodeGroupRequest(
                node_names=[start.name],
                node_group_name=group.name,
            )
        )

        assert isinstance(result, AddNodesToNodeGroupResultSuccess)
        # End should only appear once in group.nodes
        end_names = [n for n in group.nodes if n == end.name]
        assert len(end_names) == 1
        assert result.node_names_added.count(end.name) == 0  # not re-added
        assert result.node_names_added.count(start.name) == 1

    def test_plain_node_has_no_side_effects(self) -> None:
        """Adding a plain (non-iterative) node does not change the node_names_added list size."""
        plain = MockNode("PlainNode")
        _register(plain)
        group = self._build_group()

        result = GriptapeNodes.handle_request(
            AddNodesToNodeGroupRequest(
                node_names=[plain.name],
                node_group_name=group.name,
            )
        )

        assert isinstance(result, AddNodesToNodeGroupResultSuccess)
        assert result.node_names_added == [plain.name]
        assert plain.parent_group is group
