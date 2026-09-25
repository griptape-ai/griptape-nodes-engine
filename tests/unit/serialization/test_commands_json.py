"""Flow and selection commands survive a trip through JSON text.

Image metadata and the copy/paste clipboard carry these command trees as JSON laid out by their
fields, so every field has to come back with its exact type, the data has to name no engine
classes, and the value pool has to stay encoded until deserialization decodes each value where it
is used.
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from griptape.artifacts import ImageUrlArtifact
from griptape.mixins.serializable_mixin import SerializableMixin
from griptape.rules import Rule, Ruleset
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from griptape_nodes.retained_mode.events.base_events import EventRequest
from griptape_nodes.retained_mode.events.connection_events import CreateConnectionRequest
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    DeserializeFlowFromCommandsRequest,
    DeserializeFlowFromCommandsResultSuccess,
    ExtractFlowCommandsFromImageMetadataRequest,
    ExtractFlowCommandsFromImageMetadataResultSuccess,
    SerializedFlowCommands,
    SerializeFlowToCommandsRequest,
    SerializeFlowToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    CreateNodeResultSuccess,
    DeserializeSelectedNodesFromCommandsRequest,
    DeserializeSelectedNodesFromCommandsResultFailure,
    DeserializeSelectedNodesFromCommandsResultSuccess,
    SerializedParameterValueTracker,
    SerializedSelectedNodesCommands,
    SerializeNodeToCommandsRequest,
    SerializeNodeToCommandsResultSuccess,
    SerializeSelectedNodesToCommandsRequest,
    SerializeSelectedNodesToCommandsResultFailure,
    SerializeSelectedNodesToCommandsResultSuccess,
    SetLockNodeStateRequest,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.parameter_events import AddParameterToNodeRequest, SetParameterValueRequest
from griptape_nodes.retained_mode.file_metadata.workflow_metadata import FLOW_COMMANDS_KEY, _serialize_flow
from griptape_nodes.serialization.commands import CommandsFormatError, decode_commands, encode_commands
from griptape_nodes.serialization.converter import dump_json
from griptape_nodes.serialization.values import TYPE_KEY, ValueEncodeError, is_plain_data

if TYPE_CHECKING:
    from griptape_nodes.exe_types.node_types import BaseNode
    from griptape_nodes.retained_mode.engine import Engine


class _Handle:
    """An object JSON has no form for, such as a node might keep in its metadata."""


def _values(node: BaseNode) -> dict[str, Any]:
    library_module = sys.modules[type(node).__module__]
    return {
        "items": [1, "two", 3.0],
        "int_keyed": {1: "one", 2: "two"},
        "pair": (1, "b"),
        "blob": b"\x00\x01\xff",
        "image": ImageUrlArtifact("https://example.com/cat.png", id="image-id", name="cat"),
        "ruleset": Ruleset(id="ruleset-id", name="style", rules=[Rule("Be concise")]),
        "mode": library_module.FixtureMode.SLOW,
        "custom_artifact": library_module.FixtureUrlArtifact("https://example.com/m.glb", id="m-id", name="m"),
        "mapping": {"tags": {"a", "b"}, "when": datetime.date(2024, 1, 2), "where": Path("out/x.png")},
        "ratio": float("inf"),
    }


def _create_node(engine: Engine, library_name: str, flow_name: str, node_name: str) -> BaseNode:
    result = engine.handle_request(
        CreateNodeRequest(
            node_type="LegacyValuesNode",
            specific_library_name=library_name,
            node_name=node_name,
            override_parent_flow_name=flow_name,
            metadata={"position": {"x": 1, "y": 2}},
        )
    )
    assert isinstance(result, CreateNodeResultSuccess), result
    node = engine.node_manager.get_node_by_name(result.node_name)
    for parameter_name, value in _values(node).items():
        set_result = engine.handle_request(
            SetParameterValueRequest(node_name=node.name, parameter_name=parameter_name, value=value)
        )
        assert set_result.succeeded(), set_result
    return node


def _create_child_flow(engine: Engine, parent_flow_name: str, flow_name: str) -> str:
    result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=parent_flow_name, flow_name=flow_name, set_as_new_context=False)
    )
    assert isinstance(result, CreateFlowResultSuccess), result
    return result.flow_name


def _through_json[T: SerializedFlowCommands | SerializedSelectedNodesCommands](commands: T) -> T:
    return decode_commands(json.loads(json.dumps(encode_commands(commands))), type(commands))


def _type_names(data: Any) -> list[str]:
    """Every class ``data`` names, wherever it sits."""
    if isinstance(data, list):
        return [name for item in data for name in _type_names(item)]
    if not isinstance(data, dict):
        return []
    names = [data[TYPE_KEY]] if isinstance(data.get(TYPE_KEY), str) else []
    return names + [name for item in data.values() for name in _type_names(item)]


def _assert_values_restored(node: BaseNode) -> None:
    for parameter_name, expected in _values(node).items():
        actual = node.parameter_values.get(parameter_name)
        assert type(actual) is type(expected), parameter_name
        if isinstance(expected, SerializableMixin):
            assert isinstance(actual, SerializableMixin)
            assert actual.to_dict() == expected.to_dict()
        else:
            assert actual == expected


@pytest.fixture
def flow_commands(engine: Engine, library_name: str, flow_name: str) -> SerializedFlowCommands:
    """Commands for a flow with a node in it and in each of two nested child flows."""
    top = _create_node(engine, library_name, flow_name, "Top")
    child_flow = _create_child_flow(engine, flow_name, "ChildFlow")
    child = _create_node(engine, library_name, child_flow, "Child")
    grandchild_flow = _create_child_flow(engine, child_flow, "GrandchildFlow")
    _create_node(engine, library_name, grandchild_flow, "Grandchild")
    engine.handle_request(
        CreateConnectionRequest(
            source_node_name=top.name,
            source_parameter_name="result",
            target_node_name=child.name,
            target_parameter_name="text",
        )
    )
    added = engine.handle_request(
        AddParameterToNodeRequest(
            node_name=top.name,
            parameter_name="speed",
            type="any",
            default_value=sys.modules[type(top).__module__].FixtureMode.FAST,
            tooltip="",
            is_user_defined=True,
        )
    )
    assert added.succeeded(), added
    engine.handle_request(SetLockNodeStateRequest(node_name=top.name, lock=True))

    result = engine.handle_request(SerializeFlowToCommandsRequest(flow_name=flow_name))
    assert isinstance(result, SerializeFlowToCommandsResultSuccess), result
    return result.serialized_flow_commands


class TestFlowCommandsJson:
    def test_flow_commands_come_back_equal(self, flow_commands: SerializedFlowCommands) -> None:
        assert _through_json(flow_commands) == flow_commands

    def test_nested_child_flows_come_back_as_commands(self, flow_commands: SerializedFlowCommands) -> None:
        restored = _through_json(flow_commands)

        (child,) = restored.sub_flows_commands
        (grandchild,) = child.sub_flows_commands
        assert type(grandchild) is SerializedFlowCommands
        assert grandchild == flow_commands.sub_flows_commands[0].sub_flows_commands[0]

    def test_value_pool_stays_encoded(self, flow_commands: SerializedFlowCommands) -> None:
        restored = _through_json(flow_commands)

        assert restored.unique_parameter_uuid_to_values == flow_commands.unique_parameter_uuid_to_values
        assert all(is_plain_data(value) for value in restored.unique_parameter_uuid_to_values.values())
        assert any(
            isinstance(value, dict) and TYPE_KEY in value for value in restored.unique_parameter_uuid_to_values.values()
        )

    def test_sets_and_named_tuples_keep_their_types(self, flow_commands: SerializedFlowCommands) -> None:
        restored = _through_json(flow_commands)

        assert type(restored.node_types_used) is set
        assert {type(entry) for entry in restored.node_types_used} == {
            type(entry) for entry in flow_commands.node_types_used
        }
        assert restored.node_dependencies == flow_commands.node_dependencies

    def test_parameter_defaults_keep_their_types(self, engine: Engine, flow_commands: SerializedFlowCommands) -> None:
        library_module = sys.modules[type(engine.node_manager.get_node_by_name("Top")).__module__]

        restored = _through_json(flow_commands)

        (added,) = [
            command
            for node_commands in restored.serialized_node_commands
            for command in node_commands.element_modification_commands
            if isinstance(command, AddParameterToNodeRequest)
        ]
        assert added.default_value is library_module.FixtureMode.FAST

    def test_restored_commands_rebuild_the_flow(self, engine: Engine, flow_commands: SerializedFlowCommands) -> None:
        text = json.dumps(encode_commands(flow_commands))
        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        engine.context_manager.push_workflow(workflow_name="restored")

        result = engine.handle_request(
            DeserializeFlowFromCommandsRequest(
                serialized_flow_commands=decode_commands(json.loads(text), SerializedFlowCommands)
            )
        )

        assert isinstance(result, DeserializeFlowFromCommandsResultSuccess), result
        for original_name in ("Top", "Child", "Grandchild"):
            _assert_values_restored(engine.node_manager.get_node_by_name(result.node_name_mappings[original_name]))


class TestSelectedNodesCommandsJson:
    @pytest.fixture
    def selection(self, engine: Engine, library_name: str, flow_name: str) -> SerializedSelectedNodesCommands:
        pool: dict[Any, Any] = {}
        tracker = SerializedParameterValueTracker()
        node_commands = []
        parameter_commands = {}
        lock_commands = {}
        for node_name in ("A", "B"):
            node = _create_node(engine, library_name, flow_name, node_name)
            result = engine.handle_request(
                SerializeNodeToCommandsRequest(
                    node_name=node.name,
                    unique_parameter_uuid_to_values=pool,
                    serialized_parameter_value_tracker=tracker,
                )
            )
            assert isinstance(result, SerializeNodeToCommandsResultSuccess), result
            node_uuid = result.serialized_node_commands.node_uuid
            node_commands.append(result.serialized_node_commands)
            parameter_commands[node_uuid] = result.set_parameter_value_commands
            lock_commands[node_uuid] = result.serialized_node_commands.lock_node_command
        return SerializedSelectedNodesCommands(
            serialized_node_commands=node_commands,
            set_parameter_value_commands=parameter_commands,
            set_lock_commands_per_node=lock_commands,
            serialized_connection_commands=[
                SerializedSelectedNodesCommands.IndirectConnectionSerialization(
                    source_node_uuid=node_commands[0].node_uuid,
                    source_parameter_name="result",
                    target_node_uuid=node_commands[1].node_uuid,
                    target_parameter_name="text",
                )
            ],
        )

    def test_selection_commands_come_back_equal(self, selection: SerializedSelectedNodesCommands) -> None:
        assert _through_json(selection) == selection

    def test_connections_keep_their_nested_class(self, selection: SerializedSelectedNodesCommands) -> None:
        restored = _through_json(selection)

        (connection,) = restored.serialized_connection_commands
        assert type(connection) is SerializedSelectedNodesCommands.IndirectConnectionSerialization


class TestLayout:
    def test_commands_name_no_engine_classes(self, flow_commands: SerializedFlowCommands) -> None:
        names = _type_names(encode_commands(flow_commands))

        # Values still name their class, the fixture library's under its stable namespace included.
        engine_names = [
            name
            for name in names
            if name.startswith("griptape_nodes.") and not name.startswith("griptape_nodes.node_libraries.")
        ]
        assert names
        assert not engine_names

    def test_mixed_requests_carry_their_request_name(self, flow_commands: SerializedFlowCommands) -> None:
        encoded = json.loads(json.dumps(encode_commands(flow_commands)))["commands"]

        (added,) = [
            command
            for node_commands in encoded["serialized_node_commands"]
            for command in node_commands["element_modification_commands"]
            if command["request_type"] == "AddParameterToNodeRequest"
        ]
        assert added["request"]["parameter_name"] == "speed"

    def test_unknown_request_name_fails_to_read(self, flow_commands: SerializedFlowCommands) -> None:
        encoded = json.loads(json.dumps(encode_commands(flow_commands)))
        for node_commands in encoded["commands"]["serialized_node_commands"]:
            for command in node_commands["element_modification_commands"]:
                command["request_type"] = "NoSuchRequest"

        with pytest.raises(CommandsFormatError, match="incomplete or damaged"):
            decode_commands(encoded, SerializedFlowCommands)

    def test_data_from_a_later_version_fails_to_read(self, flow_commands: SerializedFlowCommands) -> None:
        encoded = {**encode_commands(flow_commands), "version": 2}

        with pytest.raises(CommandsFormatError, match="later version"):
            decode_commands(encoded, SerializedFlowCommands)

    def test_value_without_plain_data_form_fails_the_encode(self, flow_commands: SerializedFlowCommands) -> None:
        (added,) = [
            command
            for node_commands in flow_commands.serialized_node_commands
            for command in node_commands.element_modification_commands
            if isinstance(command, AddParameterToNodeRequest)
        ]
        added.default_value = object()

        with pytest.raises(ValueEncodeError, match="no plain-data form"):
            encode_commands(flow_commands)

    def test_griptape_object_in_an_untyped_field_fails_the_encode(self, flow_commands: SerializedFlowCommands) -> None:
        create_node = flow_commands.serialized_node_commands[0].create_node_command
        create_node.metadata = {**(create_node.metadata or {}), "image": ImageUrlArtifact("https://example.com/c.png")}

        with pytest.raises(ValueEncodeError, match="'ImageUrlArtifact' value"):
            encode_commands(flow_commands)

    def test_object_in_an_untyped_field_fails_the_write(self, flow_commands: SerializedFlowCommands) -> None:
        create_node = flow_commands.serialized_node_commands[0].create_node_command
        create_node.metadata = {**(create_node.metadata or {}), "handle": _Handle()}

        with pytest.raises(ValueEncodeError, match="'_Handle' value has no plain-data form"):
            dump_json(encode_commands(flow_commands))

    def test_commands_sent_in_a_request_come_back_whole(self, flow_commands: SerializedFlowCommands) -> None:
        event = EventRequest(request=DeserializeFlowFromCommandsRequest(serialized_flow_commands=flow_commands))

        restored = EventRequest.from_dict(json.loads(event.json()))

        assert isinstance(restored.request, DeserializeFlowFromCommandsRequest)
        assert restored.request.serialized_flow_commands == flow_commands


class TestCopyPaste:
    def _copy(self, engine: Engine, node_name: str) -> SerializeSelectedNodesToCommandsResultSuccess:
        result = engine.handle_request(SerializeSelectedNodesToCommandsRequest(nodes_to_serialize=[[node_name, "0"]]))
        assert isinstance(result, SerializeSelectedNodesToCommandsResultSuccess), result
        return result

    def _paste(self, engine: Engine, copied: SerializeSelectedNodesToCommandsResultSuccess) -> BaseNode:
        result = engine.handle_request(
            DeserializeSelectedNodesFromCommandsRequest(
                deserialize_commands=copied.serialized_selected_node_commands,
                pickled_values=copied.pickled_values,
            )
        )
        assert isinstance(result, DeserializeSelectedNodesFromCommandsResultSuccess), result
        return engine.node_manager.get_node_by_name(result.node_names[0])

    def test_copied_nodes_and_values_are_json(self, engine: Engine, library_name: str, flow_name: str) -> None:
        copied = self._copy(engine, _create_node(engine, library_name, flow_name, "A").name)

        commands = decode_commands(
            json.loads(copied.serialized_selected_node_commands), SerializedSelectedNodesCommands
        )
        assert type(commands) is SerializedSelectedNodesCommands
        assert all(is_plain_data(json.loads(text)) for text in copied.pickled_values.values())

    def test_pasted_values_keep_their_types(self, engine: Engine, library_name: str, flow_name: str) -> None:
        copied = self._copy(engine, _create_node(engine, library_name, flow_name, "A").name)

        _assert_values_restored(self._paste(engine, copied))

    def test_each_paste_gets_its_own_values(self, engine: Engine, library_name: str, flow_name: str) -> None:
        copied = self._copy(engine, _create_node(engine, library_name, flow_name, "A").name)

        first = self._paste(engine, copied)
        second = self._paste(engine, copied)

        assert first.parameter_values["items"] == second.parameter_values["items"]
        assert first.parameter_values["items"] is not second.parameter_values["items"]

    def test_node_holding_an_object_with_no_json_form_fails_the_copy(
        self, engine: Engine, library_name: str, flow_name: str
    ) -> None:
        node = _create_node(engine, library_name, flow_name, "A")
        node.metadata["handle"] = _Handle()

        result = engine.handle_request(SerializeSelectedNodesToCommandsRequest(nodes_to_serialize=[[node.name, "0"]]))

        assert isinstance(result, SerializeSelectedNodesToCommandsResultFailure)
        assert "'_Handle' value has no plain-data form" in str(result.result_details)

    @pytest.mark.usefixtures("flow_name")
    def test_unreadable_copied_nodes_fail_the_paste(self, engine: Engine) -> None:
        result = engine.handle_request(
            DeserializeSelectedNodesFromCommandsRequest(deserialize_commands='{"not": "nodes"}', pickled_values={})
        )

        assert isinstance(result, DeserializeSelectedNodesFromCommandsResultFailure)
        assert "not in a layout Griptape Nodes writes" in str(result.result_details)


class TestImageMetadata:
    def test_image_restores_values_with_their_types(
        self, engine: Engine, library_name: str, flow_name: str, tmp_path: Path
    ) -> None:
        _create_node(engine, library_name, flow_name, "A")
        flow_commands_text = _serialize_flow(engine, flow_name)
        assert flow_commands_text is not None
        info = PngInfo()
        info.add_text(FLOW_COMMANDS_KEY, flow_commands_text)
        image_path = tmp_path / "workflow.png"
        Image.new("RGB", (4, 4)).save(image_path, format="PNG", pnginfo=info)
        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        engine.context_manager.push_workflow(workflow_name="restored")
        engine.handle_request(CreateFlowRequest(parent_flow_name=None, set_as_new_context=True))

        result = engine.handle_request(
            ExtractFlowCommandsFromImageMetadataRequest(file_url_or_path=str(image_path), deserialize=True)
        )

        assert isinstance(result, ExtractFlowCommandsFromImageMetadataResultSuccess), result
        _assert_values_restored(engine.node_manager.get_node_by_name(result.node_name_mappings["A"]))

    def test_flow_holding_an_object_with_no_json_form_is_not_embedded(
        self, engine: Engine, library_name: str, flow_name: str
    ) -> None:
        _create_node(engine, library_name, flow_name, "A").metadata["handle"] = _Handle()

        assert _serialize_flow(engine, flow_name) is None
