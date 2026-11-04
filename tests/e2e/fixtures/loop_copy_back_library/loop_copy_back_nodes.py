"""Nodes for checking what the end of a loop writes back onto the canvas body nodes.

``ListSinkNode`` stands in for a node like Merge Images: a ``ParameterList`` whose rows are each wired
from another body node. A whole list written onto it would replace those connected rows, which the
list refuses, so the copy-back has to leave it alone.

The library is registered under the name ``Griptape Nodes Library`` because that is the name the
packager asks for its flow endpoints by.
"""

from __future__ import annotations

from typing import Any

from griptape_nodes.exe_types.core_types import Parameter, ParameterList, ParameterMode, ParameterTypeBuiltin
from griptape_nodes.exe_types.node_groups.base_iterative_node_group import BaseIterativeNodeGroup
from griptape_nodes.exe_types.node_types import ControlNode, EndNode, StartNode


class StartFlow(StartNode):
    """The packaged body's entry node."""

    def process(self) -> None:
        return None


class EndFlow(EndNode):
    """The packaged body's exit node.

    ``EndNode.process`` is left in place: it copies the node's inputs into its outputs, and that is
    where the loop reads each iteration's result from.
    """


class EchoNode(ControlNode):
    """Copies ``value`` to ``result`` as text, so it can feed a list row."""

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="value",
                tooltip="Value for this iteration",
                type="int",
                default_value=0,
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="result",
                tooltip="value, as text",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.OUTPUT, ParameterMode.PROPERTY},
            )
        )

    def process(self) -> None:
        self.parameter_output_values["result"] = str(self.get_parameter_value("value"))


class ListSinkNode(ControlNode):
    """Joins the rows of its ``items`` list into ``result``, the way Merge Images composes its images."""

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            ParameterList(
                name="items",
                tooltip="Rows are added in the editor and each wired from another node",
                type="str",
                input_types=["str", "int"],
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="result",
                tooltip="The items, joined",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.OUTPUT, ParameterMode.PROPERTY},
            )
        )

    def process(self) -> None:
        items = self.get_parameter_value("items") or []
        self.parameter_output_values["result"] = ",".join(str(item) for item in items)


class ItemsForEachGroupNode(BaseIterativeNodeGroup):
    """Iterates a fixed list of ints and exposes each as ``current_item``."""

    ITEMS = (10, 20)

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.current_item = Parameter(
            name="current_item",
            tooltip="The item for this iteration",
            output_type=ParameterTypeBuiltin.ALL.value,
            allowed_modes={ParameterMode.OUTPUT},
            settable=False,
        )
        self.add_parameter(self.current_item)

    def _get_iteration_items(self) -> list[Any]:
        return list(self.ITEMS)

    def _get_current_item_value(self, iteration_index: int) -> Any:
        return self.ITEMS[iteration_index]
