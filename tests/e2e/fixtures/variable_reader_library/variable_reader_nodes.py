"""Fixture nodes for the variable change re-run tests.

``VariableReader`` reads its ``text`` parameter, where a ``{name}`` token is substituted, the moment
it starts, then waits for an optional gate file before publishing. The gate is what lets a test
change a variable while the reader is still running, so the test decides the interleaving instead
of racing it. ``ChainSink`` and ``SetVariableNode`` are control nodes, so a workflow Run has a
control chain to walk; ``SetVariableNode`` writes a variable with the same requests the standard
library's Set Variable node sends. Every node counts its runs in ``run_count``, so a test can tell
"reused" from "ran again" without trusting resolution state.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import (
    ControlNode,
    DataNode,
    NodeDependencies,
    VariableAccess,
    VariableReference,
)
from griptape_nodes.retained_mode.events.node_events import GetFlowForNodeRequest, GetFlowForNodeResultSuccess
from griptape_nodes.retained_mode.events.variable_events import (
    SetVariableValueRequest,
    SetVariableValueResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.variable_types import VariableScope

_MAX_GATE_WAIT_SECONDS = 60
_GATE_POLL_SECONDS = 0.01


def _run_count_parameter() -> Parameter:
    return Parameter(
        name="run_count",
        type="int",
        default_value=0,
        tooltip="How many times this node has run",
        allowed_modes={ParameterMode.OUTPUT},
    )


class VariableReader(DataNode):
    """Publishes `text` as it read it when it started, optionally after waiting on `gate_file`."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)
        self.add_parameter(
            Parameter(
                name="text",
                type="str",
                default_value="",
                tooltip="Text that may hold {name} tokens",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="gate_file",
                type="str",
                default_value="",
                tooltip="Path to wait for before finishing. Empty means finish immediately.",
                allowed_modes={ParameterMode.PROPERTY},
                allow_variable_substitution=False,
            )
        )
        self.add_parameter(
            Parameter(
                name="result",
                type="str",
                default_value="",
                tooltip="The text as read at the start of the run",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(_run_count_parameter())
        self._runs = 0

    async def aprocess(self) -> None:
        self._runs += 1
        text = self.get_parameter_value("text") or ""
        await self._wait_for_gate()
        self.parameter_output_values["result"] = text
        self.parameter_output_values["run_count"] = self._runs

    async def _wait_for_gate(self) -> None:
        gate_path = self.get_parameter_value("gate_file") or ""
        if not gate_path:
            return
        gate_file = Path(gate_path)
        deadline = time.monotonic() + _MAX_GATE_WAIT_SECONDS
        while not gate_file.exists():  # noqa: ASYNC240
            if time.monotonic() > deadline:
                msg = f"Node '{self.name}' waited {_MAX_GATE_WAIT_SECONDS}s for its gate file and it never appeared."
                raise TimeoutError(msg)
            await asyncio.sleep(_GATE_POLL_SECONDS)


class Consumer(DataNode):
    """Copies `value` to `result`."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)
        self.add_parameter(
            Parameter(
                name="value",
                type="str",
                default_value="",
                tooltip="Input",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="result",
                type="str",
                default_value="",
                tooltip="Copy of the input",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(_run_count_parameter())
        self._runs = 0

    def process(self) -> None:
        self._runs += 1
        self.parameter_output_values["result"] = self.get_parameter_value("value")
        self.parameter_output_values["run_count"] = self._runs


class ChainSink(ControlNode):
    """Copies `value` to `result` as a step of a control chain."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)
        self.add_parameter(
            Parameter(
                name="value",
                type="str",
                default_value="",
                tooltip="Input",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="result",
                type="str",
                default_value="",
                tooltip="Copy of the input",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(_run_count_parameter())
        self._runs = 0

    def process(self) -> None:
        self._runs += 1
        self.parameter_output_values["result"] = self.get_parameter_value("value")
        self.parameter_output_values["run_count"] = self._runs


class SetVariableNode(ControlNode):
    """Sets the existing variable `variable_name` to `value`, found from this node's own flow."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)
        for parameter_name in ("variable_name", "value"):
            self.add_parameter(
                Parameter(
                    name=parameter_name,
                    type="str",
                    default_value="",
                    tooltip=parameter_name,
                    allowed_modes={ParameterMode.PROPERTY},
                    allow_variable_substitution=False,
                )
            )

    async def aprocess(self) -> None:
        flow_result = await GriptapeNodes.ahandle_request(GetFlowForNodeRequest(node_name=self.name))
        if not isinstance(flow_result, GetFlowForNodeResultSuccess):
            msg = f"Could not find the flow that holds '{self.name}': {flow_result.result_details}"
            raise TypeError(msg)
        set_result = await GriptapeNodes.ahandle_request(
            SetVariableValueRequest(
                name=self.get_parameter_value("variable_name"),
                value=self.get_parameter_value("value"),
                starting_flow=flow_result.flow_name,
            )
        )
        if not isinstance(set_result, SetVariableValueResultSuccess):
            msg = f"Could not set the variable: {set_result.result_details}"
            raise TypeError(msg)


class DeclaresVariable(DataNode):
    """Declares that it reads `variable_name`, the way Get Variable does, and does nothing else.

    A workflow saves only the flow variables some node declares. A `{name}` token in a text field
    declares nothing, so a test that saves and reopens a workflow needs a node like this one, as a
    real workflow has a Create Variable or Get Variable node.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)
        self.add_parameter(
            Parameter(
                name="variable_name",
                type="str",
                default_value="",
                tooltip="The variable this node declares",
                allowed_modes={ParameterMode.PROPERTY},
                allow_variable_substitution=False,
            )
        )

    def get_node_dependencies(self) -> NodeDependencies | None:
        deps = super().get_node_dependencies()
        if deps is None:
            deps = NodeDependencies()
        name = self.get_parameter_value("variable_name")
        if name:
            deps.variable_references.add(
                VariableReference(name=name, scope=VariableScope.HIERARCHICAL, access=VariableAccess.READ)
            )
        return deps

    def process(self) -> None:
        return None
