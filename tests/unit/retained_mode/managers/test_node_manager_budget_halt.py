"""A node whose Griptape Cloud call was refused over budget fails as a budget halt naming that node.

Every node failure passes through ``NodeManager.execution_failure``, so no node type has to
recognize the refusal itself. These drive it with the three shapes a refusal arrives in: the raw
Cloud 403, a halt a Cloud driver raised before it knew which node it was running in, and a halt
already worded for its node.
"""

import json
import logging
from unittest.mock import MagicMock

import httpx
import pytest

from griptape_nodes.common import failure_trees
from griptape_nodes.common.failure_trees import FAILED_NODE_PATH_FIELD, FAILED_NODE_PATH_SEPARATOR
from griptape_nodes.retained_mode.events import converters
from griptape_nodes.retained_mode.events.event_converter import converter
from griptape_nodes.retained_mode.events.execution_events import ExecuteNodeResultFailure
from griptape_nodes.retained_mode.events.failure_tree import (
    FailureKind,
    FailureLeaf,
    FailureTree,
    FailureTreeError,
    WorkflowNodeFailure,
)
from griptape_nodes.retained_mode.managers.node_manager import NodeManager
from griptape_nodes.utils.budget_refusal import (
    BUDGET_HALT_PREFIX,
    BudgetExceededError,
    describe,
    refusal_from_body,
)
from tests.unit.utils.test_budget_refusal import CLOUD_HOST, a_refusal_body


def _node_manager() -> NodeManager:
    """A NodeManager whose engine points at the Cloud deployment the fixtures come from."""
    engine = MagicMock()
    engine.secrets_manager.get_secret.return_value = f"https://{CLOUD_HOST}"
    return NodeManager(MagicMock(), engine=engine)


def _cloud_refusal() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", f"https://{CLOUD_HOST}/api/proxy/v2/models/topaz")
    response = httpx.Response(403, json=a_refusal_body(), request=request)
    return httpx.HTTPStatusError("403 Forbidden", request=request, response=response)


def _driver_halt() -> BudgetExceededError:
    """A halt raised below the node, which does not know the node's name."""
    refusal = refusal_from_body(a_refusal_body())
    assert refusal is not None
    return BudgetExceededError(describe(refusal), refusal)


class TestExecutionFailureNamesTheRefusedNode:
    def test_a_raw_cloud_refusal_becomes_a_halt(self, caplog: pytest.LogCaptureFixture) -> None:
        wrapped = RuntimeError("Attempted to upscale the video. Failed due to a 403.")
        wrapped.__cause__ = _cloud_refusal()

        with caplog.at_level(logging.ERROR, logger="griptape_nodes"):
            failure = _node_manager().execution_failure(wrapped, "Upscale")

        assert isinstance(failure.exception, BudgetExceededError)
        assert failure.exception.node_name == "Upscale"
        assert str(failure.result_details).startswith(BUDGET_HALT_PREFIX)
        assert "'Upscale'" in str(failure.result_details)
        assert '"tight"' in str(failure.result_details)
        assert any("Upscale" in record.getMessage() for record in caplog.records)

    def test_the_halt_keeps_the_original_failure_and_its_stack(self) -> None:
        """The halt replaces the node's exception, so it has to carry where the refusal came from."""
        wrapped = RuntimeError("Attempted to upscale the video. Failed due to a 403.")
        wrapped.__cause__ = _cloud_refusal()

        failure = _node_manager().execution_failure(wrapped, "Upscale")

        halt = failure.exception
        assert halt is not None
        assert halt.__cause__ is wrapped
        assert halt.__traceback__ is not None
        forwarded = converter.unstructure(halt)["traceback"]
        assert "Attempted to upscale the video" in forwarded
        assert "HTTPStatusError" in forwarded

    def test_a_driver_halt_is_reworded_to_name_the_node(self) -> None:
        driver_halt = _driver_halt()
        wrapped = RuntimeError("the driver gave up")
        wrapped.__cause__ = driver_halt

        failure = _node_manager().execution_failure(wrapped, "Upscale")

        assert isinstance(failure.exception, BudgetExceededError)
        assert failure.exception.node_name == "Upscale"
        assert failure.exception.refusal is driver_halt.refusal
        assert "'Upscale'" in str(failure.result_details)

    def test_a_halt_that_already_names_its_node_is_kept(self) -> None:
        """A nested node's halt reaches its parent wrapped, and must still name the node refused."""
        refusal = refusal_from_body(a_refusal_body())
        assert refusal is not None
        inner = BudgetExceededError(describe(refusal, node_name="Inner"), refusal, node_name="Inner")
        outer = RuntimeError("the subflow failed")
        outer.__cause__ = inner

        failure = _node_manager().execution_failure(outer, "Group")

        assert failure.exception is inner
        assert str(failure.result_details) == str(inner)

    def test_an_ordinary_failure_is_reported_as_before(self) -> None:
        error = ValueError("the image was empty")

        failure = _node_manager().execution_failure(error, "Blur")

        assert failure.exception is error
        assert str(failure.result_details) == "Attempted to execute node 'Blur'. Failed with error: the image was empty"


def _a_halt(node_name: str = "Spender") -> BudgetExceededError:
    refusal = refusal_from_body(a_refusal_body())
    assert refusal is not None
    return BudgetExceededError(describe(refusal, node_name=node_name), refusal, node_name=node_name)


def _workflow_node_error(name: str, inner: FailureTree) -> FailureTreeError:
    """What a Workflow node raises for the failure inside it."""
    return FailureTreeError(WorkflowNodeFailure(node_name=name, inner=inner), "nested words")


class TestHaltDeepInsideNestedWorkflowNodes:
    """A halt under Workflow nodes still stops the run, and the failure keeps the path to the refused node."""

    @pytest.fixture
    def halt(self) -> BudgetExceededError:
        return _a_halt()

    @pytest.fixture
    def halted_leaf(self, halt: BudgetExceededError) -> FailureTree:
        """The failure of the refused node, as the node manager builds it where the node ran."""
        result = _node_manager().execution_failure(halt, "Spender")
        assert isinstance(result.failure_tree, FailureLeaf)
        assert result.failure_tree.kind is FailureKind.BUDGET_HALT
        return result.failure_tree

    @pytest.fixture
    def nested(self, halted_leaf: FailureTree) -> FailureTreeError:
        middle = WorkflowNodeFailure(node_name="Inner", inner=halted_leaf)
        return _workflow_node_error("Outer", middle)

    def test_the_halt_is_still_what_the_run_reports(self, nested: FailureTreeError, halt: BudgetExceededError) -> None:
        failure = _node_manager().execution_failure(nested, "Outer")

        assert str(failure.result_details) == str(halt)
        assert str(failure.result_details).startswith(BUDGET_HALT_PREFIX)

    def test_the_failure_travels_up_so_the_next_level_extends_its_path(self, nested: FailureTreeError) -> None:
        failure = _node_manager().execution_failure(nested, "Outer")

        assert failure.exception is nested
        assert failure.failure_tree is nested.failure_tree

    def test_the_path_to_the_refused_node_is_attached_without_rewording_the_halt(
        self, nested: FailureTreeError, halt: BudgetExceededError
    ) -> None:
        failure = _node_manager().execution_failure(nested, "Outer")

        assert failure.error is not None
        assert failure.error.message == str(halt)
        assert failure.error.exception_type == "griptape_nodes.utils.budget_refusal.BudgetExceededError"
        assert failure.error.fields == {FAILED_NODE_PATH_FIELD: FAILED_NODE_PATH_SEPARATOR.join(["Inner", "Spender"])}

    def test_a_failure_naming_another_node_is_wrapped_and_not_reported_as_a_halt(
        self, halted_leaf: FailureTree
    ) -> None:
        inner = WorkflowNodeFailure(node_name="Inner", inner=halted_leaf)
        mismatched = _workflow_node_error("Other", inner)

        failure = _node_manager().execution_failure(mismatched, "Outer")

        assert isinstance(failure.failure_tree, FailureLeaf)
        assert failure.failure_tree.node_name == "Outer"
        assert failure.failure_tree.kind is FailureKind.RAISED
        assert failure.failure_tree is not mismatched.failure_tree
        assert not str(failure.result_details).startswith(BUDGET_HALT_PREFIX)
        assert str(failure.result_details).startswith("Attempted to execute node 'Outer'")

    def test_a_halt_not_under_a_workflow_node_is_reported_as_before(self, halt: BudgetExceededError) -> None:
        wrapped = RuntimeError("the subflow failed")
        wrapped.__cause__ = halt

        failure = _node_manager().execution_failure(wrapped, "Group")

        assert failure.exception is halt
        assert failure.error is None


class TestHaltFromWorkerHostedNodeUnderNestedWorkflowNodes:
    """A halt a worker forwarded has no live `BudgetExceededError`, but still stops the run as a halt."""

    @pytest.fixture
    def halt(self) -> BudgetExceededError:
        return _a_halt()

    @pytest.fixture
    def nested(self, halt: BudgetExceededError) -> FailureTreeError:
        result = self._result_across_worker(_node_manager().execution_failure(halt, "Spender"))
        failure_tree = failure_trees.failure_tree_from_result("Spender", result)
        assert isinstance(failure_tree, FailureLeaf)
        assert failure_tree.kind is FailureKind.BUDGET_HALT
        return _workflow_node_error("Outer", failure_tree)

    def test_the_run_reports_the_halt_wording(self, nested: FailureTreeError, halt: BudgetExceededError) -> None:
        failure = _node_manager().execution_failure(nested, "Outer")

        assert str(failure.result_details).startswith(BUDGET_HALT_PREFIX)
        assert str(failure.result_details) == str(halt)

    def test_the_path_is_kept_and_the_failure_travels_up(
        self, nested: FailureTreeError, halt: BudgetExceededError
    ) -> None:
        failure = _node_manager().execution_failure(nested, "Outer")

        assert failure.failure_tree is nested.failure_tree
        assert failure.error is not None
        assert failure.error.message == str(halt)
        assert failure.error.fields == {FAILED_NODE_PATH_FIELD: "Spender"}

    def test_a_forwarded_failure_of_another_type_is_reported_as_before(self) -> None:
        error = ValueError("the image was empty")
        leaf = failure_trees.failure_tree_from_result(
            "Blur", self._result_across_worker(_node_manager().execution_failure(error, "Blur"))
        )
        assert isinstance(leaf, FailureLeaf)
        assert leaf.kind is FailureKind.RAISED

        failure = _node_manager().execution_failure(_workflow_node_error("Outer", leaf), "Outer")

        assert not str(failure.result_details).startswith(BUDGET_HALT_PREFIX)
        assert str(failure.result_details).startswith("Attempted to execute node 'Outer'")
        assert failure.error is not None
        assert failure.error.exception_type == "builtins.ValueError"

    @staticmethod
    def _result_across_worker(result: ExecuteNodeResultFailure) -> ExecuteNodeResultFailure:
        """Send a failed result through the converter the way a worker sends it back."""
        assert result.exception is not None
        sent = json.loads(json.dumps(converters.engine.unstructure(result)))
        return converters.engine.structure(sent, ExecuteNodeResultFailure)
