"""Fixture nodes for the workflow-backed node e2e test.

Provides the Start Flow / End Flow pair and a transform node that ``shout_workflow.py`` wires
together, plus ``CountdownNode``, a branching node that ``countdown_workflow.py`` uses to stop its
own recursion, ``FailingNode``, which always raises, and ``FixtureGroupNode``, a group that runs its
members. The library JSON declares ``shout_workflow.py`` in its ``workflow_nodes`` list, so
registering this library also produces a ``ShoutWorkflow`` node type generated from the workflow
file.
"""

from __future__ import annotations

from typing import Any

from griptape_nodes.exe_types.core_types import (
    ControlParameterInput,
    ControlParameterOutput,
    NodeError,
    Parameter,
    ParameterMode,
)
from griptape_nodes.exe_types.node_groups.subflow_node_group import SubflowNodeGroup
from griptape_nodes.exe_types.node_types import BaseNode, ControlNode, EndNode, StartNode


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


class CountdownNode(BaseNode):
    """Drops the first character of ``text`` and takes ``Then`` while there was text to drop."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)
        self.add_parameter(ControlParameterInput(tooltip="Flow in", name="exec_in"))
        self.add_parameter(ControlParameterOutput(tooltip="Taken while text is left", name="Then"))
        self.add_parameter(ControlParameterOutput(tooltip="Taken once text is empty", name="Else"))
        self.add_parameter(
            Parameter(
                name="text",
                type="str",
                default_value="",
                tooltip="Text to count down",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="rest",
                type="str",
                default_value="",
                tooltip="The text without its first character",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        text = self.get_parameter_value("text") or ""
        self.parameter_output_values["rest"] = text[1:]

    def get_next_control_output(self) -> Parameter | None:
        if self.get_parameter_value("text"):
            return self.get_parameter_by_name("Then")
        return self.get_parameter_by_name("Else")


class FailingNode(ControlNode):
    """Always fails with a ``NodeError``, so a workflow can fail on purpose."""

    FAILURE_MESSAGE = "The fixture node refused to run"

    def process(self) -> None:
        raise NodeError(self.FAILURE_MESSAGE)


class FixtureGroupNode(SubflowNodeGroup):
    """A group that runs its members in-process and passes their outputs on."""

    async def aprocess(self) -> None:
        await self.execute_subflow()

    def process(self) -> Any:
        pass


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
