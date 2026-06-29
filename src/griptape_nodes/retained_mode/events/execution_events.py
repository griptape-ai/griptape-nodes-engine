from dataclasses import dataclass, field
from typing import Any, Required, TypedDict

from griptape_nodes.retained_mode.events.base_events import (
    ExecutionPayload,
    RequestPayload,
    ResultDetails,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    SkipTheLineMixin,
    WorkflowAlteredMixin,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.node_events import SerializedNodeCommands
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry

# Requests and Results TO/FROM USER! These begin requests - and are not fully Execution Events.


@dataclass
@PayloadRegistry.register
class ResolveNodeRequest(RequestPayload):
    """Resolve (execute) a specific node.

    Use when: Running individual nodes, testing node execution, debugging workflows,
    stepping through execution manually. Validates inputs and runs node logic.

    Args:
        node_name: Name of the node to resolve/execute
        debug_mode: Whether to run in debug mode (default: False)

    Results: ResolveNodeResultSuccess | ResolveNodeResultFailure (with validation exceptions)
    """

    node_name: str
    debug_mode: bool = False


@dataclass
@PayloadRegistry.register
class ResolveNodeResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Node resolved successfully. Node execution completed and outputs are available."""


@dataclass
@PayloadRegistry.register
class ResolveNodeResultFailure(ResultPayloadFailure):
    """Node resolution failed. Contains validation errors that prevented execution.

    Args:
        validation_exceptions: List of validation errors that occurred
    """

    validation_exceptions: list[Exception]


@dataclass
@PayloadRegistry.register
class StartFlowRequest(RequestPayload):
    """Start executing a flow.

    Use when: Running workflows, beginning automated execution, testing complete flows.
    Validates all nodes and begins execution from resolved nodes.

    Args:
        flow_name: Name of the flow to start (deprecated, use flow_node_name)
        flow_node_name: Name of the flow node to start
        debug_mode: Whether to run in debug mode (default: False)
        wait_for_completion: When True, the handler polls until the flow resolves before
            returning. Converts the fire-and-forget kickoff into a synchronous run so callers
            can read output values immediately afterwards without polling node state themselves.
        completion_timeout_ms: Only meaningful when wait_for_completion=True. Maximum time to
            wait for the flow to resolve. None means wait indefinitely.

    Results: StartFlowResultSuccess | StartFlowResultFailure (with validation exceptions)
    """

    # Maintaining flow_name for backwards compatibility. Will be removed in https://github.com/griptape-ai/griptape-nodes/issues/1663
    flow_name: str | None = None
    flow_node_name: str | None = None
    debug_mode: bool = False
    # If this is true, the final ControlFLowResolvedEvent will be pickled to be picked up from inside a subprocess.
    pickle_control_flow_result: bool = False
    wait_for_completion: bool = False
    completion_timeout_ms: int | None = None


@dataclass
@PayloadRegistry.register
class StartFlowResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Flow started successfully. Execution is now running."""


@dataclass
@PayloadRegistry.register
class StartFlowResultFailure(ResultPayloadFailure):
    """Flow start failed. Contains validation errors that prevented execution.

    Args:
        validation_exceptions: List of validation errors that occurred
    """

    validation_exceptions: list[Exception]


@dataclass
@PayloadRegistry.register
class StartLocalSubflowRequest(RequestPayload):
    """Start an independent local subflow that runs concurrently with the main flow.

    Use when: Running loop iterations or other independent subflows that need their own
    execution context and should not interfere with the main flow's state.

    This creates a separate ControlFlowMachine with its own DagBuilder to ensure full isolation.

    Args:
        flow_name: Name of the flow to start as a subflow
        start_node: The node to start execution from (None to auto-detect start node)
        pickle_control_flow_result: Whether to pickle the result for subprocess retrieval

    Results: StartLocalSubflowResultSuccess | StartLocalSubflowResultFailure
    """

    flow_name: str
    start_node: str | None = None
    pickle_control_flow_result: bool = False


@dataclass
@PayloadRegistry.register
class StartLocalSubflowResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Local subflow started successfully and is running independently."""


@dataclass
@PayloadRegistry.register
class StartLocalSubflowResultFailure(ResultPayloadFailure):
    """Local subflow failed to start. Check result_details for error information."""


@dataclass
@PayloadRegistry.register
class StartFlowFromNodeRequest(RequestPayload):
    """Start executing a flow from a specific node.

    Use when: Resuming execution from a particular node, debugging specific parts of a flow,
    re-running portions of a workflow, implementing custom execution control.

    Args:
        flow_name: Name of the flow to start (deprecated)
        node_name: Name of the node to start execution from
        debug_mode: Whether to run in debug mode (default: False)
        pickle_control_flow_result: If this is true, the final ControlFLowResolvedEvent will be pickled to be picked up from inside a subprocess

    Results: StartFlowFromNodeResultSuccess | StartFlowFromNodeResultFailure (with validation exceptions)
    """

    flow_name: str | None = None
    node_name: str | None = None
    debug_mode: bool = False
    pickle_control_flow_result: bool = False


@dataclass
@PayloadRegistry.register
class StartFlowFromNodeResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Flow started from node successfully. Execution is now running from the specified node."""


@dataclass
@PayloadRegistry.register
class StartFlowFromNodeResultFailure(ResultPayloadFailure):
    """Flow start from node failed. Contains validation errors that prevented execution.

    Args:
        validation_exceptions: List of validation errors that occurred
    """

    validation_exceptions: list[Exception]


@dataclass
@PayloadRegistry.register
class CancelFlowRequest(RequestPayload):
    """Cancel a running flow execution.

    Use when: Stopping long-running workflows, handling user cancellation,
    stopping execution due to errors or changes. Cleanly terminates execution.

    Args:
        flow_name: Name of the flow to cancel (deprecated)

    Results: CancelFlowResultSuccess | CancelFlowResultFailure (cancellation error)
    """

    # Maintaining flow_name for backwards compatibility. Will be removed in https://github.com/griptape-ai/griptape-nodes/issues/1663
    flow_name: str | None = None


@dataclass
@PayloadRegistry.register
class CancelFlowResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Flow cancelled successfully. Execution has been terminated."""


@dataclass
@PayloadRegistry.register
class CancelFlowResultFailure(ResultPayloadFailure):
    """Flow cancellation failed. Common causes: flow not running, cancellation error."""


@dataclass
@PayloadRegistry.register
class UnresolveFlowRequest(RequestPayload):
    # Maintaining flow_name for backwards compatibility. Will be removed in https://github.com/griptape-ai/griptape-nodes/issues/1663
    flow_name: str | None = None


@dataclass
@PayloadRegistry.register
class UnresolveFlowResultFailure(ResultPayloadFailure):
    pass


@dataclass
@PayloadRegistry.register
class UnresolveFlowResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    pass


# User Tick Events


# Step In: Execute one resolving step at a time (per parameter)
@dataclass
@PayloadRegistry.register
class SingleExecutionStepRequest(RequestPayload):
    # Maintaining flow_name for backwards compatibility. Will be removed in https://github.com/griptape-ai/griptape-nodes/issues/1663
    flow_name: str | None = None


@dataclass
@PayloadRegistry.register
class SingleExecutionStepResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    pass


@PayloadRegistry.register
class SingleExecutionStepResultFailure(ResultPayloadFailure):
    pass


# Step Over: Execute one node at a time (execute whole node and move on) IS THIS CONTROL NODE OR ANY NODE?
@dataclass
@PayloadRegistry.register
class SingleNodeStepRequest(RequestPayload):
    # Maintaining flow_name for backwards compatibility. Will be removed in https://github.com/griptape-ai/griptape-nodes/issues/1663
    flow_name: str | None = None


@dataclass
@PayloadRegistry.register
class SingleNodeStepResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    pass


@dataclass
@PayloadRegistry.register
class SingleNodeStepResultFailure(ResolveNodeResultFailure):
    pass


# Continue
@dataclass
@PayloadRegistry.register
class ContinueExecutionStepRequest(RequestPayload):
    # Maintaining flow_name for backwards compatibility. Will be removed in https://github.com/griptape-ai/griptape-nodes/issues/1663
    flow_name: str | None = None


@dataclass
@PayloadRegistry.register
class ContinueExecutionStepResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    pass


@dataclass
@PayloadRegistry.register
class ContinueExecutionStepResultFailure(ResultPayloadFailure):
    pass


@dataclass
@PayloadRegistry.register
class GetFlowStateRequest(RequestPayload):
    """Get the current execution state of a flow.

    Use when: Monitoring execution progress, debugging workflow state,
    implementing execution UIs, checking which nodes are active.

    Results: GetFlowStateResultSuccess (with control/resolving nodes) | GetFlowStateResultFailure (flow not found)
    """

    # Maintaining flow_name for backwards compatibility. Will be removed in https://github.com/griptape-ai/griptape-nodes/issues/1663
    flow_name: str | None = None


@dataclass
@PayloadRegistry.register
class GetFlowStateResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Flow execution state retrieved successfully.

    Args:
        control_nodes: Name of the current control node (if any)
        resolving_nodes: Name of the node currently being resolved (if any)
    """

    control_nodes: list[str]
    resolving_nodes: list[str]
    involved_nodes: list[str]


@dataclass
@PayloadRegistry.register
class GetFlowStateResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Flow state retrieval failed. Common causes: flow not found, no current context."""


@dataclass
@PayloadRegistry.register
class GetIsFlowRunningRequest(RequestPayload):
    """Check if a flow is currently running.

    Use when: Monitoring execution status, preventing concurrent execution,
    implementing execution controls, checking if flow can be modified.

    Results: GetIsFlowRunningResultSuccess (with running status) | GetIsFlowRunningResultFailure (flow not found)
    """

    # Maintaining flow_name for backwards compatibility. Will be removed in https://github.com/griptape-ai/griptape-nodes/issues/1663
    flow_name: str | None = None


@dataclass
@PayloadRegistry.register
class GetIsFlowRunningResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Flow running status retrieved successfully.

    Args:
        is_running: Whether the flow is currently executing
    """

    is_running: bool


@dataclass
@PayloadRegistry.register
class GetIsFlowRunningResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Flow running status retrieval failed. Common causes: flow not found, no current context."""


# Execution Events! These are sent FROM the EE to the User/GUI. HOW MANY DO WE NEED?
@dataclass
@PayloadRegistry.register
class CurrentControlNodeEvent(ExecutionPayload):
    node_name: str


@dataclass
@PayloadRegistry.register
class CurrentDataNodeEvent(ExecutionPayload):
    node_name: str


@dataclass
@PayloadRegistry.register
class SelectedControlOutputEvent(ExecutionPayload):
    node_name: str
    selected_output_parameter_name: str


@dataclass
@PayloadRegistry.register
class ParameterSpotlightEvent(ExecutionPayload):
    node_name: str
    parameter_name: str


@dataclass
@PayloadRegistry.register
class ControlFlowResolvedEvent(ExecutionPayload):
    end_node_name: str
    parameter_output_values: dict
    # Optional field for pickled parameter values - when present, parameter_output_values contains UUID references
    unique_parameter_uuid_to_values: dict[SerializedNodeCommands.UniqueParameterValueUUID, bytes] | None = field(
        default=None
    )


@dataclass
@PayloadRegistry.register
class ControlFlowCancelledEvent(ExecutionPayload):
    result_details: ResultDetails | str | None = None
    exception: Exception | None = None


@dataclass
@PayloadRegistry.register
class NodeResolvedEvent(ExecutionPayload):
    node_name: str
    parameter_output_values: dict
    node_type: str
    specific_library_name: str | None = None


@dataclass
@PayloadRegistry.register
class ParameterValueUpdateEvent(ExecutionPayload):
    node_name: str
    parameter_name: str
    data_type: str
    value: Any


@dataclass
@PayloadRegistry.register
class NodeUnresolvedEvent(ExecutionPayload):
    node_name: str


@dataclass
@PayloadRegistry.register
class NodeStartProcessEvent(ExecutionPayload):
    node_name: str


@dataclass
@PayloadRegistry.register
class NodeFinishProcessEvent(ExecutionPayload):
    node_name: str


@dataclass
@PayloadRegistry.register
class NodeErrorEvent(ExecutionPayload):
    node_name: str
    error_message: str


@dataclass
@PayloadRegistry.register
class InvolvedNodesEvent(ExecutionPayload):
    """Event indicating which nodes are involved in the current execution.

    For parallel resolution: Dynamic list based on DAG builder state
    For control flow/sequential: All nodes when started, empty when complete
    """

    involved_nodes: list[str]


@dataclass
@PayloadRegistry.register
class GriptapeEvent(ExecutionPayload):
    node_name: str
    parameter_name: str
    type: str
    value: Any


class NodeMetadata(TypedDict, total=False):
    """Metadata dict carried on nodes. node_type and library are required; all other keys are optional."""

    node_type: Required[str]
    library: Required[str]


@dataclass
@PayloadRegistry.register
class ExecuteNodeRequest(RequestPayload):
    """Execute a node's aprocess() directly with provided parameter values.

    Hydrates the node's input parameters, calls aprocess(), and returns outputs.
    Unlike ResolveNodeRequest, this bypasses flow/DAG machinery and executes
    the node's process method directly.

    Handling depends on where the request lands:

    - **Orchestrator**: the node must already exist in ObjectManager. If it does
      not, the request fails; node_metadata is ignored on this path. The
      orchestrator is the sole source of truth for node identity and parameter
      values.
    - **Worker**: a fresh transient node is constructed from node_metadata on
      every call via LibraryRegistry.create_node, hydrated, run, and discarded.
      The worker never persists nodes across requests. node_metadata is
      therefore required on this path.

    Args:
        node_name: Name of the node to execute.
        parameter_values: Input parameter values to set before execution.
        node_metadata: Full node metadata from the orchestrator. Required when
            the target library spawns a worker (used to construct the transient
            worker-side node). Ignored on the orchestrator path.
        variables: Workflow variable dict for inline {VAR} substitution, computed
            by the orchestrator from VariablesManager before the request is sent.
            An empty dict means substitution is disabled or there are no variables.
            Workers carry this field because they have no access to VariablesManager
            or the workflow context; in-process nodes use it to skip the NodeManager
            lookup that would otherwise resolve the flow.

    Results: ExecuteNodeResultSuccess | ExecuteNodeResultFailure
    """

    node_name: str
    parameter_values: dict[str, Any] = field(default_factory=dict)
    node_metadata: NodeMetadata | None = None
    variables: dict[str, str | int] = field(default_factory=dict)


@dataclass
@PayloadRegistry.register
class ExecuteNodeResultSuccess(ResultPayloadSuccess):
    """Successful result from executing a node directly.

    Args:
        parameter_output_values: Output parameter values from the node.
    """

    parameter_output_values: dict[str, Any] = field(default_factory=dict)


@dataclass
@PayloadRegistry.register
class ExecuteNodeResultFailure(ResultPayloadFailure):
    """Failed result from executing a node directly."""


@dataclass
@PayloadRegistry.register
class CancelExecuteNodeRequest(RequestPayload, SkipTheLineMixin):
    """Cancel an in-flight ExecuteNodeRequest on this engine.

    Dispatched by the orchestrator to a worker when a user cancels a flow and a
    node in that flow is currently executing on the worker. The worker locates
    the aprocess task registered under target_request_id, sets the cooperative
    cancellation flag on the node, and cancels the task.

    SkipTheLineMixin so the cancel bypasses the worker's event queue and reaches
    the dispatcher even when the queue is blocked behind the aprocess we are
    cancelling.

    Args:
        target_request_id: The request_id of the ExecuteNodeRequest to cancel.
    """

    target_request_id: str
    broadcast_result: bool = field(default=False, kw_only=True)


@dataclass
@PayloadRegistry.register
class CancelExecuteNodeResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Cancellation was delivered. The target request may or may not have been in-flight."""


@dataclass
@PayloadRegistry.register
class CancelExecuteNodeResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Cancellation could not be delivered."""
