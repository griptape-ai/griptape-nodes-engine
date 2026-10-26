"""The runtime of one open workflow."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from griptape_nodes.retained_mode.events.base_events import ExecutionEvent, ExecutionGriptapeNodeEvent
from griptape_nodes.retained_mode.runtime.context import WorkflowContextStack
from griptape_nodes.retained_mode.runtime.flows import WorkflowFlows
from griptape_nodes.retained_mode.runtime.nodes import WorkflowNodes
from griptape_nodes.retained_mode.runtime.objects import WorkflowObjects
from griptape_nodes.retained_mode.runtime.variables import WorkflowVariables

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.events.base_events import ExecutionPayload, RequestPayload, ResultPayload


class RequestDispatcher(Protocol):
    """Hands a request to the engine's handlers as if its envelope named `workflow_id`."""

    def handle_request(self, request: RequestPayload, *, workflow_id: str | None = None) -> ResultPayload: ...

    async def ahandle_request(self, request: RequestPayload, *, workflow_id: str | None = None) -> ResultPayload: ...


class WorkflowRuntime:
    """The state of one open workflow, made when it opens and dropped when it closes.

    Holds the workflow's context stack (the flows, nodes and elements entered in it), its objects
    (its flows and nodes by name), its node bookkeeping, its flow hierarchy and connections, and its
    flow and global variables, one typed attribute per concern. Its name, file and folder are on its
    registry entry, read by `workflow_id`.
    """

    def __init__(self, workflow_id: str, dispatcher: RequestDispatcher) -> None:
        """Make an empty runtime.

        Args:
            workflow_id: The registry id of the workflow this runtime holds open.
            dispatcher: The engine's request dispatch, which this runtime's requests go through.
        """
        self.workflow_id = workflow_id
        self._dispatcher = dispatcher
        self.context = WorkflowContextStack()
        self.objects = WorkflowObjects()
        self.nodes = WorkflowNodes()
        self.flows = WorkflowFlows()
        self.variables = WorkflowVariables()

    def handle_request(self, request: RequestPayload) -> ResultPayload:
        """Handle a request in this workflow, as one whose envelope names it."""
        return self._dispatcher.handle_request(request, workflow_id=self.workflow_id)

    async def ahandle_request(self, request: RequestPayload) -> ResultPayload:
        """Handle a request in this workflow, as one whose envelope names it."""
        return await self._dispatcher.ahandle_request(request, workflow_id=self.workflow_id)

    def execution_event(self, payload: ExecutionPayload) -> ExecutionGriptapeNodeEvent:
        """An execution event that belongs to this workflow."""
        return ExecutionGriptapeNodeEvent(wrapped_event=ExecutionEvent(payload=payload, workflow_id=self.workflow_id))
