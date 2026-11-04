"""What a group claims after one of its children is deleted.

Deleting a node removes it from its flow and from the object manager, but the group holding it keeps
its own `nodes` dict. Only `SubflowNodeGroup` used to be given a chance to drop the entry, so a group
that is not subflow-backed went on naming a node that no longer exists — and anything reading group
contents (the involved-node list an editor is handed at the start of a run, the saved
`node_names_in_group` metadata) reported the ghost.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from griptape_nodes.exe_types.node_groups.base_node_group import BaseNodeGroup
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    CreateNodeResultSuccess,
    DeleteNodeRequest,
    DeleteNodeResultSuccess,
)
from tests.unit.exe_types.mocks import MockNode

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine


class _PlainGroup(BaseNodeGroup):
    """A group with no subflow of its own, which is what a node library is free to define."""

    def process(self) -> None:
        return None


class TestDeleteNodesFromGroup:
    def test_drops_membership_and_metadata(self) -> None:
        group = _PlainGroup("MyGroup")
        kept = MockNode("Kept")
        deleted = MockNode("Deleted")
        group.add_nodes_to_group([kept, deleted])

        group.delete_nodes_from_group([deleted])

        assert list(group.nodes) == ["Kept"]
        assert group.metadata["node_names_in_group"] == ["Kept"]

    def test_skips_nodes_it_never_held(self) -> None:
        """A best-effort list is allowed, the same way remove_nodes_from_group allows one."""
        group = _PlainGroup("MyGroup")
        group.add_nodes_to_group([MockNode("Kept")])

        group.delete_nodes_from_group([MockNode("Stranger")])

        assert list(group.nodes) == ["Kept"]


class TestDeleteNodeRequestPrunesGroup:
    def test_deleted_child_is_no_longer_claimed_by_a_plain_group(self, engine: Engine) -> None:
        """The whole point: after the delete request, the group cannot name the deleted node."""
        engine.context_manager.push_workflow("delete_group_member_wf")
        flow_result = engine.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="delete_group_member_flow", set_as_new_context=True)
        )
        assert isinstance(flow_result, CreateFlowResultSuccess), flow_result

        child_result = engine.handle_request(CreateNodeRequest(node_type="Note", node_name="Child"))
        assert isinstance(child_result, CreateNodeResultSuccess), child_result
        child = engine.node_manager.get_node_by_name(child_result.node_name)

        # Register the group the way on_create_node_request does, since every group node type that
        # ships is subflow-backed and this covers the plain case a library can introduce.
        group = _PlainGroup("PlainGroup")
        parent_flow = engine.flow_manager.get_flow_by_name(flow_result.flow_name)
        parent_flow.add_node(group)
        engine.object_manager.add_object_by_name(group.name, group)
        engine.node_manager._name_to_parent_flow_name[group.name] = flow_result.flow_name
        group.add_nodes_to_group([child])

        delete_result = engine.handle_request(DeleteNodeRequest(node_name=child.name))

        assert isinstance(delete_result, DeleteNodeResultSuccess), delete_result
        assert child.name not in group.nodes
        assert child.name not in group.metadata["node_names_in_group"]
        assert child.name not in engine.flow_manager.get_involved_node_names(parent_flow)
