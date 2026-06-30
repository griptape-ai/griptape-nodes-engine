import logging
from typing import NamedTuple

from griptape_nodes.retained_mode.events.base_events import ResultPayload
from griptape_nodes.retained_mode.events.variable_events import (
    CreateVariableRequest,
    CreateVariableResultFailure,
    CreateVariableResultSuccess,
    DeleteVariableRequest,
    DeleteVariableResultFailure,
    DeleteVariableResultSuccess,
    GetVariableDetailsRequest,
    GetVariableDetailsResultFailure,
    GetVariableDetailsResultSuccess,
    GetVariableRequest,
    GetVariableResultFailure,
    GetVariableResultSuccess,
    GetVariablesRequest,
    GetVariablesResultFailure,
    GetVariablesResultSuccess,
    GetVariableTypeRequest,
    GetVariableTypeResultFailure,
    GetVariableTypeResultSuccess,
    GetVariableValueRequest,
    GetVariableValueResultFailure,
    GetVariableValueResultSuccess,
    HasVariableRequest,
    HasVariableResultFailure,
    HasVariableResultSuccess,
    ListVariablesRequest,
    ListVariablesResultFailure,
    ListVariablesResultSuccess,
    RenameVariableRequest,
    RenameVariableResultFailure,
    RenameVariableResultSuccess,
    SetVariablesRequest,
    SetVariablesResultFailure,
    SetVariablesResultSuccess,
    SetVariableTypeRequest,
    SetVariableTypeResultFailure,
    SetVariableTypeResultSuccess,
    SetVariableValueRequest,
    SetVariableValueResultFailure,
    SetVariableValueResultSuccess,
    VariableDetails,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.event_manager import EventManager
from griptape_nodes.retained_mode.variable_types import FlowVariable, VariableScope

logger = logging.getLogger("griptape_nodes")


class VariableLookupResult(NamedTuple):
    """Result of hierarchical variable lookup."""

    variable: FlowVariable | None
    found_scope: VariableScope | None


class VariablesManager:
    """Manager for variables with scoped access control."""

    def __init__(self, event_manager: EventManager | None = None) -> None:
        # Storage for flow-scoped variables: {flow_name: {variable_name: FlowVariable}}
        self._flow_variables: dict[str, dict[str, FlowVariable]] = {}
        # Storage for global variables: {variable_name: FlowVariable}
        self._global_variables: dict[str, FlowVariable] = {}
        if event_manager is not None:
            event_manager.assign_manager_to_request_type(CreateVariableRequest, self.on_create_variable_request)
            event_manager.assign_manager_to_request_type(GetVariableRequest, self.on_get_variable_request)
            event_manager.assign_manager_to_request_type(GetVariableValueRequest, self.on_get_variable_value_request)
            event_manager.assign_manager_to_request_type(SetVariableValueRequest, self.on_set_variable_value_request)
            event_manager.assign_manager_to_request_type(GetVariableTypeRequest, self.on_get_variable_type_request)
            event_manager.assign_manager_to_request_type(SetVariableTypeRequest, self.on_set_variable_type_request)
            event_manager.assign_manager_to_request_type(DeleteVariableRequest, self.on_delete_variable_request)
            event_manager.assign_manager_to_request_type(RenameVariableRequest, self.on_rename_variable_request)
            event_manager.assign_manager_to_request_type(HasVariableRequest, self.on_has_variable_request)
            event_manager.assign_manager_to_request_type(ListVariablesRequest, self.on_list_variables_request)
            event_manager.assign_manager_to_request_type(
                GetVariableDetailsRequest, self.on_get_variable_details_request
            )
            event_manager.assign_manager_to_request_type(GetVariablesRequest, self.on_get_variables_request)
            event_manager.assign_manager_to_request_type(SetVariablesRequest, self.on_set_variables_request)

    def clear_object_state(self) -> None:
        """Clear all variables."""
        self._flow_variables.clear()
        self._global_variables.clear()

    def _get_starting_flow(self, starting_flow: str | None) -> str:
        """Get the starting flow name, using Context Manager if None."""
        if starting_flow is not None:
            # Validate that the specified flow exists
            flow_manager = GriptapeNodes.FlowManager()
            try:
                flow_manager.get_parent_flow(starting_flow)  # This will raise if flow doesn't exist
            except Exception as e:
                msg = f"Specified starting flow '{starting_flow}' does not exist: {e}"
                raise ValueError(msg) from e
            return starting_flow

        # Get current flow from Context Manager
        context_manager = GriptapeNodes.ContextManager()

        if not context_manager.has_current_flow():
            msg = "No starting flow specified and no current flow in Context Manager"
            raise ValueError(msg)

        return context_manager.get_current_flow().name

    def _get_flow_hierarchy(self, starting_flow: str) -> list[str]:
        """Get the flow hierarchy from starting flow up to root."""
        flow_manager = GriptapeNodes.FlowManager()

        hierarchy = []
        current_flow = starting_flow

        while current_flow:
            hierarchy.append(current_flow)
            try:
                parent = flow_manager.get_parent_flow(current_flow)
                current_flow = parent
            except Exception:
                # No parent flow found, we've reached the root
                break

        return hierarchy

    def _find_variable_in_flow(self, flow_name: str, variable_name: str) -> FlowVariable | None:
        """Find a variable in a specific flow."""
        flow_vars = self._flow_variables.get(flow_name, {})
        return flow_vars.get(variable_name)

    def _find_variable_hierarchical(
        self, starting_flow: str, variable_name: str, lookup_scope: VariableScope
    ) -> VariableLookupResult:
        """Find a variable using hierarchical lookup strategy."""
        match lookup_scope:
            case VariableScope.CURRENT_FLOW_ONLY:
                variable = self._find_variable_in_flow(starting_flow, variable_name)
                found_scope = VariableScope.CURRENT_FLOW_ONLY if variable else None
                return VariableLookupResult(variable=variable, found_scope=found_scope)

            case VariableScope.GLOBAL_ONLY:
                variable = self._global_variables.get(variable_name)
                found_scope = VariableScope.GLOBAL_ONLY if variable else None
                return VariableLookupResult(variable=variable, found_scope=found_scope)

            case VariableScope.HIERARCHICAL:
                # Search through flow hierarchy
                hierarchy = self._get_flow_hierarchy(starting_flow)
                for flow_name in hierarchy:
                    variable = self._find_variable_in_flow(flow_name, variable_name)
                    if variable:
                        found_scope = (
                            VariableScope.CURRENT_FLOW_ONLY
                            if flow_name == starting_flow
                            else VariableScope.HIERARCHICAL
                        )
                        return VariableLookupResult(variable=variable, found_scope=found_scope)

                # Check global variables as fallback
                variable = self._global_variables.get(variable_name)
                found_scope = VariableScope.GLOBAL_ONLY if variable else None
                return VariableLookupResult(variable=variable, found_scope=found_scope)

            case VariableScope.ALL:
                # This is primarily for ListVariables - just search current flow for now
                variable = self._find_variable_in_flow(starting_flow, variable_name)
                found_scope = VariableScope.CURRENT_FLOW_ONLY if variable else None
                return VariableLookupResult(variable=variable, found_scope=found_scope)

            case _:
                msg = f"Attempted to find variable '{variable_name}' from starting flow '{starting_flow}', but encountered an unknown/unexpected variable scope '{lookup_scope.value}'"
                raise ValueError(msg)

    def on_create_variable_request(self, request: CreateVariableRequest) -> ResultPayload:
        """Create a new variable."""
        if request.is_global:
            # Check for name collision in global variables
            if request.name in self._global_variables:
                return CreateVariableResultFailure(
                    result_details=f"Attempted to create a global variable named '{request.name}'. Failed because a variable with that name already exists."
                )

            # Create global variable
            variable = FlowVariable(
                name=request.name,
                owning_flow_name=None,
                type=request.type,
                value=request.value,
            )

            self._global_variables[request.name] = variable
            return CreateVariableResultSuccess(result_details=f"Successfully created global variable '{request.name}'.")

        # Get the target flow
        try:
            target_flow = self._get_starting_flow(request.owning_flow)
        except ValueError as e:
            return CreateVariableResultFailure(
                result_details=f"Attempted to create variable '{request.name}'. Failed to determine target flow: {e}"
            )

        # Initialize flow storage if needed
        if target_flow not in self._flow_variables:
            self._flow_variables[target_flow] = {}

        # Check for name collision in target flow
        if request.name in self._flow_variables[target_flow]:
            return CreateVariableResultFailure(
                result_details=f"Attempted to create a variable named '{request.name}' in flow '{target_flow}'. Failed because a variable with that name already exists."
            )

        # Create flow-scoped variable
        variable = FlowVariable(
            name=request.name,
            owning_flow_name=target_flow,
            type=request.type,
            value=request.value,
        )

        self._flow_variables[target_flow][request.name] = variable
        return CreateVariableResultSuccess(
            result_details=f"Successfully created variable '{request.name}' in flow '{target_flow}'."
        )

    def on_get_variable_request(self, request: GetVariableRequest) -> ResultPayload:
        """Get a full variable by name."""
        try:
            starting_flow = self._get_starting_flow(request.starting_flow)
        except ValueError as e:
            return GetVariableResultFailure(
                result_details=f"Attempted to get variable '{request.name}'. Failed to determine starting flow: {e}"
            )

        result = self._find_variable_hierarchical(starting_flow, request.name, request.lookup_scope)

        if not result.variable:
            return GetVariableResultFailure(
                result_details=f"Attempted to get variable '{request.name}'. Failed because no such variable could be found."
            )

        return GetVariableResultSuccess(
            variable=result.variable, result_details=f"Successfully retrieved variable '{request.name}'."
        )

    def on_get_variable_value_request(self, request: GetVariableValueRequest) -> ResultPayload:
        """Get the value of a variable."""
        try:
            starting_flow = self._get_starting_flow(request.starting_flow)
        except ValueError as e:
            return GetVariableValueResultFailure(
                result_details=f"Attempted to get value for variable '{request.name}'. Failed to determine starting flow: {e}"
            )

        result = self._find_variable_hierarchical(starting_flow, request.name, request.lookup_scope)

        if not result.variable:
            return GetVariableValueResultFailure(
                result_details=f"Attempted to get value for variable '{request.name}'. Failed because no such variable could be found."
            )

        return GetVariableValueResultSuccess(
            value=result.variable.value, result_details=f"Successfully retrieved value for variable '{request.name}'."
        )

    def on_set_variable_value_request(self, request: SetVariableValueRequest) -> ResultPayload:
        """Set the value of an existing variable."""
        try:
            starting_flow = self._get_starting_flow(request.starting_flow)
        except ValueError as e:
            return SetVariableValueResultFailure(
                result_details=f"Attempted to set value for variable '{request.name}'. Failed to determine starting flow: {e}"
            )

        result = self._find_variable_hierarchical(starting_flow, request.name, request.lookup_scope)

        if not result.variable:
            return SetVariableValueResultFailure(
                result_details=f"Attempted to set value for variable '{request.name}'. Failed because no such variable could be found."
            )

        result.variable.value = request.value
        return SetVariableValueResultSuccess(result_details=f"Successfully set value for variable '{request.name}'.")

    def on_get_variable_type_request(self, request: GetVariableTypeRequest) -> ResultPayload:
        """Get the type of a variable."""
        try:
            starting_flow = self._get_starting_flow(request.starting_flow)
        except ValueError as e:
            return GetVariableTypeResultFailure(
                result_details=f"Attempted to get type for variable '{request.name}'. Failed to determine starting flow: {e}"
            )

        result = self._find_variable_hierarchical(starting_flow, request.name, request.lookup_scope)

        if not result.variable:
            return GetVariableTypeResultFailure(
                result_details=f"Attempted to get type for variable '{request.name}'. Failed because no such variable could be found."
            )

        return GetVariableTypeResultSuccess(
            type=result.variable.type, result_details=f"Successfully retrieved type for variable '{request.name}'."
        )

    def on_set_variable_type_request(self, request: SetVariableTypeRequest) -> ResultPayload:
        """Set the type of an existing variable."""
        try:
            starting_flow = self._get_starting_flow(request.starting_flow)
        except ValueError as e:
            return SetVariableTypeResultFailure(
                result_details=f"Attempted to set type for variable '{request.name}'. Failed to determine starting flow: {e}"
            )

        result = self._find_variable_hierarchical(starting_flow, request.name, request.lookup_scope)

        if not result.variable:
            return SetVariableTypeResultFailure(
                result_details=f"Attempted to set type for variable '{request.name}'. Failed because no such variable could be found."
            )

        result.variable.type = request.type
        return SetVariableTypeResultSuccess(
            result_details=f"Successfully set type for variable '{request.name}' to '{request.type}'."
        )

    def on_delete_variable_request(self, request: DeleteVariableRequest) -> ResultPayload:
        """Delete a variable."""
        try:
            starting_flow = self._get_starting_flow(request.starting_flow)
        except ValueError as e:
            return DeleteVariableResultFailure(
                result_details=f"Attempted to delete variable '{request.name}'. Failed to determine starting flow: {e}"
            )

        result = self._find_variable_hierarchical(starting_flow, request.name, request.lookup_scope)

        if not result.variable:
            return DeleteVariableResultFailure(
                result_details=f"Attempted to delete variable '{request.name}'. Failed because no such variable could be found."
            )

        variable = result.variable

        # Remove from appropriate storage based on owning flow
        if variable.owning_flow_name is None:
            # Global variable
            del self._global_variables[variable.name]
        else:
            # Flow-scoped variable
            flow_vars = self._flow_variables.get(variable.owning_flow_name, {})
            if variable.name in flow_vars:
                del flow_vars[variable.name]

        return DeleteVariableResultSuccess(result_details=f"Successfully deleted variable '{request.name}'.")

    def on_rename_variable_request(self, request: RenameVariableRequest) -> ResultPayload:
        """Rename a variable."""
        try:
            starting_flow = self._get_starting_flow(request.starting_flow)
        except ValueError as e:
            return RenameVariableResultFailure(
                result_details=f"Attempted to rename variable '{request.name}'. Failed to determine starting flow: {e}"
            )

        result = self._find_variable_hierarchical(starting_flow, request.name, request.lookup_scope)

        if not result.variable:
            return RenameVariableResultFailure(
                result_details=f"Attempted to rename variable '{request.name}'. Failed because no such variable could be found."
            )

        variable = result.variable

        # Check for name collision with new name in the same scope
        new_name_result = self._find_variable_hierarchical(starting_flow, request.new_name, request.lookup_scope)
        if new_name_result.variable and new_name_result.variable.name != variable.name:
            return RenameVariableResultFailure(
                result_details=f"Attempted to rename variable '{request.name}' to '{request.new_name}'. Failed because a variable with that name already exists."
            )

        # Update the variable name and storage key
        old_name = variable.name
        variable.name = request.new_name

        # Update in appropriate storage based on owning flow
        if variable.owning_flow_name is None:
            # Global variable
            del self._global_variables[old_name]
            self._global_variables[request.new_name] = variable
        else:
            # Flow-scoped variable
            flow_vars = self._flow_variables.get(variable.owning_flow_name, {})
            if old_name in flow_vars:
                del flow_vars[old_name]
                flow_vars[request.new_name] = variable

        return RenameVariableResultSuccess(
            result_details=f"Successfully renamed variable '{old_name}' to '{request.new_name}'."
        )

    def on_has_variable_request(self, request: HasVariableRequest) -> ResultPayload:
        """Check if a variable exists."""
        try:
            starting_flow = self._get_starting_flow(request.starting_flow)
        except ValueError as e:
            return HasVariableResultFailure(
                result_details=f"Attempted to check existence of variable '{request.name}'. Failed to determine starting flow: {e}"
            )

        result = self._find_variable_hierarchical(starting_flow, request.name, request.lookup_scope)
        exists = result.variable is not None

        return HasVariableResultSuccess(
            exists=exists,
            found_scope=result.found_scope,
            result_details=f"Successfully checked existence of variable '{request.name}': {'exists' if exists else 'not found'}.",
        )

    def _get_variables_by_scope(self, starting_flow: str, lookup_scope: VariableScope) -> list[FlowVariable]:
        """Get variables for the specified scope."""
        match lookup_scope:
            case VariableScope.CURRENT_FLOW_ONLY:
                if starting_flow in self._flow_variables:
                    return list(self._flow_variables[starting_flow].values())
                return []

            case VariableScope.GLOBAL_ONLY:
                return list(self._global_variables.values())

            case VariableScope.HIERARCHICAL:
                return self._get_hierarchical_variables(starting_flow)

            case VariableScope.ALL:
                return self._get_all_variables()

            case _:
                msg = f"Attempted to get variables from starting flow '{starting_flow}', but encountered an unknown/unexpected variable scope '{lookup_scope.value}'"
                raise ValueError(msg)

    def _get_hierarchical_variables(self, starting_flow: str) -> list[FlowVariable]:
        """Get variables using hierarchical lookup with shadowing.

        Variable shadowing behavior:
        - Child flow variables shadow (hide) parent flow variables with same name
        - Flow variables shadow global variables with same name

        Example:
        - Global: user_id = "global_user"
        - Parent flow: user_id = "parent_user"
        - Child flow: user_id = "child_user"
        Result from child flow: only user_id = "child_user" (others are shadowed)
        """
        hierarchy = self._get_flow_hierarchy(starting_flow)
        seen_names = set()
        variables = []

        # Add variables from flows (child to parent to implement shadowing)
        for flow_name in hierarchy:
            flow_vars = self._flow_variables.get(flow_name, {})
            for var in flow_vars.values():
                if var.name not in seen_names:
                    variables.append(var)
                    seen_names.add(var.name)

        # Add global variables (lowest priority, can be shadowed by flow variables)
        variables.extend(var for var in self._global_variables.values() if var.name not in seen_names)

        return variables

    def _get_all_variables(self) -> list[FlowVariable]:
        """Get all variables from all flows for GUI enumeration.

        Note: This returns ALL variables without shadowing - variables with the same name
        from different flows/scopes will all be included in the result.
        """
        variables = []

        # Add all flow variables (no shadowing - include all)
        for flow_vars in self._flow_variables.values():
            variables.extend(flow_vars.values())

        # Add all global variables
        variables.extend(self._global_variables.values())

        return variables

    def get_variables_for_macro_resolution(self, flow_name: str) -> dict[str, str | int]:
        """Return a name→value dict of variables visible from flow_name, suitable for macro resolution.

        Only str and int values are included. None values and non-str/int values are omitted.
        Uses hierarchical scope so child-flow variables shadow parent-flow variables.
        """
        variables = self._get_hierarchical_variables(flow_name)
        result: dict[str, str | int] = {}
        for var in variables:
            if var.value is None:
                continue
            # bool is a subclass of int, but substituting True/False as "True"/"False" is
            # almost certainly wrong. Exclude it explicitly so bool variables are silently
            # skipped rather than treated as integer 1/0.
            if isinstance(var.value, str | int) and not isinstance(var.value, bool):
                result[var.name] = var.value
        return result

    def on_list_variables_request(self, request: ListVariablesRequest) -> ResultPayload:
        """List all variables in the specified scope."""
        try:
            starting_flow = self._get_starting_flow(request.starting_flow)
        except ValueError as e:
            return ListVariablesResultFailure(
                result_details=f"Attempted to list variables. Failed to determine starting flow: {e}"
            )

        variables = self._get_variables_by_scope(starting_flow, request.lookup_scope)

        # Sort by name for consistent output
        variables = sorted(variables, key=lambda v: v.name)
        return ListVariablesResultSuccess(
            variables=variables, result_details=f"Successfully listed {len(variables)} variables."
        )

    def on_get_variables_request(self, request: GetVariablesRequest) -> ResultPayload:
        """Get all variable values visible from the starting flow."""
        try:
            starting_flow = self._get_starting_flow(request.starting_flow)
        except ValueError as e:
            return GetVariablesResultFailure(
                result_details=f"Attempted to get variables. Failed to determine starting flow: {e}"
            )

        variables = self._get_variables_by_scope(starting_flow, request.lookup_scope)
        result = {v.name: v.value for v in variables}
        return GetVariablesResultSuccess(
            variables=result, result_details=f"Successfully retrieved {len(result)} variable(s)."
        )

    def on_set_variables_request(self, request: SetVariablesRequest) -> ResultPayload:
        """Set multiple variable values atomically (all-or-nothing)."""
        try:
            starting_flow = self._get_starting_flow(request.starting_flow)
        except ValueError as e:
            return SetVariablesResultFailure(
                result_details=f"Attempted to set variables. Failed to determine starting flow: {e}"
            )

        # Validate all variables exist before writing any (all-or-nothing semantics).
        found: dict[str, FlowVariable] = {}
        missing: list[str] = []
        for name in request.variables:
            lookup = self._find_variable_hierarchical(starting_flow, name, request.lookup_scope)
            if lookup.variable is None:
                missing.append(name)
            else:
                found[name] = lookup.variable

        if missing:
            return SetVariablesResultFailure(
                result_details=f"Attempted to set variables. Failed because variables not found: {missing!r}"
            )

        for name, value in request.variables.items():
            found[name].value = value

        return SetVariablesResultSuccess(result_details=f"Successfully set {len(request.variables)} variable(s).")

    def on_get_variable_details_request(self, request: GetVariableDetailsRequest) -> ResultPayload:
        """Get variable details (metadata only, no heavy values)."""
        try:
            starting_flow = self._get_starting_flow(request.starting_flow)
        except ValueError as e:
            return GetVariableDetailsResultFailure(
                result_details=f"Attempted to get details for variable '{request.name}'. Failed to determine starting flow: {e}"
            )

        result = self._find_variable_hierarchical(starting_flow, request.name, request.lookup_scope)

        if not result.variable:
            return GetVariableDetailsResultFailure(
                result_details=f"Attempted to get details for variable '{request.name}'. Failed because no such variable could be found."
            )

        variable = result.variable
        details = VariableDetails(name=variable.name, owning_flow_name=variable.owning_flow_name, type=variable.type)
        return GetVariableDetailsResultSuccess(
            details=details, result_details=f"Successfully retrieved details for variable '{request.name}'."
        )

    def _find_variable_by_name(self, name: str) -> FlowVariable | None:
        """Find a variable by name in current flow context (legacy compatibility)."""
        try:
            starting_flow = self._get_starting_flow(None)
        except ValueError:
            return None

        result = self._find_variable_hierarchical(starting_flow, name, VariableScope.HIERARCHICAL)
        return result.variable
