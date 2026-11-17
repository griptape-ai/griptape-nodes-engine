"""`BaseNode.workflow` names the workflow whose objects the node joined, and nothing else."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar
from unittest.mock import MagicMock, patch

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode
from griptape_nodes.node_library.library_registry import LibraryMetadata, LibraryRegistry, LibrarySchema, NodeMetadata
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    GetTopLevelFlowRequest,
    GetTopLevelFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.library_events import (
    DescribeNodeTypeRequest,
    DescribeNodeTypeResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    CreateNodeResultSuccess,
    DeleteNodeRequest,
    DeleteNodeResultSuccess,
)
from griptape_nodes.retained_mode.events.object_events import RenameObjectRequest, RenameObjectResultSuccess
from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime
from tests.unit.exe_types.mocks import MockNode

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.exe_types.node_types import BaseNode

_LIBRARY_NAME = "Node Workflow Library"


class _ValueNode(DataNode):
    """A node with one parameter that records every instance built."""

    built: ClassVar[list[_ValueNode]] = []

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="value", tooltip="Value", type="str", default_value="", allowed_modes={ParameterMode.PROPERTY}
            )
        )
        _ValueNode.built.append(self)

    def process(self) -> None:
        pass


@pytest.fixture(autouse=True)
def node_library() -> Iterator[None]:
    """Register `_ValueNode`. LibraryRegistry is process-global, so it is cleared around the test."""
    LibraryRegistry._clear()
    _ValueNode.built.clear()
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
    try:
        yield
    finally:
        _ValueNode.built.clear()
        LibraryRegistry._clear()


@pytest.fixture
def workflow(engine: Engine) -> WorkflowRuntime:
    """An open workflow with a top-level flow named "canvas"."""
    entry = engine.workflow_registry.register_unsaved("Node workflow", listed=False)
    opened = engine.open_workflow(entry.workflow_id)
    created = opened.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas"))
    assert isinstance(created, CreateFlowResultSuccess), created.result_details
    return opened


def _create_node(engine: Engine, workflow: WorkflowRuntime) -> BaseNode:
    result = workflow.handle_request(
        CreateNodeRequest(node_type="_ValueNode", specific_library_name=_LIBRARY_NAME, node_name="node")
    )
    assert isinstance(result, CreateNodeResultSuccess), result.result_details
    return engine.node_manager.get_node_by_name_in(workflow, result.node_name)


class TestNodeWorkflow:
    def test_a_node_that_never_joined_a_workflow_has_none(self) -> None:
        assert MockNode(name="A").workflow is None

    def test_a_created_node_names_its_workflow(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        node = _create_node(engine, workflow)

        assert node.workflow is workflow

    def test_a_deleted_node_keeps_naming_its_workflow(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        node = _create_node(engine, workflow)

        result = workflow.handle_request(DeleteNodeRequest(node_name=node.name))

        assert isinstance(result, DeleteNodeResultSuccess), result.result_details
        assert node.workflow is workflow

    def test_a_node_built_to_describe_its_type_has_none(self, engine: Engine) -> None:
        result = engine.handle_request(DescribeNodeTypeRequest(node_type="_ValueNode", library=_LIBRARY_NAME))

        assert isinstance(result, DescribeNodeTypeResultSuccess), result.result_details
        assert [node.workflow for node in _ValueNode.built] == [None]

    def test_a_renamed_node_keeps_naming_its_workflow(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        node = _create_node(engine, workflow)

        result = workflow.handle_request(RenameObjectRequest(object_name=node.name, requested_name="renamed"))

        assert isinstance(result, RenameObjectResultSuccess), result.result_details
        assert node.workflow is workflow

    def test_walking_a_node_stops_at_its_workflow(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        """Publishing walks each node for the classes it holds, which must not reach the engine."""
        node = _create_node(engine, workflow)
        reached: list[type] = []

        engine.workflow_manager.codegen.walk_object_tree(node, lambda cls, _instance: reached.append(cls))

        assert _ValueNode in reached
        assert not any(issubclass(cls, (Engine, WorkflowRuntime)) for cls in reached)


class TestNodeEngine:
    def test_a_node_in_a_workflow_uses_that_workflows_engine(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        """Node code on a thread bound to another engine still drives the engine holding its workflow."""
        node = _create_node(engine, workflow)

        with patch("griptape_nodes.retained_mode.engine.current_engine", return_value=MagicMock(spec=Engine)):
            assert node.engine is engine

    def test_a_node_in_no_workflow_sends_requests_that_act_on_no_workflow(
        self, engine: Engine, workflow: WorkflowRuntime
    ) -> None:
        """A request about a workflow fails rather than act on the workflow the caller is in."""
        node = MockNode(name="helper")
        in_workflow = workflow.handle_request(GetTopLevelFlowRequest())
        assert isinstance(in_workflow, GetTopLevelFlowResultSuccess)
        assert in_workflow.flow_name == "canvas"

        with engine.bind_workflow(workflow):
            result = node._handle_request_in_workflow(GetTopLevelFlowRequest())

        assert isinstance(result, GetTopLevelFlowResultSuccess)
        assert result.flow_name is None
