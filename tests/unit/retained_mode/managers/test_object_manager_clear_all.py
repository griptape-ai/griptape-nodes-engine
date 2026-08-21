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

from unittest.mock import AsyncMock, MagicMock

import pytest

from griptape_nodes.retained_mode.events.object_events import (
    ClearAllObjectStateRequest,
    ClearAllObjectStateResultSuccess,
)
from griptape_nodes.retained_mode.managers.object_manager import ObjectManager


class TestClearAllObjectStateCancelsFirst:
    @pytest.mark.asyncio
    async def test_running_flow_is_cancelled_before_execution_state_is_reset(self) -> None:
        calls: list[str] = []

        engine = MagicMock()
        engine.flow_manager.check_for_existing_running_flow.return_value = True
        engine.flow_manager.cancel_flow_run = AsyncMock(side_effect=lambda: calls.append("cancel"))
        engine.flow_manager.reset_global_execution_state = MagicMock(side_effect=lambda: calls.append("reset"))
        engine.context_manager.has_current_workflow.return_value = False

        object_manager = ObjectManager(MagicMock(), engine=engine)
        result = await object_manager.on_clear_all_object_state_request(
            ClearAllObjectStateRequest(i_know_what_im_doing=True)
        )

        assert isinstance(result, ClearAllObjectStateResultSuccess)
        assert calls == ["cancel", "reset"], "the reset must not run ahead of the cancel"

    @pytest.mark.asyncio
    async def test_idle_engine_is_not_cancelled(self) -> None:
        engine = MagicMock()
        engine.flow_manager.check_for_existing_running_flow.return_value = False
        engine.flow_manager.cancel_flow_run = AsyncMock()
        engine.context_manager.has_current_workflow.return_value = False

        object_manager = ObjectManager(MagicMock(), engine=engine)
        result = await object_manager.on_clear_all_object_state_request(
            ClearAllObjectStateRequest(i_know_what_im_doing=True)
        )

        assert isinstance(result, ClearAllObjectStateResultSuccess)
        engine.flow_manager.cancel_flow_run.assert_not_awaited()
        engine.flow_manager.reset_global_execution_state.assert_called_once()
