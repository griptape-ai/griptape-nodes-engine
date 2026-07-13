from collections.abc import Callable
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


@dataclass
class FlowVariable:
    name: str
    owning_flow_name: str | None  # None for global variables
    type: str
    value: Any
    permission: VariablePermission = VariablePermission.READ_WRITE


class ComputedFlowVariable(FlowVariable):
    """A variable whose value is recomputed on every read.

    Used for values that depend on live runtime context — workflow_dir,
    workflow_name, template.directories macros. The resolver is invoked
    every time `.value` is accessed, so callers always see current values
    without any cache-invalidation machinery.

    Always READ_ONLY. Writing raises.
    """

    def __init__(self, name: str, type: str, resolver: Callable[[], Any]) -> None:  # noqa: A002 — matches parent FlowVariable's `type` field
        # Set the resolver FIRST so the property has something to invoke if something
        # ever reads `.value` mid-init. Then set the plain fields directly, bypassing
        # dataclass __init__ which would run `self.value = None` and trip our setter.
        self._resolver = resolver
        self.name = name
        self.owning_flow_name = None
        self.type = type
        self.permission = VariablePermission.READ_ONLY

    @property
    def value(self) -> Any:
        """Invoke the resolver. May raise if the underlying context isn't ready."""
        return self._resolver()

    @value.setter
    def value(self, _new: Any) -> None:
        msg = f"Attempted to write to computed variable '{self.name}'. Failed due to it being READ_ONLY."
        raise ValueError(msg)


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
