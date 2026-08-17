"""What a group looks like after a membership change fails partway through.

Adding nodes to a group and removing them again both take several steps that can fail: each node is
moved between flows one at a time, and a nested group's own subflow is reparented separately. If the
group records the membership before those moves, a failure halfway leaves the group claiming nodes
that are not in its subflow -- the editor draws them inside the group while a save writes them
outside it, and the artist has no way to tell.

These tests fail the move deliberately and then check the group is back to a state that matches
where the nodes actually live.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.node_groups.base_node_group import BaseNodeGroup
from griptape_nodes.retained_mode.engine import current_engine
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import (
    AddNodesToNodeGroupRequest,
    AddNodesToNodeGroupResultFailure,
    AddNodesToNodeGroupResultSuccess,
    CreateNodeRequest,
    CreateNodeResultSuccess,
    MoveNodeToNewFlowRequest,
    MoveNodeToNewFlowResultFailure,
    RemoveNodeFromNodeGroupRequest,
    RemoveNodeFromNodeGroupResultFailure,
    RemoveNodeFromNodeGroupResultSuccess,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

if TYPE_CHECKING:
    from collections.abc import Callable

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "subflow_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "subflow_echo_node.py"


@pytest.fixture
def library_name(tmp_path: Path, materialize_library: Callable[..., Path]) -> str:
    """Register the subflow fixture library into a clean engine and return its name."""
    library_json = materialize_library(
        tmp_path / "library", template=FIXTURE_LIBRARY_JSON_TEMPLATE, node_file=FIXTURE_NODE_FILE
    )
    GriptapeNodes.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    register_result = GriptapeNodes.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result
    GriptapeNodes.ContextManager().push_workflow(workflow_name="membership_failure_workflow")
    return register_result.library_name


def _create_node(node_type: str, node_name: str, library: str, **kwargs: object) -> str:
    result = GriptapeNodes.handle_request(
        CreateNodeRequest(node_type=node_type, specific_library_name=library, node_name=node_name, **kwargs)  # type: ignore[arg-type]
    )
    assert isinstance(result, CreateNodeResultSuccess), result
    return result.node_name


def _get_group(node_name: str) -> BaseNodeGroup:
    node = GriptapeNodes.NodeManager().get_node_by_name(node_name)
    assert isinstance(node, BaseNodeGroup), f"expected '{node_name}' to be a group, got {type(node).__name__}"
    return node


def _flow_of(node_name: str) -> str:
    return GriptapeNodes.NodeManager().get_node_parent_flow_by_name(node_name)


def _fail_every_move(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every MoveNodeToNewFlowRequest fail, as if the engine refused it.

    Patches the event manager's dispatch table rather than NodeManager: the handler was bound into
    that table at registration, so replacing the method on the manager would not be seen.
    """
    event_manager = current_engine().event_manager

    def refuse_move(request: MoveNodeToNewFlowRequest) -> MoveNodeToNewFlowResultFailure:  # noqa: ARG001
        return MoveNodeToNewFlowResultFailure(result_details="refused for this test")

    monkeypatch.setitem(event_manager._request_type_to_manager, MoveNodeToNewFlowRequest, refuse_move)


class TestFailedAdd:
    def test_reports_failure_rather_than_claiming_a_node_it_could_not_move(
        self, library_name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A group that cannot take a node in must not list it as a member anyway."""
        flow = GriptapeNodes.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="FailedAddFlow", set_as_new_context=False)
        )
        assert isinstance(flow, CreateFlowResultSuccess), flow

        with GriptapeNodes.ContextManager().flow(flow.flow_name):
            group_name = _create_node("SubflowGroupNode", "Group", library_name)
            loose_name = _create_node("EchoNode", "Loose", library_name)

        original_flow = _flow_of(loose_name)
        _fail_every_move(monkeypatch)

        with GriptapeNodes.ContextManager().flow(flow.flow_name):
            result = GriptapeNodes.handle_request(
                AddNodesToNodeGroupRequest(node_names=[loose_name], node_group_name=group_name)
            )

        # The artist has to be told, rather than shown a group that only looks filled.
        assert isinstance(result, AddNodesToNodeGroupResultFailure), result

        group = _get_group(group_name)
        node = GriptapeNodes.NodeManager().get_node_by_name(loose_name)
        assert loose_name not in group.nodes
        assert group.metadata.get("node_names_in_group") == []
        assert node.parent_group is None
        # The node never moved, so it is still where the artist left it.
        assert _flow_of(loose_name) == original_flow


class TestFailedRemove:
    def test_reports_failure_rather_than_dropping_a_node_it_could_not_move_out(
        self, library_name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A group that cannot let a node out must keep listing it, and say the remove failed."""
        flow = GriptapeNodes.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="FailedRemoveFlow", set_as_new_context=False)
        )
        assert isinstance(flow, CreateFlowResultSuccess), flow

        with GriptapeNodes.ContextManager().flow(flow.flow_name):
            group_name = _create_node("SubflowGroupNode", "Group", library_name)
            member_name = _create_node("EchoNode", "Member", library_name, parent_group_name=group_name)

        group = _get_group(group_name)
        assert member_name in group.nodes, "member should start out inside the group"
        subflow_name = _flow_of(member_name)

        _fail_every_move(monkeypatch)

        with GriptapeNodes.ContextManager().flow(flow.flow_name):
            result = GriptapeNodes.handle_request(
                RemoveNodeFromNodeGroupRequest(node_names=[member_name], node_group_name=group_name)
            )

        # A move failure has to surface as a failure result, not escape as an unhandled error.
        assert isinstance(result, RemoveNodeFromNodeGroupResultFailure), result

        member = GriptapeNodes.NodeManager().get_node_by_name(member_name)
        assert member_name in group.nodes
        assert group.metadata.get("node_names_in_group") == [member_name]
        assert member.parent_group is group
        # The node never moved, so the group still holds it in its subflow.
        assert _flow_of(member_name) == subflow_name

    def test_removes_a_node_from_a_group_when_the_moves_succeed(self, library_name: str) -> None:
        """The success path still works: the node leaves the group and its subflow."""
        flow = GriptapeNodes.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="RemoveFlow", set_as_new_context=False)
        )
        assert isinstance(flow, CreateFlowResultSuccess), flow

        with GriptapeNodes.ContextManager().flow(flow.flow_name):
            group_name = _create_node("SubflowGroupNode", "Group", library_name)
            member_name = _create_node("EchoNode", "Member", library_name, parent_group_name=group_name)

            result = GriptapeNodes.handle_request(
                RemoveNodeFromNodeGroupRequest(node_names=[member_name], node_group_name=group_name)
            )

        assert isinstance(result, RemoveNodeFromNodeGroupResultSuccess), result

        group = _get_group(group_name)
        member = GriptapeNodes.NodeManager().get_node_by_name(member_name)
        assert member_name not in group.nodes
        assert group.metadata.get("node_names_in_group") == []
        assert member.parent_group is None
        # It went back out to the flow holding the group, not left inside the subflow.
        assert _flow_of(member_name) == flow.flow_name

    def test_moves_a_nested_group_subflow_back_out_with_it(self, library_name: str) -> None:
        """Removing a nested group has to take its contents along, or a save loses them."""
        flow = GriptapeNodes.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="UnnestFlow", set_as_new_context=False)
        )
        assert isinstance(flow, CreateFlowResultSuccess), flow

        with GriptapeNodes.ContextManager().flow(flow.flow_name):
            outer_name = _create_node("SubflowGroupNode", "OuterGroup", library_name)
            inner_name = _create_node("SubflowGroupNode", "InnerGroup", library_name)
            _create_node("EchoNode", "Leaf", library_name, parent_group_name=inner_name)

            nest_result = GriptapeNodes.handle_request(
                AddNodesToNodeGroupRequest(node_names=[inner_name], node_group_name=outer_name)
            )
            assert isinstance(nest_result, AddNodesToNodeGroupResultSuccess), nest_result

            inner = _get_group(inner_name)
            inner_subflow = inner.metadata["subflow_name"]
            outer_subflow = _get_group(outer_name).metadata["subflow_name"]
            assert GriptapeNodes.FlowManager().get_parent_flow(inner_subflow) == outer_subflow

            result = GriptapeNodes.handle_request(
                RemoveNodeFromNodeGroupRequest(node_names=[inner_name], node_group_name=outer_name)
            )

        assert isinstance(result, RemoveNodeFromNodeGroupResultSuccess), result

        # The inner group's subflow follows it out, so its Leaf is still reachable from the top.
        assert GriptapeNodes.FlowManager().get_parent_flow(inner_subflow) == flow.flow_name
        assert _flow_of(inner_name) == flow.flow_name
