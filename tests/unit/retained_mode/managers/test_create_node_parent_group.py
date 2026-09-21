"""Unit tests for CreateNodeRequest.parent_group_name handling (PR #4195)."""

from unittest.mock import MagicMock, patch

from griptape_nodes.exe_types.node_groups.base_node_group import BaseNodeGroup
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    CreateNodeResultSuccess,
    SerializedNodeCommands,
    SerializeNodeToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.managers.node_manager import SerializedGroupResult
from tests.unit.exe_types.mocks import MockNode


class _ConcreteGroup(BaseNodeGroup):
    """Minimal concrete BaseNodeGroup for testing."""

    def run(self) -> None:
        pass

    def initialize(self) -> None:
        pass

    def process(self) -> None:
        return None


def _make_serialized_node_commands(
    node_name: str,
    node_uuid: str,
    node_names_to_add: list[str] | None = None,
    metadata: dict | None = None,
) -> SerializedNodeCommands:
    """Build a minimal SerializedNodeCommands suitable for patching on_serialize_node_to_commands."""
    return SerializedNodeCommands(
        create_node_command=CreateNodeRequest(
            node_type="T",
            node_name=node_name,
            node_names_to_add=node_names_to_add,
            metadata=metadata,
        ),
        element_modification_commands=[],
        node_dependencies=MagicMock(),
        node_uuid=SerializedNodeCommands.NodeUUID(node_uuid),
    )


def _make_serialize_result(
    node_name: str,
    node_uuid: str,
    node_names_to_add: list[str] | None = None,
    metadata: dict | None = None,
) -> SerializeNodeToCommandsResultSuccess:
    """Build a minimal SerializeNodeToCommandsResultSuccess for use as a mock return value."""
    return SerializeNodeToCommandsResultSuccess(
        serialized_node_commands=_make_serialized_node_commands(node_name, node_uuid, node_names_to_add, metadata),
        set_parameter_value_commands=[],
        result_details=MagicMock(),
    )


class TestCreateNodeParentGroupNameDefaults:
    """Verify the new parent_group_name field defaults to None in both dataclasses."""

    def test_create_node_request_defaults_to_none(self) -> None:
        assert CreateNodeRequest(node_type="T").parent_group_name is None

    def test_create_node_result_success_defaults_to_none(self) -> None:
        assert (
            CreateNodeResultSuccess(node_name="n", node_type="T", result_details=MagicMock()).parent_group_name is None
        )


class TestSerializeGroupWithChildren:
    """Tests for _serialize_group_with_children behavior introduced in PR #4195.

    Verifies two invariants:
    1. node_names_to_add is cleared on the group command so original nodes are not stolen during
       deserialization.
    2. Each child command gets _parent_group_uuid in its metadata matching the group's node_uuid,
       so deserialization can reconstruct parent-child relationships.
    """

    def _run_serialize(
        self,
        group: _ConcreteGroup,
        group_uuid: str,
        child_uuids: list[str],
        engine: Engine,
    ) -> SerializedGroupResult:
        """Call _serialize_group_with_children with mocked on_serialize_node_to_commands."""
        group_result = _make_serialize_result(
            node_name=group.name,
            node_uuid=group_uuid,
            # Simulate old-style serialization that would have included child names
            node_names_to_add=list(group.nodes.keys()),
        )
        child_results = [
            _make_serialize_result(node_name=child_name, node_uuid=uuid)
            for child_name, uuid in zip(group.nodes.keys(), child_uuids, strict=True)
        ]
        manager = engine.node_manager
        with patch.object(
            manager,
            "on_serialize_node_to_commands",
            side_effect=[group_result, *child_results],
        ):
            from griptape_nodes.retained_mode.managers.node_manager import NodeManager

            return NodeManager._serialize_group_with_children(manager, group, {}, MagicMock())

    def test_node_names_to_add_cleared_on_group_command(self, engine: Engine) -> None:
        """group_command.create_node_command.node_names_to_add is None after serialization.

        This prevents on_create_node_request from calling add_nodes_to_group on the original
        child names when the group is deserialized into a new copy.
        """
        group = _ConcreteGroup("MyGroup")
        group.add_nodes_to_group([MockNode("Child")])

        result = self._run_serialize(group, "group-uuid-1", ["child-uuid-1"], engine)

        assert result.group_command is not None
        assert result.group_command.create_node_command.node_names_to_add is None

    def test_children_embed_parent_group_uuid_in_metadata(self, engine: Engine) -> None:
        """Each child command's metadata contains _parent_group_uuid == group's node_uuid.

        This allows on_deserialize_selected_nodes_from_commands to set parent_group_name on each
        child's CreateNodeRequest after the group has been created and assigned a real name.
        """
        group = _ConcreteGroup("MyGroup")
        group.add_nodes_to_group([MockNode("ChildA"), MockNode("ChildB")])

        group_uuid = "group-uuid-abc"
        child_uuids = ["child-uuid-1", "child-uuid-2"]
        result = self._run_serialize(group, group_uuid, child_uuids, engine)

        assert len(result.child_commands) == len(child_uuids)
        for child_cmd in result.child_commands:
            metadata = child_cmd.create_node_command.metadata
            assert metadata is not None
            assert metadata.get("_parent_group_uuid") == group_uuid


class TestAddNodesToGroupMechanism:
    """Tests for the add_nodes_to_group mechanism that parent_group_name handling delegates to.

    on_create_node_request calls parent_group.add_nodes_to_group([node]) when parent_group_name
    is set. These tests confirm that mechanism behaves correctly.
    """

    def test_add_nodes_to_group_sets_parent_group_on_node(self) -> None:
        """node.parent_group is set to the group after add_nodes_to_group."""
        group = _ConcreteGroup("MyGroup")
        node = MockNode("MyNode")

        group.add_nodes_to_group([node])

        assert node.parent_group is group

    def test_add_nodes_to_group_registers_node_in_group(self) -> None:
        """Node appears in group.nodes and group.metadata['node_names_in_group'] after being added."""
        group = _ConcreteGroup("MyGroup")
        node = MockNode("MyNode")

        group.add_nodes_to_group([node])

        assert node.name in group.nodes
        assert node.name in group.metadata["node_names_in_group"]

    def test_parent_group_name_in_result_reflects_group(self) -> None:
        """CreateNodeResultSuccess.parent_group_name equals the group's name when a node belongs to a group.

        Validates the expression used in on_create_node_request:
            parent_group_name=node.parent_group.name if node.parent_group else None
        """
        group = _ConcreteGroup("MyGroup")
        node = MockNode("MyNode")

        group.add_nodes_to_group([node])

        parent_group_name = node.parent_group.name if node.parent_group else None
        assert parent_group_name == "MyGroup"

    def test_parent_group_name_is_none_when_no_group(self) -> None:
        """CreateNodeResultSuccess.parent_group_name is None when node has no parent group."""
        node = MockNode("MyNode")

        parent_group_name = node.parent_group.name if node.parent_group else None
        assert parent_group_name is None


class TestCreateNodeWithUnresolvableParentGroup:
    """An unresolvable parent_group_name must degrade to an ungrouped node, not blow up the request.

    `NodeManager.get_node_by_name` signals a missing node with ValueError, so catching KeyError here
    let that ValueError escape on_create_node_request entirely: the caller got an exception instead of
    a result, and the node — already registered — was left orphaned.
    """

    def _bootstrap(self, engine: Engine) -> None:
        from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess

        engine.context_manager.push_workflow("parent_group_wf")
        result = engine.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="parent_group_flow", set_as_new_context=True)
        )
        assert isinstance(result, CreateFlowResultSuccess)

    def test_missing_parent_group_still_returns_success(self, engine: Engine) -> None:
        """A parent_group_name naming nothing is logged and skipped; creation still succeeds."""
        self._bootstrap(engine)

        result = engine.handle_request(
            CreateNodeRequest(node_type="Note", node_name="Orphan", parent_group_name="NoSuchGroup")
        )

        assert isinstance(result, CreateNodeResultSuccess), result
        assert result.parent_group_name is None
        node = engine.node_manager.get_node_by_name(result.node_name)
        assert node.parent_group is None

    def test_parent_group_name_that_is_not_a_group_still_returns_success(self, engine: Engine) -> None:
        """Pointing parent_group_name at an ordinary node is refused without failing the create."""
        self._bootstrap(engine)

        existing = engine.handle_request(CreateNodeRequest(node_type="Note", node_name="PlainNode"))
        assert isinstance(existing, CreateNodeResultSuccess)

        result = engine.handle_request(
            CreateNodeRequest(node_type="Note", node_name="Orphan", parent_group_name=existing.node_name)
        )

        assert isinstance(result, CreateNodeResultSuccess), result
        assert result.parent_group_name is None
