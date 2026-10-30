"""The while-group path names its body copies by the canvas nodes they stand for.

The one-at-a-time and all-at-once loop paths are covered end to end in
tests/e2e/test_node_process_events.py. While groups have no end-to-end fixture, so this checks that
handle_while_group_execution builds the loop body scope and that each iteration's subflow runs
inside it.
"""

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.common.loop_body_scope import (
    LoopBodyScope,
    current_loop_body_scope,
    entered_loop_body_scope,
    loop_body_scope_for,
)
from griptape_nodes.common.node_executor import NodeExecutor
from griptape_nodes.exe_types.node_groups import BaseWhileNodeGroup
from griptape_nodes.exe_types.node_types import LOCAL_EXECUTION
from griptape_nodes.retained_mode.events.execution_events import (
    StartLocalSubflowRequest,
    StartLocalSubflowResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import PackagedNodeParameterMapping


def _make_package_result() -> MagicMock:
    package_result = MagicMock()
    package_result.parameter_name_mappings = [
        PackagedNodeParameterMapping(node_name="Start_Package_MultiNode", parameter_mappings={}),
        PackagedNodeParameterMapping(node_name="End_Multi_Package", parameter_mappings={}),
    ]
    package_result.packaged_node_names = ["Body"]
    return package_result


def _make_while_node() -> MagicMock:
    node = MagicMock(spec=BaseWhileNodeGroup)
    node.name = "RetryGroup"
    node._get_max_iterations.return_value = 2
    node._current_iteration = 0
    node.execution_environment = MagicMock()
    node.execution_environment.name = "execution_environment"
    node.get_parameter_value = MagicMock(return_value=LOCAL_EXECUTION)
    return node


def test_a_nested_loop_body_scope_resolves_through_the_enclosing_iteration() -> None:
    """A loop run inside another loop's iteration names its copies by the outer loop's canvas nodes."""
    executor = NodeExecutor(engine=MagicMock())
    outer = loop_body_scope_for({"Body": "Body_1"}, engine_made_node_names=(), enclosing_scope=None)

    with entered_loop_body_scope(outer):
        inner = executor._loop_body_scope(
            _make_package_result(), {"Body_1": "Body_2", "Start_Package_MultiNode": "S_2"}
        )

    assert inner.canvas_name_for("Body_2") == "Body"
    assert inner.is_engine_node("S_2")


@pytest.mark.asyncio
async def test_while_group_builds_the_loop_body_scope() -> None:
    """The scope maps each copy back to its canvas node and marks the engine's own nodes."""
    executor = NodeExecutor(engine=MagicMock())
    node_name_mappings = {"Start_Package_MultiNode": "S_1", "End_Multi_Package": "E_1", "Body": "Body_1"}
    run_iterations = AsyncMock(return_value=True)

    with (
        patch.object(
            NodeExecutor, "_package_subflow_group_body", new_callable=AsyncMock, return_value=_make_package_result()
        ),
        patch.object(NodeExecutor, "get_resolved_upstream_values", return_value={}),
        patch.object(NodeExecutor, "_get_while_iteration_param_mappings", return_value=[]),
        patch.object(NodeExecutor, "_deserialize_while_flow", return_value=("BodyFlow_1", node_name_mappings, "S_1")),
        patch.object(NodeExecutor, "_run_while_loop_iterations", run_iterations),
        patch.object(NodeExecutor, "get_last_iteration_values_for_packaged_nodes", return_value={}),
        patch.object(NodeExecutor, "_apply_last_iteration_to_packaged_nodes"),
    ):
        await executor.handle_while_group_execution(cast("BaseWhileNodeGroup", _make_while_node()))

    assert run_iterations.await_args is not None
    scope = run_iterations.await_args.kwargs["loop_body_scope"]
    assert scope.canvas_name_for("Body_1") == "Body"
    assert scope.is_engine_node("S_1")
    assert scope.is_engine_node("E_1")


@pytest.mark.asyncio
async def test_while_iteration_subflow_runs_inside_the_loop_body_scope() -> None:
    """Each iteration starts its subflow inside the scope, and leaves it afterward."""
    engine = MagicMock()
    executor = NodeExecutor(engine=engine)
    scope = loop_body_scope_for({"Body": "Body_1"}, engine_made_node_names=(), enclosing_scope=None)
    seen_scopes: list[LoopBodyScope | None] = []

    async def handle_request(request: Any) -> StartLocalSubflowResultSuccess:
        assert isinstance(request, StartLocalSubflowRequest)
        seen_scopes.append(current_loop_body_scope.get())
        return StartLocalSubflowResultSuccess(result_details="ran")

    engine.ahandle_request = AsyncMock(side_effect=handle_request)

    with (
        patch.object(NodeExecutor, "_set_while_iteration_parameters", new_callable=AsyncMock),
        patch.object(NodeExecutor, "_evaluate_while_iteration_result", return_value=True),
    ):
        await executor._run_while_loop_iterations(
            node=cast("BaseWhileNodeGroup", _make_while_node()),
            flow_name="BodyFlow_1",
            node_name_mappings={"Body": "Body_1"},
            packaged_start_node_name="S_1",
            resolved_upstream_values={},
            iteration_startflow_params=[],
            reverse_node_mapping={"Body_1": "Body"},
            loop_body_scope=scope,
            event_manager=engine.event_manager,
            max_iterations=2,
            total_iterations=3,
        )

    assert seen_scopes == [scope]
    assert current_loop_body_scope.get() is None
