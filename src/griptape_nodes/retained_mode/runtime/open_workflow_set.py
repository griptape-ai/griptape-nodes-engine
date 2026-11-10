"""The workflows an engine has open."""

from __future__ import annotations

from typing import TYPE_CHECKING

from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine


class OpenWorkflowError(Exception):
    """An open-workflow operation that cannot be carried out."""


class NoOpenWorkflowError(OpenWorkflowError):
    """Work that needs the open workflow found none open."""


class OpenWorkflowSet:
    """The runtimes of the workflows the engine has open, by workflow id.

    Reads never open anything: only `open` adds a runtime. `Engine.open_workflow` and
    `Engine.close_workflow` are the operations that open and close one.
    """

    def __init__(self, engine: Engine) -> None:
        """Make an empty set.

        Args:
            engine: The engine holding the runtimes, handed to each one.
        """
        self._engine = engine
        self._by_id: dict[str, WorkflowRuntime] = {}

    def open(self, workflow_id: str) -> WorkflowRuntime:
        """Open the workflow with this id and return its empty runtime.

        Raises:
            OpenWorkflowError: That workflow is already open.
        """
        if workflow_id in self._by_id:
            msg = f"Attempted to open workflow '{workflow_id}'. Failed because it is already open."
            raise OpenWorkflowError(msg)

        workflow = WorkflowRuntime(workflow_id, self._engine)
        self._by_id[workflow_id] = workflow
        return workflow

    def close(self, workflow: WorkflowRuntime) -> None:
        """Forget an open workflow and mark it closed. Call `Engine.close_workflow`, which tears it down first.

        Raises:
            OpenWorkflowError: That runtime is not the one open under its id.
        """
        if self._by_id.get(workflow.workflow_id) is not workflow:
            msg = f"Attempted to close workflow '{workflow.workflow_id}'. Failed because it is not open."
            raise OpenWorkflowError(msg)
        del self._by_id[workflow.workflow_id]
        workflow.closed = True

    def is_open(self, workflow_id: str) -> bool:
        return workflow_id in self._by_id

    def find(self, workflow_id: str) -> WorkflowRuntime | None:
        """The runtime of the open workflow with this id, or None."""
        return self._by_id.get(workflow_id)

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

    def all_open(self) -> list[WorkflowRuntime]:
        """Every open workflow's runtime, in the order they were opened."""
        return list(self._by_id.values())
