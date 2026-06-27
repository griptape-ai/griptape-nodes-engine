import logging
import re
from re import Pattern
from typing import Any

from griptape_nodes.exe_types.core_types import (
    BaseNodeElement,
    Parameter,
)
from griptape_nodes.exe_types.flow import ControlFlow
from griptape_nodes.exe_types.node_types import BaseNode
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
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.event_manager import EventManager

logger = logging.getLogger("griptape_nodes")


class ObjectManager:
    _name_to_objects: dict[str, object]

    def __init__(self, _event_manager: EventManager) -> None:
        self._name_to_objects = {}
        _event_manager.assign_manager_to_request_type(
            request_type=RenameObjectRequest, callback=self.on_rename_object_request
        )
        _event_manager.assign_manager_to_request_type(
            request_type=ClearAllObjectStateRequest, callback=self.on_clear_all_object_state_request
        )

    def on_rename_object_request(self, request: RenameObjectRequest) -> ResultPayload:
        # Does the source object exist?
        if request.object_name == request.requested_name:
            return RenameObjectResultSuccess(
                final_name=request.requested_name,
                result_details=f"Object '{request.requested_name}' already has the requested name",
            )
        source_obj = self.attempt_get_object_by_name(request.object_name)
        if source_obj is None:
            details = f"Attempted to rename object '{request.object_name}', but no object of that name could be found."
            logger.error(details)
            return RenameObjectResultFailure(next_available_name=None, result_details=details)

        # Is there a collision?
        requested_name_obj = self.attempt_get_object_by_name(request.requested_name)
        if requested_name_obj is None:
            final_name = request.requested_name
        else:
            # Collision. Decide what to do.
            next_name = self.generate_name_for_object(
                type_name=source_obj.__class__.__name__, requested_name=request.requested_name
            )

            # Will the requester allow us to use the next closest name available?
            if not request.allow_next_closest_name_available:
                # Not allowed to use it :(
                # Fail it but be nice and offer the next name that WOULD HAVE been available.
                details = f"Attempted to rename object '{request.object_name}' to '{request.requested_name}'. Failed because another object of that name exists. Next available name would have been '{next_name}'."
                logger.error(details)
                return RenameObjectResultFailure(next_available_name=next_name, result_details=details)
            # We'll use the next available name.
            final_name = next_name

            # Let the object's manager know. TODO: https://github.com/griptape-ai/griptape-nodes/issues/869
        match source_obj:
            case ControlFlow():
                GriptapeNodes.FlowManager().handle_flow_rename(old_name=request.object_name, new_name=final_name)
            case BaseNode():
                GriptapeNodes.NodeManager().handle_node_rename(old_name=request.object_name, new_name=final_name)
            case _:
                details = f"Attempted to rename an object named '{request.object_name}', but that object wasn't of a type supported for rename."
                logger.error(details)
                return RenameObjectResultFailure(next_available_name=None, result_details=details)

        # Update the object table.
        self._name_to_objects[final_name] = source_obj
        del self._name_to_objects[request.object_name]

        details = f"Successfully renamed object '{request.object_name}' to '{final_name}`."
        log_level = logging.DEBUG
        if final_name != request.requested_name:
            details += " WARNING: Originally requested the name '{request.requested_name}', but that was taken."
            log_level = logging.WARNING
        if log_level == logging.WARNING:
            result_details = ResultDetails(message=details, level=logging.WARNING)
        else:
            result_details = details
        return RenameObjectResultSuccess(final_name=final_name, result_details=result_details)

    async def on_clear_all_object_state_request(self, request: ClearAllObjectStateRequest) -> ResultPayload:
        if not request.i_know_what_im_doing:
            details = "Attempted to clear all object state and delete everything. Failed because they didn't know what they were doing."
            logger.warning(details)
            return ClearAllObjectStateResultFailure(result_details=details)

        try:
            # Reset global execution state first to eliminate all references before deletion
            GriptapeNodes.FlowManager().reset_global_execution_state()
        except Exception as e:
            details = f"Attempted to reset global execution state. Failed with exception: {e}"
            logger.error(details)
            return ClearAllObjectStateResultFailure(result_details=details)

        # Tear down each active workflow: cancel its running flows, delete its child
        # flows, drain its context substack, pop it. Stack depth is typically 1.
        try:
            context_mgr = GriptapeNodes.ContextManager()
            while context_mgr.has_current_workflow():
                GriptapeNodes.clear_current_workflow_data()
        except Exception as e:
            details = f"Attempted to clear all object state and delete everything. Failed with exception: {e}"
            logger.error(details)
            return ClearAllObjectStateResultFailure(result_details=details)

        if self._name_to_objects:
            details = f"Attempted to clear all object state, but {len(self._name_to_objects)} object(s) remained after workflow teardown."
            logger.error(details)
            return ClearAllObjectStateResultFailure(result_details=details)

        # Clear all local workflow variables
        GriptapeNodes.VariablesManager().on_clear_object_state()
        GriptapeNodes.WorkflowManager().on_clear_object_state()
        # Clear all event suppression
        GriptapeNodes.EventManager().clear_event_suppression()

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
        result = {}

        # Compile pattern if it's a string
        if name and isinstance(name, str):
            name = re.compile(name)

        for key, value in self._name_to_objects.items():
            # Check key pattern if provided
            key_match = True
            if name:
                key_match = bool(name.search(key))

            # Check value type if provided
            value_match = True
            if type:
                value_match = isinstance(value, type)

            # Add to result if both conditions match
            if key_match and value_match:
                result[key] = value

        return result

    def generate_name_for_object(self, type_name: str, requested_name: str | None = None) -> str:
        # Now ensure that we're giving a valid unique name. Here are the rules:
        # 1. If no name was requested, use the type name + first free integer.
        # 2. If a name was requested and no collision, use it as-is.
        # 3. If a name was requested and there IS a collision, check:
        #    a. If name ends in a number, find the FIRST prefix + integer value that isn't a collision.
        #    b. If name does NOT end in a number, use the name + first free integer.

        # We are going in with eyes open that the collision testing is inefficient.
        name_to_return = None
        incremental_prefix = ""

        if requested_name is None:
            # 1. If no name was requested, use the type name + first free integer.
            incremental_prefix = f"{type_name}_"
        elif requested_name not in self._name_to_objects:
            # 2. If a name was requested and no collision, use it as-is.
            name_to_return = requested_name
        else:
            # 3. If a name was requested and there IS a collision, check:
            pattern_match = re.search(r"\d+$", requested_name)
            if pattern_match is not None:
                #    a. If name ends in a number, find the FIRST prefix + integer value that isn't a collision.
                # Ends in a number. Find the FIRST prefix + integer value that isn't a collision.
                start = pattern_match.start()
                incremental_prefix = requested_name[:start]
            else:
                #    b. If name does NOT end in a number, use the name + first free integer.
                incremental_prefix = f"{requested_name}_"

        if name_to_return is None:
            # Do the incremental walk.
            curr_idx = 1
            done = False
            while not done:
                test_name = f"{incremental_prefix}{curr_idx}"
                if test_name not in self._name_to_objects:
                    # Found it.
                    name_to_return = test_name
                    done = True
                else:
                    # Keep going.
                    curr_idx += 1

        if name_to_return is None:
            msg = "Failed to generate a unique name for the object."
            raise ValueError(msg)

        return name_to_return

    def add_object_by_name(self, name: str, obj: object) -> None:
        if name in self._name_to_objects:
            msg = f"Attempted to add an object with name '{name}' but an object with that name already exists. The Object Manager is sacrosanct in this regard."
            raise ValueError(msg)
        self._name_to_objects[name] = obj

    def get_object_by_name(self, name: str) -> object:
        return self._name_to_objects[name]

    def has_object_with_name(self, name: str) -> bool:
        has_it = name in self._name_to_objects
        return has_it

    def attempt_get_object_by_name(self, name: str) -> Any | None:
        return self._name_to_objects.get(name, None)

    def attempt_get_object_by_name_as_type[T](self, name: str, cast_type: type[T]) -> T | None:
        obj = self.attempt_get_object_by_name(name)
        if obj is not None and isinstance(obj, cast_type):
            return obj
        return None

    def del_obj_by_name(self, name: str) -> None:
        # Does the object have any children? delete those
        obj = self._name_to_objects[name]
        if isinstance(obj, BaseNodeElement):
            children = obj.find_elements_by_type(BaseNodeElement)
            for child in children:
                obj.remove_child(child)
                if isinstance(child, BaseNode):
                    GriptapeNodes.handle_request(DeleteNodeRequest(child.name))
                    return
                if isinstance(child, Parameter) and isinstance(obj, BaseNode):
                    GriptapeNodes.handle_request(RemoveParameterFromNodeRequest(child.name, obj.name))
                    return
        del self._name_to_objects[name]
