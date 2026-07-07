from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from griptape_nodes.retained_mode.events.base_events import (
    RequestPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    WorkflowAlteredMixin,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry
from griptape_nodes.retained_mode.variable_types import FlowVariable, VariableScope


# Variable Events
@dataclass
@PayloadRegistry.register
class CreateVariableRequest(RequestPayload):
    """Create a new variable.

    Args:
        name: The name of the variable
        type: The user-defined type (e.g., "JSON", "str", "int")
        is_global: Whether this is a global variable (True) or current flow variable (False)
        value: The initial value of the variable
        owning_flow: Flow that should own this variable (None for current flow in the Context Manager)
        initial_setup: If True, this request is part of workflow load/deserialization. Suppresses
            workflow-altered signalling.
    """

    name: str
    type: str
    is_global: bool = False
    value: Any = None
    owning_flow: str | None = None
    initial_setup: bool = False


@dataclass
@PayloadRegistry.register
class CreateVariableResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Variable created successfully."""


@dataclass
@PayloadRegistry.register
class CreateVariableResultFailure(WorkflowAlteredMixin, ResultPayloadFailure):
    """Variable creation failed."""


# Get Variable Events
@dataclass
@PayloadRegistry.register
class GetVariableRequest(RequestPayload):
    """Get a complete variable by name.

    Args:
        name: Variable name to lookup
        lookup_scope: Variable lookup strategy (default: hierarchical search through starting flow, ancestor flows, then global)
        starting_flow: Starting flow name (None for current flow in the Context Manager)
    """

    name: str
    lookup_scope: VariableScope = VariableScope.HIERARCHICAL
    starting_flow: str | None = None


@dataclass
@PayloadRegistry.register
class GetVariableResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Variable retrieved successfully."""

    variable: FlowVariable


@dataclass
@PayloadRegistry.register
class GetVariableResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Variable retrieval failed."""


# Get Variable Value Events
@dataclass
@PayloadRegistry.register
class GetVariableValueRequest(RequestPayload):
    """Get the value of a variable by name.

    Args:
        name: Variable name to lookup
        lookup_scope: Variable lookup strategy (default: hierarchical search through starting flow, ancestor flows, then global)
        starting_flow: Starting flow name (None for current flow in the Context Manager)
    """

    name: str
    lookup_scope: VariableScope = VariableScope.HIERARCHICAL
    starting_flow: str | None = None


@dataclass
@PayloadRegistry.register
class GetVariableValueResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Variable value retrieved successfully."""

    value: Any


@dataclass
@PayloadRegistry.register
class GetVariableValueResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Variable value retrieval failed."""


# Set Variable Value Events
@dataclass
@PayloadRegistry.register
class SetVariableValueRequest(RequestPayload):
    """Set the value of a variable by name.

    Args:
        value: The new value to set
        name: Variable name to lookup
        lookup_scope: Variable lookup strategy (default: hierarchical search through starting flow, ancestor flows, then global)
        starting_flow: Starting flow name (None for current flow in the Context Manager)
    """

    value: Any
    name: str
    lookup_scope: VariableScope = VariableScope.HIERARCHICAL
    starting_flow: str | None = None


@dataclass
@PayloadRegistry.register
class SetVariableValueResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Variable value set successfully."""


@dataclass
@PayloadRegistry.register
class SetVariableValueResultFailure(WorkflowAlteredMixin, ResultPayloadFailure):
    """Variable value setting failed."""


# Get Variable Type Events
@dataclass
@PayloadRegistry.register
class GetVariableTypeRequest(RequestPayload):
    """Get the type of a variable by name.

    Args:
        name: Variable name to lookup
        lookup_scope: Variable lookup strategy (default: hierarchical search through starting flow, ancestor flows, then global)
        starting_flow: Starting flow name (None for current flow in the Context Manager)
    """

    name: str
    lookup_scope: VariableScope = VariableScope.HIERARCHICAL
    starting_flow: str | None = None


@dataclass
@PayloadRegistry.register
class GetVariableTypeResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Variable type retrieved successfully."""

    type: str


@dataclass
@PayloadRegistry.register
class GetVariableTypeResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Variable type retrieval failed."""


# Set Variable Type Events
@dataclass
@PayloadRegistry.register
class SetVariableTypeRequest(RequestPayload):
    """Set the type of a variable by name.

    Args:
        type: The new user-defined type (e.g., "JSON", "str", "int")
        name: Variable name to lookup
        lookup_scope: Variable lookup strategy (default: hierarchical search through starting flow, ancestor flows, then global)
        starting_flow: Starting flow name (None for current flow in the Context Manager)
    """

    type: str
    name: str
    lookup_scope: VariableScope = VariableScope.HIERARCHICAL
    starting_flow: str | None = None


@dataclass
@PayloadRegistry.register
class SetVariableTypeResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Variable type set successfully."""


@dataclass
@PayloadRegistry.register
class SetVariableTypeResultFailure(WorkflowAlteredMixin, ResultPayloadFailure):
    """Variable type setting failed."""


# Delete Variable Events
@dataclass
@PayloadRegistry.register
class DeleteVariableRequest(RequestPayload):
    """Delete a variable by name.

    Args:
        name: Variable name to lookup
        lookup_scope: Variable lookup strategy (default: hierarchical search through starting flow, ancestor flows, then global)
        starting_flow: Starting flow name (None for current flow in the Context Manager)
    """

    name: str
    lookup_scope: VariableScope = VariableScope.HIERARCHICAL
    starting_flow: str | None = None


@dataclass
@PayloadRegistry.register
class DeleteVariableResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Variable deleted successfully."""


@dataclass
@PayloadRegistry.register
class DeleteVariableResultFailure(WorkflowAlteredMixin, ResultPayloadFailure):
    """Variable deletion failed."""


# Rename Variable Events
@dataclass
@PayloadRegistry.register
class RenameVariableRequest(RequestPayload):
    """Rename a variable by name.

    Args:
        new_name: The new name for the variable
        name: Current variable name
        lookup_scope: Variable lookup strategy (default: hierarchical search through starting flow, ancestor flows, then global)
        starting_flow: Starting flow name (None for current flow in the Context Manager)
    """

    new_name: str
    name: str
    lookup_scope: VariableScope = VariableScope.HIERARCHICAL
    starting_flow: str | None = None


@dataclass
@PayloadRegistry.register
class RenameVariableResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Variable renamed successfully."""


@dataclass
@PayloadRegistry.register
class RenameVariableResultFailure(WorkflowAlteredMixin, ResultPayloadFailure):
    """Variable renaming failed."""


# Has Variable Events
@dataclass
@PayloadRegistry.register
class HasVariableRequest(RequestPayload):
    """Check if a variable exists by name.

    Args:
        name: Variable name to lookup
        lookup_scope: Variable lookup strategy (default: hierarchical search through starting flow, ancestor flows, then global)
        starting_flow: Starting flow name (None for current flow in the Context Manager)
    """

    name: str
    lookup_scope: VariableScope = VariableScope.HIERARCHICAL
    starting_flow: str | None = None


@dataclass
@PayloadRegistry.register
class HasVariableResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Variable existence check completed."""

    exists: bool
    found_scope: VariableScope | None = None


@dataclass
@PayloadRegistry.register
class HasVariableResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Variable existence check failed."""


# List Variables Events
@dataclass
@PayloadRegistry.register
class ListVariablesRequest(RequestPayload):
    """List all variables in the specified scope.

    Args:
        lookup_scope: Variable lookup strategy (default: hierarchical search through starting flow, ancestor flows, then global; use ALL to get variables from all flows for GUI enumeration)
        starting_flow: Starting flow name (None for current flow in the Context Manager)
    """

    lookup_scope: VariableScope = VariableScope.HIERARCHICAL
    starting_flow: str | None = None


@dataclass
@PayloadRegistry.register
class ListVariablesResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Variables listed successfully."""

    variables: list[FlowVariable]


@dataclass
@PayloadRegistry.register
class ListVariablesResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Variables listing failed."""


# Get Variable Details Events
@dataclass
@PayloadRegistry.register
class GetVariableDetailsRequest(RequestPayload):
    """Get variable details (metadata only, no heavy values).

    Args:
        name: Variable name to lookup
        lookup_scope: Variable lookup strategy (default: hierarchical search through starting flow, ancestor flows, then global)
        starting_flow: Starting flow name (None for current flow in the Context Manager)
    """

    name: str
    lookup_scope: VariableScope = VariableScope.HIERARCHICAL
    starting_flow: str | None = None


@dataclass
class VariableDetails:
    """Lightweight variable details without heavy values."""

    name: str
    owning_flow_name: str | None  # None for global variables
    type: str


@dataclass
@PayloadRegistry.register
class GetVariableDetailsResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Variable details retrieved successfully."""

    details: VariableDetails


@dataclass
@PayloadRegistry.register
class GetVariableDetailsResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Variable details retrieval failed."""


class SubstitutableSource(StrEnum):
    VARIABLE = "variable"
    MACRO = "macro"


@dataclass
class Substitutable:
    """A single value available for {VAR} substitution.

    Attributes:
        name: The token name (what goes inside the braces, e.g. "workspace_dir")
        value: The resolved value
        source: Origin of the value (see SubstitutableSource)
        read_only: Whether the user can edit this value (macros are read-only)
    """

    name: str
    value: str | int
    source: SubstitutableSource
    read_only: bool = False


@dataclass
@PayloadRegistry.register
class ListSubstitutablesRequest(RequestPayload):
    """List every value that can go inside a {VAR} token, with UI metadata attached.

    USE THIS for any frontend picker, autocomplete, or display that shows what
    the user can type inside braces. Returns a unified list of Substitutable
    objects covering both user-defined workflow variables and project macros
    (workspace_dir, workflow_name, template directories, etc.). Each entry
    carries source ("variable" | "macro") and read_only so the UI can render
    them differently.

    DO NOT use this for execution-time substitution — the list format with
    metadata is for display, not for fast dict lookup. Use ResolveSubstitutionRequest
    for that.

    Args:
        lookup_scope: Variable lookup strategy (default: hierarchical)
        starting_flow: Starting flow name (None for current flow in the Context Manager)
    """

    lookup_scope: VariableScope = VariableScope.HIERARCHICAL
    starting_flow: str | None = None


@dataclass
@PayloadRegistry.register
class ListSubstitutablesResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Substitutables listed successfully."""

    substitutables: list[Substitutable]


@dataclass
@PayloadRegistry.register
class ListSubstitutablesResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Substitutables listing failed."""


# ---------------------------------------------------------------------------
# Three events cover "get variables" — pick the right one:
#
#   GetVariablesRequest        → user-defined variables only, as a flat dict.
#                                Use for anything that works with variables the
#                                user explicitly created (variable panel, nodes).
#
#   ResolveSubstitutionRequest → user variables + project macros merged, as a
#                                flat dict. Use at execution time when you need
#                                the complete set of values that can substitute
#                                into {VAR} tokens. Project macros are the base
#                                layer; user variables override on conflict.
#
#   ListSubstitutablesRequest  → same combined set, but as list[Substitutable]
#                                with source/read_only metadata. Use for any
#                                frontend picker or autocomplete — not for
#                                execution paths.
# ---------------------------------------------------------------------------


@dataclass
@PayloadRegistry.register
class ResolveSubstitutionRequest(RequestPayload):
    """Resolve the complete {VAR} substitution context for execution.

    USE THIS when a node is about to run and you need every value that can
    substitute into a {VAR} token — user-defined variables and project macros
    (workspace_dir, workflow_name, template directories, etc.) merged into one
    dict. User variables take priority over project macros on name conflict.

    DO NOT use this to inspect what variables a user has defined — use
    GetVariablesRequest for that. DO NOT use this to populate a UI picker —
    use ListSubstitutablesRequest for that (it carries source/read_only metadata).

    When ``names`` is non-empty, looks up each name individually and fails
    (all-or-nothing) if any name is not found in either user vars or macros.
    When ``names`` is empty, returns every substitutable value in scope.

    Args:
        names: Specific names to retrieve. Empty means "all in scope".
        lookup_scope: Variable lookup strategy (default: hierarchical search through starting flow, ancestor flows, then global)
        starting_flow: Starting flow name (None for current flow in the Context Manager)
    """

    names: list[str] = field(default_factory=list)
    lookup_scope: VariableScope = VariableScope.HIERARCHICAL
    starting_flow: str | None = None


@dataclass
@PayloadRegistry.register
class ResolveSubstitutionResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Substitution values resolved successfully."""

    variables: dict[str, Any]


@dataclass
@PayloadRegistry.register
class ResolveSubstitutionResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Substitution value resolution failed."""


@dataclass
@PayloadRegistry.register
class GetVariablesRequest(RequestPayload):
    """Get user-defined variable values visible from the starting flow.

    USE THIS when you want only the variables a user explicitly created —
    the variable panel, the GetVariable node, anything that works with the
    user's own variable definitions. Returns a flat dict; no project macros
    (workspace_dir, workflow_name, etc.) are included.

    DO NOT use this at execution time when you need the full substitution
    context — use ResolveSubstitutionRequest for that.

    When ``names`` is non-empty, looks up each name individually and fails
    (all-or-nothing) if any name is not found. When ``names`` is empty,
    returns every variable in scope.

    Args:
        names: Specific variable names to retrieve. Empty means "all in scope".
        lookup_scope: Variable lookup strategy (default: hierarchical search through starting flow, ancestor flows, then global)
        starting_flow: Starting flow name (None for current flow in the Context Manager)
    """

    names: list[str] = field(default_factory=list)
    lookup_scope: VariableScope = VariableScope.HIERARCHICAL
    starting_flow: str | None = None


@dataclass
@PayloadRegistry.register
class GetVariablesResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Variables retrieved successfully."""

    variables: dict[str, Any]


@dataclass
@PayloadRegistry.register
class GetVariablesResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Variables retrieval failed."""


@dataclass
@PayloadRegistry.register
class SetVariablesRequest(RequestPayload):
    """Set multiple variable values atomically (all-or-nothing).

    All variable names are validated to exist in scope before any value is
    written. If any variable is not found the entire batch is rejected and
    no variables are modified.

    Args:
        variables: Mapping of variable name → new value
        lookup_scope: Variable lookup strategy (default: hierarchical search through starting flow, ancestor flows, then global)
        starting_flow: Starting flow name (None for current flow in the Context Manager)
    """

    variables: dict[str, Any]
    lookup_scope: VariableScope = VariableScope.HIERARCHICAL
    starting_flow: str | None = None


@dataclass
@PayloadRegistry.register
class SetVariablesResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Variables set successfully."""


@dataclass
@PayloadRegistry.register
class SetVariablesResultFailure(WorkflowAlteredMixin, ResultPayloadFailure):
    """Variables batch set failed."""
