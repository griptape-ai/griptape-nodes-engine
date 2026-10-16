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
from griptape_nodes.exe_types.node_types import ControlNode, DataNode, EndNode, StartNode


class StartFlow(StartNode):
    """The packaged body's entry node."""

    def process(self) -> None:
        return None


class EndFlow(EndNode):
    """The packaged body's exit node.

    Must call super().process(): that is what copies every dynamically-added data parameter's
    raw value into parameter_output_values, which is how the loop executor reads an iteration's
    result back out (get_parameter_values_from_iterations reads parameter_output_values directly).
    """

    def process(self) -> None:
        super().process()


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
        text_value = self.get_parameter_value("text")
        self.parameter_output_values["result"] = str(text_value) if text_value is not None else ""


class CounterControlNode(ControlNode):
    """Mimics ``GetVariable``: a control node that recomputes from its own instance state.

    No incoming data connection changes between iterations, so nothing re-sets a parameter value
    on this node from outside (that cascade would unresolve its downstream neighbor through a
    different, unrelated mechanism and mask the bug this fixture targets). The sequential loop
    executor reuses one deserialized flow instance for every iteration, so this node's own
    ``_count`` persists across iterations the same way a loop-scoped variable does.
    """

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self._count = 0
        self.add_parameter(
            Parameter(
                name="count",
                tooltip="Increments by one every time this node runs",
                type="int",
                default_value=0,
                allowed_modes={ParameterMode.OUTPUT, ParameterMode.PROPERTY},
            )
        )

    def process(self) -> None:
        self.parameter_output_values["count"] = self._count
        self._count += 1


class DoublerDataNode(DataNode):
    """A pure data node sitting between two control nodes in a loop body.

    It has an outgoing data connection (to the next control node), so it is an interior node, not
    a terminal sink. It has no control connections in use, so it is a plain ``DataNode``. This is
    the shape ``classify_nodes_for_dag`` buckets into neither ``control_nodes`` nor
    ``data_sink_nodes`` -- the gap this fixture exists to exercise.
    """

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
                name="doubled",
                tooltip="value * 2, recomputed each time this node runs",
                type="int",
                default_value=0,
                allowed_modes={ParameterMode.OUTPUT, ParameterMode.PROPERTY},
            )
        )

    def process(self) -> None:
        self.parameter_output_values["doubled"] = int(self.get_parameter_value("value") or 0) * 2


class OutsideCounterDataNode(DataNode):
    """A node living OUTSIDE a group, with a run counter standing in for an Agent/image-gen call.

    No control connections, so it is a plain data dependency. Used to measure how many times a
    node outside the group runs when the group's packaged body is reused across sequential
    iterations.
    """

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.run_count = 0
        self.add_parameter(
            Parameter(
                name="value",
                tooltip="Constant value, computed once",
                type="int",
                default_value=0,
                allowed_modes={ParameterMode.OUTPUT, ParameterMode.PROPERTY},
            )
        )

    def process(self) -> None:
        self.run_count += 1
        self.parameter_output_values["value"] = self.run_count


class ProbeForEachGroupNode(BaseIterativeNodeGroup):
    """Iterates a fixed three-item list, so no input wiring is needed to have a total."""

    ITEMS = ("first", "second", "third")

    def _get_iteration_items(self) -> list[Any]:
        return list(self.ITEMS)

    def _get_current_item_value(self, iteration_index: int) -> Any:
        return self.ITEMS[iteration_index]
