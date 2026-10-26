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
from griptape_nodes.retained_mode.runtime.objects import WorkflowObjects
from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

logger = logging.getLogger("griptape_nodes")


class ObjectManager(EngineScoped):
    """The front for the open workflow's objects, which its runtime holds.

    Lookups find nothing when no workflow is open. Adding, removing or renaming an object needs an
    open workflow to hold it.
    """

    # Registered nodes with queued UI changes, keyed by id() because a node hashes by its mutable name.
    _nodes_with_queued_changes: dict[int, BaseNode]

    def __init__(self, _event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        self._nodes_with_queued_changes = {}
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
                self.engine.flow_manager.handle_flow_rename(workflow, old_name=request.object_name, new_name=final_name)
            case BaseNode():
                self.engine.node_manager.handle_node_rename(workflow, old_name=request.object_name, new_name=final_name)
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
        """Close the workflow, or with none, clear a worker's mirror and what this process holds."""
        if not request.i_know_what_im_doing:
            details = "Attempted to clear all object state and delete everything. Failed because they didn't know what they were doing."
            return ClearAllObjectStateResultFailure(result_details=details)

        if workflow is None:
            try:
                context_manager = self.engine.context_manager
                if context_manager.has_current_workflow():
                    # With none open, the workflow in context is a worker's mirror of the orchestrator's,
                    # e.g. a worker reloading its libraries. Clear it and release what this
                    # process holds, as closing a workflow does.
                    context_manager.mirror_workflow_context(None, None, None, None)
                    self.engine.resource_manager.drop_all_local_objects()
            except Exception as e:
                details = f"Attempted to clear all object state and delete everything. Failed with exception: {e}"
                return ClearAllObjectStateResultFailure(result_details=details)
            finally:
                # The workers may still hold objects. Nothing retries this request, so a worker that
                # was never told keeps them for the life of the process.
                await self.engine.worker_manager.broadcast_local_object_teardown()
        else:
            # Cancel any in-flight run before tearing its bookkeeping down: the reset below makes the
            # flow look idle, so a cancel after it would be skipped and the previous run's node tasks
            # keep going.
            await self.engine.flow_manager.cancel_running_flow_before_teardown(workflow)

            try:
                # Eliminate all execution references before deletion
                self.engine.flow_manager.reset_global_execution_state(workflow)
            except Exception as e:
                details = f"Attempted to reset global execution state. Failed with exception: {e}"
                return ClearAllObjectStateResultFailure(result_details=details)

            try:
                await self.engine.close_workflow_state(workflow)
            except Exception as e:
                details = f"Attempted to clear all object state and delete everything. Failed with exception: {e}"
                return ClearAllObjectStateResultFailure(result_details=details)

        if workflow is not None and len(workflow.objects) > 0:
            details = f"Attempted to clear all object state, but {len(workflow.objects)} object(s) remained after workflow teardown."
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
        """Filter the open workflow's objects by name pattern and/or type.

        Args:
            name: A regex pattern string or compiled pattern to match names
            type: A type to match objects

        Returns:
            A new dictionary containing only the matching objects
        """
        return self._objects_or_empty().get_filtered_subset(name=name, type=type)

    def generate_name_for_object(self, type_name: str, requested_name: str | None = None) -> str:
        return self._objects_or_empty().generate_name(type_name=type_name, requested_name=requested_name)

    def add_object_by_name(self, name: str, obj: object) -> None:
        """Edge method for code not handed a workflow; engine code calls `add_object_by_name_in`."""
        self.add_object_by_name_in(self.engine.open_workflows.sole_workflow(), name, obj)

    def add_object_by_name_in(self, workflow: WorkflowRuntime, name: str, obj: object) -> None:
        workflow.objects.add(name, obj)
        if isinstance(obj, BaseNode):
            # The one place a node joins a workflow, so every node in `workflow.objects` knows it.
            obj._set_workflow(workflow)
            obj.set_change_listener(self._note_queued_changes)

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
        """Edge method for code not handed a workflow; engine code calls `del_obj_by_name_in`."""
        self.del_obj_by_name_in(self.engine.open_workflows.sole_workflow(), name)

    def del_obj_by_name_in(self, workflow: WorkflowRuntime, name: str) -> None:
        objects = workflow.objects
        # Does the object have any children? delete those
        obj = objects.get(name)
        if obj is None:
            raise KeyError(name)
        if isinstance(obj, BaseNodeElement):
            children = obj.find_elements_by_type(BaseNodeElement)
            for child in children:
                obj.remove_child(child)
                if isinstance(child, BaseNode):
                    workflow.handle_request(DeleteNodeRequest(child.name))
                    return
                if isinstance(child, Parameter) and isinstance(obj, BaseNode):
                    workflow.handle_request(RemoveParameterFromNodeRequest(child.name, obj.name))
                    return
        if isinstance(obj, BaseNode):
            obj._set_workflow(None)
            obj.set_change_listener(None)
            self._nodes_with_queued_changes.pop(id(obj), None)
        objects.remove(name)

    def take_nodes_with_queued_changes(self) -> list[BaseNode]:
        """Return the registered nodes with queued UI changes, in queue order, and forget them.

        Drained one atomic ``popitem`` at a time: a node can queue a change from another thread
        mid-drain, and copying then clearing would drop it while its element stays queued. The
        empty check is the pop itself, because another thread may be draining too.
        """
        nodes = []
        while True:
            try:
                _, node = self._nodes_with_queued_changes.popitem()
            except KeyError:
                break
            nodes.append(node)
        nodes.reverse()
        return nodes

    def _note_queued_changes(self, node: BaseNode) -> None:
        self._nodes_with_queued_changes[id(node)] = node

    def _objects_or_empty(self) -> WorkflowObjects:
        """The open workflow's objects for a lookup, which finds none when no workflow is open.

        Only lookups read through here. Adding or removing an object needs a workflow to hold it.
        """
        workflow = self.engine.open_workflows.sole_workflow_or_none()
        if workflow is None:
            return WorkflowObjects()
        return workflow.objects
