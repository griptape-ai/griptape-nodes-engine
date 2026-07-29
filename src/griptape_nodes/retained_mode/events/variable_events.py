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
from griptape_nodes.retained_mode.variable_types import FlowVariable, VariableLayerKind, VariableScope


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
        lookup_scope: Variable lookup strategy (default: hierarchical search through starting flow,
            ancestor flows, project layer, then global)
        starting_flow: Starting flow name (None for current flow in the Context Manager)
        project_id: Which project's variable layer to consult (None = current project)
    """

    name: str
    lookup_scope: VariableScope = VariableScope.HIERARCHICAL
    starting_flow: str | None = None
    project_id: str | None = None


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
        lookup_scope: Variable lookup strategy (default: hierarchical search through starting flow,
            ancestor flows, project layer, then global)
        starting_flow: Starting flow name (None for current flow in the Context Manager)
        project_id: Which project's variable layer to consult (None = current project)
    """

    name: str
    lookup_scope: VariableScope = VariableScope.HIERARCHICAL
    starting_flow: str | None = None
    project_id: str | None = None


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
        lookup_scope: Variable lookup strategy (default: hierarchical search through starting flow,
            ancestor flows, project layer, then global)
        starting_flow: Starting flow name (None for current flow in the Context Manager)
        project_id: Which project's variable layer to consult (None = current project)
    """

    value: Any
    name: str
    lookup_scope: VariableScope = VariableScope.HIERARCHICAL
    starting_flow: str | None = None
    project_id: str | None = None


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
        lookup_scope: Variable lookup strategy (default: hierarchical search through starting flow,
            ancestor flows, project layer, then global)
        starting_flow: Starting flow name (None for current flow in the Context Manager)
        project_id: Which project's variable layer to consult (None = current project)
    """

    name: str
    lookup_scope: VariableScope = VariableScope.HIERARCHICAL
    starting_flow: str | None = None
    project_id: str | None = None


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
        lookup_scope: Variable lookup strategy (default: hierarchical search through starting flow,
            ancestor flows, project layer, then global)
        starting_flow: Starting flow name (None for current flow in the Context Manager)
        project_id: Which project's variable layer to consult (None = current project)
    """

    type: str
    name: str
    lookup_scope: VariableScope = VariableScope.HIERARCHICAL
    starting_flow: str | None = None
    project_id: str | None = None


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
        lookup_scope: Variable lookup strategy (default: hierarchical search through starting flow,
            ancestor flows, project layer, then global)
        starting_flow: Starting flow name (None for current flow in the Context Manager)
        project_id: Which project's variable layer to consult (None = current project)
    """

    name: str
    lookup_scope: VariableScope = VariableScope.HIERARCHICAL
    starting_flow: str | None = None
    project_id: str | None = None


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
        lookup_scope: Variable lookup strategy (default: hierarchical search through starting flow,
            ancestor flows, project layer, then global)
        starting_flow: Starting flow name (None for current flow in the Context Manager)
        project_id: Which project's variable layer to consult (None = current project)
    """

    new_name: str
    name: str
    lookup_scope: VariableScope = VariableScope.HIERARCHICAL
    starting_flow: str | None = None
    project_id: str | None = None


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
        lookup_scope: Variable lookup strategy (default: hierarchical search through starting flow,
            ancestor flows, project layer, then global)
        starting_flow: Starting flow name (None for current flow in the Context Manager)
        project_id: Which project's variable layer to consult (None = current project)
    """

    name: str
    lookup_scope: VariableScope = VariableScope.HIERARCHICAL
    starting_flow: str | None = None
    project_id: str | None = None


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
        lookup_scope: Variable lookup strategy (default: hierarchical search through starting flow,
            ancestor flows, project layer, then global; use ALL to get variables from all flows for
            GUI enumeration)
        starting_flow: Starting flow name (None for current flow in the Context Manager)
        project_id: Which project's variable layer to consult (None = current project)
    """

    lookup_scope: VariableScope = VariableScope.HIERARCHICAL
    starting_flow: str | None = None
    project_id: str | None = None


@dataclass
@PayloadRegistry.register
class ListVariablesResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Variables listed successfully.

    ``layers`` is parallel to ``variables``: layers[i] names the layer variables[i]
    was resolved from (flow / project / global). Kept as a separate defaulted field
    so the shipped ``variables`` shape is unchanged (wire-additive).
    """

    variables: list[FlowVariable]
    layers: list[VariableLayerKind] = field(default_factory=list)


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
        lookup_scope: Variable lookup strategy (default: hierarchical search through starting flow,
            ancestor flows, project layer, then global)
        starting_flow: Starting flow name (None for current flow in the Context Manager)
        project_id: Which project's variable layer to consult (None = current project)
    """

    name: str
    lookup_scope: VariableScope = VariableScope.HIERARCHICAL
    starting_flow: str | None = None
    project_id: str | None = None


@dataclass
class VariableDetails:
    """Lightweight variable details without heavy values."""

    name: str
    owning_flow_name: str | None  # None for global variables
    type: str
    # True for project builtins/directories, whose names are reserved: a flow variable
    # may not be created or renamed to a reserved name. False for flow/global variables
    # and user-defined project-bag entries.
    reserved: bool = False


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

    DEPRECATED with ListSubstitutablesRequest — ListVariablesResultSuccess.layers
    supersedes source/read_only. TODO(https://github.com/griptape-ai/griptape-nodes/issues/5143): delete.

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
    """DEPRECATED: use ListVariablesRequest instead.

    ListVariablesResultSuccess carries per-entry layer provenance (``layers``),
    which supersedes Substitutable's source/read_only metadata: derive the group
    from layer (PROJECT → macro-ish) and read_only from layer + permission.
    Kept as a compatibility shim for GUI versions that still send it.
    TODO(https://github.com/griptape-ai/griptape-nodes/issues/5143): delete after
    the GUI migrates (griptape-ai/griptape-vsl-gui#2668).

    Args:
        lookup_scope: Variable lookup strategy (default: hierarchical — flow → project → global)
        starting_flow: Starting flow name (None for current flow in the Context Manager)
        project_id: Which project's variable layer to consult (None = current project)
    """

    lookup_scope: VariableScope = VariableScope.HIERARCHICAL
    starting_flow: str | None = None
    project_id: str | None = None


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
# Variable read surface — pick the right request:
#
#   ListVariablesRequest       → enumeration: every variable visible in scope,
#                                with per-entry layer provenance (flow / project /
#                                global). Use for execution-time {VAR} substitution
#                                context AND for frontend pickers (derive
#                                grouping/read_only from layer + permission).
#                                Layered resolution: closer layers shadow farther
#                                ones on name conflict (flow → project → global
#                                for HIERARCHICAL).
#
#   GetVariablesRequest        → named probe: "of THESE names, which resolve and
#                                to what?" Same scope options as List; Success
#                                carries resolved values + an unresolved list
#                                (a miss is data, not a failure). Use when a
#                                ParsedMacro (or similar) hands you a name list.
#
#   GetVariableRequest /       → point lookups by name (full variable, value
#   GetVariableValueRequest /    only, existence). The standard library's
#   HasVariableRequest           variable nodes speak these.
#
# DEPRECATED, do not add callers (deletion tracked in issue 5143):
#   ResolveSubstitutionRequest, ListSubstitutablesRequest — shims over the same
#   walk as ListVariables. SetVariablesRequest — batch write with no known
#   senders anywhere.
# ---------------------------------------------------------------------------


@dataclass
@PayloadRegistry.register
class ResolveSubstitutionRequest(RequestPayload):
    """DEPRECATED: use ListVariablesRequest instead.

    Same layered walk; build a name→value dict from the result's variables.
    No engine-internal callers remain — kept only for out-of-tree scripts.
    TODO(https://github.com/griptape-ai/griptape-nodes/issues/5143): delete.

    When ``names`` is non-empty, looks up each name individually and fails
    (all-or-nothing) if any name is not found in scope.
    When ``names`` is empty, returns every substitutable value in scope.

    Args:
        names: Specific names to retrieve. Empty means "all in scope".
        lookup_scope: Variable lookup strategy (default: hierarchical search through starting flow,
            ancestor flows, project layer, then global)
        starting_flow: Starting flow name (None for current flow in the Context Manager)
        project_id: Which project's variable layer to consult (None = current project)
    """

    names: list[str] = field(default_factory=list)
    lookup_scope: VariableScope = VariableScope.HIERARCHICAL
    starting_flow: str | None = None
    project_id: str | None = None


@dataclass
@PayloadRegistry.register
class ResolveSubstitutionResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Substitution values resolved successfully."""

    variables: dict[str, Any]


@dataclass
@PayloadRegistry.register
class ResolveSubstitutionResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Substitution value resolution failed.

    Attributes:
        resolved: Variables that were found before the failure.
        unresolved: Names that could not be found.
    """

    resolved: dict[str, Any] = field(default_factory=dict)
    unresolved: list[str] = field(default_factory=list)


@dataclass
@PayloadRegistry.register
class GetVariablesRequest(RequestPayload):
    """Probe specific variable names in scope and report which resolved.

    The named companion to ListVariablesRequest: List enumerates everything
    visible in scope; Get answers "of THESE names, which resolve and to what?"
    Misses are not failures — the Success result carries ``unresolved`` so the
    caller can decide (e.g. a ParsedMacro consumer treats a missing required
    variable as an error and a missing optional one as fine).

    ``names`` must be non-empty — to enumerate everything, use ListVariablesRequest.

    Args:
        names: Variable names to probe. Must be non-empty.
        lookup_scope: Variable lookup strategy (default: hierarchical search through starting flow,
            ancestor flows, project layer, then global)
        starting_flow: Starting flow name (None for current flow in the Context Manager)
        project_id: Which project's variable layer to consult (None = current project)
    """

    names: list[str] = field(default_factory=list)
    lookup_scope: VariableScope = VariableScope.HIERARCHICAL
    starting_flow: str | None = None
    project_id: str | None = None


@dataclass
@PayloadRegistry.register
class GetVariablesResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Probe completed. A miss is not a failure — check ``unresolved``.

    Attributes:
        variables: name → value for every probed name that resolved in scope.
        unresolved: probed names that did not resolve. Empty when all names hit.
    """

    variables: dict[str, Any]
    unresolved: list[str] = field(default_factory=list)


@dataclass
@PayloadRegistry.register
class GetVariablesResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Probe could not run (empty names, unknown starting flow)."""


@dataclass
@PayloadRegistry.register
class SetVariablesRequest(RequestPayload):
    """DEPRECATED: no known senders (engine, standard library, and GUI all confirmed clean).

    Batch write of multiple variable values atomically (all-or-nothing). Use
    SetVariableValueRequest per variable instead.
    TODO(https://github.com/griptape-ai/griptape-nodes/issues/5143): delete.

    All variable names are validated to exist in scope before any value is
    written. If any variable is not found the entire batch is rejected and
    no variables are modified.

    Args:
        variables: Mapping of variable name → new value
        lookup_scope: Variable lookup strategy (default: hierarchical search through starting flow,
            ancestor flows, project layer, then global)
        starting_flow: Starting flow name (None for current flow in the Context Manager)
        project_id: Which project's variable layer to consult (None = current project)
    """

    variables: dict[str, Any]
    lookup_scope: VariableScope = VariableScope.HIERARCHICAL
    starting_flow: str | None = None
    project_id: str | None = None


@dataclass
@PayloadRegistry.register
class SetVariablesResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Variables set successfully."""


@dataclass
@PayloadRegistry.register
class SetVariablesResultFailure(WorkflowAlteredMixin, ResultPayloadFailure):
    """Variables batch set failed."""
