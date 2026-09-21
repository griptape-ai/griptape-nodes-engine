"""Nodes used by tests/e2e/test_loose_sink_repro.py.

Minimal shapes, deliberately dependency-free so the library registers in a bare subprocess:

- ``ChainNode`` - a control node with one control in / one control out plus a data
  passthrough, for building an exec chain.
- ``SinkNode`` - a data-only node with a single input and no outputs, for the "loose
  sink hanging off the middle of a chain" shape.
- ``ListSinkNode`` - the same, taking a list, for hanging off a loop's collected results.
- ``BranchNode`` - two control outputs, one chosen at runtime, for the branching shape.
"""

from __future__ import annotations

from griptape_nodes.exe_types.core_types import (
    ControlParameterInput,
    ControlParameterOutput,
    Parameter,
    ParameterMode,
)
from griptape_nodes.exe_types.node_types import BaseNode, ControlNode, DataNode


class ChainNode(ControlNode):
    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="text",
                tooltip="Text in",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="result",
                tooltip="Text out",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.OUTPUT, ParameterMode.PROPERTY},
            )
        )

    def process(self) -> None:
        text = self.get_parameter_value("text") or ""
        self.parameter_output_values["result"] = f"{text}>{self.name}"


class SinkNode(DataNode):
    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="text",
                tooltip="Text to consume",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )

    def process(self) -> None:
        pass


class ListSinkNode(DataNode):
    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="items",
                tooltip="List to consume",
                type="list",
                input_types=["list"],
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )

    def process(self) -> None:
        pass


class BranchNode(BaseNode):
    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(ControlParameterInput(tooltip="Flow in", name="exec_in"))
        self.add_parameter(ControlParameterOutput(tooltip="Taken when evaluate is true", name="Then"))
        self.add_parameter(ControlParameterOutput(tooltip="Taken when evaluate is false", name="Else"))
        self.add_parameter(
            Parameter(
                name="evaluate",
                tooltip="Which branch to take",
                type="bool",
                default_value=False,
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="result",
                tooltip="Text out",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.OUTPUT, ParameterMode.PROPERTY},
            )
        )

    def process(self) -> None:
        self.parameter_output_values["result"] = self.name

    def get_next_control_output(self) -> Parameter | None:
        if self.get_parameter_value("evaluate"):
            return self.get_parameter_by_name("Then")
        return self.get_parameter_by_name("Else")
