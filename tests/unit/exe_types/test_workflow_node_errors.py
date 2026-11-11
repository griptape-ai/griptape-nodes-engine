"""Tests for how Workflow nodes report a failure from inside their workflow."""

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from griptape_nodes.exe_types.node_error import NodeErrorLink
from griptape_nodes.exe_types.workflow_node import WorkflowNode, _failure_exception
from griptape_nodes.exe_types.workflow_node_errors import (
    NestedWorkflowNodeError,
    SubflowNodeFailedError,
)
from griptape_nodes.retained_mode.events.base_events import ForwardedException
from griptape_nodes.retained_mode.events.execution_events import (
    StartLocalSubflowResultFailure,
    StartLocalSubflowResultSuccess,
)
from griptape_nodes.retained_mode.events.node_error_details import NodeErrorDetails
from griptape_nodes.retained_mode.events.node_events import GetFlowForNodeResultSuccess
from griptape_nodes.retained_mode.events.workflow_events import ImportWorkflowAsReferencedSubFlowResultFailure


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


class TestAprocessNestedFailure:
    """`WorkflowNode.aprocess` reports a failure from inside a nested Workflow node as one nested failure."""

    @pytest.fixture
    def workflow_node(self) -> Mock:
        node = Mock(spec=WorkflowNode)
        node.name = "Outer"
        node._load_subflow = AsyncMock(spec=WorkflowNode._load_subflow, return_value="Subflow")
        return node

    @pytest.mark.asyncio
    async def test_raises_the_nested_failure_chained_from_its_root_cause(self, workflow_node: Mock) -> None:
        root_cause = ValueError("boom")
        nested = NestedWorkflowNodeError("Outer", root_details=_details(), node_path=["Inner"], root_cause=root_cause)
        workflow_node._nested_subflow_failure.return_value = nested
        result = StartLocalSubflowResultFailure(
            result_details="subflow failed", exception=_failure("Inner", root_cause)
        )
        workflow_node._ahandle_request_in_workflow = AsyncMock(
            spec=WorkflowNode._ahandle_request_in_workflow, return_value=result
        )

        with pytest.raises(NestedWorkflowNodeError) as raised:
            await WorkflowNode.aprocess(workflow_node)

        assert raised.value is nested
        assert raised.value.__cause__ is root_cause
        workflow_node._nested_subflow_failure.assert_called_once_with(result.exception)
        workflow_node._collect_outputs.assert_not_called()

    @pytest.mark.asyncio
    async def test_collects_outputs_when_the_subflow_succeeds(self, workflow_node: Mock) -> None:
        result = StartLocalSubflowResultSuccess(result_details="ok")
        workflow_node._ahandle_request_in_workflow = AsyncMock(
            spec=WorkflowNode._ahandle_request_in_workflow, return_value=result
        )

        await WorkflowNode.aprocess(workflow_node)

        workflow_node._collect_outputs.assert_called_once()


class TestLoadSubflowImportFailure:
    """`WorkflowNode._load_subflow` reports a failed import as a plain error."""

    @pytest.fixture
    def workflow_node(self) -> Mock:
        node = Mock(spec=WorkflowNode)
        node.name = "Outer"
        node.metadata = {}
        node._handle_request_in_workflow.return_value = GetFlowForNodeResultSuccess(
            flow_name="Parent", result_details="ok"
        )
        node._register_workflow.return_value = "registry-key"
        return node

    @pytest.mark.asyncio
    async def test_any_other_import_failure_is_a_plain_runtime_error(self, workflow_node: Mock) -> None:
        workflow_node.workflow_file_path = "/library/wf.py"
        workflow_node._ahandle_request_in_workflow = AsyncMock(
            spec=WorkflowNode._ahandle_request_in_workflow,
            return_value=ImportWorkflowAsReferencedSubFlowResultFailure(result_details="no such file"),
        )

        with pytest.raises(RuntimeError, match="the workflow could not be loaded: no such file"):
            await WorkflowNode._load_subflow(workflow_node)


class TestFailureException:
    """`_failure_exception` reads the exception off a failure result."""

    def test_returns_the_exception_of_a_failure(self) -> None:
        error = ValueError("boom")

        assert _failure_exception(StartLocalSubflowResultFailure(result_details="no", exception=error)) is error

    def test_returns_none_for_a_result_that_is_not_a_failure(self) -> None:
        assert _failure_exception(StartLocalSubflowResultSuccess(result_details="ok")) is None
