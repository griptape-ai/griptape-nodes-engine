"""Contract tests for NodeExecutor.execute().

These tests describe the observable contract of `NodeExecutor.execute(node)`
without relying on internals. They cover three behavioral clusters:

1. Dispatch contract: a plain BaseNode results in an ExecuteNodeRequest whose
   payload reflects the node's name, parameter values, and metadata at the
   moment of dispatch (defensive copies, not live references).
2. Failure contract: when the dispatched request returns a failure result,
   execute() raises RuntimeError that carries the node name and the failure
   details, and outputs are not copied back onto the node.
3. Special-node routing: BaseWhileNodeGroup, BaseIterativeNodeGroup,
   BaseIterativeEndNode, and SubflowNodeGroup (with LOCAL_EXECUTION) take
   their dedicated paths and do NOT dispatch an ExecuteNodeRequest.
"""

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.common.node_executor import NodeExecutor
from griptape_nodes.exe_types.base_iterative_nodes import BaseIterativeEndNode
from griptape_nodes.exe_types.node_groups import (
    BaseIterativeNodeGroup,
    BaseWhileNodeGroup,
    SubflowNodeGroup,
)
from griptape_nodes.exe_types.node_types import LOCAL_EXECUTION
from griptape_nodes.retained_mode.events.execution_events import (
    ExecuteNodeRequest,
    ExecuteNodeResultFailure,
    ExecuteNodeResultSuccess,
)


def _make_executor() -> NodeExecutor:
    return NodeExecutor(engine=MagicMock())


def _make_node(
    name: str = "TestNode",
    parameter_values: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> MagicMock:
    node = MagicMock()
    node.name = name
    node.parameter_values = parameter_values if parameter_values is not None else {}
    node.parameter_output_values = {}
    node.metadata = metadata if metadata is not None else {}
    return node


def _success_result(outputs: dict[str, Any] | None = None) -> ExecuteNodeResultSuccess:
    return ExecuteNodeResultSuccess(
        result_details="ok",
        parameter_output_values=outputs or {},
    )


class TestExecuteDispatchContract:
    """A plain BaseNode dispatches a single ExecuteNodeRequest carrying its state."""

    @pytest.mark.asyncio
    async def test_dispatches_execute_node_request_for_plain_node(self) -> None:
        node = _make_node(name="Greeter")

        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.ahandle_request = AsyncMock(return_value=_success_result())
        await executor.execute(node)

        mock_engine.ahandle_request.assert_awaited_once()
        request = mock_engine.ahandle_request.call_args.args[0]
        assert isinstance(request, ExecuteNodeRequest)
        assert request.node_name == "Greeter"

    @pytest.mark.asyncio
    async def test_dispatches_current_parameter_values(self) -> None:
        node = _make_node(parameter_values={"in": "hi", "n": 3})

        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.ahandle_request = AsyncMock(return_value=_success_result())
        await executor.execute(node)

        request = mock_engine.ahandle_request.call_args.args[0]
        assert request.parameter_values == {"in": "hi", "n": 3}

    @pytest.mark.asyncio
    async def test_dispatched_parameter_values_are_independent_copy(self) -> None:
        """Mutating node.parameter_values after dispatch must not leak into the request."""
        node = _make_node(parameter_values={"in": "hi"})
        captured: dict[str, Any] = {}

        async def capture(req: Any) -> ExecuteNodeResultSuccess:
            # Mutate the node's live dict; the request's snapshot must not change.
            node.parameter_values["in"] = "MUTATED"
            captured["params"] = req.parameter_values
            return _success_result()

        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.ahandle_request = AsyncMock(side_effect=capture)
        await executor.execute(node)

        assert captured["params"] == {"in": "hi"}

    @pytest.mark.asyncio
    async def test_dispatches_node_metadata(self) -> None:
        node = _make_node(metadata={"node_type": "Foo", "library": "Bar"})

        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.ahandle_request = AsyncMock(return_value=_success_result())
        await executor.execute(node)

        request = mock_engine.ahandle_request.call_args.args[0]
        assert request.node_metadata == {"node_type": "Foo", "library": "Bar"}

    @pytest.mark.asyncio
    async def test_dispatched_metadata_is_independent_copy(self) -> None:
        node = _make_node(metadata={"node_type": "Foo", "library": "Bar"})
        captured: dict[str, Any] = {}

        async def capture(req: Any) -> ExecuteNodeResultSuccess:
            node.metadata["library"] = "MUTATED"
            captured["meta"] = req.node_metadata
            return _success_result()

        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.ahandle_request = AsyncMock(side_effect=capture)
        await executor.execute(node)

        assert captured["meta"] == {"node_type": "Foo", "library": "Bar"}

    @pytest.mark.asyncio
    async def test_outputs_from_result_are_copied_back_to_node(self) -> None:
        node = _make_node()

        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.ahandle_request = AsyncMock(
            return_value=_success_result({"a": 1, "b": "two"}),
        )
        await executor.execute(node)

        assert node.parameter_output_values == {"a": 1, "b": "two"}


class TestExecuteFailureContract:
    """A non-success result is converted into a RuntimeError that explains the failure."""

    @pytest.mark.asyncio
    async def test_raises_runtime_error_when_result_is_failure(self) -> None:
        node = _make_node(name="Broken")
        failure = ExecuteNodeResultFailure(result_details="boom")

        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.ahandle_request = AsyncMock(return_value=failure)

        with pytest.raises(RuntimeError):
            await executor.execute(node)

    @pytest.mark.asyncio
    async def test_runtime_error_mentions_node_name(self) -> None:
        node = _make_node(name="Broken")
        failure = ExecuteNodeResultFailure(result_details="boom")

        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.ahandle_request = AsyncMock(return_value=failure)

        with pytest.raises(RuntimeError, match="Broken"):
            await executor.execute(node)

    @pytest.mark.asyncio
    async def test_runtime_error_mentions_failure_details(self) -> None:
        node = _make_node(name="Broken")
        failure = ExecuteNodeResultFailure(result_details="kaboom-detail")

        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.ahandle_request = AsyncMock(return_value=failure)

        with pytest.raises(RuntimeError, match="kaboom-detail"):
            await executor.execute(node)

    @pytest.mark.asyncio
    async def test_outputs_are_not_copied_back_on_failure(self) -> None:
        node = _make_node()
        failure = ExecuteNodeResultFailure(result_details="boom")

        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.ahandle_request = AsyncMock(return_value=failure)

        with pytest.raises(RuntimeError):
            await executor.execute(node)

        assert node.parameter_output_values == {}


class TestExecuteSpecialNodeRouting:
    """Special node types take dedicated paths and never dispatch ExecuteNodeRequest."""

    @pytest.mark.asyncio
    async def test_basewhilenodegroup_routes_to_handle_while_group_execution(self) -> None:
        node = MagicMock(spec=BaseWhileNodeGroup)
        node.name = "WhileGroup"

        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        with (
            patch.object(NodeExecutor, "handle_while_group_execution", new_callable=AsyncMock) as mock_while,
            patch.object(NodeExecutor, "handle_iterative_group_execution", new_callable=AsyncMock) as mock_iter,
            patch.object(NodeExecutor, "handle_loop_execution", new_callable=AsyncMock) as mock_loop,
        ):
            mock_engine.ahandle_request = AsyncMock()
            await executor.execute(node)

        mock_while.assert_awaited_once_with(node)
        mock_iter.assert_not_awaited()
        mock_loop.assert_not_awaited()
        mock_engine.ahandle_request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_baseiterativenodegroup_routes_to_handle_iterative_group_execution(self) -> None:
        node = MagicMock(spec=BaseIterativeNodeGroup)
        node.name = "IterGroup"

        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        with (
            patch.object(NodeExecutor, "handle_while_group_execution", new_callable=AsyncMock) as mock_while,
            patch.object(NodeExecutor, "handle_iterative_group_execution", new_callable=AsyncMock) as mock_iter,
            patch.object(NodeExecutor, "handle_loop_execution", new_callable=AsyncMock) as mock_loop,
        ):
            mock_engine.ahandle_request = AsyncMock()
            await executor.execute(node)

        mock_iter.assert_awaited_once_with(node)
        mock_while.assert_not_awaited()
        mock_loop.assert_not_awaited()
        mock_engine.ahandle_request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_baseiterativeendnode_routes_to_handle_loop_execution(self) -> None:
        node = MagicMock(spec=BaseIterativeEndNode)
        node.name = "EndLoop"

        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        with patch.object(NodeExecutor, "handle_loop_execution", new_callable=AsyncMock) as mock_loop:
            mock_engine.ahandle_request = AsyncMock()
            await executor.execute(node)

        mock_loop.assert_awaited_once_with(node)
        mock_engine.ahandle_request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_subflownodegroup_local_execution_calls_aprocess(self) -> None:
        """When execution_environment is LOCAL_EXECUTION, run aprocess in-process and skip dispatch."""
        node = MagicMock(spec=SubflowNodeGroup)
        node.name = "Subflow"
        node.execution_environment = MagicMock()
        node.execution_environment.name = "execution_environment"
        node.get_parameter_value = MagicMock(return_value=LOCAL_EXECUTION)
        node.aprocess = AsyncMock()

        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.ahandle_request = AsyncMock()
        await executor.execute(node)

        node.aprocess.assert_awaited_once()
        mock_engine.ahandle_request.assert_not_awaited()
