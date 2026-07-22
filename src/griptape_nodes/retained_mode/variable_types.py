from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class VariableScope(StrEnum):
    """Which layers a variable lookup or listing consults."""

    CURRENT_FLOW_ONLY = "current_flow_only"
    """Only the starting flow's variables. No ancestors, project, or globals."""

    PROJECT_ONLY = "project_only"
    """Only the project's variables. No flows or globals."""

    HIERARCHICAL = "hierarchical"
    """Starting flow → ancestor flows → project → globals. Standard scope-chain resolution."""

    HIERARCHICAL_FROM_PROJECT = "hierarchical_from_project"
    """Project → globals. Skips flows entirely — for callers that want the project's view."""

    GLOBAL_ONLY = "global_only"
    """Only the global variables. No flows or project."""

    ALL = "all"
    """Every variable in every layer with no shadowing. Enumeration-only (ListVariables)."""


class VariablePermission(StrEnum):
    """What operations are allowed on a variable's value."""

    READ_ONLY = "read_only"
    WRITE_ONLY = "write_only"
    READ_WRITE = "read_write"


class VariableLayerKind(StrEnum):
    """Which tier a variable actually lives in.

    Distinct from VariableScope (a *search strategy*): this names the layer a lookup
    resolved a variable *from*, recorded at the point of discovery.
    """

    FLOW = "flow"
    PROJECT = "project"
    GLOBAL = "global"


@dataclass
class FlowVariable:
    name: str
    owning_flow_name: str | None  # None for global variables
    type: str
    value: Any
    permission: VariablePermission = VariablePermission.READ_WRITE


@dataclass
class VariableLayer:
    """Storage for all variables in one layer (a flow or the globals)."""

    _variables: dict[str, FlowVariable] = field(default_factory=dict)

    def get(self, name: str) -> FlowVariable | None:
        return self._variables.get(name)

    def has(self, name: str) -> bool:
        return name in self._variables

    def list(self) -> list[FlowVariable]:
        """Return variables in insertion order. Callers that want alphabetical order sort themselves."""
        return list(self._variables.values())

    def set(self, variable: FlowVariable) -> None:
        self._variables[variable.name] = variable

    def delete(self, name: str) -> None:
        del self._variables[name]

    def rename(self, old: str, new: str) -> None:
        # Renaming to the same name is a no-op — not a self-collision.
        if old == new:
            return
        if new in self._variables:
            msg = (
                f"Attempted to rename variable '{old}' to '{new}' in this layer. "
                f"Failed due to a variable named '{new}' already existing in the same layer."
            )
            raise ValueError(msg)
        variable = self._variables.pop(old)
        variable.name = new
        self._variables[new] = variable

    def clear(self) -> None:
        self._variables.clear()
