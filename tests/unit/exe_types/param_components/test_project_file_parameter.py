"""Unit tests for ProjectFileParameter: build_file reading a connected settings node, and attested producing-node identity."""

from collections.abc import Generator
from typing import cast
from unittest import mock

import pytest

from griptape_nodes.common.macro_parser import ParsedMacro
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode
from griptape_nodes.exe_types.param_components.project_file_parameter import ProjectFileParameter
from griptape_nodes.files.project_file import ProjectFileDestination
from griptape_nodes.node_library.library_registry import (
    LibraryMetadata,
    LibraryRegistry,
    LibrarySchema,
    NodeMetadata,
)
from griptape_nodes.retained_mode.events import destination_events
from griptape_nodes.retained_mode.events.os_events import ExistingFilePolicy
from griptape_nodes.retained_mode.events.project_events import MacroPath

_SPEC = destination_events.FileDestinationSpec(
    file_path=MacroPath(ParsedMacro("{outputs}/{name}.png"), {"name": "render"}),
    existing_file_policy=ExistingFilePolicy.FAIL,
    append=False,
    create_parents=False,
    coerce_extension_to_match_bytes=False,
    file_metadata=None,
    map_to_project=True,
)


def _make_param(result: object) -> ProjectFileParameter:
    node = mock.MagicMock()
    node.name = "Saver"
    node._handle_request_in_workflow.return_value = result
    return ProjectFileParameter(node, "output_file", default_filename="default.png")


class TestBuildFileFromConnectedSettings:
    def test_asks_for_the_destination_connected_to_this_parameter(self) -> None:
        param = _make_param(
            destination_events.GetConnectedFileDestinationResultSuccess(destination=_SPEC, result_details="ok")
        )

        param.build_file()

        cast("mock.MagicMock", param._node)._handle_request_in_workflow.assert_called_once_with(
            destination_events.GetConnectedFileDestinationRequest(node_name="Saver", parameter_name="output_file")
        )

    def test_returns_the_settings_nodes_destination(self) -> None:
        param = _make_param(
            destination_events.GetConnectedFileDestinationResultSuccess(destination=_SPEC, result_details="ok")
        )

        destination = param.build_file()

        assert isinstance(destination, ProjectFileDestination)
        assert destination.to_spec() == _SPEC

    def test_a_failed_read_raises(self) -> None:
        param = _make_param(
            destination_events.GetConnectedFileDestinationResultFailure(
                result_details="'Settings' has no destination set"
            )
        )

        with pytest.raises(ValueError, match="has no destination set"):
            param.build_file()


class _SaverNode(DataNode):
    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="output_path",
                tooltip="Where to save",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )

    def process(self) -> None:
        pass


def _component_for(node: _SaverNode) -> ProjectFileParameter:
    return ProjectFileParameter(node, "output_path", default_filename="out.png")


@pytest.fixture
def registered_library() -> Generator[str, None, None]:
    """Register _SaverNode in a tiny library so the registry is the identity authority."""
    library_name = "project-file-parameter-test-library"
    LibraryRegistry._clear()
    schema = LibrarySchema(
        name=library_name,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(author="t", description="d", library_version="0.9.1", engine_version="0.0.0", tags=[]),
        categories=[],
        nodes=[],
    )
    library = LibraryRegistry.generate_new_library(schema)
    library.register_new_node_type(_SaverNode, NodeMetadata(category="t", description="d", display_name="Saver"))
    yield library_name
    LibraryRegistry._clear()


class TestProducingNodeIdentity:
    def test_registered_node_gets_authoritative_library_name_and_version(self, registered_library: str) -> None:
        # The metadata "library" entry is only a disambiguation hint; the
        # registry answers with the authoritative name and version.
        node = _SaverNode("Saver_1", metadata={"library": registered_library})
        identity = _component_for(node)._producing_node_identity()
        assert identity.name == "Saver_1"
        assert identity.node_type == "_SaverNode"
        assert identity.library_name == registered_library
        assert identity.library_version == "0.9.1"

    def test_unregistered_node_keeps_the_hint_without_a_version(self) -> None:
        LibraryRegistry._clear()
        node = _SaverNode("Saver_2", metadata={"library": "Some Hint Library"})
        identity = _component_for(node)._producing_node_identity()
        assert identity.name == "Saver_2"
        assert identity.node_type == "_SaverNode"
        assert identity.library_name == "Some Hint Library"
        assert identity.library_version is None
