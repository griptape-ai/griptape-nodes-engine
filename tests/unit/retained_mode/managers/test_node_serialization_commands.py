"""Coverage for the node <-> commands round trip.

Covers on_serialize_node_to_commands and its deserialize counterpart, plus the node-group variant
used by copy/paste.

A node's live state is captured as a SerializedNodeCommands: a CreateNodeRequest for the shell,
a list of element-modification commands that recreate user-defined parameters and any changes to
library-declared ones, an optional lock command, and node-group bookkeeping. These tests build
real node types through a small in-process library (mirroring how a real library registers nodes)
so the serializer's library/metadata lookups exercise the real code path instead of a mock.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_groups.base_node_group import BaseNodeGroup
from griptape_nodes.exe_types.node_groups.subflow_node_group import SubflowNodeGroup
from griptape_nodes.exe_types.node_types import LOCAL_EXECUTION, DataNode, NodeDependencies
from griptape_nodes.node_library.library_registry import (
    LibraryMetadata,
    LibraryRegistry,
    LibrarySchema,
    NodeMetadata,
)
from griptape_nodes.retained_mode.events.context_events import EnsureWorkflowAndFlowRequest
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    CreateNodeResultSuccess,
    DeserializeNodeFromCommandsRequest,
    DeserializeNodeFromCommandsResultFailure,
    DeserializeNodeFromCommandsResultSuccess,
    SerializedNodeCommands,
    SerializeNodeToCommandsRequest,
    SerializeNodeToCommandsResultFailure,
    SerializeNodeToCommandsResultSuccess,
    SetLockNodeStateRequest,
    SetLockNodeStateResultSuccess,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.parameter_events import (
    AddParameterToNodeRequest,
    AddParameterToNodeResultSuccess,
    AlterParameterDetailsRequest,
)

if TYPE_CHECKING:
    from collections.abc import Generator

    from griptape_nodes.retained_mode.engine import Engine

_LIBRARY_NAME = "Node Serialization Test Library"


class _TextNode(DataNode):
    """A node with one library-declared string parameter, for round-trip coverage."""

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="text",
                tooltip="Text value",
                type="str",
                default_value="hello",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY, ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        pass


class _ComputedValueNode(DataNode):
    """A node whose parameter value is computed rather than stored in parameter_values.

    Used to exercise serialize_all_parameter_values: the value is never "explicitly set" and its
    declared default is None, so the normal save condition never captures it.
    """

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="computed",
                tooltip="Computed value",
                type="str",
                default_value=None,
                allowed_modes={ParameterMode.PROPERTY},
            )
        )

    def get_parameter_value(self, param_name: str) -> Any:
        if param_name == "computed":
            return "computed-value"
        return super().get_parameter_value(param_name)

    def process(self) -> None:
        pass


class _GroupNode(BaseNodeGroup):
    """Minimal concrete BaseNodeGroup (not a SubflowNodeGroup) for group-serialization coverage."""

    def run(self) -> None:
        pass

    def initialize(self) -> None:
        pass

    def process(self) -> None:
        return None


class _MinimalSubflowGroupNode(SubflowNodeGroup):
    """A SubflowNodeGroup that skips the real __init__'s subflow/publish-handler bookkeeping.

    Only isinstance(node, SubflowNodeGroup) matters for the contract under test (is_node_group),
    and the real __init__ reaches into engine-global registration machinery that a bare unit test
    has no reason to stand up. The execution_environment parameter is recreated by hand because
    on_serialize_node_to_commands reads it directly.
    """

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        BaseNodeGroup.__init__(self, name, metadata)
        self.execution_environment = Parameter(
            name="execution_environment",
            tooltip="Environment that the group should execute in",
            type="str",
            default_value=LOCAL_EXECUTION,
            allowed_modes={ParameterMode.PROPERTY},
        )
        self.add_parameter(self.execution_environment)

    async def aprocess(self) -> None:
        return None

    def process(self) -> None:
        return None


@pytest.fixture
def library_name(engine: Engine) -> Generator[str, None, None]:
    """Register a small real library and push a workflow/flow context to create nodes in.

    LibraryRegistry is process-global, so it is cleared before and after in case another test
    (or a leftover default library) left state behind.
    """
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    LibraryRegistry._clear()
    schema = LibrarySchema(
        name=_LIBRARY_NAME,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="test", description="test", library_version="0.1.0", engine_version="0.0.0", tags=[]
        ),
        categories=[],
        nodes=[],
    )
    library = LibraryRegistry.generate_new_library(schema)
    library.register_new_node_type(_TextNode, NodeMetadata(category="test", description="d", display_name="Text"))
    library.register_new_node_type(
        _ComputedValueNode, NodeMetadata(category="test", description="d", display_name="Computed")
    )
    library.register_new_node_type(_GroupNode, NodeMetadata(category="test", description="d", display_name="Group"))
    engine.handle_request(
        EnsureWorkflowAndFlowRequest(workflow_name="serialization_wf", flow_name="serialization_flow")
    )
    try:
        yield _LIBRARY_NAME
    finally:
        LibraryRegistry._clear()


def _create_text_node(engine: Engine, library_name: str, node_name: str, **kwargs: object) -> str:
    result = engine.handle_request(
        CreateNodeRequest(node_type="_TextNode", specific_library_name=library_name, node_name=node_name, **kwargs)  # type: ignore[arg-type]
    )
    assert isinstance(result, CreateNodeResultSuccess), result
    return result.node_name


class TestSerializeNodeToCommandsBasics:
    """create_node_command carries the identity and placement info needed to recreate the node."""

    def test_create_node_command_carries_type_library_and_metadata(self, engine: Engine, library_name: str) -> None:
        node_name = _create_text_node(
            engine, library_name, "N1", metadata={"position": {"x": 100, "y": 200}, "custom_key": "custom_value"}
        )

        result = engine.node_manager.on_serialize_node_to_commands(SerializeNodeToCommandsRequest(node_name=node_name))

        assert isinstance(result, SerializeNodeToCommandsResultSuccess)
        create_command = result.serialized_node_commands.create_node_command
        assert create_command.node_type == "_TextNode"
        assert create_command.specific_library_name == library_name
        assert create_command.metadata is not None
        assert create_command.metadata["position"] == {"x": 100, "y": 200}
        assert create_command.metadata["custom_key"] == "custom_value"

    def test_missing_node_name_with_empty_context_returns_failure(self, engine: Engine, library_name: str) -> None:  # noqa: ARG002
        result = engine.node_manager.on_serialize_node_to_commands(SerializeNodeToCommandsRequest(node_name=None))

        assert isinstance(result, SerializeNodeToCommandsResultFailure)

    def test_unknown_node_name_returns_failure_not_an_exception(self, engine: Engine, library_name: str) -> None:  # noqa: ARG002
        result = engine.node_manager.on_serialize_node_to_commands(
            SerializeNodeToCommandsRequest(node_name="DoesNotExist")
        )

        assert isinstance(result, SerializeNodeToCommandsResultFailure)


class TestElementModificationCommands:
    """User-defined parameters replay via AddParameterToNodeRequest; library ones only diff."""

    def test_user_defined_parameter_is_recreated_via_add_parameter_request(
        self, engine: Engine, library_name: str
    ) -> None:
        node_name = _create_text_node(engine, library_name, "N1")
        add_result = engine.handle_request(
            AddParameterToNodeRequest(
                node_name=node_name,
                parameter_name="extra",
                type="str",
                default_value="",
                tooltip="extra",
                is_user_defined=True,
            )
        )
        assert isinstance(add_result, AddParameterToNodeResultSuccess), add_result

        result = engine.node_manager.on_serialize_node_to_commands(SerializeNodeToCommandsRequest(node_name=node_name))

        assert isinstance(result, SerializeNodeToCommandsResultSuccess)
        add_commands = [
            command
            for command in result.serialized_node_commands.element_modification_commands
            if isinstance(command, AddParameterToNodeRequest) and command.parameter_name == "extra"
        ]
        assert len(add_commands) == 1

    def test_unchanged_library_parameter_is_not_re_added(self, engine: Engine, library_name: str) -> None:
        node_name = _create_text_node(engine, library_name, "N1")

        result = engine.node_manager.on_serialize_node_to_commands(SerializeNodeToCommandsRequest(node_name=node_name))

        assert isinstance(result, SerializeNodeToCommandsResultSuccess)
        add_commands_for_text = [
            command
            for command in result.serialized_node_commands.element_modification_commands
            if isinstance(command, AddParameterToNodeRequest) and command.parameter_name == "text"
        ]
        assert add_commands_for_text == []

    def test_altered_library_parameter_details_survive_as_alter_command(
        self, engine: Engine, library_name: str
    ) -> None:
        node_name = _create_text_node(engine, library_name, "N1")
        engine.handle_request(
            AlterParameterDetailsRequest(
                node_name=node_name, parameter_name="text", tooltip="Changed tooltip", default_value="changed default"
            )
        )

        result = engine.node_manager.on_serialize_node_to_commands(SerializeNodeToCommandsRequest(node_name=node_name))

        assert isinstance(result, SerializeNodeToCommandsResultSuccess)
        alter_commands = [
            command
            for command in result.serialized_node_commands.element_modification_commands
            if isinstance(command, AlterParameterDetailsRequest) and command.parameter_name == "text"
        ]
        assert len(alter_commands) == 1
        assert alter_commands[0].tooltip == "Changed tooltip"
        assert alter_commands[0].default_value == "changed default"


class TestLockState:
    """lock_node_command is emitted only when the live node is actually locked."""

    def test_locked_node_emits_lock_command(self, engine: Engine, library_name: str) -> None:
        node_name = _create_text_node(engine, library_name, "N1")
        lock_result = engine.handle_request(SetLockNodeStateRequest(node_name=node_name, lock=True))
        assert isinstance(lock_result, SetLockNodeStateResultSuccess), lock_result

        result = engine.node_manager.on_serialize_node_to_commands(SerializeNodeToCommandsRequest(node_name=node_name))

        assert isinstance(result, SerializeNodeToCommandsResultSuccess)
        lock_command = result.serialized_node_commands.lock_node_command
        assert lock_command is not None
        assert lock_command.lock is True

    def test_unlocked_node_emits_no_lock_command(self, engine: Engine, library_name: str) -> None:
        node_name = _create_text_node(engine, library_name, "N1")

        result = engine.node_manager.on_serialize_node_to_commands(SerializeNodeToCommandsRequest(node_name=node_name))

        assert isinstance(result, SerializeNodeToCommandsResultSuccess)
        assert result.serialized_node_commands.lock_node_command is None


class TestNodeUuidFreshness:
    """node_uuid identifies one serialization pass, so duplicate/paste needs a new one each time."""

    def test_two_serializations_of_same_node_yield_distinct_uuids(self, engine: Engine, library_name: str) -> None:
        node_name = _create_text_node(engine, library_name, "N1")

        first = engine.node_manager.on_serialize_node_to_commands(SerializeNodeToCommandsRequest(node_name=node_name))
        second = engine.node_manager.on_serialize_node_to_commands(SerializeNodeToCommandsRequest(node_name=node_name))

        assert isinstance(first, SerializeNodeToCommandsResultSuccess)
        assert isinstance(second, SerializeNodeToCommandsResultSuccess)
        assert first.serialized_node_commands.node_uuid != second.serialized_node_commands.node_uuid


class TestParameterValueSerializationMode:
    """use_pickling selects whether the value pool holds pickled bytes or plain deep copies."""

    def test_use_pickling_true_stores_pickled_bytes(self, engine: Engine, library_name: str) -> None:
        node_name = _create_text_node(engine, library_name, "N1")

        unique_values: dict = {}
        result = engine.node_manager.on_serialize_node_to_commands(
            SerializeNodeToCommandsRequest(
                node_name=node_name, use_pickling=True, unique_parameter_uuid_to_values=unique_values
            )
        )

        assert isinstance(result, SerializeNodeToCommandsResultSuccess)
        assert len(unique_values) >= 1
        assert all(isinstance(value, bytes) for value in unique_values.values())

    def test_use_pickling_false_stores_raw_value(self, engine: Engine, library_name: str) -> None:
        node_name = _create_text_node(engine, library_name, "N1")

        unique_values: dict = {}
        result = engine.node_manager.on_serialize_node_to_commands(
            SerializeNodeToCommandsRequest(
                node_name=node_name, use_pickling=False, unique_parameter_uuid_to_values=unique_values
            )
        )

        assert isinstance(result, SerializeNodeToCommandsResultSuccess)
        assert len(unique_values) >= 1
        assert all(value == "hello" for value in unique_values.values())

    def test_serialize_all_parameter_values_includes_values_not_otherwise_saved(
        self, engine: Engine, library_name: str
    ) -> None:
        """serialize_all_parameter_values=True captures a value the normal save condition would skip.

        _ComputedValueNode's 'computed' parameter is never explicitly set and its declared default
        is None, so the ordinary explicitly-set-or-matches-default rule finds nothing to save. The
        flag forces handle_parameter_value_saving to capture it anyway.
        """
        result_create = engine.handle_request(
            CreateNodeRequest(node_type="_ComputedValueNode", specific_library_name=library_name, node_name="C1")
        )
        assert isinstance(result_create, CreateNodeResultSuccess), result_create

        without_flag = engine.node_manager.on_serialize_node_to_commands(
            SerializeNodeToCommandsRequest(node_name="C1", serialize_all_parameter_values=False)
        )
        with_flag = engine.node_manager.on_serialize_node_to_commands(
            SerializeNodeToCommandsRequest(node_name="C1", serialize_all_parameter_values=True)
        )

        assert isinstance(without_flag, SerializeNodeToCommandsResultSuccess)
        assert isinstance(with_flag, SerializeNodeToCommandsResultSuccess)
        assert without_flag.set_parameter_value_commands == []
        assert len(with_flag.set_parameter_value_commands) == 1


class TestDeserializeNodeFromCommands:
    """on_deserialize_node_from_commands replays a SerializedNodeCommands into a live node."""

    def test_deserialize_recreates_node_type_library_and_metadata(self, engine: Engine, library_name: str) -> None:
        node_name = _create_text_node(engine, library_name, "N1", metadata={"position": {"x": 1, "y": 2}})
        serialize_result = engine.node_manager.on_serialize_node_to_commands(
            SerializeNodeToCommandsRequest(node_name=node_name)
        )
        assert isinstance(serialize_result, SerializeNodeToCommandsResultSuccess)

        result = engine.node_manager.on_deserialize_node_from_commands(
            DeserializeNodeFromCommandsRequest(serialized_node_commands=serialize_result.serialized_node_commands)
        )

        assert isinstance(result, DeserializeNodeFromCommandsResultSuccess)
        new_node = engine.node_manager.get_node_by_name(result.node_name)
        assert type(new_node) is _TextNode
        assert new_node.metadata["position"] == {"x": 1, "y": 2}

    def test_deserialize_assigns_fresh_unique_name_on_collision(self, engine: Engine, library_name: str) -> None:
        node_name = _create_text_node(engine, library_name, "N1")
        serialize_result = engine.node_manager.on_serialize_node_to_commands(
            SerializeNodeToCommandsRequest(node_name=node_name)
        )
        assert isinstance(serialize_result, SerializeNodeToCommandsResultSuccess)

        result = engine.node_manager.on_deserialize_node_from_commands(
            DeserializeNodeFromCommandsRequest(serialized_node_commands=serialize_result.serialized_node_commands)
        )

        assert isinstance(result, DeserializeNodeFromCommandsResultSuccess)
        assert result.node_name != node_name
        # The original must still exist untouched.
        assert engine.node_manager.get_node_by_name(node_name) is not None

    def test_deserialize_recreates_user_defined_parameter_on_the_new_node(
        self, engine: Engine, library_name: str
    ) -> None:
        node_name = _create_text_node(engine, library_name, "N1")
        engine.handle_request(
            AddParameterToNodeRequest(
                node_name=node_name,
                parameter_name="extra",
                type="str",
                default_value="",
                tooltip="extra",
                is_user_defined=True,
            )
        )
        serialize_result = engine.node_manager.on_serialize_node_to_commands(
            SerializeNodeToCommandsRequest(node_name=node_name)
        )
        assert isinstance(serialize_result, SerializeNodeToCommandsResultSuccess)

        result = engine.node_manager.on_deserialize_node_from_commands(
            DeserializeNodeFromCommandsRequest(serialized_node_commands=serialize_result.serialized_node_commands)
        )

        assert isinstance(result, DeserializeNodeFromCommandsResultSuccess)
        new_node = engine.node_manager.get_node_by_name(result.node_name)
        assert new_node.get_parameter_by_name("extra") is not None

    def test_deserialize_failure_cleans_up_the_partially_created_node(self, engine: Engine, library_name: str) -> None:
        """A failing element command must not leave a half-built node behind."""
        serialized = SerializedNodeCommands(
            create_node_command=CreateNodeRequest(
                node_type="_TextNode", specific_library_name=library_name, node_name="WillFail"
            ),
            element_modification_commands=[
                # parent_container_name is only checked when initial_setup is False; the real
                # serializer always sets initial_setup=True, so this is a hand-built failure case
                # rather than something the serializer itself would ever emit.
                AddParameterToNodeRequest(
                    node_name="WillFail",
                    parameter_name="extra",
                    type="str",
                    parent_container_name="NoSuchContainer",
                    initial_setup=False,
                )
            ],
            node_dependencies=NodeDependencies(),
        )

        result = engine.node_manager.on_deserialize_node_from_commands(
            DeserializeNodeFromCommandsRequest(serialized_node_commands=serialized)
        )

        assert isinstance(result, DeserializeNodeFromCommandsResultFailure)
        with pytest.raises(ValueError, match="not found"):
            engine.node_manager.get_node_by_name("WillFail")


class TestSerializeGroupWithChildren:
    """_serialize_group_with_children orders the group before its children and links them by UUID."""

    def test_group_and_children_are_both_serialized_with_parent_uuid_embedded(
        self, engine: Engine, library_name: str
    ) -> None:
        group_result = engine.handle_request(
            CreateNodeRequest(node_type="_GroupNode", specific_library_name=library_name, node_name="G1")
        )
        assert isinstance(group_result, CreateNodeResultSuccess), group_result
        _create_text_node(engine, library_name, "Child1", parent_group_name="G1")
        group_node = engine.node_manager.get_node_by_name("G1")
        assert isinstance(group_node, BaseNodeGroup)

        from griptape_nodes.retained_mode.events.node_events import SerializedParameterValueTracker

        group_serialization = engine.node_manager._serialize_group_with_children(
            group_node, {}, SerializedParameterValueTracker()
        )

        assert len(group_serialization.child_commands) == 1
        child_command = group_serialization.child_commands[0]
        assert child_command.create_node_command.metadata is not None
        assert group_serialization.group_command is not None
        assert (
            child_command.create_node_command.metadata["_parent_group_uuid"]
            == group_serialization.group_command.node_uuid
        )
        assert child_command.node_uuid in group_serialization.child_uuids

    def test_serialize_all_parameter_values_reaches_the_group_and_its_children(
        self, engine: Engine, library_name: str
    ) -> None:
        """The all-values flag must not stop at the group; children are serialized by sub-requests.

        _ComputedValueNode's 'computed' parameter is never explicitly set and its declared default
        is None, so the ordinary save condition records nothing for it. A caller asking for all
        parameter values must get it whether the node is serialized directly or as a group child.
        """
        group_result = engine.handle_request(
            CreateNodeRequest(node_type="_GroupNode", specific_library_name=library_name, node_name="G1")
        )
        assert isinstance(group_result, CreateNodeResultSuccess), group_result
        child_result = engine.handle_request(
            CreateNodeRequest(
                node_type="_ComputedValueNode",
                specific_library_name=library_name,
                node_name="C1",
                parent_group_name="G1",
            )
        )
        assert isinstance(child_result, CreateNodeResultSuccess), child_result
        group_node = engine.node_manager.get_node_by_name("G1")
        assert isinstance(group_node, BaseNodeGroup)

        from griptape_nodes.retained_mode.events.node_events import SerializedParameterValueTracker

        without_flag = engine.node_manager._serialize_group_with_children(
            group_node, {}, SerializedParameterValueTracker(), serialize_all_parameter_values=False
        )
        with_flag = engine.node_manager._serialize_group_with_children(
            group_node, {}, SerializedParameterValueTracker(), serialize_all_parameter_values=True
        )

        child_uuid_without_flag = without_flag.child_commands[0].node_uuid
        child_uuid_with_flag = with_flag.child_commands[0].node_uuid
        assert without_flag.child_parameter_commands[child_uuid_without_flag] == []
        assert len(with_flag.child_parameter_commands[child_uuid_with_flag]) == 1

    def test_subflow_name_is_dropped_for_copy_paste(self, engine: Engine, library_name: str) -> None:
        group_result = engine.handle_request(
            CreateNodeRequest(node_type="_GroupNode", specific_library_name=library_name, node_name="G1")
        )
        assert isinstance(group_result, CreateNodeResultSuccess), group_result
        group_node = engine.node_manager.get_node_by_name("G1")
        assert isinstance(group_node, BaseNodeGroup)
        group_node.metadata["subflow_name"] = "SomeSubflow"

        from griptape_nodes.retained_mode.events.node_events import SerializedParameterValueTracker

        group_serialization = engine.node_manager._serialize_group_with_children(
            group_node, {}, SerializedParameterValueTracker()
        )

        assert group_serialization.group_command is not None
        assert group_serialization.group_command.create_node_command.metadata is not None
        assert "subflow_name" not in group_serialization.group_command.create_node_command.metadata

    def test_subflow_name_is_kept_when_workflow_save_requests_it(self, engine: Engine, library_name: str) -> None:
        group_result = engine.handle_request(
            CreateNodeRequest(node_type="_GroupNode", specific_library_name=library_name, node_name="G1")
        )
        assert isinstance(group_result, CreateNodeResultSuccess), group_result
        group_node = engine.node_manager.get_node_by_name("G1")
        group_node.metadata["subflow_name"] = "SomeSubflow"

        result = engine.node_manager.on_serialize_node_to_commands(
            SerializeNodeToCommandsRequest(node_name="G1", include_existing_subflow_in_group=True)
        )

        assert isinstance(result, SerializeNodeToCommandsResultSuccess)
        metadata = result.serialized_node_commands.create_node_command.metadata
        assert metadata is not None
        assert metadata["subflow_name"] == "SomeSubflow"

    def test_is_node_group_false_for_a_plain_base_node_group(self, engine: Engine, library_name: str) -> None:
        """is_node_group is reserved for SubflowNodeGroup; a plain BaseNodeGroup is not one."""
        group_result = engine.handle_request(
            CreateNodeRequest(node_type="_GroupNode", specific_library_name=library_name, node_name="G1")
        )
        assert isinstance(group_result, CreateNodeResultSuccess), group_result

        result = engine.node_manager.on_serialize_node_to_commands(SerializeNodeToCommandsRequest(node_name="G1"))

        assert isinstance(result, SerializeNodeToCommandsResultSuccess)
        assert result.serialized_node_commands.is_node_group is False

    def test_is_node_group_true_for_a_subflow_node_group(self, engine: Engine, library_name: str) -> None:
        node = _MinimalSubflowGroupNode(
            name="SG1", metadata={"library": library_name, "node_type": "_MinimalSubflowGroupNode"}
        )
        engine.object_manager.add_object_by_name(node.name, node)
        engine.flow_manager.get_flow_by_name("serialization_flow").add_node(node)

        result = engine.node_manager.on_serialize_node_to_commands(SerializeNodeToCommandsRequest(node_name="SG1"))

        assert isinstance(result, SerializeNodeToCommandsResultSuccess)
        assert result.serialized_node_commands.is_node_group is True
