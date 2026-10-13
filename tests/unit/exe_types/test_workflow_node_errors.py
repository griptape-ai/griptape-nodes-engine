"""Tests for how Workflow nodes report a failure from inside their workflow."""

from typing import Any
from unittest.mock import Mock

import pytest

from griptape_nodes.exe_types.node_error import NodeErrorLink
from griptape_nodes.exe_types.workflow_node import WorkflowNode
from griptape_nodes.exe_types.workflow_node_errors import (
    NestedWorkflowNodeError,
    SubflowNodeFailedError,
    WorkflowRecursionLimitError,
    is_recursion_refusal,
)
from griptape_nodes.retained_mode.events.base_events import ForwardedException
from griptape_nodes.retained_mode.events.node_error_details import NodeErrorDetails


def _failure(node_name: str, cause: BaseException | None, message: str = "boom") -> SubflowNodeFailedError:
    return SubflowNodeFailedError(node_name, NodeErrorDetails(message=message, exception_type="builtins.X"), cause)


def _details(message: str = "boom", **overrides: Any) -> NodeErrorDetails:
    return NodeErrorDetails(message=message, exception_type="builtins.X", **overrides)


class TestNestedWorkflowNodeError:
    def test_one_level_reads_naturally(self) -> None:
        error = NestedWorkflowNodeError("Outer", root_details=_details(), node_path=["Inner"])

        assert str(error) == (
            "Attempted to run the workflow behind node 'Outer'. Failed because node 'Inner' inside it failed: boom"
        )
        assert error.fields == {"Failed node path": "Inner"}

    def test_nested_workflow_nodes_are_counted_in_the_message(self) -> None:
        error = NestedWorkflowNodeError(
            "Outer", root_details=_details(), node_path=["A", "B", "Failing"], workflow_depth=2
        )

        assert str(error) == (
            "Attempted to run the workflow behind node 'Outer'. "
            "Failed because node 'Failing', inside 2 nested Workflow nodes, failed: boom"
        )
        assert error.fields == {"Failed node path": NestedWorkflowNodeError.PATH_SEPARATOR.join(["A", "B", "Failing"])}

    def test_a_single_nested_workflow_node_is_not_pluralised(self) -> None:
        error = NestedWorkflowNodeError("Outer", root_details=_details(), node_path=["A", "Failing"], workflow_depth=1)

        assert str(error) == (
            "Attempted to run the workflow behind node 'Outer'. "
            "Failed because node 'Failing', inside a nested Workflow node, failed: boom"
        )

    def test_groups_and_loops_in_the_path_are_not_counted_as_workflow_nodes(self) -> None:
        error = NestedWorkflowNodeError("Outer", root_details=_details(), node_path=["Group", "Failing"])

        assert str(error) == (
            "Attempted to run the workflow behind node 'Outer'. Failed because node 'Failing' inside it failed: boom"
        )

    def test_empty_path_reports_a_load_failure(self) -> None:
        error = NestedWorkflowNodeError("Outer", root_details=_details(), node_path=[])

        assert str(error) == (
            "Attempted to load the workflow behind node 'Outer'. Failed because the workflow could not be loaded: boom"
        )
        assert error.fields == {}

    def test_keeps_the_roots_fields_response_and_links(self) -> None:
        link = NodeErrorLink(label="Docs", url="https://example.com")
        root = _details(fields={"request_id": "abc"}, response={"error": "nope"}, links=[link])

        error = NestedWorkflowNodeError("Outer", root_details=root, node_path=["Inner"])

        assert error.fields == {"request_id": "abc", "Failed node path": "Inner"}
        assert error.response == {"error": "nope"}
        assert error.links == [link]

    def test_the_path_overrides_a_path_field_the_root_already_carried(self) -> None:
        root = _details(fields={"Failed node path": "stale"})

        error = NestedWorkflowNodeError("Outer", root_details=root, node_path=["Inner"])

        assert error.fields == {"Failed node path": "Inner"}

    def test_a_long_path_keeps_both_ends_and_counts_the_middle(self) -> None:
        path = [f"Node {index}" for index in range(10)]

        error = NestedWorkflowNodeError("Outer", root_details=_details(), node_path=path, workflow_depth=9)

        assert str(error.fields["Failed node path"]).split(NestedWorkflowNodeError.PATH_SEPARATOR) == [
            "Node 0",
            "Node 1",
            "\u2026 (6 more)",
            "Node 8",
            "Node 9",
        ]
        assert "inside 9 nested Workflow nodes" in str(error)

    def test_a_path_at_the_limit_is_shown_whole(self) -> None:
        path = [f"Node {index}" for index in range(NestedWorkflowNodeError.MAX_PATH_ENTRIES_SHOWN)]

        error = NestedWorkflowNodeError("Outer", root_details=_details(), node_path=path)

        assert error.fields["Failed node path"] == NestedWorkflowNodeError.PATH_SEPARATOR.join(path)

    def test_inside_reuses_the_root_and_prepends_the_node(self) -> None:
        cause = ValueError("boom")
        root = _details(fields={"request_id": "abc"})
        innermost = NestedWorkflowNodeError("Inner", root_details=root, node_path=["Failing"], root_cause=cause)

        outer = innermost.inside("Outer")

        assert outer.root_details is root
        assert outer.node_path == ["Inner", "Failing"]
        assert outer.workflow_depth == 1
        assert outer.root_cause is cause
        assert outer.fields == {"request_id": "abc", "Failed node path": "Inner \u203a Failing"}
        assert str(outer).count("boom") == 1

    def test_inside_a_load_failure_does_not_count_the_node_that_could_not_load(self) -> None:
        root = _details()
        refused = NestedWorkflowNodeError("Inner", root_details=root, node_path=[])

        outer = refused.inside("Outer")

        assert outer.node_path == ["Inner"]
        assert outer.workflow_depth == 0


class TestNestedSubflowFailure:
    """`WorkflowNode._nested_subflow_failure` turns a subflow failure into one nested failure, or None."""

    @pytest.fixture
    def workflow_node(self) -> Mock:
        node = Mock(spec=WorkflowNode)
        node.name = "Outer"
        return node

    def test_ordinary_node_failure_starts_a_new_nested_failure(self, workflow_node: Mock) -> None:
        cause = ValueError("boom")
        failure = _failure("Inner", cause)

        nested = WorkflowNode._nested_subflow_failure(workflow_node, failure)

        assert nested is not None
        assert nested.workflow_node_name == "Outer"
        assert nested.root_details is failure.details
        assert nested.node_path == ["Inner"]
        assert nested.workflow_depth == 0
        assert nested.root_cause is cause

    def test_a_failure_with_no_exception_still_reports_its_details(self, workflow_node: Mock) -> None:
        failure = _failure("Inner", None)

        nested = WorkflowNode._nested_subflow_failure(workflow_node, failure)

        assert nested is not None
        assert nested.root_details is failure.details
        assert nested.node_path == ["Inner"]
        assert nested.root_cause is None

    def test_a_forwarded_exception_is_a_root_like_any_other(self, workflow_node: Mock) -> None:
        cause = ForwardedException("boom", original_type="builtins.ValueError")
        failure = _failure("Inner", cause)

        nested = WorkflowNode._nested_subflow_failure(workflow_node, failure)

        assert nested is not None
        assert nested.root_details is failure.details
        assert nested.root_cause is cause

    def test_recursion_refusal_is_the_root(self, workflow_node: Mock) -> None:
        root = _details("too deep")
        cause = NestedWorkflowNodeError(
            "Inner", root_details=root, node_path=[], root_cause=WorkflowRecursionLimitError("too deep")
        )

        nested = WorkflowNode._nested_subflow_failure(workflow_node, _failure("Inner", cause))

        assert nested is not None
        assert nested.node_path == ["Inner"]
        assert nested.workflow_depth == 0
        assert nested.root_details is root

    def test_a_nested_failure_one_level_down_is_extended_not_wrapped(self, workflow_node: Mock) -> None:
        root = _details()
        cause = NestedWorkflowNodeError("Inner", root_details=root, node_path=["Deeper", "Failing"])

        nested = WorkflowNode._nested_subflow_failure(workflow_node, _failure("Inner", cause, message=str(cause)))

        assert nested is not None
        assert nested.root_details is root
        assert nested.node_path == ["Inner", "Deeper", "Failing"]
        assert nested.workflow_depth == 1
        assert str(nested).count("boom") == 1

    def test_a_group_between_workflow_nodes_is_added_to_the_path(self, workflow_node: Mock) -> None:
        root = _details()
        deeper = NestedWorkflowNodeError("Inner Workflow", root_details=root, node_path=["Failing"])
        group_failure = RuntimeError("Node 'Inner Workflow' failed: text of the nested failure")
        group_failure.__cause__ = _failure("Inner Workflow", deeper)

        nested = WorkflowNode._nested_subflow_failure(workflow_node, _failure("Group", group_failure))

        assert nested is not None
        assert nested.root_details is root
        assert nested.node_path == ["Group", "Inner Workflow", "Failing"]
        assert nested.workflow_depth == 1
        assert str(nested).count("boom") == 1

    def test_a_group_around_an_ordinary_failure_adds_the_group_and_the_failing_node(self, workflow_node: Mock) -> None:
        cause = ValueError("boom")
        inner = _failure("Failing", cause)
        group_failure = RuntimeError("Node 'Failing' failed: boom")
        group_failure.__cause__ = inner

        nested = WorkflowNode._nested_subflow_failure(workflow_node, _failure("Group", group_failure))

        assert nested is not None
        assert nested.root_details is inner.details
        assert nested.node_path == ["Group", "Failing"]
        assert nested.workflow_depth == 0
        assert nested.root_cause is cause

    def test_a_group_with_no_failure_behind_it_is_the_failing_node(self, workflow_node: Mock) -> None:
        failure = _failure("Group", RuntimeError("could not start"))

        nested = WorkflowNode._nested_subflow_failure(workflow_node, failure)

        assert nested is not None
        assert nested.node_path == ["Group"]
        assert nested.root_details is failure.details

    @pytest.mark.parametrize("failure", [None, RuntimeError("not a subflow failure")])
    def test_falls_back_to_text_when_the_failure_is_not_recognised(
        self, workflow_node: Mock, failure: Exception | None
    ) -> None:
        assert WorkflowNode._nested_subflow_failure(workflow_node, failure) is None


class TestIsRecursionRefusal:
    """`is_recursion_refusal` finds the refusal at the end of any chain of nested failures."""

    def test_the_refusal_itself(self) -> None:
        assert is_recursion_refusal(WorkflowRecursionLimitError("too deep"))

    def test_a_nested_failure_rooted_in_the_refusal(self) -> None:
        nested = NestedWorkflowNodeError(
            "Inner", root_details=_details(), node_path=[], root_cause=WorkflowRecursionLimitError("too deep")
        )

        assert is_recursion_refusal(nested)

    def test_a_subflow_failure_wrapping_a_nested_refusal(self) -> None:
        nested = NestedWorkflowNodeError(
            "Inner", root_details=_details(), node_path=["Deeper"], root_cause=WorkflowRecursionLimitError("too deep")
        )

        assert is_recursion_refusal(_failure("Inner", nested))

    def test_a_loop_error_chained_from_the_refusal(self) -> None:
        loop_error = RuntimeError("Attempted to run all 3 iterations of loop 'Loop'.")
        loop_error.__cause__ = _failure(
            "Inner",
            NestedWorkflowNodeError(
                "Inner", root_details=_details(), node_path=[], root_cause=WorkflowRecursionLimitError("too deep")
            ),
        )

        assert is_recursion_refusal(loop_error)

    def test_an_ordinary_failure_is_not_a_refusal(self) -> None:
        nested = NestedWorkflowNodeError(
            "Inner", root_details=_details(), node_path=["Failing"], root_cause=ValueError("boom")
        )

        assert not is_recursion_refusal(_failure("Inner", nested))

    @pytest.mark.parametrize("failure", [None, RuntimeError("plain")])
    def test_nothing_or_a_plain_error_is_not_a_refusal(self, failure: BaseException | None) -> None:
        assert not is_recursion_refusal(failure)

    def test_a_cause_cycle_ends(self) -> None:
        first = RuntimeError("first")
        second = RuntimeError("second")
        first.__cause__ = second
        second.__cause__ = first

        assert not is_recursion_refusal(first)
