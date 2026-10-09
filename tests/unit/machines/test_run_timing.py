"""Node and run timings ride on the execution events, so the editor can show where a run spent its time."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.machines.control_flow import ControlFlowContext
from griptape_nodes.machines.dag_builder import DagNode
from griptape_nodes.machines.parallel_resolution import ExecuteDagState
from griptape_nodes.retained_mode.events.execution_events import ControlFlowCancelledEvent
from griptape_nodes.retained_mode.managers.event_manager import EventManager
from griptape_nodes.retained_mode.managers.flow_manager import FlowManager


def _dag_node() -> DagNode:
    return DagNode(node_reference=MagicMock(spec=BaseNode))


def _engine_whose_executor(execute: AsyncMock) -> MagicMock:
    engine = MagicMock()
    engine.flow_manager.node_executor.execute = execute
    return engine


def _context() -> ControlFlowContext:
    context = ControlFlowContext.__new__(ControlFlowContext)
    context.current_nodes = []
    context.resolution_machine = MagicMock()
    return context


class TestNodeRunSeconds:
    @pytest.mark.asyncio
    async def test_a_node_that_runs_records_how_long_it_took(self) -> None:
        dag_node = _dag_node()

        await ExecuteDagState.execute_node(_engine_whose_executor(AsyncMock()), dag_node)

        assert dag_node.run_seconds is not None
        assert dag_node.run_seconds >= 0

    @pytest.mark.asyncio
    async def test_a_node_that_fails_still_records_how_long_it_ran(self) -> None:
        dag_node = _dag_node()
        engine = _engine_whose_executor(AsyncMock(side_effect=RuntimeError("boom")))

        with pytest.raises(RuntimeError, match="boom"):
            await ExecuteDagState.execute_node(engine, dag_node)

        assert dag_node.run_seconds is not None


class TestRunSeconds:
    def test_no_run_started_means_no_run_time(self) -> None:
        assert _context().seconds_since_run_started() is None

    def test_a_started_run_reports_elapsed_time_until_reset(self) -> None:
        context = _context()
        context.run_started_at = 0.0

        run_seconds = context.seconds_since_run_started()
        assert run_seconds is not None
        assert run_seconds > 0

        context.reset()
        assert context.seconds_since_run_started() is None

    @pytest.mark.asyncio
    async def test_cancelling_a_run_reports_how_long_it_ran(self) -> None:
        engine = MagicMock()
        flow_manager = FlowManager(MagicMock(spec=EventManager), engine=engine)
        machine = MagicMock()
        machine.cancel_flow = AsyncMock()
        machine.context.seconds_since_run_started.return_value = 1.5
        # Resetting the real machine clears the run's start time, so the time must be read first.
        machine.reset_machine.side_effect = lambda **_: setattr(
            machine.context.seconds_since_run_started, "return_value", None
        )
        flow_manager._global_control_flow_machine = machine
        flow_manager.check_for_existing_running_flow = MagicMock(return_value=True)

        await flow_manager.cancel_flow_run()

        payloads = [call.args[0].wrapped_event.payload for call in engine.event_manager.put_event.call_args_list]
        cancelled = [p for p in payloads if isinstance(p, ControlFlowCancelledEvent)]
        assert [p.run_seconds for p in cancelled] == [1.5]
