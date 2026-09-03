"""Nodes for exercising loop-body packaging end to end.

``NodeExecutor._package_loop_body`` needs three things from a library: a ``StartFlow``/``EndFlow``
pair (the packager wraps every packaged body in one), an iterative Start/End pair to delimit the
loop, and something to put in the body. All three live here so the fixture can be registered from
one node file.

The library is registered under the name ``Griptape Nodes Library`` because that is the name the
packager asks for its flow endpoints by.
"""

from __future__ import annotations

from typing import Any

from griptape_nodes.exe_types.base_iterative_nodes import BaseIterativeEndNode, BaseIterativeStartNode
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode, EndNode, StartNode


class StartFlow(StartNode):
    """The packaged body's entry node."""


class EndFlow(EndNode):
    """The packaged body's exit node."""


class LoopBodyNode(DataNode):
    """One node's worth of loop body: copies ``text`` to ``result``."""

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="text",
                tooltip="Text to echo",
                type="str",
                default_value="",
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
        self.parameter_output_values["result"] = self.get_parameter_value("text") or ""


class LoopEndNode(BaseIterativeEndNode):
    """Closes the loop. Declared before the Start node so it can name it as compatible."""

    @classmethod
    def _get_compatible_start_classes(cls) -> set[type]:
        return {LoopStartNode}

    def process(self) -> None:
        return None


class LoopStartNode(BaseIterativeStartNode):
    """Iterates a fixed three-item list, so no input wiring is needed to have a total."""

    ITEMS = ("first", "second", "third")

    @classmethod
    def _get_compatible_end_classes(cls) -> set[type]:
        return {LoopEndNode}

    def _get_parameter_group_name(self) -> str:
        return "Iteration Data"

    def _get_exec_out_display_name(self) -> str:
        return "On Each Item"

    def _get_exec_out_tooltip(self) -> str:
        return "Execute for each item"

    def _get_iteration_items(self) -> list[Any]:
        return list(self.ITEMS)

    def _initialize_iteration_data(self) -> None:
        self._current_iteration_count = 0
        self._total_iterations = len(self.ITEMS)

    def _get_current_item_value(self) -> Any:
        return self.ITEMS[self._current_iteration_count]

    def is_loop_finished(self) -> bool:
        return self._current_iteration_count >= len(self.ITEMS)

    def _get_total_iterations(self) -> int:
        return len(self.ITEMS)

    def _get_current_iteration_count(self) -> int:
        return self._current_iteration_count

    def get_current_index(self) -> int:
        return self._current_iteration_count

    def _advance_to_next_iteration(self) -> None:
        self._current_iteration_count += 1
