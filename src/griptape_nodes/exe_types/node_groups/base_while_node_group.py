"""Base class for while-loop node groups."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from griptape_nodes.exe_types.core_types import (
    ControlParameterInput,
    Parameter,
    ParameterMode,
    ParameterTypeBuiltin,
)
from griptape_nodes.exe_types.node_groups.subflow_node_group import (
    LEFT_PARAMETERS_KEY,
    RIGHT_PARAMETERS_KEY,
    SubflowNodeGroup,
)

DEFAULT_MAX_ITERATIONS = 3


class WhileControlParam(StrEnum):
    """Parameter names for loop control on while node groups."""

    DONE = "done"
    CONTINUE = "continue_loop"


class BaseWhileNodeGroup(SubflowNodeGroup):
    """Base class for while-loop node groups.

    A group that executes its child nodes in a loop, re-executing them when the
    'continue_loop' control input is triggered, up to a configurable maximum
    number of iterations.

    Child nodes connect their control outputs to the group's 'done' and
    'continue_loop' control input parameters on the right side.
    When 'continue_loop' is triggered and iterations remain, the group re-executes.
    When 'done' is triggered or iterations are exhausted, execution completes.

    Subclasses may override:
        - _before_loop_iteration(iteration: int, flow_name: str): Called before each iteration (after the first)
        - _on_complete(*, condition_met: bool, iterations: int): Called when the loop finishes
    """

    # Loop state
    _current_iteration: int
    _max_iterations_value: int

    def __init__(
        self,
        name: str,
        metadata: dict[Any, Any] | None = None,
    ) -> None:
        super().__init__(name, metadata)

        # Initialize loop state
        self._current_iteration = 0
        self._max_iterations_value = DEFAULT_MAX_ITERATIONS

        # Max iterations parameter (property, shown in group settings)
        self.max_iterations = Parameter(
            name="max_iterations",
            tooltip="Maximum number of loop iterations (0 means run once with no re-iterations)",
            type=ParameterTypeBuiltin.INT.value,
            allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            default_value=DEFAULT_MAX_ITERATIONS,
        )
        self.add_parameter(self.max_iterations)

        # Iteration output (left side - feeds into group)
        self.iteration = Parameter(
            name="iteration",
            tooltip="Current iteration number (0-based)",
            type=ParameterTypeBuiltin.INT.value,
            allowed_modes={ParameterMode.OUTPUT},
            settable=False,
            default_value=0,
        )
        self.add_parameter(self.iteration)

        # Track left parameters for UI layout
        if LEFT_PARAMETERS_KEY not in self.metadata:
            self.metadata[LEFT_PARAMETERS_KEY] = []
        self.metadata[LEFT_PARAMETERS_KEY].append("iteration")

        # Control input for done (right side)
        self.done = ControlParameterInput(
            tooltip="Signal that the loop condition is met - stop looping",
            name=WhileControlParam.DONE.value,
            display_name="Done",
        )
        self.add_parameter(self.done)

        # Control input for continue (right side)
        self.continue_loop = ControlParameterInput(
            tooltip="Signal that the loop should continue - iterate again if iterations remain",
            name=WhileControlParam.CONTINUE.value,
            display_name="Continue",
        )
        self.add_parameter(self.continue_loop)

        # Total iterations output (right side - available after loop completes)
        self.total_iterations = Parameter(
            name="total_iterations",
            tooltip="Total number of iterations that were executed",
            type=ParameterTypeBuiltin.INT.value,
            allowed_modes={ParameterMode.OUTPUT},
            settable=False,
            default_value=0,
        )
        self.add_parameter(self.total_iterations)

        # Track right parameters for UI layout
        if RIGHT_PARAMETERS_KEY not in self.metadata:
            self.metadata[RIGHT_PARAMETERS_KEY] = []
        self.metadata[RIGHT_PARAMETERS_KEY].extend(
            [
                WhileControlParam.DONE.value,
                WhileControlParam.CONTINUE.value,
                "total_iterations",
            ]
        )

    def _before_loop_iteration(self, iteration: int, flow_name: str) -> None:  # noqa: ARG002
        """Called before each loop iteration (after the first).

        Subclasses can override this to prepare state for the next iteration.

        Args:
            iteration: The upcoming iteration number (1-based)
            flow_name: Name of the deserialized flow being executed
        """
        # Reset all nodes to UNRESOLVED so they re-execute. The DAG builder
        # skips RESOLVED upstream dependencies, so without this reset nodes
        # won't be added to the DAG and won't run on subsequent iterations.
        self.engine.flow_manager.unresolve_all_nodes_in_flow(flow_name)

    def _on_complete(self, *, condition_met: bool, iterations: int) -> None:  # noqa: ARG002
        """Called after all loop iterations are finished, before the node resolves.

        Subclasses can override this to update UI state, log summaries, clean up resources, etc.
        Subclasses that override must call super()._on_complete() to ensure base outputs are set.

        Args:
            condition_met: Whether the loop's 'done' condition was triggered
            iterations: Total number of iterations that were executed
        """
        self.parameter_output_values["total_iterations"] = iterations

    def _initialize_loop_data(self) -> None:
        """Initialize loop-specific data and state."""
        self._current_iteration = 0
        max_iterations = self.get_parameter_value("max_iterations")
        self._max_iterations_value = max_iterations if max_iterations is not None else DEFAULT_MAX_ITERATIONS

    def _get_max_iterations(self) -> int:
        """Return the maximum number of iterations."""
        return self._max_iterations_value

    def reset_for_workflow_run(self) -> None:
        """Reset state for a fresh workflow run."""
        self._current_iteration = 0
        self._max_iterations_value = DEFAULT_MAX_ITERATIONS

    async def aprocess(self) -> None:
        """Execute the while-loop node group.

        Note: This method is typically not called directly. The NodeExecutor
        detects BaseWhileNodeGroup instances and calls handle_while_group_execution()
        instead. This implementation exists as a fallback for direct local execution.
        """
        await self.execute_subflow()
