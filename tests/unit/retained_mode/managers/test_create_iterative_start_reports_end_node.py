"""CreateNodeResultSuccess for an iterative Start node names the End node created with it."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.node_library.library_registry import LibraryMetadata, LibraryRegistry, LibrarySchema, NodeMetadata
from griptape_nodes.retained_mode.events.base_events import ResultDetails
from griptape_nodes.retained_mode.events.connection_events import (
    ListConnectionsForNodeRequest,
    ListConnectionsForNodeResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from tests.unit.retained_mode.managers.test_add_nodes_to_group_iterative import (
    _MockIterativeEndNode,
    _MockIterativeStartNode,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.exe_types.core_types import Parameter
    from griptape_nodes.exe_types.node_types import BaseNode
    from griptape_nodes.retained_mode.engine import Engine

_LIBRARY_NAME = "Iterative Pair Library"


class _RejectingStartNode(_MockIterativeStartNode):
    """Start node whose End node refuses the loop connection."""


class _RejectingEndNode(_MockIterativeEndNode):
    """End node that refuses every incoming connection."""

    def allow_incoming_connection(
        self,
        source_node: BaseNode,  # noqa: ARG002
        source_parameter: Parameter,  # noqa: ARG002
        target_parameter: Parameter,  # noqa: ARG002
    ) -> bool:
        return False


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
    for node_class in (_MockIterativeStartNode, _MockIterativeEndNode, _RejectingStartNode, _RejectingEndNode):
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


_START_METADATA = {"position": {"x": 0, "y": 0}}


@pytest.mark.usefixtures("loop_flow")
class TestCreateIterativeStartNodeReportsEndNode:
    def test_result_names_the_end_node(self, engine: Engine) -> None:
        result = engine.handle_request(
            CreateNodeRequest(node_type="_MockIterativeStartNode", node_name="Start", metadata=dict(_START_METADATA))
        )

        assert isinstance(result, CreateNodeResultSuccess), result
        start = engine.node_manager.get_node_by_name("Start")
        assert isinstance(start, _MockIterativeStartNode)
        assert start.end_node is not None
        assert result.paired_node_name == start.end_node.name
        connections = engine.handle_request(ListConnectionsForNodeRequest(node_name="Start"))
        assert isinstance(connections, ListConnectionsForNodeResultSuccess), connections
        outgoing = {
            (c.source_parameter_name, c.target_node_name, c.target_parameter_name)
            for c in connections.outgoing_connections
        }
        assert ("loop", start.end_node.name, "from_start") in outgoing
        assert start.end_node.name in str(result.result_details)

    def test_paired_node_name_is_none_for_other_nodes(self, engine: Engine) -> None:
        result = engine.handle_request(CreateNodeRequest(node_type="_MockIterativeEndNode", node_name="End"))

        assert isinstance(result, CreateNodeResultSuccess), result
        assert result.paired_node_name is None

    def test_unconnected_end_node_is_named_with_a_warning(self, engine: Engine) -> None:
        result = engine.handle_request(
            CreateNodeRequest(node_type="_RejectingStartNode", node_name="Start", metadata=dict(_START_METADATA))
        )

        assert isinstance(result, CreateNodeResultSuccess), result
        start = engine.node_manager.get_node_by_name("Start")
        assert isinstance(start, _RejectingStartNode)
        assert isinstance(start.end_node, _RejectingEndNode)
        assert result.paired_node_name == start.end_node.name
        assert isinstance(result.result_details, ResultDetails)
        [detail] = result.result_details.result_details
        assert detail.level == logging.WARNING
        assert "from_start" in detail.message
