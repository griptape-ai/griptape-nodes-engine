"""CreateConnectionResultSuccess reports the connection it replaced on a single-connection input."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode
from griptape_nodes.node_library.library_registry import LibraryMetadata, LibraryRegistry, LibrarySchema, NodeMetadata
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
    ListConnectionsForNodeRequest,
    ListConnectionsForNodeResultSuccess,
    ReplacedConnection,
)
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.retained_mode.engine import Engine

_LIBRARY_NAME = "Replaced Connection Library"


class _ValueNode(DataNode):
    """Minimal DataNode with a connectable "value" parameter."""

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


@pytest.fixture
def value_nodes(engine: Engine) -> Iterator[None]:
    """Sources "first" and "second" and a "target", none connected, in a fresh flow."""
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
    library.register_new_node_type(_ValueNode, NodeMetadata(category="test", description="d", display_name="Value"))
    engine.context_manager.push_workflow(workflow_name="replaced_connection_wf")
    flow = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="main", set_as_new_context=True))
    assert isinstance(flow, CreateFlowResultSuccess), flow
    for name in ("first", "second", "target"):
        created = engine.handle_request(CreateNodeRequest(node_type="_ValueNode", node_name=name))
        assert isinstance(created, CreateNodeResultSuccess), created
    try:
        yield
    finally:
        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        LibraryRegistry._clear()


def _connect(engine: Engine, source: str) -> CreateConnectionResultSuccess:
    result = engine.handle_request(
        CreateConnectionRequest(
            source_node_name=source,
            source_parameter_name="value",
            target_node_name="target",
            target_parameter_name="value",
        )
    )
    assert isinstance(result, CreateConnectionResultSuccess), result
    return result


@pytest.mark.usefixtures("value_nodes")
class TestCreateConnectionReportsReplacement:
    def test_first_connection_replaces_nothing(self, engine: Engine) -> None:
        result = _connect(engine, "first")

        assert result.replaced_connection is None

    def test_second_connection_into_same_input_reports_the_first(self, engine: Engine) -> None:
        _connect(engine, "first")

        result = _connect(engine, "second")

        assert result.replaced_connection == ReplacedConnection(
            source_node_name="first",
            source_parameter_name="value",
            target_node_name="target",
            target_parameter_name="value",
        )
        assert "first.value" in str(result.result_details)
        listed = engine.handle_request(ListConnectionsForNodeRequest(node_name="target"))
        assert isinstance(listed, ListConnectionsForNodeResultSuccess), listed
        assert [conn.source_node_name for conn in listed.incoming_connections] == ["second"]

    def test_reconnecting_the_same_pair_replaces_nothing(self, engine: Engine) -> None:
        _connect(engine, "first")

        result = _connect(engine, "first")

        assert result.replaced_connection is None
