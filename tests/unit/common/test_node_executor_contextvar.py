"""Tests for current_executing_node_name ContextVar in NodeExecutor.execute().

NodeExecutor.execute now dispatches ExecuteNodeRequest through the injected
Engine's ahandle_request for both local and worker execution. The ContextVar
is set before dispatch and reset on return (or exception).
"""

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from griptape_nodes.common.node_executor import NodeExecutor, current_executing_node_name
from griptape_nodes.retained_mode.events.execution_events import ExecuteNodeResultSuccess


def _make_executor() -> NodeExecutor:
    return NodeExecutor(engine=MagicMock())


def _make_node(name: str) -> MagicMock:
    node = MagicMock()
    node.name = name
    node.metadata = {}
    node.parameter_values = {}
    node.parameter_output_values = {}
    return node


def _success_result() -> ExecuteNodeResultSuccess:
    return ExecuteNodeResultSuccess(result_details="ok", parameter_output_values={})


class TestCurrentExecutingNodeNameDefault:
    def test_default_is_none(self) -> None:
        assert current_executing_node_name.get() is None


class TestNodeExecutorContextVar:
    @pytest.mark.asyncio
    async def test_contextvar_holds_node_name_during_dispatch(self) -> None:
        """The ContextVar holds the node name while execute() is running."""
        captured: list[str | None] = []
        node = _make_node("TestNode")

        async def fake_handle(_request: Any) -> ExecuteNodeResultSuccess:
            captured.append(current_executing_node_name.get())
            return _success_result()

        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.ahandle_request = AsyncMock(side_effect=fake_handle)
        await executor.execute(node)

        assert captured == ["TestNode"]

    @pytest.mark.asyncio
    async def test_contextvar_reset_to_none_after_successful_execute(self) -> None:
        """The ContextVar is reset to None after execute() completes successfully."""
        node = _make_node("TestNode")

        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.ahandle_request = AsyncMock(return_value=_success_result())
        await executor.execute(node)

        assert current_executing_node_name.get() is None

    @pytest.mark.asyncio
    async def test_contextvar_reset_to_none_when_dispatch_raises(self) -> None:
        """The ContextVar is reset to None even when dispatch raises an exception."""
        node = _make_node("TestNode")

        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.ahandle_request = AsyncMock(side_effect=RuntimeError("node failed"))

        with pytest.raises(RuntimeError, match="node failed"):
            await executor.execute(node)

        assert current_executing_node_name.get() is None

    @pytest.mark.asyncio
    async def test_parallel_executions_are_isolated(self) -> None:
        """Concurrent executions each see only their own node name via asyncio task context isolation."""
        results: dict[str, str | None] = {}

        node_a = _make_node("NodeA")
        node_b = _make_node("NodeB")

        async def fake_handle(request: Any) -> ExecuteNodeResultSuccess:
            await asyncio.sleep(0)  # yield to allow interleaving
            results[request.node_name] = current_executing_node_name.get()
            return _success_result()

        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.ahandle_request = AsyncMock(side_effect=fake_handle)

        await asyncio.gather(
            executor.execute(node_a),
            executor.execute(node_b),
        )

        assert results["NodeA"] == "NodeA"
        assert results["NodeB"] == "NodeB"
