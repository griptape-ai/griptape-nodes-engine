"""The workflows an engine has open."""

from __future__ import annotations

from typing import TYPE_CHECKING

from griptape_nodes.retained_mode.beta_features import MULTIPLE_OPEN_WORKFLOWS, is_beta_enabled
from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine


class OpenWorkflowError(Exception):
    """An open-workflow operation that cannot be carried out."""


class NoOpenWorkflowError(OpenWorkflowError):
    """Work that needs the open workflow found none open."""


class OpenWorkflowSet:
    """The runtimes of the workflows the engine has open, by workflow id.

    One workflow is open at a time, or several with the `multiple_open_workflows` beta feature on.
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
            OpenWorkflowError: That workflow, or another one, is already open.
        """
        if workflow_id in self._by_id:
            msg = f"Attempted to open workflow '{workflow_id}'. Failed because it is already open."
            raise OpenWorkflowError(msg)
        blocking = self.workflow_blocking_open()
        if blocking is not None:
            reason = self.several_open_refusal()
            if reason is None:
                reason = f"workflow '{blocking.workflow_id}' is already open."
            msg = f"Attempted to open workflow '{workflow_id}'. Failed because {reason}"
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

    def workflow_blocking_open(self) -> WorkflowRuntime | None:
        """The open workflow that keeps another from opening, or None when another can open.

        Reads the `multiple_open_workflows` beta feature on every call, so turning it off closes
        nothing and refuses the next open.
        """
        if not self._by_id:
            return None
        if is_beta_enabled(MULTIPLE_OPEN_WORKFLOWS, self._engine.config_manager):
            return None
        return next(iter(self._by_id.values()))

    def several_open_refusal(self) -> str | None:
        """Why an open is refused while several are open and only one may be, or None otherwise.

        Phrased to follow "Failed because". Several are open only when the `multiple_open_workflows`
        beta feature was turned off after they opened, and closing just the blocking one would not
        make room.
        """
        if self.workflow_blocking_open() is None or len(self._by_id) < 2:  # noqa: PLR2004
            return None
        return f"{len(self._by_id)} workflows are open and only one can be. Close all but one first."

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
