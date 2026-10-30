import logging
from re import Pattern
from typing import Any

from griptape_nodes.exe_types.core_types import (
    BaseNodeElement,
    Parameter,
)
from griptape_nodes.exe_types.flow import ControlFlow
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.retained_mode.engine import Engine, EngineScoped
from griptape_nodes.retained_mode.events.base_events import (
    ResultDetails,
    ResultPayload,
)
from griptape_nodes.retained_mode.events.node_events import (
    DeleteNodeRequest,
)
from griptape_nodes.retained_mode.events.object_events import (
    ClearAllObjectStateRequest,
    ClearAllObjectStateResultFailure,
    ClearAllObjectStateResultSuccess,
    RenameObjectRequest,
    RenameObjectResultFailure,
    RenameObjectResultSuccess,
)
from griptape_nodes.retained_mode.events.parameter_events import (
    RemoveParameterFromNodeRequest,
)
from griptape_nodes.retained_mode.managers.event_manager import EventManager
from griptape_nodes.retained_mode.request_handlers import RequestScope, handles
from griptape_nodes.retained_mode.runtime.binding import bound_workflow
from griptape_nodes.retained_mode.runtime.objects import WorkflowObjects
from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

logger = logging.getLogger("griptape_nodes")


class ObjectManager(EngineScoped):
    """The front for the open workflow's objects, which its runtime holds.

    Lookups find nothing when no workflow is open. Adding, removing or renaming an object needs an
    open workflow to hold it.
    """

    def __init__(self, _event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        _event_manager.register_request_handlers(self)

    @handles(RenameObjectRequest, scope=RequestScope.WORKFLOW)
    def on_rename_object_request(self, request: RenameObjectRequest, workflow: WorkflowRuntime) -> ResultPayload:
        # Does the source object exist?
        if request.object_name == request.requested_name:
            unchanged = RenameObjectResultSuccess(
                final_name=request.requested_name,
                result_details=f"Object '{request.requested_name}' already has the requested name",
            )
            # Nothing changed, so this is not an edit: it must not dirty the workflow or consume an
            # undo slot that would replay as a no-op.
            unchanged.altered_workflow_state = False
            return unchanged
        source_obj = workflow.objects.get(request.object_name)
        if source_obj is None:
            details = f"Attempted to rename object '{request.object_name}', but no object of that name could be found."
            return RenameObjectResultFailure(next_available_name=None, result_details=details)

        # Is there a collision?
        requested_name_obj = workflow.objects.get(request.requested_name)
        if requested_name_obj is None:
            final_name = request.requested_name
        else:
            # Collision. Decide what to do.
            next_name = workflow.objects.generate_name(
                type_name=source_obj.__class__.__name__, requested_name=request.requested_name
            )

            # Will the requester allow us to use the next closest name available?
            if not request.allow_next_closest_name_available:
                # Not allowed to use it :(
                # Fail it but be nice and offer the next name that WOULD HAVE been available.
                details = f"Attempted to rename object '{request.object_name}' to '{request.requested_name}'. Failed because another object of that name exists. Next available name would have been '{next_name}'."
                return RenameObjectResultFailure(next_available_name=next_name, result_details=details)
            # We'll use the next available name.
            final_name = next_name

            # Let the object's manager know. TODO: https://github.com/griptape-ai/griptape-nodes/issues/869
        match source_obj:
            case ControlFlow():
                self.engine.flow_manager.handle_flow_rename(old_name=request.object_name, new_name=final_name)
            case BaseNode():
                self.engine.node_manager.handle_node_rename(old_name=request.object_name, new_name=final_name)
            case _:
                details = f"Attempted to rename an object named '{request.object_name}', but that object wasn't of a type supported for rename."
                return RenameObjectResultFailure(next_available_name=None, result_details=details)

        # Update the object table.
        workflow.objects.rename(request.object_name, final_name)

        details = f"Successfully renamed object '{request.object_name}' to '{final_name}'."
        log_level = logging.DEBUG
        if final_name != request.requested_name:
            details += f" Originally requested the name '{request.requested_name}', but that was taken."
            log_level = logging.WARNING
        if log_level == logging.WARNING:
            result_details = ResultDetails(message=details, level=logging.WARNING)
        else:
            result_details = details
        return RenameObjectResultSuccess(final_name=final_name, result_details=result_details)

    @handles(ClearAllObjectStateRequest, scope=RequestScope.OPTIONAL_WORKFLOW)
    async def on_clear_all_object_state_request(
        self, request: ClearAllObjectStateRequest, workflow: WorkflowRuntime | None = None
    ) -> ResultPayload:
        """Close `workflow`, the one the request names, or every open workflow when it names none.

        Closing a workflow releases the held objects kept for it. Clearing everything also releases
        the ones kept for no workflow, here and in every worker.
        """
        if not request.i_know_what_im_doing:
            details = "Attempted to clear all object state and delete everything. Failed because they didn't know what they were doing."
            return ClearAllObjectStateResultFailure(result_details=details)

        # Closing tells the workers which workflow closed, or, closing every one, to release everything.
        try:
            if workflow is None:
                await self.engine.aclose_all_workflows()
            elif self.engine.open_workflows.is_open(workflow.workflow_id):
                await self.engine.aclose_workflow(workflow)
        except Exception as e:
            details = f"Attempted to clear all object state and delete everything. Failed with exception: {e}"
            return ClearAllObjectStateResultFailure(result_details=details)

        # Clear all event suppression
        self.engine.event_manager.clear_event_suppression()

        details = "Successfully cleared all object state (deleted everything)."
        return ClearAllObjectStateResultSuccess(result_details=details)

    def get_filtered_subset[T](
        self,
        name: str | Pattern | None = None,
        type: type[T] | None = None,  # noqa: A002
    ) -> dict[str, T]:
        """Filter a dictionary by key pattern and/or value type.

        Args:
            name: A regex pattern string or compiled pattern to match keys
            type: A type to match values

        Returns:
            A new filtered dictionary containing only matching key-value pairs
        """
        return self._objects_or_empty().get_filtered_subset(name=name, type=type)

    def generate_name_for_object(self, type_name: str, requested_name: str | None = None) -> str:
        return self._objects_or_empty().generate_name(type_name=type_name, requested_name=requested_name)

    def add_object_by_name(self, name: str, obj: object) -> None:
        objects = self.engine.current_workflow().objects
        objects.add(name, obj)
        if isinstance(obj, BaseNode):
            # The workflow holding the node queues its changes, so they go when it closes.
            obj.set_change_listener(objects.note_queued_changes)

    def get_object_by_name(self, name: str) -> object:
        obj = self._objects_or_empty().get(name)
        if obj is None:
            raise KeyError(name)
        return obj

    def has_object_with_name(self, name: str) -> bool:
        return self._objects_or_empty().has(name)

    def attempt_get_object_by_name(self, name: str) -> Any | None:
        return self._objects_or_empty().get(name)

    def attempt_get_object_by_name_as_type[T](self, name: str, cast_type: type[T]) -> T | None:
        return self._objects_or_empty().get_as_type(name, cast_type)

    def del_obj_by_name(self, name: str) -> None:
        objects = self.engine.current_workflow().objects
        # Does the object have any children? delete those
        obj = objects.get(name)
        if obj is None:
            raise KeyError(name)
        if isinstance(obj, BaseNodeElement):
            children = obj.find_elements_by_type(BaseNodeElement)
            for child in children:
                obj.remove_child(child)
                if isinstance(child, BaseNode):
                    self.engine.handle_request(DeleteNodeRequest(child.name))
                    return
                if isinstance(child, Parameter) and isinstance(obj, BaseNode):
                    self.engine.handle_request(RemoveParameterFromNodeRequest(child.name, obj.name))
                    return
        if isinstance(obj, BaseNode):
            obj.set_change_listener(None)
            objects.forget_queued_changes(obj)
        objects.remove(name)

    def take_nodes_with_queued_changes(self) -> list[BaseNode]:
        """Return the nodes with queued UI changes, in queue order, and forget them.

        The bound workflow's nodes. Work bound to no workflow runtime takes those of every open
        workflow, or on a worker of every runtime it keeps, so it never has to choose one. A closed
        workflow's queued changes went with it.
        """
        bound = bound_workflow.get()
        if isinstance(bound, WorkflowRuntime):
            return bound.objects.take_nodes_with_queued_changes()

        nodes: list[BaseNode] = []
        for workflow in [*self.engine.open_workflows.all_open(), *self.engine.worker_workflows.all_kept()]:
            nodes.extend(workflow.objects.take_nodes_with_queued_changes())
        return nodes

    def _objects_or_empty(self) -> WorkflowObjects:
        """The open workflow's objects for a lookup, which finds none when no workflow is open.

        Only lookups read through here. Adding or removing an object needs a workflow to hold it.
        """
        workflow = self.engine.current_workflow_or_none()
        if workflow is None:
            return WorkflowObjects()
        return workflow.objects
