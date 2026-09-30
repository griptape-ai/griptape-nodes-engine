"""How FlowManager's teardown paths log the node_run_timing summary."""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from griptape_nodes.common.node_run_timing import NodeRunRecord, NodeRunStatus
from griptape_nodes.retained_mode.managers.event_manager import EventManager
from griptape_nodes.retained_mode.managers.flow_manager import FlowManager


def _flow_manager_with_live_run(*, cancel_fails: bool) -> FlowManager:
    """A FlowManager mid-run, whose in-flight node stops while the run is being cancelled."""
    flow_manager = FlowManager(MagicMock(spec=EventManager), engine=MagicMock())
    flow_manager.run_timer.start_run()

    machine = MagicMock()
    machine.current_state = MagicMock()  # Truthy and not CompleteState: a run in progress.
    machine.resolution_machine.is_complete.return_value = False
    machine.resolution_machine.is_started.return_value = True

    async def cancel_flow() -> None:
        flow_manager.run_timer.record(
            NodeRunRecord(
                node_name="WindingDown",
                node_type="SlowNode",
                parent_name=None,
                started_at=0.0,
                finished_at=1.0,
                status=NodeRunStatus.CANCELLED,
            )
        )
        if cancel_fails:
            msg = "Cancelling the run also failed"
            raise RuntimeError(msg)

    machine.cancel_flow = AsyncMock(side_effect=cancel_flow)
    flow_manager._global_control_flow_machine = machine
    return flow_manager


class TestAbandoningAFailedRun:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("cancel_fails", [False, True])
    async def test_logs_a_failed_summary_with_the_nodes_that_stopped_during_the_cancel(
        self, caplog: pytest.LogCaptureFixture, *, cancel_fails: bool
    ) -> None:
        flow_manager = _flow_manager_with_live_run(cancel_fails=cancel_fails)

        with caplog.at_level(logging.INFO, logger="griptape_nodes"):
            await flow_manager._abandon_running_flow()

        summaries = [record.getMessage() for record in caplog.records if record.getMessage().startswith("RUN SUMMARY")]
        assert len(summaries) == 1
        assert summaries[0].startswith("RUN SUMMARY: failed")
        assert "'WindingDown' (SlowNode)  CANCELLED" in summaries[0]


class TestCancellingARun:
    @pytest.mark.asyncio
    async def test_logs_a_cancelled_summary_after_the_nodes_stop(self, caplog: pytest.LogCaptureFixture) -> None:
        flow_manager = _flow_manager_with_live_run(cancel_fails=False)

        with caplog.at_level(logging.INFO, logger="griptape_nodes"):
            await flow_manager.cancel_flow_run()

        summaries = [record.getMessage() for record in caplog.records if record.getMessage().startswith("RUN SUMMARY")]
        assert len(summaries) == 1
        assert summaries[0].startswith("RUN SUMMARY: cancelled")
        assert "'WindingDown'" in summaries[0]
