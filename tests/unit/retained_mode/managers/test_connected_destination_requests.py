"""NodeManager answers GetConnected...DestinationRequest from the settings node wired to a parameter."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from griptape_nodes.common.macro_parser import ParsedMacro
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode
from griptape_nodes.files.directory import DirectoryDestination
from griptape_nodes.files.file_sequence import FileSequenceDestination
from griptape_nodes.files.project_file import ProjectFileDestination
from griptape_nodes.node_library.library_registry import LibraryMetadata, LibraryRegistry, LibrarySchema, NodeMetadata
from griptape_nodes.retained_mode.events.connection_events import CreateConnectionRequest, CreateConnectionResultSuccess
from griptape_nodes.retained_mode.events.destination_events import (
    GetConnectedDirectoryDestinationRequest,
    GetConnectedDirectoryDestinationResultFailure,
    GetConnectedDirectoryDestinationResultSuccess,
    GetConnectedFileDestinationRequest,
    GetConnectedFileDestinationResultFailure,
    GetConnectedFileDestinationResultSuccess,
    GetConnectedFileSequenceDestinationRequest,
    GetConnectedFileSequenceDestinationResultFailure,
    GetConnectedFileSequenceDestinationResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.os_events import ExistingFilePolicy
from griptape_nodes.retained_mode.events.project_events import MacroPath

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.retained_mode.engine import Engine

_LIBRARY_NAME = "Connected Destination Library"
_MACRO = MacroPath(ParsedMacro("{outputs}/{name}.bin"), {"name": "render"})


class _SettingsNode(DataNode):
    """Supplies all three destinations, or none while ``configured`` is False."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.configured = True
        self.add_parameter(Parameter(name="destination", type="str", tooltip="", allowed_modes={ParameterMode.OUTPUT}))

    @property
    def file_destination(self) -> ProjectFileDestination | None:
        if not self.configured:
            return None
        return ProjectFileDestination(_MACRO, existing_file_policy=ExistingFilePolicy.FAIL, create_parents=False)

    @property
    def directory_destination(self) -> DirectoryDestination | None:
        if not self.configured:
            return None
        return DirectoryDestination(_MACRO, existing_dir_policy=ExistingFilePolicy.FAIL, create_parents=False)

    @property
    def file_sequence_destination(self) -> FileSequenceDestination | None:
        if not self.configured:
            return None
        return FileSequenceDestination(_MACRO, existing_file_policy=ExistingFilePolicy.FAIL, create_parents=False)

    def process(self) -> None:
        pass


class _PlainNode(DataNode):
    """Has a connectable output but supplies no destination."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(Parameter(name="destination", type="str", tooltip="", allowed_modes={ParameterMode.OUTPUT}))

    def process(self) -> None:
        pass


class _SaverNode(DataNode):
    """Has the output parameter a settings node is wired to, and one it is not."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        for parameter_name in ("output", "other"):
            self.add_parameter(
                Parameter(
                    name=parameter_name,
                    type="str",
                    input_types=["str"],
                    tooltip="",
                    allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                )
            )

    def process(self) -> None:
        pass


@pytest.fixture
def nodes(engine: Engine) -> Iterator[None]:
    """A "settings" node, a "plain" node and a "saver", none connected, in a fresh flow."""
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
    for node_class in (_SettingsNode, _PlainNode, _SaverNode):
        library.register_new_node_type(node_class, NodeMetadata(category="test", description="d", display_name="n"))
    engine.context_manager.push_workflow(workflow_name="connected_destination_wf")
    flow = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="main", set_as_new_context=True))
    assert isinstance(flow, CreateFlowResultSuccess), flow
    for node_type, name in (("_SettingsNode", "settings"), ("_PlainNode", "plain"), ("_SaverNode", "saver")):
        created = engine.handle_request(CreateNodeRequest(node_type=node_type, node_name=name))
        assert isinstance(created, CreateNodeResultSuccess), created
    try:
        yield
    finally:
        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        LibraryRegistry._clear()


def _connect(engine: Engine, source: str, target_parameter: str = "output") -> None:
    result = engine.handle_request(
        CreateConnectionRequest(
            source_node_name=source,
            source_parameter_name="destination",
            target_node_name="saver",
            target_parameter_name=target_parameter,
        )
    )
    assert isinstance(result, CreateConnectionResultSuccess), result


def _unconfigure_settings(engine: Engine) -> None:
    settings = engine.node_manager.get_node_by_name("settings")
    assert isinstance(settings, _SettingsNode)
    settings.configured = False


@pytest.mark.usefixtures("nodes")
class TestGetConnectedFileDestination:
    def test_nothing_connected_is_success_with_no_destination(self, engine: Engine) -> None:
        result = engine.handle_request(GetConnectedFileDestinationRequest(node_name="saver", parameter_name="output"))

        assert isinstance(result, GetConnectedFileDestinationResultSuccess), result
        assert result.destination is None

    def test_a_settings_node_on_another_parameter_is_not_this_ones(self, engine: Engine) -> None:
        _connect(engine, "settings", target_parameter="other")

        result = engine.handle_request(GetConnectedFileDestinationRequest(node_name="saver", parameter_name="output"))

        assert isinstance(result, GetConnectedFileDestinationResultSuccess), result
        assert result.destination is None

    def test_a_node_that_supplies_no_destination_is_skipped(self, engine: Engine) -> None:
        _connect(engine, "plain")

        result = engine.handle_request(GetConnectedFileDestinationRequest(node_name="saver", parameter_name="output"))

        assert isinstance(result, GetConnectedFileDestinationResultSuccess), result
        assert result.destination is None

    def test_returns_the_settings_nodes_destination_as_plain_data(self, engine: Engine) -> None:
        _connect(engine, "settings")

        result = engine.handle_request(GetConnectedFileDestinationRequest(node_name="saver", parameter_name="output"))

        assert isinstance(result, GetConnectedFileDestinationResultSuccess), result
        assert result.destination is not None
        assert result.destination.file_path == _MACRO
        assert result.destination.existing_file_policy == ExistingFilePolicy.FAIL
        assert result.destination.create_parents is False
        assert result.destination.map_to_project is True

    def test_a_settings_node_with_no_destination_set_fails_naming_it(self, engine: Engine) -> None:
        _connect(engine, "settings")
        _unconfigure_settings(engine)

        result = engine.handle_request(GetConnectedFileDestinationRequest(node_name="saver", parameter_name="output"))

        assert isinstance(result, GetConnectedFileDestinationResultFailure), result
        assert "'settings' has no destination set" in str(result.result_details)

    def test_an_unknown_node_fails_naming_it(self, engine: Engine) -> None:
        result = engine.handle_request(GetConnectedFileDestinationRequest(node_name="missing", parameter_name="output"))

        assert isinstance(result, GetConnectedFileDestinationResultFailure), result
        assert "'missing'" in str(result.result_details)

    def test_an_unknown_parameter_fails_naming_it(self, engine: Engine) -> None:
        result = engine.handle_request(GetConnectedFileDestinationRequest(node_name="saver", parameter_name="missing"))

        assert isinstance(result, GetConnectedFileDestinationResultFailure), result
        assert "'missing'" in str(result.result_details)


@pytest.mark.usefixtures("nodes")
class TestGetConnectedDirectoryDestination:
    def test_returns_the_settings_nodes_destination_as_plain_data(self, engine: Engine) -> None:
        _connect(engine, "settings")

        result = engine.handle_request(
            GetConnectedDirectoryDestinationRequest(node_name="saver", parameter_name="output")
        )

        assert isinstance(result, GetConnectedDirectoryDestinationResultSuccess), result
        assert result.destination is not None
        assert result.destination.dir_path == _MACRO
        assert result.destination.existing_dir_policy == ExistingFilePolicy.FAIL
        assert result.destination.create_parents is False

    def test_a_settings_node_with_no_destination_set_fails_naming_it(self, engine: Engine) -> None:
        _connect(engine, "settings")
        _unconfigure_settings(engine)

        result = engine.handle_request(
            GetConnectedDirectoryDestinationRequest(node_name="saver", parameter_name="output")
        )

        assert isinstance(result, GetConnectedDirectoryDestinationResultFailure), result
        assert "'settings' has no destination set" in str(result.result_details)


@pytest.mark.usefixtures("nodes")
class TestGetConnectedFileSequenceDestination:
    def test_returns_the_settings_nodes_destination_as_plain_data(self, engine: Engine) -> None:
        _connect(engine, "settings")

        result = engine.handle_request(
            GetConnectedFileSequenceDestinationRequest(node_name="saver", parameter_name="output")
        )

        assert isinstance(result, GetConnectedFileSequenceDestinationResultSuccess), result
        assert result.destination is not None
        assert result.destination.macro_path == _MACRO
        assert result.destination.existing_file_policy == ExistingFilePolicy.FAIL
        assert result.destination.create_parents is False

    def test_a_settings_node_with_no_destination_set_fails_naming_it(self, engine: Engine) -> None:
        _connect(engine, "settings")
        _unconfigure_settings(engine)

        result = engine.handle_request(
            GetConnectedFileSequenceDestinationRequest(node_name="saver", parameter_name="output")
        )

        assert isinstance(result, GetConnectedFileSequenceDestinationResultFailure), result
        assert "'settings' has no destination set" in str(result.result_details)
