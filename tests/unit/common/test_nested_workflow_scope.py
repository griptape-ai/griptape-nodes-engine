"""Tests for running the workflow behind a Workflow node without the enclosing loop's naming context."""

import asyncio

import pytest

from griptape_nodes.common.loop_body_scope import (
    LoopBodyScope,
    current_loop_body_scope,
    entered_loop_body_scope,
)
from griptape_nodes.common.nested_workflow_scope import entered_nested_workflow


class TestEnteredNestedWorkflow:
    @pytest.fixture
    def enclosing_scope(self) -> LoopBodyScope:
        return LoopBodyScope(canvas_node_names={"Body_1": "Body"}, engine_node_names=frozenset())

    def test_clears_the_scope_inside_and_restores_it_after(self, enclosing_scope: LoopBodyScope) -> None:
        with entered_loop_body_scope(enclosing_scope):
            with entered_nested_workflow():
                assert current_loop_body_scope.get() is None

            assert current_loop_body_scope.get() is enclosing_scope

    def test_restores_the_scope_when_the_block_raises(self, enclosing_scope: LoopBodyScope) -> None:
        def fail_inside_the_nested_workflow() -> None:
            with entered_nested_workflow():
                msg = "workflow failed"
                raise RuntimeError(msg)

        with entered_loop_body_scope(enclosing_scope):
            with pytest.raises(RuntimeError):
                fail_inside_the_nested_workflow()

            assert current_loop_body_scope.get() is enclosing_scope

    def test_leaves_no_scope_behind_when_none_was_set(self) -> None:
        with entered_nested_workflow():
            assert current_loop_body_scope.get() is None

        assert current_loop_body_scope.get() is None

    @pytest.mark.asyncio
    async def test_tasks_started_inside_inherit_no_scope(self, enclosing_scope: LoopBodyScope) -> None:
        seen_in_task: list[LoopBodyScope | None] = []

        async def node_run() -> None:
            seen_in_task.append(current_loop_body_scope.get())

        with entered_loop_body_scope(enclosing_scope), entered_nested_workflow():
            await asyncio.ensure_future(node_run())

        assert seen_in_task == [None]
