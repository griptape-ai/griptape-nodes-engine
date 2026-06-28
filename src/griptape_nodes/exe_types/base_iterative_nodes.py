import asyncio
import copy
import logging
from abc import abstractmethod
from enum import StrEnum
from typing import Any, NamedTuple

from griptape_nodes.exe_types.core_types import (
    ControlParameterInput,
    ControlParameterOutput,
    Parameter,
    ParameterGroup,
    ParameterMessage,
    ParameterMode,
    ParameterTypeBuiltin,
)
from griptape_nodes.exe_types.flow import ControlFlow
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.exe_types.param_components.progress_bar_component import ProgressBarComponent


def _outgoing_connection_exists(source_node: str, source_param: str) -> bool:
    """Check if a source node/parameter has any outgoing connections.

    Args:
        source_node: Name of the node that would be sending the connection
        source_param: Name of the parameter on that node

    Returns:
        True if the parameter has at least one outgoing connection, False otherwise

    Logic: Look in connections.outgoing_index[source_node][source_param]
    """
    from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

    connections = GriptapeNodes.FlowManager().get_connections()

    # Check if source_node has any outgoing connections at all
    source_connections = connections.outgoing_index.get(source_node)
    if source_connections is None:
        return False

    # Check if source_param has any outgoing connections
    param_connections = source_connections.get(source_param)
    if param_connections is None:
        return False

    # Return True if connections list is populated
    return bool(param_connections)


def _incoming_connection_exists(target_node: str, target_param: str) -> bool:
    """Check if a target node/parameter has any incoming connections.

    Args:
        target_node: Name of the node that would be receiving the connection
        target_param: Name of the parameter on that node

    Returns:
        True if the parameter has at least one incoming connection, False otherwise

    Logic: Look in connections.incoming_index[target_node][target_param]
    """
    from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

    connections = GriptapeNodes.FlowManager().get_connections()

    # Check if target_node has any incoming connections at all
    target_connections = connections.incoming_index.get(target_node)
    if target_connections is None:
        return False

    # Check if target_param has any incoming connections
    param_connections = target_connections.get(target_param)
    if param_connections is None:
        return False

    # Return True if connections list is populated
    return bool(param_connections)


class IterativeNodeParam(StrEnum):
    """Parameter names for iterative start and end nodes."""

    # BaseIterativeStartNode parameters
    EXEC_IN = "exec_in"
    EXEC_OUT = "exec_out"
    INDEX = "index"
    LOOP = "loop"
    TRIGGER_NEXT_ITERATION_SIGNAL = "trigger_next_iteration_signal"
    BREAK_LOOP_SIGNAL = "break_loop_signal"
    LOOP_END_CONDITION_MET_SIGNAL = "loop_end_condition_met_signal"
    STATUS_MESSAGE = "status_message"

    # BaseIterativeEndNode parameters
    FROM_START = "from_start"
    ADD_ITEM = "add_item"
    NEW_ITEM_TO_ADD = "new_item_to_add"
    RESULTS = "results"
    SKIP_ITERATION = "skip_iteration"
    BREAK_LOOP = "break_loop"
    LOOP_END_CONDITION_MET_SIGNAL_INPUT = "loop_end_condition_met_signal_input"
    TRIGGER_NEXT_ITERATION_SIGNAL_OUTPUT = "trigger_next_iteration_signal_output"
    BREAK_LOOP_SIGNAL_OUTPUT = "break_loop_signal_output"


class NodeParameterPair(NamedTuple):
    """A named tuple for storing a pair of node and parameters for connections.

    Fields:
        node: The node the parameter lives on
        parameter: The parameter connected
    """

    node: BaseNode
    parameter: Parameter


class BaseIterativeStartNode(BaseNode):
    """Base class for all iterative start nodes (ForEach, ForLoop, etc.).

    This class consolidates all shared signal logic, connection management,
    state tracking, and validation logic used by iterative loop start nodes.
    """

    end_node: "BaseIterativeEndNode | None" = None
    exec_out: ControlParameterOutput
    _current_iteration_count: int
    _total_iterations: int
    _flow: ControlFlow | None = None

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)
        self._current_iteration_count = 0

        # This is the total number of iterations that WILL be run (calculated during init)
        self._total_iterations = 0

        # Connection tracking for validation
        self._connected_parameters: set[str] = set()

        # Main control flow
        self.exec_in = ControlParameterInput(tooltip="Start Loop", name=IterativeNodeParam.EXEC_IN.value)
        self.exec_in.ui_options = {"display_name": "Start Loop"}
        self.add_parameter(self.exec_in)

        # On Each Item control output - moved outside group for proper rendering
        self.exec_out = ControlParameterOutput(
            tooltip=self._get_exec_out_tooltip(), name=IterativeNodeParam.EXEC_OUT.value
        )
        self.exec_out.ui_options = {"display_name": self._get_exec_out_display_name()}
        self.add_parameter(self.exec_out)

        # Create parameter group for iteration data
        with ParameterGroup(name=self._get_parameter_group_name()) as group:
            # Add index parameter that all iterative nodes have
            self.index_count = Parameter(
                name=IterativeNodeParam.INDEX.value,
                tooltip="Current index of the iteration",
                type=ParameterTypeBuiltin.INT.value,
                allowed_modes={ParameterMode.PROPERTY, ParameterMode.OUTPUT},
                settable=False,
                default_value=0,
                hide_property=True,
            )
        self.add_node_element(group)

        # Explicit tethering to corresponding End node (hidden)
        self.loop = Parameter(
            name=IterativeNodeParam.LOOP.value,
            tooltip="Connected Loop End Node",
            output_type=ParameterTypeBuiltin.ALL.value,
            allowed_modes={ParameterMode.OUTPUT},
        )
        self.loop.ui_options = {"hide": True, "display_name": "Loop End Node"}
        self.add_parameter(self.loop)

        # Hidden signal inputs from End node
        self.trigger_next_iteration_signal = ControlParameterInput(
            tooltip="Signal from End to continue to next iteration",
            name=IterativeNodeParam.TRIGGER_NEXT_ITERATION_SIGNAL.value,
        )
        self.trigger_next_iteration_signal.ui_options = {"hide": True, "display_name": "Next Iteration Signal"}
        self.trigger_next_iteration_signal.settable = False

        self.break_loop_signal = ControlParameterInput(
            tooltip="Signal from End to break out of loop", name=IterativeNodeParam.BREAK_LOOP_SIGNAL.value
        )
        self.break_loop_signal.ui_options = {"hide": True, "display_name": "Break Loop Signal"}
        self.break_loop_signal.settable = False

        # Hidden control output - loop end condition
        self.loop_end_condition_met_signal = ControlParameterOutput(
            tooltip="Signal to End when loop should end", name=IterativeNodeParam.LOOP_END_CONDITION_MET_SIGNAL.value
        )
        self.loop_end_condition_met_signal.ui_options = {"hide": True, "display_name": "Loop End Signal"}
        self.loop_end_condition_met_signal.settable = False

        # Add hidden parameters
        self.add_parameter(self.trigger_next_iteration_signal)
        self.add_parameter(self.break_loop_signal)
        self.add_parameter(self.loop_end_condition_met_signal)

        # Control output tracking
        self.next_control_output: Parameter | None = None
        self._logger = logging.getLogger(f"{__name__}.{self.name}")

        # Progress bar
        self._progress_bar = ProgressBarComponent(self)
        self._progress_bar.add_property_parameters()

        # Hidden status_message kept for backward-compat (standard library nodes reference it for positioning)
        self.status_message = ParameterMessage(
            name=IterativeNodeParam.STATUS_MESSAGE.value,
            variant="info",
            value="",
            ui_options={"hide": True},
        )
        self.add_node_element(self.status_message)

    def _get_base_node_type_name(self) -> str:
        """Get the base node type name (e.g., 'ForLoop' from 'ForLoopStartNode')."""
        return self.__class__.__name__.replace("StartNode", "")

    @classmethod
    @abstractmethod
    def _get_compatible_end_classes(cls) -> set[type]:
        """Return the set of End node classes that this Start node can connect to."""

    @abstractmethod
    def _get_parameter_group_name(self) -> str:
        """Return the name for the parameter group containing iteration data."""

    @abstractmethod
    def _get_exec_out_display_name(self) -> str:
        """Return the display name for the exec_out parameter."""

    @abstractmethod
    def _get_exec_out_tooltip(self) -> str:
        """Return the tooltip for the exec_out parameter."""

    @abstractmethod
    def _get_iteration_items(self) -> list[Any]:
        """Get the list of items to iterate over."""

    @abstractmethod
    def _initialize_iteration_data(self) -> None:
        """Initialize iteration-specific data and state."""

    @abstractmethod
    def _get_current_item_value(self) -> Any:
        """Get the current iteration value."""

    @abstractmethod
    def is_loop_finished(self) -> bool:
        """Return True if the loop has completed all iterations.

        This method must be implemented by subclasses to define when
        the loop should terminate.
        """

    @abstractmethod
    def _get_total_iterations(self) -> int:
        """Return the total number of iterations for this loop."""

    @abstractmethod
    def _get_current_iteration_count(self) -> int:
        """Return the current iteration count (0-based)."""

    @abstractmethod
    def get_current_index(self) -> int:
        """Return the current index value for this iteration type.

        For ForEach: returns array position (0, 1, 2, ...)
        For ForLoop: returns actual loop value (start, start+step, start+2*step, ...)
        """

    @abstractmethod
    def _advance_to_next_iteration(self) -> None:
        """Advance to the next iteration.

        For ForEach: increment index by 1
        For ForLoop: increment current value by step, increment index by 1
        """

    def get_all_iteration_values(self) -> list[int]:
        """Calculate and return all iteration values for this loop.

        For ForEach nodes, this returns indices 0, 1, 2, ...
        For ForLoop nodes, this returns actual loop values (start, start+step, start+2*step, ...).

        This is used by parallel execution to set correct parameter values for each iteration.

        Returns:
            List of integer values for each iteration
        """
        # Default implementation for ForEach: return 0-based indices
        return list(range(self._get_total_iterations()))

    async def aprocess(self) -> None:
        # Overrides aprocess() directly (rather than process()) because iterations
        # need await asyncio.sleep(0) to let the event loop publish UI updates mid-loop.
        # BaseIterativeEndNode uses process() since it has no async work.
        if self._flow is None:
            return

        # Handle different control entry points with direct logic
        match self._entry_control_parameter:
            case self.exec_in | None:
                # Starting the loop (initialization)
                self._initialize_loop()
                self._check_completion_and_set_output()
            case self.trigger_next_iteration_signal:
                # Next iteration signal from End - advance to next iteration
                self._advance_to_next_iteration()
                self._progress_bar.increment()
                await asyncio.sleep(0)
                self._check_completion_and_set_output()
            case self.break_loop_signal:
                # Break signal from End - halt loop immediately
                self._complete_loop()
            case _:
                # Unexpected control entry point - log error for debugging
                err_str = f"Iterative Start node '{self.name}' received unexpected control parameter: {self._entry_control_parameter}. "
                "Expected: exec_in, trigger_next_iteration_signal, break_loop_signal, or None."
                self._logger.error(err_str)
                return

    def _validate_start_node(self) -> list[Exception] | None:
        """Common validation logic for both workflow and node run validation."""
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        exceptions = []

        # Validate end node connection
        if self.end_node is None:
            msg = f"{self.name}: End node not found or connected."
            exceptions.append(Exception(msg))

        # Validate all required connections exist
        validation_errors = self._validate_iterative_connections()
        if validation_errors:
            exceptions.extend(validation_errors)

        try:
            flow = GriptapeNodes.ObjectManager().get_object_by_name(
                GriptapeNodes.NodeManager().get_node_parent_flow_by_name(self.name)
            )
            if isinstance(flow, ControlFlow):
                self._flow = flow
        except Exception as e:
            exceptions.append(e)
        return exceptions

    def validate_before_workflow_run(self) -> list[Exception] | None:
        return self._validate_start_node()

    def validate_before_node_run(self) -> list[Exception] | None:
        return self._validate_start_node()

    def get_next_control_output(self) -> Parameter | None:
        return self.next_control_output

    def allow_outgoing_connection(
        self,
        source_parameter: Parameter,
        target_node: BaseNode,
        target_parameter: Parameter,
    ) -> bool:
        """Validate outgoing connections for type safety."""
        # Check if this is a loop tethering connection
        if source_parameter == self.loop:
            # Ensure target node is compatible
            compatible_end_classes = self._get_compatible_end_classes()
            compatible_class_names = {cls.__name__ for cls in compatible_end_classes}
            target_class_name = target_node.__class__.__name__
            if target_class_name not in compatible_class_names:
                self._logger.warning(
                    "Incompatible connection: %s can only connect to %s, but attempted to connect to %s",
                    self.__class__.__name__,
                    list(compatible_class_names),
                    target_class_name,
                )
                return False
        return super().allow_outgoing_connection(source_parameter, target_node, target_parameter)

    @classmethod
    def allow_outgoing_connection_by_class(
        cls,
        target_node_class: type[BaseNode],
        source_parameter_name: str,  # We always know OUR parameter name
        target_parameter_name: str | None,  # noqa: ARG003 - May not know OTHER node's parameter
    ) -> bool:
        """Class-level validation for outgoing connections from iterative start nodes."""
        # Check if this is a loop tethering connection (by OUR parameter name)
        if source_parameter_name == IterativeNodeParam.LOOP.value:
            compatible_end_classes = cls._get_compatible_end_classes()
            compatible_class_names = {cls.__name__ for cls in compatible_end_classes}
            target_class_name = target_node_class.__name__
            if target_class_name not in compatible_class_names:
                return False
        return True

    @classmethod
    def allow_incoming_connection_by_class(
        cls,
        source_node_class: type[BaseNode],
        source_parameter_name: str | None,  # noqa: ARG003 - May not know OTHER node's parameter
        target_parameter_name: str,  # We always know OUR parameter name
    ) -> bool:
        """Class-level validation for incoming connections to iterative start nodes."""
        # Check signal connections (by OUR parameter name)
        if target_parameter_name in (
            IterativeNodeParam.TRIGGER_NEXT_ITERATION_SIGNAL.value,
            IterativeNodeParam.BREAK_LOOP_SIGNAL.value,
        ):
            compatible_end_classes = cls._get_compatible_end_classes()
            compatible_class_names = {cls.__name__ for cls in compatible_end_classes}
            source_class_name = source_node_class.__name__
            if source_class_name not in compatible_class_names:
                return False
        return True

    def allow_incoming_connection(
        self,
        source_node: BaseNode,
        source_parameter: Parameter,
        target_parameter: Parameter,
    ) -> bool:
        """Validate incoming connections for type safety."""
        # Check signal connections from End nodes
        if target_parameter in (self.trigger_next_iteration_signal, self.break_loop_signal):
            # Ensure source node is compatible
            compatible_end_classes = self._get_compatible_end_classes()
            compatible_class_names = {cls.__name__ for cls in compatible_end_classes}
            source_class_name = source_node.__class__.__name__
            if source_class_name not in compatible_class_names:
                self._logger.warning(
                    "Incompatible connection: %s can only receive signals from %s, but %s attempted to connect",
                    self.__class__.__name__,
                    list(compatible_class_names),
                    source_class_name,
                )
                return False
        return super().allow_incoming_connection(source_node, source_parameter, target_parameter)

    def advance_sequential_progress(self, iteration_index: int) -> None:
        """Update progress bar and index display after one sequential iteration completes."""
        self._progress_bar.increment()
        all_values = self.get_all_iteration_values()
        if iteration_index < len(all_values):
            current_index = all_values[iteration_index]
            self.parameter_output_values[IterativeNodeParam.INDEX.value] = current_index
            self.publish_update_to_parameter(IterativeNodeParam.INDEX.value, current_index)

    def _initialize_loop(self) -> None:
        """Initialize the loop with fresh parameter values."""
        # Reset all state for fresh loop execution
        self._current_iteration_count = 0
        self.next_control_output = None

        # Reset the coupled End node's state for fresh loop runs
        if self.end_node and isinstance(self.end_node, BaseIterativeEndNode):
            self.end_node.reset_for_workflow_run()

        # Initialize iteration-specific data and set total iterations
        self._initialize_iteration_data()
        self._total_iterations = self._get_total_iterations()
        self._progress_bar.initialize(self._total_iterations)

    def _check_completion_and_set_output(self) -> None:
        """Check if loop should end or continue and set appropriate control output."""
        if self.is_loop_finished():
            self._complete_loop()
            return

        # Continue with current item - unresolve future nodes for fresh evaluation
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        connections = GriptapeNodes.FlowManager().get_connections()
        connections.unresolve_future_nodes(self)

        # Always set the index output in base class
        current_index = self.get_current_index()
        self.parameter_output_values[IterativeNodeParam.INDEX.value] = current_index
        self.publish_update_to_parameter(IterativeNodeParam.INDEX.value, current_index)

        # Get current item value from subclass (subclasses handle their own logic)
        current_item_value = self._get_current_item_value()
        if current_item_value is not None:
            # Subclasses can handle their own current_item logic
            pass

        self.next_control_output = self.exec_out

    def _complete_loop(self) -> None:
        """Complete the loop and set final state."""
        self._current_iteration_count = 0
        self._total_iterations = 0
        self.next_control_output = self.loop_end_condition_met_signal

    def _validate_iterative_connections(self) -> list[Exception]:
        """Validate that all required iterative connections are properly established."""
        errors = []
        node_type = self._get_base_node_type_name()

        # Check if exec_out has outgoing connections
        if not _outgoing_connection_exists(self.name, self.exec_out.name):
            exec_out_display_name = self._get_exec_out_display_name()
            errors.append(
                Exception(
                    f"{self.name}: Missing required connection from '{exec_out_display_name}'. "
                    f"REQUIRED ACTION: Connect {node_type} Start '{exec_out_display_name}' to interior loop nodes. "
                    "The start node must connect to other nodes to execute the loop body."
                )
            )

        # Check if loop has outgoing connection to End
        if self.end_node is None:
            errors.append(
                Exception(
                    f"{self.name}: Missing required tethering connection. "
                    f"REQUIRED ACTION: Connect {node_type} Start 'Loop End Node' to {node_type} End 'Loop Start Node'. "
                    "This establishes the explicit relationship between start and end nodes."
                )
            )

        # Check if all hidden signal connections exist (only if end_node is connected)
        if self.end_node:
            # Check trigger_next_iteration_signal connection
            if not _incoming_connection_exists(self.name, self.trigger_next_iteration_signal.name):
                errors.append(
                    Exception(
                        f"{self.name}: Missing hidden signal connection. "
                        f"REQUIRED ACTION: Connect {node_type} End 'Next Iteration Signal Output' to {node_type} Start 'Next Iteration Signal'. "
                        "This signal tells the start node to continue to the next item."
                    )
                )

            # Check break_loop_signal connection
            if not _incoming_connection_exists(self.name, self.break_loop_signal.name):
                errors.append(
                    Exception(
                        f"{self.name}: Missing hidden signal connection. "
                        f"REQUIRED ACTION: Connect {node_type} End 'Break Loop Signal Output' to {node_type} Start 'Break Loop Signal'. "
                        "This signal tells the start node to break out of the loop early."
                    )
                )

        return errors

    def after_incoming_connection(
        self,
        source_node: BaseNode,
        source_parameter: Parameter,
        target_parameter: Parameter,
    ) -> None:
        # Track incoming connections for validation
        self._connected_parameters.add(target_parameter.name)
        return super().after_incoming_connection(source_node, source_parameter, target_parameter)

    def after_incoming_connection_removed(
        self,
        source_node: BaseNode,
        source_parameter: Parameter,
        target_parameter: Parameter,
    ) -> None:
        # Remove from tracking when connection is removed
        self._connected_parameters.discard(target_parameter.name)
        return super().after_incoming_connection_removed(source_node, source_parameter, target_parameter)

    def after_outgoing_connection(
        self,
        source_parameter: Parameter,
        target_node: BaseNode,
        target_parameter: Parameter,
    ) -> None:
        if source_parameter == self.loop and isinstance(target_node, BaseIterativeEndNode):
            self.end_node = target_node
        return super().after_outgoing_connection(source_parameter, target_node, target_parameter)

    def after_outgoing_connection_removed(
        self,
        source_parameter: Parameter,
        target_node: BaseNode,
        target_parameter: Parameter,
    ) -> None:
        if source_parameter == self.loop and isinstance(target_node, BaseIterativeEndNode):
            self.end_node = None
        return super().after_outgoing_connection_removed(source_parameter, target_node, target_parameter)


class BaseIterativeEndNode(BaseNode):
    """Base class for all iterative end nodes (ForEach, ForLoop, etc.).

    This class consolidates all shared signal logic, connection management,
    conditional evaluation, and result accumulation logic used by iterative loop end nodes.
    """

    start_node: "BaseIterativeStartNode | None" = None

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)
        self.start_node = None

        # End node manages its own results list
        self._results_list: list[Any] = []

        # Connection tracking for validation
        self._connected_parameters: set[str] = set()

        # Explicit tethering to Start node
        self.from_start = Parameter(
            name=IterativeNodeParam.FROM_START.value,
            tooltip="Connected Loop Start Node",
            input_types=[ParameterTypeBuiltin.ALL.value],
            allowed_modes={ParameterMode.INPUT},
        )
        self.from_start.ui_options = {"hide": True, "display_name": "Loop Start Node"}

        # Main control input and data parameter
        self.add_item_control = ControlParameterInput(
            tooltip="Add current item to output and continue loop", name=IterativeNodeParam.ADD_ITEM.value
        )
        self.add_item_control.ui_options = {"display_name": "Add Item to Output"}

        # Data input for the item to add - positioned right under Add Item control
        self.new_item_to_add = Parameter(
            name=IterativeNodeParam.NEW_ITEM_TO_ADD.value,
            tooltip="Item to add to results list",
            type=ParameterTypeBuiltin.ANY.value,
            allowed_modes={ParameterMode.INPUT},
        )

        # Loop completion output
        self.exec_out = ControlParameterOutput(
            tooltip="Triggered when loop completes", name=IterativeNodeParam.EXEC_OUT.value
        )
        self.exec_out.ui_options = {"display_name": "On Loop Complete"}

        # Results output - positioned below On Loop Complete
        self.results = Parameter(
            name=IterativeNodeParam.RESULTS.value,
            tooltip="Collected loop results",
            output_type="list",
            allowed_modes={ParameterMode.OUTPUT},
        )

        # Advanced control options for skip and break
        self.skip_control = ControlParameterInput(
            tooltip="Skip current item and continue to next iteration", name=IterativeNodeParam.SKIP_ITERATION.value
        )
        self.skip_control.ui_options = {"display_name": "Skip to Next Iteration"}

        self.break_control = ControlParameterInput(
            tooltip="Break out of loop immediately", name=IterativeNodeParam.BREAK_LOOP.value
        )
        self.break_control.ui_options = {"display_name": "Break Out of Loop"}

        # Hidden inputs from Start
        self.loop_end_condition_met_signal_input = ControlParameterInput(
            tooltip="Signal from Start when loop should end",
            name=IterativeNodeParam.LOOP_END_CONDITION_MET_SIGNAL_INPUT.value,
        )
        self.loop_end_condition_met_signal_input.ui_options = {"hide": True, "display_name": "Loop End Signal Input"}
        self.loop_end_condition_met_signal_input.settable = False

        # Hidden outputs to Start
        self.trigger_next_iteration_signal_output = ControlParameterOutput(
            tooltip="Signal to Start to continue to next iteration",
            name=IterativeNodeParam.TRIGGER_NEXT_ITERATION_SIGNAL_OUTPUT.value,
        )
        self.trigger_next_iteration_signal_output.ui_options = {
            "hide": True,
            "display_name": "Next Iteration Signal Output",
        }
        self.trigger_next_iteration_signal_output.settable = False

        self.break_loop_signal_output = ControlParameterOutput(
            tooltip="Signal to Start to break out of loop", name=IterativeNodeParam.BREAK_LOOP_SIGNAL_OUTPUT.value
        )
        self.break_loop_signal_output.ui_options = {"hide": True, "display_name": "Break Loop Signal Output"}
        self.break_loop_signal_output.settable = False

        # Output to iteratively update results
        self.results_output = None

        # Add main workflow parameters first
        self.add_parameter(self.add_item_control)
        self.add_parameter(self.new_item_to_add)
        self.add_parameter(self.exec_out)
        self.add_parameter(self.results)

        # Add advanced control options before tethering connection
        self.add_parameter(self.skip_control)
        self.add_parameter(self.break_control)

        # Add hidden parameters
        self.add_parameter(self.from_start)
        self.add_parameter(self.loop_end_condition_met_signal_input)
        self.add_parameter(self.trigger_next_iteration_signal_output)
        self.add_parameter(self.break_loop_signal_output)

    def _get_base_node_type_name(self) -> str:
        """Get the base node type name (e.g., 'ForLoop' from 'ForLoopEndNode')."""
        return self.__class__.__name__.replace("EndNode", "")

    @classmethod
    @abstractmethod
    def _get_compatible_start_classes(cls) -> set[type]:
        """Return the set of Start node classes that this End node can connect to."""

    def _output_results_list(self) -> None:
        """Output the current results list to the results parameter.

        Uses deep copy to ensure nested objects (like dictionaries) are properly copied
        and won't have unintended side effects if modified later.
        """
        self.parameter_output_values[IterativeNodeParam.RESULTS.value] = copy.deepcopy(self._results_list)

    def _validate_end_node(self) -> list[Exception] | None:
        """Common validation logic for both workflow and node run validation."""
        exceptions = []
        if self.start_node is None:
            exceptions.append(Exception("Start node is not set on End Node."))

        # Validate all required connections exist
        validation_errors = self._validate_iterative_connections()
        if validation_errors:
            exceptions.extend(validation_errors)

        if exceptions:
            return exceptions
        return super().validate_before_node_run()

    def validate_before_node_run(self) -> list[Exception] | None:
        return self._validate_end_node()

    def validate_before_workflow_run(self) -> list[Exception] | None:
        return self._validate_end_node()

    def process(self) -> None:
        """Process the end node based on the control path taken."""
        match self._entry_control_parameter:
            case self.add_item_control:
                # Only evaluate new_item_to_add parameter when adding to output
                new_item_value = self.get_parameter_value(IterativeNodeParam.NEW_ITEM_TO_ADD.value)
                self._results_list.append(new_item_value)
                if self.results_output is not None:
                    node, param = self.results_output
                    # Set the parameter value on the node. This should trigger after_value_set.
                    node.set_parameter_value(param.name, self._results_list)
            case self.skip_control:
                # Skip - don't add anything to output, just continue loop
                pass
            case self.break_control:
                # Break - emit current results and trigger break signal in get_next_control_output
                self._output_results_list()
            case self.loop_end_condition_met_signal_input:
                # Loop has ended naturally, output final results as standard parameter
                self._output_results_list()
                return

    def get_next_control_output(self) -> Parameter | None:
        """Return the appropriate signal output based on the control path taken."""
        match self._entry_control_parameter:
            case self.add_item_control | self.skip_control:
                # Both add and skip trigger next iteration
                return self.trigger_next_iteration_signal_output
            case self.break_control:
                # Break triggers break loop signal
                return self.break_loop_signal_output
            case self.loop_end_condition_met_signal_input:
                # Loop end condition triggers normal completion
                return self.exec_out
            case _:
                # Default fallback - should not happen
                return self.exec_out

    def allow_outgoing_connection(
        self,
        source_parameter: Parameter,
        target_node: BaseNode,
        target_parameter: Parameter,
    ) -> bool:
        """Validate outgoing connections for type safety."""
        # Check signal connections to Start nodes
        if source_parameter in (self.trigger_next_iteration_signal_output, self.break_loop_signal_output):
            # Ensure target node is compatible
            compatible_start_classes = self._get_compatible_start_classes()
            compatible_class_names = {cls.__name__ for cls in compatible_start_classes}
            target_class_name = target_node.__class__.__name__
            if target_class_name not in compatible_class_names:
                logger = logging.getLogger(__name__ + "." + self.name)
                logger.warning(
                    "Incompatible connection: %s can only connect to %s, but attempted to connect to %s",
                    self.__class__.__name__,
                    list(compatible_class_names),
                    target_class_name,
                )
                return False
        return super().allow_outgoing_connection(source_parameter, target_node, target_parameter)

    def allow_incoming_connection(
        self,
        source_node: BaseNode,
        source_parameter: Parameter,
        target_parameter: Parameter,
    ) -> bool:
        """Validate incoming connections for type safety."""
        # Check if this is a loop tethering connection
        if target_parameter == self.from_start:
            # Ensure source node is compatible
            compatible_start_classes = self._get_compatible_start_classes()
            compatible_class_names = {cls.__name__ for cls in compatible_start_classes}
            source_class_name = source_node.__class__.__name__
            if source_class_name not in compatible_class_names:
                logger = logging.getLogger(__name__ + "." + self.name)
                logger.warning(
                    "Incompatible connection: %s can only receive connections from %s, but %s attempted to connect",
                    self.__class__.__name__,
                    list(compatible_class_names),
                    source_class_name,
                )
                return False
        return super().allow_incoming_connection(source_node, source_parameter, target_parameter)

    @classmethod
    def allow_outgoing_connection_by_class(
        cls,
        target_node_class: type[BaseNode],
        source_parameter_name: str,  # We always know OUR parameter name
        target_parameter_name: str | None,  # noqa: ARG003 - May not know OTHER node's parameter
    ) -> bool:
        """Class-level validation for outgoing connections from iterative end nodes."""
        # Check signal connections (by OUR parameter name)
        if source_parameter_name in (
            IterativeNodeParam.TRIGGER_NEXT_ITERATION_SIGNAL_OUTPUT.value,
            IterativeNodeParam.BREAK_LOOP_SIGNAL_OUTPUT.value,
        ):
            compatible_start_classes = cls._get_compatible_start_classes()
            compatible_class_names = {class_type.__name__ for class_type in compatible_start_classes}
            target_class_name = target_node_class.__name__
            if target_class_name not in compatible_class_names:
                return False
        return True

    @classmethod
    def allow_incoming_connection_by_class(
        cls,
        source_node_class: type[BaseNode],
        source_parameter_name: str | None,  # noqa: ARG003 - May not know OTHER node's parameter
        target_parameter_name: str,  # We always know OUR parameter name
    ) -> bool:
        """Class-level validation for incoming connections to iterative end nodes."""
        # Check loop tethering connection (by OUR parameter name)
        if target_parameter_name == IterativeNodeParam.FROM_START.value:
            compatible_start_classes = cls._get_compatible_start_classes()
            compatible_class_names = {class_type.__name__ for class_type in compatible_start_classes}
            source_class_name = source_node_class.__name__
            if source_class_name not in compatible_class_names:
                return False
        return True

    def _validate_iterative_connections(self) -> list[Exception]:
        """Validate that all required iterative connections are properly established."""
        errors = []
        node_type = self._get_base_node_type_name()

        # Check if from_start has incoming connection from Start
        if self.start_node is None:
            errors.append(
                Exception(
                    f"{self.name}: Missing required tethering connection. "
                    f"REQUIRED ACTION: Connect {node_type} Start 'Loop End Node' to {node_type} End 'Loop Start Node'. "
                    "This establishes the explicit relationship between start and end nodes."
                )
            )

        # Check if all hidden signal connections exist (only if start_node is connected)
        if self.start_node and not _incoming_connection_exists(self.name, "loop_end_condition_met_signal_input"):
            errors.append(
                Exception(
                    f"{self.name}: Missing hidden signal connection. "
                    f"REQUIRED ACTION: Connect {node_type} Start 'Loop End Signal' to {node_type} End 'Loop End Signal Input'. "
                    "This receives the signal when the loop has completed naturally."
                )
            )

        # Check if control inputs have at least one connection
        control_names = [
            IterativeNodeParam.ADD_ITEM.value,
            IterativeNodeParam.SKIP_ITERATION.value,
            IterativeNodeParam.BREAK_LOOP.value,
        ]
        connected_controls = []

        for control_name in control_names:
            if _incoming_connection_exists(self.name, control_name):
                connected_controls.append(control_name)  # noqa: PERF401

        if not connected_controls:
            errors.append(
                Exception(
                    f"{self.name}: No control flow connections found. "
                    f"REQUIRED ACTION: Connect at least one control flow to {node_type} End. "
                    "Options: 'Add Item to Output', 'Skip to Next Iteration', or 'Break Out of Loop'. "
                    "The End node needs to receive control flow from your loop body logic."
                )
            )

        return errors

    def initialize_spotlight(self) -> None:
        """Custom spotlight initialization for conditional dependency resolution."""
        match self._entry_control_parameter:
            case self.add_item_control:
                # Only resolve new_item_to_add dependency if we're actually going to use it
                new_item_param = self.get_parameter_by_name(IterativeNodeParam.NEW_ITEM_TO_ADD.value)
                if new_item_param and ParameterMode.INPUT in new_item_param.get_mode():
                    self.current_spotlight_parameter = new_item_param
            case _:
                # For skip or break paths, don't resolve any input dependencies
                self.current_spotlight_parameter = None

    def advance_parameter(self) -> bool:
        """Custom parameter advancement with conditional dependency resolution."""
        if self.current_spotlight_parameter is None:
            return False

        # Use default advancement behavior for the new_item_to_add parameter
        if self.current_spotlight_parameter.next is not None:
            self.current_spotlight_parameter = self.current_spotlight_parameter.next
            return True

        self.current_spotlight_parameter = None
        return False

    def reset_for_workflow_run(self) -> None:
        """Reset End state for a fresh workflow run."""
        self._results_list = []
        self._output_results_list()

    def after_incoming_connection(
        self,
        source_node: BaseNode,
        source_parameter: Parameter,
        target_parameter: Parameter,
    ) -> None:
        # Track incoming connections for validation
        self._connected_parameters.add(target_parameter.name)

        if target_parameter is self.from_start and isinstance(source_node, BaseIterativeStartNode):
            self.start_node = source_node
            # Auto-create all hidden signal connections when main tethering connection is made
            self._create_hidden_signal_connections(source_node)
        return super().after_incoming_connection(source_node, source_parameter, target_parameter)

    def after_incoming_connection_removed(
        self,
        source_node: BaseNode,
        source_parameter: Parameter,
        target_parameter: Parameter,
    ) -> None:
        # Remove from tracking when connection is removed
        self._connected_parameters.discard(target_parameter.name)

        if target_parameter is self.from_start and isinstance(source_node, BaseIterativeStartNode):
            self.start_node = None
            # Clean up hidden signal connections when main tethering connection is removed
            self._remove_hidden_signal_connections(source_node)
        return super().after_incoming_connection_removed(source_node, source_parameter, target_parameter)

    def _create_hidden_signal_connections(self, start_node: BaseNode) -> None:
        """Automatically create all hidden signal connections between Start and End nodes."""
        from griptape_nodes.retained_mode.events.connection_events import CreateConnectionRequest
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        # Create the hidden signal connections and default control flow:

        # 1. Start → End: loop_end_condition_met_signal → loop_end_condition_met_signal_input
        GriptapeNodes.handle_request(
            CreateConnectionRequest(
                source_node_name=start_node.name,
                source_parameter_name=IterativeNodeParam.LOOP_END_CONDITION_MET_SIGNAL.value,
                target_node_name=self.name,
                target_parameter_name=IterativeNodeParam.LOOP_END_CONDITION_MET_SIGNAL_INPUT.value,
            )
        )

        # 2. End → Start: trigger_next_iteration_signal_output → trigger_next_iteration_signal
        GriptapeNodes.handle_request(
            CreateConnectionRequest(
                source_node_name=self.name,
                source_parameter_name=IterativeNodeParam.TRIGGER_NEXT_ITERATION_SIGNAL_OUTPUT.value,
                target_node_name=start_node.name,
                target_parameter_name=IterativeNodeParam.TRIGGER_NEXT_ITERATION_SIGNAL.value,
            )
        )

        # 3. End → Start: break_loop_signal_output → break_loop_signal
        GriptapeNodes.handle_request(
            CreateConnectionRequest(
                source_node_name=self.name,
                source_parameter_name=IterativeNodeParam.BREAK_LOOP_SIGNAL_OUTPUT.value,
                target_node_name=start_node.name,
                target_parameter_name=IterativeNodeParam.BREAK_LOOP_SIGNAL.value,
            )
        )

        # 4. Default control flow: Start → End: exec_out → add_item (default "happy path")
        # Only create this connection if the exec_out parameter doesn't already have a connection
        if not _outgoing_connection_exists(start_node.name, IterativeNodeParam.EXEC_OUT.value):
            GriptapeNodes.handle_request(
                CreateConnectionRequest(
                    source_node_name=start_node.name,
                    source_parameter_name=IterativeNodeParam.EXEC_OUT.value,
                    target_node_name=self.name,
                    target_parameter_name=IterativeNodeParam.ADD_ITEM.value,
                )
            )

    def _remove_hidden_signal_connections(self, start_node: BaseNode) -> None:
        """Remove all hidden signal connections when the main tethering connection is removed."""
        from griptape_nodes.retained_mode.events.connection_events import (
            DeleteConnectionRequest,
            ListConnectionsForNodeRequest,
            ListConnectionsForNodeResultSuccess,
        )
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        # Get current connections for start node to check what still exists
        list_connections_result = GriptapeNodes.handle_request(ListConnectionsForNodeRequest(node_name=start_node.name))
        if not isinstance(list_connections_result, ListConnectionsForNodeResultSuccess):
            return  # Cannot determine what connections exist, exit gracefully

        # Helper function to check if a connection exists
        def connection_exists(
            source_node_name: str, source_param: str, target_node_name: str, target_param: str
        ) -> bool:
            # Check in outgoing connections from source node
            for conn in list_connections_result.outgoing_connections:
                if (
                    conn.source_parameter_name == source_param
                    and conn.target_node_name == target_node_name
                    and conn.target_parameter_name == target_param
                ):
                    return True
            # Check in incoming connections to source node
            for conn in list_connections_result.incoming_connections:
                if (
                    conn.source_node_name == source_node_name
                    and conn.source_parameter_name == source_param
                    and conn.target_parameter_name == target_param
                ):
                    return True
            return False

        # Remove the hidden signal connections:

        # 1. Start → End: loop_end_condition_met_signal → loop_end_condition_met_signal_input
        if connection_exists(
            start_node.name,
            IterativeNodeParam.LOOP_END_CONDITION_MET_SIGNAL.value,
            self.name,
            IterativeNodeParam.LOOP_END_CONDITION_MET_SIGNAL_INPUT.value,
        ):
            GriptapeNodes.handle_request(
                DeleteConnectionRequest(
                    source_node_name=start_node.name,
                    source_parameter_name=IterativeNodeParam.LOOP_END_CONDITION_MET_SIGNAL.value,
                    target_node_name=self.name,
                    target_parameter_name=IterativeNodeParam.LOOP_END_CONDITION_MET_SIGNAL_INPUT.value,
                )
            )

        # 2. End → Start: trigger_next_iteration_signal_output → trigger_next_iteration_signal
        if connection_exists(
            self.name,
            IterativeNodeParam.TRIGGER_NEXT_ITERATION_SIGNAL_OUTPUT.value,
            start_node.name,
            IterativeNodeParam.TRIGGER_NEXT_ITERATION_SIGNAL.value,
        ):
            GriptapeNodes.handle_request(
                DeleteConnectionRequest(
                    source_node_name=self.name,
                    source_parameter_name=IterativeNodeParam.TRIGGER_NEXT_ITERATION_SIGNAL_OUTPUT.value,
                    target_node_name=start_node.name,
                    target_parameter_name=IterativeNodeParam.TRIGGER_NEXT_ITERATION_SIGNAL.value,
                )
            )

        # 3. End → Start: break_loop_signal_output → break_loop_signal
        if connection_exists(
            self.name,
            IterativeNodeParam.BREAK_LOOP_SIGNAL_OUTPUT.value,
            start_node.name,
            IterativeNodeParam.BREAK_LOOP_SIGNAL.value,
        ):
            GriptapeNodes.handle_request(
                DeleteConnectionRequest(
                    source_node_name=self.name,
                    source_parameter_name=IterativeNodeParam.BREAK_LOOP_SIGNAL_OUTPUT.value,
                    target_node_name=start_node.name,
                    target_parameter_name=IterativeNodeParam.BREAK_LOOP_SIGNAL.value,
                )
            )

    def after_outgoing_connection(
        self, source_parameter: Parameter, target_node: BaseNode, target_parameter: Parameter
    ) -> None:
        if source_parameter == self.results:
            # Update value on each iteration
            self.results_output = NodeParameterPair(node=target_node, parameter=target_parameter)
        return super().after_outgoing_connection(source_parameter, target_node, target_parameter)

    def after_outgoing_connection_removed(
        self, source_parameter: Parameter, target_node: BaseNode, target_parameter: Parameter
    ) -> None:
        if source_parameter == self.results:
            # Update value on each iteration
            self.results_output = None
        return super().after_outgoing_connection_removed(source_parameter, target_node, target_parameter)
