"""Execution events name the workflow they belong to when they are built, never after."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import ControlNode
from griptape_nodes.node_library.library_registry import LibraryMetadata, LibraryRegistry, LibrarySchema, NodeMetadata
from griptape_nodes.retained_mode.events.base_events import ExecutionGriptapeNodeEvent, ProgressEvent
from griptape_nodes.retained_mode.events.execution_events import (
    ControlFlowCancelledEvent,
    CurrentControlNodeEvent,
    InvolvedNodesEvent,
    NodeResolvedEvent,
    ParameterValueUpdateEvent,
    StartFlowRequest,
    StartFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.parameter_events import AlterElementEvent
from griptape_nodes.retained_mode.managers.event_manager import EventTranslationContext
from griptape_nodes.retained_mode.managers.object_manager import ObjectManager
from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime
from tests.unit.exe_types.mocks import MockNode
from tests.workflow_context import push_unsaved_workflow

_LIBRARY_NAME = "Execution Event Workflow Id Library"

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.retained_mode.engine import Engine


def _node_in(workflow: WorkflowRuntime, node: MockNode | None = None) -> MockNode:
    if node is None:
        node = MockNode(name="node")
    ObjectManager(MagicMock(), engine=MagicMock()).add_object_by_name_in(workflow, node.name, node)
    return node


class _StreamingNode(ControlNode):
    """Streams a chunk and publishes its output while it runs."""

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(Parameter(name="out", type="str", tooltip="out", allowed_modes={ParameterMode.OUTPUT}))

    def process(self) -> None:
        self.append_value_to_parameter("out", "chunk")
        self.publish_update_to_parameter("out", "done")


@pytest.fixture
def streaming_library(engine: Engine) -> Iterator[None]:
    """A library providing `_StreamingNode`, so the run below creates a real node."""
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
    library.register_new_node_type(
        _StreamingNode, NodeMetadata(category="test", description="d", display_name="Streaming")
    )
    try:
        yield
    finally:
        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        LibraryRegistry._clear()


def _node_with_output(name: str = "node") -> MockNode:
    node = MockNode(name=name)
    node.add_parameter(
        Parameter(name="out", type="str", tooltip="out", allowed_modes={ParameterMode.OUTPUT, ParameterMode.PROPERTY})
    )
    return node


@pytest.fixture
def put_events(engine: Engine) -> Iterator[list[Any]]:
    """Every event put on the engine's queue while the test runs."""
    captured: list[Any] = []
    with (
        patch.object(engine.event_manager, "put_event", side_effect=captured.append),
        patch.object(engine.event_manager, "aput_event", side_effect=captured.append),
    ):
        yield captured


def _execution_events(captured: list[Any], payload_type: type) -> list[Any]:
    return [
        event.wrapped_event
        for event in captured
        if isinstance(event, ExecutionGriptapeNodeEvent) and isinstance(event.wrapped_event.payload, payload_type)
    ]


class TestWorkflowRuntimeExecutionEvent:
    def test_names_the_runtimes_workflow(self) -> None:
        workflow = WorkflowRuntime("wf", MagicMock())

        event = workflow.execution_event(CurrentControlNodeEvent(node_name="node"))

        assert event.wrapped_event.workflow_id == "wf"
        assert event.wrapped_event.payload == CurrentControlNodeEvent(node_name="node")

    def test_the_workflow_cannot_be_changed_after(self) -> None:
        event = WorkflowRuntime("wf", MagicMock()).execution_event(CurrentControlNodeEvent(node_name="node"))

        with pytest.raises(ValidationError):
            event.wrapped_event.workflow_id = "other"


class TestBaseNodeExecutionEvent:
    def test_names_the_nodes_workflow(self) -> None:
        node = _node_in(WorkflowRuntime("wf", MagicMock()))

        event = node.execution_event(CurrentControlNodeEvent(node_name=node.name))

        assert event.wrapped_event.workflow_id == "wf"

    def test_nodes_in_two_workflows_name_their_own(self) -> None:
        first = _node_in(WorkflowRuntime("first", MagicMock()))
        second = _node_in(WorkflowRuntime("second", MagicMock()))

        first_event = first.execution_event(CurrentControlNodeEvent(node_name="node"))
        second_event = second.execution_event(CurrentControlNodeEvent(node_name="node"))

        assert first_event.wrapped_event.workflow_id == "first"
        assert second_event.wrapped_event.workflow_id == "second"

    def test_a_node_in_no_workflow_names_none(self) -> None:
        event = MockNode(name="node").execution_event(CurrentControlNodeEvent(node_name="node"))

        assert event.wrapped_event.workflow_id is None

    def test_a_deleted_node_names_none(self) -> None:
        object_manager = ObjectManager(MagicMock(), engine=MagicMock())
        workflow = WorkflowRuntime("wf", MagicMock())
        node = MockNode(name="node")
        object_manager.add_object_by_name_in(workflow, "node", node)
        object_manager.del_obj_by_name_in(workflow, "node")

        event = node.execution_event(CurrentControlNodeEvent(node_name="node"))

        assert event.wrapped_event.workflow_id is None

    def test_a_workers_copy_names_the_orchestrator_workflow_it_runs_for(self) -> None:
        node = MockNode(name="node")
        node._set_mirrored_workflow_id("orchestrator-wf")

        event = node.execution_event(CurrentControlNodeEvent(node_name="node"))

        assert node.workflow is None
        assert event.wrapped_event.workflow_id == "orchestrator-wf"


class TestNodeEventsNameTheNodesWorkflow:
    def test_a_published_parameter_update(self, put_events: list[Any]) -> None:
        node = _node_in(WorkflowRuntime("wf", MagicMock()), _node_with_output())

        node.publish_update_to_parameter("out", "value")

        assert [event.workflow_id for event in _execution_events(put_events, ParameterValueUpdateEvent)] == ["wf"]

    def test_a_parameter_added_and_an_output_set(self, put_events: list[Any]) -> None:
        node = _node_in(WorkflowRuntime("wf", MagicMock()), _node_with_output())
        # Building the node before it joined the workflow sent events naming none.
        put_events.clear()

        node.add_parameter(Parameter(name="extra", type="str", tooltip="extra"))
        node.parameter_output_values["out"] = "value"

        altered = _execution_events(put_events, AlterElementEvent)
        assert altered
        assert {event.workflow_id for event in altered} == {"wf"}

    def test_a_streamed_progress_chunk(self, put_events: list[Any]) -> None:
        node = _node_in(WorkflowRuntime("wf", MagicMock()), _node_with_output())

        node.append_value_to_parameter("out", "chunk")

        progress = [event for event in put_events if isinstance(event, ProgressEvent)]
        assert [(event.value, event.workflow_id) for event in progress] == [("chunk", "wf")]

    def test_a_deleted_node_names_none(self, put_events: list[Any]) -> None:
        object_manager = ObjectManager(MagicMock(), engine=MagicMock())
        workflow = WorkflowRuntime("wf", MagicMock())
        node = _node_with_output()
        object_manager.add_object_by_name_in(workflow, node.name, node)
        object_manager.del_obj_by_name_in(workflow, node.name)

        node.publish_update_to_parameter("out", "value")
        node.append_value_to_parameter("out", "chunk")

        updates = _execution_events(put_events, ParameterValueUpdateEvent)
        progress = [event for event in put_events if isinstance(event, ProgressEvent)]
        assert [event.workflow_id for event in updates] == [None]
        assert [event.workflow_id for event in progress] == [None]


class TestARunsEventsNameItsWorkflow:
    @pytest.mark.usefixtures("streaming_library")
    @pytest.mark.asyncio
    async def test_every_event_of_a_run_names_the_workflow_it_runs_in(
        self, engine: Engine, put_events: list[Any]
    ) -> None:
        entry = push_unsaved_workflow(engine)
        created_flow = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas"))
        assert isinstance(created_flow, CreateFlowResultSuccess), created_flow.result_details
        created = engine.handle_request(
            CreateNodeRequest(node_type="_StreamingNode", specific_library_name=_LIBRARY_NAME, node_name="stream")
        )
        assert isinstance(created, CreateNodeResultSuccess), created.result_details
        put_events.clear()

        result = await engine.ahandle_request(StartFlowRequest(flow_name="canvas", flow_node_name="stream"))

        assert isinstance(result, StartFlowResultSuccess), result.result_details
        execution_events = [
            event.wrapped_event for event in put_events if isinstance(event, ExecutionGriptapeNodeEvent)
        ]
        progress_events = [event for event in put_events if isinstance(event, ProgressEvent)]
        sent_payload_types = {type(event.payload) for event in execution_events}
        assert {InvolvedNodesEvent, CurrentControlNodeEvent, NodeResolvedEvent} <= sent_payload_types
        assert progress_events
        assert {event.workflow_id for event in execution_events} == {entry.workflow_id}
        assert {event.workflow_id for event in progress_events} == {entry.workflow_id}


class TestFlowManagerEventsNameTheWorkflowItActsOn:
    def test_abandoning_a_run_in_a_workflow_that_is_not_the_open_one(
        self, engine: Engine, put_events: list[Any]
    ) -> None:
        push_unsaved_workflow(engine)
        other = WorkflowRuntime("other", engine)

        asyncio.run(engine.flow_manager._abandon_running_flow(other))

        sent = _execution_events(put_events, InvolvedNodesEvent) + _execution_events(
            put_events, ControlFlowCancelledEvent
        )
        assert len(sent) == 2  # noqa: PLR2004
        assert {event.workflow_id for event in sent} == {"other"}


class TestEventTranslationKeepsTheWorkflow:
    def test_a_translated_event_names_the_workflow_it_was_built_for(self, engine: Engine) -> None:
        delivered: list[Any] = []
        workflow = WorkflowRuntime("wf", MagicMock())
        with (
            patch.object(engine.event_manager, "put_event", side_effect=delivered.append),
            EventTranslationContext(engine.event_manager, {"packaged": "original"}),
        ):
            engine.event_manager.put_event(workflow.execution_event(CurrentControlNodeEvent(node_name="packaged")))

        [event] = delivered
        assert event.wrapped_event.payload.node_name == "original"
        assert event.wrapped_event.workflow_id == "wf"
