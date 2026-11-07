"""The variables of one open workflow."""

from __future__ import annotations

from dataclasses import dataclass, field

from griptape_nodes.retained_mode.variable_types import VariableLayer


@dataclass
class WorkflowVariables:
    """The flow and global variables of one open workflow.

    Project variables aren't here: they belong to a project, which several workflows can share.
    """

    # One layer per flow, made the first time a variable is written to it.
    flow_layers: dict[str, VariableLayer] = field(default_factory=dict)
    global_layer: VariableLayer = field(default_factory=VariableLayer)
    # Whether `{VAR}` references in parameter values resolve. A saved workflow sets it as it loads,
    # since build_workflow() bakes in a SetVariableSubstitutionEnabledRequest.
    substitution_enabled: bool = True
