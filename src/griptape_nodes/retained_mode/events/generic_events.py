from dataclasses import dataclass, field

from griptape_nodes.retained_mode.events.app_events import OpenWorkflowInfo
from griptape_nodes.retained_mode.events.base_events import (
    ResultPayloadFailure,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry


@dataclass
@PayloadRegistry.register
class GenericResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Failure result not tied to a specific request type.

    Returned when the dispatcher must fail a request before (or instead of) its
    manager callback runs, so there is no request-specific failure type to use --
    for example when a pre-dispatch hook raises. Unlike the abstract
    ``ResultPayloadFailure`` base, this is concrete and registered, so it
    round-trips through ``PayloadRegistry`` on the worker-forward path instead of
    raising "result_type is not registered".
    """


@dataclass
@PayloadRegistry.register
class WorkflowNotOpenResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """The request's envelope named a workflow the engine doesn't have open, so it was not handled.

    The one failure for every request whose envelope `open_workflow_id` names no open workflow,
    whatever the request, so a client can tell it apart and reopen the workflow or pick another.
    HOST requests (`RequestScope`) ignore the id, so they never fail with it.

    Args:
        open_workflows: Every workflow the engine has open, as ListOpenWorkflowsRequest lists them.
    """

    open_workflows: list[OpenWorkflowInfo] = field(default_factory=list)


@dataclass
@PayloadRegistry.register
class WorkflowNotNamedResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """The request names no workflow it can act on, so it did nothing.

    The one failure for every request that acts on a workflow (`RequestScope.WORKFLOW`) but names
    none on its envelope, and for `EnsureWorkflowAndFlowRequest` naming none while several are
    open. A client subscribed to the request's own failure type still receives it:
    the result envelope carries `request_type`, and the bridge routes on that.

    Args:
        open_workflows: Every workflow the engine has open, as ListOpenWorkflowsRequest lists them.
    """

    open_workflows: list[OpenWorkflowInfo] = field(default_factory=list)
