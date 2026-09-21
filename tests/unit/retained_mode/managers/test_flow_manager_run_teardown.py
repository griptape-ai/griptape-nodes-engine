"""A run that dies mid-drive must leave the engine restartable.

`FSM._advance` leaves `_current_state` pointing at whatever state it was entering when a state
raised, and `check_for_existing_running_flow` reads that state. So anything that starts a run owns
the cleanup: if it does not reset the machine on the way out, the engine reports a run in progress
forever, the Run button never clears, and every later start is refused.

The graceful path is `cancel_flow_run`, but it awaits every node's cancellation and can fail in its
own right. When it does, the reset still has to happen -- a failure to cancel politely must not be
the reason the engine wedges permanently -- and it must not replace the error that actually ended
the run, which is the one worth reporting.
"""

from typing import NamedTuple
from unittest.mock import AsyncMock, MagicMock

import pytest

from griptape_nodes.retained_mode.managers import flow_manager as flow_manager_module
from griptape_nodes.retained_mode.managers.event_manager import EventManager
from griptape_nodes.retained_mode.managers.flow_manager import FlowManager


class _DoomedRun(NamedTuple):
    flow_manager: FlowManager
    machine: MagicMock


_RUN_FAILURE = "Node blew up mid-run"
_CANCEL_FAILURE = "Cancelling the run also blew up"


def _doomed_run(monkeypatch: pytest.MonkeyPatch, *, cancel_also_fails: bool = False) -> _DoomedRun:
    """A FlowManager whose next run will raise, with a machine that reports itself live.

    `reset_machine` drops the state the liveness check reads, the way the real one does, so the
    tests can assert on the check itself rather than on the call that is supposed to satisfy it.
    """
    flow_manager = FlowManager(MagicMock(spec=EventManager), engine=MagicMock())

    machine = MagicMock()
    machine.current_state = MagicMock()  # Truthy and not CompleteState: a run in progress.
    machine.resolution_machine.is_complete.return_value = False
    machine.resolution_machine.is_started.return_value = True
    machine.start_flow = AsyncMock(side_effect=RuntimeError(_RUN_FAILURE))

    def reset_machine(*, cancel: bool = False) -> None:  # noqa: ARG001
        machine.current_state = None
        machine.resolution_machine.is_started.return_value = False

    machine.reset_machine = MagicMock(side_effect=reset_machine)

    if cancel_also_fails:
        machine.cancel_flow = AsyncMock(side_effect=RuntimeError(_CANCEL_FAILURE))
    else:
        machine.cancel_flow = AsyncMock()

    monkeypatch.setattr(flow_manager_module, "ControlFlowMachine", MagicMock(return_value=machine))
    flow_manager._global_dag_builder.node_to_reference["Doomed"] = MagicMock()

    return _DoomedRun(flow_manager=flow_manager, machine=machine)


def _assert_engine_is_restartable(flow_manager: FlowManager) -> None:
    assert flow_manager.check_for_existing_running_flow() is False, (
        "The engine still reports a run in progress, so the Run button stays lit and every later start is refused."
    )
    assert flow_manager.global_single_node_resolution is False
    assert flow_manager._global_dag_builder.node_to_reference == {}


class TestStartFlowCleansUpAfterAFailedRun:
    @pytest.mark.asyncio
    async def test_reports_the_run_failure_and_leaves_the_engine_restartable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        flow_manager, _ = _doomed_run(monkeypatch)

        with pytest.raises(RuntimeError, match=_RUN_FAILURE):
            await flow_manager.start_flow(MagicMock(), MagicMock())

        _assert_engine_is_restartable(flow_manager)

    @pytest.mark.asyncio
    async def test_a_failed_cancellation_neither_wedges_the_engine_nor_hides_the_run_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        flow_manager, machine = _doomed_run(monkeypatch, cancel_also_fails=True)

        # The error worth reporting is the one that ended the run, not the one raised while
        # clearing up after it.
        with pytest.raises(RuntimeError, match=_RUN_FAILURE):
            await flow_manager.start_flow(MagicMock(), MagicMock())

        machine.cancel_flow.assert_awaited_once()
        _assert_engine_is_restartable(flow_manager)


class TestResolveSingularNodeCleansUpAfterAFailedRun:
    @pytest.mark.asyncio
    async def test_reports_the_run_failure_and_leaves_the_engine_restartable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        flow_manager, _ = _doomed_run(monkeypatch)

        with pytest.raises(RuntimeError, match=_RUN_FAILURE):
            await flow_manager.resolve_singular_node(MagicMock(), MagicMock())

        _assert_engine_is_restartable(flow_manager)

    @pytest.mark.asyncio
    async def test_a_failed_cancellation_neither_wedges_the_engine_nor_hides_the_run_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        flow_manager, machine = _doomed_run(monkeypatch, cancel_also_fails=True)

        with pytest.raises(RuntimeError, match=_RUN_FAILURE):
            await flow_manager.resolve_singular_node(MagicMock(), MagicMock())

        machine.cancel_flow.assert_awaited_once()
        _assert_engine_is_restartable(flow_manager)

    @pytest.mark.asyncio
    async def test_single_node_mode_is_cleared_even_when_the_run_never_got_started(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The flag is raised before the run begins, so a run that never begins still has to drop it.

        Here the failure happens with nothing yet live, so the liveness check reads false and the
        graceful cancel is skipped -- which used to mean nothing cleared the flag at all, and the
        engine went on treating single-node mode as active for the rest of the session.
        """
        flow_manager, machine = _doomed_run(monkeypatch)
        machine.current_state = None
        machine.resolution_machine.is_started.return_value = False

        with pytest.raises(RuntimeError, match=_RUN_FAILURE):
            await flow_manager.resolve_singular_node(MagicMock(), MagicMock())

        machine.cancel_flow.assert_not_awaited()
        _assert_engine_is_restartable(flow_manager)
