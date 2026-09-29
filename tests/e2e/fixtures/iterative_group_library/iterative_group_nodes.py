"""Nodes for exercising iterative node group execution end to end.

``NodeExecutor.handle_iterative_group_execution`` needs a ``StartFlow``/``EndFlow`` pair (the
packager wraps every packaged body in one), an iterative group node, and something to put in the
body. All three live here so the fixture can be registered from one node file.

The library is registered under the name ``Griptape Nodes Library`` because that is the name the
packager asks for its flow endpoints by.
"""

from __future__ import annotations

from typing import Any

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_groups.base_iterative_node_group import BaseIterativeNodeGroup
from griptape_nodes.exe_types.node_types import ControlNode, EndNode, StartNode


class StartFlow(StartNode):
    """The packaged body's entry node."""

    def process(self) -> None:
        return None


class EndFlow(EndNode):
    """The packaged body's exit node."""

    def process(self) -> None:
        return None


class LoopBodyControlNode(ControlNode):
    """A loop body that participates in control flow, so it re-runs on every iteration.

    A data-only body is not enough: with nothing wired to the group's ``loop_complete`` it resolves
    once and later iterations have no reason to touch it. A realistic body is wired
    ``on_each -> exec_in`` and ``exec_out -> loop_complete``, which is what makes each iteration
    re-execute it -- and that is the shape any test of per-iteration highlighting needs.
    """

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="text",
                tooltip="Value for this iteration (wired from the group's index)",
                type="int",
                default_value=0,
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="result",
                tooltip="Echoed text",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.OUTPUT, ParameterMode.PROPERTY},
            )
        )

    def process(self) -> None:
        self.parameter_output_values["result"] = str(self.get_parameter_value("text") or "")


class ProbeForEachGroupNode(BaseIterativeNodeGroup):
    """Iterates a fixed three-item list, so no input wiring is needed to have a total."""

    ITEMS = ("first", "second", "third")

    def _get_iteration_items(self) -> list[Any]:
        return list(self.ITEMS)

    def _get_current_item_value(self, iteration_index: int) -> Any:
        return self.ITEMS[iteration_index]
