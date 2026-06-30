# Control flow machine
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from griptape_nodes.exe_types.node_types import (
    BaseNode,
    NodeResolutionState,
)
from griptape_nodes.machines.dag_builder import DagBuilder, DagNodeCategories
from griptape_nodes.machines.fsm import FSM, State
from griptape_nodes.machines.parallel_resolution import ParallelResolutionMachine
from griptape_nodes.retained_mode.events.base_events import ExecutionEvent, ExecutionGriptapeNodeEvent
from griptape_nodes.retained_mode.events.execution_events import (
    ControlFlowResolvedEvent,
    CurrentControlNodeEvent,
    InvolvedNodesEvent,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.node_manager import NodeManager
from griptape_nodes.retained_mode.managers.settings import WorkflowExecutionMode

if TYPE_CHECKING:
    from griptape_nodes.exe_types.core_types import Parameter
    from griptape_nodes.exe_types.flow import ControlFlow
    from griptape_nodes.retained_mode.managers.flow_manager import FlowManager


@dataclass
class NextNodeInfo:
    """Information about the next node to execute and how to reach it."""

    node: BaseNode
    entry_parameter: Parameter | None


logger = logging.getLogger("griptape_nodes")


# This is the control flow context. Owns the Resolution Machine
class ControlFlowContext:
    flow: ControlFlow
    current_nodes: list[BaseNode]
    resolution_machine: ParallelResolutionMachine
    selected_output: Parameter | None
    paused: bool = False
    flow_name: str
    pickle_control_flow_result: bool
    end_node: BaseNode | None = None
    is_isolated: bool

    def __init__(
        self,
        flow_name: str,
        max_nodes_in_parallel: int,
        *,
        pickle_control_flow_result: bool = False,
        is_isolated: bool = False,
    ) -> None:
        self.flow_name = flow_name

        # ALWAYS create ParallelResolutionMachine (SEQUENTIAL mode now maps to PARALLEL with max_nodes_in_parallel=1)
        # Get the global DagBuilder from FlowManager

        # Create isolated DagBuilder for independent subflows
        if is_isolated:
            dag_builder = DagBuilder()
            logger.debug("Created isolated DagBuilder for flow '%s'", flow_name)
        else:
            dag_builder = GriptapeNodes.FlowManager().global_dag_builder

        self.resolution_machine = ParallelResolutionMachine(flow_name, max_nodes_in_parallel, dag_builder=dag_builder)
        self.current_nodes = []
        self.pickle_control_flow_result = pickle_control_flow_result
        self.is_isolated = is_isolated

    def reset(self, *, cancel: bool = False) -> None:
        if self.current_nodes is not None:
            for node in self.current_nodes:
                node.clear_node()
        self.current_nodes = []
        self.resolution_machine.reset_machine(cancel=cancel)
        self.selected_output = None
        self.paused = False


# GOOD!
class ResolveNodeState(State):
    @staticmethod
    async def on_enter(context: ControlFlowContext) -> type[State] | None:
        # The state machine has started, but it hasn't began to execute yet.
        if len(context.current_nodes) == 0:
            # We don't have anything else to do. Move back to Complete State so it has to restart.
            return CompleteState

        # Mark all current nodes unresolved and broadcast events
        for current_node in context.current_nodes:
            if not current_node.lock:
                current_node.make_node_unresolved(
                    current_states_to_trigger_change_event=set(
                        {NodeResolutionState.UNRESOLVED, NodeResolutionState.RESOLVED, NodeResolutionState.RESOLVING}
                    )
                )
            # Now broadcast that we have a current control node.
            GriptapeNodes.EventManager().put_event(
                ExecutionGriptapeNodeEvent(
                    wrapped_event=ExecutionEvent(payload=CurrentControlNodeEvent(node_name=current_node.name))
                )
            )
            logger.info("Resolving %s", current_node.name)
        if not context.paused:
            # Call the update. Otherwise wait
            return ResolveNodeState
        return None

    # This is necessary to transition to the next step.
    @staticmethod
    async def on_update(context: ControlFlowContext) -> type[State] | None:
        # If no current nodes, we're done
        if len(context.current_nodes) == 0:
            return CompleteState

        # Resolve nodes - pass first node for sequential resolution
        current_node = context.current_nodes[0] if context.current_nodes else None
        await context.resolution_machine.resolve_node(current_node)
        if context.resolution_machine.is_complete():
            # Get the last resolved node from the DAG and set it as current
            last_resolved_node = context.resolution_machine.get_last_resolved_node()
            if last_resolved_node:
                context.current_nodes = [last_resolved_node]
            return CompleteState
        return None


class CompleteState(State):
    @staticmethod
    async def on_enter(context: ControlFlowContext) -> type[State] | None:
        # Broadcast completion events for any remaining current nodes
        for current_node in context.current_nodes:
            # Use pickle-based serialization for complex parameter output values

            parameter_output_values, unique_uuid_to_values = NodeManager.serialize_parameter_output_values(
                current_node, use_pickling=context.pickle_control_flow_result
            )
            GriptapeNodes.EventManager().put_event(
                ExecutionGriptapeNodeEvent(
                    wrapped_event=ExecutionEvent(
                        payload=ControlFlowResolvedEvent(
                            end_node_name=current_node.name,
                            parameter_output_values=parameter_output_values,
                            unique_parameter_uuid_to_values=unique_uuid_to_values or None,
                        )
                    )
                )
            )
        context.end_node = None
        logger.info("Flow is complete.")
        return None

    @staticmethod
    async def on_update(context: ControlFlowContext) -> type[State] | None:  # noqa: ARG004
        return None


# MACHINE TIME!!!
class ControlFlowMachine(FSM[ControlFlowContext]):
    def __init__(
        self,
        flow_name: str,
        *,
        pickle_control_flow_result: bool = False,
        is_isolated: bool = False,
    ) -> None:
        execution_type = GriptapeNodes.ConfigManager().get_config_value(
            "workflow_execution_mode", default=WorkflowExecutionMode.SEQUENTIAL
        )
        max_nodes_in_parallel = GriptapeNodes.ConfigManager().get_config_value("max_nodes_in_parallel", default=5)

        # SEQUENTIAL mode uses ParallelResolutionMachine with max_nodes_in_parallel=1
        if execution_type == WorkflowExecutionMode.SEQUENTIAL:
            max_nodes_in_parallel = 1

        context = ControlFlowContext(
            flow_name,
            max_nodes_in_parallel,
            pickle_control_flow_result=pickle_control_flow_result,
            is_isolated=is_isolated,
        )
        super().__init__(context)

    async def start_flow(
        self, start_node: BaseNode, end_node: BaseNode | None = None, *, debug_mode: bool = False
    ) -> None:
        # If using DAG resolution, process data_nodes from queue first
        current_nodes = await self._process_nodes_for_dag(start_node)
        self._context.current_nodes = current_nodes
        self._context.end_node = end_node
        # Set entry control parameter for initial node (None for workflow start)
        for node in current_nodes:
            node.set_entry_control_parameter(None)
        # Set up to debug
        self._context.paused = debug_mode
        flow_manager = GriptapeNodes.FlowManager()
        flow = flow_manager.get_flow_by_name(self._context.flow_name)
        if start_node != end_node:
            # This blocks all nodes in the entire flow from running. If we're just resolving one node, we don't want to block that.
            involved_nodes = list(flow.nodes.keys())
            GriptapeNodes.EventManager().put_event(
                ExecutionGriptapeNodeEvent(
                    wrapped_event=ExecutionEvent(payload=InvolvedNodesEvent(involved_nodes=involved_nodes))
                )
            )
        await self.start(ResolveNodeState)  # Begins the flow

    async def update(self) -> None:
        if self._current_state is None:
            msg = "Attempted to run the next step of a workflow that was either already complete or has not started."
            raise RuntimeError(msg)
        await super().update()

    def change_debug_mode(self, debug_mode: bool) -> None:  # noqa: FBT001
        self._context.paused = debug_mode
        self._context.resolution_machine.change_debug_mode(debug_mode=debug_mode)

    async def granular_step(self, change_debug_mode: bool) -> None:  # noqa: FBT001
        resolution_machine = self._context.resolution_machine

        if change_debug_mode:
            resolution_machine.change_debug_mode(debug_mode=True)
        await resolution_machine.update()

        # Tick the control flow if the current machine isn't busy
        if self._current_state is ResolveNodeState and (  # noqa: SIM102
            resolution_machine.is_complete() or not resolution_machine.is_started()
        ):
            # Don't tick ourselves if we are already complete.
            if self._current_state is not None:
                await self.update()

    async def node_step(self) -> None:
        resolution_machine = self._context.resolution_machine

        resolution_machine.change_debug_mode(debug_mode=False)

        # If we're in the resolution phase, step the resolution machine
        if self._current_state is ResolveNodeState:
            await resolution_machine.update()

        # Tick the control flow if the current machine isn't busy
        if self._current_state is ResolveNodeState and (
            resolution_machine.is_complete() or not resolution_machine.is_started()
        ):
            await self.update()

    async def _process_nodes_for_dag(self, start_node: BaseNode) -> list[BaseNode]:
        """Seed the DAG for this run and return the entry (control) nodes.

        Top-level runs and isolated subflows share one classifier and one seeding routine; they
        differ only in scope. A top-level run draws its categorized nodes from the global queue
        (already scoped by get_start_node_queue to exclude referenced subflows and group
        children). An isolated subflow classifies its own nodes directly, since the global queue
        deliberately omits referenced subflows. Either way the same seeding logic runs, so a
        data-only subflow resolves its leaf nodes instead of stopping at the start node.
        """
        # Use the DagBuilder from the resolution machine context (may be isolated or global)
        dag_builder = self._context.resolution_machine.context.dag_builder
        if dag_builder is None:
            msg = "DAG builder is not initialized."
            raise ValueError(msg)

        # Build with the first node (it should already be the proxy if it's part of a group)
        dag_builder.add_node_with_dependencies(start_node, start_node.name)

        flow_manager = GriptapeNodes.FlowManager()
        node_manager = GriptapeNodes.NodeManager()
        is_isolated = dag_builder is not flow_manager.global_dag_builder

        if is_isolated:
            subflow = flow_manager.get_flow_by_name(self._context.flow_name)
            categories = flow_manager.classify_nodes_for_dag(list(subflow.nodes.values()))
            logger.debug("Seeding isolated subflow '%s' DAG from its own nodes", self._context.flow_name)
        else:
            categories = self._drain_global_flow_queue(flow_manager)

        return self._seed_dag_from_categories(start_node, categories, dag_builder, flow_manager, node_manager)

    @staticmethod
    def _drain_global_flow_queue(flow_manager: FlowManager) -> DagNodeCategories:
        """Consume the global flow queue into categorized node lists for seeding.

        The queue is populated (and scoped) by get_start_node_queue before a top-level run. Draining
        it here preserves the existing contract that the queue is emptied during DAG construction.
        """
        from griptape_nodes.retained_mode.managers.flow_manager import DagExecutionType

        start_nodes: list[BaseNode] = []
        control_nodes: list[BaseNode] = []
        data_sink_nodes: list[BaseNode] = []
        for item in list(flow_manager.global_flow_queue.queue):
            if item.dag_execution_type == DagExecutionType.START_NODE:
                start_nodes.append(item.node)
            elif item.dag_execution_type == DagExecutionType.CONTROL_NODE:
                control_nodes.append(item.node)
            elif item.dag_execution_type == DagExecutionType.DATA_NODE:
                data_sink_nodes.append(item.node)
            flow_manager.global_flow_queue.queue.remove(item)
        return DagNodeCategories(
            start_nodes=start_nodes, control_nodes=control_nodes, data_sink_nodes=data_sink_nodes
        )

    def _seed_dag_from_categories(
        self,
        start_node: BaseNode,
        categories: DagNodeCategories,
        dag_builder: DagBuilder,
        flow_manager: FlowManager,
        node_manager: NodeManager,
    ) -> list[BaseNode]:
        """Seed the DAG from categorized nodes and return the entry (control) nodes.

        PASS 1 adds start/control entry nodes so control-flow graphs exist. PASS 2 adds data
        sink (terminal/leaf) nodes: a sink reachable from a graph's forward control path is
        registered as a control-gated candidate (so branches stay gated), while a sink that is
        only data-connected gets its own graph so its dependencies resolve unconditionally.
        """
        start_nodes = [start_node]

        # PASS 1: control/start entries build the control-flow graphs.
        for node in (*categories.start_nodes, *categories.control_nodes):
            node.state = NodeResolutionState.UNRESOLVED
            if node.name not in dag_builder.node_to_reference:
                dag_builder.add_node_with_dependencies(node, node.name)
                if node not in start_nodes:
                    start_nodes.append(node)

        # PASS 2: data sinks, after the control graphs exist.
        for node in categories.data_sink_nodes:
            node.state = NodeResolutionState.UNRESOLVED
            if node.name in dag_builder.node_to_reference:
                continue
            disconnected = True
            for graph_start_node_name in list(dag_builder.graphs):
                graph_start_node = node_manager.get_node_by_name(graph_start_node_name)
                boundary_nodes = flow_manager.is_node_connected(graph_start_node, node)
                if boundary_nodes:
                    disconnected = False
                    if node.name not in dag_builder.start_node_candidates:
                        dag_builder.start_node_candidates[node.name] = {}
                    dag_builder.start_node_candidates[node.name][graph_start_node_name] = set(boundary_nodes)
            if disconnected:
                # Not gated by any control graph - resolve it (and its dependencies) on its own.
                dag_builder.add_node_with_dependencies(node, node.name)

        return start_nodes

    async def cancel_flow(self) -> None:
        """Cancel all nodes in the flow by delegating to the resolution machine."""
        await self.resolution_machine.cancel_all_nodes()

    def reset_machine(self, *, cancel: bool = False) -> None:
        self._context.reset(cancel=cancel)
        self._current_state = None

    @property
    def resolution_machine(self) -> ParallelResolutionMachine:
        return self._context.resolution_machine
