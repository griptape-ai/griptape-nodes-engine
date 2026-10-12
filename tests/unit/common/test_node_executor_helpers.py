"""Contract tests for NodeExecutor helpers and SubflowNodeGroup execute() branches.

These tests describe the observable contract of:

* ``NodeExecutor.get_workflow_handler`` - returns the registered handler for a
  library, or raises ``ValueError`` with the library name in the message.
* ``NodeExecutor._extract_parameter_output_values`` - merges per-node output
  dicts from a subprocess result.
* ``NodeExecutor.execute`` - the remaining ``SubflowNodeGroup`` branches
  (private execution, library-name execution) and the unexpected-result-type
  edge case.
"""

import json
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.common.node_executor import NodeExecutor
from griptape_nodes.common.private_execution_scope import PrivateExecutionScope, current_private_execution_scope
from griptape_nodes.exe_types.node_groups import SubflowNodeGroup
from griptape_nodes.exe_types.node_types import LOCAL_EXECUTION, PRIVATE_EXECUTION
from griptape_nodes.retained_mode.events import converters
from griptape_nodes.retained_mode.events.execution_events import ExecuteNodeResultSuccess
from griptape_nodes.retained_mode.events.workflow_events import PublishWorkflowRequest


def _make_executor() -> NodeExecutor:
    return NodeExecutor(engine=MagicMock())


def _make_subflow_node(execution_type: str) -> MagicMock:
    node = MagicMock(spec=SubflowNodeGroup)
    node.name = "Subflow"
    node.execution_environment = MagicMock()
    node.execution_environment.name = "execution_environment"
    node.get_parameter_value = MagicMock(return_value=execution_type)
    node.aprocess = AsyncMock()
    node.subflow_execution_component = MagicMock()
    node.subflow_execution_component.clear_execution_state = MagicMock()
    return node


class TestGetWorkflowHandler:
    """get_workflow_handler returns the PublishWorkflowRequest handler for a library."""

    def test_returns_registered_handler_for_known_library(self) -> None:
        sentinel_handler = object()
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_lm = MagicMock()
        mock_lm.get_registered_event_handlers.return_value = {"my_lib": sentinel_handler}
        mock_engine.library_manager = mock_lm

        handler = executor.get_workflow_handler("my_lib")

        assert handler is sentinel_handler
        mock_lm.get_registered_event_handlers.assert_called_once_with(PublishWorkflowRequest)

    def test_raises_value_error_when_library_unregistered(self) -> None:
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_lm = MagicMock()
        mock_lm.get_registered_event_handlers.return_value = {}
        mock_engine.library_manager = mock_lm

        with pytest.raises(ValueError, match="missing_lib"):
            executor.get_workflow_handler("missing_lib")


class TestExtractParameterOutputValues:
    """_extract_parameter_output_values merges per-node outputs from a subprocess result."""

    def test_returns_empty_dict_for_empty_input(self) -> None:
        assert _make_executor()._extract_parameter_output_values({}) == {}

    def test_merges_outputs_from_multiple_end_nodes(self) -> None:
        executor = _make_executor()
        subprocess_result: dict[str, Any] = {"node_a": {"a": 1}, "node_b": {"b": 2}}

        assert executor._extract_parameter_output_values(subprocess_result) == {"a": 1, "b": 2}


class TestExecuteSubflowNodeGroupBranches:
    """SubflowNodeGroup non-local branches delegate to dedicated workflow paths."""

    @pytest.mark.asyncio
    async def test_private_execution_calls_private_workflow_path(self) -> None:
        node = _make_subflow_node(PRIVATE_EXECUTION)

        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        with (
            patch.object(NodeExecutor, "_execute_private_workflow", new_callable=AsyncMock) as mock_private,
            patch.object(NodeExecutor, "_execute_library_workflow", new_callable=AsyncMock) as mock_library,
        ):
            mock_engine.ahandle_request = AsyncMock()
            await executor.execute(node)

        mock_private.assert_awaited_once_with(node)
        mock_library.assert_not_awaited()
        node.aprocess.assert_not_awaited()
        mock_engine.ahandle_request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_library_execution_routes_through_library_workflow(self) -> None:
        """A non-Local, non-Private execution_environment is treated as a library name."""
        node = _make_subflow_node("some_library_name")

        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        with (
            patch.object(NodeExecutor, "_execute_private_workflow", new_callable=AsyncMock) as mock_private,
            patch.object(NodeExecutor, "_execute_library_workflow", new_callable=AsyncMock) as mock_library,
        ):
            mock_engine.ahandle_request = AsyncMock()
            await executor.execute(node)

        mock_library.assert_awaited_once_with(node, "some_library_name")
        mock_private.assert_not_awaited()
        node.aprocess.assert_not_awaited()
        mock_engine.ahandle_request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_subprocess_paths_clear_execution_state_first(self) -> None:
        """Both PRIVATE and library paths must clear execution state before running."""
        node = _make_subflow_node(PRIVATE_EXECUTION)

        executor = _make_executor()
        with patch.object(NodeExecutor, "_execute_private_workflow", new_callable=AsyncMock):
            await executor.execute(node)

        node.subflow_execution_component.clear_execution_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_local_execution_does_not_clear_execution_state(self) -> None:
        """LOCAL_EXECUTION runs aprocess directly; clearing subprocess state is unnecessary."""
        node = _make_subflow_node(LOCAL_EXECUTION)

        await _make_executor().execute(node)

        node.subflow_execution_component.clear_execution_state.assert_not_called()


_IS_BETA_ENABLED_PATH = "griptape_nodes.common.node_executor.is_beta_enabled"


class TestPrivateExecutionInWorker:
    """With the beta feature on, a Private Execution group runs locally and names the worker for its nodes."""

    @pytest.mark.asyncio
    async def test_runs_the_group_locally_inside_the_scope(self) -> None:
        node = _make_subflow_node(PRIVATE_EXECUTION)
        seen: list[PrivateExecutionScope | None] = []
        node.aprocess = AsyncMock(side_effect=lambda: seen.append(current_private_execution_scope.get()))

        with (
            patch(_IS_BETA_ENABLED_PATH, return_value=True),
            patch.object(NodeExecutor, "_execute_private_workflow", new_callable=AsyncMock) as mock_private,
        ):
            await _make_executor().execute(node)

        assert seen == [PrivateExecutionScope(owner_name="Subflow")]
        mock_private.assert_not_awaited()
        # Nothing is packaged for a subprocess, so there is no subprocess state to clear.
        node.subflow_execution_component.clear_execution_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_scope_ends_with_the_group(self) -> None:
        node = _make_subflow_node(PRIVATE_EXECUTION)

        with patch(_IS_BETA_ENABLED_PATH, return_value=True):
            await _make_executor().execute(node)

        assert current_private_execution_scope.get() is None

    @pytest.mark.asyncio
    async def test_the_subprocess_path_is_kept_when_the_feature_is_off(self) -> None:
        node = _make_subflow_node(PRIVATE_EXECUTION)

        with (
            patch(_IS_BETA_ENABLED_PATH, return_value=False),
            patch.object(NodeExecutor, "_execute_private_workflow", new_callable=AsyncMock) as mock_private,
        ):
            await _make_executor().execute(node)

        mock_private.assert_awaited_once_with(node)
        node.aprocess.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("execution_type", [LOCAL_EXECUTION, "some_library_name"])
    async def test_other_environments_set_no_scope(self, execution_type: str) -> None:
        node = _make_subflow_node(execution_type)
        seen: list[PrivateExecutionScope | None] = []
        node.aprocess = AsyncMock(side_effect=lambda: seen.append(current_private_execution_scope.get()))

        with (
            patch(_IS_BETA_ENABLED_PATH, return_value=True),
            patch.object(NodeExecutor, "_execute_library_workflow", new_callable=AsyncMock),
        ):
            await _make_executor().execute(node)

        assert all(scope is None for scope in seen)

    @pytest.mark.parametrize(
        ("execution_type", "beta_enabled", "runs_locally"),
        [
            (LOCAL_EXECUTION, False, True),
            (PRIVATE_EXECUTION, True, True),
            (PRIVATE_EXECUTION, False, False),
            ("some_library_name", True, False),
        ],
    )
    def test_which_loops_run_through_the_local_path(
        self, execution_type: str, *, beta_enabled: bool, runs_locally: bool
    ) -> None:
        with patch(_IS_BETA_ENABLED_PATH, return_value=beta_enabled):
            assert _make_executor()._runs_locally(execution_type) is runs_locally

    def test_a_private_loop_runs_inside_the_scope_and_a_local_one_does_not(self) -> None:
        executor = _make_executor()

        with executor._local_execution_scope(PRIVATE_EXECUTION, "Loop End"):
            assert current_private_execution_scope.get() == PrivateExecutionScope(owner_name="Loop End")
        with executor._local_execution_scope(LOCAL_EXECUTION, "Loop End"):
            assert current_private_execution_scope.get() is None


class TestExecuteUnexpectedResultType:
    """Anything that isn't an ExecuteNodeResultSuccess is surfaced as a RuntimeError."""

    @pytest.mark.asyncio
    async def test_raises_when_result_is_not_a_success_payload(self) -> None:
        node = MagicMock()
        node.name = "Weird"
        node.parameter_values = {}
        node.parameter_output_values = {}
        node.metadata = {}

        not_a_payload: Any = "not a payload at all"

        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.ahandle_request = AsyncMock(return_value=not_a_payload)

        with pytest.raises(RuntimeError, match="Weird"):
            await executor.execute(node)


class TestExecuteSuccessReturnsNone:
    """The contract of execute() is to return None; all output flows via parameter_output_values."""

    @pytest.mark.asyncio
    async def test_returns_none_on_success(self) -> None:
        node = MagicMock()
        node.name = "Plain"
        node.parameter_values = {}
        node.parameter_output_values = {}
        node.metadata = {}

        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.ahandle_request = AsyncMock(
            return_value=ExecuteNodeResultSuccess(result_details="ok", parameter_output_values={"x": 1}),
        )
        result = await executor.execute(node)

        assert result is None


class TestFormatNodeFailureMessage:
    """Worker-side traceback frames have to land in the RuntimeError message.

    Chaining via ``raise RuntimeError(msg) from exc`` is not enough on
    its own: a ForwardedException is constructed (not raised) on the
    receiving side, so its ``__traceback__`` is None and Python's
    chained-exception display prints only the cause's
    ``Type: message`` line. The helper interpolates
    ``original_traceback`` so the worker frames are actually visible.
    """

    def test_includes_original_type_prefix(self) -> None:
        from griptape_nodes.retained_mode.events.base_events import ForwardedException

        exc = ForwardedException("rebuilt", original_type="builtins.RuntimeError")

        msg = NodeExecutor._format_node_failure_message("MyNode", MagicMock(result_details="oops"), exc)

        assert "[builtins.RuntimeError]" in msg
        assert "MyNode" in msg

    def test_appends_worker_traceback_when_present(self) -> None:
        from griptape_nodes.retained_mode.events.base_events import ForwardedException

        exc = ForwardedException(
            "rebuilt",
            original_type="builtins.ValueError",
            original_traceback='Traceback...\n  File "a.py", line 1, in <module>\nValueError: rebuilt\n',
        )

        msg = NodeExecutor._format_node_failure_message("MyNode", MagicMock(result_details="oops"), exc)

        assert "Worker traceback:" in msg
        assert 'File "a.py"' in msg

    def test_omits_worker_block_for_local_exceptions(self) -> None:
        # Plain Exception (not a ForwardedException) means we're on the
        # local path. No type prefix, no worker-traceback block.
        msg = NodeExecutor._format_node_failure_message(
            "MyNode", MagicMock(result_details="oops"), RuntimeError("local")
        )

        assert "Worker traceback:" not in msg
        assert "[" not in msg.split("execution failed:")[1].split(":")[0]


class TestControlFlowResolvedEventWireForm:
    """The subprocess sends its flow result as JSON; the parent gets back the same values."""

    def test_values_keep_their_types_across_the_wire(self) -> None:
        from griptape_nodes.retained_mode.events.execution_events import ControlFlowResolvedEvent

        converter = converters.engine

        values = {"flag": True, "count": 1, "pair": (1, 2), "blob": b"\x00"}
        event = ControlFlowResolvedEvent(end_node_name="EndFlow", parameter_output_values=values)

        sent = json.loads(json.dumps(converter.unstructure(event)))
        received = converter.structure(sent, ControlFlowResolvedEvent)

        assert received.parameter_output_values == values
        assert type(received.parameter_output_values["flag"]) is bool
        assert type(received.parameter_output_values["count"]) is int
