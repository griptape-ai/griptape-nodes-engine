"""Tests for building and rendering ``FailureTree`` values."""

from __future__ import annotations

import pytest

from griptape_nodes.common import failure_trees
from griptape_nodes.common.failure_trees import MAX_REPORTED_ITERATION_FAILURES, IterationFailure
from griptape_nodes.exe_types.node_error import NodeError
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
    WorkflowNodeFailure,
)
from griptape_nodes.retained_mode.events.node_error_details import NodeErrorDetails
from griptape_nodes.utils.budget_refusal import BudgetExceededError, BudgetRefusal


def _leaf(node_name: str, message: str = "boom", kind: FailureKind = FailureKind.RAISED) -> FailureLeaf:
    return FailureLeaf(node_name=node_name, kind=kind, details=NodeErrorDetails(message=message))


def _leaf_of(failure_tree: FailureTree) -> FailureLeaf:
    assert isinstance(failure_tree, FailureLeaf)
    return failure_tree


def _loop_of(failure_tree: FailureTree) -> LoopFailure:
    assert isinstance(failure_tree, LoopFailure)
    return failure_tree


def _iteration_failures(details: list[str]) -> list[IterationFailure]:
    return [IterationFailure(iteration_index=index, detail=detail) for index, detail in enumerate(details)]


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

    def test_a_reason_without_a_failure_tree_has_none(self) -> None:
        result = failure_trees.loop_failure_from_iterations("Loop", 1, _iteration_failures(["a"]))

        assert result.reasons[0].failure_tree is None


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
