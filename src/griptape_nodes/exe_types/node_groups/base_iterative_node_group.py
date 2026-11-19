"""Base class for iterative node groups (ForEach, ForLoop, etc.)."""

from __future__ import annotations

from abc import abstractmethod
from enum import StrEnum
from typing import Any

from griptape_nodes.exe_types.core_types import (
    ControlParameterInput,
    ControlParameterOutput,
    Parameter,
    ParameterMode,
    ParameterTypeBuiltin,
)
from griptape_nodes.exe_types.node_groups.subflow_node_group import (
    LEFT_PARAMETERS_KEY,
    RIGHT_PARAMETERS_KEY,
    SubflowNodeGroup,
)
from griptape_nodes.exe_types.param_types.parameter_bool import ParameterBool
from griptape_nodes.exe_types.param_types.parameter_int import ParameterInt
from griptape_nodes.traits.options import Options
from griptape_nodes.utils.list_utils import flatten_list

# Execution mode choices and their corresponding boolean values (True = run in order)
EXECUTION_MODE_ONE_AT_A_TIME = "Run Group Items One at a Time"
EXECUTION_MODE_ALL_AT_ONCE = "Run Group Items All at Once"
EXECUTION_MODE_CHOICES = [EXECUTION_MODE_ONE_AT_A_TIME, EXECUTION_MODE_ALL_AT_ONCE]
EXECUTION_MODE_VALUE_LOOKUP = {
    EXECUTION_MODE_ONE_AT_A_TIME: True,
    EXECUTION_MODE_ALL_AT_ONCE: False,
}


# Saved connections may still name a loop group's entry and exit by the base class's port names.
# Each resolves to the loop port that carries that wire now.
LEGACY_CONTROL_PORT_NAMES = {
    "group_exec_in": "exec_in",
    "group_exec_out": "exec_out",
}


class IterationControlParam(StrEnum):
    """Parameter names for iteration control on iterative node groups."""

    LOOP_COMPLETE = "loop_complete"
    SKIP_ITERATION = "skip_iteration"
    BREAK_LOOP = "break_loop"


class BaseIterativeNodeGroup(SubflowNodeGroup):
    """Base class for iterative node groups (ForEach, ForLoop, etc.).

    Combines the functionality of BaseIterativeStartNode and BaseIterativeEndNode
    into a single group node that encapsulates the loop body as child nodes.

    This provides a simpler user experience than separate start/end nodes while
    maintaining the same execution capabilities (sequential/parallel, local/private/cloud).

    The NodeExecutor detects instances of this class and handles iteration execution
    via handle_iterative_group_execution(), similar to how it handles BaseIterativeEndNode.

    Subclasses must implement:
        - _get_iteration_items(): Return the list of items to iterate over
        - _get_current_item_value(iteration_index): Get the value for current iteration
    """

    # Iteration state
    _items: list[Any]
    _current_iteration_count: int
    _total_iterations: int

    # Results storage
    _results_list: list[Any]

    def __init__(
        self,
        name: str,
        metadata: dict[Any, Any] | None = None,
    ) -> None:
        # The group's one control pair is the loop's entry and exit; label it for what it means here.
        super().__init__(
            name,
            metadata,
            control_in=ControlParameterInput(
                tooltip="Wire the previous node's control output here to start the loop.",
                display_name="Start Loop",
            ),
            control_out=ControlParameterOutput(
                tooltip="Fires once after every iteration has finished. Wire the next node's control input here.",
                display_name="On Complete",
            ),
        )
        self.exec_in = self.control_in
        self.exec_out = self.control_out
        # A workflow saved while the loop still carried the base pair lists those names on its rails.
        for metadata_key in (LEFT_PARAMETERS_KEY, RIGHT_PARAMETERS_KEY):
            side_parameters: list[str] | None = self.metadata.get(metadata_key)
            if side_parameters is None:
                continue
            side_parameters[:] = [name for name in side_parameters if name not in LEGACY_CONTROL_PORT_NAMES]

        # Per-iteration control output — pairs visually with exec_in on the LEFT rail.
        # Connect this to the first body node to make that node each iteration's entry point.
        # If unconnected, executor falls back to implicit child-discovery.
        self.on_each = ControlParameterOutput(
            tooltip="Fires at the start of each iteration. Connect to the first node inside the loop body.",
            name="on_each",
        )
        self.on_each.ui_options = {"display_name": "On Each"}
        self.add_parameter(self.on_each)
        # No ordering needed here: the renderer pins control ports to the top of a rail by parameter
        # type, and the rail list is an unordered membership set (see LEFT_PARAMETERS_KEY).
        self._register_side_parameter(LEFT_PARAMETERS_KEY, self.on_each.name)

        # Initialize iteration state
        self._items = []
        self._current_iteration_count = 0
        self._total_iterations = 0
        self._results_list = []

        # Hidden boolean parameter used by node_executor for execution logic
        self.run_in_order = Parameter(
            name="run_in_order",
            tooltip="Execute all iterations in order or concurrently",
            type=ParameterTypeBuiltin.BOOL.value,
            allowed_modes={ParameterMode.PROPERTY},
            default_value=True,
            hide=True,
        )
        self.add_parameter(self.run_in_order)

        # User selection that controls run_in_order
        self.execution_mode = Parameter(
            name="execution_mode",
            tooltip="Execute all iterations in order or concurrently",
            type=ParameterTypeBuiltin.STR.value,
            allowed_modes={ParameterMode.PROPERTY},
            default_value=EXECUTION_MODE_ONE_AT_A_TIME,
            traits={Options(choices=EXECUTION_MODE_CHOICES, show_search=False)},
            ui_options={"display_name": "Execution Mode"},
        )
        self.add_parameter(self.execution_mode)

        # Index parameter - available in all iterative nodes (left side - feeds into group)
        self.index_param = Parameter(
            name="index",
            tooltip="Current index of the iteration",
            type=ParameterTypeBuiltin.INT.value,
            allowed_modes={ParameterMode.OUTPUT},
            settable=False,
            default_value=0,
        )
        self.add_parameter(self.index_param)

        # Track left parameters for UI layout
        self._register_side_parameter(LEFT_PARAMETERS_KEY, self.index_param.name)

        # Control input for loop completion (right side - primary loop completion path)
        self.loop_complete = ControlParameterInput(
            tooltip="Signal that this iteration is complete and continue to next iteration",
            name=IterationControlParam.LOOP_COMPLETE.value,
        )
        self.loop_complete.ui_options = {"display_name": "Loop Complete"}
        self.add_parameter(self.loop_complete)

        # Data parameter for the item to add (right side - collects from group)
        self.new_item_to_add = Parameter(
            name="new_item_to_add",
            tooltip="Item to add to results list for each iteration",
            type=ParameterTypeBuiltin.ANY.value,
            allowed_modes={ParameterMode.INPUT},
        )
        self.add_parameter(self.new_item_to_add)

        # Skip and Break control inputs (right side - for loop control)
        self.skip_iteration = ControlParameterInput(
            tooltip="Skip current item and continue to next iteration",
            name=IterationControlParam.SKIP_ITERATION.value,
        )
        self.skip_iteration.ui_options = {"display_name": "Skip to Next Iteration"}
        self.add_parameter(self.skip_iteration)

        self.break_loop = ControlParameterInput(
            tooltip="Break out of loop immediately",
            name=IterationControlParam.BREAK_LOOP.value,
        )
        self.break_loop.ui_options = {"display_name": "Break Out of Loop"}
        self.add_parameter(self.break_loop)

        self.results = Parameter(
            name="results",
            tooltip="Collected results from all iterations",
            output_type="list",
            allowed_modes={ParameterMode.OUTPUT},
        )
        self.add_parameter(self.results)

        self.flatten_results = ParameterBool(
            name="flatten_results",
            tooltip="When an iteration adds a list, put its items into Results instead of the list itself.",
            default_value=False,
            allowed_modes={ParameterMode.PROPERTY},
            display_name="Flatten Results",
        )
        self.add_parameter(self.flatten_results)

        self.flatten_depth = ParameterInt(
            name="flatten_depth",
            tooltip="How many levels of nesting to flatten. 0 flattens every level.",
            default_value=0,
            min_val=0,
            allowed_modes={ParameterMode.PROPERTY},
            display_name="Flatten Depth",
            hide=True,
        )
        self.add_parameter(self.flatten_depth)
        self.add_parameter_to_group_settings(self.flatten_results)
        self.add_parameter_to_group_settings(self.flatten_depth)

        # Track right parameters for UI layout
        for parameter_name in (
            self.loop_complete.name,
            self.new_item_to_add.name,
            self.skip_iteration.name,
            self.break_loop.name,
            self.results.name,
        ):
            self._register_side_parameter(RIGHT_PARAMETERS_KEY, parameter_name)

    def get_parameter_by_name(self, param_name: str) -> Parameter | None:
        # Connections in a workflow saved while the loop carried the base pair name group_exec_in /
        # group_exec_out. Answer with the loop port that replaced each so those workflows load and run;
        # the connection is recorded against the Parameter, so the next save writes the current name.
        return super().get_parameter_by_name(LEGACY_CONTROL_PORT_NAMES.get(param_name, param_name))

    def after_value_set(self, parameter: Parameter, value: Any) -> None:
        """Handle parameter value changes."""
        super().after_value_set(parameter, value)
        if parameter == self.execution_mode:
            # Convert string choice to boolean and update run_in_order parameter
            run_in_order = EXECUTION_MODE_VALUE_LOOKUP.get(value, True)
            self.set_parameter_value("run_in_order", run_in_order)

            # Skip works in both modes: a skipped iteration adds nothing to results. Break is only
            # shown when running in order, since in parallel every iteration has already started.
            if run_in_order:
                self.show_parameter_by_name(self.break_loop.name)
            else:
                self.hide_parameter_by_name(self.break_loop.name)
        elif parameter == self.flatten_results:
            if value:
                self.show_parameter_by_name(self.flatten_depth.name)
            else:
                self.hide_parameter_by_name(self.flatten_depth.name)

    def get_next_control_output(self) -> Parameter | None:
        # Say which port the chain continues from once every iteration has finished, rather than
        # leaning on the base's first-control-output scan and the order ports happen to be added in.
        return self.exec_out

    @abstractmethod
    def _get_iteration_items(self) -> list[Any]:
        """Get the list of items to iterate over.

        Returns:
            List of items for iteration. Empty list if no items.
        """

    @abstractmethod
    def _get_current_item_value(self, iteration_index: int) -> Any:
        """Get the value for a specific iteration.

        Args:
            iteration_index: 0-based iteration index

        Returns:
            The value to use for this iteration
        """

    def _initialize_iteration_data(self) -> None:
        """Initialize iteration-specific data and state."""
        self._items = self._get_iteration_items()
        self._total_iterations = len(self._items) if self._items else 0
        self._current_iteration_count = 0
        self._results_list = []

    def _get_total_iterations(self) -> int:
        """Return the total number of iterations for this loop."""
        return self._total_iterations

    def get_all_iteration_values(self) -> list[int]:
        """Calculate and return all iteration index values.

        For ForEach nodes, this returns indices 0, 1, 2, ...
        For ForLoop nodes, this could return actual loop values.

        Returns:
            List of integer values for each iteration
        """
        return list(range(self._get_total_iterations()))

    def _output_results_list(self) -> None:
        """Output the current results list to the results parameter."""
        # Shallow copy: items are never mutated after append, and the caller always
        # replaces _results_list via `= []` rather than clearing in-place, so shared
        # item references are safe. deepcopy was breaking websocket payload deduplication
        # by creating new object identities for every item on every broadcast.
        if self.get_parameter_value(self.flatten_results.name):
            depth = max(self.get_parameter_value(self.flatten_depth.name) or 0, 0)
            self.parameter_output_values["results"] = flatten_list(self._results_list, depth)
        else:
            self.parameter_output_values["results"] = list(self._results_list)

    def reset_for_workflow_run(self) -> None:
        """Reset state for a fresh workflow run."""
        self._results_list = []
        self._current_iteration_count = 0
        self._total_iterations = 0
        self._output_results_list()

    async def aprocess(self) -> None:
        """Execute the iterative node group.

        Note: This method is typically not called directly. The NodeExecutor
        detects BaseIterativeNodeGroup instances and calls handle_iterative_group_execution()
        instead. This implementation exists as a fallback for direct local execution.
        """
        # For direct local execution (when NodeExecutor doesn't intercept),
        # just execute the subflow once. The NodeExecutor handles iteration logic.
        await self.execute_subflow()
