"""Build and render ``FailureTree`` values, and word the failures a loop reports.

Every place the engine learns that a node failed builds its ``FailureTree`` here, so the choice of kind and
the shape of the tree are made once. The renderers below produce the text that
``NodeErrorEvent.error_message`` and the run's recorded message carry.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from itertools import islice
from typing import TYPE_CHECKING, Any, NamedTuple, TypeGuard, assert_never

from griptape_nodes.retained_mode.events.base_events import ForwardedException
from griptape_nodes.retained_mode.events.execution_events import (
    ExecuteNodeResultFailure,
    StartLocalSubflowResultFailure,
)
from griptape_nodes.retained_mode.events.failure_tree import (
    ExecuteNodeFailureError,
    FailureKind,
    FailureLeaf,
    FailureTree,
    FailureTreeError,
    GroupFailure,
    IterationRange,
    LoopFailure,
    LoopReason,
    WorkflowNodeFailure,
)
from griptape_nodes.retained_mode.events.node_error_details import (
    NodeErrorDetails,
    build_engine_error_details,
    build_node_error_details,
)
from griptape_nodes.utils.budget_refusal import BUDGET_EXCEEDED_TYPE_NAME, BudgetExceededError

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

# How many distinct failure reasons a loop error message names before deferring to the log.
MAX_REPORTED_ITERATION_FAILURES = 5

# How many individual iteration numbers to name before switching to "(+N more)". Only reached when
# the failed iterations are not a contiguous run, which is already summarised as "first-last".
MAX_ENUMERATED_ITERATION_NUMBERS = 6

# The field a Workflow node's failure lists the nodes between it and the one that failed in.
FAILED_NODE_PATH_FIELD = "Failed node path"
FAILED_NODE_PATH_SEPARATOR = " \u203a "
# A path longer than this is shown with its middle left out, so the field stays readable.
MAX_PATH_ENTRIES_SHOWN = 6
PATH_ENTRIES_KEPT_AT_EACH_END = 2


@dataclass(frozen=True)
class IterationFailure:
    """One loop iteration that did not finish, with the reason it gave.

    ``iteration_index`` is zero-based to match the executor's internal indexing; everything shown to an artist
    adds one, because a loop's first pass is iteration 1 to the person who built it. ``failure_tree`` is what
    failed inside the iteration, when the iteration said.
    """

    # Not hashable: ``failure_tree`` holds details.
    __hash__ = None  # type: ignore[assignment]

    iteration_index: int
    detail: str
    failure_tree: FailureTree | None = None


@dataclass(frozen=True)
class IterationOutcome:
    """What one parallel loop iteration did, as reported back by its task.

    A bool would be enough to route the iteration, but not enough to explain it: the reason a parallel
    iteration failed is only available inside the task that ran it, so the verdict and the reason have to
    travel together or the reason is lost.
    """

    # Not hashable: ``failure_tree`` holds details.
    __hash__ = None  # type: ignore[assignment]

    iteration_index: int
    succeeded: bool
    detail: str
    failure_tree: FailureTree | None = None


@dataclass(frozen=True)
class _NestedReport:
    """What a Workflow node reports about the failure inside it."""

    # Not hashable: ``blamed_node_error_details`` and the path hold details.
    __hash__ = None  # type: ignore[assignment]

    path: list[FailureTree]
    """The nodes below the Workflow node, down to the one it names as having failed."""

    blamed_node_error_details: NodeErrorDetails
    """The error details of the node the failure is blamed on, at the end of the path: the node that failed,
    or a loop whose iterations failed for different reasons, where no single one can be followed further.
    The Workflow node reports these as its own."""

    message: str
    """What the Workflow node says about the failure: the node that failed, how deep, and its message."""


# --- Builders ---


def failure_tree_from_exception(
    node_name: str, exc: BaseException, *, budget_halt: BaseException | None = None
) -> FailureTree:
    """Classify the exception a node raised.

    ``budget_halt`` is the halt the caller recognised behind the exception, if any.
    """
    # The caller already recognised a budget halt behind this exception, so the leaf is the engine's wording of that
    # halt, not the exception's own text.
    if budget_halt is not None:
        details = build_engine_error_details(node_name, str(budget_halt), budget_halt)
        return FailureLeaf(node_name=node_name, kind=FailureKind.BUDGET_HALT, details=details)
    if isinstance(exc, FailureTreeError) and exc.failure_tree.node_name == node_name:
        return exc.failure_tree
    # A FailureTreeError for another node is just text to this one, as a plain RuntimeError would be.
    details = build_node_error_details(node_name, _as_plain_exception(exc))
    return FailureLeaf(node_name=node_name, kind=FailureKind.RAISED, details=details)


def failure_leaf_from_validation(node_name: str, exceptions: list[Exception]) -> FailureLeaf:
    """The failure of a node that declined to run, for the reasons it gave."""
    details = build_node_error_details(node_name, exceptions)
    return FailureLeaf(node_name=node_name, kind=FailureKind.VALIDATION, details=details)


def failure_leaf_from_engine_failure(node_name: str, message: str, cause: BaseException | None) -> FailureLeaf:
    """The failure of a node the engine described in its own words, such as one whose worker stopped."""
    details = build_engine_error_details(node_name, message, cause)
    return FailureLeaf(node_name=node_name, kind=FailureKind.ENGINE, details=details)


def failure_tree_from_result(node_name: str, result: Any) -> FailureTree:
    """The failure a failed result reports: its own, else its details, else the engine's words."""
    # The result carries the tree built where the node failed, which is more exact than one rebuilt here.
    if isinstance(result, ExecuteNodeResultFailure) and result.failure_tree is not None:
        return result.failure_tree
    # A result received from another process has no tree, but its exception still says it was a budget halt, and the
    # engine's own wording of the halt, not the node's details, is what describes it.
    exception = getattr(result, "exception", None)
    if _is_budget_halt_exception(exception):
        details = build_engine_error_details(node_name, str(getattr(result, "result_details", result)), exception)
        return FailureLeaf(node_name=node_name, kind=FailureKind.BUDGET_HALT, details=details)
    if isinstance(result, ExecuteNodeResultFailure) and result.error is not None:
        kind = FailureKind.RAISED
        if result.validation_exceptions:
            kind = FailureKind.VALIDATION
        return FailureLeaf(node_name=node_name, kind=kind, details=result.error)
    # The result has neither a tree nor details (such as a worker that stopped), so the failure is described in
    # the engine's own words.
    return failure_leaf_from_engine_failure(node_name, str(getattr(result, "result_details", result)), exception)


def execute_node_failure_error_from_result(
    node_name: str, result: Any, exc: Exception | None
) -> ExecuteNodeFailureError:
    """The error for a failed ``ExecuteNodeRequest``.

    Its message is ``format_node_failure_message``'s text. Its ``failure_tree`` is the failure the result reports,
    and ``details`` is the result's ``error`` when it has one, otherwise rendered from that tree.
    """
    message = format_node_failure_message(node_name, result, exc)
    failure_tree = failure_tree_from_result(node_name, result)
    # The details the node built where it failed are kept, since they hold its fields and links.
    if isinstance(result, ExecuteNodeResultFailure) and result.error is not None:
        details = result.error
    else:
        details = render_error_details(
            failure_tree, text=str(getattr(result, "result_details", result)), node_name=node_name
        )
    return ExecuteNodeFailureError(failure_tree, message, details)


def loop_failure_from_iterations(
    loop_name: str, total_iterations: int, iteration_failures: list[IterationFailure]
) -> LoopFailure:
    """The failure of a loop that lost iterations, with one reason per distinct detail text."""
    summary = _summarize_reasons(iteration_failures, MAX_REPORTED_ITERATION_FAILURES)
    return LoopFailure(
        node_name=loop_name,
        reasons=_loop_reasons(summary),
        total_iterations=total_iterations,
        failed_iterations=len(iteration_failures),
        omitted_reasons=summary.omitted_reasons,
        omitted_iterations=summary.omitted_iterations,
    )


def renamed(failure_tree: FailureTree, names: Mapping[str, str]) -> FailureTree:
    """The failure with each node renamed by ``names``, leaving alone any a mapping doesn't name.

    A Workflow node is renamed but not what is inside it, because that is another flow's nodes.
    """
    node_name = names.get(failure_tree.node_name, failure_tree.node_name)
    if isinstance(failure_tree, FailureLeaf):
        return replace(failure_tree, node_name=node_name)
    if isinstance(failure_tree, WorkflowNodeFailure):
        return replace(failure_tree, node_name=node_name)
    # A group's inner failure is renamed too, unlike a Workflow node's, because the group's nodes belong to the same flow.
    if isinstance(failure_tree, GroupFailure):
        return replace(failure_tree, node_name=node_name, inner=renamed(failure_tree.inner, names))
    if isinstance(failure_tree, LoopFailure):
        reasons = tuple(
            replace(reason, failure_tree=_renamed_or_none(reason.failure_tree, names))
            for reason in failure_tree.reasons
        )
        return replace(failure_tree, node_name=node_name, reasons=reasons)
    assert_never(failure_tree)


def failure_tree_from_subflow_result(result: Any, names: Mapping[str, str]) -> FailureTree | None:
    """What a failed subflow result says failed inside it, renamed by ``names``, or None when it says nothing."""
    if not isinstance(result, StartLocalSubflowResultFailure) or result.failure_tree is None:
        return None
    return renamed(result.failure_tree, names)


def error_details_from_exception(exc: BaseException, node_name: str) -> NodeErrorDetails:
    """The details for ``NodeErrorEvent.error`` of the node whose task raised ``exc``."""
    # The failure already carries the details built where the node failed. A copy is returned so the event cannot
    # alter the error's own.
    if isinstance(exc, ExecuteNodeFailureError) and exc.details is not None:
        return copy.deepcopy(exc.details)
    if isinstance(exc, FailureTreeError):
        return render_error_details(exc.failure_tree, text=str(exc), node_name=node_name)
    return build_node_error_details(node_name, exc)


# --- Renderers ---


def render_error_details(failure_tree: FailureTree, *, text: str, node_name: str) -> NodeErrorDetails:
    """A copy of the details of a leaf, or the details a container reports, from its ``text``.

    A Workflow node reports the node that failed inside it, with its type, fields, response and links.
    Any other container failed because something inside it did, and reports the text its raise point
    gave it as the message of a ``builtins.RuntimeError``. ``text`` is ignored for a leaf, which
    already has its details.
    """
    match failure_tree:
        # A leaf already has its details, so a copy is returned rather than text-only details built from ``text``.
        case FailureLeaf():
            return copy.deepcopy(failure_tree.details)
        case WorkflowNodeFailure():
            return _nested_workflow_details(failure_tree)
        # A group or loop has no details of its own, so it reports the text its raise point gave it.
        case GroupFailure() | LoopFailure():
            return build_node_error_details(node_name, RuntimeError(text))
        case _:
            assert_never(failure_tree)


def nested_workflow_message(failure_tree: WorkflowNodeFailure) -> str:
    """What a Workflow node says when something inside it failed.

    The message names the node that failed and how many nested Workflow nodes it was inside, and
    leads with that node's own message.
    """
    return _workflow_node_nested_report(failure_tree).message


def _nested_workflow_details(failure_tree: WorkflowNodeFailure) -> NodeErrorDetails:
    """The details a Workflow node reports for the failure inside it.

    They are those of the node at the end of the path, with the nodes on the path added as a field. The
    message is the Workflow node's own, except for a budget halt, whose wording leads. The validation
    ``messages`` are not kept: they tell the editor a node never ran, which the Workflow node did.
    """
    report = _workflow_node_nested_report(failure_tree)
    message = report.message
    # A budget halt's own wording leads, so the run stops with the same message as a halt at the top level.
    if _is_budget_halt(report.path[-1]):
        message = report.blamed_node_error_details.message
    fields = dict(report.blamed_node_error_details.fields)
    fields[FAILED_NODE_PATH_FIELD] = _format_path([step.node_name for step in report.path])
    return replace(copy.deepcopy(report.blamed_node_error_details), message=message, messages=None, fields=fields)


def budget_halt_behind_workflow_node(failure_tree: FailureTree) -> str | None:
    """The wording of the budget halt a Workflow node's failure ends at, or None when it ends at anything else."""
    # A halt at the top level is already worded as the halt; only one under Workflow nodes needs its wording brought
    # up.
    if not isinstance(failure_tree, WorkflowNodeFailure):
        return None
    end = _failure_path(failure_tree)[-1]
    if not _is_budget_halt(end):
        return None
    return end.details.message


def iteration_failure_detail(failure_tree: FailureTree | None, result_details: str) -> str:
    """Describe a failed iteration by its Workflow node's nested message when that failed, else by ``result_details``.

    A failure inside a nested Workflow node is described by its nested message, which names the node that
    failed, with a frame for each group it sits in. Any other failure keeps the generic text.
    """
    group_names: list[str] = []
    current = failure_tree
    # Each group the Workflow node sits in failed because of it, so their names are collected, outermost first.
    while isinstance(current, GroupFailure):
        group_names.append(current.node_name)
        current = current.inner
    # Only a failed Workflow node has a nested message; any other failure keeps the iteration's own text.
    if not isinstance(current, WorkflowNodeFailure):
        return result_details
    message = nested_workflow_message(current)
    # Each group adds the frame it raises with, wrapping innermost first so the outermost group leads.
    for group_name in reversed(group_names):
        message = encountered_a_problem_message(group_name, message)
    return message


def encountered_a_problem_message(node_name: str, text: str) -> str:
    """The message for a node that failed with ``text`` but does not name itself in it."""
    return f"Node '{node_name}' encountered a problem: {text}"


def format_node_failure_message(node_name: str, result: Any, exc: BaseException | None) -> str:
    """Compose the RuntimeError message for a failed node execution.

    When the worker rebuilt a ``ForwardedException``, surface its ``original_type`` and ``original_traceback``
    directly in the message. Chaining via ``from exc`` is not enough on its own: a ForwardedException is
    constructed (not raised) on the receiving side, so its ``__traceback__`` is None and Python's
    chained-exception display prints only the cause's ``Type: message`` line with no frames. Interpolating
    ``original_traceback`` here is what actually puts the worker frames in front of the user.
    """
    # A node that declined to run did not fail while running, and saying so sends the reader looking
    # for a crash that never happened. Matched on the type rather than on the attribute's presence:
    # `result` is typed `Any` here, and anything at all answers a `getattr`.
    if isinstance(result, ExecuteNodeResultFailure) and result.validation_exceptions:
        reasons = "; ".join(str(exception) for exception in result.validation_exceptions)
        return f"Node '{node_name}' did not run because it failed validation: {reasons}"

    type_prefix = ""
    tb_suffix = ""
    # Only an exception rebuilt from a worker carries the worker's type and traceback.
    if isinstance(exc, ForwardedException):
        if exc.original_type:
            type_prefix = f"[{exc.original_type}] "
        # The worker's frames are the only traceback there is, because the node raised in another process.
        if exc.original_traceback:
            tb_suffix = f"\nWorker traceback:\n{exc.original_traceback}"
    return f"Node '{node_name}' execution failed: {type_prefix}{getattr(result, 'result_details', result)}{tb_suffix}"


def render_loop_message(loop: LoopFailure) -> str:
    """Compose the RuntimeError message for a loop that lost iterations.

    This lands on the artist's node via the execution machine, so it leads with what was attempted and how
    much was lost, then names the iterations and their reasons.
    """
    summary = (
        f"Attempted to run all {loop.total_iterations} iterations of loop '{loop.node_name}'. "
        f"Failed because {loop.failed_iterations} of them did not finish."
    )
    detail_lines = _loop_lines(
        loop.reasons,
        total_iterations=loop.total_iterations,
        omitted_reasons=loop.omitted_reasons,
        omitted_iterations=loop.omitted_iterations,
    )
    if not detail_lines:
        return summary
    return "\n".join([summary, *detail_lines])


def format_iteration_failure_lines(
    iteration_failures: list[IterationFailure],
    *,
    total_iterations: int | None = None,
    max_lines: int = MAX_REPORTED_ITERATION_FAILURES,
) -> list[str]:
    """Render one indented line per distinct failure reason.

    Iterations that failed for the same reason share a line: the common case is every iteration failing
    identically, and repeating one sentence dozens of times buries it. At most ``max_lines`` reasons are
    rendered so a long loop cannot produce an unreadable wall of text; the engine log has already recorded
    every iteration individually, so the tail line points there.

    ``total_iterations`` is optional only because the parallel path logs these lines without a summary above
    them; pass it whenever it is known so a wholly-failed loop can say so.
    """
    summary = _summarize_reasons(iteration_failures, max_lines)
    return _loop_lines(
        _loop_reasons(summary),
        total_iterations=total_iterations,
        omitted_reasons=summary.omitted_reasons,
        omitted_iterations=summary.omitted_iterations,
    )


# --- Helpers ---


class _ReasonGroup(NamedTuple):
    """The iterations of a loop that failed with the same detail text."""

    detail: str
    failures: list[IterationFailure]


class _ReasonSummary(NamedTuple):
    """The reasons a loop reports, and how many reasons and iterations fall beyond the cap."""

    reported: list[_ReasonGroup]
    omitted_reasons: int
    omitted_iterations: int


def _summarize_reasons(iteration_failures: list[IterationFailure], max_reasons: int) -> _ReasonSummary:
    """The first ``max_reasons`` distinct reasons, in the order they first appear, and what is left over."""
    groups = [_ReasonGroup(detail, failures) for detail, failures in _group_by_detail(iteration_failures).items()]
    unreported = groups[max_reasons:]
    return _ReasonSummary(
        reported=groups[:max_reasons],
        omitted_reasons=len(unreported),
        omitted_iterations=sum(len(group.failures) for group in unreported),
    )


def _group_by_detail(iteration_failures: list[IterationFailure]) -> dict[str, list[IterationFailure]]:
    """The failures sharing each detail text, in the order the texts first appear."""
    groups: dict[str, list[IterationFailure]] = {}
    for iteration_failure in iteration_failures:
        groups.setdefault(iteration_failure.detail, []).append(iteration_failure)
    return groups


def _loop_reasons(summary: _ReasonSummary) -> tuple[LoopReason, ...]:
    """One reason per reported group, naming its iterations from 1 and carrying its first iteration's failure."""
    reasons = []
    for group in summary.reported:
        numbers = sorted(failure.iteration_index + 1 for failure in group.failures)
        reasons.append(
            LoopReason(
                iterations=_ranges_of(numbers),
                detail=group.detail,
                failure_tree=_first_failure_tree(group.failures),
            )
        )
    return tuple(reasons)


def _first_failure_tree(failures: list[IterationFailure]) -> FailureTree | None:
    for iteration_failure in failures:
        # Iterations that did not say what failed are skipped, so the reason carries the first failure tree there is.
        if iteration_failure.failure_tree is not None:
            return iteration_failure.failure_tree
    return None


def _ranges_of(sorted_numbers: list[int]) -> tuple[IterationRange, ...]:
    """Collapse ascending iteration numbers into runs of consecutive ones."""
    ranges: list[IterationRange] = []
    for number in sorted_numbers:
        if ranges and ranges[-1].last + 1 == number:
            ranges[-1] = IterationRange(first=ranges[-1].first, last=number)
            continue
        ranges.append(IterationRange(first=number, last=number))
    return tuple(ranges)


def _loop_lines(
    reasons: tuple[LoopReason, ...],
    *,
    total_iterations: int | None,
    omitted_reasons: int,
    omitted_iterations: int,
) -> list[str]:
    """One indented line per reason, then a tail line counting what the cap left out."""
    lines = [f"  {_describe_iterations(reason.iterations, total_iterations)}: {reason.detail}" for reason in reasons]
    if omitted_reasons:
        # Count the iterations behind the omitted reasons, not just the reasons: the lines above are phrased
        # in iterations, so a bare reason count reads as one and can undersell the tail by two orders of
        # magnitude.
        lines.append(
            f"  ... and {omitted_reasons} more reason(s) affecting {omitted_iterations} iteration(s). "
            f"See the engine log for every iteration."
        )
    return lines


def _describe_iterations(ranges: tuple[IterationRange, ...], total_iterations: int | None) -> str:
    """Name a group of failed iterations in a handful of characters, however many there are.

    Capping the number of *reasons* is not enough on its own: a 500-iteration loop that fails identically
    every time collapses to a single line, and enumerating all 500 numbers pushes the reason -- the part worth
    reading -- kilobytes to the right.
    """
    count = sum(iteration_range.last - iteration_range.first + 1 for iteration_range in ranges)

    # Naming the single iteration comes first: "Every iteration" is only more informative than a number when
    # there is more than one, and a one-item loop satisfies both tests.
    if count == 1:
        return f"Iteration {ranges[0].first}"
    # Every iteration of the loop failed this way, so listing their numbers would add nothing.
    if total_iterations is not None and count >= total_iterations:
        return "Every iteration"

    if len(ranges) == 1:
        return f"Iterations {ranges[0].first}-{ranges[0].last}"

    shown = ", ".join(str(number) for number in islice(_numbers_in(ranges), MAX_ENUMERATED_ITERATION_NUMBERS))
    if count <= MAX_ENUMERATED_ITERATION_NUMBERS:
        return f"Iterations {shown}"
    return f"Iterations {shown} (+{count - MAX_ENUMERATED_ITERATION_NUMBERS} more)"


def _numbers_in(ranges: tuple[IterationRange, ...]) -> Iterator[int]:
    for iteration_range in ranges:
        yield from range(iteration_range.first, iteration_range.last + 1)


def _workflow_node_nested_report(failure_tree: WorkflowNodeFailure) -> _NestedReport:
    path = _failure_path(failure_tree)[1:]
    blamed_node_error_details = _blamed_node_error_details(path[-1])
    depth = _workflow_depth(path)
    message = _nested_message(failure_tree.node_name, path, blamed_node_error_details, depth)
    return _NestedReport(path=path, blamed_node_error_details=blamed_node_error_details, message=message)


def _is_budget_halt(failure_tree: FailureTree) -> TypeGuard[FailureLeaf]:
    return isinstance(failure_tree, FailureLeaf) and failure_tree.kind is FailureKind.BUDGET_HALT


def _failure_path(failure_tree: FailureTree) -> list[FailureTree]:
    """The failures from this one down to the node that failed.

    The path continues through a Workflow node or group to what failed inside it, and through a loop
    whose iterations failed for exactly one reason that says what failed. It ends at a leaf, and at a
    loop whose iterations failed for several reasons or for one that says nothing more, because no one
    node explains such a loop.
    """
    path = [failure_tree]
    current = failure_tree
    while True:
        child = _reported_child(current)
        if child is None:
            return path
        path.append(child)
        current = child


def _reported_child(failure_tree: FailureTree) -> FailureTree | None:
    # A leaf has nothing inside it. A Workflow node or group fails because of its inner failure. A loop is explained
    # by its reason when it has exactly one.
    match failure_tree:
        case FailureLeaf():
            return None
        case WorkflowNodeFailure() | GroupFailure():
            return failure_tree.inner
        case LoopFailure():
            # A single reason says which failure explains the loop. With several, no one node does, so the path ends at
            # the loop.
            if len(failure_tree.reasons) == 1:
                return failure_tree.reasons[0].failure_tree
            return None
        case _:
            assert_never(failure_tree)


def _workflow_depth(path: list[FailureTree]) -> int:
    """How many Workflow nodes a path of failures passes through."""
    return sum(1 for step in path if isinstance(step, WorkflowNodeFailure))


def _blamed_node_error_details(end: FailureTree) -> NodeErrorDetails:
    """The error details of the node the failure path ends at; a loop is worded by its own summary."""
    # A leaf has details of its own. A loop has none, so its summary is the message.
    match end:
        case FailureLeaf():
            return end.details
        case LoopFailure():
            return build_node_error_details(end.node_name, RuntimeError(render_loop_message(end)))
        case WorkflowNodeFailure() | GroupFailure():
            # The path continues through a Workflow node or group, so it cannot end at one.
            msg = "a failure path never ends at a node that holds an inner failure"
            raise AssertionError(msg)
        case _:
            assert_never(end)


def _nested_message(
    workflow_node_name: str, path: list[FailureTree], blamed_node_error_details: NodeErrorDetails, depth: int
) -> str:
    failed_node = path[-1].node_name
    # The wording follows how many Workflow nodes the failure passed through below this one.
    if depth == 0:
        where = f"node '{failed_node}' inside it failed"
    elif depth == 1:
        where = f"node '{failed_node}', inside a nested Workflow node, failed"
    else:
        where = f"node '{failed_node}', inside {depth} nested Workflow nodes, failed"
    return f"Attempted to run the workflow behind node '{workflow_node_name}'. Failed because {where}: {blamed_node_error_details.message}"


def _format_path(names: list[str]) -> str:
    # A short path reads fine whole. A long one keeps only its ends, so the field stays short.
    if len(names) <= MAX_PATH_ENTRIES_SHOWN:
        return FAILED_NODE_PATH_SEPARATOR.join(names)
    kept = PATH_ENTRIES_KEPT_AT_EACH_END
    left_out = len(names) - 2 * kept
    shown = [*names[:kept], f"\u2026 ({left_out} more)", *names[-kept:]]
    return FAILED_NODE_PATH_SEPARATOR.join(shown)


def _renamed_or_none(failure_tree: FailureTree | None, names: Mapping[str, str]) -> FailureTree | None:
    if failure_tree is None:
        return None
    return renamed(failure_tree, names)


def _as_plain_exception(exc: BaseException) -> BaseException:
    """A FailureTreeError named for another node reads as the plain RuntimeError it is, text only."""
    # Only its text is kept, so the tree and type of another node's failure do not carry over.
    if isinstance(exc, FailureTreeError):
        return RuntimeError(str(exc))
    return exc


def _is_budget_halt_exception(exception: BaseException | None) -> bool:
    """Whether the exception is a budget halt, or the placeholder a worker sent for one."""
    if isinstance(exception, BudgetExceededError):
        return True
    return isinstance(exception, ForwardedException) and exception.original_type == BUDGET_EXCEEDED_TYPE_NAME
