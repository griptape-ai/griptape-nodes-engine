"""Tests for the node_run_timing beta feature in NodeExecutor.execute()."""

import asyncio
import logging
import re
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from griptape_nodes.common.node_executor import NodeExecutor, canvas_names_for_loop_copies
from griptape_nodes.common.node_run_timing import NodeRunTimer, RunOutcome
from griptape_nodes.retained_mode.beta_features import NODE_RUN_TIMING
from griptape_nodes.retained_mode.events.execution_events import ExecuteNodeRequest, ExecuteNodeResultSuccess
from griptape_nodes.retained_mode.managers.settings import LOG_NODE_RUN_TIMING_KEY


def _make_executor(config: dict[str, object]) -> NodeExecutor:
    executor = NodeExecutor(engine=MagicMock())
    mock_engine = cast("MagicMock", executor.engine)
    mock_engine.config_manager.get_config_value.side_effect = lambda key, default=None, **_kwargs: config.get(
        key, default
    )
    mock_engine.flow_manager.run_timer = NodeRunTimer()
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
    async def test_logs_nothing_when_the_logging_setting_is_off(self, caplog: pytest.LogCaptureFixture) -> None:
        executor = _make_executor({NODE_RUN_TIMING.config_key: True, LOG_NODE_RUN_TIMING_KEY: False})

        with caplog.at_level(logging.INFO, logger="griptape_nodes"):
            await executor.execute(_make_node())

        assert not any("TIME TO RUN" in record.getMessage() for record in caplog.records)

    @pytest.mark.asyncio
    async def test_logging_setting_alone_does_not_turn_timing_on(self, caplog: pytest.LogCaptureFixture) -> None:
        executor = _make_executor({LOG_NODE_RUN_TIMING_KEY: True})

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
        executor.engine.flow_manager.run_timer.start_run()
        await executor.execute(_make_node("Loop"))

        with caplog.at_level(logging.INFO, logger="griptape_nodes"):
            executor.engine.flow_manager.run_timer.finish_run(RunOutcome.COMPLETED)

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
        executor.engine.flow_manager.run_timer.start_run()

        with pytest.raises(RuntimeError):
            await executor.execute(_make_node())
        with caplog.at_level(logging.INFO, logger="griptape_nodes"):
            executor.engine.flow_manager.run_timer.finish_run(RunOutcome.FAILED)

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
        executor.engine.flow_manager.run_timer.start_run()

        with pytest.raises(asyncio.CancelledError):
            await executor.execute(_make_node())
        with caplog.at_level(logging.INFO, logger="griptape_nodes"):
            executor.engine.flow_manager.run_timer.finish_run(RunOutcome.CANCELLED)

        assert "'TestNode' (MagicMock)  CANCELLED" in caplog.text

    @pytest.mark.asyncio
    async def test_loop_copies_are_recorded_under_their_canvas_names(self, caplog: pytest.LogCaptureFixture) -> None:
        executor = _make_executor({NODE_RUN_TIMING.config_key: True})

        async def run_iterations_during_loop(request: ExecuteNodeRequest) -> ExecuteNodeResultSuccess:
            # Each parallel iteration runs its own renamed copy of the body node.
            if request.node_name == "Loop":

                async def run_iteration(copy_name: str) -> None:
                    with canvas_names_for_loop_copies({copy_name: "Upscale"}):
                        await executor.execute(_make_node(copy_name))

                await asyncio.gather(*(run_iteration(f"Upscale_{i}") for i in range(1, 4)))
            return ExecuteNodeResultSuccess(result_details="ok", parameter_output_values={})

        cast("MagicMock", executor.engine).ahandle_request = AsyncMock(side_effect=run_iterations_during_loop)
        executor.engine.flow_manager.run_timer.start_run()
        await executor.execute(_make_node("Loop"))

        with caplog.at_level(logging.INFO, logger="griptape_nodes"):
            executor.engine.flow_manager.run_timer.finish_run(RunOutcome.COMPLETED)

        summaries = [record.getMessage() for record in caplog.records if record.getMessage().startswith("RUN SUMMARY")]
        assert len(summaries) == 1
        assert ", 2 nodes ran" in summaries[0]
        assert re.search(r"└── \d+\.\d{3} s  'Upscale' \(MagicMock\) x3$", summaries[0], re.MULTILINE)
        assert "Upscale_" not in summaries[0]

    @pytest.mark.asyncio
    async def test_nested_loop_copies_resolve_to_canvas_names(self) -> None:
        executor = _make_executor({NODE_RUN_TIMING.config_key: True})
        timer = MagicMock()
        cast("MagicMock", executor.engine).flow_manager.run_timer = timer

        async def run_inner_iteration(request: ExecuteNodeRequest) -> ExecuteNodeResultSuccess:
            # The inner loop's body is a copy of a copy: Body -> Body_1 -> Body_1_1.
            if request.node_name == "Inner_1":
                with canvas_names_for_loop_copies({"Body_1_1": "Body_1"}):
                    await executor.execute(_make_node("Body_1_1"))
            return ExecuteNodeResultSuccess(result_details="ok", parameter_output_values={})

        cast("MagicMock", executor.engine).ahandle_request = AsyncMock(side_effect=run_inner_iteration)
        with canvas_names_for_loop_copies({"Inner_1": "Inner", "Body_1": "Body"}):
            await executor.execute(_make_node("Inner_1"))

        body, inner = (call.args[0] for call in timer.record.call_args_list)
        assert (body.node_name, body.parent_name) == ("Body", "Inner")
        assert inner.node_name == "Inner"
