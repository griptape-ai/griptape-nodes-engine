"""Golden tests that pin the failure a parallel run records for a node it cannot dispatch.

Most failures reach the run as an exception from a node's task, and the end-to-end goldens cover them. This
one is recorded before any task exists, from a node that no flow can reach through validation, so it is
driven here against the scheduler directly.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from griptape_nodes.exe_types.base_iterative_nodes import BaseIterativeStartNode
from griptape_nodes.exe_types.node_types import BaseNode, TrackedParameterOutputValues
from griptape_nodes.machines.dag_builder import NodeState
from griptape_nodes.machines.fsm import WorkflowState
from griptape_nodes.machines.parallel_resolution import ErrorState, ExecuteDagState
from griptape_nodes.retained_mode.events.execution_events import NodeErrorEvent
from tests.unit.machines.scheduler_stubs import machine_running_one_node

if TYPE_CHECKING:
    from collections.abc import Callable


async def _until(condition: Callable[[], object], message: str) -> None:
    for _ in range(1000):
        if condition():
            return
        await asyncio.sleep(0.005)
    pytest.fail(message)


class TestStartLoopNodeWithoutAnEndNode:
    """Golden case 15: a Start Loop node reaches the scheduler with no End Loop node tethered to it.

    ``validate_before_node_run`` normally refuses such a node first, so the stub passes it. The run records a
    message that names the node but is not the failure of any node's task, so no ``NodeErrorEvent`` is sent.
    """

    @pytest.fixture
    def start_loop_node(self) -> BaseIterativeStartNode:
        """A Start Loop node whose validation passes and which has no End Loop node."""
        node = MagicMock(spec=BaseIterativeStartNode)
        node.name = "Loop Start"
        node.lock = False
        node.parameters = []
        node.parameter_output_values = MagicMock(spec=TrackedParameterOutputValues)
        node.validate_before_node_run = Mock(spec=BaseNode.validate_before_node_run, return_value=None)
        node.end_node = None
        return cast("BaseIterativeStartNode", node)

    @pytest.mark.asyncio
    async def test_the_run_records_a_message_with_no_node_error(
        self,
        held_node_execution: asyncio.Event,
        monkeypatch: pytest.MonkeyPatch,
        start_loop_node: BaseIterativeStartNode,
    ) -> None:
        monkeypatch.setattr(
            ExecuteDagState, "publish_resolved_node", AsyncMock(spec=ExecuteDagState.publish_resolved_node)
        )
        machine, release, dag_builder = machine_running_one_node(max_nodes_in_parallel=2)
        driver = asyncio.create_task(machine.resolve_node())
        machine.inject_node(start_loop_node)
        await _until(lambda: machine.current_state is ErrorState, "the node without an end node never stopped the run")

        assert machine.get_error_message() == "Cannot have a Start Loop Node without an End Loop Node: Loop Start"
        assert machine.get_error_failure_tree() is None
        assert machine.context.workflow_state == WorkflowState.ERRORED
        assert dag_builder.node_to_reference["Loop Start"].node_state is NodeState.DONE
        put_event = cast("AsyncMock", machine.context.engine.event_manager.aput_event)
        payloads = [call.args[0].wrapped_event.payload for call in put_event.call_args_list]
        assert [payload for payload in payloads if isinstance(payload, NodeErrorEvent)] == []

        release.set()
        held_node_execution.set()
        await asyncio.wait_for(driver, timeout=1)
