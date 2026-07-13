from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class VariableScope(StrEnum):
    CURRENT_FLOW_ONLY = "current_flow_only"
    HIERARCHICAL = "hierarchical"
    GLOBAL_ONLY = "global_only"
    ALL = "all"  # For ListVariables to get all variables from all flows


@dataclass
class FlowVariable:
    name: str
    owning_flow_name: str | None  # None for global variables
    type: str
    value: Any


@dataclass
class VariableLayer:
    """Storage for all variables in one layer (a flow, the globals, or a project)."""

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
