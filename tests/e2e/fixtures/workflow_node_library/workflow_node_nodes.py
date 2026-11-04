"""Fixture nodes for the workflow-backed node e2e test.

Provides the Start Flow / End Flow pair and a transform node that ``shout_workflow.py`` wires
together. The library JSON declares that workflow in its ``workflow_nodes`` list, so registering
this library also produces a ``ShoutWorkflow`` node type generated from the workflow file.
"""

from __future__ import annotations

from typing import Any

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import ControlNode, EndNode, StartNode


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


class ShoutNode(ControlNode):
    """Uppercases ``text`` and appends an exclamation mark."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)
        self.add_parameter(
            Parameter(
                name="text",
                type="str",
                default_value="",
                tooltip="Text to shout",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="shouted",
                type="str",
                default_value="",
                tooltip="The shouted text",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        text = self.get_parameter_value("text") or ""
        self.parameter_output_values["shouted"] = f"{text.upper()}!"


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
