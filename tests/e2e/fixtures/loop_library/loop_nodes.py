"""Nodes for exercising loop-body packaging end to end.

``NodeExecutor._package_loop_body`` needs three things from a library: a ``StartFlow``/``EndFlow``
pair (the packager wraps every packaged body in one), an iterative Start/End pair to delimit the
loop, and something to put in the body. All three live here so the fixture can be registered from
one node file.

The library is registered under the name ``Griptape Nodes Library`` because that is the name the
packager asks for its flow endpoints by.
"""

from __future__ import annotations

import asyncio
from typing import Any

from griptape_nodes.exe_types.base_iterative_nodes import BaseIterativeEndNode, BaseIterativeStartNode
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import ControlNode, DataNode, EndNode, StartNode


class StartFlow(StartNode):
    """The packaged body's entry node. Publishes the per-iteration values set on it as outputs."""

    def process(self) -> None:
        for param in self.parameters:
            if param.name in self.parameter_values:
                self.parameter_output_values[param.name] = self.parameter_values[param.name]


class EndFlow(EndNode):
    """The packaged body's exit node.

    Inherits ``EndNode.process``, which copies every non-control parameter's raw value into
    ``parameter_output_values`` -- the loop packager reads a packaged body's result back out of
    exactly that dict.
    """


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


class LoopJoinNode(DataNode):
    """A body node with two inputs, so one can come from outside the loop: ``prefix`` + ``item``."""

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="prefix",
                tooltip="Text to put first",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="item",
                tooltip="Value to put after the prefix",
                input_types=["any"],
                type="any",
                default_value="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="result",
                tooltip="Joined text",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.OUTPUT, ParameterMode.PROPERTY},
            )
        )

    def process(self) -> None:
        prefix = self.get_parameter_value("prefix") or ""
        item = self.get_parameter_value("item")
        self.parameter_output_values["result"] = f"{prefix}{item}"


class LoopResultsNode(DataNode):
    """Sits after a loop and takes its ``results``: copies ``items`` to ``result``."""

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="items",
                tooltip="The loop's results",
                type="list",
                default_value=[],
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="result",
                tooltip="The loop's results, copied",
                type="list",
                default_value=[],
                allowed_modes={ParameterMode.OUTPUT, ParameterMode.PROPERTY},
            )
        )

    def process(self) -> None:
        self.parameter_output_values["result"] = list(self.get_parameter_value("items") or [])


class OutsideCounterDataNode(DataNode):
    """A node living OUTSIDE the loop, with a run counter standing in for an Agent/image-gen call.

    No control connections, so it is a plain data dependency. Used to measure how many times a
    node outside a packaged loop body actually runs when it gets pulled into the packaged flow as
    an unresolved data dependency (see ``NodeExecutor._collect_loop_body_nodes`` ->
    ``DagBuilder.collect_data_dependencies_for_node``).
    """

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.run_count = 0
        self.add_parameter(
            Parameter(
                name="value",
                tooltip="Constant value, computed once",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.OUTPUT, ParameterMode.PROPERTY},
            )
        )

    def process(self) -> None:
        self.run_count += 1
        self.parameter_output_values["value"] = f"outside-run-{self.run_count}"


class SlowSourceControlNode(ControlNode):
    """A control-typed node left with no control wires, so it classifies as a data node.

    Simulates an async upstream call (an Agent or image generator): ``aprocess`` really
    awaits, and its output changes between runs when ``seconds`` is edited.
    """

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="seconds",
                tooltip="How long to sleep",
                type="float",
                default_value=0.1,
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="result_details",
                tooltip="Describes how long this run slept",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.OUTPUT, ParameterMode.PROPERTY},
            )
        )

    async def aprocess(self) -> None:
        seconds = self.get_parameter_value("seconds") or 0.0
        await asyncio.sleep(seconds)
        self.parameter_output_values["result_details"] = f"Slept for {seconds} seconds"


class AnySinkDataNode(DataNode):
    """A downstream sink that accepts any type, used to pull an upstream node in as a dependency."""

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="value",
                tooltip="Anything",
                type="any",
                default_value=None,
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )

    def process(self) -> None:
        return None


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

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="current_item",
                tooltip="The item for the current pass",
                type="str",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="run_in_order",
                tooltip="Run passes one at a time instead of all at once",
                type="bool",
                default_value=False,
                allowed_modes={ParameterMode.PROPERTY},
            )
        )

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
