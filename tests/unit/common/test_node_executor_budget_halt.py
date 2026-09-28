"""A budget refusal inside a loop body stops the loop instead of being collected as one failed iteration.

Every later iteration would spend against the same budget and be refused in turn, so each loop
runner -- sequential, while, and parallel -- raises the halt in its own words the moment it sees
one. The parallel runner also cancels the iterations still in flight.
"""

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from griptape_nodes.common.node_executor import IterationOutcome, NodeExecutor
from griptape_nodes.retained_mode.events.base_events import ResultDetails
from griptape_nodes.retained_mode.events.execution_events import (
    StartLocalSubflowRequest,
    StartLocalSubflowResultFailure,
    StartLocalSubflowResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import DeserializeFlowFromCommandsResultSuccess
from griptape_nodes.utils.budget_refusal import BUDGET_HALT_PREFIX

HALT = f"{BUDGET_HALT_PREFIX} Griptape Cloud refused the next call from 'Upscale'."


def _failure(message: str) -> StartLocalSubflowResultFailure:
    return StartLocalSubflowResultFailure(result_details=ResultDetails(message=message, level=logging.ERROR))


def _success() -> StartLocalSubflowResultSuccess:
    return StartLocalSubflowResultSuccess(result_details="ran")


def _package_result() -> MagicMock:
    """A packaged loop body with one Start node and nothing to silence or map back."""
    package_result = MagicMock()
    package_result.serialized_flow_commands.serialized_node_commands = []
    start_mapping = MagicMock()
    start_mapping.node_name = "Start"
    start_mapping.parameter_mappings = {}
    end_mapping = MagicMock()
    end_mapping.node_name = "End"
    end_mapping.parameter_mappings = {}
    package_result.parameter_name_mappings = [start_mapping, end_mapping]
    return package_result


def _executor(run_subflow: Any) -> NodeExecutor:
    """An executor whose engine deserializes one flow per call and runs subflows with `run_subflow`."""
    engine = MagicMock()
    engine.context_manager.has_current_flow.return_value = False
    engine.object_manager.attempt_get_object_by_name.return_value = None
    flow_count = iter(range(100))

    def deserialize(_request: object) -> DeserializeFlowFromCommandsResultSuccess:
        index = next(flow_count)
        return DeserializeFlowFromCommandsResultSuccess(
            result_details="deserialized",
            flow_name=f"iteration_{index}",
            node_name_mappings={"Start": f"Start_{index}"},
        )

    async def ahandle_request(request: object) -> object:
        if isinstance(request, StartLocalSubflowRequest):
            return await run_subflow(request)
        return MagicMock()

    engine.handle_request.side_effect = deserialize
    engine.ahandle_request.side_effect = ahandle_request
    return NodeExecutor(engine=engine)


class TestRaiseIfBudgetHalt:
    def test_a_halt_is_raised_in_its_own_words(self) -> None:
        with pytest.raises(RuntimeError) as caught:
            NodeExecutor._raise_if_budget_halt(ResultDetails(message=HALT, level=logging.ERROR))

        assert str(caught.value) == HALT

    def test_an_ordinary_failure_lets_the_loop_carry_on(self) -> None:
        NodeExecutor._raise_if_budget_halt(ResultDetails(message="the image was empty", level=logging.ERROR))


class TestBudgetHaltAmong:
    @staticmethod
    async def _finished(outcome: IterationOutcome | Exception | None) -> asyncio.Task[IterationOutcome]:
        """A task that has already finished with `outcome`, or been cancelled when it is None."""

        async def run() -> IterationOutcome:
            if outcome is None:
                await asyncio.Event().wait()
            if isinstance(outcome, Exception):
                raise outcome
            assert outcome is not None
            return outcome

        task = asyncio.ensure_future(run())
        await asyncio.sleep(0)
        if outcome is None:
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return task

    @pytest.mark.asyncio
    async def test_the_halt_is_found_among_other_outcomes(self) -> None:
        finished = {
            await self._finished(None),
            await self._finished(ValueError("crashed")),
            await self._finished(IterationOutcome(iteration_index=0, succeeded=True, detail="")),
            await self._finished(IterationOutcome(iteration_index=1, succeeded=False, detail="the image was empty")),
            await self._finished(IterationOutcome(iteration_index=2, succeeded=False, detail=HALT)),
        }

        assert NodeExecutor._budget_halt_among(finished) == HALT

    @pytest.mark.asyncio
    async def test_ordinary_failures_are_not_a_halt(self) -> None:
        finished = {
            await self._finished(IterationOutcome(iteration_index=0, succeeded=True, detail="")),
            await self._finished(IterationOutcome(iteration_index=1, succeeded=False, detail="the image was empty")),
        }

        assert NodeExecutor._budget_halt_among(finished) is None


class TestSequentialLoopStopsOnAHalt:
    @pytest.mark.asyncio
    async def test_no_iteration_runs_after_the_refused_one(self) -> None:
        run_subflow = AsyncMock(return_value=_failure(HALT))
        executor = _executor(run_subflow)

        with pytest.raises(RuntimeError, match=BUDGET_HALT_PREFIX):
            await executor._execute_loop_iterations_sequentially(
                package_result=_package_result(),
                total_iterations=3,
                parameter_values_per_iteration={0: {}, 1: {}, 2: {}},
                end_loop_node=MagicMock(),
            )

        run_subflow.assert_awaited_once()


class TestWhileLoopStopsOnAHalt:
    @pytest.mark.asyncio
    async def test_no_iteration_runs_after_the_refused_one(self) -> None:
        run_subflow = AsyncMock(return_value=_failure(HALT))
        executor = _executor(run_subflow)

        with pytest.raises(RuntimeError, match=BUDGET_HALT_PREFIX):
            await executor._run_while_loop_iterations(
                node=MagicMock(),
                flow_name="iteration_0",
                node_name_mappings={},
                packaged_start_node_name="Start_0",
                resolved_upstream_values={},
                iteration_startflow_params=[],
                reverse_node_mapping={},
                event_manager=MagicMock(),
                max_iterations=3,
                total_iterations=3,
            )

        run_subflow.assert_awaited_once()


class TestParallelLoopStopsOnAHalt:
    @pytest.mark.asyncio
    async def test_the_iterations_still_running_are_cancelled(self) -> None:
        """The refused iteration finishes first; the others would spend against the same budget."""
        cancelled: list[str] = []

        async def run_subflow(request: StartLocalSubflowRequest) -> StartLocalSubflowResultFailure:
            if request.flow_name == "iteration_0":
                return _failure(HALT)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(request.flow_name)
                raise
            return _failure("unreachable")

        executor = _executor(run_subflow)

        with pytest.raises(RuntimeError) as caught:
            await executor._execute_loop_iterations_locally(
                package_result=_package_result(),
                total_iterations=3,
                parameter_values_per_iteration={0: {}, 1: {}, 2: {}},
                end_loop_node=MagicMock(),
            )

        assert str(caught.value) == HALT
        assert sorted(cancelled) == ["iteration_1", "iteration_2"]

    @pytest.mark.asyncio
    async def test_ordinary_failures_still_wait_for_every_iteration(self) -> None:
        executor = _executor(AsyncMock(side_effect=[_success(), _failure("the image was empty")]))
        executor.get_parameter_values_from_iterations = MagicMock(return_value={0: "done"})  # type: ignore[method-assign]
        executor.get_last_iteration_values_for_packaged_nodes = MagicMock(return_value={})  # type: ignore[method-assign]

        iteration_results, successful_iterations, _ = await executor._execute_loop_iterations_locally(
            package_result=_package_result(),
            total_iterations=2,
            parameter_values_per_iteration={0: {}, 1: {}},
            end_loop_node=MagicMock(),
        )

        assert successful_iterations == [0]
        assert iteration_results == {0: "done", 1: None}
