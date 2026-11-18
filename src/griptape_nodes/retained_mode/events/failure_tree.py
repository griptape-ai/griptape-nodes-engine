"""What failed, and where: the structure of a node failure, as data.

A leaf names the node that failed and carries the ``NodeErrorDetails`` for it. A container (a group,
a loop, a Workflow node) names the node that failed because something inside it did, and holds what
failed inside.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from griptape_nodes.retained_mode.events.node_error_details import NodeErrorDetails


class FailureKind(StrEnum):
    """Why a node counts as failed."""

    RAISED = "raised"
    VALIDATION = "validation"
    BUDGET_HALT = "budget_halt"
    ENGINE = "engine"


@dataclass(frozen=True)
class FailureLeaf:
    """A node that failed, with the details of its failure."""

    # Not hashable: ``NodeErrorDetails`` is not.
    __hash__ = None  # type: ignore[assignment]

    node_name: str
    kind: FailureKind
    details: NodeErrorDetails


@dataclass(frozen=True)
class WorkflowNodeFailure:
    """A Workflow node that failed because a node in the workflow it runs did."""

    # Not hashable: ``inner`` holds details.
    __hash__ = None  # type: ignore[assignment]

    node_name: str
    inner: FailureTree


@dataclass(frozen=True)
class GroupFailure:
    """A node group that failed because a node inside it did."""

    # Not hashable: ``inner`` holds details.
    __hash__ = None  # type: ignore[assignment]

    node_name: str
    inner: FailureTree


@dataclass(frozen=True)
class IterationRange:
    """A run of loop iterations, counted from 1, that failed for the same reason."""

    first: int
    last: int


@dataclass(frozen=True)
class LoopReason:
    """One reason a loop lost iterations, and the iterations it cost."""

    # Not hashable: ``failure_tree`` holds details.
    __hash__ = None  # type: ignore[assignment]

    iterations: tuple[IterationRange, ...]
    detail: str
    failure_tree: FailureTree | None


@dataclass(frozen=True)
class LoopFailure:
    """A loop that failed because some of its iterations did.

    ``reasons`` holds one entry per distinct failure text, in the order each first appeared, capped
    at the number a loop's message names. ``omitted_reasons`` and ``omitted_iterations`` count what
    the cap left out.
    """

    # Not hashable: the reasons hold details.
    __hash__ = None  # type: ignore[assignment]

    node_name: str
    reasons: tuple[LoopReason, ...]
    total_iterations: int
    failed_iterations: int
    omitted_reasons: int = 0
    omitted_iterations: int = 0


FailureTree = FailureLeaf | WorkflowNodeFailure | GroupFailure | LoopFailure


class FailureTreeError(RuntimeError):
    """A node's failure tree, raised with the message its raise site composes."""

    def __init__(self, failure_tree: FailureTree, message: str) -> None:
        super().__init__(message)
        self.failure_tree = failure_tree


class ExecuteNodeFailureError(FailureTreeError):
    """Raised by ``NodeExecutor`` when an ``ExecuteNodeRequest`` fails.

    The message is the flattened text that ends up in ``NodeErrorEvent.error_message``. The node's
    exception, if any, is chained as ``__cause__``. Unlike any other exception, its text already names
    the node, so the run adds no "encountered a problem" prefix to it. ``details`` are the details the
    client of the request was given, when the failure has them.
    """

    def __init__(self, failure_tree: FailureTree, message: str, details: NodeErrorDetails | None = None) -> None:
        super().__init__(failure_tree, message)
        self.details = details


class CarriesFailureTree:
    """A result that can hold, within this process only, the failure tree behind it.

    Not a dataclass field: no converter serializes it, so a result received from another process has none,
    and ``failure_trees.failure_tree_from_result`` rebuilds it from the result's error. ``dataclasses.replace``,
    and anything else that rebuilds a result from its fields, drops the tree, so attach it again.
    """

    failure_tree: FailureTree | None = None

    def attach_failure_tree(self, failure_tree: FailureTree | None) -> Self:
        """Attach the failure tree to this result, in place, and return the result for chaining."""
        self.failure_tree = failure_tree
        return self
