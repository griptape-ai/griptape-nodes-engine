"""`BaseNode.workflow` names the workflow whose objects the node is in, and nothing else."""

from __future__ import annotations

from unittest.mock import MagicMock

from griptape_nodes.retained_mode.managers.object_manager import ObjectManager
from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime
from tests.unit.exe_types.mocks import MockNode


def _object_manager() -> ObjectManager:
    return ObjectManager(MagicMock(), engine=MagicMock())


class TestObjectManagerSetsNodeWorkflow:
    def test_a_node_in_no_workflow_has_none(self) -> None:
        assert MockNode(name="A").workflow is None

    def test_adding_a_node_to_a_workflow_sets_it(self) -> None:
        workflow = WorkflowRuntime("wf", MagicMock())
        node = MockNode(name="A")

        _object_manager().add_object_by_name_in(workflow, "A", node)

        assert node.workflow is workflow

    def test_removing_a_node_from_its_workflow_clears_it(self) -> None:
        object_manager = _object_manager()
        workflow = WorkflowRuntime("wf", MagicMock())
        node = MockNode(name="A")
        object_manager.add_object_by_name_in(workflow, "A", node)

        object_manager.del_obj_by_name_in(workflow, "A")

        assert node.workflow is None

    def test_nodes_in_two_workflows_each_have_their_own(self) -> None:
        object_manager = _object_manager()
        first = WorkflowRuntime("first", MagicMock())
        second = WorkflowRuntime("second", MagicMock())
        first_node = MockNode(name="A")
        second_node = MockNode(name="A")

        object_manager.add_object_by_name_in(first, "A", first_node)
        object_manager.add_object_by_name_in(second, "A", second_node)

        assert first_node.workflow is first
        assert second_node.workflow is second
