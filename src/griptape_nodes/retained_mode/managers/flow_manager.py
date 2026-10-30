from __future__ import annotations

import asyncio
import contextlib
import logging
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from queue import Queue
from typing import TYPE_CHECKING, NamedTuple, cast
from uuid import uuid4

from griptape_nodes.common.node_executor import NodeExecutor
from griptape_nodes.exe_types.base_iterative_nodes import BaseIterativeStartNode
from griptape_nodes.exe_types.connections import Connections
from griptape_nodes.exe_types.core_types import (
    Parameter,
    ParameterContainer,
    ParameterMode,
    ParameterType,
    ParameterTypeBuiltin,
)
from griptape_nodes.exe_types.flow import ControlFlow
from griptape_nodes.exe_types.node_groups import BaseNodeGroup, SubflowNodeGroup
from griptape_nodes.exe_types.node_types import (
    BaseNode,
    ErrorProxyNode,
    NodeResolutionState,
    StartNode,
)
from griptape_nodes.machines.control_flow import CompleteState, ControlFlowMachine
from griptape_nodes.machines.dag_builder import DagBuilder, DagNodeCategories, NodeState
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.base_events import (
    ExecutionEvent,
    ExecutionGriptapeNodeEvent,
    ResultDetails,
)
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultFailure,
    CreateConnectionResultSuccess,
    DeleteConnectionRequest,
    DeleteConnectionResultFailure,
    DeleteConnectionResultSuccess,
    ReplacedConnection,
)
from griptape_nodes.retained_mode.events.execution_events import (
    CancelAllRunsRequest,
    CancelAllRunsResultFailure,
    CancelAllRunsResultSuccess,
    CancelFlowRequest,
    CancelFlowResultFailure,
    CancelFlowResultSuccess,
    CancelRunRequest,
    CancelRunResultFailure,
    CancelRunResultSuccess,
    ContinueExecutionStepRequest,
    ContinueExecutionStepResultFailure,
    ContinueExecutionStepResultSuccess,
    ControlFlowCancelledEvent,
    GetFlowStateRequest,
    GetFlowStateResultFailure,
    GetFlowStateResultSuccess,
    GetIsFlowRunningRequest,
    GetIsFlowRunningResultFailure,
    GetIsFlowRunningResultSuccess,
    InvolvedNodesEvent,
    ListRunsRequest,
    ListRunsResultSuccess,
    RunIdMixin,
    RunInfo,
    RunQueuedEvent,
    SingleExecutionStepRequest,
    SingleExecutionStepResultFailure,
    SingleExecutionStepResultSuccess,
    SingleNodeStepRequest,
    SingleNodeStepResultFailure,
    SingleNodeStepResultSuccess,
    StartFlowFromNodeRequest,
    StartFlowFromNodeResultFailure,
    StartFlowFromNodeResultSuccess,
    StartFlowRequest,
    StartFlowResultFailure,
    StartFlowResultSuccess,
    StartLocalSubflowRequest,
    StartLocalSubflowResultFailure,
    StartLocalSubflowResultSuccess,
    UnresolveFlowRequest,
    UnresolveFlowResultFailure,
    UnresolveFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import (
    TRANSIENT_KEY,
    AutoLayoutFlowRequest,
    AutoLayoutFlowResultFailure,
    AutoLayoutFlowResultSuccess,
    CreateFlowRequest,
    CreateFlowResultFailure,
    CreateFlowResultSuccess,
    DeleteFlowRequest,
    DeleteFlowResultFailure,
    DeleteFlowResultSuccess,
    GetFlowDetailsRequest,
    GetFlowDetailsResultFailure,
    GetFlowDetailsResultSuccess,
    GetFlowMetadataRequest,
    GetFlowMetadataResultFailure,
    GetFlowMetadataResultSuccess,
    GetTopLevelFlowRequest,
    GetTopLevelFlowResultSuccess,
    ListFlowsInCurrentContextRequest,
    ListFlowsInCurrentContextResultFailure,
    ListFlowsInCurrentContextResultSuccess,
    ListFlowsInFlowRequest,
    ListFlowsInFlowResultFailure,
    ListFlowsInFlowResultSuccess,
    ListNodesInFlowRequest,
    ListNodesInFlowResultFailure,
    ListNodesInFlowResultSuccess,
    NodePosition,
    SetFlowMetadataRequest,
    SetFlowMetadataResultFailure,
    SetFlowMetadataResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import (
    DeleteNodeRequest,
    DeleteNodeResultFailure,
    SetNodeMetadataRequest,
    SetNodeMetadataResultSuccess,
)
from griptape_nodes.retained_mode.events.parameter_events import (
    SetParameterValueRequest,
)
from griptape_nodes.retained_mode.events.validation_events import (
    ValidateFlowDependenciesRequest,
    ValidateFlowDependenciesResultFailure,
    ValidateFlowDependenciesResultSuccess,
)
from griptape_nodes.retained_mode.managers.event_manager import current_request_id
from griptape_nodes.retained_mode.managers.flow.packaging import FlowPackaging
from griptape_nodes.retained_mode.managers.flow.serialization import FlowSerialization
from griptape_nodes.retained_mode.managers.run_queue import Run, RunQueue, RunState
from griptape_nodes.retained_mode.managers.settings import WorkflowExecutionMode
from griptape_nodes.retained_mode.request_handlers import handles

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager
    from griptape_nodes.retained_mode.managers.run_queue import RunSpec

logger = logging.getLogger("griptape_nodes")

# The run the code executing now belongs to. Tasks copy it when they are created, so every node a
# run executes sees it, and a request a node makes can tell it comes from inside the run.
_current_run_id: ContextVar[str | None] = ContextVar("current_run_id", default=None)

_CANCELLED_BEFORE_STARTING = "The run did not start: it was cancelled."


@dataclass(frozen=True)
class StartFlowSpec:
    """A run of a whole flow, from a `StartFlowRequest`."""

    request: StartFlowRequest
    flow_name: str

    async def execute(self, engine: Engine) -> ResultPayload:
        try:
            flow = engine.flow_manager.get_flow_by_name(self.flow_name)
        except KeyError as err:
            return StartFlowResultFailure(
                validation_exceptions=[err], result_details=f"Cannot start flow. Error: {err}"
            )
        return await engine.flow_manager.execute_start_flow(self.request, self.flow_name, flow)

    def failure(self, details: str) -> ResultPayload:
        return StartFlowResultFailure(validation_exceptions=[], result_details=details)


@dataclass(frozen=True)
class StartFlowFromNodeSpec:
    """A run of a flow from one of its nodes, from a `StartFlowFromNodeRequest`."""

    request: StartFlowFromNodeRequest
    flow_name: str
    node_name: str

    async def execute(self, engine: Engine) -> ResultPayload:
        start_node = engine.object_manager.attempt_get_object_by_name_as_type(self.node_name, BaseNode)
        if not start_node:
            return self.failure(f"Provided node with name {self.node_name} does not exist")
        try:
            flow = engine.flow_manager.get_flow_by_name(self.flow_name)
        except KeyError as err:
            return StartFlowFromNodeResultFailure(
                validation_exceptions=[err], result_details=f"Cannot start flow. Error: {err}"
            )
        return await engine.flow_manager.execute_start_flow_from_node(self.request, self.flow_name, flow, start_node)

    def failure(self, details: str) -> ResultPayload:
        return StartFlowFromNodeResultFailure(validation_exceptions=[], result_details=details)


class DagExecutionType(StrEnum):
    START_NODE = "start_node"
    CONTROL_NODE = "control_node"
    DATA_NODE = "data_node"


class QueueItem(NamedTuple):
    """Represents an item in the flow execution queue."""

    node: BaseNode
    dag_execution_type: DagExecutionType


class FlowManager(EngineScoped):
    _name_to_parent_name: dict[str, str | None]
    _flow_to_referenced_workflow_name: dict[ControlFlow, str]
    _connections: Connections

    # Global execution state (moved from individual ControlFlows)
    _global_flow_queue: Queue[QueueItem]
    _global_control_flow_machine: ControlFlowMachine | None
    _global_single_node_resolution: bool
    _global_dag_builder: DagBuilder
    _node_executor: NodeExecutor

    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        event_manager.register_request_handlers(self)

        self._name_to_parent_name = {}
        self._flow_to_referenced_workflow_name = {}
        self._connections = Connections()

        # Initialize global execution state
        self._global_flow_queue = Queue[QueueItem]()
        self._global_control_flow_machine = None  # Track the current control flow machine
        self._global_single_node_resolution = False
        self._global_dag_builder = DagBuilder(self.engine)
        self._node_executor = NodeExecutor(self.engine)
        # The run queue and the run in progress are read and written without a lock, so only the engine loop
        # may touch them. A run request sent synchronously from another thread can reach them from that
        # thread's loop, which is not supported yet (#6039).
        self._run_queue = RunQueue()
        self._run_in_progress: Run | None = None
        # True from when the run in progress starts until its request returns. Until then only `_drive` may
        # end the run: its flow may not have started yet (still validating) or may have just been cancelled,
        # and the paths that end a paused run must not mistake either for the run being over.
        self._driving = False
        # True while cancel_all_runs awaits the run in progress, so a run submitted meanwhile waits to be turned away.
        self._cancelling_all = False

        self.packaging = FlowPackaging(event_manager, engine=engine)
        self.serialization = FlowSerialization(event_manager, engine=engine)

    @property
    def global_single_node_resolution(self) -> bool:
        return self._global_single_node_resolution

    @property
    def global_flow_queue(self) -> Queue[QueueItem]:
        return self._global_flow_queue

    @property
    def global_dag_builder(self) -> DagBuilder:
        return self._global_dag_builder

    @property
    def node_executor(self) -> NodeExecutor:
        return self._node_executor

    def get_connections(self) -> Connections:
        """Get the connections instance."""
        return self._connections

    def _cleanup_flow_on_failed_deserialization(self, flow_name: str, *, pushed_flow_context: bool) -> None:
        """Clean up a flow that failed during deserialization.

        This method deletes the flow (which cascades to delete all nodes and connections)
        and pops the flow context if it was pushed.

        Args:
            flow_name: The name of the flow to delete
            pushed_flow_context: Whether we pushed this flow to the context
        """
        if pushed_flow_context:
            self.engine.context_manager.pop_flow()

        delete_flow_request = DeleteFlowRequest(flow_name=flow_name)
        delete_result = self.engine.handle_request(delete_flow_request)
        if delete_result.failed():
            logger.warning(
                "Failed to clean up flow '%s' after deserialization failure: %s",
                flow_name,
                delete_result.result_details,
            )

    def _has_connection(
        self,
        source_node: BaseNode,
        source_parameter: Parameter,
        target_node: BaseNode,
        target_parameter: Parameter,
    ) -> bool:
        """Check if a connection exists."""
        connected_outputs = self.get_connected_output_parameters(source_node, source_parameter)
        for connected_node, connected_param in connected_outputs:
            # By name, as the connection indexes are: a node can replace a connected parameter with
            # a same-named one, and the connection still holds the one it replaced.
            if connected_node is target_node and connected_param.name == target_parameter.name:
                return True
        return False

    def get_connected_output_parameters(self, node: BaseNode, param: Parameter) -> list[tuple[BaseNode, Parameter]]:
        """Get connected output parameters."""
        connections = []
        if node.name in self._connections.outgoing_index:
            outgoing_params = self._connections.outgoing_index[node.name]
            if param.name in outgoing_params:
                for connection_id in outgoing_params[param.name]:
                    connection = self._connections.connections[connection_id]
                    connections.append((connection.target_node, connection.target_parameter))
        return connections

    def _get_connections_for_flow(self, flow: ControlFlow) -> list:
        """Get connections where both nodes are in the specified flow or its child flows.

        For parent flows, this includes cross-flow connections between the parent and its children.
        For child flows, this only includes connections within that specific flow.
        """
        flow_connections = []
        flow_name = flow.name

        # Get all child flow names for this flow
        child_flow_names = set()
        for child_name, parent_name in self._name_to_parent_name.items():
            if parent_name == flow_name:
                child_flow_names.add(child_name)

        # Build set of all node names in this flow and its direct children.
        # Exclude nodes from referenced workflow children - their internal connections
        # are not serialized at the parent level (they serialize as a standalone workflow).
        # Also exclude transient children (e.g. per-iteration loop-body flows): their nodes are
        # skipped by the node-serialization loop and never enter the UUID map, so collecting their
        # connections here would fail the whole save with a "node not found in UUID map" error.
        all_node_names = set(flow.nodes.keys())
        for child_flow_name in child_flow_names:
            child_flow = self.engine.object_manager.attempt_get_object_by_name_as_type(child_flow_name, ControlFlow)
            if child_flow is None:
                continue
            if self.is_referenced_workflow(child_flow) or child_flow.metadata.get(TRANSIENT_KEY):
                continue
            all_node_names.update(child_flow.nodes.keys())

        # Include connections where both nodes are in this flow hierarchy
        for connection in self._connections.connections.values():
            source_in_hierarchy = connection.source_node.name in all_node_names
            target_in_hierarchy = connection.target_node.name in all_node_names

            if source_in_hierarchy and target_in_hierarchy:
                flow_connections.append(connection)

        return flow_connections

    def get_parent_flow(self, flow_name: str) -> str | None:
        if flow_name in self._name_to_parent_name:
            return self._name_to_parent_name[flow_name]
        msg = f"Flow with name {flow_name} doesn't exist"
        raise ValueError(msg)

    def reparent_flow(self, flow_name: str, new_parent_flow_name: str) -> None:
        """Move a Flow under a different parent Flow.

        Used when a node group is nested inside another: the inner group's subflow has to become a
        child of the outer group's subflow so the saved flow hierarchy mirrors the group nesting.
        Serialization walks parent/child links, so a subflow left parented to the top-level flow is
        saved outside its enclosing group and its members go missing on load.

        No peer manager has to be told: ``_name_to_parent_name`` is the only record of parentage, and
        NodeManager tracks a node's flow by name, which reparenting does not change.

        Unlike every other structural change in retained mode, this is a plain method rather than a
        request, so nothing is emitted and the editor is not told. That is a real gap, not a design
        choice: there is no ``ReparentFlowRequest`` to route it through. It holds today because the
        nesting that triggers it happens inside ``AddNodesToNodeGroupRequest``, whose own result the
        editor already acts on, and because the editor derives group nesting from ``parent_group``
        rather than from flow parentage. An editor feature that reads flow parentage directly would
        need this promoted to a request first.

        Args:
            flow_name: The Flow to move
            new_parent_flow_name: The Flow that should become its parent

        Raises:
            ValueError: If either Flow is unknown, or the move would create a cycle
        """
        if flow_name not in self._name_to_parent_name:
            msg = f"Flow with name {flow_name} doesn't exist"
            raise ValueError(msg)
        if new_parent_flow_name not in self._name_to_parent_name:
            msg = f"Flow with name {new_parent_flow_name} doesn't exist"
            raise ValueError(msg)
        if flow_name == new_parent_flow_name:
            msg = f"Attempted to make Flow '{flow_name}' its own parent."
            raise ValueError(msg)

        # Walk up from the proposed parent: if we meet the flow being moved, the move would
        # detach that branch from the tree and make the ancestor walk loop forever.
        ancestor = self._name_to_parent_name[new_parent_flow_name]
        while ancestor is not None:
            if ancestor == flow_name:
                msg = (
                    f"Attempted to move Flow '{flow_name}' under '{new_parent_flow_name}', which is already inside it."
                )
                raise ValueError(msg)
            ancestor = self._name_to_parent_name.get(ancestor)

        self._name_to_parent_name[flow_name] = new_parent_flow_name

    def is_referenced_workflow(self, flow: ControlFlow) -> bool:
        """Check if this flow was created by importing a referenced workflow.

        Returns True if this flow originated from a workflow import operation,
        False if it was created standalone.
        """
        return flow in self._flow_to_referenced_workflow_name

    def get_referenced_workflow_name(self, flow: ControlFlow) -> str | None:
        """Get the name of the referenced workflow, if any.

        Returns the workflow name that was imported to create this flow,
        or None if this flow was created standalone.
        """
        return self._flow_to_referenced_workflow_name.get(flow)

    @handles(GetTopLevelFlowRequest)
    def on_get_top_level_flow_request(self, request: GetTopLevelFlowRequest) -> ResultPayload:  # noqa: ARG002 (the request has to be assigned to the method)
        for flow_name, parent in self._name_to_parent_name.items():
            if parent is None:
                return GetTopLevelFlowResultSuccess(
                    flow_name=flow_name, result_details=f"Successfully found top level flow: '{flow_name}'"
                )
        msg = "Attempted to get top level flow, but no such flow exists"
        logger.debug(msg)
        return GetTopLevelFlowResultSuccess(flow_name=None, result_details=msg)

    @handles(GetFlowDetailsRequest)
    def on_get_flow_details_request(self, request: GetFlowDetailsRequest) -> ResultPayload:
        flow_name = request.flow_name
        flow = None

        if flow_name is None:
            # We want to get details for whatever is at the top of the Current Context.
            if not self.engine.context_manager.has_current_flow():
                details = "Attempted to get Flow details from the Current Context. Failed because the Current Context was empty."
                return GetFlowDetailsResultFailure(result_details=details)
            flow = self.engine.context_manager.get_current_flow()
            flow_name = flow.name
        else:
            flow = self.engine.object_manager.attempt_get_object_by_name_as_type(flow_name, ControlFlow)
            if flow is None:
                details = (
                    f"Attempted to get Flow details for '{flow_name}'. Failed because no Flow with that name exists."
                )
                return GetFlowDetailsResultFailure(result_details=details)

        try:
            parent_flow_name = self.get_parent_flow(flow_name)
        except ValueError:
            details = f"Attempted to get Flow details for '{flow_name}'. Failed because Flow does not exist in parent mapping."
            return GetFlowDetailsResultFailure(result_details=details)

        referenced_workflow_name = None
        if self.is_referenced_workflow(flow):
            referenced_workflow_name = self.get_referenced_workflow_name(flow)

        flow_type = flow.metadata.get("flow_type")

        details = f"Successfully retrieved Flow details for '{flow_name}'."
        return GetFlowDetailsResultSuccess(
            referenced_workflow_name=referenced_workflow_name,
            parent_flow_name=parent_flow_name,
            flow_type=flow_type,
            result_details=details,
        )

    @handles(GetFlowMetadataRequest)
    def on_get_flow_metadata_request(self, request: GetFlowMetadataRequest) -> ResultPayload:
        flow_name = request.flow_name
        flow = None
        if flow_name is None:
            # Get from the current context.
            if not self.engine.context_manager.has_current_flow():
                details = "Attempted to get metadata for a Flow from the Current Context. Failed because the Current Context is empty."
                return GetFlowMetadataResultFailure(result_details=details)

            flow = self.engine.context_manager.get_current_flow()
            flow_name = flow.name

        # Does this flow exist?
        if flow is None:
            obj_mgr = self.engine.object_manager
            flow = obj_mgr.attempt_get_object_by_name_as_type(flow_name, ControlFlow)
            if flow is None:
                details = f"Attempted to get metadata for a Flow '{flow_name}', but no such Flow was found."
                return GetFlowMetadataResultFailure(result_details=details)

        metadata = flow.metadata
        details = f"Successfully retrieved metadata for a Flow '{flow_name}'."

        return GetFlowMetadataResultSuccess(metadata=metadata, result_details=details)

    @handles(SetFlowMetadataRequest)
    def on_set_flow_metadata_request(self, request: SetFlowMetadataRequest) -> ResultPayload:
        flow_name = request.flow_name
        flow = None
        if flow_name is None:
            # Get from the current context.
            if not self.engine.context_manager.has_current_flow():
                details = "Attempted to set metadata for a Flow from the Current Context. Failed because the Current Context is empty."
                return SetFlowMetadataResultFailure(result_details=details)

            flow = self.engine.context_manager.get_current_flow()
            flow_name = flow.name

        # Does this flow exist?
        if flow is None:
            obj_mgr = self.engine.object_manager
            flow = obj_mgr.attempt_get_object_by_name_as_type(flow_name, ControlFlow)
            if flow is None:
                details = f"Attempted to set metadata for a Flow '{flow_name}', but no such Flow was found."
                return SetFlowMetadataResultFailure(result_details=details)

        # We can't completely overwrite metadata.
        for key, value in request.metadata.items():
            flow.metadata[key] = value
        details = f"Successfully set metadata for a Flow '{flow_name}'."

        return SetFlowMetadataResultSuccess(result_details=details)

    def does_canvas_exist(self) -> bool:
        """Determines if there is already an existing flow with no parent flow.Returns True if there is an existing flow with no parent flow.Return False if there is no existing flow with no parent flow."""
        return any([parent is None for parent in self._name_to_parent_name.values()])  # noqa: C419

    @handles(CreateFlowRequest)
    def on_create_flow_request(self, request: CreateFlowRequest) -> ResultPayload:
        # Who is the parent?
        parent_name = request.parent_flow_name

        # This one's tricky. If they said "None" for the parent, they could either be saying:
        # 1. Use whatever the current context is to be the parent.
        # 2. Create me as the canvas (i.e., the top-level flow, of which there can be only one)

        # We'll explore #1 first by seeing if the Context Manager already has a current flow,
        # which would mean the canvas is already established:
        parent = None
        if (parent_name is None) and (self.engine.context_manager.has_current_flow()):
            # Aha! Just use that.
            parent = self.engine.context_manager.get_current_flow()
            parent_name = parent.name

        # TODO: FIX THIS LOGIC MESS https://github.com/griptape-ai/griptape-nodes/issues/616

        if parent_name is not None and parent is None:
            parent = self.engine.object_manager.attempt_get_object_by_name_as_type(parent_name, ControlFlow)
        if parent_name is None:
            if self.does_canvas_exist():
                # We're trying to create the canvas. Ensure that parent does NOT already exist.
                details = "Attempted to create a Flow as the Canvas (top-level Flow with no parents), but the Canvas already exists."
                result = CreateFlowResultFailure(result_details=details)
                return result
        # Now our parent exists, right?
        elif parent is None:
            details = f"Attempted to create a Flow with a parent '{request.parent_flow_name}', but no parent with that name could be found."

            result = CreateFlowResultFailure(result_details=details)

            return result

        # We need to have a current workflow context to proceed.
        if not self.engine.context_manager.has_current_workflow():
            details = "Attempted to create a Flow, but no Workflow was active in the Current Context."
            return CreateFlowResultFailure(result_details=details)

        # Create it.
        final_flow_name = self.engine.object_manager.generate_name_for_object(
            type_name="ControlFlow", requested_name=request.flow_name
        )
        # Check if we're creating this flow within a referenced workflow context
        # This will inform the engine to maintain a reference to the workflow
        # when serializing it. It may inform the editor to render it differently.
        workflow_manager = self.engine.workflow_manager
        flow = ControlFlow(name=final_flow_name, engine=self.engine, metadata=request.metadata)
        self.engine.object_manager.add_object_by_name(name=final_flow_name, obj=flow)
        self._name_to_parent_name[final_flow_name] = parent_name

        # Track referenced workflow if this flow was created within a referenced workflow context
        if workflow_manager.has_current_referenced_workflow():
            referenced_workflow_name = workflow_manager.get_current_referenced_workflow()
            self._flow_to_referenced_workflow_name[flow] = referenced_workflow_name

        # See if we need to push it as the current context.
        if request.set_as_new_context:
            self.engine.context_manager.push_flow(flow)

        # Success
        details = f"Successfully created Flow '{final_flow_name}'."
        log_level = logging.DEBUG
        if (request.flow_name is not None) and (final_flow_name != request.flow_name):
            details = f"{details} WARNING: Had to rename from original Flow requested '{request.flow_name}' as an object with this name already existed."
            log_level = logging.WARNING

        result = CreateFlowResultSuccess(
            flow_name=final_flow_name, result_details=ResultDetails(message=details, level=log_level)
        )
        return result

    # This needs to have a lot of branches to check the flow in all possible situations. In Current Context, or when the name is passed in.
    @handles(DeleteFlowRequest)
    def on_delete_flow_request(self, request: DeleteFlowRequest) -> ResultPayload:  # noqa: C901, PLR0911, PLR0912, PLR0915
        flow_name = request.flow_name
        flow = None
        if flow_name is None:
            # We want to delete whatever is at the top of the Current Context.
            if not self.engine.context_manager.has_current_flow():
                details = (
                    "Attempted to delete a Flow from the Current Context. Failed because the Current Context was empty."
                )
                result = DeleteFlowResultFailure(result_details=details)
                return result
            # We pop it off here, but we'll re-add it using context in a moment.
            flow = self.engine.context_manager.pop_flow()

        # Does this Flow even exist?
        if flow is None and flow_name is not None:
            obj_mgr = self.engine.object_manager
            flow = obj_mgr.attempt_get_object_by_name_as_type(flow_name, ControlFlow)
        if flow is None:
            details = f"Attempted to delete Flow '{flow_name}', but no Flow with that name could be found."
            result = DeleteFlowResultFailure(result_details=details)
            return result

        # Before the cancel below, which would start the next waiting run against the flow being deleted.
        self._turn_away(self._run_queue.runs_for_flow(flow.name), f"its flow '{flow.name}' was deleted.")

        # Only cancel if the flow being deleted is the one tracked by the global control flow machine.
        # Isolated subflows (e.g., ForEach loop iterations) have their own separate ControlFlowMachine
        # and should not trigger cancellation of the global machine.
        if (
            self.check_for_existing_running_flow()
            and self._global_control_flow_machine is not None
            and self._global_control_flow_machine.context.flow_name == flow.name
        ):
            result = self.engine.handle_request(CancelFlowRequest(flow_name=flow.name))
            if result.failed():
                details = f"Attempted to delete flow '{flow_name}'. Failed because running flow could not cancel."
                return DeleteFlowResultFailure(result_details=details)

        # Let this Flow assume the Current Context while we delete everything within it.
        with self.engine.context_manager.flow(flow=flow):
            # Delete all child Flows of this Flow.
            # Note: We use ListFlowsInCurrentContextRequest here instead of ListFlowsInFlowRequest(parent_flow_name=None)
            # because None in ListFlowsInFlowRequest means "get canvas/top-level flows". We want the flows in the
            # current context, which is the flow we're deleting.
            list_flows_request = ListFlowsInCurrentContextRequest()
            list_flows_result = self.engine.handle_request(list_flows_request)
            if not isinstance(list_flows_result, ListFlowsInCurrentContextResultSuccess):
                details = f"Attempted to delete Flow '{flow_name}', but failed while attempting to get the list of Flows owned by this Flow."
                result = DeleteFlowResultFailure(result_details=details)
                return result
            flow_names = list_flows_result.flow_names
            obj_mgr = self.engine.object_manager
            for child_flow_name in flow_names:
                child_flow = obj_mgr.attempt_get_object_by_name_as_type(child_flow_name, ControlFlow)
                if child_flow is None:
                    details = (
                        f"Attempted to delete Flow '{child_flow_name}', but no Flow with that name could be found."
                    )
                    result = DeleteFlowResultFailure(result_details=details)
                    return result
                with self.engine.context_manager.flow(flow=child_flow):
                    # Delete them.
                    delete_flow_request = DeleteFlowRequest(flow_name=child_flow_name)
                    delete_flow_result = self.engine.handle_request(delete_flow_request)
                    if isinstance(delete_flow_result, DeleteFlowResultFailure):
                        details = f"Attempted to delete Flow '{flow.name}', but failed while attempting to delete child Flow '{child_flow.name}'."
                        result = DeleteFlowResultFailure(result_details=details)
                        return result
            # Delete all child nodes in this Flow.
            list_nodes_request = ListNodesInFlowRequest()
            list_nodes_result = self.engine.handle_request(list_nodes_request)
            if not isinstance(list_nodes_result, ListNodesInFlowResultSuccess):
                details = f"Attempted to delete Flow '{flow.name}', but failed while attempting to get the list of Nodes owned by this Flow."
                result = DeleteFlowResultFailure(result_details=details)
                return result
            node_names = list_nodes_result.node_names
            for node_name in node_names:
                delete_node_request = DeleteNodeRequest(node_name=node_name)
                delete_node_result = self.engine.handle_request(delete_node_request)
                if isinstance(delete_node_result, DeleteNodeResultFailure):
                    details = f"Attempted to delete Flow '{flow.name}', but failed while attempting to delete child Node '{node_name}'."
                    result = DeleteFlowResultFailure(result_details=details)
                    return result

            # If we've made it this far, we have deleted all the children Flows and their nodes.
            # Remove the flow from our map.
            self.forget_empty_flow(flow)

            # Clean up ControlFlowMachine and DAG orchestrator only if this is the global flow.
            # Isolated subflows have their own machines and should not clear the global state.
            if (
                self._global_control_flow_machine is not None
                and self._global_control_flow_machine.context.flow_name == flow.name
            ):
                self._global_control_flow_machine = None
                self._global_dag_builder.clear()
                self._finish_run_if_over()

        details = f"Successfully deleted Flow '{flow_name}'."
        result = DeleteFlowResultSuccess(result_details=details)
        return result

    def forget_empty_flow(self, flow: ControlFlow) -> None:
        """Drop an empty flow from the records.

        The part of deleting a flow that a worker discarding its copy's flow shares. Not a delete:
        ``DeleteFlowRequest`` deletes the flow's nodes as well, which releases what they cached.
        """
        self.engine.object_manager.del_obj_by_name(flow.name)
        del self._name_to_parent_name[flow.name]

        # Clean up referenced workflow tracking
        if flow in self._flow_to_referenced_workflow_name:
            del self._flow_to_referenced_workflow_name[flow]

    @handles(GetIsFlowRunningRequest)
    def on_get_is_flow_running_request(self, request: GetIsFlowRunningRequest) -> ResultPayload:
        obj_mgr = self.engine.object_manager
        if request.flow_name is None:
            details = "Attempted to get Flow, but no flow name was provided."
            return GetIsFlowRunningResultFailure(result_details=details)
        flow = obj_mgr.attempt_get_object_by_name_as_type(request.flow_name, ControlFlow)
        if flow is None:
            details = f"Attempted to get Flow '{request.flow_name}', but no Flow with that name could be found."
            result = GetIsFlowRunningResultFailure(result_details=details)
            return result
        try:
            is_running = self.check_for_existing_running_flow()
        except Exception:
            details = f"Error while trying to get status of '{request.flow_name}'."
            result = GetIsFlowRunningResultFailure(result_details=details)
            return result
        return GetIsFlowRunningResultSuccess(
            is_running=is_running, result_details=f"Successfully checked if flow is running: {is_running}"
        )

    @handles(ListNodesInFlowRequest)
    def on_list_nodes_in_flow_request(self, request: ListNodesInFlowRequest) -> ResultPayload:
        flow_name = request.flow_name
        flow = None
        if flow_name is None:
            # First check if we have a current flow
            if not self.engine.context_manager.has_current_flow():
                details = "Attempted to list Nodes in a Flow in the Current Context. Failed because the Current Context was empty."
                result = ListNodesInFlowResultFailure(result_details=details)
                return result
            # Get the current flow from context
            flow = self.engine.context_manager.get_current_flow()
            flow_name = flow.name
        # Does this Flow even exist?
        if flow is None:
            obj_mgr = self.engine.object_manager
            flow = obj_mgr.attempt_get_object_by_name_as_type(flow_name, ControlFlow)
        if flow is None:
            details = (
                f"Attempted to list Nodes in Flow '{flow_name}'. Failed because no Flow with that name could be found."
            )
            result = ListNodesInFlowResultFailure(result_details=details)
            return result

        if request.node_types is not None:
            node_type_set = set(request.node_types)
            ret_list = [name for name, node in flow.nodes.items() if type(node).__name__ in node_type_set]
        else:
            ret_list = list(flow.nodes.keys())
        details = f"Successfully got the list of Nodes within Flow '{flow_name}'."

        result = ListNodesInFlowResultSuccess(node_names=ret_list, result_details=details)
        return result

    @handles(ListFlowsInFlowRequest)
    def on_list_flows_in_flow_request(self, request: ListFlowsInFlowRequest) -> ResultPayload:
        if request.parent_flow_name is not None:
            # Does this Flow even exist?
            obj_mgr = self.engine.object_manager
            flow = obj_mgr.attempt_get_object_by_name_as_type(request.parent_flow_name, ControlFlow)
            if flow is None:
                details = f"Attempted to list Flows that are children of Flow '{request.parent_flow_name}', but no Flow with that name could be found."
                result = ListFlowsInFlowResultFailure(result_details=details)
                return result

        # Create a list of all child flow names that point DIRECTLY to us.
        ret_list = []
        for flow_name, parent_name in self._name_to_parent_name.items():
            if parent_name == request.parent_flow_name:
                ret_list.append(flow_name)

        details = f"Successfully got the list of Flows that are direct children of Flow '{request.parent_flow_name}'."

        result = ListFlowsInFlowResultSuccess(flow_names=ret_list, result_details=details)
        return result

    def get_flow_by_name(self, flow_name: str) -> ControlFlow:
        obj_mgr = self.engine.object_manager
        flow = obj_mgr.attempt_get_object_by_name_as_type(flow_name, ControlFlow)
        if flow is None:
            msg = f"Flow with name {flow_name} doesn't exist"
            raise KeyError(msg)

        return flow

    def unresolve_all_nodes_in_flow(self, flow_name: str) -> None:
        """Reset all nodes in a flow to UNRESOLVED state.

        Args:
            flow_name: Name of the flow whose nodes should be unresolved
        """
        flow = self.get_flow_by_name(flow_name)
        for node in flow.nodes.values():
            node.state = NodeResolutionState.UNRESOLVED

    def handle_flow_rename(self, old_name: str, new_name: str) -> None:
        # Replace the old flow name and its parent first.
        parent = self._name_to_parent_name[old_name]
        self._name_to_parent_name[new_name] = parent
        del self._name_to_parent_name[old_name]

        # Now iterate through everyone who pointed to the old one as a parent and update it.
        for flow_name, parent_name in self._name_to_parent_name.items():
            if parent_name == old_name:
                self._name_to_parent_name[flow_name] = new_name

        # Let the Node Manager know about the change, too.
        self.engine.node_manager.handle_flow_rename(old_name=old_name, new_name=new_name)

    @handles(CreateConnectionRequest)
    def on_create_connection_request(self, request: CreateConnectionRequest) -> ResultPayload:  # noqa: PLR0911, PLR0912, PLR0915, C901
        # Vet the two nodes first.
        source_node_name = request.source_node_name
        source_node = None
        if source_node_name is None:
            # First check if we have a current node
            if not self.engine.context_manager.has_current_node():
                details = "Attempted to create a Connection with a source node from the Current Context. Failed because the Current Context was empty."
                return CreateConnectionResultFailure(result_details=details)

            # Get the current node from context
            source_node = self.engine.context_manager.get_current_node()
            source_node_name = source_node.name
        if source_node is None:
            try:
                source_node = self.engine.node_manager.get_node_by_name(source_node_name)
            except ValueError as err:
                details = f'Connection failed: "{source_node_name}" does not exist. Error: {err}.'

                return CreateConnectionResultFailure(result_details=details)

        target_node_name = request.target_node_name
        target_node = None
        if target_node_name is None:
            # First check if we have a current node
            if not self.engine.context_manager.has_current_node():
                details = "Attempted to create a Connection with the target node from the Current Context. Failed because the Current Context was empty."
                return CreateConnectionResultFailure(result_details=details)

            # Get the current node from context
            target_node = self.engine.context_manager.get_current_node()
            target_node_name = target_node.name
        if target_node is None:
            try:
                target_node = self.engine.node_manager.get_node_by_name(target_node_name)
            except ValueError as err:
                details = f'Connection failed: "{target_node_name}" does not exist. Error: {err}.'
                return CreateConnectionResultFailure(result_details=details)

        # The two nodes exist.
        # Get the parent flows.
        source_flow_name = None
        try:
            source_flow_name = self.engine.node_manager.get_node_parent_flow_by_name(source_node_name)
            self.get_flow_by_name(flow_name=source_flow_name)
        except KeyError as err:
            details = f'Connection "{source_node_name}.{request.source_parameter_name}" to "{target_node_name}.{request.target_parameter_name}" failed: {err}.'
            return CreateConnectionResultFailure(result_details=details)

        target_flow_name = None
        try:
            target_flow_name = self.engine.node_manager.get_node_parent_flow_by_name(target_node_name)
            self.get_flow_by_name(flow_name=target_flow_name)
        except KeyError as err:
            details = f'Connection "{source_node_name}.{request.source_parameter_name}" to "{target_node_name}.{request.target_parameter_name}" failed: {err}.'
            return CreateConnectionResultFailure(result_details=details)

        # Cross-flow connections are now supported via global connection storage

        # Call before_connection callbacks to allow nodes to prepare parameters
        source_node.before_outgoing_connection(
            source_parameter_name=request.source_parameter_name,
            target_node=target_node,
            target_parameter_name=request.target_parameter_name,
        )
        target_node.before_incoming_connection(
            source_node=source_node,
            source_parameter_name=request.source_parameter_name,
            target_parameter_name=request.target_parameter_name,
        )

        # Now validate the parameters.
        source_param = source_node.get_parameter_by_name(request.source_parameter_name)
        if source_param is None:
            details = f'Connection failed: "{source_node_name}.{request.source_parameter_name}" not found'
            return CreateConnectionResultFailure(result_details=details)

        target_param = target_node.get_parameter_by_name(request.target_parameter_name)
        if target_param is None:
            # TODO: https://github.com/griptape-ai/griptape-nodes/issues/860
            details = f'Connection failed: "{target_node_name}.{request.target_parameter_name}" not found'
            return CreateConnectionResultFailure(result_details=details)
        # Validate parameter modes accept this type of connection.
        source_modes_allowed = source_param.allowed_modes
        if ParameterMode.OUTPUT not in source_modes_allowed:
            details = (
                f'Connection failed: "{source_node_name}.{request.source_parameter_name}" is not an allowed OUTPUT'
            )
            return CreateConnectionResultFailure(result_details=details)

        target_modes_allowed = target_param.allowed_modes
        if ParameterMode.INPUT not in target_modes_allowed:
            details = f'Connection failed: "{target_node_name}.{request.target_parameter_name}" is not an allowed INPUT'
            return CreateConnectionResultFailure(result_details=details)

        # Validate that the data type from the source is allowed by the target.
        if not target_param.is_incoming_type_allowed(source_param.output_type):
            details = f'Connection failed on type mismatch "{source_node_name}.{request.source_parameter_name}" type({source_param.output_type}) to "{target_node_name}.{request.target_parameter_name}" types({target_param.input_types}) '
            return CreateConnectionResultFailure(result_details=details)

        # Ask each node involved to bless this union.
        if not source_node.allow_outgoing_connection(
            source_parameter=source_param,
            target_node=target_node,
            target_parameter=target_param,
        ):
            details = (
                f'Connection failed : "{source_node_name}.{request.source_parameter_name}" rejected the connection '
            )
            return CreateConnectionResultFailure(result_details=details)

        if not target_node.allow_incoming_connection(
            source_node=source_node,
            source_parameter=source_param,
            target_parameter=target_param,
        ):
            details = (
                f'Connection failed : "{target_node_name}.{request.target_parameter_name}" rejected the connection '
            )
            return CreateConnectionResultFailure(result_details=details)

        # Based on user feedback, if a connection already exists in a scenario where only ONE such connection can exist
        # (e.g., connecting to a data input that already has a connection, or from a control output that is already wired up),
        # delete the old connection and replace it with this one.
        old_source_node_name = None
        old_source_param_name = None
        old_target_node_name = None
        old_target_param_name = None
        replaced_connection = None
        replaced_details = ""

        # Some scenarios restrict when we can have more than one connection. See if we're in such a scenario and replace the
        # existing connection instead of adding a new one.
        connection_mgr = self._connections
        # Try the OUTGOING restricted scenario first.
        restricted_scenario_connection = connection_mgr.get_existing_connection_for_restricted_scenario(
            node=source_node, parameter=source_param, is_source=True
        )
        if not restricted_scenario_connection:
            # Check the INCOMING scenario.
            restricted_scenario_connection = connection_mgr.get_existing_connection_for_restricted_scenario(
                node=target_node, parameter=target_param, is_source=False
            )

        if restricted_scenario_connection:
            # Record the original data in case we need to back out of this.
            old_source_node_name = restricted_scenario_connection.source_node.name
            old_source_param_name = restricted_scenario_connection.source_parameter.name
            old_target_node_name = restricted_scenario_connection.target_node.name
            old_target_param_name = restricted_scenario_connection.target_parameter.name

            delete_old_request = DeleteConnectionRequest(
                source_node_name=old_source_node_name,
                source_parameter_name=old_source_param_name,
                target_node_name=old_target_node_name,
                target_parameter_name=old_target_param_name,
            )
            delete_old_result = self.engine.handle_request(delete_old_request)
            if delete_old_result.failed():
                details = f"Attempted to connect '{source_node_name}.{request.source_parameter_name}'. Failed because there was a previous connection from '{old_source_node_name}.{old_source_param_name}' to '{old_target_node_name}.{old_target_param_name}' that could not be deleted."
                return CreateConnectionResultFailure(result_details=details)

            is_same_connection = (
                old_source_node_name == source_node_name
                and old_source_param_name == request.source_parameter_name
                and old_target_node_name == target_node_name
                and old_target_param_name == request.target_parameter_name
            )
            if not is_same_connection:
                replaced_connection = ReplacedConnection(
                    source_node_name=old_source_node_name,
                    source_parameter_name=old_source_param_name,
                    target_node_name=old_target_node_name,
                    target_parameter_name=old_target_param_name,
                )
                replaced_details = f" Replaced the previous connection from '{old_source_node_name}.{old_source_param_name}' to '{old_target_node_name}.{old_target_param_name}', since only one connection is allowed there."
        try:
            # Actually create the Connection.
            # A connection between a group and a node inside it is internal. "Inside" is
            # transitive: with nested groups the node's own parent_group may be an inner group,
            # so we test containment across the whole nesting tree rather than the direct parent.
            if (isinstance(source_node, SubflowNodeGroup) and source_node.contains_node(target_node)) or (
                isinstance(target_node, SubflowNodeGroup) and target_node.contains_node(source_node)
            ):
                # Here we're checking if it's an internal connection. (from the NodeGroup to a node within it.)
                # If that's true, we set that automatically.
                is_node_group_internal = True
            else:
                # If not true, we default to the request
                is_node_group_internal = request.is_node_group_internal
            conn = self._connections.add_connection(
                source_node=source_node,
                source_parameter=source_param,
                target_node=target_node,
                target_parameter=target_param,
                is_node_group_internal=is_node_group_internal,
            )
            id(conn)
        except ValueError as e:
            details = f'Connection failed: "{e}"'

            # Attempt to restore any old connection that may have been present.
            if (
                (old_source_node_name is not None)
                and (old_source_param_name is not None)
                and (old_target_node_name is not None)
                and (old_target_param_name is not None)
            ):
                create_old_connection_request = CreateConnectionRequest(
                    source_node_name=old_source_node_name,
                    source_parameter_name=old_source_param_name,
                    target_node_name=old_target_node_name,
                    target_parameter_name=old_target_param_name,
                    initial_setup=request.initial_setup,
                )
                create_old_connection_result = self.engine.handle_request(create_old_connection_request)
                if create_old_connection_result.failed():
                    details = "Failed attempting to restore the old Connection after failing the replacement. A thousand pardons."
            return CreateConnectionResultFailure(result_details=details)

        # Let the source make any internal handling decisions now that the Connection has been made.
        with source_node._reporting_state_set_by("after_outgoing_connection"):
            source_node.after_outgoing_connection(
                source_parameter=source_param, target_node=target_node, target_parameter=target_param
            )

        # And target.
        with target_node._reporting_state_set_by("after_incoming_connection"):
            target_node.after_incoming_connection(
                source_node=source_node,
                source_parameter=source_param,
                target_parameter=target_param,
            )

        # Check if either node is in a NodeGroup and track connections

        source_parent = source_node.parent_group
        target_parent = target_node.parent_group

        # Only a connection that actually leaves the group needs a proxy parameter. Containment is
        # transitive, so a group must treat a node nested deeper inside it as internal: testing only
        # the direct parent made a group proxy connections that never crossed its boundary, and the
        # replacement connections re-entered here and were proxied again without end.
        # Each remap hands off one level outward (a child's group proxies to its own parent group),
        # so the walk terminates at the outermost group.
        crossed_source_group = None
        if (
            isinstance(source_parent, SubflowNodeGroup)
            and source_parent is not target_node
            and not source_parent.contains_node(target_node)
        ):
            crossed_source_group = source_parent

        crossed_target_group = None
        if (
            isinstance(target_parent, SubflowNodeGroup)
            and target_parent is not source_node
            and not target_parent.contains_node(source_node)
        ):
            crossed_target_group = target_parent

        # If source is in a group, this is an outgoing external connection
        if crossed_source_group is not None:
            success = crossed_source_group.map_external_connection(
                conn=conn,
                is_incoming=False,
            )
            if success:
                details = f'Connected "{source_node_name}.{request.source_parameter_name}" to "{target_node_name}.{request.target_parameter_name}, remapped with proxy parameter."{replaced_details}'
                return CreateConnectionResultSuccess(result_details=details, replaced_connection=replaced_connection)
            details = f'Failed to connect "{source_node_name}.{request.source_parameter_name}" to "{target_node_name}.{request.target_parameter_name} by remapping to proxy."'
            return CreateConnectionResultFailure(result_details=details)

        # If target is in a group, this is an incoming external connection
        if crossed_target_group is not None:
            success = crossed_target_group.map_external_connection(
                conn=conn,
                is_incoming=True,
            )
            if success:
                details = f'Connected "{source_node_name}.{request.source_parameter_name}" to "{target_node_name}.{request.target_parameter_name}, remapped with proxy parameter."{replaced_details}'
                return CreateConnectionResultSuccess(result_details=details, replaced_connection=replaced_connection)
            details = f'Failed to connect "{source_node_name}.{request.source_parameter_name}" to "{target_node_name}.{request.target_parameter_name} by remapping to proxy."'
            return CreateConnectionResultFailure(result_details=details)

        details = f'Connected "{source_node_name}.{request.source_parameter_name}" to "{target_node_name}.{request.target_parameter_name}".{replaced_details}'

        # Now update the parameter values if it exists.
        # check if it's been resolved/has a value in parameter_output_values
        if source_param.name in source_node.parameter_output_values:
            value = source_node.parameter_output_values[source_param.name]
        # if it doesn't let's use the one in parameter_values! that's the most updated.
        elif source_param.name in source_node.parameter_values:
            value = source_node._get_raw_parameter_value(source_param.name)
        # if not even that.. then does it have a default value?
        elif source_param.default_value:
            value = source_param.default_value
        else:
            value = None
            if isinstance(target_param, ParameterContainer):
                target_node.kill_parameter_children(target_param)
        # Set the parameter value (including None/empty values) unless we're in initial setup
        # Skip propagation for:
        # 1. Control Parameters as they should not receive values
        # 2. Locked nodes
        # 3. Initial Setup (this is used during deserialization; the downstream node may not be created yet)
        is_control_parameter = (
            ParameterType.attempt_get_builtin(source_param.output_type) == ParameterTypeBuiltin.CONTROL_TYPE
        )
        is_dest_node_locked = target_node.lock
        if (not is_control_parameter) and (not is_dest_node_locked) and (not request.initial_setup):
            # When creating a connection, pass the initial value from source to target parameter
            # Set incoming_connection_source fields to identify this as legitimate connection value passing
            # (not manual property setting) so it bypasses the INPUT+PROPERTY connection blocking logic
            self.engine.handle_request(
                SetParameterValueRequest(
                    parameter_name=target_param.name,
                    node_name=target_node.name,
                    value=value,
                    data_type=source_param.type,
                    incoming_connection_source_node_name=source_node.name,
                    incoming_connection_source_parameter_name=source_param.name,
                )
            )

        # Mark all downstream nodes as dirty when connection is created
        # This ensures dependency graph changes propagate even if values don't change
        # Matches the pattern used in connection deletion (https://github.com/griptape-ai/griptape-nodes/blob/9f299338193edc4a10f60728172c1ad214d1eb46/src/griptape_nodes/retained_mode/managers/flow_manager.py#L1227)
        if not is_control_parameter and not request.initial_setup:
            target_node.make_node_unresolved(
                current_states_to_trigger_change_event=set(
                    {NodeResolutionState.RESOLVED, NodeResolutionState.RESOLVING}
                )
            )
            self._connections.unresolve_future_nodes(target_node)

        # Check if either node is ErrorProxyNode and mark connection modification if not initial_setup
        if not request.initial_setup:
            if isinstance(source_node, ErrorProxyNode):
                source_node.set_post_init_connections_modified()
            if isinstance(target_node, ErrorProxyNode):
                target_node.set_post_init_connections_modified()

        result = CreateConnectionResultSuccess(result_details=details, replaced_connection=replaced_connection)

        return result

    @handles(DeleteConnectionRequest)
    def on_delete_connection_request(self, request: DeleteConnectionRequest) -> ResultPayload:  # noqa: C901, PLR0911, PLR0912, PLR0915 (complex logic, multiple edge cases)
        # Vet the two nodes first.
        source_node_name = request.source_node_name
        target_node_name = request.target_node_name
        source_node = None
        target_node = None
        if source_node_name is None:
            # First check if we have a current node
            if not self.engine.context_manager.has_current_node():
                details = "Attempted to delete a Connection with a source node from the Current Context. Failed because the Current Context was empty."
                return DeleteConnectionResultFailure(result_details=details)

            # Get the current node from context
            source_node = self.engine.context_manager.get_current_node()
            source_node_name = source_node.name
        if source_node is None:
            try:
                source_node = self.engine.node_manager.get_node_by_name(source_node_name)
            except ValueError as err:
                details = f'Connection not deleted "{source_node_name}.{request.source_parameter_name}" to "{target_node_name}.{request.target_parameter_name}". Error: {err}'

                return DeleteConnectionResultFailure(result_details=details)

        target_node_name = request.target_node_name
        if target_node_name is None:
            # First check if we have a current node
            if not self.engine.context_manager.has_current_node():
                details = "Attempted to delete a Connection with a target node from the Current Context. Failed because the Current Context was empty."
                return DeleteConnectionResultFailure(result_details=details)

            # Get the current node from context
            target_node = self.engine.context_manager.get_current_node()
            target_node_name = target_node.name
        if target_node is None:
            try:
                target_node = self.engine.node_manager.get_node_by_name(target_node_name)
            except ValueError as err:
                details = f'Connection not deleted "{source_node_name}.{request.source_parameter_name}" to "{target_node_name}.{request.target_parameter_name}". Error: {err}'

                return DeleteConnectionResultFailure(result_details=details)

        # The two nodes exist.
        # Get the parent flows.
        source_flow_name = None
        try:
            source_flow_name = self.engine.node_manager.get_node_parent_flow_by_name(source_node_name)
            self.get_flow_by_name(flow_name=source_flow_name)
        except KeyError as err:
            details = f'Connection not deleted "{source_node_name}.{request.source_parameter_name}" to "{target_node_name}.{request.target_parameter_name}". Error: {err}'

            return DeleteConnectionResultFailure(result_details=details)

        target_flow_name = None
        try:
            target_flow_name = self.engine.node_manager.get_node_parent_flow_by_name(target_node_name)
            self.get_flow_by_name(flow_name=target_flow_name)
        except KeyError as err:
            details = f'Connection not deleted "{source_node_name}.{request.source_parameter_name}" to "{target_node_name}.{request.target_parameter_name}". Error: {err}'

            return DeleteConnectionResultFailure(result_details=details)

        # Cross-flow connections are now supported via global connection storage

        # Now validate the parameters. A node can remove a connected parameter without a request, which
        # leaves the connection holding the only reference to it; that connection must still be deletable,
        # or neither node nor their flow can be deleted.
        stored_connection = self._connections.find_connection(
            source_node=source_node_name,
            source_parameter=request.source_parameter_name,
            target_node=target_node_name,
            target_parameter=request.target_parameter_name,
        )
        source_param = source_node.get_parameter_by_name(request.source_parameter_name)
        if source_param is None and stored_connection is not None:
            source_param = stored_connection.source_parameter
        if source_param is None:
            details = f'Connection not deleted "{source_node_name}.{request.source_parameter_name}" Not found.'

            return DeleteConnectionResultFailure(result_details=details)

        target_param = target_node.get_parameter_by_name(request.target_parameter_name)
        if target_param is None and stored_connection is not None:
            target_param = stored_connection.target_parameter
        if target_param is None:
            details = f'Connection not deleted "{target_node_name}.{request.target_parameter_name}" Not found.'

            return DeleteConnectionResultFailure(result_details=details)

        # Vet that a Connection actually exists between them already.
        if not self._has_connection(
            source_node=source_node,
            source_parameter=source_param,
            target_node=target_node,
            target_parameter=target_param,
        ):
            details = f'Connection does not exist: "{source_node_name}.{request.source_parameter_name}" to "{target_node_name}.{request.target_parameter_name}"'

            return DeleteConnectionResultFailure(result_details=details)

        # Remove the connection.
        if not self._connections.remove_connection(
            source_node=source_node.name,
            source_parameter=source_param.name,
            target_node=target_node.name,
            target_parameter=target_param.name,
        ):
            details = f'Connection not deleted "{source_node_name}.{request.source_parameter_name}" to "{target_node_name}.{request.target_parameter_name}". Unknown failure.'

            return DeleteConnectionResultFailure(result_details=details)

        # Let the source make any internal handling decisions before the Connection is REMOVED.
        source_node.before_outgoing_connection_removed(
            source_parameter=source_param, target_node=target_node, target_parameter=target_param
        )

        # And target.
        target_node.before_incoming_connection_removed(
            source_node=source_node,
            source_parameter=source_param,
            target_parameter=target_param,
        )

        # After the connection has been removed, if it doesn't have PROPERTY as a type, wipe the set parameter value and unresolve future nodes
        if ParameterMode.PROPERTY not in target_param.allowed_modes:
            if self._is_node_executing(target_node):
                # The node is mid-execution on this very value. Resetting it now would make the node
                # finish on its parameter default instead, and the run would report success with the
                # wrong answer; marking the node unresolved would be a claim the finishing run
                # immediately contradicts. Defer the reset until the node is done.
                target_node.reset_input_value_after_execution(target_param.name)
                # Consumers still need invalidating, and this pass only unresolves targets that are
                # already RESOLVED, so it cannot disturb another node mid-flight either.
                self._connections.unresolve_future_nodes(target_node)
            else:
                try:
                    # Only try to remove a value where one exists, otherwise it will generate errant warnings.
                    if target_param.name in target_node.parameter_values:
                        target_node.remove_parameter_value(target_param.name)
                    # It removed it accurately
                    # Unresolve future nodes that depended on that value
                    self._connections.unresolve_future_nodes(target_node)
                    target_node.make_node_unresolved(
                        current_states_to_trigger_change_event=set(
                            {NodeResolutionState.RESOLVED, NodeResolutionState.RESOLVING}
                        )
                    )
                except KeyError as e:
                    logger.warning(e)
        # Let the source make any internal handling decisions now that the Connection has been REMOVED.
        with source_node._reporting_state_set_by("after_outgoing_connection_removed"):
            source_node.after_outgoing_connection_removed(
                source_parameter=source_param, target_node=target_node, target_parameter=target_param
            )

        # And target.
        with target_node._reporting_state_set_by("after_incoming_connection_removed"):
            target_node.after_incoming_connection_removed(
                source_node=source_node,
                source_parameter=source_param,
                target_parameter=target_param,
            )

        details = f'Connection "{source_node_name}.{request.source_parameter_name}" to "{target_node_name}.{request.target_parameter_name}" deleted.'

        # Check if either node is ErrorProxyNode and mark connection modification (deletes are always user-initiated)
        if isinstance(source_node, ErrorProxyNode):
            source_node.set_post_init_connections_modified()
        if isinstance(target_node, ErrorProxyNode):
            target_node.set_post_init_connections_modified()

        result = DeleteConnectionResultSuccess(result_details=details)
        return result

    def _is_node_executing(self, node: BaseNode) -> bool:
        """Whether a run is currently inside this node's process method.

        Read from the live DAG rather than from `node.state`. The two are set in the same breath, but
        only the DAG entry is guaranteed to be dropped when a run tears down, so a run that ends
        badly cannot leave a node looking permanently busy and quietly change ordinary editing.

        Scope: the *global* DAG only. A node running inside an isolated subflow -- a group body, a
        ForEach iteration -- executes on that subflow's own `DagBuilder` (`control_flow.py`, the
        `is_isolated` branch), which is never registered anywhere: the machine that owns it is a local
        variable in `on_start_local_subflow_request`. So this reports False for a node that is in fact
        mid-process, and the caller then treats tearing its input connection down as ordinary editing
        and resets the value to the parameter default underneath it. Closing that needs a way to find
        the `DagBuilder` a node is running on; `NodeManager._find_entangled_live_node` is blind the
        same way, and cannot cancel a subflow run for the same reason.
        """
        dag_node = self._global_dag_builder.node_to_reference.get(node.name)
        return dag_node is not None and dag_node.node_state is NodeState.PROCESSING

    @handles(StartFlowRequest)
    async def on_start_flow_request(self, request: StartFlowRequest) -> ResultPayload:
        # which flow
        flow_name = request.flow_name
        if not flow_name:
            # Fall back to the current-context flow so callers do not have to echo the name
            # EnsureWorkflowAndFlowRequest / CreateFlowRequest just returned. If nothing is in
            # context either, preserve the original "you must provide one" error.
            if self.engine.context_manager.has_current_flow():
                flow_name = self.engine.context_manager.get_current_flow().name
            else:
                details = (
                    "Must provide flow_name, or set a flow in the Current Context "
                    "(e.g. via EnsureWorkflowAndFlowRequest or CreateFlowRequest) first."
                )
                return StartFlowResultFailure(validation_exceptions=[], result_details=details)
        return await self.start_run(StartFlowSpec(request=request, flow_name=flow_name))

    async def execute_start_flow(  # noqa: C901, PLR0911, PLR0912
        self, request: StartFlowRequest, flow_name: str, flow: ControlFlow
    ) -> ResultPayload:
        # Check to see if the flow is already running.
        if self.check_for_existing_running_flow():
            details = "Cannot start flow. Flow is already running."
            return StartFlowResultFailure(validation_exceptions=[], result_details=details)
        # A node has been provided to either start or to run up to.
        if request.flow_node_name:
            flow_node_name = request.flow_node_name
            flow_node = self.engine.object_manager.attempt_get_object_by_name_as_type(flow_node_name, BaseNode)
            if not flow_node:
                details = f"Provided node with name {flow_node_name} does not exist"
                return StartFlowResultFailure(validation_exceptions=[], result_details=details)
            # lets get the first control node in the flow!
            start_node = self.get_start_node_from_node(flow, flow_node)
            # if the start is not the node provided, set a breakpoint at the stop (we're running up until there)
            if not start_node:
                details = f"Start node for node with name {flow_node_name} does not exist"
                return StartFlowResultFailure(validation_exceptions=[], result_details=details)
            if start_node != flow_node:
                flow_node.stop_flow = True
        else:
            # we wont hit this if we dont have a request id, our requests always have nodes
            # If there is a request, reinitialize the queue
            self.get_start_node_queue()  # initialize the start flow queue!
            start_node = None
        # Run Validation before starting a flow
        result = await self.on_validate_flow_dependencies_request(
            ValidateFlowDependenciesRequest(flow_name=flow_name, flow_node_name=start_node.name if start_node else None)
        )
        try:
            if result.failed():
                details = f"Couldn't start flow with name {flow_name}. Flow Validation Failed"
                return StartFlowResultFailure(validation_exceptions=[], result_details=details)
            result = cast("ValidateFlowDependenciesResultSuccess", result)

            if not result.validation_succeeded:
                details = f"Couldn't start flow with name {flow_name}. Flow Validation Failed."
                if len(result.exceptions) > 0:
                    for exception in result.exceptions:
                        details = f"{details}\n\t{exception}"
                return StartFlowResultFailure(validation_exceptions=result.exceptions, result_details=details)
        except Exception as e:
            details = f"Couldn't start flow with name {flow_name}. Flow Validation Failed: {e}"
            return StartFlowResultFailure(validation_exceptions=[e], result_details=details)
        # By now, it has been validated with no exceptions.
        try:
            await self.start_flow(
                flow,
                start_node,
                debug_mode=request.debug_mode,
            )
        except Exception as e:
            details = f"Attempted to run flow '{flow_name}'. Failed due to: {e}"
            return StartFlowResultFailure(validation_exceptions=[e], result_details=details)

        if self._global_control_flow_machine:
            resolution_machine = self._global_control_flow_machine.resolution_machine
            if resolution_machine.is_errored():
                error_message = resolution_machine.get_error_message()
                result_details = f"Attempted to run flow '{flow_name}'. Failed due to: {error_message}"
                exception = RuntimeError(error_message)
                # Pass through the error message without adding extra wrapping
                return StartFlowResultFailure(
                    validation_exceptions=[exception] if error_message else [], result_details=result_details
                )

        details = f"Flow '{flow_name}' ran to completion."

        return StartFlowResultSuccess(result_details=details)

    @handles(StartFlowFromNodeRequest)
    async def on_start_flow_from_node_request(self, request: StartFlowFromNodeRequest) -> ResultPayload:
        # Resolve the node first, falling back to the current-context node when node_name is
        # omitted. flow_name on this request is deprecated; when not supplied we derive it
        # from the node's parent flow so callers do not have to keep them in sync.
        node_name = request.node_name
        if node_name is None:
            if self.engine.context_manager.has_current_node():
                node_name = self.engine.context_manager.get_current_node().name
            else:
                details = "Must provide node_name, or set a node in the Current Context first."
                return StartFlowFromNodeResultFailure(validation_exceptions=[], result_details=details)
        start_node = self.engine.object_manager.attempt_get_object_by_name_as_type(node_name, BaseNode)
        if not start_node:
            details = f"Provided node with name {node_name} does not exist"
            return StartFlowFromNodeResultFailure(validation_exceptions=[], result_details=details)
        # which flow
        flow_name = request.flow_name
        if not flow_name:
            try:
                flow_name = self.engine.node_manager.get_node_parent_flow_by_name(node_name)
            except KeyError as err:
                details = f"Could not derive parent flow for node '{node_name}': {err}"
                return StartFlowFromNodeResultFailure(validation_exceptions=[err], result_details=details)
        return await self.start_run(
            StartFlowFromNodeSpec(request=request, flow_name=flow_name, node_name=start_node.name)
        )

    async def execute_start_flow_from_node(  # noqa: PLR0911
        self, request: StartFlowFromNodeRequest, flow_name: str, flow: ControlFlow, start_node: BaseNode
    ) -> ResultPayload:
        # Check to see if the flow is already running.
        if self.check_for_existing_running_flow():
            details = "Cannot start flow. Flow is already running."
            return StartFlowFromNodeResultFailure(validation_exceptions=[], result_details=details)
        result = await self.on_validate_flow_dependencies_request(
            ValidateFlowDependenciesRequest(flow_name=flow_name, flow_node_name=start_node.name if start_node else None)
        )
        try:
            if result.failed():
                details = f"Couldn't start flow with name {flow_name}. Flow Validation Failed"
                return StartFlowFromNodeResultFailure(validation_exceptions=[], result_details=details)
            result = cast("ValidateFlowDependenciesResultSuccess", result)

            if not result.validation_succeeded:
                details = f"Couldn't start flow with name {flow_name}. Flow Validation Failed."
                if len(result.exceptions) > 0:
                    for exception in result.exceptions:
                        details = f"{details}\n\t{exception}"
                return StartFlowFromNodeResultFailure(validation_exceptions=result.exceptions, result_details=details)
        except Exception as e:
            details = f"Couldn't start flow with name {flow_name}. Flow Validation Failed: {e}"
            return StartFlowFromNodeResultFailure(validation_exceptions=[e], result_details=details)
        # By now, it has been validated with no exceptions.
        try:
            await self.start_flow(
                flow,
                start_node,
                debug_mode=request.debug_mode,
            )
        except Exception as e:
            details = f"Attempted to run flow '{flow_name}'. Failed due to: {e}"
            return StartFlowFromNodeResultFailure(validation_exceptions=[e], result_details=details)

        if self._global_control_flow_machine:
            resolution_machine = self._global_control_flow_machine.resolution_machine
            if resolution_machine.is_errored():
                error_message = resolution_machine.get_error_message()
                # Pass through the error message without adding extra wrapping
                return StartFlowFromNodeResultFailure(
                    validation_exceptions=[], result_details=error_message or "Flow execution failed"
                )

        details = f"Successfully kicked off flow with name {flow_name}"

        return StartFlowFromNodeResultSuccess(result_details=details)

    def get_start_nodes_in_flow(self, flow: ControlFlow) -> list[BaseNode]:  # noqa: C901, PLR0912, PLR0915
        """Find start nodes in a specific flow.

        A start node is defined as:
        1. An explicit StartNode instance, OR
        2. A control node with no incoming control connections, OR
        3. A data node with no outgoing connections

        Nodes that are children of SubflowNodeGroups are excluded.

        Args:
            flow: The flow to search for start nodes

        Returns:
            List of start nodes, prioritized as: StartNodes, control nodes, data nodes
        """
        connections = self.get_connections()
        all_nodes = list(flow.nodes.values())
        if not all_nodes:
            return []

        start_nodes = []
        control_nodes = []
        data_nodes = []

        for node in all_nodes:
            if isinstance(node, StartNode):
                start_nodes.append(node)
                continue

            has_control_param = False
            for parameter in node.parameters:
                if ParameterTypeBuiltin.CONTROL_TYPE.value == parameter.output_type:
                    incoming_control = (
                        node.name in connections.incoming_index
                        and parameter.name in connections.incoming_index[node.name]
                    )
                    outgoing_control = (
                        node.name in connections.outgoing_index
                        and parameter.name in connections.outgoing_index[node.name]
                    )
                    if incoming_control or outgoing_control:
                        has_control_param = True
                        break

            if not has_control_param:
                data_nodes.append(node)
                continue

            has_incoming_control = False
            if node.name in connections.incoming_index:
                for param_name in connections.incoming_index[node.name]:
                    param = node.get_parameter_by_name(param_name)
                    if param and ParameterTypeBuiltin.CONTROL_TYPE.value == param.output_type:
                        connection_ids = connections.incoming_index[node.name][param_name]
                        has_external_control_connection = False
                        for connection_id in connection_ids:
                            connection = connections.connections[connection_id]
                            # Skip internal NodeGroup connections
                            if connection.is_node_group_internal:
                                continue
                            if isinstance(node, BaseIterativeStartNode):
                                connected_node = connection.get_source_node()
                                if connected_node == node.end_node:
                                    continue
                            has_external_control_connection = True
                            break
                        if has_external_control_connection:
                            has_incoming_control = True
                            break

            if has_incoming_control:
                continue

            if node.name in connections.outgoing_index:
                for param_name in connections.outgoing_index[node.name]:
                    param = node.get_parameter_by_name(param_name)
                    if param and ParameterTypeBuiltin.CONTROL_TYPE.value == param.output_type:
                        control_nodes.append(node)
                        break
            else:
                control_nodes.append(node)

        valid_data_nodes = []
        for node in data_nodes:
            # Check if the node has any non-internal outgoing connections
            has_external_outgoing = False
            if node.name in connections.outgoing_index:
                for param_name in connections.outgoing_index[node.name]:
                    connection_ids = connections.outgoing_index[node.name][param_name]
                    for connection_id in connection_ids:
                        connection = connections.connections[connection_id]
                        # Skip internal NodeGroup connections
                        if connection.is_node_group_internal:
                            continue
                        has_external_outgoing = True
                        break
                    if has_external_outgoing:
                        break
            # Only add nodes that have no non-internal outgoing connections
            if not has_external_outgoing:
                valid_data_nodes.append(node)

        return start_nodes + control_nodes + valid_data_nodes

    def _validate_and_get_start_node(
        self, flow_name: str, start_node_name: str | None, flow: ControlFlow
    ) -> BaseNode | StartLocalSubflowResultFailure:
        """Validate and get the start node for subflow execution."""
        if start_node_name is None:
            start_nodes = self.get_start_nodes_in_flow(flow)
            if not start_nodes:
                details = f"Cannot start subflow '{flow_name}'. No start nodes found in flow."
                return StartLocalSubflowResultFailure(result_details=details)
            return start_nodes[0]

        try:
            return self.engine.node_manager.get_node_by_name(start_node_name)
        except ValueError as err:
            details = f"Cannot start subflow '{flow_name}'. Start node '{start_node_name}' not found: {err}"
            return StartLocalSubflowResultFailure(result_details=details)

    @handles(StartLocalSubflowRequest)
    async def on_start_local_subflow_request(self, request: StartLocalSubflowRequest) -> ResultPayload:  # noqa: C901, PLR0911
        flow_name = request.flow_name
        if not flow_name:
            details = "Must provide flow name to start a flow."
            return StartFlowResultFailure(validation_exceptions=[], result_details=details)

        try:
            flow = self.get_flow_by_name(flow_name)
        except KeyError as err:
            details = f"Cannot start flow. Error: {err}"
            return StartFlowFromNodeResultFailure(validation_exceptions=[err], result_details=details)

        if not self.check_for_existing_running_flow():
            msg = "There must be a flow going to start a Subflow"
            return StartLocalSubflowResultFailure(result_details=msg)

        start_node = self._validate_and_get_start_node(flow_name, request.start_node, flow)
        if isinstance(start_node, StartLocalSubflowResultFailure):
            return start_node

        # Run validation before starting the subflow
        validation_result = await self.on_validate_flow_dependencies_request(
            ValidateFlowDependenciesRequest(flow_name=flow_name, flow_node_name=start_node.name if start_node else None)
        )
        if validation_result.failed():
            # Extract error details from the failed validation result
            details = (
                validation_result.result_details
                if hasattr(validation_result, "result_details")
                else f"Subflow '{flow_name}' validation failed"
            )
            return StartLocalSubflowResultFailure(result_details=details)

        validation_result = cast("ValidateFlowDependenciesResultSuccess", validation_result)
        if not validation_result.validation_succeeded:
            # Build detailed error message with all validation exceptions
            details_lines = []
            if validation_result.exceptions:
                for exception in validation_result.exceptions:
                    details_lines.append(str(exception))  # noqa: PERF401 keeping in for loop for clarity.
            details = "\n".join(details_lines) if details_lines else f"Subflow '{flow_name}' validation failed"
            return StartLocalSubflowResultFailure(result_details=details)

        subflow_machine = ControlFlowMachine(
            flow.name,
            is_isolated=True,
            engine=self.engine,
        )

        try:
            await subflow_machine.start_flow(start_node)
        except asyncio.CancelledError:
            # The caller (e.g. a ForEach iteration) was cancelled. This isolated machine spawns its
            # own asyncio tasks for the body nodes; cancelling the awaiting coroutine does NOT cancel
            # those tasks, so without this they would run detached to completion and then touch a
            # flow the loop's cleanup has already deleted ("no Flow with that name exists"). Cancel
            # and await this machine's own tasks before unwinding, then re-raise so cancellation
            # still propagates to the caller.
            await subflow_machine.cancel_flow()
            raise
        except Exception as err:
            msg = f"Failed to run flow {flow_name}. Error: {err}"
            return StartLocalSubflowResultFailure(result_details=msg)

        if subflow_machine.resolution_machine.is_errored():
            error_message = subflow_machine.resolution_machine.get_error_message()
            # Pass through the error message directly without wrapping
            return StartLocalSubflowResultFailure(result_details=error_message or "Subflow errored during execution")

        return StartLocalSubflowResultSuccess(result_details=f"Successfully executed local subflow '{flow_name}'")

    @handles(GetFlowStateRequest)
    def on_get_flow_state_request(self, event: GetFlowStateRequest) -> ResultPayload:
        flow_name = event.flow_name
        if not flow_name:
            details = "Could not get flow state. No flow name was provided."
            return GetFlowStateResultFailure(result_details=details)
        try:
            flow = self.get_flow_by_name(flow_name)
        except KeyError as err:
            details = f"Could not get flow state. Error: {err}"
            return GetFlowStateResultFailure(result_details=details)
        try:
            control_nodes, resolving_nodes, involved_nodes = self.flow_state(flow)
        except Exception as e:
            details = f"Failed to get flow state of flow with name {flow_name}. Exception occurred: {e} "
            logger.exception(details)
            return GetFlowStateResultFailure(result_details=details)
        details = f"Successfully got flow state for flow with name {flow_name}."
        return GetFlowStateResultSuccess(
            control_nodes=control_nodes,
            resolving_nodes=resolving_nodes,
            involved_nodes=involved_nodes,
            result_details=details,
        )

    @handles(CancelFlowRequest)
    async def on_cancel_flow_request(self, request: CancelFlowRequest) -> ResultPayload:
        flow_name = request.flow_name
        if not flow_name:
            details = "Could not cancel flow execution. No flow name was provided."

            return CancelFlowResultFailure(result_details=details)
        try:
            self.get_flow_by_name(flow_name)
        except KeyError as err:
            details = f"Could not cancel flow execution. Error: {err}"

            return CancelFlowResultFailure(result_details=details)
        run = self._run_in_progress
        try:
            if run is not None:
                await self.cancel_run(run.run_id)
            else:
                await self.cancel_flow_run()
        except Exception as e:
            details = f"Could not cancel flow execution. Exception: {e}"

            return CancelFlowResultFailure(result_details=details)
        details = f"Successfully cancelled flow execution with name {flow_name}"

        return CancelFlowResultSuccess(result_details=details)

    @handles(CancelRunRequest)
    async def on_cancel_run_request(self, request: CancelRunRequest) -> ResultPayload:
        cancelled = await self.cancel_run(request.run_id)
        if not cancelled:
            details = f"Attempted to cancel run '{request.run_id}'. Failed because no run with that id is in progress or waiting."
            return CancelRunResultFailure(result_details=details)
        return CancelRunResultSuccess(result_details=f"Cancelled run '{request.run_id}'.")

    @handles(CancelAllRunsRequest)
    async def on_cancel_all_runs_request(self, _request: CancelAllRunsRequest) -> ResultPayload:
        try:
            await self.cancel_all_runs()
        except Exception as e:
            # Cancelling the run in progress awaits its nodes' own cancellation, which can raise anything.
            details = f"Attempted to cancel every run. Failed because the run in progress could not be cancelled: {e}"
            return CancelAllRunsResultFailure(result_details=details)
        return CancelAllRunsResultSuccess(result_details="Cancelled every run.")

    @handles(ListRunsRequest)
    def on_list_runs_request(self, _request: ListRunsRequest) -> ResultPayload:
        runs: list[RunInfo] = []
        in_progress = self._run_in_progress
        if in_progress is not None:
            runs.append(
                RunInfo(
                    run_id=in_progress.run_id,
                    flow_name=in_progress.flow_name,
                    state=in_progress.state,
                    position=0,
                    request_id=in_progress.request_id,
                )
            )
        for index, waiting in enumerate(self._run_queue.snapshot()):
            runs.append(
                RunInfo(
                    run_id=waiting.run_id,
                    flow_name=waiting.flow_name,
                    state=waiting.state,
                    position=index + 1,
                    request_id=waiting.request_id,
                )
            )
        return ListRunsResultSuccess(runs=runs, result_details=f"Listed {len(runs)} run(s).")

    @handles(SingleNodeStepRequest)
    async def on_single_node_step_request(self, request: SingleNodeStepRequest) -> ResultPayload:
        flow_name = request.flow_name
        if not flow_name:
            details = "Could not advance to the next step of a running workflow. No flow name was provided."

            return SingleNodeStepResultFailure(validation_exceptions=[], result_details=details)
        try:
            self.get_flow_by_name(flow_name)
        except KeyError as err:
            details = f"Could not advance to the next step of a running workflow. No flow with name {flow_name} exists. Error: {err}"

            return SingleNodeStepResultFailure(validation_exceptions=[err], result_details=details)
        try:
            flow = self.get_flow_by_name(flow_name)
            with self._continuing_run_in_progress():
                await self.single_node_step(flow)
        except Exception as e:
            details = f"Could not advance to the next step of a running workflow. Exception: {e}"
            return SingleNodeStepResultFailure(validation_exceptions=[], result_details=details)

        # All completed happily
        details = f"Successfully advanced to the next step of a running workflow with name {flow_name}"

        return SingleNodeStepResultSuccess(result_details=details)

    @handles(SingleExecutionStepRequest)
    async def on_single_execution_step_request(self, request: SingleExecutionStepRequest) -> ResultPayload:
        flow_name = request.flow_name
        if not flow_name:
            details = "Could not advance to the next step of a running workflow. No flow name was provided."

            return SingleExecutionStepResultFailure(result_details=details)
        try:
            flow = self.get_flow_by_name(flow_name)
        except KeyError as err:
            details = f"Could not advance to the next step of a running workflow. Error: {err}."

            return SingleExecutionStepResultFailure(result_details=details)
        change_debug_mode = request.request_id is not None
        try:
            with self._continuing_run_in_progress():
                await self.single_execution_step(flow, change_debug_mode)
        except Exception as e:
            # We REALLY don't want to fail here, else we'll take the whole engine down
            try:
                if self.check_for_existing_running_flow():
                    await self.cancel_flow_run()
            except Exception as e_inner:
                details = f"Could not cancel flow execution. Exception: {e_inner}"

            details = f"Could not advance to the next step of a running workflow. Exception: {e}"
            return SingleNodeStepResultFailure(validation_exceptions=[e], result_details=details)
        details = f"Successfully advanced to the next step of a running workflow with name {flow_name}"

        return SingleExecutionStepResultSuccess(result_details=details)

    @handles(ContinueExecutionStepRequest)
    async def on_continue_execution_step_request(self, request: ContinueExecutionStepRequest) -> ResultPayload:
        flow_name = request.flow_name
        if not flow_name:
            details = "Failed to continue execution step because no flow name was provided"

            return ContinueExecutionStepResultFailure(result_details=details)
        try:
            flow = self.get_flow_by_name(flow_name)
        except KeyError as err:
            details = f"Failed to continue execution step. Error: {err}"

            return ContinueExecutionStepResultFailure(result_details=details)
        try:
            with self._continuing_run_in_progress():
                await self.continue_executing(flow)
        except Exception as e:
            details = f"Failed to continue execution step. An exception occurred: {e}."
            return ContinueExecutionStepResultFailure(result_details=details)
        details = f"Successfully continued flow with name {flow_name}"
        return ContinueExecutionStepResultSuccess(result_details=details)

    @handles(UnresolveFlowRequest)
    def on_unresolve_flow_request(self, request: UnresolveFlowRequest) -> ResultPayload:
        flow_name = request.flow_name
        if not flow_name:
            details = "Failed to unresolve flow because no flow name was provided"
            return UnresolveFlowResultFailure(result_details=details)
        try:
            flow = self.get_flow_by_name(flow_name)
        except KeyError as err:
            details = f"Failed to unresolve flow. Error: {err}"
            return UnresolveFlowResultFailure(result_details=details)
        try:
            self.unresolve_whole_flow(flow)
        except Exception as e:
            details = f"Failed to unresolve flow. An exception occurred: {e}."
            return UnresolveFlowResultFailure(result_details=details)
        details = f"Unresolved flow with name {flow_name}"
        return UnresolveFlowResultSuccess(result_details=details)

    @handles(ValidateFlowDependenciesRequest)
    async def on_validate_flow_dependencies_request(self, request: ValidateFlowDependenciesRequest) -> ResultPayload:
        flow_name = request.flow_name
        # get the flow name
        try:
            flow = self.get_flow_by_name(flow_name)
        except KeyError as err:
            details = f"Failed to validate flow. Error: {err}"
            return ValidateFlowDependenciesResultFailure(result_details=details)
        if request.flow_node_name:
            flow_node_name = request.flow_node_name
            flow_node = self.engine.object_manager.attempt_get_object_by_name_as_type(flow_node_name, BaseNode)
            if not flow_node:
                details = f"Provided node with name {flow_node_name} does not exist"
                return ValidateFlowDependenciesResultFailure(result_details=details)
            # Gets all nodes in that connected group to be ran
            nodes = flow.get_all_connected_nodes(flow_node)
        else:
            nodes = flow.nodes.values()
        # If we're just running the whole flow
        all_exceptions = []
        for node in nodes:
            exceptions = node.validate_before_workflow_run()
            if exceptions:
                all_exceptions = all_exceptions + exceptions
        return ValidateFlowDependenciesResultSuccess(
            validation_succeeded=len(all_exceptions) == 0,
            exceptions=all_exceptions,
            result_details=f"Validated flow dependencies: {len(all_exceptions)} exceptions found",
        )

    @handles(ListFlowsInCurrentContextRequest)
    def on_list_flows_in_current_context_request(self, request: ListFlowsInCurrentContextRequest) -> ResultPayload:  # noqa: ARG002 (request isn't actually used)
        if not self.engine.context_manager.has_current_flow():
            details = "Attempted to list Flows in the Current Context. Failed because the Current Context was empty."
            return ListFlowsInCurrentContextResultFailure(result_details=details)

        parent_flow = self.engine.context_manager.get_current_flow()
        parent_flow_name = parent_flow.name

        # Create a list of all child flow names that point DIRECTLY to us.
        ret_list = []
        for flow_name, parent_name in self._name_to_parent_name.items():
            if parent_name == parent_flow_name:
                ret_list.append(flow_name)

        details = f"Successfully got the list of Flows in the Current Context (Flow '{parent_flow_name}')."

        return ListFlowsInCurrentContextResultSuccess(flow_names=ret_list, result_details=details)

    @handles(AutoLayoutFlowRequest)
    async def on_auto_layout_flow_request(self, request: AutoLayoutFlowRequest) -> ResultPayload:  # noqa: C901, PLR0912
        """Assign editor positions to every node in a flow by topological column layout.

        Algorithm (Kahn-ish): walk the data+control edges internal to this flow, compute a
        layer per node as the longest path from any source (node with no incoming edges),
        then place layers as columns (x = origin_x + layer * layer_spacing) and siblings
        within a layer as rows (y = origin_y + row_index * row_spacing). Writes
        `node.metadata['position'] = {'x', 'y'}` for each node, preserving other metadata
        keys.

        Cycles should not occur in a well-formed flow. If one is detected, nodes that
        couldn't be topologically ordered are parked at layer 0 and a warning is logged.
        """
        flow_name = request.flow_name
        if not flow_name:
            if self.engine.context_manager.has_current_flow():
                flow_name = self.engine.context_manager.get_current_flow().name
            else:
                details = (
                    "Attempted to auto-layout a flow. Failed because no flow_name was provided "
                    "and the Current Context has no flow."
                )
                return AutoLayoutFlowResultFailure(result_details=details)

        try:
            flow = self.get_flow_by_name(flow_name)
        except KeyError as err:
            details = f"Attempted to auto-layout flow '{flow_name}'. Failed because: {err}"
            return AutoLayoutFlowResultFailure(result_details=details)

        node_names = list(flow.nodes.keys())
        if not node_names:
            return AutoLayoutFlowResultSuccess(
                flow_name=flow_name,
                positioned_nodes=[],
                result_details=f"Flow '{flow_name}' has no nodes; nothing to lay out.",
            )

        # Build directed edges restricted to this flow so sub-flows and other workflow state
        # do not contaminate the topological ordering.
        node_name_set = set(node_names)
        incoming: dict[str, set[str]] = {name: set() for name in node_names}
        outgoing: dict[str, set[str]] = {name: set() for name in node_names}
        for connection in self._connections.connections.values():
            src = connection.source_node.name
            tgt = connection.target_node.name
            if src in node_name_set and tgt in node_name_set and src != tgt:
                outgoing[src].add(tgt)
                incoming[tgt].add(src)

        # Kahn's topological sort with layer assignment. `layer[n]` is the longest path from
        # any source to n, which gives a column that lines up fan-out naturally.
        layer: dict[str, int] = dict.fromkeys(node_names, 0)
        in_degree = {name: len(incoming[name]) for name in node_names}
        ready = [name for name in node_names if in_degree[name] == 0]
        ready.sort()  # stable layer ordering across runs
        visited = 0
        while ready:
            name = ready.pop(0)
            visited += 1
            for child in sorted(outgoing[name]):
                layer[child] = max(layer[child], layer[name] + 1)
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    ready.append(child)

        if visited < len(node_names):
            logger.warning(
                "Auto-layout detected a cycle in flow '%s'; %d node(s) could not be ordered and stay at layer 0.",
                flow_name,
                len(node_names) - visited,
            )

        layers: dict[int, list[str]] = {}
        for name in node_names:
            layers.setdefault(layer[name], []).append(name)

        positioned: list[NodePosition] = []
        for layer_idx in sorted(layers.keys()):
            for row_idx, name in enumerate(sorted(layers[layer_idx])):
                x = request.origin_x + layer_idx * request.layer_spacing
                y = request.origin_y + row_idx * request.row_spacing
                # Dispatch the position update through self.engine.ahandle_request so the
                # per-node SetNodeMetadataResultSuccess event is broadcast to subscribers.
                # The editor listens on that event to move nodes on the canvas live; mutating
                # node.metadata directly here would update the engine state but leave the UI
                # stale until a reload.
                set_metadata_result = await self.engine.ahandle_request(
                    SetNodeMetadataRequest(node_name=name, metadata={"position": {"x": x, "y": y}})
                )
                if not isinstance(set_metadata_result, SetNodeMetadataResultSuccess):
                    logger.warning(
                        "Auto-layout failed to update position for node '%s' in flow '%s': %s",
                        name,
                        flow_name,
                        set_metadata_result.result_details,
                    )
                    continue
                positioned.append(NodePosition(node_name=name, x=x, y=y))

        details = f"Laid out {len(positioned)} node(s) in flow '{flow_name}' across {len(layers)} layer(s)."
        return AutoLayoutFlowResultSuccess(
            flow_name=flow_name,
            positioned_nodes=positioned,
            result_details=details,
        )

    async def start_flow(
        self,
        flow: ControlFlow,
        start_node: BaseNode | None = None,
        *,
        debug_mode: bool = False,
    ) -> None:
        if self.check_for_existing_running_flow():
            # If flow already exists, throw an error
            errormsg = "This workflow is already in progress. Please wait for the current process to finish before starting again."
            raise RuntimeError(errormsg)
        self._raise_if_run_cancelled()

        if start_node is None:
            if self._global_flow_queue.empty():
                errormsg = "No Flow exists. You must create at least one control connection."
                raise RuntimeError(errormsg)
            queue_item = self._global_flow_queue.get()
            start_node = queue_item.node
            self._global_flow_queue.task_done()

        # Initialize global control flow machine and DAG builder

        self._global_control_flow_machine = ControlFlowMachine(flow.name, engine=self.engine)
        # Set off the request here.
        try:
            await self._global_control_flow_machine.start_flow(start_node, debug_mode=debug_mode)
        except Exception:
            await self._abandon_running_flow()
            raise
        self.engine.event_manager.put_event(
            ExecutionGriptapeNodeEvent(wrapped_event=ExecutionEvent(payload=InvolvedNodesEvent(involved_nodes=[])))
        )

    async def start_run(self, spec: RunSpec) -> ResultPayload:
        """Run `spec` once the runs ahead of it end, returning what its request returns.

        A request made from inside the run in progress belongs to that run, so it executes at once:
        queueing it would wait on itself.
        """
        if self._is_inside_run_in_progress():
            return await self.join_run_in_progress(spec)
        run = self.submit_run(spec)
        try:
            return await asyncio.shield(run.outcome)
        except asyncio.CancelledError:
            # Nothing will wait for this run any more, so if it has not started it never should.
            self._turn_away([run], "its request was cancelled.")
            raise

    async def join_run_in_progress(self, spec: RunSpec) -> ResultPayload:
        """Execute `spec` at once as part of the run in progress, returning what its request returns."""
        run_id = None
        if self._run_in_progress is not None:
            run_id = self._run_in_progress.run_id
        return self._of_run(await spec.execute(self.engine), run_id)

    def submit_run(self, spec: RunSpec) -> Run:
        """Queue a run of `spec`, starting it at once if no run is in progress."""
        run = Run(
            run_id=str(uuid4()),
            spec=spec,
            flow_name=spec.flow_name,
            queued_at=datetime.now(tz=UTC),
            state=RunState.QUEUED,
            outcome=asyncio.get_running_loop().create_future(),
            context=copy_context(),
            request_id=current_request_id(),
        )
        self._run_queue.enqueue(run)
        self._start_next_run_if_idle()
        self._report_places_in_line()
        return run

    async def cancel_run(self, run_id: str) -> bool:
        """Cancel a run, returning whether there was one: a waiting run leaves the line, the run in progress stops."""
        waiting = [run for run in self._run_queue.snapshot() if run.run_id == run_id]
        if waiting:
            self._turn_away(waiting, "it was cancelled.")
            return True
        run = self._run_in_progress
        if run is None or run.run_id != run_id:
            return False
        run.state = RunState.CANCELLED
        # A run whose flow has not started stops itself before starting it.
        if self.check_for_existing_running_flow():
            await self.cancel_flow_run()
        return True

    async def cancel_all_runs(self) -> None:
        """Cancel every waiting run, then the run in progress, without starting another."""
        # The waiting runs go first: cancelling the run in progress would otherwise start the next one.
        self._turn_away(self._run_queue.snapshot(), "it was cancelled.")
        if self._run_in_progress is not None:
            self._run_in_progress.state = RunState.CANCELLED
        self._cancelling_all = True
        try:
            if self.check_for_existing_running_flow():
                await self.cancel_flow_run()
        finally:
            self._cancelling_all = False
        # Runs submitted while the cancel awaited were held, so they go too.
        self._turn_away(self._run_queue.snapshot(), "it was cancelled.")

    def _turn_away(self, runs: list[Run], reason: str) -> None:
        """Take waiting runs out of the line without executing them; each one's request returns `reason`."""
        for run in runs:
            if self._run_queue.remove(run.run_id) is None:
                continue
            run.state = RunState.CANCELLED
            self._leave_line(run)
            failure = self._of_run(run.spec.failure(f"The run did not start: {reason}"), run.run_id)
            self._on_run_loop(run, run.outcome.set_result, failure)
        self._report_places_in_line()

    def _raise_if_run_cancelled(self) -> None:
        """Stop a run cancelled before its flow started, just before it would start it.

        Raises:
            RuntimeError: The run in progress was cancelled.
        """
        run = self._run_in_progress
        if run is not None and run.state is RunState.CANCELLED:
            raise RuntimeError(_CANCELLED_BEFORE_STARTING)

    @staticmethod
    def _of_run(result: ResultPayload, run_id: str | None) -> ResultPayload:
        """`result`, naming the run its request started or joined."""
        if isinstance(result, RunIdMixin):
            result.run_id = run_id
        return result

    def _start_next_run_if_idle(self) -> None:
        if self._run_in_progress is not None or self._cancelling_all:
            return
        run = self._run_queue.pop_next()
        if run is None:
            return
        self._leave_line(run)
        run.state = RunState.RUNNING
        self._run_in_progress = run
        self._driving = True
        # The run that just ended may have ended on a short-lived loop, such as a synchronous request's.
        self._on_run_loop(run, self._create_driver, run)

    def _create_driver(self, run: Run) -> None:
        asyncio.get_running_loop().create_task(self._drive(run), context=run.context)

    async def _drive(self, run: Run) -> None:
        # run.context is the submitter's context copied, so this marks only the run's own tasks.
        _current_run_id.set(run.run_id)
        if run.state is RunState.CANCELLED:
            run.outcome.set_result(self._of_run(run.spec.failure(_CANCELLED_BEFORE_STARTING), run.run_id))
            self._driving = False
            self._finish_run(run)
            return
        try:
            result = await run.spec.execute(self.engine)
        except asyncio.CancelledError:
            run.outcome.cancel()
            self._driving = False
            self._finish_run(run)
            raise
        except Exception as e:
            # The request awaiting the outcome reports the error, so the driver does not raise it again.
            run.outcome.set_exception(e)
            run.state = RunState.FAILED
            self._driving = False
            self._finish_run(run)
            return
        run.outcome.set_result(self._of_run(result, run.run_id))
        self._driving = False
        if self.check_for_existing_running_flow():
            run.state = RunState.PAUSED
            return
        if run.state is RunState.RUNNING:
            if result.succeeded():
                run.state = RunState.FINISHED
            else:
                run.state = RunState.FAILED
        self._finish_run(run)

    def _finish_run_if_over(self) -> None:
        """End the run in progress if its flow has stopped, on the paths that can stop a paused run.

        Wait while anything still holds the machines. For example, in debug mode the user steps a
        node, presses Cancel while its check is still running, then presses Run: without this wait,
        the new run starts before the old step has finished. `_driving` only covers a normal Run;
        `execution_is_advancing()` also covers a step or continue. Each of those calls this again
        when it finishes, so the run still ends.
        """
        run = self._run_in_progress
        if run is None or self._driving or self.execution_is_advancing() or self.check_for_existing_running_flow():
            return
        if run.state is RunState.PAUSED:
            run.state = RunState.FINISHED
        self._finish_run(run)

    def _finish_run(self, run: Run) -> None:
        """End `run`, the run in progress, and start the next. The only place a run ends."""
        if self._run_in_progress is not run:
            return
        self._run_in_progress = None
        self._start_next_run_if_idle()
        self._report_places_in_line()

    @staticmethod
    def _on_run_loop(run: Run, callback: Callable[..., object], *args: object) -> None:
        """Call `callback` on the loop the run was submitted on, which owns its outcome."""
        loop = run.outcome.get_loop()
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            callback(*args)
            return
        loop.call_soon_threadsafe(callback, *args)

    def _is_inside_run_in_progress(self) -> bool:
        run = self._run_in_progress
        return run is not None and _current_run_id.get() == run.run_id

    def _leave_line(self, run: Run) -> None:
        if run.position:
            run.position = 0
            self._report_place_in_line(run)

    def _report_places_in_line(self) -> None:
        for index, run in enumerate(self._run_queue.snapshot()):
            position = index + 1
            if run.position != position:
                run.position = position
                self._report_place_in_line(run)

    def _report_place_in_line(self, run: Run) -> None:
        self.engine.event_manager.put_event(
            ExecutionGriptapeNodeEvent(
                wrapped_event=ExecutionEvent(
                    payload=RunQueuedEvent(
                        run_id=run.run_id,
                        flow_name=run.flow_name,
                        position=run.position,
                        request_id=run.request_id,
                    )
                )
            )
        )

    @contextlib.contextmanager
    def _continuing_run_in_progress(self) -> Iterator[None]:
        """Count what a step or continue request executes as part of the run in progress."""
        run = self._run_in_progress
        run_id = None
        if run is not None:
            run_id = run.run_id
        token = _current_run_id.set(run_id)
        try:
            yield
        finally:
            _current_run_id.reset(token)
            self._finish_run_if_over()

    def check_for_existing_running_flow(self) -> bool:
        if self._global_control_flow_machine is None:
            return False
        current_state = self._global_control_flow_machine.current_state
        if current_state and current_state is not CompleteState:
            # Flow already exists in progress
            return True
        return bool(
            not self._global_control_flow_machine.context.resolution_machine.is_complete()
            and self._global_control_flow_machine.context.resolution_machine.is_started()
        )

    def running_flow_is_stopping_after_failure(self) -> bool:
        """True while the running flow has failed and is waiting for its other nodes to finish."""
        if self._global_control_flow_machine is None:
            return False
        return self._global_control_flow_machine.context.resolution_machine.is_stopping_after_failure()

    def execution_is_advancing(self) -> bool:
        """True while a coroutine is already driving the running flow's machines.

        A continuously running flow drives itself, so a step or continue request
        arriving mid-drive has nothing to do, and driving anyway would make it a
        second driver of the same machine. A driver parked on a running node is
        still advancing even once the paused flag is set -- it observes the flag
        and stops when that node finishes -- which is why callers apply their
        debug-mode change before consulting this.
        """
        if self._global_control_flow_machine is None:
            return False
        return (
            self._global_control_flow_machine.is_advancing
            or self._global_control_flow_machine.resolution_machine.is_advancing
        )

    async def cancel_flow_run(self) -> None:
        if not self.check_for_existing_running_flow():
            errormsg = "Flow has not yet been started. Cannot cancel flow that hasn't begun."
            raise RuntimeError(errormsg)
        self._global_flow_queue.queue.clear()

        # Request cancellation on all nodes and wait for them to complete
        run_seconds = None
        if self._global_control_flow_machine is not None:
            await self._global_control_flow_machine.cancel_flow()
            # Read before the reset below clears the run's start time.
            run_seconds = self._global_control_flow_machine.context.seconds_since_run_started()

        # Reset control flow machine
        if self._global_control_flow_machine is not None:
            self._global_control_flow_machine.reset_machine(cancel=True)
        self._global_single_node_resolution = False
        self._global_dag_builder.clear()
        logger.debug("Cancelling flow run")

        self.engine.event_manager.put_event(
            ExecutionGriptapeNodeEvent(wrapped_event=ExecutionEvent(payload=InvolvedNodesEvent(involved_nodes=[])))
        )
        self.engine.event_manager.put_event(
            ExecutionGriptapeNodeEvent(
                wrapped_event=ExecutionEvent(payload=ControlFlowCancelledEvent(run_seconds=run_seconds))
            )
        )
        self._finish_run_if_over()

    async def _abandon_running_flow(self) -> None:
        """Give up on a run that has already failed, leaving the engine able to start another.

        `FSM._advance` leaves `_current_state` pointing at the state that raised, and that state is
        what `check_for_existing_running_flow` reads. So a run that dies mid-drive and is not cleaned
        up goes on being reported as in progress for the rest of the session: the Run button never
        clears and every later start is refused.

        Cancelling politely is the preferred way out, but it awaits every node's cancellation and can
        fail on its own account. A failure to cancel politely must not be the reason the engine wedges
        permanently, so the reset happens either way -- and the cancellation's own error is logged
        rather than raised, because the error worth reporting is the one that ended the run.
        """
        cancelled_gracefully = False
        if self.check_for_existing_running_flow():
            try:
                await self.cancel_flow_run()
                cancelled_gracefully = True
            except Exception:
                # Cancelling awaits arbitrary node code, so there is no narrower type to catch.
                logger.exception("Failed to cancel a run that had already failed. Abandoning it instead.")

        if cancelled_gracefully:
            # cancel_flow_run already reset the machine and told the editor the run is over.
            return

        run_seconds = None
        if self._global_control_flow_machine is not None:
            run_seconds = self._global_control_flow_machine.context.seconds_since_run_started()
            self._global_control_flow_machine.reset_machine(cancel=True)
        self._global_single_node_resolution = False
        self._global_dag_builder.clear()
        self.engine.event_manager.put_event(
            ExecutionGriptapeNodeEvent(wrapped_event=ExecutionEvent(payload=InvolvedNodesEvent(involved_nodes=[])))
        )
        self.engine.event_manager.put_event(
            ExecutionGriptapeNodeEvent(
                wrapped_event=ExecutionEvent(payload=ControlFlowCancelledEvent(run_seconds=run_seconds))
            )
        )
        self._finish_run_if_over()

    def reset_global_execution_state(self) -> None:
        """Reset all global execution state - useful when clearing all workflows."""
        self._global_flow_queue.queue.clear()

        # Cleanup proxy nodes and restore connections before resetting machine
        if self._global_control_flow_machine is not None:
            self._global_control_flow_machine.reset_machine()

        # Reset control flow machine
        self._global_single_node_resolution = False

        # Clear all connections to prevent memory leaks and stale references
        self._connections.connections.clear()
        self._connections.outgoing_index.clear()
        self._connections.incoming_index.clear()
        self._finish_run_if_over()

        logger.debug("Reset global execution state")

    # Public methods to replace private variable access from external classes
    def is_execution_queue_empty(self) -> bool:
        """Check if the global execution queue is empty."""
        return self._global_flow_queue.empty()

    def get_next_node_from_execution_queue(self) -> BaseNode | None:
        """Get the next node from the global execution queue, or None if empty."""
        if self._global_flow_queue.empty():
            return None
        queue_item = self._global_flow_queue.get()
        self._global_flow_queue.task_done()
        return queue_item.node

    def clear_execution_queue(self, flow: ControlFlow) -> None:
        """Clear all nodes from the global execution queue."""
        if self._global_control_flow_machine and self._global_control_flow_machine.context.flow_name == flow.name:
            self._global_flow_queue.queue.clear()

    def has_connection(
        self,
        source_node: BaseNode,
        source_parameter: Parameter,
        target_node: BaseNode,
        target_parameter: Parameter,
    ) -> bool:
        """Check if a connection exists between the specified nodes and parameters."""
        return self._has_connection(source_node, source_parameter, target_node, target_parameter)

    # Internal execution queue helper methods to consolidate redundant operations
    async def _handle_flow_start_if_not_running(
        self,
        flow: ControlFlow,
        *,
        debug_mode: bool,
        error_message: str,
    ) -> None:
        """Common logic for starting flow execution if not already running."""
        if not self.check_for_existing_running_flow():
            if self._global_flow_queue.empty():
                raise RuntimeError(error_message)
            queue_item = self._global_flow_queue.get()
            start_node = queue_item.node
            self._global_flow_queue.task_done()
            # Get or create machine
            if self._global_control_flow_machine is None:
                self._global_control_flow_machine = ControlFlowMachine(flow.name, engine=self.engine)
            await self._global_control_flow_machine.start_flow(start_node, debug_mode=debug_mode)

    async def _handle_post_execution_queue_processing(self, *, debug_mode: bool) -> None:
        """Handle execution queue processing after execution completes."""
        if not self.check_for_existing_running_flow() and not self._global_flow_queue.empty():
            queue_item = self._global_flow_queue.get()
            start_node = queue_item.node
            self._global_flow_queue.task_done()
            machine = self._global_control_flow_machine
            if machine is not None:
                await machine.start_flow(start_node, debug_mode=debug_mode)

    async def resolve_singular_node(self, flow: ControlFlow, node: BaseNode, *, debug_mode: bool = False) -> None:
        # We are now going to have different behavior depending on how the node is behaving.
        if self.check_for_existing_running_flow():
            # Now we know something is running, it's ParallelResolutionMachine, and that we are in single_node_resolution.
            # Nothing below awaits before inject_node: that is what lets the running driver
            # see this as one atomic change, rather than catching it half-applied or tearing
            # the DAG down between the check above and the mutation.
            machine = self._global_control_flow_machine
            if machine is None:
                msg = f"Attempted to run '{node.name}' as part of the workflow already in progress, but that workflow could not be found. Wait for it to finish and try again."
                raise RuntimeError(msg)
            # The cold path below unresolves the node before queueing it, and so must this
            # one: otherwise an already-resolved node is treated as a duplicate completion
            # and never re-runs, and the editor keeps showing it as resolved.
            node.prepare_to_run_again()
            machine.resolution_machine.inject_node(node)
            # Emit involved nodes update after adding node to DAG
            involved_nodes = list(self._global_dag_builder.node_to_reference.keys())
            self.engine.event_manager.put_event(
                ExecutionGriptapeNodeEvent(
                    wrapped_event=ExecutionEvent(payload=InvolvedNodesEvent(involved_nodes=involved_nodes))
                )
            )
        else:
            self._raise_if_run_cancelled()
            # Set that we are only working on one node right now!
            self._global_single_node_resolution = True
            # Get or create machine
            self._global_control_flow_machine = ControlFlowMachine(flow.name, engine=self.engine)
            self._global_control_flow_machine.context.current_nodes = [node]
            resolution_machine = self._global_control_flow_machine.resolution_machine
            resolution_machine.change_debug_mode(debug_mode=debug_mode)
            node.state = NodeResolutionState.UNRESOLVED
            # Build the DAG for the node
            self._global_dag_builder.add_node_with_dependencies(node)
            resolution_machine.context.dag_builder = self._global_dag_builder

            # In SEQUENTIAL mode, show all flow nodes as involved. In PARALLEL mode, show only DAG nodes.
            execution_type = self.engine.config_manager.get_config_value(
                "workflow_execution_mode", default=WorkflowExecutionMode.SEQUENTIAL
            )
            if execution_type == WorkflowExecutionMode.SEQUENTIAL:
                involved_nodes = self.get_involved_node_names(flow)
            else:
                involved_nodes = list(self._global_dag_builder.node_to_reference.keys())
            # Send a InvolvedNodesRequest

            self.engine.event_manager.put_event(
                ExecutionGriptapeNodeEvent(
                    wrapped_event=ExecutionEvent(payload=InvolvedNodesEvent(involved_nodes=involved_nodes))
                )
            )
            try:
                await self._global_control_flow_machine.start_flow(
                    start_node=node, end_node=node, debug_mode=debug_mode
                )
            except Exception as e:
                logger.exception("Exception during single node resolution")
                # Single-node mode was raised before the run began, so a run that never begins still
                # has to drop it -- which is why this cleanup cannot be conditional on liveness.
                await self._abandon_running_flow()
                raise RuntimeError(e) from e

            if resolution_machine.is_errored():
                error_message = resolution_machine.get_error_message()
                self._global_single_node_resolution = False
                self._global_control_flow_machine.context.current_nodes = []
                self.engine.event_manager.put_event(
                    ExecutionGriptapeNodeEvent(
                        wrapped_event=ExecutionEvent(payload=InvolvedNodesEvent(involved_nodes=[]))
                    )
                )
                # Re-raise with the original error message
                raise RuntimeError(error_message or "Node resolution failed")

            if resolution_machine.is_complete():
                self._global_single_node_resolution = False
                self._global_control_flow_machine.context.current_nodes = []
            self.engine.event_manager.put_event(
                ExecutionGriptapeNodeEvent(wrapped_event=ExecutionEvent(payload=InvolvedNodesEvent(involved_nodes=[])))
            )

    async def single_execution_step(self, flow: ControlFlow, change_debug_mode: bool) -> None:  # noqa: FBT001
        # do a granular step
        await self._handle_flow_start_if_not_running(
            flow, debug_mode=True, error_message="Flow has not yet been started. Cannot step while no flow has begun."
        )
        if not self.check_for_existing_running_flow():
            return
        if self._global_control_flow_machine is None:
            return
        # The only path that pauses a live run, so it applies whether or not we go
        # on to drive.
        if change_debug_mode:
            self._global_control_flow_machine.resolution_machine.change_debug_mode(debug_mode=True)
        if self.execution_is_advancing():
            return
        await self._global_control_flow_machine.granular_step()
        if self._global_control_flow_machine.resolution_machine.is_complete():
            self._global_single_node_resolution = False

    async def single_node_step(self, flow: ControlFlow) -> None:
        # It won't call single_node_step without an existing flow running from US.
        await self._handle_flow_start_if_not_running(
            flow, debug_mode=True, error_message="Flow has not yet been started. Cannot step while no flow has begun."
        )
        if not self.check_for_existing_running_flow():
            return
        # Step over a whole node
        if self._global_single_node_resolution:
            msg = "Cannot step through the Control Flow in Single Node Execution"
            raise RuntimeError(msg)
        if self._global_control_flow_machine is None:
            return
        # Clearing the paused flag is what resumes a paused run, so it applies
        # whether or not we go on to drive.
        self._global_control_flow_machine.resolution_machine.change_debug_mode(debug_mode=False)
        if self.execution_is_advancing():
            return
        await self._global_control_flow_machine.node_step()
        # Start the next resolution step now please.
        await self._handle_post_execution_queue_processing(debug_mode=True)

    async def continue_executing(self, flow: ControlFlow) -> None:
        await self._handle_flow_start_if_not_running(
            flow, debug_mode=False, error_message="Flow has not yet been started. Cannot step while no flow has begun."
        )
        if not self.check_for_existing_running_flow():
            return
        # Turn all debugging to false and continue on. This resumes a paused run,
        # so it applies whether or not we go on to drive.
        if self._global_control_flow_machine is not None:
            self._global_control_flow_machine.change_debug_mode(False)
        if self.execution_is_advancing():
            return
        if self._global_control_flow_machine is not None:
            if self._global_single_node_resolution:
                if self._global_control_flow_machine.resolution_machine.is_complete():
                    self._global_single_node_resolution = False
                else:
                    await self._global_control_flow_machine.resolution_machine.update()
            else:
                await self._global_control_flow_machine.node_step()
        # Now it is done executing. make sure it's actually done?
        await self._handle_post_execution_queue_processing(debug_mode=False)

    def unresolve_whole_flow(self, flow: ControlFlow) -> None:
        for node in flow.nodes.values():
            node.make_node_unresolved(current_states_to_trigger_change_event=None)
            # Clear entry control parameter for new execution
            node.set_entry_control_parameter(None)

    def flow_state(self, flow: ControlFlow) -> tuple[list[str], list[str], list[str]]:
        if not self.check_for_existing_running_flow():
            return [], [], []
        if self._global_control_flow_machine is None:
            return [], [], []
        control_flow_context = self._global_control_flow_machine.context
        current_control_nodes = (
            [control_flow_node.name for control_flow_node in control_flow_context.current_nodes]
            if control_flow_context.current_nodes is not None
            else []
        )
        if self._global_single_node_resolution:
            involved_nodes = list(self._global_dag_builder.node_to_reference.keys())
        else:
            involved_nodes = self.get_involved_node_names(flow)

        # Get currently resolving nodes from the resolution machine (always ParallelResolutionMachine)
        current_resolving_nodes = [
            node.node_reference.name for node in control_flow_context.resolution_machine.context.task_to_node.values()
        ]
        return current_control_nodes, current_resolving_nodes, involved_nodes

    def get_involved_node_names(self, flow: ControlFlow) -> list[str]:
        """The nodes an editor should treat as involved in a run of `flow`.

        A group's children live in the group's own `nodes` dict rather than the flow's, so
        `flow.nodes` names the group but nothing inside it. The editor gates a node's run
        status on involvement, so without this expansion the children of a running group can
        never light up. Nested groups are walked to any depth.

        Args:
            flow: The flow whose run is being announced

        Returns:
            Every node name in the flow, including nodes nested inside groups
        """
        involved_node_names: list[str] = []
        nodes_to_visit = list(flow.nodes.values())
        # Deduplicates. A plain BaseNodeGroup takes a node into its own `nodes` dict without
        # removing it from the flow's -- only a SubflowNodeGroup relocates its children -- so on a
        # well-formed graph the walk reaches such a child twice, once from the flow and once from
        # the group. Without this set the name would be announced twice. (Cycles cannot happen:
        # BaseNodeGroup._validate_nodes_can_be_nested rejects them when the node is added.)
        visited_node_ids: set[int] = set()
        while nodes_to_visit:
            node = nodes_to_visit.pop()
            if id(node) in visited_node_ids:
                continue
            visited_node_ids.add(id(node))
            involved_node_names.append(node.name)
            if isinstance(node, BaseNodeGroup):
                nodes_to_visit.extend(node.nodes.values())
        return involved_node_names

    def get_start_node_from_node(self, flow: ControlFlow, node: BaseNode) -> BaseNode | None:
        # backwards chain in control outputs.
        if node not in flow.nodes.values():
            return None
        # Go back through incoming control connections to get the start node
        curr_node = node
        prev_node = self.get_prev_node(flow, curr_node)
        # Fencepost loop - get the first previous node name and then we go
        while prev_node:
            curr_node = prev_node
            prev_node = self.get_prev_node(flow, prev_node)
        return curr_node

    def get_prev_node(self, flow: ControlFlow, node: BaseNode) -> BaseNode | None:  # noqa: ARG002
        connections = self.get_connections()
        if node.name in connections.incoming_index:
            parameters = connections.incoming_index[node.name]
            for parameter_name in parameters:
                parameter = node.get_parameter_by_name(parameter_name)
                if parameter and ParameterTypeBuiltin.CONTROL_TYPE.value == parameter.output_type:
                    # this is a control connection
                    connection_ids = connections.incoming_index[node.name][parameter_name]
                    for connection_id in connection_ids:
                        connection = connections.connections[connection_id]
                        # Skip internal NodeGroup connections
                        if connection.is_node_group_internal:
                            continue
                        return connection.get_source_node()
        return None

    def get_start_node_queue(self) -> Queue | None:
        # For cross-flow execution, we consider ALL nodes across ALL (non-referenced) flows.
        # Referenced subflows are executed explicitly via StartLocalSubflowRequest and must not
        # participate in the top-level queue; SubflowNodeGroup children run inside their group's
        # subflow. Both are ownership/scope decisions and live here, NOT in the classifier, so
        # the classifier stays scope-agnostic and is shared with isolated subflow execution.
        self._global_flow_queue.queue.clear()

        all_flows = self.engine.object_manager.get_filtered_subset(type=ControlFlow)
        scope_nodes: list[BaseNode] = []
        for current_flow in all_flows.values():
            if self.is_referenced_workflow(current_flow):
                continue
            scope_nodes.extend(current_flow.nodes.values())
        scope_nodes = self.exclude_subflow_group_children(scope_nodes)

        # if no nodes across all flows, no execution possible
        if not scope_nodes:
            return None

        categories = self.classify_nodes_for_dag(scope_nodes)

        # populate the global flow queue with node type information
        for node in categories.start_nodes:
            self._global_flow_queue.put(QueueItem(node=node, dag_execution_type=DagExecutionType.START_NODE))
        for node in categories.control_nodes:
            self._global_flow_queue.put(QueueItem(node=node, dag_execution_type=DagExecutionType.CONTROL_NODE))
        for node in categories.data_sink_nodes:
            self._global_flow_queue.put(QueueItem(node=node, dag_execution_type=DagExecutionType.DATA_NODE))

        return self._global_flow_queue

    @staticmethod
    def exclude_subflow_group_children(nodes: list[BaseNode]) -> list[BaseNode]:
        """Drop nodes owned by a SubflowNodeGroup; they run inside their group's own subflow.

        This is a scope/ownership decision kept out of classify_nodes_for_dag so the classifier
        stays scope-agnostic. Only the top-level run (get_start_node_queue) applies it, to keep a
        group's members from being seeded into the cross-flow DAG (they run when the group's proxy
        node executes its subflow). The isolated-subflow path must NOT apply it: inside a group's
        own subflow every member carries that group as its parent_group, so excluding them would
        strand the members and seed only the start node.
        """
        return [node for node in nodes if not isinstance(node.parent_group, SubflowNodeGroup)]

    def classify_nodes_for_dag(self, nodes: list[BaseNode]) -> DagNodeCategories:  # noqa: C901, PLR0912, PLR0915
        """Bucket a scope of nodes into start / control-entry / data-sink roles for DAG seeding.

        This is scope-agnostic: callers decide which nodes are in scope (a whole flow's worth of
        nodes for a top-level run, or a single subflow's nodes for an isolated subflow run). The
        same categorization then drives DAG seeding for both, so the two paths cannot drift.

        - StartNode instances are start nodes.
        - A node with active control parameters is a control-entry node unless it has an external
          incoming control connection (in which case the forward control walk reaches it).
        - A node with no active control parameters is a data node; data nodes with no external
          outgoing connection are terminal/leaf sinks and get seeded so their dependencies resolve.
        """
        data_nodes: list[BaseNode] = []
        start_nodes: list[BaseNode] = []
        control_nodes: list[BaseNode] = []
        cn_mgr = self.get_connections()
        for node in nodes:
            # if it's a start node, start here!
            if isinstance(node, StartNode):
                start_nodes.append(node)
                continue
            # if it's a control node, there could be a flow.
            control_param = False
            for parameter in node.parameters:
                if ParameterTypeBuiltin.CONTROL_TYPE.value == parameter.output_type:
                    # Check if the control parameters are being used at all. If they are not, treat it as a data node.
                    incoming_control = (
                        node.name in cn_mgr.incoming_index and parameter.name in cn_mgr.incoming_index[node.name]
                    )
                    outgoing_control = (
                        node.name in cn_mgr.outgoing_index and parameter.name in cn_mgr.outgoing_index[node.name]
                    )
                    if incoming_control or outgoing_control:
                        control_param = True
                        break
            if not control_param:
                # saving this for later
                data_nodes.append(node)
                continue
            # check if it has an incoming connection. If it does, it's not a start node
            has_control_connection = False
            if node.name in cn_mgr.incoming_index:
                for param_name in cn_mgr.incoming_index[node.name]:
                    param = node.get_parameter_by_name(param_name)
                    if param and ParameterTypeBuiltin.CONTROL_TYPE.value == param.output_type:
                        # there is a control connection coming in
                        # Check each connection to see if it's an internal NodeGroup connection
                        connection_ids = cn_mgr.incoming_index[node.name][param_name]
                        has_external_control_connection = False
                        for connection_id in connection_ids:
                            connection = cn_mgr.connections[connection_id]
                            # Skip internal NodeGroup connections - they shouldn't disqualify a node from being a start node
                            if connection.is_node_group_internal:
                                continue
                            # If the node is a BaseIterativeStartNode, it may have an incoming hidden connection from it's BaseIterativeEndNode for iteration.
                            if isinstance(node, BaseIterativeStartNode):
                                connected_node = connection.get_source_node()
                                # Check if the source node is the end loop node associated with this BaseIterativeStartNode.
                                # If it is, then this could still be the first node in the control flow.
                                if connected_node == node.end_node:
                                    continue
                            has_external_control_connection = True
                            break
                        if has_external_control_connection:
                            has_control_connection = True
                            break
            # if there is a connection coming in, isn't a start.
            if has_control_connection:
                continue
            # Does it have an outgoing connection?
            if node.name in cn_mgr.outgoing_index:
                # If one of the outgoing connections is control, add it. otherwise don't.
                for param_name in cn_mgr.outgoing_index[node.name]:
                    param = node.get_parameter_by_name(param_name)
                    if param and ParameterTypeBuiltin.CONTROL_TYPE.value == param.output_type:
                        control_nodes.append(node)
                        break
            else:
                control_nodes.append(node)

        # A data node with no OUTGOING (non-internal) data connection is a terminal/leaf sink.
        valid_data_nodes: list[BaseNode] = []
        for node in data_nodes:
            has_external_outgoing = False
            if node.name in cn_mgr.outgoing_index:
                for param_name in cn_mgr.outgoing_index[node.name]:
                    connection_ids = cn_mgr.outgoing_index[node.name][param_name]
                    for connection_id in connection_ids:
                        connection = cn_mgr.connections[connection_id]
                        # Skip internal NodeGroup connections
                        if connection.is_node_group_internal:
                            continue
                        has_external_outgoing = True
                        break
                    if has_external_outgoing:
                        break
            # Only add nodes that have no non-internal outgoing connections
            if not has_external_outgoing:
                valid_data_nodes.append(node)

        return DagNodeCategories(start_nodes=start_nodes, control_nodes=control_nodes, data_sink_nodes=valid_data_nodes)

    def get_connected_input_from_node(self, flow: ControlFlow, node: BaseNode) -> list[tuple[BaseNode, Parameter]]:  # noqa: ARG002
        global_connections = self.get_connections()
        connections = []
        if node.name in global_connections.incoming_index:
            connection_ids = [
                item for value_list in global_connections.incoming_index[node.name].values() for item in value_list
            ]
            for connection_id in connection_ids:
                connection = global_connections.connections[connection_id]
                connections.append((connection.source_node, connection.source_parameter))
        return connections

    def get_connected_output_from_node(self, flow: ControlFlow, node: BaseNode) -> list[tuple[BaseNode, Parameter]]:  # noqa: ARG002
        global_connections = self.get_connections()
        connections = []
        if node.name in global_connections.outgoing_index:
            connection_ids = [
                item for value_list in global_connections.outgoing_index[node.name].values() for item in value_list
            ]
            for connection_id in connection_ids:
                connection = global_connections.connections[connection_id]
                connections.append((connection.target_node, connection.target_parameter))
        return connections

    def get_connected_input_parameters(
        self,
        flow: ControlFlow,  # noqa: ARG002
        node: BaseNode,
        param: Parameter,
    ) -> list[tuple[BaseNode, Parameter]]:
        global_connections = self.get_connections()
        connections = []
        if node.name in global_connections.incoming_index:
            incoming_params = global_connections.incoming_index[node.name]
            if param.name in incoming_params:
                for connection_id in incoming_params[param.name]:
                    connection = global_connections.connections[connection_id]
                    connections.append((connection.source_node, connection.source_parameter))
        return connections

    def get_connections_on_node(self, node: BaseNode) -> list[BaseNode] | None:
        connections = self.get_connections()
        # get all of the connection ids
        connected_nodes = []
        # Handle outgoing connections
        if node.name in connections.outgoing_index:
            outgoing_params = connections.outgoing_index[node.name]
            outgoing_connection_ids = []
            for connection_ids in outgoing_params.values():
                outgoing_connection_ids = outgoing_connection_ids + connection_ids
            for connection_id in outgoing_connection_ids:
                connection = connections.connections[connection_id]
                if connection.source_node not in connected_nodes:
                    connected_nodes.append(connection.target_node)
        # Handle incoming connections
        if node.name in connections.incoming_index:
            incoming_params = connections.incoming_index[node.name]
            incoming_connection_ids = []
            for connection_ids in incoming_params.values():
                incoming_connection_ids = incoming_connection_ids + connection_ids
            for connection_id in incoming_connection_ids:
                connection = connections.connections[connection_id]
                if connection.source_node not in connected_nodes:
                    connected_nodes.append(connection.source_node)
        # Return all connected nodes. No duplicates
        return connected_nodes

    def get_all_connected_nodes(self, node: BaseNode) -> list[BaseNode]:
        discovered = {}
        processed = {}
        queue = Queue()
        queue.put(node)
        discovered[node] = True
        while not queue.empty():
            curr_node = queue.get()
            processed[curr_node] = True
            next_nodes = self.get_connections_on_node(curr_node)
            if next_nodes:
                for next_node in next_nodes:
                    if next_node not in discovered:
                        discovered[next_node] = True
                        queue.put(next_node)
        return list(processed.keys())

    def is_node_connected(self, start_node: BaseNode, node: BaseNode) -> list[str]:
        """Check if node is in the forward control path from start_node, returning boundary nodes if connected.

        Returns:
            list[str]: Names of nodes that have direct connections to 'node' and are in the forward control path,
                      or empty list if not in forward path.
        """
        connections = self.get_connections()

        # Check if node is in the forward control path from start_node
        if not connections.is_node_in_forward_control_path(start_node, node):
            return []

        # Node is in forward path - find boundary nodes that connect to it
        boundary_nodes = []

        # Check incoming connections to the target node
        if node.name in connections.incoming_index:
            incoming_params = connections.incoming_index[node.name]
            for connection_ids in incoming_params.values():
                for connection_id in connection_ids:
                    connection = connections.connections[connection_id]
                    source_node_name = connection.source_node.name

                    # Only include if source node is also in the forward control path from start_node
                    if (
                        connections.is_node_in_forward_control_path(start_node, connection.source_node)
                        and source_node_name not in boundary_nodes
                    ):
                        boundary_nodes.append(source_node_name)

        return boundary_nodes

    def get_node_dependencies(self, flow: ControlFlow, node: BaseNode) -> list[BaseNode]:
        """Get all upstream nodes that the given node depends on.

        This method performs a breadth-first search starting from the given node and working backwards through its non-control input connections to identify all nodes that must run before this node can be resolved.
        It ignores control connections, since we're only focusing on node dependencies.

        Args:
            flow (ControlFlow): The flow containing the node
            node (BaseNode): The node to find dependencies for

        Returns:
            list[BaseNode]: A list of all nodes that the given node depends on, including the node itself (as the first element)
        """
        node_list = [node]
        node_queue = Queue()
        node_queue.put(node)
        while not node_queue.empty():
            curr_node = node_queue.get()
            input_connections = self.get_connected_input_from_node(flow, curr_node)
            if input_connections:
                for input_node, input_parameter in input_connections:
                    if (
                        ParameterTypeBuiltin.CONTROL_TYPE.value != input_parameter.output_type
                        and input_node not in node_list
                    ):
                        node_list.append(input_node)
                        node_queue.put(input_node)
        return node_list
