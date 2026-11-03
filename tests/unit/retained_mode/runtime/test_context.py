"""Tests for WorkflowContextStack, FlowContextState and NodeContextState."""

from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest

from griptape_nodes.exe_types.flow import ControlFlow
from griptape_nodes.retained_mode.runtime.context import EmptyStackError, WorkflowContextStack


def _flow(name: str) -> Mock:
    flow = Mock(spec=ControlFlow)
    flow.name = name
    return flow


class TestWorkflowContextStack:
    """WorkflowContextStack flow stack and its task-local overlay."""

    @pytest.fixture
    def stack(self) -> WorkflowContextStack:
        stack = WorkflowContextStack()
        stack.push_flow(_flow("root"))
        return stack

    def test_current_flow_state_raises_when_empty(self) -> None:
        with pytest.raises(EmptyStackError):
            WorkflowContextStack().current_flow_state()

    def test_overlay_isolates_pushes_and_pops(self, stack: WorkflowContextStack) -> None:
        with stack.task_local_flow_stack():
            stack.push_flow(_flow("child"))
            assert stack.get_current_flow().name == "child"
            stack.pop_flow()
            stack.pop_flow()
            assert not stack.has_current_flow()
            with pytest.raises(EmptyStackError):
                stack.pop_flow()

        assert stack.get_current_flow().name == "root"
        assert len(stack._flow_stack) == 1

    def test_shared_exit_action_pops_the_shared_stack_even_inside_an_overlay(self, stack: WorkflowContextStack) -> None:
        exit_flow = stack.enter_flow(_flow("a"))

        with stack.task_local_flow_stack():
            stack.push_flow(_flow("overlay"))
            exit_flow()
            assert [state._flow.name for state in stack._flow_stack] == ["root"]
            assert stack.get_current_flow().name == "overlay"
            assert stack.pop_flow().name == "overlay"
            assert stack.get_current_flow().name == "a"

    def test_a_push_in_one_stacks_overlay_does_not_affect_another_stack(self, stack: WorkflowContextStack) -> None:
        other = WorkflowContextStack()

        with stack.task_local_flow_stack():
            stack.push_flow(_flow("child"))
            assert not other.has_current_flow()
            assert other._flow_stack == []

        assert not other.has_current_flow()

    @pytest.mark.asyncio
    async def test_a_sibling_task_keeps_using_the_shared_stack_while_another_holds_an_overlay(
        self, stack: WorkflowContextStack
    ) -> None:
        overlay_held = asyncio.Event()
        sibling_done = asyncio.Event()

        async def holder() -> None:
            with stack.task_local_flow_stack():
                overlay_held.set()
                await asyncio.wait_for(sibling_done.wait(), timeout=5)
                assert stack.get_current_flow().name == "root"

        async def sibling() -> None:
            await asyncio.wait_for(overlay_held.wait(), timeout=5)
            assert stack.get_current_flow().name == "root"
            stack.push_flow(_flow("sibling"))
            sibling_done.set()

        await asyncio.gather(holder(), sibling())

        assert [state._flow.name for state in stack._flow_stack] == ["root", "sibling"]
