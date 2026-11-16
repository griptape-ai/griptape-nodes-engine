"""Tests for WorkflowContextStack, FlowContextState and NodeContextState."""

from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest

from griptape_nodes.exe_types.flow import ControlFlow
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.retained_mode.runtime.context import EmptyStackError, WorkflowContextStack


def _flow(name: str) -> Mock:
    flow = Mock(spec=ControlFlow)
    flow.name = name
    return flow


def _node(name: str) -> Mock:
    node = Mock(spec=BaseNode)
    node.name = name
    return node


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

    def test_pop_flow_raises_when_the_shared_stack_is_empty(self) -> None:
        with pytest.raises(EmptyStackError, match="Cannot pop Flow: no active Flows in Workflow"):
            WorkflowContextStack().pop_flow()

    def test_shared_exit_action_raises_when_the_shared_stack_was_already_emptied(self) -> None:
        stack = WorkflowContextStack()
        exit_flow = stack.enter_flow(_flow("a"))
        stack.pop_flow()

        with pytest.raises(EmptyStackError, match="Cannot pop Flow: no active Flows in Workflow"):
            exit_flow()

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


class TestAccessorsInsideAnOverlay:
    """Flow and node accessors act on the task-local stack, never the shared one, inside an overlay."""

    @pytest.fixture
    def stack(self) -> WorkflowContextStack:
        stack = WorkflowContextStack()
        stack.push_flow(_flow("root"))
        return stack

    def test_flow_context_manager_enters_and_leaves_on_the_overlay(self, stack: WorkflowContextStack) -> None:
        with stack.task_local_flow_stack():
            with stack.flow(_flow("child")) as child:
                assert stack.get_current_flow() is child
                assert [state._flow.name for state in stack._flow_stack] == ["root"]
            assert stack.get_current_flow().name == "root"

        assert [state._flow.name for state in stack._flow_stack] == ["root"]

    def test_flow_context_manager_on_the_shared_stack_pops_the_shared_stack(self, stack: WorkflowContextStack) -> None:
        with stack.flow(_flow("child")):
            assert [state._flow.name for state in stack._flow_stack] == ["root", "child"]

        assert [state._flow.name for state in stack._flow_stack] == ["root"]

    def test_push_and_pop_node_affect_the_overlay_only(self, stack: WorkflowContextStack) -> None:
        node = _node("N")

        with stack.task_local_flow_stack():
            stack.push_node(node)
            assert stack.has_current_node()
            assert stack.get_current_node() is node
            assert not stack._flow_stack[-1].has_current_node()
            assert stack.pop_node() is node
            assert not stack.has_current_node()

    def test_node_context_manager_enters_and_leaves_on_the_overlay(self, stack: WorkflowContextStack) -> None:
        node = _node("N")

        with stack.task_local_flow_stack():
            with stack.node(node):
                assert stack.get_current_node() is node
                assert not stack._flow_stack[-1].has_current_node()
            assert not stack.has_current_node()

    def test_node_context_manager_leaves_the_flow_it_entered_on_the_overlay(self, stack: WorkflowContextStack) -> None:
        node = _node("N")

        with stack.task_local_flow_stack():
            entered_state = stack.current_flow_state()
            with stack.node(node):
                stack.push_flow(_flow("other"))
            assert not entered_state.has_current_node()
            assert stack.get_current_flow().name == "other"

    def test_has_and_get_current_node_see_the_snapshot_not_later_shared_pushes(
        self, stack: WorkflowContextStack
    ) -> None:
        with stack.task_local_flow_stack():
            stack._flow_stack[-1].push_node(_node("SharedLater"))
            assert not stack.has_current_node()
            with pytest.raises(EmptyStackError):
                stack.get_current_node()

    def test_node_accessors_raise_without_a_flow_in_the_overlay(self, stack: WorkflowContextStack) -> None:
        with stack.task_local_flow_stack():
            stack.pop_flow()
            assert not stack.has_current_node()
            with pytest.raises(EmptyStackError):
                stack.push_node(_node("N"))
            with pytest.raises(EmptyStackError):
                stack.pop_node()
            with pytest.raises(EmptyStackError):
                stack.get_current_node()
