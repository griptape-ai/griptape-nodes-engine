from dataclasses import dataclass

from griptape_nodes.retained_mode.events.base_events import (
    AppPayload,
    RequestPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    WorkflowAlteredMixin,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry


@dataclass
@PayloadRegistry.register
class SetWorkflowContextRequest(RequestPayload):
    """Set the current workflow context.

    Use when: Switching between workflows, initializing workflow sessions,
    setting the active workflow for subsequent operations, workflow navigation.

    Args:
        workflow_name: Name of the workflow to set as current context. When None,
                       the handler mints a fresh "unsaved:<uuid>" registry key and
                       auto-registers an unsaved entry under it. When provided and
                       starting with the unsaved-registry-key prefix ("unsaved:"),
                       the handler auto-registers a fresh unsaved entry keyed by
                       that exact value if one does not already exist.
        display_name: Human-readable name used when auto-registering an unsaved entry.
                      Ignored when the workflow is already in the registry. Defaults to
                      None; in that case the auto-registered entry gets a placeholder
                      name.
        working_directory: Folder this workflow belongs to, for a workflow that has no file
                      of its own yet. `{workflow_dir}` -- and so every project directory
                      built on it, `{outputs}` among them -- answers with this until the
                      workflow is saved, at which point the saved file's own directory takes
                      over. A DIRECTORY, not a file path. Absolute paths are recommended;
                      if you supply a relative path it is anchored to the workspace directory
                      (not the project base directory, which may sit one level above). Optional:
                      when None, `{workflow_dir}` answers the folder this workflow's first save
                      would land in, read from the project's `save_workflow` situation.

    Results: SetWorkflowContextSuccess (carries the resolved workflow_name) |
             SetWorkflowContextFailure (workflow not found, working_directory names an
             existing non-directory)
    """

    workflow_name: str | None = None
    display_name: str | None = None
    working_directory: str | None = None


@dataclass
@PayloadRegistry.register
class SetWorkflowContextSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Workflow context set successfully. Subsequent operations will use this workflow.

    Args:
        workflow_name: Resolved registry key for the workflow now in context. When the
                       request omitted `workflow_name`, this is the freshly minted
                       "unsaved:<uuid>" key. Otherwise it echoes the requested key.
    """

    workflow_name: str = ""


@dataclass
@PayloadRegistry.register
class SetWorkflowContextFailure(WorkflowAlteredMixin, ResultPayloadFailure):
    """Workflow context setting failed. Common causes: workflow not found, invalid workflow name."""


@dataclass
@PayloadRegistry.register
class GetWorkflowContextRequest(RequestPayload):
    """Get the current workflow context.

    Use when: Checking which workflow is active, displaying current workflow info,
    validating workflow state, debugging context issues.

    Results: GetWorkflowContextSuccess (with workflow name) | GetWorkflowContextFailure (no context set)
    """


@dataclass
@PayloadRegistry.register
class GetWorkflowContextSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Workflow context retrieved successfully.

    Args:
        workflow_name: Name of the current workflow context (None if no context set)
        is_saved: Whether the current workflow is backed by a file on disk. None when
                  no context is set or the context key is not in the registry. Reported
                  identically by the CurrentWorkflowChanged app event, which carries both
                  fields, so a client following that event does not need to poll this.
    """

    workflow_name: str | None
    is_saved: bool | None = None


@dataclass
@PayloadRegistry.register
class GetWorkflowContextFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Workflow context retrieval failed. Common causes: context not initialized, system error."""


@dataclass
@PayloadRegistry.register
class EnsureWorkflowAndFlowRequest(RequestPayload):
    """Ensure a workflow + flow context exists, creating scratch ones if needed.

    Use when: Bootstrapping from a cold engine state. This is the typical opening call from
    an MCP client that is about to build a workflow from scratch. Idempotent: if both a
    workflow and a flow are already in the current context, returns their names without
    creating new ones. Only creates the pieces that are missing.

    Args:
        workflow_name: Name to use if a new workflow must be created. Ignored when a
            workflow is already in context. When None or starting with the unsaved
            registry-key prefix ("unsaved:"), the engine auto-registers an unsaved
            entry and the resolved key is returned in the success result.
        display_name: Human-readable name attached to the auto-registered unsaved
            entry. Ignored when the workflow is already in context or when a saved
            workflow_name is supplied.
        flow_name: Name to use if a new flow must be created. Ignored when a flow is
            already in context. Defaults to the engine-assigned name.
        working_directory: Folder the workflow belongs to until it is saved, forwarded to
            SetWorkflowContextRequest. Ignored when a workflow is already in context. See
            SetWorkflowContextRequest for what the value means and how it is normalized.

    Results: EnsureWorkflowAndFlowResultSuccess | EnsureWorkflowAndFlowResultFailure
    """

    workflow_name: str | None = None
    display_name: str | None = None
    flow_name: str | None = None
    working_directory: str | None = None


@dataclass
@PayloadRegistry.register
class EnsureWorkflowAndFlowResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Workflow + flow context is ready for subsequent CreateNode calls.

    Args:
        workflow_name: Name of the workflow in the current context.
        flow_name: Name of the flow in the current context.
        created_workflow: True if this call created the workflow; False if an existing one was reused.
        created_flow: True if this call created the flow; False if an existing one was reused.
    """

    workflow_name: str
    flow_name: str
    created_workflow: bool
    created_flow: bool


@dataclass
@PayloadRegistry.register
class EnsureWorkflowAndFlowResultFailure(ResultPayloadFailure):
    """EnsureWorkflowAndFlow failed. Common causes: could not push workflow context, flow creation rejected by the engine."""


@dataclass
@PayloadRegistry.register
class CurrentWorkflowChanged(AppPayload):
    """Current workflow switched notification.

    Emitted by ContextManager whenever the workflow it reports as current changes: opening
    a saved workflow, starting a scratch one, its first save (which rekeys it from
    "unsaved:<uuid>" to the key derived from its new path), a move, a clear, or a delete.
    It is the authoritative "you are now looking at a different workflow" signal, observed
    by every attached client -- including one that did not make the change itself, such as
    a second editor or an agent driving the engine over MCP.

    It reports which workflow, not whether its contents have finished loading:
    RunWorkflowFromRegistry switches the context before replaying the saved file, so this
    arrives first and the nodes follow as ordinary creation events. GetWorkflowContextRequest
    answers the same way, so it is not a readiness check either.

    One operation can move the context more than once, and the last event is the truth. An
    open while another workflow is open emits None (the wipe) then the new workflow; a failed
    open adds a trailing None, since the failure reverts to an empty engine. A request
    rejected before the switch -- an unknown registry key, an unsaved workflow with no file
    to replay -- leaves the context alone and emits nothing.

    Renaming the open saved workflow is the one surprise: the rename deletes the old registry
    entry while it is still in context, and that delete tears the context down, so this
    reports the teardown's result (None, or whatever was underneath on the stack) rather than
    the new name. Renaming an open workflow that was never saved goes through the inner save
    instead, so it reports the new key as expected.

    Args:
        workflow_name: Registry key of the workflow now in context, or None when the engine
            has no current workflow. A workflow that has never been saved reports its
            "unsaved:<uuid>" key. No default: None is a meaningful answer, so a field that
            never arrived must not be able to deserialize into it.
        is_saved: Whether that workflow is backed by a file on disk -- the same answer
            GetWorkflowContextRequest gives, so a client can drive save/dirty UI from this
            event alone. False for a workflow that has never been saved, None when none is
            open. No default, for the same reason as workflow_name.
    """

    workflow_name: str | None
    is_saved: bool | None
