"""Regression tests for the interactive GUI path.

An iterative Start/End pair must never be split across a group boundary: adding either
half to a group automatically pulls in its counterpart. The GUI sends only the name of
whichever node the user selected, so both directions matter.
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
from griptape_nodes.exe_types.node_groups.subflow_node_group import SubflowNodeGroup
from griptape_nodes.retained_mode.events.node_events import (
    AddNodesToNodeGroupRequest,
    AddNodesToNodeGroupResultSuccess,
    RemoveNodeFromNodeGroupRequest,
    RemoveNodeFromNodeGroupResultSuccess,
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
    """Minimal concrete BaseNodeGroup for testing.

    A plain group is a visual grouping with no subflow, so it must NOT tether.
    """

    def run(self) -> None:
        pass

    def initialize(self) -> None:
        pass

    def process(self) -> None:
        return None


class _ConcreteSubflowGroup(SubflowNodeGroup):
    """Minimal concrete SubflowNodeGroup: tethering is scoped to groups that own a subflow.

    `_create_subflow` is a no-op so `subflow_name` stays unset and the per-node
    MoveNodeToNewFlowRequest loop is skipped — group membership is what these tests exercise,
    not flow relocation.
    """

    def _create_subflow(self) -> None:
        return

    async def aprocess(self) -> None:
        return

    def run(self) -> None:
        pass

    def initialize(self) -> None:
        pass


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

    def _build_group(self) -> _ConcreteSubflowGroup:
        """Build a subflow group and register it in the ObjectManager."""
        group = _ConcreteSubflowGroup("TestGroup")
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

    def test_start_node_is_pulled_into_group_automatically(self) -> None:
        """Mirror of the Start case: grouping only the End node must also group its Start."""
        start, end = self._build_paired_nodes()
        group = self._build_group()

        result = GriptapeNodes.handle_request(
            AddNodesToNodeGroupRequest(
                node_names=[end.name],
                node_group_name=group.name,
            )
        )

        assert isinstance(result, AddNodesToNodeGroupResultSuccess), result
        assert start.parent_group is group
        assert end.parent_group is group
        assert start.name in group.nodes
        assert end.name in group.nodes
        assert result.node_names_added == [end.name, start.name]

    def test_untethered_nodes_pull_in_nothing(self) -> None:
        """An iterative node with no counterpart wired up must not pull in anything."""
        lone_start = _MockIterativeStartNode("LoneStart")
        lone_end = _MockIterativeEndNode("LoneEnd")
        _register(lone_start)
        _register(lone_end)
        group = self._build_group()

        nodes_added = group.add_nodes_to_group([lone_start, lone_end])

        assert [n.name for n in nodes_added] == [lone_start.name, lone_end.name]

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

    def test_group_pulls_in_end_node_without_going_through_manager(self) -> None:
        """The pairing rule is enforced by BaseNodeGroup itself, not by NodeManager.

        Calling add_nodes_to_group directly must still group the End node and report it.
        """
        start, end = self._build_paired_nodes()
        group = self._build_group()

        nodes_added = group.add_nodes_to_group([start])

        assert end.name in group.nodes
        assert end.parent_group is group
        assert [n.name for n in nodes_added] == [start.name, end.name]

    def test_group_pulls_in_start_node_without_going_through_manager(self) -> None:
        """Mirror: the reverse direction is also enforced at the group layer."""
        start, end = self._build_paired_nodes()
        group = self._build_group()

        nodes_added = group.add_nodes_to_group([end])

        assert start.name in group.nodes
        assert start.parent_group is group
        assert [n.name for n in nodes_added] == [end.name, start.name]

    def test_group_returns_only_requested_node_when_end_already_present(self) -> None:
        """add_nodes_to_group must not re-report an End node that is already in the group."""
        start, end = self._build_paired_nodes()
        group = self._build_group()
        group.add_nodes_to_group([end])

        nodes_added = group.add_nodes_to_group([start])

        assert [n.name for n in nodes_added] == [start.name]

    def test_group_returns_only_requested_node_when_start_already_present(self) -> None:
        """Mirror: an End node must not re-report a Start node already in the group."""
        start, end = self._build_paired_nodes()
        group = self._build_group()
        # Adding Start pulls End in as well, so both are already present here.
        group.add_nodes_to_group([start])

        nodes_added = group.add_nodes_to_group([end])

        assert [n.name for n in nodes_added] == [end.name]
        assert start.name in group.nodes

    def test_companion_follows_its_partner_between_groups(self) -> None:
        """Reparenting a Start node moves its End node along, leaving no split across groups.

        The old group must not keep advertising a node it no longer holds.
        """
        start, end = self._build_paired_nodes()
        group_a = self._build_group()
        group_b = _ConcreteSubflowGroup("TestGroupB")
        _register(group_b)
        group_a.add_nodes_to_group([start])

        nodes_added = group_b.add_nodes_to_group([start])

        assert {n.name for n in nodes_added} == {start.name, end.name}
        assert start.parent_group is group_b
        assert end.parent_group is group_b
        assert group_a.nodes == {}
        assert group_a.metadata["node_names_in_group"] == []

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


class TestRemoveNodesFromGroupIterativePath:
    """Verify that removal is the mirror of addition: the pair leaves the group together."""

    @pytest.fixture(autouse=True)
    def _setup_context(self, griptape_nodes: GriptapeNodes) -> None:  # noqa: ARG002
        """Push a workflow and flow context so group-operation helpers can find a current flow."""
        GriptapeNodes.ContextManager().push_workflow(workflow_name="test_workflow")
        flow = ControlFlow(name="test_flow")
        GriptapeNodes.ObjectManager().add_object_by_name(flow.name, flow)
        GriptapeNodes.ContextManager().push_flow(flow)

    def _build_grouped_pair(self) -> tuple[_MockIterativeStartNode, _MockIterativeEndNode, _ConcreteSubflowGroup]:
        """Build a tethered Start/End pair already sitting inside a subflow group."""
        start = _MockIterativeStartNode("TestStartNode")
        end = _MockIterativeEndNode("TestEndNode")
        start.end_node = end
        end.start_node = start
        _register(start)
        _register(end)
        group = _ConcreteSubflowGroup("TestGroup")
        _register(group)
        group.add_nodes_to_group([start])
        return start, end, group

    def test_remove_start_node_also_removes_end_node(self) -> None:
        """Removing only the Start node must not orphan its End node inside the group."""
        start, end, group = self._build_grouped_pair()

        result = GriptapeNodes.handle_request(
            RemoveNodeFromNodeGroupRequest(
                node_names=[start.name],
                node_group_name=group.name,
            )
        )

        assert isinstance(result, RemoveNodeFromNodeGroupResultSuccess), result
        assert group.nodes == {}
        assert start.parent_group is None
        assert end.parent_group is None
        assert group.metadata["node_names_in_group"] == []
        assert set(result.node_names_removed) == {start.name, end.name}
        assert result.node_group_name == group.name

    def test_remove_end_node_also_removes_start_node(self) -> None:
        """Mirror: removing only the End node must also pull its Start node out."""
        start, end, group = self._build_grouped_pair()

        result = GriptapeNodes.handle_request(
            RemoveNodeFromNodeGroupRequest(
                node_names=[end.name],
                node_group_name=group.name,
            )
        )

        assert isinstance(result, RemoveNodeFromNodeGroupResultSuccess), result
        assert group.nodes == {}
        assert start.parent_group is None
        assert end.parent_group is None

    def test_remove_does_not_duplicate_when_both_named(self) -> None:
        """Naming both halves explicitly removes each exactly once."""
        start, end, group = self._build_grouped_pair()

        result = GriptapeNodes.handle_request(
            RemoveNodeFromNodeGroupRequest(
                node_names=[start.name, end.name],
                node_group_name=group.name,
            )
        )

        assert isinstance(result, RemoveNodeFromNodeGroupResultSuccess)
        assert result.node_names_removed.count(start.name) == 1
        assert result.node_names_removed.count(end.name) == 1

    def test_remove_ignores_companion_outside_this_group(self) -> None:
        """A companion this group does not own must not be reported as removed from it.

        Grouping the Start node normally pulls the End node in too, so build the split state
        directly: only the Start node is a member, while the pair stays tethered.
        """
        start = _MockIterativeStartNode("TestStartNode")
        end = _MockIterativeEndNode("TestEndNode")
        start.end_node = end
        end.start_node = start
        _register(start)
        _register(end)
        group = _ConcreteSubflowGroup("TestGroup")
        _register(group)
        group._add_nodes_to_group_dict([start])

        nodes_removed = group.remove_nodes_from_group([start])

        assert [n.name for n in nodes_removed] == [start.name]
        assert end.parent_group is None

    def test_remove_plain_node_has_no_side_effects(self) -> None:
        """Removing a plain (non-iterative) node pulls nothing else out."""
        plain = MockNode("PlainNode")
        other = MockNode("OtherNode")
        _register(plain)
        _register(other)
        group = _ConcreteSubflowGroup("TestGroup")
        _register(group)
        group.add_nodes_to_group([plain, other])

        result = GriptapeNodes.handle_request(
            RemoveNodeFromNodeGroupRequest(
                node_names=[plain.name],
                node_group_name=group.name,
            )
        )

        assert isinstance(result, RemoveNodeFromNodeGroupResultSuccess)
        assert result.node_names_removed == [plain.name]
        assert plain.parent_group is None
        assert other.name in group.nodes


class TestPlainNodeGroupDoesNotTether:
    """Tethering is scoped to subflow groups; a plain BaseNodeGroup groups exactly what it is asked to.

    A plain group is a visual grouping with no subflow of its own, so a Start/End pair spanning its
    boundary costs nothing. Only subflow groups relocate nodes into a real flow, which is what makes
    a split pair a problem worth auto-correcting.
    """

    @pytest.fixture(autouse=True)
    def _setup_context(self, griptape_nodes: GriptapeNodes) -> None:  # noqa: ARG002
        """Push a workflow and flow context so group-operation helpers can find a current flow."""
        GriptapeNodes.ContextManager().push_workflow(workflow_name="test_workflow")
        flow = ControlFlow(name="test_flow")
        GriptapeNodes.ObjectManager().add_object_by_name(flow.name, flow)
        GriptapeNodes.ContextManager().push_flow(flow)

    def _build_paired_nodes(self) -> tuple[_MockIterativeStartNode, _MockIterativeEndNode]:
        start = _MockIterativeStartNode("TestStartNode")
        end = _MockIterativeEndNode("TestEndNode")
        start.end_node = end
        end.start_node = start
        _register(start)
        _register(end)
        return start, end

    def test_plain_group_does_not_pull_in_end_node(self) -> None:
        """Adding a Start node to a plain group leaves its End node alone."""
        start, end = self._build_paired_nodes()
        group = _ConcreteGroup("PlainGroup")
        _register(group)

        nodes_added = group.add_nodes_to_group([start])

        assert [n.name for n in nodes_added] == [start.name]
        assert end.name not in group.nodes
        assert end.parent_group is None

    def test_plain_group_does_not_pull_out_end_node(self) -> None:
        """Removing a Start node from a plain group leaves its End node in place."""
        start, end = self._build_paired_nodes()
        group = _ConcreteGroup("PlainGroup")
        _register(group)
        group.add_nodes_to_group([start, end])

        nodes_removed = group.remove_nodes_from_group([start])

        assert [n.name for n in nodes_removed] == [start.name]
        assert end.name in group.nodes
        assert end.parent_group is group

    def test_remove_reports_only_nodes_that_were_members(self) -> None:
        """A non-member is skipped, so it must not be reported as removed.

        The handler forwards this list to the GUI as `node_names_removed`; including a node that
        never left would make the GUI unparent something still in the group.
        """
        start, _ = self._build_paired_nodes()
        stranger = MockNode(name="StrangerNode")
        _register(stranger)
        group = _ConcreteGroup("PlainGroup")
        _register(group)
        group.add_nodes_to_group([start])

        nodes_removed = group.remove_nodes_from_group([start, stranger])

        assert [n.name for n in nodes_removed] == [start.name]


class TestCrossGroupMoveKeepsTetheredCompanion:
    """Moving one half of a tethered pair out of a subflow group must not orphan the other half.

    The destination asks the source to release the node it was given; a subflow source releases the
    companion too. Those extra releases have to land in the destination, or the companion ends up in
    no group at all while the GUI is told nothing about it.
    """

    @pytest.fixture(autouse=True)
    def _setup_context(self, griptape_nodes: GriptapeNodes) -> None:  # noqa: ARG002
        """Push a workflow and flow context so group-operation helpers can find a current flow."""
        GriptapeNodes.ContextManager().push_workflow(workflow_name="test_workflow")
        flow = ControlFlow(name="test_flow")
        GriptapeNodes.ObjectManager().add_object_by_name(flow.name, flow)
        GriptapeNodes.ContextManager().push_flow(flow)

    def _build_paired_nodes(self) -> tuple[_MockIterativeStartNode, _MockIterativeEndNode]:
        start = _MockIterativeStartNode("TestStartNode")
        end = _MockIterativeEndNode("TestEndNode")
        start.end_node = end
        end.start_node = start
        _register(start)
        _register(end)
        return start, end

    def test_plain_destination_absorbs_companion_released_by_subflow_source(self) -> None:
        """A plain group does not tether, but must still keep whatever the source hands it."""
        start, end = self._build_paired_nodes()
        source = _ConcreteSubflowGroup("SourceGroup")
        _register(source)
        source.add_nodes_to_group([start])
        assert end.parent_group is source

        destination = _ConcreteGroup("DestinationGroup")
        _register(destination)
        nodes_added = destination.add_nodes_to_group([start])

        # The companion followed its partner rather than being ejected into no group at all.
        assert {n.name for n in nodes_added} == {start.name, end.name}
        assert start.parent_group is destination
        assert end.parent_group is destination
        assert end.name in destination.nodes
        assert end.name in destination.metadata["node_names_in_group"]
        assert source.nodes == {}

    def test_move_between_subflow_groups_keeps_pair_together(self) -> None:
        """Same guarantee when both ends are subflow groups, which tether on add as well."""
        start, end = self._build_paired_nodes()
        source = _ConcreteSubflowGroup("SourceGroup")
        _register(source)
        source.add_nodes_to_group([start])

        destination = _ConcreteSubflowGroup("DestinationGroup")
        _register(destination)
        nodes_added = destination.add_nodes_to_group([start])

        assert {n.name for n in nodes_added} == {start.name, end.name}
        assert start.parent_group is destination
        assert end.parent_group is destination
        assert source.nodes == {}
