"""Tests for building and rendering ``FailureTree`` values."""

from __future__ import annotations

import asyncio

import pytest

from griptape_nodes.common import failure_trees
from griptape_nodes.common.failure_trees import (
    FAILED_NODE_PATH_FIELD,
    FAILED_NODE_PATH_SEPARATOR,
    MAX_REPORTED_ITERATION_FAILURES,
    IterationFailure,
    IterationOutcome,
)
from griptape_nodes.exe_types.node_error import NodeError, NodeErrorLink
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
    WorkflowRecursionLimitError,
)
from griptape_nodes.retained_mode.events.node_error_details import NodeErrorDetails
from griptape_nodes.utils.budget_refusal import BudgetExceededError, BudgetRefusal


def _leaf(node_name: str, message: str = "boom", kind: FailureKind = FailureKind.RAISED) -> FailureLeaf:
    return FailureLeaf(node_name=node_name, kind=kind, details=NodeErrorDetails(message=message))


def _early(node_name: str, message: str = "too deep") -> FailureLeaf:
    return _leaf(node_name, message, kind=FailureKind.RECURSION_LIMIT)


def _leaf_of(failure_tree: FailureTree) -> FailureLeaf:
    assert isinstance(failure_tree, FailureLeaf)
    return failure_tree


def _loop_of(failure_tree: FailureTree) -> LoopFailure:
    assert isinstance(failure_tree, LoopFailure)
    return failure_tree


def _iteration_failures(details: list[str]) -> list[IterationFailure]:
    return [IterationFailure(iteration_index=index, detail=detail) for index, detail in enumerate(details)]


def _early_iteration(index: int, detail: str = "too deep") -> IterationFailure:
    return IterationFailure(iteration_index=index, detail=detail, failure_tree=_early("Inner", detail))


def _loop_message(total_iterations: int, iteration_failures: list[IterationFailure]) -> str:
    loop = failure_trees.loop_failure_from_iterations("Loop", total_iterations, iteration_failures)
    return failure_trees.render_loop_message(loop)


class TestFailureTreeFromException:
    def test_a_raised_exception_is_a_raised_leaf_with_the_nodes_details(self) -> None:
        leaf = _leaf_of(failure_trees.failure_tree_from_exception("Node", KeyError("Node: missing")))

        assert leaf.kind is FailureKind.RAISED
        assert leaf.node_name == "Node"
        assert leaf.details.exception_type == "builtins.KeyError"
        assert leaf.details.message == "missing"

    def test_node_error_attachments_are_kept(self) -> None:
        exc = NodeError("refused", fields={"request_id": "r-1"})

        leaf = _leaf_of(failure_trees.failure_tree_from_exception("Node", exc))

        assert leaf.details.fields == {"request_id": "r-1"}

    def test_a_forwarded_exception_keeps_the_type_it_had_in_the_worker(self) -> None:
        exc = ForwardedException("rebuilt", original_type="my_library.CustomError")

        leaf = _leaf_of(failure_trees.failure_tree_from_exception("Node", exc))

        assert leaf.kind is FailureKind.RAISED
        assert leaf.details.exception_type == "my_library.CustomError"

    def test_a_budget_halt_is_a_budget_halt_leaf_in_the_engines_words(self) -> None:
        halt = BudgetExceededError("Budget stopped this run.", BudgetRefusal(), node_name="Node")

        leaf = _leaf_of(failure_trees.failure_tree_from_exception("Node", RuntimeError("inner"), budget_halt=halt))

        assert leaf.kind is FailureKind.BUDGET_HALT
        assert leaf.details.message == "Budget stopped this run."
        assert leaf.details.exception_type == "griptape_nodes.utils.budget_refusal.BudgetExceededError"

    def test_a_failure_tree_error_for_this_node_is_its_failure_tree(self) -> None:
        carried = GroupFailure(node_name="Group", inner=_leaf("Child"))

        failure_tree = failure_trees.failure_tree_from_exception("Group", FailureTreeError(carried, "text"))

        assert failure_tree is carried

    def test_an_execute_node_failure_error_for_this_node_is_its_failure_tree(self) -> None:
        carried = _leaf("Node")

        failure_tree = failure_trees.failure_tree_from_exception("Node", ExecuteNodeFailureError(carried, "text"))

        assert failure_tree is carried

    def test_a_failure_tree_error_for_another_node_is_a_raised_leaf_of_its_text(self) -> None:
        carried = GroupFailure(node_name="Other", inner=_leaf("Child"))

        failure_tree = failure_trees.failure_tree_from_exception("Group", FailureTreeError(carried, "Group: the text"))

        assert failure_tree == FailureLeaf(
            node_name="Group",
            kind=FailureKind.RAISED,
            details=NodeErrorDetails(message="the text", exception_type="builtins.RuntimeError"),
        )

    def test_a_recursion_limit_error_is_a_recursion_limit_leaf_with_the_nodes_details(self) -> None:
        failure_tree = failure_trees.failure_tree_from_exception(
            "Inner Workflow", WorkflowRecursionLimitError("too deep")
        )

        assert isinstance(failure_tree, FailureLeaf)
        assert failure_tree.kind is FailureKind.RECURSION_LIMIT
        assert failure_tree.node_name == "Inner Workflow"
        assert failure_tree.details.message == "too deep"


class TestOtherBuilders:
    def test_validation_is_a_validation_leaf_with_one_message_per_exception(self) -> None:
        leaf = failure_trees.failure_leaf_from_validation("Node", [ValueError("a"), ValueError("b")])

        assert leaf.kind is FailureKind.VALIDATION
        assert leaf.details.messages == ["a", "b"]

    def test_an_engine_failure_keeps_the_engines_words_and_the_causes_type(self) -> None:
        leaf = failure_trees.failure_leaf_from_engine_failure("Node", "Node: no worker is available", KeyError("x"))

        assert leaf.kind is FailureKind.ENGINE
        assert leaf.details == NodeErrorDetails(message="no worker is available", exception_type="builtins.KeyError")


class TestFailureTreeFromResult:
    def test_the_results_own_failure_tree_comes_first(self) -> None:
        own = _leaf("Node", "own")
        result = ExecuteNodeResultFailure(
            result_details="words", error=NodeErrorDetails(message="error")
        ).attach_failure_tree(own)

        assert failure_trees.failure_tree_from_result("Node", result) is own

    def test_the_results_details_come_second(self) -> None:
        error = NodeErrorDetails(message="error", exception_type="builtins.KeyError")
        result = ExecuteNodeResultFailure(result_details="words", error=error)

        leaf = _leaf_of(failure_trees.failure_tree_from_result("Node", result))

        assert leaf.kind is FailureKind.RAISED
        assert leaf.details is error

    def test_a_result_that_declined_to_run_is_a_validation_failure(self) -> None:
        error = NodeErrorDetails(message="declined", messages=["declined"])
        result = ExecuteNodeResultFailure(
            result_details="words", error=error, validation_exceptions=[ValueError("declined")]
        )

        leaf = _leaf_of(failure_trees.failure_tree_from_result("Node", result))

        assert leaf.kind is FailureKind.VALIDATION

    def test_the_engines_words_come_last(self) -> None:
        result = ExecuteNodeResultFailure(result_details="Node: no worker is available", exception=KeyError("x"))

        leaf = _leaf_of(failure_trees.failure_tree_from_result("Node", result))

        assert leaf.kind is FailureKind.ENGINE
        assert leaf.details == NodeErrorDetails(message="no worker is available", exception_type="builtins.KeyError")


class TestErrorDetailsFromException:
    def test_an_execute_node_failure_error_gives_a_copy_of_the_leaf_details(self) -> None:
        carried = _leaf("Node", "built where the node failed")

        details = failure_trees.error_details_from_exception(ExecuteNodeFailureError(carried, "flattened"), "Node")

        assert details == carried.details
        assert details is not carried.details

    def test_a_container_gives_its_text_as_the_message_of_a_runtime_error(self) -> None:
        container = GroupFailure(node_name="Group", inner=_leaf("Child"))

        details = failure_trees.error_details_from_exception(FailureTreeError(container, "the text"), "Group")

        assert details == NodeErrorDetails(message="the text", exception_type="builtins.RuntimeError")

    def test_a_container_given_to_a_failed_result_gets_the_results_error(self) -> None:
        container = GroupFailure(node_name="Group", inner=_leaf("Child"))
        result = ExecuteNodeResultFailure(
            result_details="words", error=NodeErrorDetails(message="the child refused")
        ).attach_failure_tree(container)
        exc = failure_trees.execute_node_failure_error_from_result("Group", result, None)

        details = failure_trees.error_details_from_exception(exc, "Group")

        assert details == NodeErrorDetails(message="the child refused")
        assert details is not result.error
        assert str(exc) != details.message

    def test_a_container_given_to_a_failed_result_without_an_error_gets_the_results_text(self) -> None:
        container = GroupFailure(node_name="Group", inner=_leaf("Child"))
        result = ExecuteNodeResultFailure(result_details="Group: the child refused").attach_failure_tree(container)

        details = failure_trees.error_details_from_exception(
            failure_trees.execute_node_failure_error_from_result("Group", result, None), "Group"
        )

        assert details == NodeErrorDetails(message="the child refused", exception_type="builtins.RuntimeError")

    def test_an_exception_that_is_not_a_failure_is_built_from_itself(self) -> None:
        details = failure_trees.error_details_from_exception(ValueError("Node: bad"), "Node")

        assert details == NodeErrorDetails(message="bad", exception_type="builtins.ValueError")


class TestRenderErrorDetails:
    def test_copies_a_leaf_details(self) -> None:
        details = NodeErrorDetails(message="m", messages=["m"], fields={"a": "b"}, response={"k": {"n": 1}})
        leaf = FailureLeaf(node_name="Node", kind=FailureKind.RAISED, details=details)

        rendered = failure_trees.render_error_details(leaf, text="m", node_name="Node")
        rendered.messages.append("changed")  # type: ignore[union-attr]
        rendered.fields["c"] = "d"
        rendered.response["k"]["n"] = 2  # type: ignore[index]

        assert details == NodeErrorDetails(message="m", messages=["m"], fields={"a": "b"}, response={"k": {"n": 1}})

    def test_a_container_renders_its_text_stripped_of_the_nodes_name(self) -> None:
        container = GroupFailure(node_name="Group", inner=_leaf("Child"))

        rendered = failure_trees.render_error_details(container, text="Group: failed", node_name="Group")

        assert rendered == NodeErrorDetails(message="failed", exception_type="builtins.RuntimeError")


class TestExecuteNodeFailureErrorFromResult:
    def test_carries_the_results_failure_tree_in_the_flattened_text(self) -> None:
        own = _leaf("Node", "own")
        result = ExecuteNodeResultFailure(result_details="words").attach_failure_tree(own)

        error = failure_trees.execute_node_failure_error_from_result("Node", result, None)

        assert error.failure_tree is own
        assert str(error) == "Node 'Node' execution failed: words"


class TestFailureTreesAreNotHashable:
    def test_hashing_a_leaf_raises(self) -> None:
        with pytest.raises(TypeError):
            hash(_leaf("Node"))

    def test_hashing_a_container_raises(self) -> None:
        with pytest.raises(TypeError):
            hash(GroupFailure(node_name="Group", inner=_leaf("Child")))

    def test_hashing_a_loop_raises(self) -> None:
        with pytest.raises(TypeError):
            hash(failure_trees.loop_failure_from_iterations("Loop", 1, _iteration_failures(["a"])))


class TestFailureTreeFromSubflowResult:
    def test_renames_the_failure_tree_the_result_carries(self) -> None:
        result = StartLocalSubflowResultFailure(result_details="words").attach_failure_tree(_leaf("X_copy"))

        failure_tree = failure_trees.failure_tree_from_subflow_result(result, {"X_copy": "X"})

        assert failure_tree == _leaf("X")

    def test_a_result_without_a_failure_tree_gives_none(self) -> None:
        result = StartLocalSubflowResultFailure(result_details="Cannot start subflow")

        assert failure_trees.failure_tree_from_subflow_result(result, {"Loop": "Loop"}) is None

    def test_a_result_of_another_type_gives_none(self) -> None:
        assert (
            failure_trees.failure_tree_from_subflow_result(ExecuteNodeResultFailure(result_details="words"), {}) is None
        )

    def test_renames_the_failures_behind_a_loops_early_termination_and_cancelled_reasons(self) -> None:
        loop = LoopFailure(
            node_name="Loop_copy",
            reasons=(),
            total_iterations=2,
            failed_iterations=2,
            early_termination=LoopReason(
                iterations=(IterationRange(first=1, last=1),), detail="d", failure_tree=_early("X_copy")
            ),
            cancelled=LoopReason(
                iterations=(IterationRange(first=2, last=2),), detail="c", failure_tree=_leaf("Loop_copy")
            ),
        )

        result = _loop_of(failure_trees.renamed(loop, {"X_copy": "X", "Loop_copy": "Loop"}))

        assert result.node_name == "Loop"
        assert result.early_termination is not None
        assert result.early_termination.failure_tree == _early("X")
        assert result.cancelled is not None
        assert result.cancelled.failure_tree == _leaf("Loop")


class TestRenamed:
    def test_renames_the_failure_and_everything_inside_it(self) -> None:
        group = GroupFailure(node_name="G_copy", inner=_leaf("X_copy"))

        result = failure_trees.renamed(group, {"G_copy": "G", "X_copy": "X"})

        assert result == GroupFailure(node_name="G", inner=_leaf("X"))

    def test_renames_the_failures_behind_a_loops_reasons(self) -> None:
        iteration = IterationFailure(iteration_index=0, detail="d", failure_tree=_leaf("X_copy"))
        loop = failure_trees.loop_failure_from_iterations("Loop", 1, [iteration])

        result = _loop_of(failure_trees.renamed(loop, {"X_copy": "X"}))

        assert result.reasons[0].failure_tree == _leaf("X")

    def test_leaves_unmapped_names_alone(self) -> None:
        assert failure_trees.renamed(_leaf("Other"), {"X_copy": "X"}).node_name == "Other"

    def test_stops_at_a_workflow_node(self) -> None:
        workflow_node = WorkflowNodeFailure(node_name="W_copy", inner=_leaf("X_copy"))

        result = failure_trees.renamed(workflow_node, {"W_copy": "W", "X_copy": "X"})

        assert result == WorkflowNodeFailure(node_name="W", inner=_leaf("X_copy"))

    def test_does_not_change_the_original(self) -> None:
        group = GroupFailure(node_name="G_copy", inner=_leaf("X_copy"))

        failure_trees.renamed(group, {"G_copy": "G", "X_copy": "X"})

        assert group.node_name == "G_copy"


class TestLoopFailureFromIterations:
    def test_one_reason_per_distinct_detail(self) -> None:
        failures = _iteration_failures(["a", "b", "a"])

        result = failure_trees.loop_failure_from_iterations("Loop", 3, failures)

        assert result.node_name == "Loop"
        assert [reason.detail for reason in result.reasons] == ["a", "b"]
        assert result.total_iterations == 3  # noqa: PLR2004
        assert result.failed_iterations == 3  # noqa: PLR2004

    def test_iterations_are_counted_from_one_and_grouped_into_ranges(self) -> None:
        failures = [IterationFailure(iteration_index=index, detail="same") for index in (0, 1, 2, 5, 7, 8)]

        result = failure_trees.loop_failure_from_iterations("Loop", 10, failures)

        assert result.reasons[0].iterations == (
            IterationRange(first=1, last=3),
            IterationRange(first=6, last=6),
            IterationRange(first=8, last=9),
        )

    def test_reasons_are_capped_and_the_rest_counted(self) -> None:
        failures = _iteration_failures([f"reason {index}" for index in range(MAX_REPORTED_ITERATION_FAILURES)])
        failures += [IterationFailure(iteration_index=100 + index, detail="cut off") for index in range(4)]
        failures += [IterationFailure(iteration_index=200, detail="also cut off")]

        result = failure_trees.loop_failure_from_iterations("Loop", 300, failures)

        assert len(result.reasons) == MAX_REPORTED_ITERATION_FAILURES
        assert result.omitted_reasons == 2  # noqa: PLR2004
        assert result.omitted_iterations == 5  # noqa: PLR2004
        assert result.failed_iterations == len(failures)

    def test_nothing_is_omitted_at_the_cap(self) -> None:
        failures = _iteration_failures([f"reason {index}" for index in range(MAX_REPORTED_ITERATION_FAILURES)])

        result = failure_trees.loop_failure_from_iterations("Loop", 10, failures)

        assert result.omitted_reasons == 0
        assert result.omitted_iterations == 0

    def test_a_reason_carries_the_failure_tree_of_its_first_iteration_that_has_one(self) -> None:
        kept = _leaf("X", "kept")
        failures = [
            IterationFailure(iteration_index=0, detail="same"),
            IterationFailure(iteration_index=1, detail="same", failure_tree=kept),
            IterationFailure(iteration_index=2, detail="same", failure_tree=_leaf("X", "later")),
        ]

        result = failure_trees.loop_failure_from_iterations("Loop", 3, failures)

        assert result.reasons[0].failure_tree is kept

    def test_it_skips_failures_without_a_failure_tree_when_chaining_a_single_reason(self) -> None:
        kept = _leaf("X", "kept")
        failures = [
            IterationFailure(iteration_index=0, detail="same"),
            IterationFailure(iteration_index=1, detail="same", failure_tree=kept),
        ]

        result = failure_trees.loop_failure_from_iterations("Loop", 2, failures)

        assert result.reasons[0].failure_tree is kept

    def test_a_reason_without_a_failure_tree_has_none(self) -> None:
        result = failure_trees.loop_failure_from_iterations("Loop", 1, _iteration_failures(["a"]))

        assert result.reasons[0].failure_tree is None

    def test_it_carries_the_iterations_run(self) -> None:
        result = failure_trees.loop_failure_from_iterations("Loop", 3, _iteration_failures(["a"]), iterations_run=1)

        assert result.iterations_run == 1

    def test_an_early_termination_has_its_own_field_and_is_not_among_the_reasons(self) -> None:
        early = _early("Inner")
        failures = [
            IterationFailure(iteration_index=0, detail="ordinary", failure_tree=_leaf("Body", "ordinary")),
            IterationFailure(iteration_index=1, detail="too deep", failure_tree=early),
        ]

        result = failure_trees.loop_failure_from_iterations("Loop", 4, failures, iterations_run=2)

        assert [reason.detail for reason in result.reasons] == ["ordinary"]
        assert result.early_termination == LoopReason(
            iterations=(IterationRange(first=2, last=2),), detail="too deep", failure_tree=early
        )
        assert result.failed_iterations == 2  # noqa: PLR2004

    def test_an_early_termination_buried_in_a_workflow_node_has_its_own_field(self) -> None:
        buried = _workflow_node("Outer", _early("Inner"))
        failures = [
            IterationFailure(iteration_index=0, detail="ordinary", failure_tree=_leaf("Body")),
            IterationFailure(iteration_index=1, detail="refused", failure_tree=buried),
        ]

        result = failure_trees.loop_failure_from_iterations("Loop", 2, failures)

        assert result.early_termination is not None
        assert result.early_termination.detail == "refused"
        assert [reason.detail for reason in result.reasons] == ["ordinary"]

    def test_cancelled_iterations_have_their_own_field_named_after_the_loop(self) -> None:
        failures = [
            failure_trees.iteration_failure_from_cancellation("Loop", 0),
            IterationFailure(iteration_index=1, detail="refused", failure_tree=_early("Inner")),
            failure_trees.iteration_failure_from_cancellation("Loop", 2),
        ]

        result = failure_trees.loop_failure_from_iterations("Loop", 3, failures)

        assert result.reasons == ()
        assert result.cancelled is not None
        assert result.cancelled.iterations == (IterationRange(first=1, last=1), IterationRange(first=3, last=3))
        assert result.cancelled.failure_tree == failures[0].failure_tree

    def test_ordinary_reasons_are_capped_without_counting_the_early_termination(self) -> None:
        failures = _iteration_failures([f"reason {index}" for index in range(MAX_REPORTED_ITERATION_FAILURES + 2)])
        failures.append(IterationFailure(iteration_index=len(failures), detail="refused", failure_tree=_early("X")))

        result = failure_trees.loop_failure_from_iterations("Loop", len(failures), failures)

        assert len(result.reasons) == MAX_REPORTED_ITERATION_FAILURES
        assert result.omitted_reasons == 2  # noqa: PLR2004
        assert result.omitted_iterations == 2  # noqa: PLR2004
        assert result.early_termination is not None

    def test_a_second_early_termination_text_is_kept_among_the_reasons_in_order(self) -> None:
        failures = [
            IterationFailure(iteration_index=0, detail="first", failure_tree=_early("A", "first")),
            IterationFailure(iteration_index=1, detail="second", failure_tree=_early("B", "second")),
        ]

        result = failure_trees.loop_failure_from_iterations("Loop", 2, failures)

        assert result.early_termination is not None
        assert result.early_termination.detail == "first"
        assert [reason.detail for reason in result.reasons] == ["second"]

    def test_later_early_terminations_share_the_cap_with_the_ordinary_reasons(self) -> None:
        failures = [IterationFailure(iteration_index=index, detail=f"reason {index}") for index in range(5)]
        failures.append(IterationFailure(iteration_index=5, detail="first", failure_tree=_early("A", "first")))
        failures.append(IterationFailure(iteration_index=6, detail="second", failure_tree=_early("B", "second")))

        result = failure_trees.loop_failure_from_iterations("Loop", 7, failures)

        assert result.early_termination is not None
        assert result.early_termination.detail == "first"
        assert len(result.reasons) == MAX_REPORTED_ITERATION_FAILURES
        assert result.omitted_reasons == 1
        assert result.omitted_iterations == 1
        assert _loop_message(7, failures) == (
            "Attempted to run all 7 iterations of loop 'Loop'. Failed because 7 of them did not finish.\n"
            "  Iteration 1: reason 0\n"
            "  Iteration 2: reason 1\n"
            "  Iteration 3: reason 2\n"
            "  Iteration 4: reason 3\n"
            "  Iteration 5: reason 4\n"
            "  ... and 2 more reason(s) affecting 2 iteration(s). See the engine log for every iteration."
        )

    def test_there_is_no_early_termination_or_cancelled_reason_without_one(self) -> None:
        result = failure_trees.loop_failure_from_iterations("Loop", 2, _iteration_failures(["a"]))

        assert result.early_termination is None
        assert result.cancelled is None
        assert result.iterations_run is None


class TestRenderLoopMessage:
    """The text of a loop's failure: what was attempted, how much was lost, then the reasons."""

    def test_a_single_reason(self) -> None:
        failures = [IterationFailure(iteration_index=1, detail="boom")]

        assert _loop_message(3, failures) == (
            "Attempted to run all 3 iterations of loop 'Loop'. Failed because 1 of them did not finish.\n"
            "  Iteration 2: boom"
        )

    def test_every_iteration_failing_for_one_reason(self) -> None:
        assert _loop_message(3, _iteration_failures(["boom", "boom", "boom"])) == (
            "Attempted to run all 3 iterations of loop 'Loop'. Failed because 3 of them did not finish.\n"
            "  Every iteration: boom"
        )

    def test_a_contiguous_run_is_named_by_its_ends(self) -> None:
        failures = [IterationFailure(iteration_index=index, detail="boom") for index in (1, 2, 3)]

        assert _loop_message(10, failures) == (
            "Attempted to run all 10 iterations of loop 'Loop'. Failed because 3 of them did not finish.\n"
            "  Iterations 2-4: boom"
        )

    def test_a_scattered_few_are_all_named(self) -> None:
        failures = [IterationFailure(iteration_index=index, detail="boom") for index in (0, 4)]

        assert _loop_message(10, failures) == (
            "Attempted to run all 10 iterations of loop 'Loop'. Failed because 2 of them did not finish.\n"
            "  Iterations 1, 5: boom"
        )

    def test_a_scattered_many_are_cut_off_with_a_count(self) -> None:
        failures = [IterationFailure(iteration_index=index, detail="boom") for index in range(0, 14, 2)]

        assert _loop_message(20, failures) == (
            "Attempted to run all 20 iterations of loop 'Loop'. Failed because 7 of them did not finish.\n"
            "  Iterations 1, 3, 5, 7, 9, 11 (+1 more): boom"
        )

    def test_runs_are_expanded_to_name_the_first_iterations(self) -> None:
        indexes = (0, 1, 2, 3, 4, 5, 6, 10)
        failures = [IterationFailure(iteration_index=index, detail="boom") for index in indexes]

        assert _loop_message(20, failures) == (
            "Attempted to run all 20 iterations of loop 'Loop'. Failed because 8 of them did not finish.\n"
            "  Iterations 1, 2, 3, 4, 5, 6 (+2 more): boom"
        )

    def test_reasons_beyond_the_cap_are_counted_with_their_iterations(self) -> None:
        failures = _iteration_failures([f"reason {index}" for index in range(5)])
        failures += [
            IterationFailure(iteration_index=5, detail="cut off"),
            IterationFailure(iteration_index=6, detail="also cut off"),
        ]

        assert _loop_message(7, failures) == (
            "Attempted to run all 7 iterations of loop 'Loop'. Failed because 7 of them did not finish.\n"
            "  Iteration 1: reason 0\n"
            "  Iteration 2: reason 1\n"
            "  Iteration 3: reason 2\n"
            "  Iteration 4: reason 3\n"
            "  Iteration 5: reason 4\n"
            "  ... and 2 more reason(s) affecting 2 iteration(s). See the engine log for every iteration."
        )

    def test_no_failures_is_the_summary_alone(self) -> None:
        assert _loop_message(3, []) == (
            "Attempted to run all 3 iterations of loop 'Loop'. Failed because 0 of them did not finish."
        )

    def test_says_how_many_iterations_were_never_run(self) -> None:
        loop = failure_trees.loop_failure_from_iterations("Loop", 3, [_early_iteration(0)], iterations_run=1)

        assert failure_trees.render_loop_message(loop) == (
            "Attempted to run all 3 iterations of loop 'Loop'. Failed because 1 of them did not finish. "
            "The remaining 2 iterations were not run.\n"
            "  Iteration 1: too deep"
        )

    def test_says_it_in_the_singular_for_one_unrun_iteration(self) -> None:
        loop = failure_trees.loop_failure_from_iterations("Loop", 2, [_early_iteration(0)], iterations_run=1)

        assert failure_trees.render_loop_message(loop) == (
            "Attempted to run all 2 iterations of loop 'Loop'. Failed because 1 of them did not finish. "
            "The remaining iteration was not run.\n"
            "  Iteration 1: too deep"
        )

    def test_says_nothing_when_every_iteration_was_run(self) -> None:
        loop = failure_trees.loop_failure_from_iterations("Loop", 1, [_early_iteration(0)], iterations_run=1)

        assert failure_trees.render_loop_message(loop) == (
            "Attempted to run all 1 iterations of loop 'Loop'. Failed because 1 of them did not finish.\n"
            "  Iteration 1: too deep"
        )

    def test_an_early_termination_among_ordinary_failures_is_in_the_order_it_happened(self) -> None:
        failures = [
            IterationFailure(iteration_index=0, detail="ordinary", failure_tree=_leaf("Body", "ordinary")),
            _early_iteration(1),
        ]

        loop = failure_trees.loop_failure_from_iterations("Loop", 4, failures, iterations_run=2)

        assert failure_trees.render_loop_message(loop) == (
            "Attempted to run all 4 iterations of loop 'Loop'. Failed because 2 of them did not finish. "
            "The remaining 2 iterations were not run.\n"
            "  Iteration 1: ordinary\n"
            "  Iteration 2: too deep"
        )

    def test_an_early_termination_before_ordinary_failures_comes_first(self) -> None:
        failures = [
            _early_iteration(0),
            IterationFailure(iteration_index=1, detail="ordinary", failure_tree=_leaf("Body", "ordinary")),
        ]

        assert _loop_message(2, failures) == (
            "Attempted to run all 2 iterations of loop 'Loop'. Failed because 2 of them did not finish.\n"
            "  Iteration 1: too deep\n"
            "  Iteration 2: ordinary"
        )

    def test_a_parallel_early_termination_names_its_cancelled_siblings_and_has_no_iterations_run(self) -> None:
        failures = [
            failure_trees.iteration_failure_from_cancellation("Loop", 0),
            _early_iteration(1),
            failure_trees.iteration_failure_from_cancellation("Loop", 2),
            IterationFailure(iteration_index=3, detail="ordinary", failure_tree=_leaf("Body", "ordinary")),
        ]

        assert _loop_message(4, failures) == (
            "Attempted to run all 4 iterations of loop 'Loop'. Failed because 4 of them did not finish.\n"
            "  Iterations 1, 3: Stopped because another iteration ran a workflow nested deeper than "
            "`max_workflow_recursion_depth` allows.\n"
            "  Iteration 2: too deep\n"
            "  Iteration 4: ordinary"
        )

    def test_an_early_termination_past_the_cap_shows_only_in_the_count(self) -> None:
        failures = _iteration_failures([f"reason {index}" for index in range(5)])
        failures.append(_early_iteration(5))

        loop = failure_trees.loop_failure_from_iterations("Loop", 6, failures, iterations_run=6)

        assert failure_trees.render_loop_message(loop) == (
            "Attempted to run all 6 iterations of loop 'Loop'. Failed because 6 of them did not finish.\n"
            "  Iteration 1: reason 0\n"
            "  Iteration 2: reason 1\n"
            "  Iteration 3: reason 2\n"
            "  Iteration 4: reason 3\n"
            "  Iteration 5: reason 4\n"
            "  ... and 1 more reason(s) affecting 1 iteration(s). See the engine log for every iteration."
        )

    def test_the_count_adds_reasons_the_ordinary_cap_left_out_to_those_the_merge_left_out(self) -> None:
        failures = _iteration_failures([f"reason {index}" for index in range(7)])
        failures.append(_early_iteration(7))

        message = failure_trees.render_loop_message(failure_trees.loop_failure_from_iterations("Loop", 8, failures))

        assert message.endswith(
            "  ... and 3 more reason(s) affecting 3 iteration(s). See the engine log for every iteration."
        )

    def test_a_while_group_names_the_iteration_that_ended_it_and_why(self) -> None:
        loop = failure_trees.loop_failure_from_while_iteration("While", 3, _early_iteration(2))

        assert failure_trees.render_loop_message(loop) == (
            "Attempted to run iteration 3 of while group 'While'. Failed because too deep"
        )


def _workflow_node(name: str, inner: FailureTree) -> WorkflowNodeFailure:
    return WorkflowNodeFailure(node_name=name, inner=inner)


def _mixed_loop(name: str = "Loop") -> LoopFailure:
    return failure_trees.loop_failure_from_iterations(
        name,
        4,
        [
            IterationFailure(iteration_index=0, detail="even refused", failure_tree=_leaf("Body", "even refused")),
            IterationFailure(iteration_index=1, detail="odd refused", failure_tree=_leaf("Body", "odd refused")),
            IterationFailure(iteration_index=2, detail="even refused", failure_tree=_leaf("Body", "even refused")),
        ],
    )


def _path_below(workflow_node: WorkflowNodeFailure) -> list[str]:
    details = failure_trees.render_error_details(workflow_node, text="", node_name=workflow_node.node_name)
    return details.fields[FAILED_NODE_PATH_FIELD].split(FAILED_NODE_PATH_SEPARATOR)


class TestFailedNodePath:
    def test_follows_containers_down_to_the_leaf(self) -> None:
        group = GroupFailure(node_name="Group", inner=_leaf("Failing"))

        assert _path_below(_workflow_node("Outer", group)) == ["Group", "Failing"]

    def test_follows_a_loop_whose_iterations_agree(self) -> None:
        loop = failure_trees.loop_failure_from_iterations(
            "Loop",
            2,
            [
                IterationFailure(iteration_index=0, detail="same", failure_tree=_leaf("Body")),
                IterationFailure(iteration_index=1, detail="same", failure_tree=_leaf("Body")),
            ],
        )

        assert _path_below(_workflow_node("Outer", loop)) == ["Loop", "Body"]

    def test_stops_at_a_loop_whose_iterations_failed_for_different_reasons(self) -> None:
        assert _path_below(_workflow_node("Outer", _mixed_loop())) == ["Loop"]

    def test_stops_at_a_loop_whose_only_reason_names_no_failure(self) -> None:
        loop = failure_trees.loop_failure_from_iterations(
            "Loop", 1, [IterationFailure(iteration_index=0, detail="said nothing")]
        )

        assert _path_below(_workflow_node("Outer", loop)) == ["Loop"]

    def test_a_node_directly_inside_is_a_path_of_one(self) -> None:
        assert _path_below(_workflow_node("Outer", _leaf("Node"))) == ["Node"]

    def test_goes_on_through_an_early_termination_even_beside_other_reasons(self) -> None:
        early = _early("Inner")
        loop = failure_trees.loop_failure_from_iterations(
            "Loop",
            3,
            [
                IterationFailure(iteration_index=0, detail="ordinary", failure_tree=_leaf("Body", "ordinary")),
                IterationFailure(iteration_index=1, detail="other", failure_tree=_leaf("Body", "other")),
                IterationFailure(iteration_index=2, detail="refused", failure_tree=early),
            ],
        )

        assert _path_below(_workflow_node("Outer", loop)) == ["Loop", "Inner"]

    def test_goes_on_through_an_early_termination_beside_cancelled_siblings(self) -> None:
        early = _early("Inner")
        loop = failure_trees.loop_failure_from_iterations(
            "Loop",
            3,
            [
                failure_trees.iteration_failure_from_cancellation("Loop", 0),
                IterationFailure(iteration_index=1, detail="refused", failure_tree=early),
                failure_trees.iteration_failure_from_cancellation("Loop", 2),
            ],
        )

        assert _path_below(_workflow_node("Outer", loop)) == ["Loop", "Inner"]

    def test_goes_on_through_an_early_termination_inside_a_workflow_node(self) -> None:
        loop = failure_trees.loop_failure_from_iterations(
            "Loop",
            2,
            [
                IterationFailure(iteration_index=0, detail="ordinary", failure_tree=_leaf("Body")),
                IterationFailure(
                    iteration_index=1, detail="refused", failure_tree=_workflow_node("Deeper", _early("Inner"))
                ),
            ],
        )

        assert _path_below(_workflow_node("Outer", loop)) == ["Loop", "Deeper", "Inner"]

    def test_an_early_termination_directly_inside_is_a_path_of_one(self) -> None:
        assert _path_below(_workflow_node("Outer", _early("Inner"))) == ["Inner"]


class TestNestedWorkflowMessage:
    def test_a_node_directly_inside_reads_naturally(self) -> None:
        message = failure_trees.nested_workflow_message(_workflow_node("Outer", _leaf("Inner")))

        assert message == (
            "Attempted to run the workflow behind node 'Outer'. Failed because node 'Inner' inside it failed: boom"
        )

    def test_a_node_inside_one_nested_workflow_node_is_not_pluralised(self) -> None:
        failure = _workflow_node("Outer", _workflow_node("Middle", _leaf("Failing")))

        assert failure_trees.nested_workflow_message(failure) == (
            "Attempted to run the workflow behind node 'Outer'. "
            "Failed because node 'Failing', inside a nested Workflow node, failed: boom"
        )

    def test_nested_workflow_nodes_are_counted(self) -> None:
        failure = _workflow_node("Outer", _workflow_node("A", _workflow_node("B", _leaf("Failing"))))

        assert failure_trees.nested_workflow_message(failure) == (
            "Attempted to run the workflow behind node 'Outer'. "
            "Failed because node 'Failing', inside 2 nested Workflow nodes, failed: boom"
        )

    def test_workflow_nodes_are_counted_through_groups_and_loops(self) -> None:
        group = GroupFailure(node_name="Group", inner=_workflow_node("Inner", _leaf("Failing")))
        loop = failure_trees.loop_failure_from_iterations(
            "Loop", 1, [IterationFailure(iteration_index=0, detail="boom", failure_tree=group)]
        )

        assert failure_trees.nested_workflow_message(_workflow_node("Outer", loop)) == (
            "Attempted to run the workflow behind node 'Outer'. "
            "Failed because node 'Failing', inside a nested Workflow node, failed: boom"
        )

    def test_groups_and_loops_on_the_path_are_not_counted_as_workflow_nodes(self) -> None:
        group = GroupFailure(node_name="Group", inner=_leaf("Failing"))

        assert failure_trees.nested_workflow_message(_workflow_node("Outer", group)) == (
            "Attempted to run the workflow behind node 'Outer'. Failed because node 'Failing' inside it failed: boom"
        )

    def test_loop_iterations_that_disagree_are_reported_by_the_loops_own_summary(self) -> None:
        message = failure_trees.nested_workflow_message(_workflow_node("Outer", _mixed_loop()))

        assert message == (
            "Attempted to run the workflow behind node 'Outer'. Failed because node 'Loop' inside it failed: "
            "Attempted to run all 4 iterations of loop 'Loop'. Failed because 3 of them did not finish.\n"
            "  Iterations 1, 3: even refused\n"
            "  Iteration 2: odd refused"
        )

    def test_a_renamed_loop_is_worded_by_its_canvas_name(self) -> None:
        copy_loop = _mixed_loop("Loop_copy")

        message = failure_trees.nested_workflow_message(
            _workflow_node("Outer", failure_trees.renamed(copy_loop, {"Loop_copy": "Loop"}))
        )

        assert "node 'Loop' inside it failed" in message
        assert "loop 'Loop'" in message
        assert "Loop_copy" not in message

    def test_an_early_termination_named_after_a_workflow_node_is_not_counted_as_one(self) -> None:
        early = _early("Inner", "refused")

        message = failure_trees.nested_workflow_message(_workflow_node("Outer", _workflow_node("Middle", early)))

        assert message == (
            "Attempted to run the workflow behind node 'Outer'. "
            "Failed because node 'Inner', inside a nested Workflow node, failed: refused"
        )

    def test_a_refused_load_named_after_a_workflow_node_is_not_counted(self) -> None:
        group = GroupFailure(node_name="Group", inner=_workflow_node("Inner", _early("Inner")))

        assert failure_trees.nested_workflow_message(_workflow_node("Outer", group)) == (
            "Attempted to run the workflow behind node 'Outer'. "
            "Failed because node 'Inner', inside a nested Workflow node, failed: too deep"
        )

    def test_a_loop_ended_early_is_reported_by_what_ended_it_not_the_loops_summary(self) -> None:
        loop = failure_trees.loop_failure_from_iterations(
            "Loop",
            3,
            [
                IterationFailure(iteration_index=0, detail="ordinary", failure_tree=_leaf("Body", "ordinary")),
                _early_iteration(1),
            ],
            iterations_run=2,
        )

        assert failure_trees.nested_workflow_message(_workflow_node("Outer", loop)) == (
            "Attempted to run the workflow behind node 'Outer'. Failed because node 'Inner' inside it failed: too deep"
        )


class TestRenderErrorDetailsOfAWorkflowNodeFailure:
    def test_the_leaf_type_is_reported_with_the_path_as_a_field(self) -> None:
        details = failure_trees.render_error_details(
            _workflow_node("Outer", _leaf("Inner")), text="", node_name="Outer"
        )

        assert details == NodeErrorDetails(
            message=(
                "Attempted to run the workflow behind node 'Outer'. Failed because node 'Inner' inside it failed: boom"
            ),
            fields={FAILED_NODE_PATH_FIELD: "Inner"},
        )

    def test_keeps_the_leaf_type_fields_response_and_links(self) -> None:
        link = NodeErrorLink(label="Docs", url="https://example.com")
        leaf = FailureLeaf(
            node_name="Inner",
            kind=FailureKind.RAISED,
            details=NodeErrorDetails(
                message="refused",
                exception_type="builtins.KeyError",
                fields={"request_id": "abc"},
                response={"error": "nope"},
                links=[link],
            ),
        )

        details = failure_trees.render_error_details(_workflow_node("Outer", leaf), text="", node_name="Outer")

        assert details.exception_type == "builtins.KeyError"
        assert details.fields == {"request_id": "abc", FAILED_NODE_PATH_FIELD: "Inner"}
        assert details.response == {"error": "nope"}
        assert details.links == [link]

    def test_does_not_carry_the_leaf_validation_messages(self) -> None:
        leaf = failure_trees.failure_leaf_from_validation("Inner", [ValueError("bad input"), ValueError("worse input")])
        assert leaf.details.messages == ["bad input", "worse input"]

        details = failure_trees.render_error_details(_workflow_node("Outer", leaf), text="", node_name="Outer")

        assert details.messages is None
        assert details.exception_type == "builtins.ValueError"

    def test_does_not_change_the_leaf_own_details(self) -> None:
        leaf = _leaf("Inner")

        failure_trees.render_error_details(_workflow_node("Outer", leaf), text="", node_name="Outer")

        assert leaf.details.fields == {}

    def test_the_path_replaces_a_path_field_the_leaf_already_carried(self) -> None:
        leaf = FailureLeaf(
            node_name="Inner",
            kind=FailureKind.RAISED,
            details=NodeErrorDetails(message="boom", fields={FAILED_NODE_PATH_FIELD: "stale"}),
        )

        details = failure_trees.render_error_details(_workflow_node("Outer", leaf), text="", node_name="Outer")

        assert details.fields == {FAILED_NODE_PATH_FIELD: "Inner"}

    def test_the_path_lists_every_node_below_the_workflow_node(self) -> None:
        group = GroupFailure(node_name="Group", inner=_leaf("Failing"))
        failure = _workflow_node("Outer", _workflow_node("Middle", group))

        details = failure_trees.render_error_details(failure, text="", node_name="Outer")

        assert details.fields[FAILED_NODE_PATH_FIELD].split(FAILED_NODE_PATH_SEPARATOR) == [
            "Middle",
            "Group",
            "Failing",
        ]

    def test_a_long_path_keeps_both_ends_and_counts_the_middle(self) -> None:
        failure = _leaf("Node 9")
        for index in range(8, 0, -1):
            failure = GroupFailure(node_name=f"Node {index}", inner=failure)
        failure = _workflow_node("Outer", failure)

        details = failure_trees.render_error_details(failure, text="", node_name="Outer")

        assert details.fields[FAILED_NODE_PATH_FIELD].split(FAILED_NODE_PATH_SEPARATOR) == [
            "Node 1",
            "Node 2",
            "\u2026 (5 more)",
            "Node 8",
            "Node 9",
        ]

    def test_a_path_of_exactly_the_longest_unshortened_length_is_listed_in_full(self) -> None:
        failure = _leaf("Node 6")
        for index in range(5, 0, -1):
            failure = GroupFailure(node_name=f"Node {index}", inner=failure)

        details = failure_trees.render_error_details(_workflow_node("Outer", failure), text="", node_name="Outer")

        assert len(details.fields[FAILED_NODE_PATH_FIELD].split(FAILED_NODE_PATH_SEPARATOR)) == 6  # noqa: PLR2004

    def test_a_loop_whose_iterations_disagree_is_text_only_and_the_path_ends_at_it(self) -> None:
        details = failure_trees.render_error_details(_workflow_node("Outer", _mixed_loop()), text="", node_name="Outer")

        assert details.exception_type == "builtins.RuntimeError"
        assert details.fields == {FAILED_NODE_PATH_FIELD: "Loop"}
        assert details.response is None
        assert details.links == []
        assert details.messages is None
        assert details.message.count("even refused") == 1
        assert details.message.count("odd refused") == 1

    def test_a_loop_whose_iterations_agree_reports_the_node_that_failed_in_them_once(self) -> None:
        loop = failure_trees.loop_failure_from_iterations(
            "Loop",
            2,
            [
                IterationFailure(iteration_index=0, detail="same", failure_tree=_leaf("Body", "same")),
                IterationFailure(iteration_index=1, detail="same", failure_tree=_leaf("Body", "same")),
            ],
        )

        details = failure_trees.render_error_details(_workflow_node("Outer", loop), text="", node_name="Outer")

        assert details.message == (
            "Attempted to run the workflow behind node 'Outer'. Failed because node 'Body' inside it failed: same"
        )
        assert details.fields == {FAILED_NODE_PATH_FIELD: "Loop \u203a Body"}

    def test_a_budget_halt_keeps_its_own_wording_and_gains_the_path(self) -> None:
        halt = FailureLeaf(
            node_name="Spender",
            kind=FailureKind.BUDGET_HALT,
            details=NodeErrorDetails(message="Budget stopped this run. Spender was blocked.", exception_type="x.Halt"),
        )

        details = failure_trees.render_error_details(
            _workflow_node("Outer", _workflow_node("Middle", halt)), text="", node_name="Outer"
        )

        assert details.message == "Budget stopped this run. Spender was blocked."
        assert details.exception_type == "x.Halt"
        assert details.fields == {FAILED_NODE_PATH_FIELD: f"Middle{FAILED_NODE_PATH_SEPARATOR}Spender"}


class TestRenderErrorDetailsOfAWorkflowNode:
    def test_reports_the_failure_inside_it(self) -> None:
        details = failure_trees.render_error_details(
            _workflow_node("Outer", _leaf("Inner")), text="ignored", node_name="Outer"
        )

        assert details.fields == {FAILED_NODE_PATH_FIELD: "Inner"}

    def test_a_group_is_still_text_only(self) -> None:
        group = GroupFailure(node_name="Group", inner=_leaf("Inner"))

        details = failure_trees.render_error_details(group, text="the words", node_name="Group")

        assert (details.message, details.fields) == ("the words", {})

    def test_error_details_from_a_workflow_nodes_exception_are_its_nested_details(self) -> None:
        failure = _workflow_node("Outer", _leaf("Inner"))
        error = FailureTreeError(failure, "words")

        assert failure_trees.error_details_from_exception(error, "Outer") == failure_trees.render_error_details(
            failure, text="", node_name="Outer"
        )


class TestBudgetHaltBehindWorkflowNode:
    @pytest.fixture
    def halt(self) -> FailureTree:
        return FailureLeaf(
            node_name="Spender",
            kind=FailureKind.BUDGET_HALT,
            details=NodeErrorDetails(message="Budget stopped this run."),
        )

    def test_gives_the_halts_wording(self, halt: FailureTree) -> None:
        failure = _workflow_node("Outer", _workflow_node("Middle", halt))

        assert failure_trees.budget_halt_behind_workflow_node(failure) == "Budget stopped this run."

    def test_a_halt_inside_a_group_is_found(self, halt: FailureTree) -> None:
        group = GroupFailure(node_name="Group", inner=halt)

        assert (
            failure_trees.budget_halt_behind_workflow_node(_workflow_node("Outer", group)) == "Budget stopped this run."
        )

    def test_another_failure_gives_none(self) -> None:
        assert failure_trees.budget_halt_behind_workflow_node(_workflow_node("Outer", _leaf("Inner"))) is None

    def test_a_halt_that_is_not_behind_a_workflow_node_gives_none(self, halt: FailureTree) -> None:
        group = GroupFailure(node_name="Group", inner=halt)

        assert failure_trees.budget_halt_behind_workflow_node(group) is None
        assert failure_trees.budget_halt_behind_workflow_node(halt) is None

    def test_a_halt_among_a_loops_several_reasons_is_not_the_end_of_the_path(self, halt: FailureTree) -> None:
        loop = failure_trees.loop_failure_from_iterations(
            "Loop",
            2,
            [
                IterationFailure(iteration_index=0, detail="halted", failure_tree=halt),
                IterationFailure(iteration_index=1, detail="other", failure_tree=_leaf("Body")),
            ],
        )

        assert failure_trees.budget_halt_behind_workflow_node(_workflow_node("Outer", loop)) is None


class TestIterationFailureDetail:
    NESTED = "Attempted to run the workflow behind node 'W'. Failed because node 'Deeper' inside it failed: boom"

    def test_a_workflow_node_failure_is_described_by_its_nested_message(self) -> None:
        tree = _workflow_node("W", _leaf("Deeper", "boom"))

        assert failure_trees.iteration_failure_detail(tree, "flat text") == self.NESTED

    def test_a_renamed_workflow_node_is_described_by_its_canvas_name(self) -> None:
        tree = _workflow_node("W_1", _leaf("Deeper", "boom"))
        renamed = failure_trees.renamed(tree, {"W_1": "W"})

        assert failure_trees.iteration_failure_detail(renamed, "flat text") == self.NESTED

    def test_a_group_holding_a_workflow_node_adds_one_frame(self) -> None:
        tree = failure_trees.renamed(
            GroupFailure(node_name="G_1", inner=_workflow_node("W_1", _leaf("Deeper", "boom"))),
            {"G_1": "G", "W_1": "W"},
        )

        assert failure_trees.iteration_failure_detail(tree, "flat text") == (
            f"Node 'G' encountered a problem: {self.NESTED}"
        )

    def test_a_group_inside_a_group_adds_a_frame_for_each_outermost_first(self) -> None:
        tree = GroupFailure(
            node_name="Outer",
            inner=GroupFailure(node_name="Inner", inner=_workflow_node("W", _leaf("Deeper", "boom"))),
        )

        assert failure_trees.iteration_failure_detail(tree, "flat text") == (
            f"Node 'Outer' encountered a problem: Node 'Inner' encountered a problem: {self.NESTED}"
        )

    def test_a_plain_leaf_inside_a_group_keeps_the_result_details(self) -> None:
        tree = GroupFailure(node_name="G", inner=_leaf("Node"))

        assert failure_trees.iteration_failure_detail(tree, "flat text") == "flat text"

    def test_a_loop_inside_a_group_keeps_the_result_details(self) -> None:
        loop = failure_trees.loop_failure_from_iterations(
            "Loop", 1, [IterationFailure(iteration_index=0, detail="nope", failure_tree=_leaf("Body"))]
        )
        tree = GroupFailure(node_name="G", inner=loop)

        assert failure_trees.iteration_failure_detail(tree, "flat text") == "flat text"

    def test_a_leaf_keeps_the_result_details(self) -> None:
        assert failure_trees.iteration_failure_detail(_leaf("Node"), "flat text") == "flat text"

    def test_a_failure_without_a_tree_keeps_the_result_details(self) -> None:
        assert failure_trees.iteration_failure_detail(None, "flat text") == "flat text"


class TestEncounteredAProblemMessage:
    def test_names_the_node_and_the_text(self) -> None:
        assert failure_trees.encountered_a_problem_message("G", "boom") == "Node 'G' encountered a problem: boom"


class TestLoopFailureFromWhileIteration:
    def test_is_a_loop_on_the_while_group_with_the_iteration_as_its_early_termination(self) -> None:
        iteration = _early_iteration(1)

        loop = failure_trees.loop_failure_from_while_iteration("While", 2, iteration)

        assert loop.node_name == "While"
        assert loop.reasons == ()
        assert loop.early_termination == LoopReason(
            iterations=(IterationRange(first=2, last=2),), detail="too deep", failure_tree=iteration.failure_tree
        )
        assert loop.iterations_run == 2  # noqa: PLR2004

    def test_does_not_set_the_totals_so_it_is_never_worded_as_every_iteration_failing(self) -> None:
        loop = failure_trees.loop_failure_from_while_iteration("While", 1, _early_iteration(0))

        assert loop.total_iterations is None
        assert loop.failed_iterations is None

    def test_the_path_goes_through_it_to_the_early_termination(self) -> None:
        loop = failure_trees.loop_failure_from_while_iteration("While", 1, _early_iteration(0))

        assert _path_below(_workflow_node("Outer", loop)) == ["While", "Inner"]


class TestIterationFailureFromCancellation:
    def test_is_a_cancelled_leaf_named_after_the_loop_with_its_own_reason(self) -> None:
        iteration = failure_trees.iteration_failure_from_cancellation("Loop", 3)

        assert iteration.iteration_index == 3  # noqa: PLR2004
        leaf = _leaf_of(iteration.failure_tree)  # type: ignore[arg-type]
        assert leaf.kind is FailureKind.CANCELLED
        assert leaf.node_name == "Loop"
        assert leaf.details.message == iteration.detail

    def test_says_another_iteration_ran_a_workflow_nested_too_deeply(self) -> None:
        assert failure_trees.iteration_failure_from_cancellation("Loop", 0).detail == (
            "Stopped because another iteration ran a workflow nested deeper than `max_workflow_recursion_depth` allows."
        )


class TestHasEarlyTermination:
    def test_finds_an_early_termination_deep_in_the_tree(self) -> None:
        group = GroupFailure(node_name="Group", inner=_workflow_node("Inner", _early("Deepest")))
        loop = LoopFailure(
            node_name="Loop",
            reasons=(),
            total_iterations=1,
            failed_iterations=1,
            early_termination=LoopReason(iterations=(IterationRange(first=1, last=1),), detail="d", failure_tree=group),
        )

        assert failure_trees.has_early_termination(_workflow_node("Outer", GroupFailure("G", loop)))
        assert failure_trees.has_early_termination(group)
        assert failure_trees.has_early_termination(_early("Deepest"))

    def test_does_not_find_one_that_is_absent(self) -> None:
        loop = failure_trees.loop_failure_from_iterations(
            "Loop", 1, [IterationFailure(iteration_index=0, detail="d", failure_tree=_leaf("X"))]
        )
        tree = _workflow_node("Outer", GroupFailure(node_name="Group", inner=loop))

        assert not failure_trees.has_early_termination(tree)
        assert not failure_trees.has_early_termination(_leaf("Budget", kind=FailureKind.BUDGET_HALT))
        assert not failure_trees.has_early_termination(_leaf("Loop", kind=FailureKind.CANCELLED))


class TestIterationsRunBeforeEarlyTermination:
    def test_counts_up_to_and_including_the_iteration_that_ended_the_loop(self) -> None:
        failures = [_early_iteration(1)]

        assert failure_trees.iterations_run_before_early_termination(failures) == 2  # noqa: PLR2004

    def test_is_none_when_the_last_failure_is_ordinary(self) -> None:
        failures = [IterationFailure(iteration_index=1, detail="ordinary", failure_tree=_leaf("Body"))]

        assert failure_trees.iterations_run_before_early_termination(failures) is None

    def test_is_none_when_the_last_failure_names_no_failure_tree(self) -> None:
        assert failure_trees.iterations_run_before_early_termination(_iteration_failures(["said nothing"])) is None

    def test_is_none_when_there_are_no_failures(self) -> None:
        assert failure_trees.iterations_run_before_early_termination([]) is None


class TestTaskHitEarlyTermination:
    @pytest.mark.asyncio
    async def test_a_cancelled_task_is_not_one(self) -> None:
        async def run_forever() -> IterationOutcome:
            await asyncio.sleep(3600)
            return IterationOutcome(iteration_index=0, succeeded=True, detail="")

        task = asyncio.ensure_future(run_forever())
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert not failure_trees.task_hit_early_termination(task)

    @pytest.mark.asyncio
    async def test_a_task_that_raised_is_not_one(self) -> None:
        async def fail() -> IterationOutcome:
            msg = "boom"
            raise ValueError(msg)

        task = asyncio.ensure_future(fail())
        await asyncio.gather(task, return_exceptions=True)

        assert not failure_trees.task_hit_early_termination(task)

    @pytest.mark.asyncio
    async def test_a_task_that_returned_success_is_not_one(self) -> None:
        async def succeed() -> IterationOutcome:
            return IterationOutcome(iteration_index=0, succeeded=True, detail="")

        task = asyncio.ensure_future(succeed())
        await asyncio.gather(task)

        assert not failure_trees.task_hit_early_termination(task)

    @pytest.mark.asyncio
    async def test_a_task_that_returned_an_ordinary_failure_is_not_one(self) -> None:
        async def fail() -> IterationOutcome:
            return IterationOutcome(iteration_index=0, succeeded=False, detail="x", failure_tree=_leaf("Body"))

        task = asyncio.ensure_future(fail())
        await asyncio.gather(task)

        assert not failure_trees.task_hit_early_termination(task)

    @pytest.mark.asyncio
    async def test_a_task_that_returned_a_failure_without_a_tree_is_not_one(self) -> None:
        async def fail() -> IterationOutcome:
            return IterationOutcome(iteration_index=0, succeeded=False, detail="x")

        task = asyncio.ensure_future(fail())
        await asyncio.gather(task)

        assert not failure_trees.task_hit_early_termination(task)

    @pytest.mark.asyncio
    async def test_a_task_that_returned_an_early_termination_is_one(self) -> None:
        early = _workflow_node("Outer", _early("Inner"))

        async def refused() -> IterationOutcome:
            return IterationOutcome(iteration_index=0, succeeded=False, detail="x", failure_tree=early)

        task = asyncio.ensure_future(refused())
        await asyncio.gather(task)

        assert failure_trees.task_hit_early_termination(task)
