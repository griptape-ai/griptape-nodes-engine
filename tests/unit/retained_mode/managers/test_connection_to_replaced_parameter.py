"""A connection into a parameter its node replaced or removed locally can still be edited.

Nodes that rebuild parameters while loading remove and re-add them locally, without requests. The
connection keeps the Parameter it was made to, and finds the node's current one by name. When the node
no longer has the parameter at all, the connection can still be deleted, and so can its nodes and flow.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import BaseNode, DataNode
from griptape_nodes.node_library.library_registry import LibraryMetadata, LibraryRegistry, LibrarySchema, NodeMetadata
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
    DeleteConnectionRequest,
    DeleteConnectionResultSuccess,
    ListConnectionsForNodeRequest,
    ListConnectionsForNodeResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    DeleteFlowRequest,
    DeleteFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    CreateNodeResultSuccess,
    DeleteNodeRequest,
    DeleteNodeResultSuccess,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.retained_mode.engine import Engine

_LIBRARY_NAME = "Replaced Parameter Library"


def _value_parameter() -> Parameter:
    return Parameter(
        name="value",
        tooltip="Value to echo",
        type="str",
        default_value="",
        allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY, ParameterMode.OUTPUT},
    )


class _ValueNode(DataNode):
    """Minimal DataNode with a connectable "value" parameter."""

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(_value_parameter())

    def process(self) -> None:
        self.parameter_output_values["value"] = self.get_parameter_value("value")


@pytest.fixture
def connected_nodes(engine: Engine) -> Iterator[None]:
    """Node "first" connected into "target", whose "value" is then replaced locally, and an unconnected "second"."""
    target = _connect_first_into_target(engine)
    original = target.get_parameter_by_name("value")
    assert original is not None
    target.remove_parameter_element(original)
    target.add_parameter(_value_parameter())
    assert target.get_parameter_by_name("value") is not original
    try:
        yield
    finally:
        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        LibraryRegistry._clear()


@pytest.fixture
def connection_to_removed_parameter(engine: Engine) -> Iterator[None]:
    """Node "first" connected into "target", whose "value" is then removed locally and not added back."""
    target = _connect_first_into_target(engine)
    original = target.get_parameter_by_name("value")
    assert original is not None
    target.remove_parameter_element(original)
    try:
        yield
    finally:
        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        LibraryRegistry._clear()


def _connect_first_into_target(engine: Engine) -> BaseNode:
    """Nodes "first", "second" and "target" in flow "main", with "first" connected into "target". Returns "target"."""
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
    engine.context_manager.push_workflow(workflow_name="replaced_parameter_wf")
    flow = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="main", set_as_new_context=True))
    assert isinstance(flow, CreateFlowResultSuccess), flow
    for name in ("first", "second", "target"):
        created = engine.handle_request(CreateNodeRequest(node_type="_ValueNode", node_name=name))
        assert isinstance(created, CreateNodeResultSuccess), created
    _connect(engine, "first")
    return engine.node_manager.get_node_by_name("target")


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


def _incoming_sources(engine: Engine) -> list[str]:
    listed = engine.handle_request(ListConnectionsForNodeRequest(node_name="target"))
    assert isinstance(listed, ListConnectionsForNodeResultSuccess), listed
    return [conn.source_node_name for conn in listed.incoming_connections]


@pytest.mark.usefixtures("connected_nodes")
class TestConnectionToReplacedParameter:
    def test_the_connection_can_be_deleted(self, engine: Engine) -> None:
        result = engine.handle_request(
            DeleteConnectionRequest(
                source_node_name="first",
                source_parameter_name="value",
                target_node_name="target",
                target_parameter_name="value",
            )
        )

        assert isinstance(result, DeleteConnectionResultSuccess), result
        assert _incoming_sources(engine) == []

    def test_a_new_connection_into_the_input_replaces_it(self, engine: Engine) -> None:
        result = _connect(engine, "second")

        assert result.replaced_connection is not None
        assert result.replaced_connection.source_node_name == "first"
        assert _incoming_sources(engine) == ["second"]

    def test_the_node_can_be_deleted(self, engine: Engine) -> None:
        result = engine.handle_request(DeleteNodeRequest(node_name="target"))

        assert isinstance(result, DeleteNodeResultSuccess), result


@pytest.mark.usefixtures("connection_to_removed_parameter")
class TestConnectionToRemovedParameter:
    def test_the_connection_can_be_deleted(self, engine: Engine) -> None:
        result = engine.handle_request(
            DeleteConnectionRequest(
                source_node_name="first",
                source_parameter_name="value",
                target_node_name="target",
                target_parameter_name="value",
            )
        )

        assert isinstance(result, DeleteConnectionResultSuccess), result
        assert _incoming_sources(engine) == []

    @pytest.mark.parametrize("node_name", ["target", "first"])
    def test_either_node_can_be_deleted(self, engine: Engine, node_name: str) -> None:
        result = engine.handle_request(DeleteNodeRequest(node_name=node_name))

        assert isinstance(result, DeleteNodeResultSuccess), result

    def test_the_flow_can_be_deleted(self, engine: Engine) -> None:
        result = engine.handle_request(DeleteFlowRequest(flow_name="main"))

        assert isinstance(result, DeleteFlowResultSuccess), result
