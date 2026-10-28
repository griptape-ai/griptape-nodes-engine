"""The runtime of one open workflow."""

from __future__ import annotations


class WorkflowRuntime:
    """One open workflow, made when it opens and dropped when it closes."""

    def __init__(self, workflow_id: str) -> None:
        """Make an empty runtime.

        Args:
            workflow_id: The registry id of the workflow this runtime holds open. Saving an open
                library workflow moves it onto the saved copy's id (see `OpenWorkflowSet.move`).
        """
        self.workflow_id = workflow_id
        # Set when it closes. Work still bound to it is refused rather than acting on a dead runtime.
        self.closed = False
