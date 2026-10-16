"""Nodes for exercising a loop body that branches before it reaches the group's exit.

The packager wraps every packaged body in a ``StartFlow``/``EndFlow`` pair, so both live here
alongside a two-way branch, a step to put on one of its paths, and the two loop group kinds.

The library is registered under the name ``Griptape Nodes Library`` because that is the name the
packager asks for its flow endpoints by.
"""

from __future__ import annotations

from typing import Any, ClassVar

from griptape_nodes.exe_types.core_types import (
    ControlParameterInput,
    ControlParameterOutput,
    Parameter,
    ParameterMode,
)
from griptape_nodes.exe_types.node_groups.base_iterative_node_group import BaseIterativeNodeGroup
from griptape_nodes.exe_types.node_groups.base_while_node_group import BaseWhileNodeGroup
from griptape_nodes.exe_types.node_types import BaseNode, ControlNode, EndNode, StartNode


class StartFlow(StartNode):
    """The packaged body's entry node."""

    def process(self) -> None:
        return None


class EndFlow(EndNode):
    """The packaged body's exit node."""


class BranchNode(BaseNode):
    """Sends control down ``yes`` or ``no``, whichever ``take`` names.

    ``take_first``, when set, overrides ``take`` on the first run only, so a loop's first pass can
    branch differently from the rest.

    Every run is recorded, so a test can count how many passes the loop made.
    """

    runs: ClassVar[list[str]] = []

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(ControlParameterInput(name="exec_in"))
        self.add_parameter(ControlParameterOutput(name="yes"))
        self.add_parameter(ControlParameterOutput(name="no"))
        self.add_parameter(
            Parameter(
                name="take",
                tooltip="Which control output to follow",
                type="str",
                default_value="yes",
                allowed_modes={ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="take_first",
                tooltip="Which control output to follow on the first run, if not the same as take",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.PROPERTY},
            )
        )

    def process(self) -> None:
        BranchNode.runs.append(self.name)

    def get_next_control_output(self) -> Parameter | None:
        take_first = self.get_parameter_value("take_first")
        if take_first and len(BranchNode.runs) == 1:
            return self.get_parameter_by_name(take_first)
        return self.get_parameter_by_name(self.get_parameter_value("take"))


class StepNode(ControlNode):
    """A plain control node with a data output, as found at the end of most branches.

    Every run is recorded, so a test can tell whether the branch it sits on ran.
    """

    runs: ClassVar[list[str]] = []

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="result",
                tooltip="How many times this step has run",
                type="int",
                default_value=0,
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        StepNode.runs.append(self.name)
        self.parameter_output_values["result"] = len(StepNode.runs)


class ProbeWhileGroupNode(BaseWhileNodeGroup):
    """A while group with no behavior of its own beyond the base class."""


class ProbeForEachGroupNode(BaseIterativeNodeGroup):
    """Iterates a fixed three-item list, so no input wiring is needed to have a total."""

    ITEMS = ("first", "second", "third")

    def _get_iteration_items(self) -> list[Any]:
        return list(self.ITEMS)

    def _get_current_item_value(self, iteration_index: int) -> Any:
        return self.ITEMS[iteration_index]
