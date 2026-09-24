"""Shared base for nodes whose work is an inner flow that can run somewhere other than in-process."""

from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING, Any

from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.exe_types.param_components.execution_environment_component import ExecutionEnvironmentComponent

if TYPE_CHECKING:
    from griptape_nodes.exe_types.core_types import Parameter
    from griptape_nodes.exe_types.param_components.subflow_execution_component import SubflowExecutionComponent


class InnerFlowNode(BaseNode):
    """A node backed by an inner flow, with an execution_environment the user picks per instance.

    For Local Execution the node's own aprocess runs the inner flow. For any other environment the
    NodeExecutor calls aprepare_inner_flow, sends the inner flow's nodes to that environment, then
    calls collect_inner_flow_outputs once the results have been written back into the inner flow.

    List this class before the concrete node base (e.g. ControlNode) so after_value_set reaches it.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)
        self._execution_environment_component = ExecutionEnvironmentComponent(
            self, tooltip="Environment that the subflow should execute in"
        )
        self._execution_environment_component.add_parameters()

    @property
    def execution_environment(self) -> Parameter:
        return self._execution_environment_component.parameter

    @property
    def subflow_execution_component(self) -> SubflowExecutionComponent:
        return self._execution_environment_component.subflow_execution_component

    @property
    @abstractmethod
    def inner_flow_name(self) -> str | None:
        """Name of the live inner flow, or None if it has not been created or loaded yet."""

    def after_value_set(self, parameter: Parameter, value: Any) -> None:
        super().after_value_set(parameter, value)
        self._execution_environment_component.after_value_set(parameter, value)

    def get_execution_environment(self) -> str:
        return self._execution_environment_component.get_execution_environment()

    @abstractmethod
    async def aprepare_inner_flow(self) -> str:
        """Make sure the inner flow is live, push this node's inputs into it, and return its name."""

    @abstractmethod
    def collect_inner_flow_outputs(self, flow_name: str) -> None:
        """Copy the inner flow's End Flow values onto this node's outputs."""
