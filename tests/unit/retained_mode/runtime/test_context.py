"""Tests for WorkflowContextStack."""

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


class TestCurrentFlow:
    def test_outside_every_block_it_is_the_top_level_flow(self) -> None:
        top = _flow("top")

        assert WorkflowContextStack(lambda: top).get_current_flow() is top

    def test_with_no_top_level_flow_there_is_none(self) -> None:
        context = WorkflowContextStack(lambda: None)

        assert not context.has_current_flow()
        with pytest.raises(EmptyStackError, match="no top-level flow"):
            context.get_current_flow()

    def test_a_flow_block_overrides_the_top_level_flow_until_it_ends(self) -> None:
        top = _flow("top")
        inner = _flow("inner")
        context = WorkflowContextStack(lambda: top)

        with context.flow(inner):
            assert context.get_current_flow() is inner
        assert context.get_current_flow() is top


class TestCurrentNode:
    def test_outside_a_node_block_there_is_none(self) -> None:
        context = WorkflowContextStack(lambda: _flow("top"))

        assert not context.has_current_node()
        with pytest.raises(EmptyStackError, match="No node is current"):
            context.get_current_node()

    def test_a_node_block_sits_in_the_current_flow(self) -> None:
        top = _flow("top")
        node = _node("n")
        context = WorkflowContextStack(lambda: top)

        with context.node(node):
            assert context.get_current_node() is node
            assert context.get_current_flow() is top
        assert not context.has_current_node()

    def test_a_flow_block_inside_a_node_block_has_no_node(self) -> None:
        context = WorkflowContextStack(lambda: _flow("top"))
        node = _node("n")

        with context.node(node):
            with context.flow(_flow("inner")):
                assert not context.has_current_node()
            assert context.get_current_node() is node

    def test_a_node_block_needs_a_current_flow(self) -> None:
        context = WorkflowContextStack(lambda: None)

        with pytest.raises(EmptyStackError), context.node(_node("n")):
            pytest.fail("should not enter")


class TestBlocksBelongToTheirCaller:
    def test_two_workflows_keep_separate_blocks(self) -> None:
        first = WorkflowContextStack(lambda: _flow("first_top"))
        second_top = _flow("second_top")
        second = WorkflowContextStack(lambda: second_top)

        with first.flow(_flow("inner")):
            assert second.get_current_flow() is second_top

    @pytest.mark.asyncio
    async def test_a_sibling_task_does_not_see_another_tasks_block(self) -> None:
        top = _flow("top")
        context = WorkflowContextStack(lambda: top)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def hold() -> None:
            with context.flow(_flow("held")):
                entered.set()
                await release.wait()

        holder = asyncio.create_task(hold())
        await entered.wait()
        seen = context.get_current_flow()
        release.set()
        await holder

        assert seen is top
