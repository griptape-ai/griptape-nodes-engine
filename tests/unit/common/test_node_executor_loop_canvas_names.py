"""Loop iterations name their body copies after the canvas nodes while the subflow runs.

The node_run_timing summary reads `loop_copy_canvas_names` to record `Upscale` rather than the
copy an iteration runs, such as `Upscale_1`. These tests check that each local loop path, the
sequential, while, and parallel ones, sets it for the duration of the iteration's subflow.
"""

from collections.abc import Mapping
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.common.node_executor import IterationControlAction, NodeExecutor, loop_copy_canvas_names
from griptape_nodes.retained_mode.events.execution_events import (
    StartLocalSubflowRequest,
    StartLocalSubflowResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import DeserializeFlowFromCommandsResultSuccess


def _make_executor(subflow_canvas_names: dict[str, Mapping[str, str] | None]) -> NodeExecutor:
    """An executor whose engine records the canvas names in effect when each subflow starts."""
    executor = NodeExecutor(engine=MagicMock())
    engine = cast("MagicMock", executor.engine)
    engine.context_manager.has_current_flow.return_value = False

    async def handle_request(request: Any) -> Any:
        if isinstance(request, StartLocalSubflowRequest):
            subflow_canvas_names[request.flow_name] = loop_copy_canvas_names.get()
        return StartLocalSubflowResultSuccess(result_details="ok")

    engine.ahandle_request = AsyncMock(side_effect=handle_request)
    return executor


def _make_package_result() -> MagicMock:
    package_result = MagicMock()
    start_mapping = MagicMock()
    start_mapping.node_name = "Start"
    start_mapping.parameter_mappings = {}
    end_mapping = MagicMock()
    end_mapping.node_name = "End"
    end_mapping.parameter_mappings = {}
    package_result.parameter_name_mappings = [start_mapping, end_mapping]
    return package_result


def _stub_iteration_bookkeeping(executor: NodeExecutor) -> list[Any]:
    """Patch out the work around each iteration that these tests do not exercise."""
    return [
        patch.object(executor, "_silence_packaged_node_creation_broadcasts"),
        patch.object(executor, "_delete_iteration_flows", new=AsyncMock()),
        patch.object(executor, "_get_iteration_control_action", return_value=IterationControlAction.ADD),
        patch.object(executor, "get_parameter_values_from_iterations", return_value={}),
        patch.object(executor, "get_last_iteration_values_for_packaged_nodes", return_value={}),
    ]


class TestLoopIterationsSetCanvasNames:
    @pytest.mark.asyncio
    async def test_sequential_iterations(self) -> None:
        subflow_canvas_names: dict[str, Mapping[str, str] | None] = {}
        executor = _make_executor(subflow_canvas_names)
        cast("MagicMock", executor.engine).handle_request.return_value = DeserializeFlowFromCommandsResultSuccess(
            result_details="ok",
            flow_name="IterationFlow",
            node_name_mappings={"Start": "Start_1", "Upscale": "Upscale_1"},
        )
        stubs = _stub_iteration_bookkeeping(executor)
        for stub in stubs:
            stub.start()
        try:
            await executor._execute_loop_iterations_sequentially(
                package_result=_make_package_result(),
                total_iterations=1,
                parameter_values_per_iteration={0: {}},
                end_loop_node=MagicMock(),
            )
        finally:
            for stub in stubs:
                stub.stop()

        assert subflow_canvas_names == {"IterationFlow": {"Start_1": "Start", "Upscale_1": "Upscale"}}
        assert loop_copy_canvas_names.get() is None

    @pytest.mark.asyncio
    async def test_parallel_iterations_each_name_their_own_copies(self) -> None:
        subflow_canvas_names: dict[str, Mapping[str, str] | None] = {}
        executor = _make_executor(subflow_canvas_names)
        cast("MagicMock", executor.engine).handle_request.side_effect = [
            DeserializeFlowFromCommandsResultSuccess(
                result_details="ok",
                flow_name=f"IterationFlow{i}",
                node_name_mappings={"Start": f"Start_{i}", "Upscale": f"Upscale_{i}"},
            )
            for i in (1, 2)
        ]
        stubs = _stub_iteration_bookkeeping(executor)
        for stub in stubs:
            stub.start()
        try:
            await executor._execute_loop_iterations_locally(
                package_result=_make_package_result(),
                total_iterations=2,
                parameter_values_per_iteration={0: {}, 1: {}},
                end_loop_node=MagicMock(),
            )
        finally:
            for stub in stubs:
                stub.stop()

        assert subflow_canvas_names == {
            "IterationFlow1": {"Start_1": "Start", "Upscale_1": "Upscale"},
            "IterationFlow2": {"Start_2": "Start", "Upscale_2": "Upscale"},
        }

    @pytest.mark.asyncio
    async def test_while_iterations(self) -> None:
        subflow_canvas_names: dict[str, Mapping[str, str] | None] = {}
        executor = _make_executor(subflow_canvas_names)
        reverse_node_mapping = {"Start_1": "Start", "Retry_1": "Retry"}

        with (
            patch.object(executor, "_set_while_iteration_parameters", new=AsyncMock()),
            patch.object(executor, "_evaluate_while_iteration_result", return_value=True),
        ):
            done = await executor._run_while_loop_iterations(
                node=MagicMock(),
                flow_name="IterationFlow",
                node_name_mappings={"Start": "Start_1", "Retry": "Retry_1"},
                packaged_start_node_name="Start_1",
                resolved_upstream_values={},
                iteration_startflow_params=[],
                reverse_node_mapping=reverse_node_mapping,
                event_manager=MagicMock(),
                max_iterations=1,
                total_iterations=1,
            )

        assert done is True
        assert subflow_canvas_names == {"IterationFlow": reverse_node_mapping}
        assert loop_copy_canvas_names.get() is None
