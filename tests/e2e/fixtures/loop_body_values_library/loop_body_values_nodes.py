"""Nodes for checking what a packaged loop body carries, and what its copies get fed.

``NarrowingSliderNode`` stands in for a node like Crop Image: an input (``image_width``) that, when
set, narrows the hard ``Slider`` on another parameter (``left``) to fit. The engine never re-checks
the value ``left`` already holds when the limit moves, so the node can end up holding a value its
own validator rejects. That is the state these tests package and rebuild.

The library is registered under the name ``Griptape Nodes Library`` because that is the name the
packager asks for its flow endpoints by.
"""

from __future__ import annotations

from typing import Any

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode, ParameterTypeBuiltin
from griptape_nodes.exe_types.node_groups.base_iterative_node_group import BaseIterativeNodeGroup
from griptape_nodes.exe_types.node_types import ControlNode, EndNode, StartNode
from griptape_nodes.exe_types.param_types.parameter_int import ParameterInt
from griptape_nodes.traits.slider import Slider

SLIDER_CEILING = 10000


class StartFlow(StartNode):
    """The packaged body's entry node."""

    def process(self) -> None:
        return None


class EndFlow(EndNode):
    """The packaged body's exit node.

    ``EndNode.process`` is left in place: it copies the node's inputs into its outputs, and that is
    where the loop reads each iteration's result from.
    """


class NarrowingSliderNode(ControlNode):
    """``image_width`` narrows the hard slider on ``left``; ``result`` echoes ``left``."""

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="image_width",
                tooltip="Narrows the slider on left when set",
                type="int",
                default_value=SLIDER_CEILING,
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            ParameterInt(
                name="left",
                default_value=0,
                tooltip="Hard-limited to 0..image_width",
                traits={Slider(min_val=0, max_val=SLIDER_CEILING)},
            )
        )
        self.add_parameter(
            Parameter(
                name="note",
                tooltip="An unconnected value, kept as-is",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="result",
                tooltip="left, as text",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.OUTPUT, ParameterMode.PROPERTY},
            )
        )

    def validate_before_workflow_run(self) -> list[Exception] | None:
        """Insist on ``note`` the way the BaseNode docstring shows, with the engine's own helper."""
        note_error = self.validate_empty_parameter(param="note", skip_if_connected=True)
        if note_error is None:
            return None
        return [note_error]

    def after_value_set(self, parameter: Parameter, value: Any) -> None:
        if parameter.name == "image_width" and value:
            left_param = self.get_parameter_by_name("left")
            if left_param is not None:
                left_param.update_ui_options({"slider": {"max_val": value}})
        return super().after_value_set(parameter, value)

    def process(self) -> None:
        self.parameter_output_values["result"] = str(self.get_parameter_value("left"))


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
