"""Start and End nodes for the workflow behind the loop-in-a-Workflow-node fixture.

A workflow can only back a node when its Start Flow and End Flow nodes expose a parameter, and the
packager's `StartFlow`/`EndFlow` expose none. These two carry a `text` value in and out. The loop group
and body nodes come from `iterative_group_library/iterative_group_nodes.py`, which the fixture
library ships alongside this file.
"""

from __future__ import annotations

from typing import Any

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import EndNode, StartNode


class TextStartNode(StartNode):
    """Start Flow node exposing a single ``text`` input to the workflow."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)
        self.add_parameter(
            Parameter(
                name="text",
                type="str",
                default_value="",
                tooltip="Text handed to the workflow",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY, ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        self.parameter_output_values["text"] = self.get_parameter_value("text") or ""


class TextEndNode(EndNode):
    """End Flow node exposing a single ``result`` output from the workflow."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)
        self.add_parameter(
            Parameter(
                name="result",
                type="str",
                default_value="",
                tooltip="Text produced by the workflow",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY, ParameterMode.OUTPUT},
            )
        )
