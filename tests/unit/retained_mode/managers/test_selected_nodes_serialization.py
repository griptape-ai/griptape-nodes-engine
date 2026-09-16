"""Copy/paste (selected nodes) serialization tests.

``on_serialize_selected_nodes_to_commands`` packages a selection of nodes into a
``SerializedSelectedNodesCommands`` object, pickles it, and hands back the pickle bytes
(latin-1 decoded into a ``str`` so the payload can travel inside a JSON envelope alongside
the rest of an ``EventResult``). ``on_deserialize_selected_nodes_from_commands`` reverses
that to rebuild the nodes, their parameter values, their lock state, and the connections
between them.

Node types come from a library registered in-process for this module (``LibraryRegistry``
is process-global; nothing here assumes a real Griptape Nodes standard library is installed
on the host running the suite).
"""

from __future__ import annotations

import pickle
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_groups.base_node_group import BaseNodeGroup
from griptape_nodes.exe_types.node_types import DataNode
from griptape_nodes.node_library.library_registry import (
    LibraryMetadata,
    LibraryRegistry,
    LibrarySchema,
    NodeMetadata,
)
from griptape_nodes.retained_mode.events.connection_events import CreateConnectionRequest, CreateConnectionResultSuccess
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.node_events import (
    AddNodesToNodeGroupRequest,
    AddNodesToNodeGroupResultSuccess,
    CreateNodeRequest,
    CreateNodeResultSuccess,
    DeserializeSelectedNodesFromCommandsRequest,
    DeserializeSelectedNodesFromCommandsResultSuccess,
    NewPosition,
    SerializedSelectedNodesCommands,
    SerializeSelectedNodesToCommandsRequest,
    SerializeSelectedNodesToCommandsResultFailure,
    SerializeSelectedNodesToCommandsResultSuccess,
    SetLockNodeStateRequest,
    SetLockNodeStateResultSuccess,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.parameter_events import (
    GetParameterValueRequest,
    GetParameterValueResultSuccess,
    SetParameterValueRequest,
)

if TYPE_CHECKING:
    from collections.abc import Generator

    from griptape_nodes.retained_mode.engine import Engine

_LIBRARY_NAME = "selected-nodes-serialization-test-library"


class _CopyPasteNode(DataNode):
    """Minimal DataNode with a connectable "value" parameter for round-trip assertions."""

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="value",
                tooltip="Value to echo",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY, ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        self.parameter_output_values["value"] = self.get_parameter_value("value")


class _CopyPasteGroup(BaseNodeGroup):
    """Minimal concrete BaseNodeGroup: a plain visual grouping with no subflow."""

    def run(self) -> None:
        pass

    def initialize(self) -> None:
        pass

    def process(self) -> None:
        return None


@pytest.fixture
def library_name() -> Generator[str, None, None]:
    """Register a tiny real library in-process so serialized nodes resolve real library metadata.

    LibraryRegistry keeps its state in class-level dicts shared across the whole process, so it
    is cleared before and after this fixture runs to avoid bleeding into other test modules.
    """
    LibraryRegistry._clear()
    schema = LibrarySchema(
        name=_LIBRARY_NAME,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="test",
            description="Selected-nodes serialization test fixtures",
            library_version="1.0.0",
            engine_version="1.0.0",
            tags=[],
        ),
        categories=[],
        nodes=[],
    )
    library = LibraryRegistry.generate_new_library(library_data=schema)
    library.register_new_node_type(
        _CopyPasteNode,
        NodeMetadata(category="test", description="Echoes a value", display_name="Copy Paste Node"),
    )
    library.register_new_node_type(
        _CopyPasteGroup,
        NodeMetadata(category="test", description="Plain grouping node", display_name="Copy Paste Group"),
    )
    yield _LIBRARY_NAME
    LibraryRegistry._clear()


@pytest.fixture
def clean_object_state(engine: Engine) -> Generator[None, None, None]:
    """Clear all object state around a test so leftover flows never bleed across tests."""
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    try:
        yield
    finally:
        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))


@pytest.fixture
def flow_name(engine: Engine, library_name: str, clean_object_state: None) -> str:  # noqa: ARG001
    """Push a fresh workflow/flow context that the rest of the test builds nodes in."""
    engine.context_manager.push_workflow(workflow_name="selected_nodes_wf")
    result = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="main", set_as_new_context=True))
    assert isinstance(result, CreateFlowResultSuccess), result
    return result.flow_name


def _create_node(engine: Engine, node_name: str) -> str:
    """Create a real ``_CopyPasteNode`` through the normal CreateNodeRequest path."""
    result = engine.handle_request(
        CreateNodeRequest(node_type="_CopyPasteNode", specific_library_name=_LIBRARY_NAME, node_name=node_name)
    )
    assert isinstance(result, CreateNodeResultSuccess), result
    return result.node_name


def _create_group(engine: Engine, node_name: str) -> str:
    """Create a real ``_CopyPasteGroup`` through the normal CreateNodeRequest path."""
    result = engine.handle_request(
        CreateNodeRequest(node_type="_CopyPasteGroup", specific_library_name=_LIBRARY_NAME, node_name=node_name)
    )
    assert isinstance(result, CreateNodeResultSuccess), result
    return result.node_name


def _connect(engine: Engine, source_node: str, target_node: str) -> None:
    result = engine.handle_request(
        CreateConnectionRequest(
            source_node_name=source_node,
            source_parameter_name="value",
            target_node_name=target_node,
            target_parameter_name="value",
        )
    )
    assert isinstance(result, CreateConnectionResultSuccess), result


def _serialize(
    engine: Engine, node_names: list[str], *, copy_to_clipboard: bool = True
) -> SerializeSelectedNodesToCommandsResultSuccess:
    request = SerializeSelectedNodesToCommandsRequest(
        nodes_to_serialize=[[name, "0"] for name in node_names],
        copy_to_clipboard=copy_to_clipboard,
    )
    result = engine.handle_request(request)
    assert isinstance(result, SerializeSelectedNodesToCommandsResultSuccess), result
    return result


def _decode_commands(serialized: str) -> SerializedSelectedNodesCommands:
    """Mirror the deserialize handler's own decoding: latin-1 bytes, then unpickle."""
    return pickle.loads(serialized.encode("latin1"))  # noqa: S301 test-only, mirrors production decode path


def _deserialize(
    engine: Engine,
    serialize_result: SerializeSelectedNodesToCommandsResultSuccess,
    *,
    positions: list[NewPosition] | None = None,
) -> DeserializeSelectedNodesFromCommandsResultSuccess:
    request = DeserializeSelectedNodesFromCommandsRequest(
        deserialize_commands=serialize_result.serialized_selected_node_commands,
        pickled_values=serialize_result.pickled_values,
        positions=positions,
    )
    result = engine.handle_request(request)
    assert isinstance(result, DeserializeSelectedNodesFromCommandsResultSuccess), result
    return result


def _get_value(engine: Engine, node_name: str) -> str:
    result = engine.handle_request(GetParameterValueRequest(node_name=node_name, parameter_name="value"))
    assert isinstance(result, GetParameterValueResultSuccess), result
    return result.value


def _set_value(engine: Engine, node_name: str, value: str) -> None:
    result = engine.handle_request(SetParameterValueRequest(node_name=node_name, parameter_name="value", value=value))
    assert result.succeeded(), result


@pytest.mark.usefixtures("flow_name")
class TestSerializedCommandsWireFormat:
    """The wire payload is a pickle blob transported as a latin-1 string, not JSON text.

    ``SerializedNodeCommands`` holds request dataclasses and enum-valued fields that are not
    all cattrs/JSON friendly, so the multi-node copy/paste path pickles the whole command tree
    instead of using the JSON path that single-node serialization uses. The ``str`` type on the
    dataclass field describes the wire shape (a string that survives inside a JSON envelope),
    not the payload's internal structure.
    """

    def test_result_field_decodes_into_the_commands_dataclass(self, engine: Engine) -> None:
        node_name = _create_node(engine, "NodeA")

        result = _serialize(engine, [node_name])

        assert isinstance(result.serialized_selected_node_commands, str)
        decoded = _decode_commands(result.serialized_selected_node_commands)
        assert isinstance(decoded, SerializedSelectedNodesCommands)

    def test_decoded_commands_contain_exactly_the_selected_node(self, engine: Engine) -> None:
        node_name = _create_node(engine, "NodeA")

        result = _serialize(engine, [node_name])
        decoded = _decode_commands(result.serialized_selected_node_commands)

        serialized_names = {cmd.create_node_command.node_name for cmd in decoded.serialized_node_commands}
        assert serialized_names == {node_name}

    def test_pickled_values_carries_the_value_pool_separately(self, engine: Engine) -> None:
        node_name = _create_node(engine, "NodeA")
        _set_value(engine, node_name, "hello")

        result = _serialize(engine, [node_name])
        decoded = _decode_commands(result.serialized_selected_node_commands)

        assert isinstance(result.pickled_values, dict)
        assert len(result.pickled_values) > 0
        param_commands = decoded.set_parameter_value_commands[decoded.serialized_node_commands[0].node_uuid]
        for indirect_command in param_commands:
            assert indirect_command.unique_value_uuid in result.pickled_values

    def test_node_names_serialized_lists_exactly_the_requested_nodes(self, engine: Engine) -> None:
        first = _create_node(engine, "NodeA")
        second = _create_node(engine, "NodeB")

        result = _serialize(engine, [first, second])

        assert result.node_names_serialized == [first, second]

    def test_copy_to_clipboard_false_still_returns_the_payload(self, engine: Engine) -> None:
        """The flag only controls an external clipboard side effect; the handler always returns commands."""
        node_name = _create_node(engine, "NodeA")

        with_clipboard = _serialize(engine, [node_name], copy_to_clipboard=True)
        without_clipboard = _serialize(engine, [node_name], copy_to_clipboard=False)

        decoded_with = _decode_commands(with_clipboard.serialized_selected_node_commands)
        decoded_without = _decode_commands(without_clipboard.serialized_selected_node_commands)
        assert len(decoded_with.serialized_node_commands) == len(decoded_without.serialized_node_commands)
        assert without_clipboard.node_names_serialized == [node_name]

    def test_unknown_node_name_fails_cleanly_and_copies_nothing(self, engine: Engine) -> None:
        """A request naming a node that cannot be found fails with the handler's own declared type.

        Callers pattern-matching on SerializeSelectedNodesToCommandsResultFailure must not miss this
        case, so the handler cannot fall back to the single-node SerializeNodeToCommandsResultFailure.
        """
        request = SerializeSelectedNodesToCommandsRequest(nodes_to_serialize=[["does_not_exist", "0"]])

        result = engine.handle_request(request)

        assert isinstance(result, SerializeSelectedNodesToCommandsResultFailure)


@pytest.mark.usefixtures("flow_name")
class TestSelectedNodesConnectionFiltering:
    """A copied selection can only carry edges whose both endpoints were copied.

    Pasting cannot invent a connection to a node that was never part of the selection, so a
    connection reaching outside the selected set must be silently dropped rather than causing
    a dangling reference during deserialization.
    """

    def test_connection_between_two_selected_nodes_is_preserved(self, engine: Engine) -> None:
        source = _create_node(engine, "NodeSource")
        target = _create_node(engine, "NodeTarget")
        _connect(engine, source, target)

        result = _serialize(engine, [source, target])
        decoded = _decode_commands(result.serialized_selected_node_commands)

        assert len(decoded.serialized_connection_commands) == 1
        connection = decoded.serialized_connection_commands[0]
        assert connection.source_parameter_name == "value"
        assert connection.target_parameter_name == "value"

    def test_connection_to_an_unselected_node_is_dropped(self, engine: Engine) -> None:
        source = _create_node(engine, "NodeSource")
        target = _create_node(engine, "NodeTarget")
        _connect(engine, source, target)

        # Only the source is selected for copy; target is left out of the selection entirely.
        result = _serialize(engine, [source])
        decoded = _decode_commands(result.serialized_selected_node_commands)

        assert decoded.serialized_connection_commands == []


@pytest.mark.usefixtures("flow_name")
class TestSelectedNodesDeserializationRebuildsGraph:
    """Rebuild an equivalent, independently-named copy of the selection.

    Deserializing the payload from ``on_serialize_selected_nodes_to_commands`` must produce the
    same values and the same wiring, under new node names.
    """

    def test_pasting_creates_fresh_names_and_leaves_originals_untouched(self, engine: Engine) -> None:
        node_name = _create_node(engine, "NodeA")
        _set_value(engine, node_name, "hello world")

        serialize_result = _serialize(engine, [node_name])
        deserialize_result = _deserialize(engine, serialize_result)

        assert len(deserialize_result.node_names) == 1
        pasted_name = deserialize_result.node_names[0]
        assert pasted_name != node_name
        assert _get_value(engine, node_name) == "hello world"
        assert _get_value(engine, pasted_name) == "hello world"

    def test_pasting_twice_from_the_same_payload_yields_independent_node_sets(self, engine: Engine) -> None:
        node_name = _create_node(engine, "NodeA")
        serialize_result = _serialize(engine, [node_name])

        first_paste = _deserialize(engine, serialize_result)
        second_paste = _deserialize(engine, serialize_result)

        assert first_paste.node_names != second_paste.node_names
        assert set(first_paste.node_names).isdisjoint(second_paste.node_names)

    def test_connection_between_pasted_nodes_is_recreated(self, engine: Engine) -> None:
        source = _create_node(engine, "NodeSource")
        target = _create_node(engine, "NodeTarget")
        _connect(engine, source, target)

        serialize_result = _serialize(engine, [source, target])
        deserialize_result = _deserialize(engine, serialize_result)

        expected_pasted_node_count = 2
        assert len(deserialize_result.node_names) == expected_pasted_node_count
        pasted_source, pasted_target = deserialize_result.node_names
        connections = engine.flow_manager.get_connections()
        outgoing = connections.outgoing_index.get(pasted_source, {})
        assert "value" in outgoing
        incoming = connections.incoming_index.get(pasted_target, {})
        assert "value" in incoming

    def test_lock_state_survives_the_round_trip(self, engine: Engine) -> None:
        node_name = _create_node(engine, "NodeA")
        lock_result = engine.handle_request(SetLockNodeStateRequest(node_name=node_name, lock=True))
        assert isinstance(lock_result, SetLockNodeStateResultSuccess), lock_result

        serialize_result = _serialize(engine, [node_name])
        deserialize_result = _deserialize(engine, serialize_result)

        pasted_name = deserialize_result.node_names[0]
        pasted_node = engine.node_manager.get_node_by_name(pasted_name)
        assert pasted_node.lock is True

    def test_positions_place_the_pasted_nodes(self, engine: Engine) -> None:
        node_name = _create_node(engine, "NodeA")
        serialize_result = _serialize(engine, [node_name])

        deserialize_result = _deserialize(engine, serialize_result, positions=[NewPosition(x=123.0, y=456.0)])

        pasted_name = deserialize_result.node_names[0]
        pasted_node = engine.node_manager.get_node_by_name(pasted_name)
        assert pasted_node.metadata["position"] == {"x": 123.0, "y": 456.0}

    def test_omitting_positions_does_not_crash_and_uses_defaults(self, engine: Engine) -> None:
        node_name = _create_node(engine, "NodeA")
        serialize_result = _serialize(engine, [node_name])

        deserialize_result = _deserialize(engine, serialize_result, positions=None)

        assert len(deserialize_result.node_names) == 1


@pytest.mark.usefixtures("flow_name")
class TestSelectedNodeGroupCopy:
    """Selecting a node group for copy/paste must bring its children along.

    A group is a visual container; copying only the container without its members would
    silently drop content the artist explicitly grouped together.
    """

    def test_selecting_a_group_copies_its_children(self, engine: Engine) -> None:
        group_name = _create_group(engine, "MyGroup")
        child_name = _create_node(engine, "ChildNode")
        add_result = engine.handle_request(
            AddNodesToNodeGroupRequest(node_names=[child_name], node_group_name=group_name)
        )
        assert isinstance(add_result, AddNodesToNodeGroupResultSuccess), add_result

        serialize_result = _serialize(engine, [group_name])
        decoded = _decode_commands(serialize_result.serialized_selected_node_commands)

        serialized_names = {cmd.create_node_command.node_name for cmd in decoded.serialized_node_commands}
        assert group_name in serialized_names
        assert child_name in serialized_names

    def test_pasting_a_copied_group_recreates_child_membership(self, engine: Engine) -> None:
        group_name = _create_group(engine, "MyGroup")
        child_name = _create_node(engine, "ChildNode")
        add_result = engine.handle_request(
            AddNodesToNodeGroupRequest(node_names=[child_name], node_group_name=group_name)
        )
        assert isinstance(add_result, AddNodesToNodeGroupResultSuccess), add_result

        serialize_result = _serialize(engine, [group_name])
        deserialize_result = _deserialize(engine, serialize_result)

        # Both the group and its child must have been recreated under fresh names.
        expected_pasted_node_count = 2
        assert len(deserialize_result.node_names) == expected_pasted_node_count
        assert len(deserialize_result.non_children_names) == 1
        pasted_group_name = deserialize_result.non_children_names[0]
        pasted_group = engine.node_manager.get_node_by_name(pasted_group_name)
        assert isinstance(pasted_group, BaseNodeGroup)
        assert len(pasted_group.nodes) == 1
