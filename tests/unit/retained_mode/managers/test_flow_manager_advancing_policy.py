"""Step and continue requests must not become a second driver of a running flow.

A continuously running flow is one long drive held by whoever started it, so a
step or continue request arriving mid-drive cannot advance the machine without
corrupting its task bookkeeping. FlowManager checks `execution_is_advancing()`
and declines to drive.

Pausing is the exception: `single_execution_step` is the only path that pauses a
live run, so it must still raise the paused flag even when it declines to drive.
The running driver then stops itself once its current node finishes.
"""

from typing import NamedTuple
from unittest.mock import MagicMock

import pytest

from griptape_nodes.retained_mode.managers.event_manager import EventManager
from griptape_nodes.retained_mode.managers.flow_manager import FlowManager


class _AdvancingRun(NamedTuple):
    flow_manager: FlowManager
    machine: MagicMock


def _flow_manager_with_advancing_machine() -> _AdvancingRun:
    """A FlowManager whose committed run is mid-drive."""
    flow_manager = FlowManager(MagicMock(spec=EventManager), engine=MagicMock())

    machine = MagicMock()
    machine.is_advancing = True
    machine.resolution_machine.is_advancing = True
    flow_manager._global_control_flow_machine = machine

    return _AdvancingRun(flow_manager=flow_manager, machine=machine)


class TestExecutionIsAdvancing:
    def test_reports_false_with_no_machine(self) -> None:
        flow_manager = FlowManager(MagicMock(spec=EventManager), engine=MagicMock())
        assert flow_manager.execution_is_advancing() is False

    def test_reports_true_when_only_the_resolution_machine_is_advancing(self) -> None:
        flow_manager, machine = _flow_manager_with_advancing_machine()
        machine.is_advancing = False
        assert flow_manager.execution_is_advancing() is True

    def test_reports_false_when_nothing_is_advancing(self) -> None:
        flow_manager, machine = _flow_manager_with_advancing_machine()
        machine.is_advancing = False
        machine.resolution_machine.is_advancing = False
        assert flow_manager.execution_is_advancing() is False


class TestStepRequestsDeclineToDoubleDrive:
    @pytest.mark.asyncio
    async def test_single_execution_step_pauses_instead_of_driving(self, monkeypatch: pytest.MonkeyPatch) -> None:
        flow_manager, machine = _flow_manager_with_advancing_machine()

        monkeypatch.setattr(flow_manager, "check_for_existing_running_flow", lambda: True)
        await flow_manager.single_execution_step(MagicMock(), change_debug_mode=True)

        # Pausing a live run is the whole point of this request; dropping it
        # would leave the flow unpausable while reporting success.
        machine.resolution_machine.change_debug_mode.assert_called_once_with(debug_mode=True)
        machine.granular_step.assert_not_called()

    @pytest.mark.asyncio
    async def test_single_node_step_resumes_instead_of_driving(self, monkeypatch: pytest.MonkeyPatch) -> None:
        flow_manager, machine = _flow_manager_with_advancing_machine()

        monkeypatch.setattr(flow_manager, "check_for_existing_running_flow", lambda: True)
        await flow_manager.single_node_step(MagicMock())

        # Dropping the resume would leave a run the user asked to step sitting
        # paused, with the request reporting success.
        machine.resolution_machine.change_debug_mode.assert_called_once_with(debug_mode=False)
        machine.node_step.assert_not_called()

    @pytest.mark.asyncio
    async def test_continue_executing_resumes_instead_of_driving(self, monkeypatch: pytest.MonkeyPatch) -> None:
        flow_manager, machine = _flow_manager_with_advancing_machine()

        monkeypatch.setattr(flow_manager, "check_for_existing_running_flow", lambda: True)
        await flow_manager.continue_executing(MagicMock())

        # A Continue pressed while the current node is still running must still
        # clear the paused flag, or the run halts once that node finishes.
        machine.change_debug_mode.assert_called_once_with(False)
        machine.node_step.assert_not_called()
