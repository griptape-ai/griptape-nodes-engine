"""Fixture nodes for the data-only subflow regression test (issue #4993).

The subflow is wired with DATA connections only (no control edges):

    DataStartNode.value --> DataPassNode.value --> DataEndNode.value

An isolated subflow used to resolve only the start node and silently skip the
downstream/leaf nodes when there were no control edges to walk, so DataEndNode
never ran and produced no outputs. RunSubflowNode drives the subflow exactly the
way SubflowWorkflowNode does (StartLocalSubflowRequest) and reads the end node's
outputs back.
"""

from __future__ import annotations

from typing import Any

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import ControlNode, DataNode, EndNode, StartNode
from griptape_nodes.retained_mode.events.execution_events import (
    StartLocalSubflowRequest,
    StartLocalSubflowResultFailure,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes


class DataStartNode(StartNode):
    """A StartNode carrying a passthrough ``value`` (mirrors a workflow's Start Flow input)."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)
        self.add_parameter(
            Parameter(
                name="value",
                type="str",
                default_value="",
                tooltip="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY, ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        self.parameter_output_values["value"] = self.get_parameter_value("value") or ""


class DataPassNode(DataNode):
    """An intermediate data node that copies ``value`` straight through."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)
        self.add_parameter(
            Parameter(
                name="value",
                type="str",
                default_value="",
                tooltip="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY, ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        self.parameter_output_values["value"] = self.get_parameter_value("value") or ""


class DataEndNode(EndNode):
    """An EndNode carrying a ``value`` output (mirrors a workflow's End Flow output)."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)
        self.add_parameter(
            Parameter(
                name="value",
                type="str",
                default_value="",
                tooltip="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY, ParameterMode.OUTPUT},
            )
        )


class RunSubflowNode(ControlNode):
    """Drives a named subflow like SubflowWorkflowNode and collects the end node's output."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)
        self.add_parameter(
            Parameter(
                name="collected_value",
                type="str",
                default_value=None,
                tooltip="",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    async def aprocess(self) -> None:
        subflow = self.metadata["subflow_name"]
        end_node_name = self.metadata["end_node_name"]
        result = await GriptapeNodes.FlowManager().on_start_local_subflow_request(
            StartLocalSubflowRequest(flow_name=subflow)
        )
        if isinstance(result, StartLocalSubflowResultFailure):
            self.parameter_output_values["collected_value"] = f"START_FAIL: {result.result_details}"
            return
        flow = GriptapeNodes.FlowManager().get_flow_by_name(subflow)
        end_node = flow.nodes[end_node_name]
        if "value" in end_node.parameter_output_values:
            self.parameter_output_values["collected_value"] = end_node.parameter_output_values["value"]
        else:
            self.parameter_output_values["collected_value"] = end_node.get_parameter_value("value")

    def process(self) -> None:
        pass
