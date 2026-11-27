"""A parallel loop keeps what failed inside each iteration that did not finish."""

from __future__ import annotations

from contextlib import nullcontext
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

import pytest

from griptape_nodes.common import failure_trees, node_executor
from griptape_nodes.common.failure_trees import IterationFailure
from griptape_nodes.common.node_executor import NodeExecutor
from griptape_nodes.exe_types.base_iterative_nodes import BaseIterativeEndNode
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.execution_events import StartLocalSubflowResultFailure
from griptape_nodes.retained_mode.events.failure_tree import FailureKind, FailureLeaf
from griptape_nodes.retained_mode.events.flow_events import (
    DeserializeFlowFromCommandsResultSuccess,
    PackageNodesAsSerializedFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.node_error_details import NodeErrorDetails
from griptape_nodes.retained_mode.managers.flow_manager import FlowManager
from griptape_nodes.retained_mode.runtime.nodes import WorkflowNodes
from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.events.failure_tree import FailureTree


class TestExecuteLoopIterationsLocally:
    @pytest.fixture
    def executor(self, monkeypatch: pytest.MonkeyPatch) -> NodeExecutor:
        engine = Mock(spec=Engine)
        engine.event_manager = Mock()
        engine.config_manager = Mock()
        engine.config_manager.get_config_value.return_value = 0
        engine.flow_manager = Mock(spec=FlowManager)
        executor = NodeExecutor(engine=engine)
        start_or_end_mapping = Mock()
        start_or_end_mapping.node_name = "Start"
        start_or_end_mapping.parameter_mappings = {}
        monkeypatch.setattr(executor, "get_node_parameter_mappings", Mock(return_value=start_or_end_mapping))
        monkeypatch.setattr(executor, "_silence_packaged_node_creation_broadcasts", Mock())
        monkeypatch.setattr(executor, "_delete_iteration_flows", AsyncMock())
        monkeypatch.setattr(executor, "get_parameter_values_from_iterations", Mock(return_value={}))
        monkeypatch.setattr(executor, "get_last_iteration_values_for_packaged_nodes", Mock(return_value={}))
        monkeypatch.setattr(node_executor, "EventSuppressionContext", lambda *_args: nullcontext())
        return executor

    @pytest.fixture
    def inner_failure(self) -> FailureLeaf:
        return FailureLeaf(
            node_name="Body_1", kind=FailureKind.RAISED, details=NodeErrorDetails(message="boom", messages=["boom"])
        )

    @pytest.fixture
    def workflow(self, inner_failure: FailureTree) -> Mock:
        workflow = Mock(spec=WorkflowRuntime)
        workflow.nodes = Mock(spec=WorkflowNodes)
        workflow.nodes.parent_flow_name.return_value = "Holding_flow"
        workflow.context = Mock()
        workflow.context.has_current_flow.return_value = False
        workflow.handle_request.return_value = DeserializeFlowFromCommandsResultSuccess(
            flow_name="iteration_flow", node_name_mappings={"Body": "Body_1"}, result_details="ok"
        )
        workflow.ahandle_request = AsyncMock(
            return_value=StartLocalSubflowResultFailure(result_details="boom").attach_failure_tree(inner_failure)
        )
        return workflow

    @pytest.fixture
    def end_loop_node(self, workflow: Mock) -> Mock:
        end_loop_node = Mock(spec=BaseIterativeEndNode)
        end_loop_node.name = "Loop End"
        end_loop_node._require_workflow.return_value = workflow
        return end_loop_node

    @pytest.fixture
    def reported_failures(self, monkeypatch: pytest.MonkeyPatch) -> Mock:
        format_lines = Mock(spec=failure_trees.format_iteration_failure_lines, return_value=[])
        monkeypatch.setattr(failure_trees, "format_iteration_failure_lines", format_lines)
        return format_lines

    @pytest.mark.asyncio
    async def test_a_failed_iteration_carries_what_failed_inside_it_named_for_the_canvas(
        self, executor: NodeExecutor, end_loop_node: Mock, reported_failures: Mock
    ) -> None:
        package_result: Any = Mock(spec=PackageNodesAsSerializedFlowResultSuccess)
        package_result.serialized_flow_commands = Mock()

        await executor._execute_loop_iterations_locally(
            package_result,
            total_iterations=1,
            parameter_values_per_iteration={0: {}},
            end_loop_node=end_loop_node,
        )

        canvas_named_failure = FailureLeaf(
            node_name="Body", kind=FailureKind.RAISED, details=NodeErrorDetails(message="boom", messages=["boom"])
        )
        reported_failures.assert_called_once_with(
            [IterationFailure(iteration_index=0, detail="boom", failure_tree=canvas_named_failure)],
            total_iterations=1,
        )
