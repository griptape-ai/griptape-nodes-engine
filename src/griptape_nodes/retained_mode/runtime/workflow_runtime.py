"""The runtime of one open workflow."""

from __future__ import annotations

from typing import TYPE_CHECKING

from griptape_nodes.retained_mode.events.app_events import OpenWorkflowState, WorkflowRunState
from griptape_nodes.retained_mode.events.base_events import build_execution_event
from griptape_nodes.retained_mode.runtime.binding import bound_workflow
from griptape_nodes.retained_mode.runtime.context import WorkflowContextStack
from griptape_nodes.retained_mode.runtime.flows import WorkflowFlows
from griptape_nodes.retained_mode.runtime.nodes import WorkflowNodes
from griptape_nodes.retained_mode.runtime.objects import WorkflowObjects
from griptape_nodes.retained_mode.runtime.variables import WorkflowVariables

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import (
        ExecutionGriptapeNodeEvent,
        ExecutionPayload,
        RequestPayload,
        ResultPayload,
    )


class WorkflowRuntime:
    """The state of one open workflow, made when it opens and dropped when it closes.

    Holds the workflow's context stack (the flows, nodes and elements entered in it), its objects
    (its flows and nodes by name, and their queued UI changes), its node bookkeeping, its flow
    hierarchy, connections and run state, and its flow and global variables, one typed attribute per
    concern.
    """

    def __init__(self, workflow_id: str, engine: Engine) -> None:
        """Make an empty runtime.

        Args:
            workflow_id: The registry id of the workflow this runtime holds open, which never changes.
            engine: The engine holding this runtime. Its requests go through that engine's dispatch,
                and nodes in it reach that engine's managers (`BaseNode.engine`).
        """
        self.workflow_id = workflow_id
        self.engine = engine
        # Set when it closes. Work still bound to it is refused rather than acting on a dead runtime.
        self.closed = False
        self.context = WorkflowContextStack()
        self.objects = WorkflowObjects()
        self.nodes = WorkflowNodes()
        self.flows = WorkflowFlows(self)
        self.variables = WorkflowVariables()
        # Set by a request that changes the workflow, cleared when the workflow's own file is written.
        self.has_unsaved_changes = False
        # Why the latest run failed, cleared when the next starts.
        self.last_run_error: str | None = None
        # The state clients were last told of, so `Engine.report_workflow_state` tells them only of a change.
        self.reported_state: OpenWorkflowState | None = None

    def state(self) -> OpenWorkflowState:
        """Its unsaved changes and runs, as clients are told of them."""
        run_state = WorkflowRunState.IDLE
        if self.flows.run_in_progress is not None:
            run_state = WorkflowRunState.RUNNING
        return OpenWorkflowState(
            has_unsaved_changes=self.has_unsaved_changes,
            run_state=run_state,
            queued_run_count=len(self.flows.run_queue.snapshot()),
            last_run_error=self.last_run_error,
        )

    def handle_request(self, request: RequestPayload) -> ResultPayload:
        """Handle a request bound to this workflow. Refused once it has closed, never sent to another."""
        token = bound_workflow.set(self)
        try:
            return self.engine.handle_request(request)
        finally:
            bound_workflow.reset(token)

    async def ahandle_request(self, request: RequestPayload) -> ResultPayload:
        """Handle a request bound to this workflow. Refused once it has closed, never sent to another."""
        token = bound_workflow.set(self)
        try:
            return await self.engine.ahandle_request(request)
        finally:
            bound_workflow.reset(token)

    def execution_event(self, payload: ExecutionPayload) -> ExecutionGriptapeNodeEvent:
        """An execution event that belongs to this workflow."""
        return build_execution_event(payload, self.workflow_id)
