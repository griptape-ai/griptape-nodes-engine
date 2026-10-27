"""The runtime of one open workflow."""

from __future__ import annotations

from griptape_nodes.retained_mode.runtime.flows import WorkflowFlows
from griptape_nodes.retained_mode.runtime.nodes import WorkflowNodes
from griptape_nodes.retained_mode.runtime.objects import WorkflowObjects
from griptape_nodes.retained_mode.runtime.variables import WorkflowVariables


class WorkflowRuntime:
    """The state of one open workflow, made when it opens and dropped when it closes.

    Holds the workflow's objects (its flows and nodes by name), its node bookkeeping, its flow
    hierarchy, connections and run state, and its flow and global variables, one typed attribute
    per concern.
    """

    def __init__(self, workflow_id: str) -> None:
        """Make an empty runtime.

        Args:
            workflow_id: The registry id of the workflow this runtime holds open. Saving an open
                library workflow moves it onto the saved copy's id (see `OpenWorkflowSet.move`).
        """
        self.workflow_id = workflow_id
        # Set when it closes. Work still bound to it is refused rather than acting on a dead runtime.
        self.closed = False
        self.objects = WorkflowObjects()
        self.nodes = WorkflowNodes()
        self.flows = WorkflowFlows()
        self.variables = WorkflowVariables()
