"""The runtimes a worker keeps for the workflows its orchestrator has open."""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from typing import TYPE_CHECKING

from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.retained_mode.runtime.workflow_runtime import RequestDispatcher


class WorkerWorkflowSet:
    """A worker's runtimes for its orchestrator's open workflows, by the orchestrator's workflow id.

    A worker opens no workflow of its own. It keeps an ordinary `WorkflowRuntime` for each
    orchestrator workflow it runs nodes for, so their context, paths and imports resolve as they do
    in an open workflow. These are not open workflows: listings, the heartbeat and dispatch by
    envelope id read `OpenWorkflowSet` only, and a worker's node-execution request binds the one its
    node belongs to while the node runs (`running`).
    """

    def __init__(self, dispatcher: RequestDispatcher) -> None:
        """Make an empty set.

        Args:
            dispatcher: The engine's request dispatch, handed to each runtime for the requests it sends.
        """
        self._dispatcher = dispatcher
        self._by_id: dict[str, WorkflowRuntime] = {}
        self._running_nodes: Counter[str] = Counter()

    def runtime_for(self, workflow_id: str) -> WorkflowRuntime:
        """The runtime kept for the orchestrator workflow with this id, made if there is none yet."""
        workflow = self._by_id.get(workflow_id)
        if workflow is None:
            workflow = WorkflowRuntime(workflow_id, self._dispatcher)
            self._by_id[workflow_id] = workflow
        return workflow

    def find(self, workflow_id: str) -> WorkflowRuntime | None:
        """The runtime kept for the orchestrator workflow with this id, or None."""
        return self._by_id.get(workflow_id)

    def all_kept(self) -> list[WorkflowRuntime]:
        """Every runtime kept, in the order they were made."""
        return list(self._by_id.values())

    def holds(self, workflow_id: str) -> bool:
        return workflow_id in self._by_id

    @contextmanager
    def running(self, workflow: WorkflowRuntime) -> Iterator[None]:
        """Mark a node as running in `workflow` for the block, so `idle` leaves it out."""
        self._running_nodes[workflow.workflow_id] += 1
        try:
            yield
        finally:
            self._running_nodes[workflow.workflow_id] -= 1
            if self._running_nodes[workflow.workflow_id] <= 0:
                del self._running_nodes[workflow.workflow_id]

    def is_running(self, workflow: WorkflowRuntime) -> bool:
        """Whether a node is running in `workflow`."""
        return workflow.workflow_id in self._running_nodes

    def close(self, workflow: WorkflowRuntime) -> bool:
        """Forget a runtime and mark it closed, and return whether it was held. Does nothing if it is already gone."""
        if self._by_id.get(workflow.workflow_id) is not workflow:
            return False
        del self._by_id[workflow.workflow_id]
        workflow.closed = True
        return True

    def idle(self) -> list[WorkflowRuntime]:
        """The runtimes no node is running in, in the order they were made."""
        return [workflow for workflow_id, workflow in self._by_id.items() if workflow_id not in self._running_nodes]
