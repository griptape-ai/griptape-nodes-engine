from dataclasses import dataclass, field
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


# Bulk Get/Set Variable Events
@dataclass
@PayloadRegistry.register
class GetVariablesRequest(RequestPayload):
    """Get variable values visible from the starting flow.

    When ``names`` is non-empty, looks up each name individually via
    ``_find_variable_hierarchical`` and fails (all-or-nothing) if any name is
    not found. When ``names`` is empty, returns every variable in scope.

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
