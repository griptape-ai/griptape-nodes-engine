"""Tests for output copy-back behavior in NodeExecutor.execute().

NodeExecutor.execute dispatches a single ExecuteNodeRequest for both local and
worker execution. After the handler returns, outputs are copied back onto the
in-memory node via parameter_output_values (not set_parameter_value). The
copy-back is idempotent: TrackedParameterOutputValues.__setitem__ guards on
old_value != new_value, so on the local path -- where aprocess already wrote
these entries in place -- no duplicate AlterElementEvent is emitted. On the
worker path the orchestrator stub has not seen the writes, so the copy-back
is the first (and only) emit per key.
"""

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.common.node_executor import NodeExecutor
from griptape_nodes.exe_types.node_types import TrackedParameterOutputValues
from griptape_nodes.retained_mode.events.execution_events import ExecuteNodeResultSuccess

_EXPECTED_FRESH_OUTPUT_EMITS = 2


def _make_executor() -> NodeExecutor:
    return NodeExecutor(engine=MagicMock())


def _make_node_with_tracked_outputs(name: str = "TestNode") -> MagicMock:
    """Mock node with a real TrackedParameterOutputValues so __setitem__ guards run."""
    node = MagicMock()
    node.name = name
    node.parameter_values = {}
    node.parameter_output_values = TrackedParameterOutputValues(node)
    node.metadata = {}
    return node


class TestLocalExecuteCopyBack:
    """Copy-back writes into parameter_output_values, not via set_parameter_value."""

    @pytest.mark.asyncio
    async def test_copy_back_does_not_call_set_parameter_value(self) -> None:
        """Copy-back must not route through BaseNode.set_parameter_value."""
        node = _make_node_with_tracked_outputs()
        result = ExecuteNodeResultSuccess(result_details="ok", parameter_output_values={"out": 42})

        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.ahandle_request = AsyncMock(return_value=result)
        await executor.execute(node)

        assert node.parameter_output_values == {"out": 42}
        node.set_parameter_value.assert_not_called()

    @pytest.mark.asyncio
    async def test_copy_back_emits_once_per_key_when_aprocess_already_wrote(self) -> None:
        """Idempotent copy-back: aprocess-writes + copy-back = exactly one emit per key."""
        node = _make_node_with_tracked_outputs()

        # Simulate in-process execution: the handler writes directly onto
        # node.parameter_output_values (via aprocess), then returns a result
        # whose parameter_output_values is a dict copy of that same state.
        async def fake_handle(_req: Any) -> ExecuteNodeResultSuccess:
            node.parameter_output_values["out"] = 42  # first (and should-be-only) emit
            return ExecuteNodeResultSuccess(
                result_details="ok",
                parameter_output_values=dict(node.parameter_output_values),
            )

        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.ahandle_request = AsyncMock(side_effect=fake_handle)
        with patch.object(TrackedParameterOutputValues, "_emit_parameter_change_event") as mock_emit:
            await executor.execute(node)

        assert mock_emit.call_count == 1
        assert node.parameter_output_values == {"out": 42}

    @pytest.mark.asyncio
    async def test_copy_back_emits_for_fresh_outputs_on_worker_path(self) -> None:
        """Worker path: copy-back is a first-time assignment per key, one emit per key.

        Simulated by returning a result whose outputs are not yet on the node --
        matches the orchestrator's view of a node whose aprocess ran remotely.
        """
        node = _make_node_with_tracked_outputs()

        async def fake_handle(_req: Any) -> ExecuteNodeResultSuccess:
            return ExecuteNodeResultSuccess(
                result_details="ok",
                parameter_output_values={"a": 1, "b": 2},
            )

        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.ahandle_request = AsyncMock(side_effect=fake_handle)
        with patch.object(TrackedParameterOutputValues, "_emit_parameter_change_event") as mock_emit:
            await executor.execute(node)

        # Two fresh keys; each __setitem__ sees old_value None != new_value.
        assert mock_emit.call_count == _EXPECTED_FRESH_OUTPUT_EMITS
        assert node.parameter_output_values == {"a": 1, "b": 2}
