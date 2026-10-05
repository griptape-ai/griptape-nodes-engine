"""Tests for how ControlFlowMachine starts and finishes node_run_timing runs."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from griptape_nodes.common.node_run_timing import RunOutcome
from griptape_nodes.machines.control_flow import CompleteState, ControlFlowMachine, ResolveNodeState
from griptape_nodes.machines.fsm import WorkflowState
from griptape_nodes.machines.parallel_resolution import ParallelResolutionMachine
from griptape_nodes.retained_mode.beta_features import NODE_RUN_TIMING
from griptape_nodes.retained_mode.managers.settings import LOG_NODE_RUN_TIMING_KEY


def _make_context(*, is_isolated: bool = False, errored: bool = False, canceled: bool = False) -> MagicMock:
    context = MagicMock()
    context.is_isolated = is_isolated
    context.current_nodes = []
    context.resolution_machine.is_errored.return_value = errored
    context.resolution_machine.is_canceled.return_value = canceled
    return context


def _make_machine(context: MagicMock) -> ControlFlowMachine:
    # Skip __init__, which builds a real resolution machine; start_flow only needs the context.
    machine = ControlFlowMachine.__new__(ControlFlowMachine)
    machine._context = context
    machine._process_nodes_for_dag = AsyncMock(return_value=[])
    machine.start = AsyncMock()
    return machine


class TestCompleteStateFinishesTheRun:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("errored", "canceled", "outcome"),
        [
            (False, False, RunOutcome.COMPLETED),
            (True, False, RunOutcome.FAILED),
            (False, True, RunOutcome.CANCELLED),
            (True, True, RunOutcome.FAILED),
        ],
    )
    async def test_finishes_with_the_resolution_outcome(
        self, *, errored: bool, canceled: bool, outcome: RunOutcome
    ) -> None:
        context = _make_context(errored=errored, canceled=canceled)

        await CompleteState.on_enter(context)

        run_timer = context.engine.flow_manager.run_timer
        run_timer.finish_run.assert_called_once_with(outcome)

    @pytest.mark.asyncio
    async def test_isolated_flow_leaves_the_run_open(self) -> None:
        context = _make_context(is_isolated=True)

        await CompleteState.on_enter(context)

        context.engine.flow_manager.run_timer.finish_run.assert_not_called()


class TestStartFlowStartsTheRun:
    @staticmethod
    def _set_timing(context: MagicMock, *, enabled: bool) -> None:
        config = {NODE_RUN_TIMING.config_key: enabled}
        context.engine.config_manager.get_config_value.side_effect = lambda key, default=None, **_kwargs: config.get(
            key, default
        )

    @pytest.mark.asyncio
    async def test_starts_timing_when_enabled(self) -> None:
        context = _make_context()
        self._set_timing(context, enabled=True)
        machine = _make_machine(context)
        node = MagicMock()

        await machine.start_flow(node, node)

        context.engine.flow_manager.run_timer.start_run.assert_called_once_with()
        cast("AsyncMock", machine.start).assert_awaited_once_with(ResolveNodeState)

    @pytest.mark.asyncio
    async def test_does_not_start_timing_when_disabled(self) -> None:
        context = _make_context()
        self._set_timing(context, enabled=False)
        machine = _make_machine(context)
        node = MagicMock()

        await machine.start_flow(node, node)

        context.engine.flow_manager.run_timer.start_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_start_timing_when_the_logging_setting_is_off(self) -> None:
        context = _make_context()
        config = {NODE_RUN_TIMING.config_key: True, LOG_NODE_RUN_TIMING_KEY: False}
        context.engine.config_manager.get_config_value.side_effect = lambda key, default=None, **_kwargs: config.get(
            key, default
        )
        machine = _make_machine(context)
        node = MagicMock()

        await machine.start_flow(node, node)

        context.engine.flow_manager.run_timer.start_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_isolated_flow_does_not_restart_timing(self) -> None:
        context = _make_context(is_isolated=True)
        self._set_timing(context, enabled=True)
        machine = _make_machine(context)
        node = MagicMock()

        await machine.start_flow(node, node)

        context.engine.flow_manager.run_timer.start_run.assert_not_called()


class TestParallelResolutionIsCanceled:
    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            (WorkflowState.CANCELED, True),
            (WorkflowState.ERRORED, False),
            (WorkflowState.NO_ERROR, False),
        ],
    )
    def test_reports_cancellation_from_workflow_state(self, state: WorkflowState, *, expected: bool) -> None:
        machine = ParallelResolutionMachine("flow", dag_builder=MagicMock(), engine=MagicMock())
        machine.context.workflow_state = state

        assert machine.is_canceled() is expected
