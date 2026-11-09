"""Packaging a group carries in a copy of the output settings wired to it from outside.

An Isolated Execution group runs from a packaged copy of its nodes, in a process that has nothing
else from the canvas. A saving node finds its destination by following its connection to a
destination provider such as File Output Settings, so a provider outside the group has to travel
with the package, wired straight to the node it feeds.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    OriginalNodeParameter,
    PackageNodesAsSerializedFlowRequest,
    PackageNodesAsSerializedFlowResultSuccess,
    SerializedFlowCommands,
)
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import (
    AddNodesToNodeGroupRequest,
    AddNodesToNodeGroupResultSuccess,
    CreateNodeRequest,
    CreateNodeResultSuccess,
    SerializedNodeCommands,
)
from griptape_nodes.retained_mode.events.parameter_events import (
    SetParameterValueRequest,
    SetParameterValueResultSuccess,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.engine import Engine

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "output_settings_packaging_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "output_settings_packaging_nodes.py"
# The packager resolves its StartFlow/EndFlow endpoints from a library with exactly this name.
LIBRARY_NAME = "Griptape Nodes Library"

GROUP = "Group"
SAVER = "Saver"
SETTINGS = "Settings"
PLAIN = "Plain"
FILENAME = "/renders/out.png"


def _create(engine: Engine, flow_name: str, node_type: str, node_name: str) -> None:
    with engine.context_manager.flow(flow_name):
        result = engine.handle_request(
            CreateNodeRequest(node_type=node_type, specific_library_name=LIBRARY_NAME, node_name=node_name)
        )
    assert isinstance(result, CreateNodeResultSuccess), result


def _connect(engine: Engine, source_node: str, source_param: str, target_node: str, target_param: str) -> None:
    result = engine.handle_request(
        CreateConnectionRequest(
            source_node_name=source_node,
            source_parameter_name=source_param,
            target_node_name=target_node,
            target_parameter_name=target_param,
        )
    )
    assert isinstance(result, CreateConnectionResultSuccess), (
        f"{source_node}.{source_param} -> {target_node}.{target_param}: {result}"
    )


def _group(engine: Engine, member_names: list[str]) -> None:
    result = engine.handle_request(AddNodesToNodeGroupRequest(node_names=member_names, node_group_name=GROUP))
    assert isinstance(result, AddNodesToNodeGroupResultSuccess), result


def _node_uuid(commands: SerializedFlowCommands, node_name: str) -> SerializedNodeCommands.NodeUUID:
    for node_commands in commands.serialized_node_commands:
        if node_commands.create_node_command.node_name == node_name:
            return node_commands.node_uuid
    msg = f"no serialized node named {node_name!r}"
    raise AssertionError(msg)


def _connections(commands: SerializedFlowCommands) -> set[tuple[str, str, str, str]]:
    return {
        (
            connection.source_node_uuid,
            connection.source_parameter_name,
            connection.target_node_uuid,
            connection.target_parameter_name,
        )
        for connection in commands.serialized_connections
    }


def _start_inputs(result: PackageNodesAsSerializedFlowResultSuccess) -> set[OriginalNodeParameter]:
    return set(result.parameter_name_mappings[0].parameter_mappings.values())


@pytest.fixture
def flow_name(tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path]) -> str:
    """A canvas with Settings and Plain outside `Group`, both wired into Saver inside it."""
    library_json = materialize_library(
        tmp_path / "library", template=FIXTURE_LIBRARY_JSON_TEMPLATE, node_file=FIXTURE_NODE_FILE, name=LIBRARY_NAME
    )
    registered = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(registered, RegisterLibraryFromFileResultSuccess), registered
    engine.context_manager.push_workflow(workflow_name="output_settings_packaging")
    flow = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="Parent", set_as_new_context=False))
    assert isinstance(flow, CreateFlowResultSuccess), flow

    for node_type, node_name in (
        ("GroupNode", GROUP),
        ("SaverNode", SAVER),
        ("SettingsNode", SETTINGS),
        ("PlainNode", PLAIN),
    ):
        _create(engine, flow.flow_name, node_type, node_name)
    filename = engine.handle_request(
        SetParameterValueRequest(node_name=SETTINGS, parameter_name="filename", value=FILENAME)
    )
    assert isinstance(filename, SetParameterValueResultSuccess), filename
    _connect(engine, SETTINGS, "file_destination", SAVER, "output")
    _connect(engine, PLAIN, "text", SAVER, "note")
    return flow.flow_name


def _package(engine: Engine, flow_name: str) -> PackageNodesAsSerializedFlowResultSuccess:
    with engine.context_manager.flow(flow_name):
        result = engine.handle_request(PackageNodesAsSerializedFlowRequest(node_names=[SAVER], node_group_name=GROUP))
    assert isinstance(result, PackageNodesAsSerializedFlowResultSuccess), result
    return result


class TestASettingsNodeOutsideTheGroup:
    def test_travels_with_the_package_wired_straight_to_the_saver(self, engine: Engine, flow_name: str) -> None:
        _group(engine, [SAVER])

        result = _package(engine, flow_name)
        commands = result.serialized_flow_commands

        settings_uuid = _node_uuid(commands, SETTINGS)
        saver_uuid = _node_uuid(commands, SAVER)
        assert (settings_uuid, "file_destination", saver_uuid, "output") in _connections(commands)
        assert OriginalNodeParameter(SAVER, "output") not in _start_inputs(result)

    def test_keeps_its_settings(self, engine: Engine, flow_name: str) -> None:
        _group(engine, [SAVER])

        commands = _package(engine, flow_name).serialized_flow_commands

        settings_uuid = _node_uuid(commands, SETTINGS)
        values = {
            command.set_parameter_value_command.parameter_name: commands.unique_parameter_uuid_to_values[
                command.unique_value_uuid
            ]
            for command in commands.set_parameter_value_commands.get(settings_uuid, [])
            if not command.set_parameter_value_command.is_output
        }
        assert values["filename"] == FILENAME

    def test_is_a_copy_not_a_packaged_node(self, engine: Engine, flow_name: str) -> None:
        """Nothing from the run is written back onto the original settings node."""
        _group(engine, [SAVER])

        result = _package(engine, flow_name)

        assert result.packaged_node_names == [SAVER]
        end_outputs = set(result.parameter_name_mappings[1].parameter_mappings.values())
        assert all(output.node_name != SETTINGS for output in end_outputs)

    def test_an_ordinary_input_still_comes_through_the_start_node(self, engine: Engine, flow_name: str) -> None:
        _group(engine, [SAVER])

        result = _package(engine, flow_name)

        assert OriginalNodeParameter(SAVER, "note") in _start_inputs(result)
        with pytest.raises(AssertionError):
            _node_uuid(result.serialized_flow_commands, PLAIN)


class TestASettingsNodeInsideTheGroup:
    def test_is_packaged_as_a_member_and_not_copied_again(self, engine: Engine, flow_name: str) -> None:
        _group(engine, [SAVER, SETTINGS])

        with engine.context_manager.flow(flow_name):
            result = engine.handle_request(
                PackageNodesAsSerializedFlowRequest(node_names=[SAVER, SETTINGS], node_group_name=GROUP)
            )
        assert isinstance(result, PackageNodesAsSerializedFlowResultSuccess), result
        commands = result.serialized_flow_commands

        settings_nodes = [
            node for node in commands.serialized_node_commands if node.create_node_command.node_name == SETTINGS
        ]
        assert len(settings_nodes) == 1
        settings_uuid = settings_nodes[0].node_uuid
        assert (settings_uuid, "file_destination", _node_uuid(commands, SAVER), "output") in _connections(commands)
