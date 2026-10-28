"""The workflows an engine has open."""

from __future__ import annotations

from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime


class OpenWorkflowError(Exception):
    """An open-workflow operation that cannot be carried out."""


class NoOpenWorkflowError(OpenWorkflowError):
    """Work that needs the open workflow found none open."""


class OpenWorkflowSet:
    """The runtimes of the workflows the engine has open, by workflow id.

    One workflow is open at a time. Reads never open anything: only `open` adds a runtime.
    `Engine.open_workflow` and `Engine.close_workflow` are the operations that open and close one.
    """

    def __init__(self, *, allow_several_open: bool = False) -> None:
        """Make an empty set.

        Args:
            allow_several_open: Test seam that lets a workflow open beside another. The engine never
                sets it: it opens one workflow at a time.
        """
        self._by_id: dict[str, WorkflowRuntime] = {}
        self._allow_several_open = allow_several_open

    def open(self, workflow_id: str) -> WorkflowRuntime:
        """Open the workflow with this id and return its empty runtime.

        Raises:
            OpenWorkflowError: That workflow, or another one, is already open.
        """
        if workflow_id in self._by_id:
            msg = f"Attempted to open workflow '{workflow_id}'. Failed because it is already open."
            raise OpenWorkflowError(msg)
        blocking = self.workflow_blocking_open()
        if blocking is not None:
            msg = (
                f"Attempted to open workflow '{workflow_id}'. Failed because workflow '{blocking.workflow_id}' "
                "is already open."
            )
            raise OpenWorkflowError(msg)

        workflow = WorkflowRuntime(workflow_id)
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

    def move(self, workflow: WorkflowRuntime, workflow_id: str) -> None:
        """Keep an open workflow open under another id, as when saving it writes a copy it continues in.

        The old id stops naming it: an open workflow has exactly one id.

        Raises:
            OpenWorkflowError: That runtime is not open, or another workflow is open under the new id.
        """
        if self._by_id.get(workflow.workflow_id) is not workflow:
            msg = (
                f"Attempted to move workflow '{workflow.workflow_id}' to '{workflow_id}'. "
                "Failed because it is not open."
            )
            raise OpenWorkflowError(msg)
        if workflow_id in self._by_id:
            msg = (
                f"Attempted to move workflow '{workflow.workflow_id}' to '{workflow_id}'. "
                f"Failed because workflow '{workflow_id}' is already open."
            )
            raise OpenWorkflowError(msg)

        del self._by_id[workflow.workflow_id]
        workflow.workflow_id = workflow_id
        self._by_id[workflow_id] = workflow

    def workflow_blocking_open(self) -> WorkflowRuntime | None:
        """The open workflow that keeps another from opening: the one open, when only one may be."""
        if self._allow_several_open or not self._by_id:
            return None
        return next(iter(self._by_id.values()))

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
