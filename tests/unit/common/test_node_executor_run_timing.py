"""Tests for the node_run_timing beta feature in NodeExecutor.execute()."""

import asyncio
import logging
import re
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from griptape_nodes.common.node_executor import NodeExecutor
from griptape_nodes.common.node_run_timing import RunOutcome
from griptape_nodes.retained_mode.beta_features import NODE_RUN_TIMING
from griptape_nodes.retained_mode.events.execution_events import ExecuteNodeRequest, ExecuteNodeResultSuccess


def _make_executor(config: dict[str, object]) -> NodeExecutor:
    executor = NodeExecutor(engine=MagicMock())
    mock_engine = cast("MagicMock", executor.engine)
    mock_engine.config_manager.get_config_value.side_effect = lambda key, **_kwargs: config.get(key)
    mock_engine.ahandle_request = AsyncMock(
        return_value=ExecuteNodeResultSuccess(result_details="ok", parameter_output_values={})
    )
    return executor


def _make_node(name: str = "TestNode") -> MagicMock:
    node = MagicMock()
    node.name = name
    node.metadata = {}
    node.parameter_values = {}
    node.parameter_output_values = {}
    return node


class TestNodeRunTiming:
    @pytest.mark.asyncio
    async def test_logs_time_to_run_when_enabled(self, caplog: pytest.LogCaptureFixture) -> None:
        executor = _make_executor({NODE_RUN_TIMING.config_key: True})

        with caplog.at_level(logging.INFO, logger="griptape_nodes"):
            await executor.execute(_make_node())

        messages = [record.getMessage() for record in caplog.records]
        assert any(re.fullmatch(r"TIME TO RUN: \d+\.\d{3} s for 'TestNode' \(MagicMock\)", m) for m in messages)

    @pytest.mark.asyncio
    async def test_logs_nothing_when_disabled(self, caplog: pytest.LogCaptureFixture) -> None:
        executor = _make_executor({})

        with caplog.at_level(logging.INFO, logger="griptape_nodes"):
            await executor.execute(_make_node())

        assert not any("TIME TO RUN" in record.getMessage() for record in caplog.records)

    @pytest.mark.asyncio
    async def test_records_the_run_for_the_summary(self, caplog: pytest.LogCaptureFixture) -> None:
        executor = _make_executor({NODE_RUN_TIMING.config_key: True})
        child = _make_node("Body")

        async def run_child_during_parent(request: ExecuteNodeRequest) -> ExecuteNodeResultSuccess:
            if request.node_name == "Loop":
                await executor.execute(child)
            return ExecuteNodeResultSuccess(result_details="ok", parameter_output_values={})

        cast("MagicMock", executor.engine).ahandle_request = AsyncMock(side_effect=run_child_during_parent)
        executor.run_timer.start_run()
        await executor.execute(_make_node("Loop"))

        with caplog.at_level(logging.INFO, logger="griptape_nodes"):
            executor.run_timer.finish_run(RunOutcome.COMPLETED)

        # The child ran while the parent was executing, so it is listed indented under it.
        # The TIME TO RUN lines are captured too when the logger is already at INFO, so pick the
        # summary out by its text rather than its position.
        summaries = [record.getMessage() for record in caplog.records if record.getMessage().startswith("RUN SUMMARY")]
        assert len(summaries) == 1
        lines = summaries[0].splitlines()
        assert re.fullmatch(r"    └── \d+\.\d{3} s  'Loop' \(MagicMock\)", lines[2])
        assert re.fullmatch(r"        └── \d+\.\d{3} s  'Body' \(MagicMock\)", lines[3])

    @pytest.mark.asyncio
    async def test_records_a_failed_node_as_failed(self, caplog: pytest.LogCaptureFixture) -> None:
        executor = _make_executor({NODE_RUN_TIMING.config_key: True})
        cast("MagicMock", executor.engine).ahandle_request = AsyncMock(side_effect=RuntimeError("node failed"))
        executor.run_timer.start_run()

        with pytest.raises(RuntimeError):
            await executor.execute(_make_node())
        with caplog.at_level(logging.INFO, logger="griptape_nodes"):
            executor.run_timer.finish_run(RunOutcome.FAILED)

        assert "'TestNode' (MagicMock)  FAILED" in caplog.text

    @pytest.mark.asyncio
    async def test_logs_time_to_run_when_the_node_fails(self, caplog: pytest.LogCaptureFixture) -> None:
        executor = _make_executor({NODE_RUN_TIMING.config_key: True})
        cast("MagicMock", executor.engine).ahandle_request = AsyncMock(side_effect=RuntimeError("node failed"))

        with caplog.at_level(logging.INFO, logger="griptape_nodes"), pytest.raises(RuntimeError):
            await executor.execute(_make_node())

        assert any(record.getMessage().startswith("TIME TO RUN: ") for record in caplog.records)

    @pytest.mark.asyncio
    async def test_records_a_cancelled_node_as_cancelled(self, caplog: pytest.LogCaptureFixture) -> None:
        executor = _make_executor({NODE_RUN_TIMING.config_key: True})
        cast("MagicMock", executor.engine).ahandle_request = AsyncMock(side_effect=asyncio.CancelledError)
        executor.run_timer.start_run()

        with pytest.raises(asyncio.CancelledError):
            await executor.execute(_make_node())
        with caplog.at_level(logging.INFO, logger="griptape_nodes"):
            executor.run_timer.finish_run(RunOutcome.CANCELLED)

        assert "'TestNode' (MagicMock)  CANCELLED" in caplog.text
