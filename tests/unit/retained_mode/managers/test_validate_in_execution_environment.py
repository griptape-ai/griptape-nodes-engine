"""A node validates in the process that runs it, not only in the one that dispatches it.

`validate_before_node_run` runs on the orchestrator, which carries a library's edit-time dependencies
only, and a value a worker produced stays in that worker -- so validation reaching for a loaded model or
an upstream tensor cannot run there at all.

`validate_in_execution_environment` is the counterpart that runs where `aprocess` runs, on both the
worker and in-process paths, so a library behaves the same either way.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.retained_mode.events.execution_events import (
    ExecuteNodeRequest,
    ExecuteNodeResultFailure,
    ExecuteNodeResultSuccess,
)
from griptape_nodes.retained_mode.managers.node_manager import NodeManager


def _manager(*, is_worker: bool = False) -> NodeManager:
    engine = MagicMock()
    engine.library_manager.is_worker = is_worker
    return NodeManager(MagicMock(), engine=engine)


def _node(name: str = "Node 1", *, validation: list[Exception] | None = None) -> MagicMock:
    node = MagicMock()
    node.name = name
    node.parameters = []
    node.parameter_values = {}
    node.parameter_output_values = {}
    node.aprocess = AsyncMock()
    node.validate_in_execution_environment = MagicMock(return_value=validation)
    return node


def _request(node_name: str = "Node 1") -> ExecuteNodeRequest:
    return ExecuteNodeRequest(node_name=node_name, parameter_values={})


class TestTheHookGatesExecution:
    @pytest.mark.asyncio
    async def test_a_node_that_declines_does_not_run(self) -> None:
        node = _node(validation=[ValueError("input latent has no source_shape")])

        result = await _manager()._hydrate_and_run_node(node, _request())

        assert isinstance(result, ExecuteNodeResultFailure)
        node.aprocess.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_reasons_travel_as_themselves(self) -> None:
        """Not only in the message: a caller has to be able to tell a refusal from a crash."""
        reason = ValueError("input latent has no source_shape")
        result = await _manager()._hydrate_and_run_node(_node(validation=[reason]), _request())

        assert isinstance(result, ExecuteNodeResultFailure)
        assert result.validation_exceptions == [reason]
        assert "source_shape" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_a_check_that_raises_is_still_a_refusal(self) -> None:
        """An ImportError out of the check is the likeliest outcome, not an engine bug to re-raise."""
        reason = ImportError("No module named 'torch'")
        node = _node()
        node.validate_in_execution_environment = MagicMock(side_effect=reason)

        result = await _manager()._hydrate_and_run_node(node, _request())

        assert isinstance(result, ExecuteNodeResultFailure)
        assert result.validation_exceptions == [reason]
        node.aprocess.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_node_with_nothing_to_say_runs(self) -> None:
        node = _node(validation=None)

        result = await _manager()._hydrate_and_run_node(node, _request())

        assert isinstance(result, ExecuteNodeResultSuccess)
        node.aprocess.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_empty_list_is_not_a_refusal(self) -> None:
        """`[]` means "I looked and found nothing", which is the same as not implementing the hook."""
        node = _node(validation=[])

        result = await _manager()._hydrate_and_run_node(node, _request())

        assert isinstance(result, ExecuteNodeResultSuccess)
        node.aprocess.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("is_worker", [False, True])
    async def test_both_execution_paths_call_it(self, *, is_worker: bool) -> None:
        """A library must not have to know whether it was routed to a worker."""
        node = _node(validation=None)

        await _manager(is_worker=is_worker)._hydrate_and_run_node(node, _request())

        node.validate_in_execution_environment.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_it_runs_after_hydration(self) -> None:
        """Checking a value before it is set would read the wrong thing."""
        order: list[str] = []
        node = _node()
        node.validate_in_execution_environment = MagicMock(side_effect=lambda: order.append("validate"))

        def record_set(name: str, _value: Any, **_kwargs: Any) -> None:
            order.append(f"set:{name}")

        node.set_parameter_value = MagicMock(side_effect=record_set)
        node.get_parameter_by_name = MagicMock(return_value=MagicMock())
        request = ExecuteNodeRequest(node_name="Node 1", parameter_values={"width": 512})

        await _manager()._hydrate_and_run_node(node, request)

        assert order.index("set:width") < order.index("validate")


class TestTheFailureReadsAsARefusal:
    def test_the_message_says_it_did_not_run(self) -> None:
        from griptape_nodes.common.node_executor import NodeExecutor

        result = ExecuteNodeResultFailure(
            result_details="whatever the executor put here",
            validation_exceptions=[ValueError("no pipeline connected")],
        )

        message = NodeExecutor._format_node_failure_message("Node 1", result, None)

        assert "did not run because it failed validation" in message
        assert "no pipeline connected" in message
        assert "execution failed" not in message

    def test_a_real_crash_still_reads_as_one(self) -> None:
        from griptape_nodes.common.node_executor import NodeExecutor

        result = ExecuteNodeResultFailure(result_details="boom")

        message = NodeExecutor._format_node_failure_message("Node 1", result, RuntimeError("boom"))

        assert "execution failed" in message
        assert "failed validation" not in message


class TestTheHookIsOptIn:
    def test_a_node_that_does_not_implement_it_says_nothing(self) -> None:
        """Every existing library inherits the default, so it has to read as "no objection"."""

        class _Probe(BaseNode):
            pass

        assert _Probe(name="Probe").validate_in_execution_environment() is None
