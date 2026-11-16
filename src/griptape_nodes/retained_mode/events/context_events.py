from dataclasses import dataclass

from griptape_nodes.retained_mode.events.app_events import OpenWorkflowInfo
from griptape_nodes.retained_mode.events.base_events import (
    GraphAccessMixin,
    RequestPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    WorkflowAlteredMixin,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry
from griptape_nodes.retained_mode.events.workflow_events import WorkflowStatus


@dataclass
@PayloadRegistry.register
class SetWorkflowContextRequest(RequestPayload):
    """Set the current workflow context.

    Use when: Switching between workflows, initializing workflow sessions,
    setting the active workflow for subsequent operations, workflow navigation.

    Args:
        workflow_name: Name of the workflow to set as current context. When None,
                       the handler registers a new unsaved workflow, named
                       "unsaved:<workflow id>". A name the registry does not hold fails,
                       "unsaved:" names included. So does a workflow with a file that isn't
                       open, since this request doesn't load its nodes: open it with
                       OpenWorkflowRequest.
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

    Ignores the envelope's `open_workflow_id`.

    Results: SetWorkflowContextSuccess (carries the resolved workflow_name and workflow_id) |
             SetWorkflowContextFailure (workflow not found or already open, working_directory
             names an existing non-directory)
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
                       request omitted `workflow_name`, this is the new unsaved workflow's
                       "unsaved:<workflow id>" key. Otherwise it echoes the requested key.
        workflow_id: Id of the workflow now in context.
        open_workflow: The workflow open after the request, as `ListOpenWorkflowsRequest` lists it.
    """

    workflow_name: str = ""
    workflow_id: str | None = None
    open_workflow: OpenWorkflowInfo | None = None


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
                  no context is set.
        workflow_id: Id of the current workflow (None if no context set).
    """

    workflow_name: str | None
    is_saved: bool | None = None
    workflow_id: str | None = None


@dataclass
@PayloadRegistry.register
class GetWorkflowContextFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Workflow context retrieval failed. Common causes: context not initialized, system error."""


@dataclass
@PayloadRegistry.register
class CloseWorkflowRequest(RequestPayload):
    """Close the open workflow `workflow_id` names, leaving other open workflows alone.

    Use when: A client is done with a workflow, such as when its tab closes. Deletes the workflow's
    flows and nodes and ends its run. Its saved file, if any, stays as it is. Ignores the
    envelope's `open_workflow_id`. ClearAllObjectStateRequest closes every open workflow instead.

    A workflow that is not open succeeds with `already_closed`. The engine forgets a closed unsaved
    workflow's id, so an id it never had and one that already closed get the same answer.

    Args:
        workflow_id: Id of the workflow to close.

    Results: CloseWorkflowResultSuccess | CloseWorkflowResultFailure (closing the workflow failed,
        so it stays open)
    """

    workflow_id: str


@dataclass
@PayloadRegistry.register
class CloseWorkflowResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """The workflow is closed. A close leaves nothing unsaved, so it never marks a workflow changed.

    Args:
        workflow_id: Id of the workflow the request named.
        already_closed: Whether that workflow was not open, so the request closed nothing.
    """

    workflow_id: str
    already_closed: bool


@dataclass
@PayloadRegistry.register
class CloseWorkflowResultFailure(ResultPayloadFailure):
    """Closing the workflow failed, so it stays open."""


@dataclass
@PayloadRegistry.register
class OpenWorkflowRequest(RequestPayload):
    """Open a workflow, or get it back if it is already open. Never closes a workflow.

    Use when: A client opens a workflow in a tab, reopens the one it had after reconnecting, or
    starts a new one. Name the workflow by `workflow_id` or by `workflow_name`, not both; name
    neither to start a new unsaved workflow. To switch the workflow in a tab, close the open one
    with CloseWorkflowRequest first. Ignores the envelope's `open_workflow_id`.

    Args:
        workflow_id: Id of the workflow to open, as ListAllWorkflowsRequest or
            ListOpenWorkflowsRequest report it.
        workflow_name: Registry name of the workflow to open.
        display_name: Name of a new unsaved workflow. Ignored when a workflow is named.
        working_directory: Folder the workflow belongs to until it is saved, as
            SetWorkflowContextRequest describes. Ignored when the workflow is already open.
        create_flow: Whether to give the workflow its top-level flow when it has none once opened,
            which a new workflow needs before it can be saved. A saved workflow's file builds its own.

    A workflow already open comes back as it is, with `already_open` set, and nothing changes. One
    that another request is still opening comes back once its file has built. A saved workflow
    opens and its file builds its graph into it. A Griptape-provided workflow never opens itself:
    each open of one opens a new unsaved copy with an id of its own and the library's file built
    into it. A new or unsaved workflow opens empty.

    Results: OpenWorkflowResultSuccess | WorkflowNotFoundResultFailure (no workflow has that id or
        name, such as an unsaved workflow that was closed or lost to an engine restart) |
        InvalidWorkingDirectoryResultFailure (working_directory names a file) |
        OpenWorkflowResultFailure (both an id and a name, the file failed to build)
    """

    workflow_id: str | None = None
    workflow_name: str | None = None
    display_name: str | None = None
    working_directory: str | None = None
    create_flow: bool = False


@dataclass
@PayloadRegistry.register
class OpenWorkflowResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """The workflow is open.

    Clients get one `WorkflowOpenedEvent` for a workflow, with the first result that reports it open
    and built, so a result with `already_open` set usually sends none.

    Args:
        open_workflow: The workflow, as ListOpenWorkflowsRequest lists it.
        already_open: Whether it was open before the request, which then changed nothing.
        status: Fitness of the load of a saved workflow's file. FLAWED means the graph is on the
            canvas with placeholders standing in for nodes whose library could not be registered.
            GOOD for a workflow with no file, or one already open.
    """

    open_workflow: OpenWorkflowInfo
    already_open: bool
    status: WorkflowStatus = WorkflowStatus.GOOD


@dataclass
@PayloadRegistry.register
class OpenWorkflowResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """The workflow did not open, and no workflow opened or closed."""


@dataclass
@PayloadRegistry.register
class WorkflowNotFoundResultFailure(OpenWorkflowResultFailure):
    """No workflow has the id or name OpenWorkflowRequest named.

    An unsaved workflow's id names nothing once it closes or the engine restarts, so a client that
    gets this for one has lost that workflow.
    """


@dataclass
@PayloadRegistry.register
class InvalidWorkingDirectoryResultFailure(OpenWorkflowResultFailure):
    """OpenWorkflowRequest was refused because its `working_directory` names a file, not a folder.

    Only a new workflow takes a `working_directory`, so a client can send the request again without
    one to open the workflow with the default folder.
    """


@dataclass
@PayloadRegistry.register
class ListOpenWorkflowsRequest(RequestPayload):
    """List the workflows the engine has open.

    Use when: A client starts or reconnects and needs to know which workflows are open, for
    example to find the one it had open before. The request ignores the envelope's `open_workflow_id`.

    Results: ListOpenWorkflowsResultSuccess | ListOpenWorkflowsResultFailure (engine error)
    """


@dataclass
@PayloadRegistry.register
class ListOpenWorkflowsResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """The workflows the engine has open.

    Args:
        open_workflows: Every open workflow, in the order they were opened. One still opening is
            left out until it has finished.
        engine_run_id: Changes each time the engine restarts. Workflow ids last only until a
            restart, so drop the ids you kept when it changes.
    """

    open_workflows: list[OpenWorkflowInfo]
    engine_run_id: str


@dataclass
@PayloadRegistry.register
class ListOpenWorkflowsResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Listing the open workflows failed."""


@dataclass
@PayloadRegistry.register
class EnsureWorkflowAndFlowRequest(RequestPayload, GraphAccessMixin):
    """Ensure a workflow + flow context exists, creating scratch ones if needed.

    Use when: Bootstrapping from a cold engine state. This is the typical opening call from
    an MCP client that is about to build a workflow from scratch. Idempotent: if both a
    workflow and a flow are already in the current context, returns their names without
    creating new ones. Only creates the pieces that are missing.

    Args:
        workflow_name: Registered workflow to enter if no workflow is in context. Ignored
            when a workflow is already in context. When None, the engine registers a new
            unsaved workflow and returns its key in the success result. A name the registry
            does not hold fails; pass display_name to name a new workflow. So does a workflow
            with a file that isn't open, since this request doesn't load its nodes: open it
            with OpenWorkflowRequest.
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
        workflow_id: Id of the workflow in the current context.
        open_workflow: The workflow open after the request, as `ListOpenWorkflowsRequest` lists it: the
            one it used or opened.
    """

    workflow_name: str
    flow_name: str
    created_workflow: bool
    created_flow: bool
    workflow_id: str | None = None
    open_workflow: OpenWorkflowInfo | None = None


@dataclass
@PayloadRegistry.register
class EnsureWorkflowAndFlowResultFailure(ResultPayloadFailure):
    """EnsureWorkflowAndFlow failed. Common causes: could not push workflow context, flow creation rejected by the engine."""
