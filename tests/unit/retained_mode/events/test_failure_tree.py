"""Tests for ``CarriesFailureTree``: a result holds its failure tree in this process only."""

from __future__ import annotations

import dataclasses
import json

import pytest

from griptape_nodes.common import failure_trees
from griptape_nodes.retained_mode.events import converters
from griptape_nodes.retained_mode.events.base_events import EventResultFailure, ForwardedException
from griptape_nodes.retained_mode.events.execution_events import (
    ExecuteNodeRequest,
    ExecuteNodeResultFailure,
    StartLocalSubflowRequest,
    StartLocalSubflowResultFailure,
)
from griptape_nodes.retained_mode.events.failure_tree import (
    FailureKind,
    FailureLeaf,
    FailureTree,
    GroupFailure,
)
from griptape_nodes.retained_mode.events.node_error_details import NodeErrorDetails
from griptape_nodes.servers.mcp import _trim_response
from griptape_nodes.utils.budget_refusal import BudgetExceededError


def _leaf(node_name: str, kind: FailureKind = FailureKind.RAISED) -> FailureLeaf:
    return FailureLeaf(
        node_name=node_name,
        kind=kind,
        details=NodeErrorDetails(message="boom", exception_type="builtins.ValueError", fields={"request_id": "r-1"}),
    )


class TestAttachFailureTree:
    @pytest.fixture
    def failure_tree(self) -> FailureTree:
        """A group holding a leaf."""
        return GroupFailure(node_name="G", inner=_leaf("X"))

    def test_attaches_in_place_and_returns_the_result(self, failure_tree: FailureTree) -> None:
        result = ExecuteNodeResultFailure(result_details="words")

        returned = result.attach_failure_tree(failure_tree)

        assert returned is result
        assert result.failure_tree == failure_tree

    def test_a_result_starts_without_one(self) -> None:
        assert ExecuteNodeResultFailure(result_details="words").failure_tree is None
        assert StartLocalSubflowResultFailure(result_details="words").failure_tree is None


class TestFailureTreeIsNeverSerialized:
    @pytest.fixture(params=[ExecuteNodeResultFailure, StartLocalSubflowResultFailure])
    def result_type(self, request: pytest.FixtureRequest) -> type:
        """Each result type that carries a failure tree."""
        return request.param

    @pytest.fixture(params=["engine", "client"])
    def converter(self, request: pytest.FixtureRequest) -> converters.EventConverter:
        """Each converter."""
        return getattr(converters, request.param)

    @pytest.fixture
    def node_result(self) -> ExecuteNodeResultFailure:
        """A failed node result carrying a failure tree."""
        return ExecuteNodeResultFailure(
            result_details="words", error=NodeErrorDetails(message="boom")
        ).attach_failure_tree(_leaf("X"))

    def test_it_is_not_a_dataclass_field(self, result_type: type) -> None:
        assert "failure_tree" not in {field.name for field in dataclasses.fields(result_type)}

    def test_no_converter_writes_it(
        self, converter: converters.EventConverter, node_result: ExecuteNodeResultFailure
    ) -> None:
        sent = converter.unstructure(node_result)

        assert "failure_tree" not in sent
        assert sent["error"]["message"] == "boom"

    def test_no_copy_of_a_converter_writes_it(
        self, converter: converters.EventConverter, node_result: ExecuteNodeResultFailure
    ) -> None:
        assert "failure_tree" not in converter.copy().unstructure(node_result)

    def test_a_subflow_result_is_not_written_with_it(self) -> None:
        result = StartLocalSubflowResultFailure(result_details="words").attach_failure_tree(_leaf("X"))

        assert "failure_tree" not in converters.engine.unstructure(result)
        assert "failure_tree" not in converters.client.unstructure(result)

    def test_a_result_received_from_another_process_has_none(self, node_result: ExecuteNodeResultFailure) -> None:
        sent = json.loads(converters.engine.dumps(node_result))

        received = converters.engine.structure(sent, ExecuteNodeResultFailure)

        assert received.failure_tree is None
        assert received.error == node_result.error

    def test_a_result_sent_as_an_event_has_none(self, node_result: ExecuteNodeResultFailure) -> None:
        event = EventResultFailure(request=ExecuteNodeRequest(node_name="Node"), result=node_result)

        assert "failure_tree" not in json.loads(converters.client.dumps(event))["result"]
        assert "failure_tree" not in json.loads(converters.engine.dumps(event))["result"]

    def test_the_mcp_response_has_none(self, node_result: ExecuteNodeResultFailure) -> None:
        event = EventResultFailure(request=StartLocalSubflowRequest(flow_name="Flow"), result=node_result)
        wire = json.loads(converters.client.dumps_result(event).text)

        trimmed = _trim_response(wire)

        assert "failure_tree" not in trimmed
        assert trimmed["error"]["message"] == "boom"


class TestFailureTreeFromAReceivedResult:
    def test_a_received_leaf_is_rebuilt_from_its_error(self) -> None:
        details = NodeErrorDetails(message="boom", exception_type="builtins.ValueError")
        sent = json.loads(
            converters.engine.dumps(
                ExecuteNodeResultFailure(result_details="words", error=details).attach_failure_tree(_leaf("X"))
            )
        )
        received = converters.engine.structure(sent, ExecuteNodeResultFailure)

        failure_tree = failure_trees.failure_tree_from_result("X", received)

        assert failure_tree == FailureLeaf(node_name="X", kind=FailureKind.RAISED, details=details)

    def test_a_budget_halt_from_a_worker_is_rebuilt_as_a_budget_halt(self) -> None:
        halt_type = f"{BudgetExceededError.__module__}.{BudgetExceededError.__qualname__}"
        exception = ForwardedException("Griptape Cloud refused the call", original_type=halt_type)
        result = ExecuteNodeResultFailure(
            result_details="Griptape Cloud refused the call",
            exception=exception,
            error=NodeErrorDetails(message="not the engine's wording"),
        )

        failure_tree = failure_trees.failure_tree_from_result("Spender", result)

        assert isinstance(failure_tree, FailureLeaf)
        assert failure_tree.kind is FailureKind.BUDGET_HALT
        assert failure_tree.node_name == "Spender"
        assert failure_tree.details.message == "Griptape Cloud refused the call"
        assert failure_tree.details.exception_type == halt_type

    def test_another_forwarded_exception_is_not_a_budget_halt(self) -> None:
        result = ExecuteNodeResultFailure(
            result_details="words",
            exception=ForwardedException("boom", original_type="builtins.ValueError"),
            error=NodeErrorDetails(message="boom"),
        )

        failure_tree = failure_trees.failure_tree_from_result("X", result)

        assert isinstance(failure_tree, FailureLeaf)
        assert failure_tree.kind is FailureKind.RAISED
