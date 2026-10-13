"""Exceptions that carry a failure up through the Workflow nodes that run it.

``WorkflowRecursionLimitError`` marks the refusal to import a workflow that sits inside itself too
deeply. ``SubflowNodeFailedError`` names the node that failed inside a subflow and what it raised.
``NestedWorkflowNodeError`` is what a Workflow node raises for either, so each level of nesting adds
one step to a path instead of wrapping the message again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from griptape_nodes.exe_types.node_error import NodeError

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.events.node_error_details import NodeErrorDetails


class WorkflowRecursionLimitError(RuntimeError):
    """A workflow was refused because it already contains itself as deep as the recursion limit allows.

    It is not a ``NodeError``: the engine raises it while importing, not a node while running, and it
    has no fields or links to show.
    """


class SubflowNodeFailedError(RuntimeError):
    """The first node to fail inside a subflow, the details of its failure, and the exception behind it."""

    def __init__(self, node_name: str, details: NodeErrorDetails, cause: BaseException | None) -> None:
        super().__init__(details.message)
        self.node_name = node_name
        self.details = details
        self.cause = cause


class NestedWorkflowNodeError(NodeError):
    """A node inside a Workflow node failed, however many Workflow nodes down it sits.

    Carries the failing node's own details once, plus the names of the nodes from this Workflow node
    down to the failing one. A path of length 0 means this Workflow node itself could not load.
    ``workflow_depth`` counts the Workflow nodes below this one that contain the failing node; groups
    and loops on the path are not counted, and a failing node that is itself a Workflow node that
    could not load is not counted either.
    """

    PATH_FIELD = "Failed node path"
    PATH_SEPARATOR = " \u203a "
    # A path longer than this is shown with its middle left out, so the field stays readable.
    MAX_PATH_ENTRIES_SHOWN = 6
    PATH_ENTRIES_KEPT_AT_EACH_END = 2

    def __init__(
        self,
        workflow_node_name: str,
        *,
        root_details: NodeErrorDetails,
        node_path: list[str],
        root_cause: BaseException | None = None,
        workflow_depth: int = 0,
    ) -> None:
        fields: dict[str, str | int | float | bool] = dict(root_details.fields)
        if node_path:
            fields[self.PATH_FIELD] = self._format_path(node_path)
        super().__init__(
            self._build_message(workflow_node_name, root_details.message, node_path, workflow_depth),
            fields=fields,
            response=root_details.response,
            links=list(root_details.links),
        )
        self.workflow_node_name = workflow_node_name
        self.root_details = root_details
        self.node_path = node_path
        self.root_cause = root_cause
        self.workflow_depth = workflow_depth

    def inside(self, workflow_node_name: str) -> NestedWorkflowNodeError:
        """Return the same failure as seen from the Workflow node that runs the one that raised this."""
        return NestedWorkflowNodeError(
            workflow_node_name,
            root_details=self.root_details,
            node_path=[self.workflow_node_name, *self.node_path],
            root_cause=self.root_cause,
            workflow_depth=self.workflow_depth + self.workflow_nodes_containing_failure(),
        )

    def workflow_nodes_containing_failure(self) -> int:
        """Return 1 when this Workflow node runs the failing node (so it sits above it), else 0.

        A path of length 0 means this Workflow node is the one that failed to load, so it does not
        contain the failure.
        """
        if self.node_path:
            return 1
        return 0

    @classmethod
    def _format_path(cls, node_path: list[str]) -> str:
        if len(node_path) <= cls.MAX_PATH_ENTRIES_SHOWN:
            return cls.PATH_SEPARATOR.join(node_path)
        kept = cls.PATH_ENTRIES_KEPT_AT_EACH_END
        left_out = len(node_path) - 2 * kept
        shown = [*node_path[:kept], f"\u2026 ({left_out} more)", *node_path[-kept:]]
        return cls.PATH_SEPARATOR.join(shown)

    @staticmethod
    def _build_message(workflow_node_name: str, root_message: str, node_path: list[str], workflow_depth: int) -> str:
        if not node_path:
            return (
                f"Attempted to load the workflow behind node '{workflow_node_name}'. "
                f"Failed because the workflow could not be loaded: {root_message}"
            )
        failed_node = node_path[-1]
        if workflow_depth == 0:
            where = f"node '{failed_node}' inside it failed"
        elif workflow_depth == 1:
            where = f"node '{failed_node}', inside a nested Workflow node, failed"
        else:
            where = f"node '{failed_node}', inside {workflow_depth} nested Workflow nodes, failed"
        return (
            f"Attempted to run the workflow behind node '{workflow_node_name}'. Failed because {where}: {root_message}"
        )


def next_failure_link(cause: BaseException | None) -> SubflowNodeFailedError | NestedWorkflowNodeError | None:
    """Find the next subflow failure on a cause chain, or None when the chain ends at a plain exception."""
    seen: set[int] = set()
    current = cause
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, SubflowNodeFailedError | NestedWorkflowNodeError):
            return current
        current = current.__cause__
    return None


def is_recursion_refusal(failure: BaseException | None) -> bool:
    """Whether a failure is, or chains down to, the refusal to import a workflow nested too deeply."""
    seen: set[int] = set()
    current = failure
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, WorkflowRecursionLimitError):
            return True
        if isinstance(current, NestedWorkflowNodeError):
            current = current.root_cause
        elif isinstance(current, SubflowNodeFailedError):
            current = current.cause
        else:
            current = current.__cause__
    return False
