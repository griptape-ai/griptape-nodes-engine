from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, NamedTuple

from griptape_nodes.exe_types.base_iterative_nodes import BaseIterativeEndNode, BaseIterativeStartNode
from griptape_nodes.exe_types.connections import Direction
from griptape_nodes.exe_types.core_types import Parameter, ParameterTypeBuiltin
from griptape_nodes.exe_types.node_types import (
    BaseNode,
    NodeResolutionState,
)
from griptape_nodes.machines.dag_builder import NodeState
from griptape_nodes.machines.fsm import FSM, State, WorkflowState
from griptape_nodes.machines.node_priority_queue import NodePriorityQueue
from griptape_nodes.node_library.library_registry import LibraryRegistry
from griptape_nodes.retained_mode.events.base_events import (
    ExecutionEvent,
    ExecutionGriptapeNodeEvent,
)
from griptape_nodes.retained_mode.events.event_converter import safe_unstructure
from griptape_nodes.retained_mode.events.execution_events import (
    CurrentControlNodeEvent,
    CurrentDataNodeEvent,
    InvolvedNodesEvent,
    NodeErrorEvent,
    NodeResolvedEvent,
    ParameterValueUpdateEvent,
)
from griptape_nodes.retained_mode.events.parameter_events import (
    SetParameterValueRequest,
    SetParameterValueResultFailure,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

if TYPE_CHECKING:
    from griptape_nodes.common.directed_graph import DirectedGraph
    from griptape_nodes.machines.dag_builder import DagBuilder, DagNode
    from griptape_nodes.retained_mode.managers.flow_manager import FlowManager

logger = logging.getLogger("griptape_nodes")


class NodeStatesResult(NamedTuple):
    """Result of building node states from the DAG networks.

    Attributes:
        canceled_nodes: Set of node names that are in a canceled state
        leaf_nodes: Set of node names that are leaf nodes (no dependencies)
    """

    canceled_nodes: set[str]
    leaf_nodes: set[str]


class ParallelResolutionContext:
    paused: bool
    flow_name: str
    error_message: str | None
    workflow_state: WorkflowState
    # Execution fields
    max_nodes_in_parallel: int
    running_tasks_count: int
    task_to_node: dict[asyncio.Task, DagNode]
    node_priority_queue: NodePriorityQueue
    dag_builder: DagBuilder | None
    last_resolved_node: BaseNode | None  # Track the last node that was resolved

    def __init__(
        self, flow_name: str, max_nodes_in_parallel: int | None = None, dag_builder: DagBuilder | None = None
    ) -> None:
        self.flow_name = flow_name
        self.paused = False
        self.error_message = None
        self.workflow_state = WorkflowState.NO_ERROR
        self.dag_builder = dag_builder
        self.last_resolved_node = None
        self.node_priority_queue = NodePriorityQueue(self)

        # Initialize execution fields
        self.max_nodes_in_parallel = max_nodes_in_parallel if max_nodes_in_parallel is not None else 5
        self.running_tasks_count = 0
        self.task_to_node = {}

    @property
    def node_to_reference(self) -> dict[str, DagNode]:
        """Get node_to_reference from dag_builder if available."""
        if not self.dag_builder:
            msg = "DagBuilder is not initialized"
            raise ValueError(msg)
        return self.dag_builder.node_to_reference

    @property
    def networks(self) -> dict[str, DirectedGraph]:
        """Get node_to_reference from dag_builder if available."""
        if not self.dag_builder:
            msg = "DagBuilder is not initialized"
            raise ValueError(msg)
        return self.dag_builder.graphs

    def reset(self, *, cancel: bool = False) -> None:
        self.paused = False
        if cancel:
            self.workflow_state = WorkflowState.CANCELED
            # Only access node_to_reference if dag_builder exists
            if self.dag_builder:
                for node in self.node_to_reference.values():
                    node.node_state = NodeState.CANCELED
        else:
            self.workflow_state = WorkflowState.NO_ERROR
            self.error_message = None
            self.task_to_node.clear()
            self.last_resolved_node = None

        # Reset task counter
        self.running_tasks_count = 0

        # Clear the priority queue when resetting
        # Create a new instance to ensure clean state
        self.node_priority_queue = NodePriorityQueue(self)

        # Clear DAG builder state to allow re-adding nodes on subsequent runs
        if self.dag_builder:
            self.dag_builder.clear()


class ExecuteDagState(State):
    @staticmethod
    def check_for_new_start_nodes(
        context: ParallelResolutionContext, current_node_name: str, network_name: str
    ) -> None:
        # Remove this node from dependencies and get newly available nodes
        if context.dag_builder is not None:
            newly_available = context.dag_builder.remove_node_from_dependencies(current_node_name, network_name)
            for data_node_name in newly_available:
                data_node = GriptapeNodes.NodeManager().get_node_by_name(data_node_name)
                added_nodes = context.dag_builder.add_node_with_dependencies(data_node, data_node_name)
                if added_nodes:
                    for added_node in added_nodes:
                        ExecuteDagState._try_queue_waiting_node(context, added_node.name)

    @staticmethod
    async def handle_done_nodes(context: ParallelResolutionContext, done_node: DagNode, network_name: str) -> None:
        current_node = done_node.node_reference

        # Remove the node from the priority queue now that it's done
        context.node_priority_queue.remove_node(current_node.name)

        # Check if node was already resolved (shouldn't happen)
        if current_node.state == NodeResolutionState.RESOLVED and not current_node.lock:
            logger.error(
                "DUPLICATE COMPLETION DETECTED: Node '%s' was already RESOLVED but handle_done_nodes was called again from network '%s'. This should not happen!",
                current_node.name,
                network_name,
            )
            return

        # Special handling for BaseIterativeStartNode
        # Remove it from the network so the end node can process control flow
        if isinstance(current_node, BaseIterativeStartNode):
            current_node.state = NodeResolutionState.RESOLVED

            # Remove start node from ALL networks where it appears
            for network in list(context.networks.values()):
                if current_node.name in network.nodes():
                    network.remove_node(current_node.name)

            return

        # Publish all parameter updates.
        current_node.state = NodeResolutionState.RESOLVED
        # Track this as the last resolved node
        context.last_resolved_node = current_node
        # Mark the priority queue as needing recalculation
        context.node_priority_queue.mark_priorities_stale()
        # Serialization can be slow so only do it if the user wants debug details.
        if logger.level <= logging.DEBUG:
            logger.debug(
                "INPUTS: %s\nOUTPUTS: %s",
                safe_unstructure(current_node.parameter_values),
                safe_unstructure(current_node.parameter_output_values),
            )

        for parameter_name, value in current_node.parameter_output_values.items():
            parameter = current_node.get_parameter_by_name(parameter_name)
            if parameter is None:
                err = f"Canceling flow run. Node '{current_node.name}' specified a Parameter '{parameter_name}', but no such Parameter could be found on that Node."
                raise KeyError(err)
            data_type = parameter.type
            if data_type is None:
                data_type = ParameterTypeBuiltin.NONE.value

            # Use the template (e.g. "{SHOT}") instead of the substituted value
            # (e.g. "25") for PROPERTY parameters that contain a variable macro.
            # This ParameterValueUpdateEvent is the authoritative "node done"
            # broadcast and fires outside aprocess_scope, so without this
            # suppression it would overwrite the display that
            # _emit_parameter_change_event already set correctly during execution.
            display_value = current_node.get_display_value_for_output(parameter_name, value)
            await GriptapeNodes.EventManager().aput_event(
                ExecutionGriptapeNodeEvent(
                    wrapped_event=ExecutionEvent(
                        payload=ParameterValueUpdateEvent(
                            node_name=current_node.name,
                            parameter_name=parameter_name,
                            data_type=data_type,
                            value=safe_unstructure(display_value),
                        )
                    ),
                )
            )
        # Output values should already be saved!
        library = LibraryRegistry.get_libraries_with_node_type(current_node.__class__.__name__)
        if len(library) == 1:
            library_name = library[0]
        else:
            library_name = None

        # Apply the same display suppression here: NodeResolvedEvent carries the
        # full parameter_output_values dict and the frontend uses it to update
        # displayed values, so raw substituted values (e.g. "25") would
        # overwrite the template (e.g. "{SHOT}") the user sees on the node.
        display_output_values = {
            param_name: safe_unstructure(current_node.get_display_value_for_output(param_name, val))
            for param_name, val in current_node.parameter_output_values.items()
        }
        await GriptapeNodes.EventManager().aput_event(
            ExecutionGriptapeNodeEvent(
                wrapped_event=ExecutionEvent(
                    payload=NodeResolvedEvent(
                        node_name=current_node.name,
                        parameter_output_values=display_output_values,
                        node_type=current_node.__class__.__name__,
                        specific_library_name=library_name,
                    )
                )
            )
        )
        # Now the final thing to do, is to take their directed graph and update it.
        ExecuteDagState.get_next_control_graph(context, current_node, network_name)
        ExecuteDagState.check_for_new_start_nodes(context, current_node.name, network_name)

    @staticmethod
    def get_next_control_graph(context: ParallelResolutionContext, node: BaseNode, network_name: str) -> None:
        """Get next control flow nodes and add them to the DAG graph."""
        flow_manager = GriptapeNodes.FlowManager()

        # Early returns for various conditions
        if ExecuteDagState._should_skip_control_flow(context, node, network_name, flow_manager):
            return
        next_output = node.get_next_control_output()
        if next_output is not None:
            ExecuteDagState._process_next_control_node(context, node, next_output, network_name, flow_manager)

    @staticmethod
    def _should_skip_control_flow(
        context: ParallelResolutionContext, node: BaseNode, network_name: str, flow_manager: FlowManager
    ) -> bool:
        """Check if control flow processing should be skipped."""
        # Get network once to avoid duplicate lookups
        if context.dag_builder is None:
            msg = "DAG builder is not initialized"
            raise ValueError(msg)
        network = context.dag_builder.graphs.get(network_name, None)
        if network is None:
            msg = f"Network {network_name} not found in DAG builder"
            raise ValueError(msg)
        is_isolated = context.dag_builder is not flow_manager.global_dag_builder
        if flow_manager.global_single_node_resolution and not is_isolated:
            # Clean up nodes from emptied graphs in single node resolution mode
            if len(network) == 0 and context.dag_builder is not None:
                context.dag_builder.cleanup_empty_graph_nodes(network_name)
                ExecuteDagState._emit_involved_nodes_update(context)
            return True

        return bool(len(network) > 0 or node.stop_flow)

    @staticmethod
    def _process_next_control_node(
        context: ParallelResolutionContext,
        node: BaseNode,
        next_output: Parameter,
        network_name: str,
        flow_manager: FlowManager,
    ) -> None:
        """Process the next control node in the flow."""
        node_connection = flow_manager.get_connections().get_connected_node(node, next_output, include_internal=False)
        if node_connection is not None:
            next_node, next_parameter = node_connection

            # Add this control successor to the last_resolved_successors set
            context.node_priority_queue._last_resolved_successors.add(next_node.name)

            # Set entry control parameter
            logger.debug(
                "Parallel Resolution: Setting entry control parameter for node '%s' to '%s'",
                next_node.name,
                next_parameter.name if next_parameter else None,
            )
            next_node.set_entry_control_parameter(next_parameter)
            # Prepare next node for execution
            if not next_node.lock:
                next_node.make_node_unresolved(
                    current_states_to_trigger_change_event=set(
                        {
                            NodeResolutionState.UNRESOLVED,
                            NodeResolutionState.RESOLVED,
                            NodeResolutionState.RESOLVING,
                        }
                    )
                )
                GriptapeNodes.EventManager().put_event(
                    ExecutionGriptapeNodeEvent(
                        wrapped_event=ExecutionEvent(payload=CurrentControlNodeEvent(node_name=next_node.name))
                    )
                )
            ExecuteDagState._add_and_queue_nodes(context, next_node, network_name)

    @staticmethod
    def _emit_involved_nodes_update(context: ParallelResolutionContext) -> None:
        """Emit update of involved nodes based on current DAG state."""
        if context.dag_builder is not None:
            involved_nodes = list(context.node_to_reference.keys())
            GriptapeNodes.EventManager().put_event(
                ExecutionGriptapeNodeEvent(
                    wrapped_event=ExecutionEvent(payload=InvolvedNodesEvent(involved_nodes=involved_nodes))
                )
            )

    @staticmethod
    def _add_and_queue_nodes(context: ParallelResolutionContext, next_node: BaseNode, network_name: str) -> None:
        """Add nodes to DAG and queue them if ready."""
        if context.dag_builder is not None:
            added_nodes = context.dag_builder.add_node_with_dependencies(next_node, network_name)
            if next_node not in added_nodes:
                added_nodes.append(next_node)

            # Queue nodes that are ready for execution
            if added_nodes:
                for added_node in added_nodes:
                    ExecuteDagState._try_queue_waiting_node(context, added_node.name)

    @staticmethod
    def _try_queue_waiting_node(context: ParallelResolutionContext, node_name: str) -> None:
        """Try to queue a specific waiting node if it can now be queued."""
        if context.dag_builder is None:
            logger.warning("DAG builder is None - cannot check queueing for node '%s'", node_name)
            return

        if node_name not in context.node_to_reference:
            logger.warning("Node '%s' not found in node_to_reference - cannot check queueing", node_name)
            return

        dag_node = context.node_to_reference[node_name]

        # Only check nodes that are currently waiting
        if dag_node.node_state == NodeState.WAITING:
            can_queue = context.dag_builder.can_queue_control_node(dag_node)
            if can_queue:
                dag_node.node_state = NodeState.QUEUED
                context.node_priority_queue.add_node(dag_node)

    @staticmethod
    async def collect_values_from_upstream_nodes(node_reference: DagNode) -> None:
        """Collect output values from resolved upstream nodes and pass them to the current node.

        This method iterates through all input parameters of the current node, finds their
        connected upstream nodes, and if those nodes are resolved, retrieves their output
        values and passes them through using SetParameterValueRequest.

        Args:
            node_reference (DagOrchestrator.DagNode): The node to collect values for.
        """
        current_node = node_reference.node_reference

        # A locked node is frozen: it is skipped for execution and keeps its existing output
        # values so downstream nodes consume those frozen outputs. Pushing an upstream value into
        # it would be rejected by the SetParameterValueRequest handler and escalated into a fatal
        # error, so halt propagation into the locked node quietly instead. Mirrors the
        # editor/manual set-parameter path, which already skips locked destination nodes.
        if current_node.lock:
            return

        connections = GriptapeNodes.FlowManager().get_connections()

        for parameter in current_node.parameters:
            # Get the connected upstream node for this parameter
            upstream_connection = connections.get_connected_node(current_node, parameter, direction=Direction.UPSTREAM)
            if upstream_connection:
                upstream_node, upstream_parameter = upstream_connection

                # If the upstream node is resolved, collect its output value
                if upstream_parameter.name in upstream_node.parameter_output_values:
                    output_value = upstream_node.parameter_output_values[upstream_parameter.name]
                else:
                    output_value = upstream_node.get_parameter_value(upstream_parameter.name)

                # Pass the value through using the same mechanism as normal resolution
                result = await GriptapeNodes.get_instance().ahandle_request(
                    SetParameterValueRequest(
                        parameter_name=parameter.name,
                        node_name=current_node.name,
                        value=output_value,
                        data_type=upstream_parameter.output_type,
                        incoming_connection_source_node_name=upstream_node.name,
                        incoming_connection_source_parameter_name=upstream_parameter.name,
                    )
                )
                if isinstance(result, SetParameterValueResultFailure):
                    msg = f"Failed to set parameter value for node '{current_node.name}' and parameter '{parameter.name}'. Details: {result.result_details}"
                    logger.error(msg)
                    raise RuntimeError(msg)

    @staticmethod
    def build_node_states(context: ParallelResolutionContext) -> NodeStatesResult:
        networks = context.networks
        leaf_nodes = set()
        for network in networks.values():
            # Check and see if there are leaf nodes that are cancelled.
            # Reinitialize leaf nodes since maybe we changed things up.
            # We removed nodes from the network. There may be new leaf nodes.
            # Add all leaf nodes from all networks (using set union to avoid duplicates)
            network_leaf_nodes = [n for n in network.nodes() if network.in_degree(n) == 0]
            leaf_nodes.update(network_leaf_nodes)
        canceled_nodes = set()
        for node in leaf_nodes:
            node_reference = context.node_to_reference[node]
            # If the node is locked, mark it as done so it skips execution
            if node_reference.node_reference.lock:
                node_reference.node_state = NodeState.DONE
                continue
            node_state = node_reference.node_state
            if node_state == NodeState.CANCELED:
                canceled_nodes.add(node)
        return NodeStatesResult(canceled_nodes=canceled_nodes, leaf_nodes=leaf_nodes)

    @staticmethod
    async def pop_done_states(context: ParallelResolutionContext) -> None:
        networks = context.networks
        handled_nodes = set()  # Track nodes we've already processed to avoid duplicates

        # Create a copy of items to avoid "dictionary changed size during iteration" error
        # This is necessary because handle_done_nodes can add new networks via the DAG builder
        for network_name, network in list(networks.items()):
            # Check and see if there are leaf nodes that are cancelled.
            # Reinitialize leaf nodes since maybe we changed things up.
            # We removed nodes from the network. There may be new leaf nodes.
            leaf_nodes = [n for n in network.nodes() if network.in_degree(n) == 0]
            for node in leaf_nodes:
                node_reference = context.node_to_reference[node]
                node_state = node_reference.node_state
                # If the node is locked, mark it as done so it skips execution
                if node_reference.node_reference.lock or node_state == NodeState.DONE:
                    node_reference.node_state = NodeState.DONE

                    # Initialize successors set with data successors from this network
                    successors = set()
                    for other_node in network.nodes():
                        if node in network._predecessors.get(other_node, set()):
                            successors.add(other_node)

                    # Set initial data successors (control successors will be added in handle_done_nodes)
                    context.node_priority_queue._last_resolved_successors = successors

                    network.remove_node(node)

                    # Only call handle_done_nodes once per node (first network that processes it)
                    if node not in handled_nodes:
                        handled_nodes.add(node)
                        # handle_done_nodes will append control successors to the set
                        await ExecuteDagState.handle_done_nodes(context, context.node_to_reference[node], network_name)

            # After processing completions in this network, check if any remaining leaf nodes can now be queued
            remaining_leaf_nodes = [n for n in network.nodes() if network.in_degree(n) == 0]

            for leaf_node in remaining_leaf_nodes:
                if leaf_node in context.node_to_reference:
                    node_state = context.node_to_reference[leaf_node].node_state
                ExecuteDagState._try_queue_waiting_node(context, leaf_node)

    @staticmethod
    async def execute_node(current_node: DagNode) -> None:
        executor = GriptapeNodes.FlowManager().node_executor
        await executor.execute(current_node.node_reference)

    @staticmethod
    async def on_enter(context: ParallelResolutionContext) -> type[State] | None:
        # Start DAG execution after resolution is complete
        for node in context.node_to_reference.values():
            # Only queue nodes that are waiting - preserve state of already processed nodes.
            if node.node_state == NodeState.WAITING:
                # Use proper queueing method that checks can_queue_control_node()
                # This prevents premature queueing of nodes with multiple control connections
                ExecuteDagState._try_queue_waiting_node(context, node.node_reference.name)

        context.workflow_state = WorkflowState.NO_ERROR

        if not context.paused:
            return ExecuteDagState
        return None

    @staticmethod
    async def on_update(context: ParallelResolutionContext) -> type[State] | None:  # noqa: C901, PLR0911, PLR0912, PLR0915
        # Check if execution is paused
        if context.paused:
            return None

        # Check if DAG execution is complete
        # Check and see if there are leaf nodes that are cancelled.
        # Reinitialize leaf nodes since maybe we changed things up.
        # We removed nodes from the network. There may be new leaf nodes.
        node_states = ExecuteDagState.build_node_states(context)
        # We have no more leaf nodes. Quit early.
        if not node_states.leaf_nodes:
            context.workflow_state = WorkflowState.WORKFLOW_COMPLETE
            return DagCompleteState
        if len(node_states.canceled_nodes) == len(node_states.leaf_nodes):
            # All leaf nodes are cancelled.
            # Set state to workflow complete.
            context.workflow_state = WorkflowState.CANCELED
            return DagCompleteState
        # Create tasks only while we have capacity
        while context.running_tasks_count < context.max_nodes_in_parallel:
            # Get next highest priority node
            node = context.node_priority_queue.get_next_node()
            if node is None:
                break  # No more nodes to process

            # Increment counter BEFORE any await points
            context.running_tasks_count += 1

            node_reference = context.node_to_reference[node]

            # Skip BaseIterativeEndNode as it's handled by loop execution flow
            if isinstance(node_reference.node_reference, BaseIterativeEndNode):
                context.running_tasks_count -= 1  # Decrement since we're skipping
                continue

            # Collect parameter values from upstream nodes before executing
            try:
                await ExecuteDagState.collect_values_from_upstream_nodes(node_reference)
            except Exception as e:
                context.running_tasks_count -= 1  # Decrement on error
                logger.exception("Error collecting parameter values for node '%s'", node_reference.node_reference.name)
                error_node_name = node_reference.node_reference.name
                await GriptapeNodes.EventManager().aput_event(
                    ExecutionGriptapeNodeEvent(
                        wrapped_event=ExecutionEvent(
                            payload=NodeErrorEvent(
                                node_name=error_node_name,
                                error_message=str(e),
                            )
                        )
                    )
                )
                context.error_message = f"Parameter passthrough failed for node '{error_node_name}': {e}"
                context.workflow_state = WorkflowState.ERRORED
                return ErrorState

            # Clear all of the current output values but don't broadcast the clearing.
            # to avoid any flickering in subscribers (UI).
            node_reference.node_reference.parameter_output_values.silent_clear()
            exceptions = node_reference.node_reference.validate_before_node_run()
            if exceptions:
                context.running_tasks_count -= 1  # Decrement on error
                validation_node_name = node_reference.node_reference.name
                msg = f"Node '{validation_node_name}' encountered problems: {exceptions}"
                logger.error("Canceling flow run. %s", msg)
                await GriptapeNodes.EventManager().aput_event(
                    ExecutionGriptapeNodeEvent(
                        wrapped_event=ExecutionEvent(
                            payload=NodeErrorEvent(
                                node_name=validation_node_name,
                                error_message=str(exceptions),
                            )
                        )
                    )
                )
                context.error_message = msg
                context.workflow_state = WorkflowState.ERRORED
                return ErrorState

            # We've set up the node for success completely. Now we check and handle accordingly if it's a for-each-start node
            # if False:
            if isinstance(node_reference.node_reference, BaseIterativeStartNode):
                # Call handle_done_state to clear it from everything
                end_loop_node = node_reference.node_reference.end_node
                # Set start node to DONE! even if it isn't truly done lolllll.
                node_reference.node_state = NodeState.DONE
                if end_loop_node is None:
                    context.running_tasks_count -= 1  # Decrement on error
                    msg = (
                        f"Cannot have a Start Loop Node without an End Loop Node: {node_reference.node_reference.name}"
                    )
                    logger.error(msg)
                    context.error_message = msg
                    context.workflow_state = WorkflowState.ERRORED
                    return ErrorState
                # We're going to skip straight to the end node here instead.
                # Set end node to node reference
                if context.dag_builder is not None:
                    # Check if BaseIterativeEndNode is already in DAG (from pre-building phase)
                    if end_loop_node.name in context.dag_builder.node_to_reference:
                        # BaseIterativeEndNode already exists in DAG, just get reference and queue it
                        end_node_reference = context.dag_builder.node_to_reference[end_loop_node.name]
                        end_node_reference.node_state = NodeState.QUEUED
                        context.node_priority_queue.add_node(end_node_reference)
                        node_reference = end_node_reference
                    else:
                        # BaseIterativeEndNode not in DAG yet (backwards compatibility), add it
                        end_node_reference = context.dag_builder.add_node(end_loop_node)
                        end_node_reference.node_state = NodeState.QUEUED
                        context.node_priority_queue.add_node(end_node_reference)
                        node_reference = end_node_reference

            def on_task_done(task: asyncio.Task) -> None:
                if task in context.task_to_node:
                    node = context.task_to_node[task]
                    node.node_state = NodeState.DONE

            # Execute the node asynchronously
            logger.debug(
                "CREATING EXECUTION TASK for node '%s' - this should only happen once per node!",
                node_reference.node_reference.name,
            )
            # Set state BEFORE adding to task_to_node to avoid race condition
            node_reference.node_state = NodeState.PROCESSING
            node_reference.node_reference.state = NodeResolutionState.RESOLVING

            node_task = asyncio.create_task(ExecuteDagState.execute_node(node_reference))
            # Add a callback to set node to done when task has finished.
            context.task_to_node[node_task] = node_reference
            node_reference.task_reference = node_task
            node_task.add_done_callback(on_task_done)

            # Send an event that this is a current data node:

            await GriptapeNodes.EventManager().aput_event(
                ExecutionGriptapeNodeEvent(wrapped_event=ExecutionEvent(payload=CurrentDataNodeEvent(node_name=node)))
            )

        # Wait for a task to finish - only if there are tasks running
        if context.task_to_node:
            done, _ = await asyncio.wait(context.task_to_node.keys(), return_when=asyncio.FIRST_COMPLETED)

            # Decrement counter for completed tasks
            context.running_tasks_count -= len(done)
            # New node has finished - priorities are stale
            context.node_priority_queue.mark_priorities_stale()
            # Check for task exceptions and handle them properly
            for task in done:
                if task.cancelled():
                    # Task was cancelled - this is expected during flow cancellation
                    context.task_to_node.pop(task)
                    logger.info("Task execution was cancelled.")
                    return ErrorState
                if task.exception():
                    exc = task.exception()
                    dag_node = context.task_to_node.get(task)
                    node_name = dag_node.node_reference.name if dag_node else "Unknown"

                    logger.exception("Error processing node '%s'", node_name)
                    msg = f"Node '{node_name}' encountered a problem: {exc}"

                    await GriptapeNodes.EventManager().aput_event(
                        ExecutionGriptapeNodeEvent(
                            wrapped_event=ExecutionEvent(
                                payload=NodeErrorEvent(
                                    node_name=node_name,
                                    error_message=str(exc),
                                )
                            )
                        )
                    )
                    context.task_to_node.pop(task)
                    context.error_message = msg
                    context.workflow_state = WorkflowState.ERRORED
                    return ErrorState
                context.task_to_node.pop(task)

        # Once a task has finished, loop back to the top.
        await ExecuteDagState.pop_done_states(context)
        # Remove all nodes that are done
        if context.paused:
            return None
        return ExecuteDagState


class ErrorState(State):
    @staticmethod
    async def on_enter(context: ParallelResolutionContext) -> type[State] | None:
        for node in context.node_to_reference.values():
            # Cancel all nodes that haven't yet begun processing.
            if node.node_state == NodeState.QUEUED:
                node.node_state = NodeState.CANCELED
                # Remove from priority queue since it's being canceled
                context.node_priority_queue.remove_node(node.node_reference.name)

        # Shut down and cancel all threads/tasks that haven't yet ran. Currently running ones will not be affected.
        # Cancel async tasks
        for task in list(context.task_to_node.keys()):
            if not task.done():
                task.cancel()
        return ErrorState

    @staticmethod
    async def on_update(context: ParallelResolutionContext) -> type[State] | None:
        # Don't modify lists while iterating through them.
        task_to_node = context.task_to_node
        for task, node in task_to_node.copy().items():
            if task.done():
                node.node_state = NodeState.DONE
            elif task.cancelled():
                node.node_state = NodeState.CANCELED
            task_to_node.pop(task)

        if len(task_to_node) == 0:
            # ErrorState is entered either because a task raised an exception
            # (error_message is set) or because a task was cancelled via user-
            # initiated flow cancel (error_message is None). Distinguish here
            # so flow_manager.is_errored() doesn't surface "Exception occurred:
            # None" for a clean cancel.
            if context.error_message is None:
                context.workflow_state = WorkflowState.CANCELED
            else:
                context.workflow_state = WorkflowState.ERRORED
            context.networks.clear()
            context.node_to_reference.clear()
            context.task_to_node.clear()
            return DagCompleteState
        # Let's continue going through until everything is cancelled.
        return ErrorState


class DagCompleteState(State):
    @staticmethod
    async def on_enter(context: ParallelResolutionContext) -> type[State] | None:
        # Clear the DAG builder so we don't have any leftover nodes in node_to_reference.
        if context.dag_builder is not None:
            context.dag_builder.clear()
        return None

    @staticmethod
    async def on_update(context: ParallelResolutionContext) -> type[State] | None:  # noqa: ARG004
        return None


class ParallelResolutionMachine(FSM[ParallelResolutionContext]):
    """State machine for building DAG structure without execution."""

    def __init__(
        self, flow_name: str, max_nodes_in_parallel: int | None = None, dag_builder: DagBuilder | None = None
    ) -> None:
        resolution_context = ParallelResolutionContext(
            flow_name, max_nodes_in_parallel=max_nodes_in_parallel, dag_builder=dag_builder
        )
        super().__init__(resolution_context)

    async def resolve_node(self, node: BaseNode | None = None) -> None:  # noqa: ARG002
        """Execute the DAG structure using the existing DagBuilder."""
        if self.context.dag_builder is None:
            self.context.dag_builder = GriptapeNodes.FlowManager().global_dag_builder
        await self.start(ExecuteDagState)

    async def cancel_all_nodes(self) -> None:
        """Cancel all executing tasks and set cancellation flags on all nodes."""
        # Set cancellation flag on all nodes being tracked
        for dag_node in self.context.node_to_reference.values():
            dag_node.node_reference.request_cancellation()

        # For nodes whose ExecuteNodeRequest is routed to a worker subprocess,
        # the asyncio task we'd cancel below is only the orchestrator's wait on
        # the worker's response. Cancelling it leaves the worker running to
        # completion and pinning the per-worker request slot. Dispatch an
        # explicit CancelExecuteNodeRequest to each affected worker so its
        # aprocess task is cancelled on the worker side too. No-op for nodes
        # running locally on the orchestrator.
        node_manager = GriptapeNodes.NodeManager()
        for dag_node in self.context.node_to_reference.values():
            await node_manager.cancel_worker_execution(dag_node.node_reference.name)

        # Cancel all running tasks
        tasks = list(self.context.task_to_node.keys())
        for task in tasks:
            if not task.done():
                task.cancel()

        # Wait for all tasks to complete
        await asyncio.gather(*tasks, return_exceptions=True)

    def change_debug_mode(self, *, debug_mode: bool) -> None:
        self._context.paused = debug_mode

    def is_complete(self) -> bool:
        return self._current_state is DagCompleteState

    def is_started(self) -> bool:
        return self._current_state is not None

    def reset_machine(self, *, cancel: bool = False) -> None:
        self._context.reset(cancel=cancel)
        self._current_state = None

    def get_last_resolved_node(self) -> BaseNode | None:
        """Get the last node that was resolved in the DAG execution."""
        return self._context.last_resolved_node

    def is_errored(self) -> bool:
        return self._context.workflow_state == WorkflowState.ERRORED

    def get_error_message(self) -> str | None:
        return self._context.error_message
