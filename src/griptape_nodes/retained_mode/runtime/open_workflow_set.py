"""The workflows an engine has open."""

from __future__ import annotations

from typing import TYPE_CHECKING

from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.retained_mode.runtime.workflow_runtime import RequestDispatcher


class OpenWorkflowError(Exception):
    """An open-workflow operation that cannot be carried out."""


class NoOpenWorkflowError(OpenWorkflowError):
    """Work that needs the open workflow found none open."""


class OpenWorkflowSet:
    """The runtimes of the workflows the engine has open, by workflow id.

    One workflow is open at a time. Reads never open anything: only `open` adds a runtime.
    """

    def __init__(self, dispatcher: RequestDispatcher) -> None:
        """Make an empty set.

        Args:
            dispatcher: The engine's request dispatch, handed to each runtime for the requests it sends.
        """
        self._dispatcher = dispatcher
        self._by_id: dict[str, WorkflowRuntime] = {}

    def __iter__(self) -> Iterator[WorkflowRuntime]:
        """The open workflows' runtimes, in the order they were opened."""
        return iter(list(self._by_id.values()))

    def open(self, workflow_id: str) -> WorkflowRuntime:
        """Open the workflow with this id and return its empty runtime.

        Raises:
            OpenWorkflowError: That workflow, or another one, is already open.
        """
        if workflow_id in self._by_id:
            msg = f"Attempted to open workflow '{workflow_id}'. Failed because it is already open."
            raise OpenWorkflowError(msg)
        if self._by_id:
            open_id = next(iter(self._by_id))
            msg = f"Attempted to open workflow '{workflow_id}'. Failed because workflow '{open_id}' is already open."
            raise OpenWorkflowError(msg)

        workflow = WorkflowRuntime(workflow_id, self._dispatcher)
        self._by_id[workflow_id] = workflow
        return workflow

    def close(self, workflow: WorkflowRuntime) -> None:
        """Forget an open workflow. Tearing down its state is the caller's job.

        Raises:
            OpenWorkflowError: That runtime is not the one open under its id.
        """
        if self._by_id.get(workflow.workflow_id) is not workflow:
            msg = f"Attempted to close workflow '{workflow.workflow_id}'. Failed because it is not open."
            raise OpenWorkflowError(msg)
        del self._by_id[workflow.workflow_id]

    def is_open(self, workflow_id: str) -> bool:
        return workflow_id in self._by_id

    def get(self, workflow_id: str) -> WorkflowRuntime:
        """The runtime of the open workflow with this id.

        Raises:
            OpenWorkflowError: No open workflow has that id.
        """
        workflow = self._by_id.get(workflow_id)
        if workflow is None:
            msg = f"Attempted to use workflow '{workflow_id}'. Failed because it is not open."
            raise OpenWorkflowError(msg)
        return workflow

    def find_by_id(self, workflow_id: str) -> WorkflowRuntime | None:
        """The runtime of the open workflow with this id, or None when that workflow isn't open."""
        return self._by_id.get(workflow_id)

    def sole_workflow(self) -> WorkflowRuntime:
        """The one open workflow, for work that needs a workflow but is not handed one.

        Exists until handlers receive their workflow.

        Raises:
            NoOpenWorkflowError: No workflow is open.
            OpenWorkflowError: More than one workflow is open.
        """
        workflow = self.sole_workflow_or_none()
        if workflow is None:
            msg = "Attempted to change the open workflow. Failed because no workflow is open. Open or create a workflow first."
            raise NoOpenWorkflowError(msg)
        return workflow

    def sole_workflow_or_none(self) -> WorkflowRuntime | None:
        """The one open workflow, or None when none is open, for reads that find nothing without one.

        Exists until handlers receive their workflow. Never picks one of several.

        Raises:
            OpenWorkflowError: More than one workflow is open.
        """
        if len(self._by_id) > 1:
            msg = f"Attempted to find the open workflow. Failed because {len(self._by_id)} workflows are open."
            raise OpenWorkflowError(msg)
        return next(iter(self._by_id.values()), None)
