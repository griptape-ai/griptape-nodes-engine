"""The runtime of one open workflow."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from griptape_nodes.retained_mode.runtime.binding import bound_workflow
from griptape_nodes.retained_mode.runtime.context import WorkflowContextStack
from griptape_nodes.retained_mode.runtime.flows import WorkflowFlows
from griptape_nodes.retained_mode.runtime.loading import WorkflowLoading
from griptape_nodes.retained_mode.runtime.nodes import WorkflowNodes
from griptape_nodes.retained_mode.runtime.objects import WorkflowObjects
from griptape_nodes.retained_mode.runtime.variables import WorkflowVariables

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload


class RequestDispatcher(Protocol):
    """Hands a request to the engine's handlers, acting on whatever the caller has bound."""

    def handle_request(self, request: RequestPayload) -> ResultPayload: ...

    async def ahandle_request(self, request: RequestPayload) -> ResultPayload: ...


class WorkflowRuntime:
    """The state of one open workflow, made when it opens and dropped when it closes.

    Holds the workflow's context stack (the flows, nodes and elements entered in it), its objects
    (its flows and nodes by name, and their queued UI changes), its node bookkeeping, its flow
    hierarchy, connections and run state, its flow and global variables, and the referenced
    workflows it is importing, one typed attribute per concern.
    """

    def __init__(self, workflow_id: str, dispatcher: RequestDispatcher) -> None:
        """Make an empty runtime.

        Args:
            workflow_id: The registry id of the workflow this runtime holds open. Saving an open
                library workflow moves it onto the saved copy's id (see `OpenWorkflowSet.move`).
            dispatcher: The engine's request dispatch, which this runtime's requests go through.
        """
        self.workflow_id = workflow_id
        self._dispatcher = dispatcher
        # Set when it closes. Work still bound to it is refused rather than acting on a dead runtime.
        self.closed = False
        self.context = WorkflowContextStack()
        self.objects = WorkflowObjects()
        self.nodes = WorkflowNodes()
        self.flows = WorkflowFlows()
        self.variables = WorkflowVariables()
        self.loading = WorkflowLoading()

    def handle_request(self, request: RequestPayload) -> ResultPayload:
        """Handle a request bound to this workflow. Refused once it has closed, never sent to another."""
        token = bound_workflow.set(self)
        try:
            return self._dispatcher.handle_request(request)
        finally:
            bound_workflow.reset(token)

    async def ahandle_request(self, request: RequestPayload) -> ResultPayload:
        """Handle a request bound to this workflow. Refused once it has closed, never sent to another."""
        token = bound_workflow.set(self)
        try:
            return await self._dispatcher.ahandle_request(request)
        finally:
            bound_workflow.reset(token)
