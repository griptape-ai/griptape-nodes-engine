"""The runtime of one open workflow."""

from __future__ import annotations

from typing import TYPE_CHECKING

from griptape_nodes.exe_types.flow import ControlFlow
from griptape_nodes.retained_mode.events.app_events import OpenWorkflowInfo, OpenWorkflowState, WorkflowRunState
from griptape_nodes.retained_mode.events.base_events import build_execution_event
from griptape_nodes.retained_mode.runtime.binding import bound_workflow
from griptape_nodes.retained_mode.runtime.context import WorkflowContextStack
from griptape_nodes.retained_mode.runtime.flows import WorkflowFlows
from griptape_nodes.retained_mode.runtime.nodes import WorkflowNodes
from griptape_nodes.retained_mode.runtime.objects import WorkflowObjects
from griptape_nodes.retained_mode.runtime.undo_history import UndoHistory
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

    Holds the flow and node blocks callers entered in it, its objects
    (its flows and nodes by name, and their queued UI changes), its node bookkeeping, its flow
    hierarchy, connections and run state, its flow and global variables, and its undo history, one
    typed attribute per concern.
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
        self.context = WorkflowContextStack(self.top_level_flow)
        self.objects = WorkflowObjects()
        self.nodes = WorkflowNodes()
        self.flows = WorkflowFlows(self)
        self.variables = WorkflowVariables()
        # Set by a request that changes the workflow, cleared when the workflow's own file is written.
        self.has_unsaved_changes = False
        # Why the latest run failed, cleared when the next starts.
        self.last_run_error: str | None = None
        self.undo = UndoHistory()
        # The state clients were last told of, so `Engine.report_workflow_state` tells them only of a change.
        self.reported_state: OpenWorkflowState | None = None
        # The details clients were last told of, so `Engine.report_workflow_details` tells them only of a change.
        self.reported_details: OpenWorkflowInfo | None = None

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
            undo_label=self.undo.undo_label,
            redo_label=self.undo.redo_label,
        )

    def top_level_flow(self) -> ControlFlow | None:
        """The workflow's top-level flow, the one with no parent, if it has one."""
        for flow_name, parent_name in self.flows.name_to_parent_name.items():
            if parent_name is None:
                return self.objects.get_as_type(flow_name, ControlFlow)
        return None

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
