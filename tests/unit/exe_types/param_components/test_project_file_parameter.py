"""Unit tests for ProjectFileParameter's attested producing-node identity."""

from collections.abc import Generator

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode
from griptape_nodes.exe_types.param_components.project_file_parameter import ProjectFileParameter
from griptape_nodes.node_library.library_registry import (
    LibraryMetadata,
    LibraryRegistry,
    LibrarySchema,
    NodeMetadata,
)


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
