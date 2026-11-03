from unittest.mock import MagicMock

from griptape_nodes.exe_types.core_types import Parameter
from griptape_nodes.retained_mode.managers.object_manager import ObjectManager
from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime
from tests.unit.exe_types.mocks import MockNode


def _node_with_parameter(name: str) -> tuple[MockNode, Parameter]:
    node = MockNode(name=name)
    parameter = Parameter(name="text", tooltip="Text", type="str")
    node.add_parameter(parameter)
    node._tracked_parameters.clear()
    return node, parameter


def _object_manager_with_an_open_workflow() -> ObjectManager:
    engine = MagicMock()
    workflow = WorkflowRuntime("wf")
    engine.current_workflow.return_value = workflow
    engine.current_workflow_or_none.return_value = workflow
    engine.open_workflows.all_open.return_value = [workflow]
    engine.worker_workflows.all_kept.return_value = []
    return ObjectManager(MagicMock(), engine=engine)


class TestQueuedChanges:
    def test_a_registered_node_is_queued_when_an_element_changes(self) -> None:
        object_manager = _object_manager_with_an_open_workflow()
        node, parameter = _node_with_parameter("A")
        object_manager.add_object_by_name("A", node)

        parameter.track_change("tooltip", "Changed")

        assert object_manager.take_nodes_with_queued_changes() == [node]
        assert object_manager.take_nodes_with_queued_changes() == []

    def test_changes_queued_before_registration_are_queued_on_registration(self) -> None:
        object_manager = _object_manager_with_an_open_workflow()
        node, parameter = _node_with_parameter("A")

        parameter.track_change("tooltip", "Changed")
        object_manager.add_object_by_name("A", node)

        assert object_manager.take_nodes_with_queued_changes() == [node]

    def test_a_deleted_node_is_not_queued(self) -> None:
        object_manager = _object_manager_with_an_open_workflow()
        node, parameter = _node_with_parameter("A")
        object_manager.add_object_by_name("A", node)
        parameter.track_change("tooltip", "Changed")

        object_manager.del_obj_by_name("A")
        parameter.track_change("tooltip", "Changed again")

        assert object_manager.take_nodes_with_queued_changes() == []

    def test_a_node_is_queued_once_however_many_elements_change(self) -> None:
        object_manager = _object_manager_with_an_open_workflow()
        node, parameter = _node_with_parameter("A")
        other = Parameter(name="other", tooltip="Other", type="str")
        node.add_parameter(other)
        node._tracked_parameters.clear()
        object_manager.add_object_by_name("A", node)

        parameter.track_change("tooltip", "Changed")
        other.track_change("tooltip", "Changed")

        assert object_manager.take_nodes_with_queued_changes() == [node]

    def test_nodes_are_returned_in_the_order_they_queued(self) -> None:
        object_manager = _object_manager_with_an_open_workflow()
        first, first_parameter = _node_with_parameter("A")
        second, second_parameter = _node_with_parameter("B")
        object_manager.add_object_by_name("A", first)
        object_manager.add_object_by_name("B", second)

        first_parameter.track_change("tooltip", "Changed")
        second_parameter.track_change("tooltip", "Changed")

        assert object_manager.take_nodes_with_queued_changes() == [first, second]
