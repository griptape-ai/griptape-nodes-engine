"""Execution events name the workflow they belong to when they are built, never after."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from griptape_nodes.retained_mode.events.execution_events import CurrentControlNodeEvent
from griptape_nodes.retained_mode.managers.object_manager import ObjectManager
from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime
from tests.unit.exe_types.mocks import MockNode


def _node_in(workflow: WorkflowRuntime, name: str = "node") -> MockNode:
    node = MockNode(name=name)
    ObjectManager(MagicMock(), engine=MagicMock()).add_object_by_name_in(workflow, name, node)
    return node


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
