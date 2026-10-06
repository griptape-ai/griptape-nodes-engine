"""The pickle-era reader builds saved values and nothing else."""

from __future__ import annotations

import base64
import pickle
import sys
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

import pytest
from PIL import Image

from griptape_nodes.exe_types.node_types import NodeDependencies
from griptape_nodes.retained_mode.events.arbitrary_python_events import RunArbitraryPythonStringRequest
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, SerializedFlowCommands
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    DeserializeSelectedNodesFromCommandsRequest,
    SerializedNodeCommands,
    SerializedSelectedNodesCommands,
)
from griptape_nodes.retained_mode.file_metadata.workflow_metadata import FLOW_COMMANDS_KEY
from griptape_nodes.serialization.legacy_pickle import (
    LegacyPickleError,
    load_legacy_pickle,
    read_legacy_clipboard_commands,
    read_legacy_image_flow_commands,
)
from griptape_nodes.serialization.values import is_plain_data
from tests.unit.serialization.conftest import FIXTURES

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine

_FIXTURE_MODULE = "griptape_nodes.node_libraries.pickle_era_fixture_library.legacy_values_node"


def _call(module: str, name: str, argument: str) -> bytes:
    """A protocol-0 pickle that calls ``module.name(argument)``."""
    return f"c{module}\n{name}\n(S'{argument}'\ntR.".encode()


class TestRestrictedUnpickling:
    def test_builds_a_value_class(self) -> None:
        assert load_legacy_pickle(_call("pathlib", "PurePosixPath", "a/b"), ()) == PurePosixPath("a/b")

    def test_refuses_a_function(self) -> None:
        with pytest.raises(LegacyPickleError, match="not a class"):
            load_legacy_pickle(_call("os", "system", "echo hi"), ())

    def test_refuses_a_class_that_is_not_a_saved_value(self) -> None:
        with pytest.raises(LegacyPickleError, match="not a type of saved value"):
            load_legacy_pickle(_call("subprocess", "Popen", "ls"), ())

    def test_does_not_import_a_module_to_look_for_a_class(self) -> None:
        assert "this" not in sys.modules

        with pytest.raises(LegacyPickleError, match="not loaded"):
            load_legacy_pickle(_call("this", "Anything", "x"), ())

        assert "this" not in sys.modules

    def test_refuses_data_that_is_not_a_pickle(self) -> None:
        with pytest.raises(LegacyPickleError, match="not readable"):
            load_legacy_pickle(b"not a pickle", ())


@pytest.mark.usefixtures("library_name")
class TestLibraryModuleNames:
    """Library classes pickled under another process's ``gtn_dynamic_module_*`` name."""

    _DYNAMIC_MODULE = "gtn_dynamic_module_legacy_values_node_py_-123"

    def test_finds_the_library_file_by_name(self) -> None:
        value = load_legacy_pickle(_call(self._DYNAMIC_MODULE, "FixtureMode", "slow"), [_FIXTURE_MODULE])

        assert value is sys.modules[_FIXTURE_MODULE].FixtureMode.SLOW

    def test_refuses_a_name_no_registered_library_has(self) -> None:
        with pytest.raises(LegacyPickleError, match="no registered library has"):
            load_legacy_pickle(_call(self._DYNAMIC_MODULE, "FixtureMode", "slow"), [])

    def test_refuses_a_name_more_than_one_registered_library_has(self) -> None:
        library_modules = [_FIXTURE_MODULE, "griptape_nodes.node_libraries.other_library.legacy_values_node"]

        with pytest.raises(LegacyPickleError, match="more than one"):
            load_legacy_pickle(_call(self._DYNAMIC_MODULE, "FixtureMode", "slow"), library_modules)


class TestLegacyPayloads:
    @pytest.mark.usefixtures("library_name")
    def test_image_commands_come_back_with_their_value_pool_encoded(self, engine: Engine) -> None:
        text = Image.open(FIXTURES / "pickle_era_image_library_values.png").info[FLOW_COMMANDS_KEY]

        commands = read_legacy_image_flow_commands(text, engine.library_manager.module_loading.stable_module_names())

        assert commands.unique_parameter_uuid_to_values
        assert all(is_plain_data(value) for value in commands.unique_parameter_uuid_to_values.values())

    def test_image_payload_that_is_not_flow_commands_is_refused(self) -> None:
        text = base64.b64encode(_call("pathlib", "PurePosixPath", "a")).decode("ascii")

        with pytest.raises(LegacyPickleError, match="not flow commands"):
            read_legacy_image_flow_commands(text, ())

    def test_clipboard_payload_that_is_not_node_commands_is_refused(self) -> None:
        text = _call("pathlib", "PurePosixPath", "a").decode("latin-1")

        with pytest.raises(LegacyPickleError, match="not copied nodes"):
            read_legacy_clipboard_commands(text, ())


def _node_commands_running_arbitrary_python() -> SerializedNodeCommands:
    """A node's commands holding an element command serialization never writes."""
    return SerializedNodeCommands(
        create_node_command=CreateNodeRequest(node_type="Holder"),
        element_modification_commands=[RunArbitraryPythonStringRequest(python_string="import os")],
        node_dependencies=NodeDependencies(),
    )


class TestElementCommandAllowlist:
    """An element command naming a request serialization never writes is refused, not run."""

    def test_image_payload_with_disallowed_element_command_is_refused(self) -> None:
        commands = SerializedFlowCommands(
            flow_initialization_command=None,
            serialized_node_commands=[_node_commands_running_arbitrary_python()],
            serialized_connections=[],
            unique_parameter_uuid_to_values={},
            set_parameter_value_commands={},
            set_lock_commands_per_node={},
            sub_flows_commands=[],
            node_dependencies=NodeDependencies(),
            node_types_used=set(),
        )
        text = base64.b64encode(pickle.dumps(commands)).decode("ascii")

        with pytest.raises(LegacyPickleError, match="RunArbitraryPythonStringRequest"):
            read_legacy_image_flow_commands(text, ())

    def test_clipboard_payload_with_disallowed_element_command_is_refused(self) -> None:
        commands = SerializedSelectedNodesCommands(
            serialized_node_commands=[_node_commands_running_arbitrary_python()],
            set_parameter_value_commands={},
            set_lock_commands_per_node={},
            serialized_connection_commands=[],
        )
        text = pickle.dumps(commands).decode("latin-1")

        with pytest.raises(LegacyPickleError, match="RunArbitraryPythonStringRequest"):
            read_legacy_clipboard_commands(text, ())


def _smuggled_selected_nodes(**overrides: object) -> SerializedSelectedNodesCommands:
    node = SerializedNodeCommands(
        create_node_command=CreateNodeRequest(node_type="Holder"),
        element_modification_commands=[],
        node_dependencies=NodeDependencies(),
        node_uuid=SerializedNodeCommands.NodeUUID("node-1"),
    )
    fields: dict[str, object] = {
        "serialized_node_commands": [node],
        "set_parameter_value_commands": {"node-1": []},
        "set_lock_commands_per_node": {"node-1": None},
        "serialized_connection_commands": [],
    }
    fields.update(overrides)
    return SerializedSelectedNodesCommands(**fields)  # type: ignore[arg-type]


class TestCommandFieldTypes:
    """A request in a field declared for another request type is refused, not run.

    Pickle ignores declared field types, so each request-bearing field is checked after reading.
    """

    def test_request_in_the_lock_slot_is_refused(self) -> None:
        commands = _smuggled_selected_nodes(
            set_lock_commands_per_node={"node-1": RunArbitraryPythonStringRequest(python_string="import os")}
        )
        text = pickle.dumps(commands).decode("latin-1")

        with pytest.raises(LegacyPickleError, match="RunArbitraryPythonStringRequest"):
            read_legacy_clipboard_commands(text, ())

    def test_request_in_a_parameter_value_slot_is_refused(self) -> None:
        smuggled = SerializedNodeCommands.IndirectSetParameterValueCommand(
            set_parameter_value_command=RunArbitraryPythonStringRequest(python_string="import os"),  # type: ignore[arg-type]
            unique_value_uuid=SerializedNodeCommands.UniqueParameterValueUUID("value-1"),
        )
        commands = _smuggled_selected_nodes(set_parameter_value_commands={"node-1": [smuggled]})
        text = pickle.dumps(commands).decode("latin-1")

        with pytest.raises(LegacyPickleError, match="RunArbitraryPythonStringRequest"):
            read_legacy_clipboard_commands(text, ())

    def test_request_in_the_create_node_slot_is_refused(self) -> None:
        node = SerializedNodeCommands(
            create_node_command=RunArbitraryPythonStringRequest(python_string="import os"),  # type: ignore[arg-type]
            element_modification_commands=[],
            node_dependencies=NodeDependencies(),
        )
        commands = _smuggled_selected_nodes(serialized_node_commands=[node])
        text = pickle.dumps(commands).decode("latin-1")

        with pytest.raises(LegacyPickleError, match="RunArbitraryPythonStringRequest"):
            read_legacy_clipboard_commands(text, ())

    def test_request_in_the_flow_initialization_slot_is_refused(self) -> None:
        commands = SerializedFlowCommands(
            flow_initialization_command=RunArbitraryPythonStringRequest(python_string="import os"),  # type: ignore[arg-type]
            serialized_node_commands=[],
            serialized_connections=[],
            unique_parameter_uuid_to_values={},
            set_parameter_value_commands={},
            set_lock_commands_per_node={},
            sub_flows_commands=[],
            node_dependencies=NodeDependencies(),
            node_types_used=set(),
        )
        text = base64.b64encode(pickle.dumps(commands)).decode("ascii")

        with pytest.raises(LegacyPickleError, match="RunArbitraryPythonStringRequest"):
            read_legacy_image_flow_commands(text, ())

    def test_paste_of_a_smuggled_lock_request_fails_without_running_it(
        self,
        engine: Engine,
        library_name: str,
        flow_name: str,  # noqa: ARG002
    ) -> None:
        node = SerializedNodeCommands(
            create_node_command=CreateNodeRequest(node_type="LegacyValuesNode", specific_library_name=library_name),
            element_modification_commands=[],
            node_dependencies=NodeDependencies(),
            node_uuid=SerializedNodeCommands.NodeUUID("node-1"),
        )
        commands = _smuggled_selected_nodes(
            serialized_node_commands=[node],
            set_lock_commands_per_node={"node-1": CreateFlowRequest(flow_name="Smuggled", parent_flow_name=None)},
        )
        result = engine.handle_request(
            DeserializeSelectedNodesFromCommandsRequest(
                deserialize_commands=pickle.dumps(commands).decode("latin-1"), pickled_values={}
            )
        )

        assert not result.succeeded()
        assert engine.object_manager.attempt_get_object_by_name("Smuggled") is None
