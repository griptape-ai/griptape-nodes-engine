"""CreateNodeRequest for an iterative Start node, which also creates and wires its End node."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from griptape_nodes.node_library.library_registry import LibraryMetadata, LibraryRegistry, LibrarySchema, NodeMetadata
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from tests.unit.retained_mode.managers.test_add_nodes_to_group_iterative import (
    _MockIterativeEndNode,
    _MockIterativeStartNode,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.retained_mode.engine import Engine

_LIBRARY_NAME = "Iterative Pair Library"


@pytest.fixture
def loop_flow(engine: Engine) -> Iterator[None]:
    """A fresh flow with a library that provides a Start/End pair named the way pairing expects."""
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
    for node_class in (_MockIterativeStartNode, _MockIterativeEndNode):
        library.register_new_node_type(
            node_class, NodeMetadata(category="test", description="d", display_name=node_class.__name__)
        )
    engine.context_manager.push_workflow(workflow_name="iterative_pair_wf")
    flow = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="main", set_as_new_context=True))
    assert isinstance(flow, CreateFlowResultSuccess), flow
    try:
        yield
    finally:
        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        LibraryRegistry._clear()


@pytest.mark.usefixtures("loop_flow")
class TestCreateIterativeStartNodeWithoutPosition:
    def test_start_without_metadata_creates_and_wires_end(self, engine: Engine) -> None:
        result = engine.handle_request(CreateNodeRequest(node_type="_MockIterativeStartNode", node_name="Start"))

        assert isinstance(result, CreateNodeResultSuccess), result
        start = engine.node_manager.get_node_by_name("Start")
        assert isinstance(start, _MockIterativeStartNode)
        assert isinstance(start.end_node, _MockIterativeEndNode)
        assert start.end_node.start_node is start
        assert "position" not in start.end_node.metadata

    def test_end_is_offset_from_start_position(self, engine: Engine) -> None:
        result = engine.handle_request(
            CreateNodeRequest(
                node_type="_MockIterativeStartNode",
                node_name="Start",
                metadata={"position": {"x": 100, "y": 40}},
            )
        )

        assert isinstance(result, CreateNodeResultSuccess), result
        start = engine.node_manager.get_node_by_name("Start")
        assert isinstance(start, _MockIterativeStartNode)
        assert start.end_node is not None
        assert start.end_node.metadata["position"] == {"x": 750, "y": 40}
