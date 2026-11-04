"""Clearing all object state must cancel an in-flight run before tearing it down.

`reset_global_execution_state()` drops the run's task bookkeeping and nulls its
machine state, which makes the flow look idle. Anything that cancels on the
strength of `check_for_existing_running_flow()` afterwards -- including
`Engine.clear_current_workflow_data`, whose whole job here is to "cancel any
running flow so the delete path doesn't race with execution" -- is then a no-op,
and the previous run's node tasks carry on: still billing API calls, and still
emitting parameter updates keyed by node name that land on the same-named nodes
of whatever run is loaded next.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from griptape_nodes.retained_mode.events.object_events import (
    ClearAllObjectStateRequest,
    ClearAllObjectStateResultSuccess,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine


class TestClearAllObjectStateCancelsFirst:
    @pytest.mark.asyncio
    async def test_running_flow_is_cancelled_before_execution_state_is_reset(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="running")
        calls: list[str] = []
        flow_manager = engine.flow_manager

        async def cancel() -> None:
            calls.append("cancel")

        with (
            patch.object(flow_manager, "cancel_all_runs", AsyncMock(side_effect=cancel)),
            patch.object(flow_manager, "reset_global_execution_state", side_effect=lambda: calls.append("reset")),
        ):
            result = await engine.ahandle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

        assert isinstance(result, ClearAllObjectStateResultSuccess), result.result_details
        assert calls[0] == "cancel", "the runs are cancelled first"
        assert calls[-1] == "reset", "the reset must not run ahead of the cancel"

    @pytest.mark.asyncio
    async def test_a_cancel_that_fails_does_not_stop_the_clear(self, engine: Engine) -> None:
        """A node that refuses to cancel must not leave the workflow half torn down."""
        engine.context_manager.push_workflow(workflow_name="stuck")
        flow_manager = engine.flow_manager

        with (
            patch.object(flow_manager, "cancel_all_runs", AsyncMock(side_effect=RuntimeError("a node would not stop"))),
            patch.object(flow_manager, "reset_global_execution_state") as reset,
        ):
            result = await engine.ahandle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

        assert isinstance(result, ClearAllObjectStateResultSuccess), result.result_details
        reset.assert_called_once()
        assert engine.open_workflows.all_open() == []

    @pytest.mark.asyncio
    async def test_idle_engine_is_not_cancelled(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="idle")
        flow_manager = engine.flow_manager

        with (
            patch.object(flow_manager, "_cancel_run", AsyncMock()) as cancel,
            patch.object(flow_manager, "reset_global_execution_state") as reset,
        ):
            result = await engine.ahandle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

        assert isinstance(result, ClearAllObjectStateResultSuccess), result.result_details
        cancel.assert_not_awaited()
        reset.assert_called_once()
