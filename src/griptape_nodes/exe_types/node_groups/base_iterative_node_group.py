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
from griptape_nodes.traits.options import Options

# Execution mode choices and their corresponding boolean values (True = run in order)
EXECUTION_MODE_ONE_AT_A_TIME = "Run Group Items One at a Time"
EXECUTION_MODE_ALL_AT_ONCE = "Run Group Items All at Once"
EXECUTION_MODE_CHOICES = [EXECUTION_MODE_ONE_AT_A_TIME, EXECUTION_MODE_ALL_AT_ONCE]
EXECUTION_MODE_VALUE_LOOKUP = {
    EXECUTION_MODE_ONE_AT_A_TIME: True,
    EXECUTION_MODE_ALL_AT_ONCE: False,
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
        super().__init__(name, metadata)

        # Top-level control flow ports — wires this group into a control chain
        self.exec_in = ControlParameterInput(
            tooltip="Start the loop",
            name="exec_in",
        )
        self.exec_in.ui_options = {"display_name": "Start Loop"}
        self.add_parameter(self.exec_in)
        # The GroupNode renderer orders rail params by their position in LEFT/RIGHT_PARAMETERS_KEY.
        # Inserting at index 0 pins the control port to the top of the rail, above data parameters.
        if LEFT_PARAMETERS_KEY not in self.metadata:
            self.metadata[LEFT_PARAMETERS_KEY] = []
        self.metadata[LEFT_PARAMETERS_KEY].insert(0, self.exec_in.name)

        # Per-iteration control output — pairs visually with exec_in on the LEFT rail.
        # Connect this to the first body node to make that node each iteration's entry point.
        # If unconnected, executor falls back to implicit child-discovery.
        self.on_each = ControlParameterOutput(
            tooltip="Fired at the start of each iteration. Connect to the first node in the loop body.",
            name="on_each",
        )
        self.on_each.ui_options = {"display_name": "On Each"}
        self.add_parameter(self.on_each)
        self.metadata[LEFT_PARAMETERS_KEY].append(self.on_each.name)

        self.exec_out = ControlParameterOutput(
            tooltip="Fired after all iterations complete",
            name="exec_out",
        )
        self.exec_out.ui_options = {"display_name": "On Complete"}
        self.add_parameter(self.exec_out)
        # Insert at index 0 so exec_out appears at the top of the right rail (see LEFT_PARAMETERS_KEY comment above).
        if RIGHT_PARAMETERS_KEY not in self.metadata:
            self.metadata[RIGHT_PARAMETERS_KEY] = []
        self.metadata[RIGHT_PARAMETERS_KEY].insert(0, self.exec_out.name)

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
        if LEFT_PARAMETERS_KEY not in self.metadata:
            self.metadata[LEFT_PARAMETERS_KEY] = []
        self.metadata[LEFT_PARAMETERS_KEY].append(self.index_param.name)

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

        # Track right parameters for UI layout
        if RIGHT_PARAMETERS_KEY not in self.metadata:
            self.metadata[RIGHT_PARAMETERS_KEY] = []
        self.metadata[RIGHT_PARAMETERS_KEY].extend(
            [
                self.loop_complete.name,
                self.new_item_to_add.name,
                self.skip_iteration.name,
                self.break_loop.name,
                self.results.name,
            ]
        )

    def after_value_set(self, parameter: Parameter, value: Any) -> None:
        """Handle parameter value changes."""
        super().after_value_set(parameter, value)
        if parameter == self.execution_mode:
            # Convert string choice to boolean and update run_in_order parameter
            run_in_order = EXECUTION_MODE_VALUE_LOOKUP.get(value, True)
            self.set_parameter_value("run_in_order", run_in_order)

            # Hide or show skip/break controls based on execution mode
            # Skip and Break are only supported in sequential mode (run_in_order=True)
            if run_in_order:
                # Show controls when running sequentially
                self.show_parameter_by_name(self.skip_iteration.name)
                self.show_parameter_by_name(self.break_loop.name)
            else:
                # Hide controls when running in parallel (not supported)
                self.hide_parameter_by_name(self.skip_iteration.name)
                self.hide_parameter_by_name(self.break_loop.name)

    def get_next_control_output(self) -> Parameter | None:
        # Without this override the base returns None and the DAG executor stops at the group
        # after all iterations complete, never advancing to whatever is wired downstream of exec_out.
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
