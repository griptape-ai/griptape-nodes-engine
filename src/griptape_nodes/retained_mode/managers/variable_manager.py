import logging
from typing import Any, NamedTuple

from griptape_nodes.retained_mode.events.base_events import ResultPayload
from griptape_nodes.retained_mode.events.project_events import (
    GetProjectVariableRequest,
    GetProjectVariableResultSuccess,
    ListProjectVariablesRequest,
    ListProjectVariablesResultSuccess,
)
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
    ListSubstitutablesRequest,
    ListSubstitutablesResultFailure,
    ListSubstitutablesResultSuccess,
    ListVariablesRequest,
    ListVariablesResultFailure,
    ListVariablesResultSuccess,
    RenameVariableRequest,
    RenameVariableResultFailure,
    RenameVariableResultSuccess,
    ResolveSubstitutionRequest,
    ResolveSubstitutionResultFailure,
    ResolveSubstitutionResultSuccess,
    SetVariablesRequest,
    SetVariablesResultFailure,
    SetVariablesResultSuccess,
    SetVariableTypeRequest,
    SetVariableTypeResultFailure,
    SetVariableTypeResultSuccess,
    SetVariableValueRequest,
    SetVariableValueResultFailure,
    SetVariableValueResultSuccess,
    Substitutable,
    SubstitutableSource,
    VariableDetails,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.event_manager import EventManager
from griptape_nodes.retained_mode.variable_types import (
    FlowVariable,
    VariableLayer,
    VariablePermission,
    VariableScope,
)

logger = logging.getLogger("griptape_nodes")


class VariableLookupResult(NamedTuple):
    """Result of hierarchical variable lookup."""

    variable: FlowVariable | None
    found_scope: VariableScope | None


class VariablesManager:
    """Manager for variables with scoped access control."""

    def __init__(self, event_manager: EventManager | None = None) -> None:
        # Storage for flow-scoped variables: one VariableLayer per flow, lazily created.
        self._flow_layers: dict[str, VariableLayer] = {}
        # Storage for global variables: single VariableLayer.
        self._global_layer: VariableLayer = VariableLayer()
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
            event_manager.assign_manager_to_request_type(
                ResolveSubstitutionRequest, self.on_resolve_substitution_request
            )
            event_manager.assign_manager_to_request_type(ListSubstitutablesRequest, self.on_list_substitutables_request)
            event_manager.assign_manager_to_request_type(SetVariablesRequest, self.on_set_variables_request)

    def clear_object_state(self) -> None:
        """Clear all flow and global variables. Project layers are owned by ProjectManager."""
        self._flow_layers.clear()
        self._global_layer.clear()

    def _get_or_create_flow_layer(self, flow_name: str) -> VariableLayer:
        """Return the flow's VariableLayer, lazily creating an empty one on first touch."""
        layer = self._flow_layers.get(flow_name)
        if layer is None:
            layer = VariableLayer()
            self._flow_layers[flow_name] = layer
        return layer

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
        layer = self._flow_layers.get(flow_name)
        if layer is None:
            return None
        return layer.get(variable_name)

    def _get_project_variable(self, name: str, project_id: str | None) -> FlowVariable | None:
        """Fetch a single project-layer variable via ProjectManager.

        Returns None if the project isn't loaded, the name isn't defined in that
        project, or the variable's resolver raised. Callers who need to distinguish
        those cases should issue GetProjectVariableRequest directly.
        """
        result = GriptapeNodes.handle_request(GetProjectVariableRequest(name=name, project_id=project_id))
        if not isinstance(result, GetProjectVariableResultSuccess):
            return None
        return result.variable

    def _list_project_variable_names(self, project_id: str | None) -> list[str]:
        """List every variable name defined in a project's variable layer (metadata only)."""
        result = GriptapeNodes.handle_request(ListProjectVariablesRequest(project_id=project_id))
        if not isinstance(result, ListProjectVariablesResultSuccess):
            return []
        return [v.name for v in result.variables]

    def _collect_resolvable_project_variables(self, project_id: str | None, seen: set[str]) -> list[FlowVariable]:
        """List project variables via events, resolving each name and skipping resolution failures.

        Mutates `seen` to include each collected name so downstream layers can shadow correctly.
        Preserves the pre-layering silent-skip behavior of get_project_substitution_variables
        for bulk enumeration: variables whose resolver raises today (e.g. workflow_dir before
        the workflow is saved) are omitted from the returned list.
        """
        collected: list[FlowVariable] = []
        for name in self._list_project_variable_names(project_id):
            if name in seen:
                continue
            variable = self._get_project_variable(name, project_id)
            if variable is None:
                continue
            collected.append(variable)
            seen.add(name)
        return collected

    def _find_variable_hierarchical(  # noqa: C901, PLR0911
        self, starting_flow: str, variable_name: str, lookup_scope: VariableScope, project_id: str | None
    ) -> VariableLookupResult:
        """Find a variable using the requested layering strategy."""
        match lookup_scope:
            case VariableScope.CURRENT_FLOW_ONLY:
                variable = self._find_variable_in_flow(starting_flow, variable_name)
                found_scope = VariableScope.CURRENT_FLOW_ONLY if variable else None
                return VariableLookupResult(variable=variable, found_scope=found_scope)

            case VariableScope.PROJECT_ONLY:
                variable = self._get_project_variable(variable_name, project_id)
                found_scope = VariableScope.PROJECT_ONLY if variable else None
                return VariableLookupResult(variable=variable, found_scope=found_scope)

            case VariableScope.GLOBAL_ONLY:
                variable = self._global_layer.get(variable_name)
                found_scope = VariableScope.GLOBAL_ONLY if variable else None
                return VariableLookupResult(variable=variable, found_scope=found_scope)

            case VariableScope.HIERARCHICAL:
                # Flow chain → project layer → global.
                for flow_name in self._get_flow_hierarchy(starting_flow):
                    variable = self._find_variable_in_flow(flow_name, variable_name)
                    if variable:
                        found_scope = (
                            VariableScope.CURRENT_FLOW_ONLY
                            if flow_name == starting_flow
                            else VariableScope.HIERARCHICAL
                        )
                        return VariableLookupResult(variable=variable, found_scope=found_scope)

                variable = self._get_project_variable(variable_name, project_id)
                if variable is not None:
                    return VariableLookupResult(variable=variable, found_scope=VariableScope.PROJECT_ONLY)

                variable = self._global_layer.get(variable_name)
                found_scope = VariableScope.GLOBAL_ONLY if variable else None
                return VariableLookupResult(variable=variable, found_scope=found_scope)

            case VariableScope.HIERARCHICAL_FROM_PROJECT:
                variable = self._get_project_variable(variable_name, project_id)
                if variable is not None:
                    return VariableLookupResult(variable=variable, found_scope=VariableScope.PROJECT_ONLY)

                variable = self._global_layer.get(variable_name)
                found_scope = VariableScope.GLOBAL_ONLY if variable else None
                return VariableLookupResult(variable=variable, found_scope=found_scope)

            case VariableScope.ALL:
                # ALL is primarily an enumeration scope. For single-name lookup, treat it as CURRENT_FLOW_ONLY.
                variable = self._find_variable_in_flow(starting_flow, variable_name)
                found_scope = VariableScope.CURRENT_FLOW_ONLY if variable else None
                return VariableLookupResult(variable=variable, found_scope=found_scope)

            case _:
                msg = (
                    f"Attempted to find variable '{variable_name}' from starting flow '{starting_flow}', "
                    f"but encountered an unknown/unexpected variable scope '{lookup_scope.value}'"
                )
                raise ValueError(msg)

    @staticmethod
    def _refuse_write_if_read_only(variable: FlowVariable, verb: str) -> str | None:
        """If the variable is READ_ONLY, return an artist-comprehensible failure message. Else None."""
        if variable.permission is VariablePermission.READ_ONLY:
            return (
                f"Attempted to {verb} variable '{variable.name}'. Failed due to it being READ_ONLY — "
                f"project builtins and directory macros can't be modified through this request; modify "
                f"the project template via SaveProjectTemplateRequest instead."
            )
        return None

    def on_create_variable_request(self, request: CreateVariableRequest) -> ResultPayload:
        """Create a new variable."""
        if request.is_global:
            # Check for name collision in global variables
            if self._global_layer.has(request.name):
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

            self._global_layer.set(variable)
            return CreateVariableResultSuccess(result_details=f"Successfully created global variable '{request.name}'.")

        # Get the target flow
        try:
            target_flow = self._get_starting_flow(request.owning_flow)
        except ValueError as e:
            return CreateVariableResultFailure(
                result_details=f"Attempted to create variable '{request.name}'. Failed to determine target flow: {e}"
            )

        flow_layer = self._get_or_create_flow_layer(target_flow)

        # Check for name collision in target flow
        if flow_layer.has(request.name):
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

        flow_layer.set(variable)
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

        result = self._find_variable_hierarchical(starting_flow, request.name, request.lookup_scope, request.project_id)

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

        result = self._find_variable_hierarchical(starting_flow, request.name, request.lookup_scope, request.project_id)

        if not result.variable:
            return GetVariableValueResultFailure(
                result_details=f"Attempted to get value for variable '{request.name}'. Failed because no such variable could be found."
            )

        return GetVariableValueResultSuccess(
            value=result.variable.value, result_details=f"Successfully retrieved value for variable '{request.name}'."
        )

    def on_set_variable_value_request(self, request: SetVariableValueRequest) -> ResultPayload:
        """Set the value of an existing variable.

        Refuses writes to READ_ONLY variables (project builtins, template directories).
        """
        try:
            starting_flow = self._get_starting_flow(request.starting_flow)
        except ValueError as e:
            return SetVariableValueResultFailure(
                result_details=f"Attempted to set value for variable '{request.name}'. Failed to determine starting flow: {e}"
            )

        result = self._find_variable_hierarchical(starting_flow, request.name, request.lookup_scope, request.project_id)

        if not result.variable:
            return SetVariableValueResultFailure(
                result_details=f"Attempted to set value for variable '{request.name}'. Failed because no such variable could be found."
            )

        refusal = self._refuse_write_if_read_only(result.variable, verb="set the value of")
        if refusal is not None:
            return SetVariableValueResultFailure(result_details=refusal)

        result.variable.value = request.value
        self._unresolve_nodes_referencing_variables([request.name])
        return SetVariableValueResultSuccess(result_details=f"Successfully set value for variable '{request.name}'.")

    def on_get_variable_type_request(self, request: GetVariableTypeRequest) -> ResultPayload:
        """Get the type of a variable."""
        try:
            starting_flow = self._get_starting_flow(request.starting_flow)
        except ValueError as e:
            return GetVariableTypeResultFailure(
                result_details=f"Attempted to get type for variable '{request.name}'. Failed to determine starting flow: {e}"
            )

        result = self._find_variable_hierarchical(starting_flow, request.name, request.lookup_scope, request.project_id)

        if not result.variable:
            return GetVariableTypeResultFailure(
                result_details=f"Attempted to get type for variable '{request.name}'. Failed because no such variable could be found."
            )

        return GetVariableTypeResultSuccess(
            type=result.variable.type, result_details=f"Successfully retrieved type for variable '{request.name}'."
        )

    def on_set_variable_type_request(self, request: SetVariableTypeRequest) -> ResultPayload:
        """Set the type of an existing variable.

        Refuses type changes on READ_ONLY variables.
        """
        try:
            starting_flow = self._get_starting_flow(request.starting_flow)
        except ValueError as e:
            return SetVariableTypeResultFailure(
                result_details=f"Attempted to set type for variable '{request.name}'. Failed to determine starting flow: {e}"
            )

        result = self._find_variable_hierarchical(starting_flow, request.name, request.lookup_scope, request.project_id)

        if not result.variable:
            return SetVariableTypeResultFailure(
                result_details=f"Attempted to set type for variable '{request.name}'. Failed because no such variable could be found."
            )

        refusal = self._refuse_write_if_read_only(result.variable, verb="set the type of")
        if refusal is not None:
            return SetVariableTypeResultFailure(result_details=refusal)

        result.variable.type = request.type
        return SetVariableTypeResultSuccess(
            result_details=f"Successfully set type for variable '{request.name}' to '{request.type}'."
        )

    def on_delete_variable_request(self, request: DeleteVariableRequest) -> ResultPayload:
        """Delete a variable.

        Refuses deletion of READ_ONLY variables.
        """
        try:
            starting_flow = self._get_starting_flow(request.starting_flow)
        except ValueError as e:
            return DeleteVariableResultFailure(
                result_details=f"Attempted to delete variable '{request.name}'. Failed to determine starting flow: {e}"
            )

        result = self._find_variable_hierarchical(starting_flow, request.name, request.lookup_scope, request.project_id)

        if not result.variable:
            return DeleteVariableResultFailure(
                result_details=f"Attempted to delete variable '{request.name}'. Failed because no such variable could be found."
            )

        refusal = self._refuse_write_if_read_only(result.variable, verb="delete")
        if refusal is not None:
            return DeleteVariableResultFailure(result_details=refusal)

        variable = result.variable

        # Remove from appropriate storage based on owning flow
        if variable.owning_flow_name is None:
            # Global variable
            self._global_layer.delete(variable.name)
        else:
            # Flow-scoped variable
            flow_layer = self._flow_layers.get(variable.owning_flow_name)
            if flow_layer is not None and flow_layer.has(variable.name):
                flow_layer.delete(variable.name)

        return DeleteVariableResultSuccess(result_details=f"Successfully deleted variable '{request.name}'.")

    def on_rename_variable_request(self, request: RenameVariableRequest) -> ResultPayload:
        """Rename a variable.

        Refuses renaming of READ_ONLY variables.
        """
        try:
            starting_flow = self._get_starting_flow(request.starting_flow)
        except ValueError as e:
            return RenameVariableResultFailure(
                result_details=f"Attempted to rename variable '{request.name}'. Failed to determine starting flow: {e}"
            )

        result = self._find_variable_hierarchical(starting_flow, request.name, request.lookup_scope, request.project_id)

        if not result.variable:
            return RenameVariableResultFailure(
                result_details=f"Attempted to rename variable '{request.name}'. Failed because no such variable could be found."
            )

        refusal = self._refuse_write_if_read_only(result.variable, verb="rename")
        if refusal is not None:
            return RenameVariableResultFailure(result_details=refusal)

        variable = result.variable

        # Check for name collision with new name in the same scope
        new_name_result = self._find_variable_hierarchical(
            starting_flow, request.new_name, request.lookup_scope, request.project_id
        )
        if new_name_result.variable and new_name_result.variable.name != variable.name:
            return RenameVariableResultFailure(
                result_details=f"Attempted to rename variable '{request.name}' to '{request.new_name}'. Failed because a variable with that name already exists."
            )

        # Update the variable name and storage key
        old_name = variable.name

        # Update in appropriate storage based on owning flow
        if variable.owning_flow_name is None:
            # Global variable
            self._global_layer.rename(old_name, request.new_name)
        else:
            # Flow-scoped variable
            flow_layer = self._flow_layers.get(variable.owning_flow_name)
            if flow_layer is not None and flow_layer.has(old_name):
                flow_layer.rename(old_name, request.new_name)

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

        result = self._find_variable_hierarchical(starting_flow, request.name, request.lookup_scope, request.project_id)
        exists = result.variable is not None

        return HasVariableResultSuccess(
            exists=exists,
            found_scope=result.found_scope,
            result_details=f"Successfully checked existence of variable '{request.name}': {'exists' if exists else 'not found'}.",
        )

    def _get_variables_by_scope(  # noqa: PLR0911
        self, starting_flow: str, lookup_scope: VariableScope, project_id: str | None
    ) -> list[FlowVariable]:
        """Get variables for the specified scope."""
        match lookup_scope:
            case VariableScope.CURRENT_FLOW_ONLY:
                layer = self._flow_layers.get(starting_flow)
                if layer is None:
                    return []
                return layer.list()

            case VariableScope.PROJECT_ONLY:
                return self._collect_resolvable_project_variables(project_id, set())

            case VariableScope.GLOBAL_ONLY:
                return self._global_layer.list()

            case VariableScope.HIERARCHICAL:
                return self._get_hierarchical_variables(starting_flow, project_id)

            case VariableScope.HIERARCHICAL_FROM_PROJECT:
                seen: set[str] = set()
                result = self._collect_resolvable_project_variables(project_id, seen)
                result.extend(v for v in self._global_layer.list() if v.name not in seen)
                return result

            case VariableScope.ALL:
                return self._get_all_variables(project_id)

            case _:
                msg = f"Attempted to get variables from starting flow '{starting_flow}', but encountered an unknown/unexpected variable scope '{lookup_scope.value}'"
                raise ValueError(msg)

    def _get_hierarchical_variables(self, starting_flow: str, project_id: str | None) -> list[FlowVariable]:
        """Get variables using hierarchical lookup with shadowing.

        Variable shadowing precedence (innermost wins):
        - Child flow variables shadow ancestor flow variables of the same name
        - Flow variables shadow project layer entries of the same name
        - Project layer entries shadow global variables of the same name
        """
        hierarchy = self._get_flow_hierarchy(starting_flow)
        seen_names: set[str] = set()
        variables: list[FlowVariable] = []

        # Flow ancestry (innermost first)
        for flow_name in hierarchy:
            flow_layer = self._flow_layers.get(flow_name)
            if flow_layer is None:
                continue
            for var in flow_layer.list():
                if var.name not in seen_names:
                    variables.append(var)
                    seen_names.add(var.name)

        # Project layer (shadows global, shadowed by flow)
        variables.extend(self._collect_resolvable_project_variables(project_id, seen_names))

        # Global layer (lowest priority)
        variables.extend(var for var in self._global_layer.list() if var.name not in seen_names)

        return variables

    def _get_all_variables(self, project_id: str | None) -> list[FlowVariable]:
        """Get all variables from every layer for GUI enumeration.

        Note: This returns ALL variables without shadowing - variables with the same
        name from different flows / project / global will all be included.
        Project entries whose resolvers currently raise are omitted.
        """
        variables: list[FlowVariable] = []

        for flow_layer in self._flow_layers.values():
            variables.extend(flow_layer.list())

        # Project layer entries — silent-skip resolution failures for enumeration.
        variables.extend(self._collect_resolvable_project_variables(project_id, set()))

        variables.extend(self._global_layer.list())

        return variables

    def on_list_variables_request(self, request: ListVariablesRequest) -> ResultPayload:
        """List all variables in the specified scope."""
        try:
            starting_flow = self._get_starting_flow(request.starting_flow)
        except ValueError as e:
            return ListVariablesResultFailure(
                result_details=f"Attempted to list variables. Failed to determine starting flow: {e}"
            )

        variables = self._get_variables_by_scope(starting_flow, request.lookup_scope, request.project_id)

        # Sort by name for consistent output
        variables = sorted(variables, key=lambda v: v.name)
        return ListVariablesResultSuccess(
            variables=variables, result_details=f"Successfully listed {len(variables)} variables."
        )

    def on_list_substitutables_request(self, request: ListSubstitutablesRequest) -> ResultPayload:
        """List all values available for {VAR} substitution, unified across sources.

        Uses layered resolution — the same walk that ResolveSubstitutionRequest uses —
        so the picker and the resolver always agree on precedence. Each entry's
        source and read_only flag come from the variable's permission and type.
        """
        # Lazy import to avoid circular dependency between retained_mode and exe_types.
        from griptape_nodes.exe_types.variable_resolver import VariableResolver

        try:
            starting_flow = self._get_starting_flow(request.starting_flow)
        except ValueError as e:
            return ListSubstitutablesResultFailure(
                result_details=f"Attempted to list substitutables. Failed to determine starting flow: {e}"
            )

        variables = self._get_variables_by_scope(starting_flow, request.lookup_scope, request.project_id)
        # Names owned by the project layer — used to tag source=MACRO. Project variables
        # arrive here as plain FlowVariables (unwrapped at the request boundary), so we
        # can't use isinstance to distinguish them.
        project_names = set(self._list_project_variable_names(request.project_id))

        # Only str/int values (excluding bool) can actually substitute into {VAR} tokens.
        substitutables: list[Substitutable] = []
        for variable in variables:
            try:
                value = variable.value
            except (RuntimeError, NotImplementedError, ValueError) as e:
                logger.debug("Skipping variable %r whose value cannot be resolved: %s", variable.name, e)
                continue
            filtered = VariableResolver._filter_for_substitution({variable.name: value})
            if variable.name not in filtered:
                continue
            from_project = variable.name in project_names and variable.owning_flow_name is None
            source = SubstitutableSource.MACRO if from_project else SubstitutableSource.VARIABLE
            read_only = variable.permission is VariablePermission.READ_ONLY
            substitutables.append(
                Substitutable(name=variable.name, value=filtered[variable.name], source=source, read_only=read_only)
            )

        substitutables.sort(key=lambda s: s.name)
        return ListSubstitutablesResultSuccess(
            substitutables=substitutables,
            result_details=f"Successfully listed {len(substitutables)} substitutable(s).",
        )

    def on_get_variables_request(self, request: GetVariablesRequest) -> ResultPayload:
        """Get user-defined variable values visible from the starting flow.

        Returns only user-defined workflow variables — project layer entries are excluded.
        """
        try:
            starting_flow = self._get_starting_flow(request.starting_flow)
        except ValueError as e:
            return GetVariablesResultFailure(
                result_details=f"Attempted to get variables. Failed to determine starting flow: {e}"
            )

        # Names owned by the project layer — used to filter out project-layer entries,
        # which arrive here as plain FlowVariables after being unwrapped at the request
        # boundary (isinstance check won't identify them).
        project_names = set(self._list_project_variable_names(request.project_id))

        if request.names:
            result: dict[str, Any] = {}
            missing: list[str] = []
            for name in request.names:
                lookup = self._find_variable_hierarchical(starting_flow, name, request.lookup_scope, request.project_id)
                from_project = (
                    lookup.variable is not None
                    and lookup.variable.name in project_names
                    and lookup.variable.owning_flow_name is None
                )
                if lookup.variable is None or from_project:
                    # Project-layer entries are excluded from the user-vars-only view.
                    missing.append(name)
                else:
                    result[name] = lookup.variable.value
            if missing:
                return GetVariablesResultFailure(
                    result_details=f"Attempted to get variables. Failed because variables not found: {missing!r}"
                )
            return GetVariablesResultSuccess(
                variables=result, result_details=f"Successfully retrieved {len(result)} variable(s)."
            )

        variables = self._get_variables_by_scope(starting_flow, request.lookup_scope, request.project_id)
        # Drop project layer entries — they aren't user-defined variables.
        all_vars = {v.name: v.value for v in variables if not (v.name in project_names and v.owning_flow_name is None)}
        return GetVariablesResultSuccess(
            variables=all_vars, result_details=f"Successfully retrieved {len(all_vars)} variable(s)."
        )

    def on_resolve_substitution_request(self, request: ResolveSubstitutionRequest) -> ResultPayload:
        """Resolve every {VAR}-substitutable value visible from the starting flow.

        Layered resolution: for HIERARCHICAL the walk is flow → project → global,
        with closer layers shadowing farther ones. Callers pass PROJECT_ONLY /
        HIERARCHICAL_FROM_PROJECT / etc. via lookup_scope to override the walk.
        """
        try:
            starting_flow = self._get_starting_flow(request.starting_flow)
        except ValueError as e:
            return ResolveSubstitutionResultFailure(
                result_details=f"Attempted to get variables. Failed to determine starting flow: {e}"
            )

        if request.names:
            result: dict[str, Any] = {}
            missing: set[str] = set()
            for name in request.names:
                lookup = self._find_variable_hierarchical(starting_flow, name, request.lookup_scope, request.project_id)
                if lookup.variable is None:
                    missing.add(name)
                    continue
                try:
                    result[name] = lookup.variable.value
                except (RuntimeError, NotImplementedError, ValueError) as e:
                    logger.debug("Skipping variable %r whose value cannot be resolved: %s", name, e)
                    missing.add(name)
            if missing:
                missing_list = sorted(missing)
                logger.warning("Variable substitution incomplete: resolved %s, missing %s", list(result), missing_list)
                return ResolveSubstitutionResultFailure(
                    result_details=f"Attempted to get variables. Failed because variables not found: {missing_list!r}",
                    resolved=result,
                    unresolved=missing_list,
                )
            return ResolveSubstitutionResultSuccess(
                variables=result, result_details=f"Successfully retrieved {len(result)} variable(s)."
            )

        variables = self._get_variables_by_scope(starting_flow, request.lookup_scope, request.project_id)
        all_vars: dict[str, Any] = {}
        for variable in variables:
            try:
                all_vars[variable.name] = variable.value
            except (RuntimeError, NotImplementedError, ValueError) as e:
                logger.debug("Skipping variable %r whose value cannot be resolved: %s", variable.name, e)
        return ResolveSubstitutionResultSuccess(
            variables=all_vars, result_details=f"Successfully retrieved {len(all_vars)} variable(s)."
        )

    def on_set_variables_request(self, request: SetVariablesRequest) -> ResultPayload:
        """Set multiple variable values atomically (all-or-nothing).

        Refuses batches that would touch any READ_ONLY variable — the whole batch fails
        so callers see the constraint without any partial writes.
        """
        try:
            starting_flow = self._get_starting_flow(request.starting_flow)
        except ValueError as e:
            return SetVariablesResultFailure(
                result_details=f"Attempted to set variables. Failed to determine starting flow: {e}"
            )

        # Validate all variables exist and are writable before writing any (all-or-nothing).
        found: dict[str, FlowVariable] = {}
        missing: list[str] = []
        read_only_hits: list[str] = []
        for name in request.variables:
            lookup = self._find_variable_hierarchical(starting_flow, name, request.lookup_scope, request.project_id)
            if lookup.variable is None:
                missing.append(name)
                continue
            if lookup.variable.permission is VariablePermission.READ_ONLY:
                read_only_hits.append(name)
                continue
            found[name] = lookup.variable

        if read_only_hits:
            return SetVariablesResultFailure(
                result_details=(
                    f"Attempted to set variables {read_only_hits!r}. Failed due to at least one being "
                    f"READ_ONLY — project builtins and directory macros can't be modified through this "
                    f"request; modify the project template via SaveProjectTemplateRequest instead."
                )
            )

        if missing:
            return SetVariablesResultFailure(
                result_details=f"Attempted to set variables. Failed because variables not found: {missing!r}"
            )

        for name, value in request.variables.items():
            found[name].value = value

        self._unresolve_nodes_referencing_variables(list(request.variables.keys()))
        return SetVariablesResultSuccess(result_details=f"Successfully set {len(request.variables)} variable(s).")

    def on_get_variable_details_request(self, request: GetVariableDetailsRequest) -> ResultPayload:
        """Get variable details (metadata only, no heavy values)."""
        try:
            starting_flow = self._get_starting_flow(request.starting_flow)
        except ValueError as e:
            return GetVariableDetailsResultFailure(
                result_details=f"Attempted to get details for variable '{request.name}'. Failed to determine starting flow: {e}"
            )

        result = self._find_variable_hierarchical(starting_flow, request.name, request.lookup_scope, request.project_id)

        if not result.variable:
            return GetVariableDetailsResultFailure(
                result_details=f"Attempted to get details for variable '{request.name}'. Failed because no such variable could be found."
            )

        variable = result.variable
        details = VariableDetails(name=variable.name, owning_flow_name=variable.owning_flow_name, type=variable.type)
        return GetVariableDetailsResultSuccess(
            details=details, result_details=f"Successfully retrieved details for variable '{request.name}'."
        )

    def _unresolve_nodes_referencing_variables(self, variable_names: list[str]) -> None:
        # Lazy imports to avoid circular dependency between retained_mode and exe_types.
        from griptape_nodes.exe_types.node_types import BaseNode, NodeResolutionState
        from griptape_nodes.exe_types.variable_resolver import VariableResolver

        flow_manager = GriptapeNodes.FlowManager()
        if flow_manager.check_for_existing_running_flow():
            # Mid-run: downstream UNRESOLVED nodes pick up new values naturally via ResolveSubstitutionRequest.
            return

        connections = flow_manager.get_connections()

        for obj in list(GriptapeNodes.ObjectManager()._name_to_objects.values()):
            if not isinstance(obj, BaseNode):
                continue
            if obj.state not in (NodeResolutionState.RESOLVED, NodeResolutionState.RESOLVING):
                continue
            for param in obj.parameters:
                value = obj.parameter_values.get(param.name, param.default_value)
                if any(VariableResolver.references_variable(value, name) for name in variable_names):
                    obj.make_node_unresolved(
                        current_states_to_trigger_change_event={
                            NodeResolutionState.RESOLVED,
                            NodeResolutionState.RESOLVING,
                        }
                    )
                    connections.unresolve_future_nodes(obj)
                    break

    def _find_variable_by_name(self, name: str) -> FlowVariable | None:
        """Find a variable by name in current flow context (legacy compatibility)."""
        try:
            starting_flow = self._get_starting_flow(None)
        except ValueError:
            return None

        result = self._find_variable_hierarchical(starting_flow, name, VariableScope.HIERARCHICAL, None)
        return result.variable
