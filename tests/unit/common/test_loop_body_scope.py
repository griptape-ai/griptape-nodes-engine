"""Tests for the scope that names a loop body's copies by the canvas nodes they stand for."""

import asyncio

import pytest

from griptape_nodes.common.loop_body_scope import (
    current_loop_body_scope,
    entered_loop_body_scope,
    loop_body_scope_for,
)

ENGINE_MADE = {"Start_Package_MultiNode", "End_Multi_Package"}


class TestLoopBodyScopeFor:
    def test_copies_of_a_canvas_loop_stand_for_the_packaged_names(self) -> None:
        scope = loop_body_scope_for(
            {"Body": "Body_1", "Other": "Other_1"},
            engine_made_node_names=ENGINE_MADE,
            enclosing_scope=None,
        )

        assert scope.canvas_name_for("Body_1") == "Body"
        assert scope.canvas_name_for("Other_1") == "Other"
        assert scope.canvas_name_for("Unrelated") is None

    def test_engine_made_copies_stand_for_no_canvas_node(self) -> None:
        scope = loop_body_scope_for(
            {"Start_Package_MultiNode": "S_1", "End_Multi_Package": "E_1", "Body": "Body_1"},
            engine_made_node_names=ENGINE_MADE,
            enclosing_scope=None,
        )

        assert scope.is_engine_node("S_1")
        assert scope.is_engine_node("E_1")
        assert not scope.is_engine_node("Body_1")
        assert scope.canvas_name_for("S_1") is None

    def test_nested_loop_copies_resolve_through_the_enclosing_scope(self) -> None:
        outer = loop_body_scope_for(
            {"Body": "Body_1", "InnerGroup": "InnerGroup_1"},
            engine_made_node_names=ENGINE_MADE,
            enclosing_scope=None,
        )

        # The inner loop packages the outer iteration's copies, so those are its packaged names.
        inner = loop_body_scope_for(
            {"Body_1": "Body_2", "Start_Package_MultiNode": "S_2"},
            engine_made_node_names=ENGINE_MADE,
            enclosing_scope=outer,
        )

        assert inner.canvas_name_for("Body_2") == "Body"
        assert inner.is_engine_node("S_2")

    def test_nested_copy_with_no_canvas_node_is_left_out(self) -> None:
        outer = loop_body_scope_for({"Body": "Body_1"}, engine_made_node_names=ENGINE_MADE, enclosing_scope=None)

        inner = loop_body_scope_for(
            {"NotInOuter": "NotInOuter_1"}, engine_made_node_names=ENGINE_MADE, enclosing_scope=outer
        )

        assert inner.canvas_name_for("NotInOuter_1") is None


class TestEnteredLoopBodyScope:
    def test_scope_is_set_inside_and_reset_after(self) -> None:
        scope = loop_body_scope_for({"Body": "Body_1"}, engine_made_node_names=ENGINE_MADE, enclosing_scope=None)

        with entered_loop_body_scope(scope):
            assert current_loop_body_scope.get() is scope

        assert current_loop_body_scope.get() is None

    def test_scope_is_reset_when_the_block_raises(self) -> None:
        scope = loop_body_scope_for({"Body": "Body_1"}, engine_made_node_names=ENGINE_MADE, enclosing_scope=None)

        def fail_inside_the_scope() -> None:
            with entered_loop_body_scope(scope):
                msg = "iteration failed"
                raise RuntimeError(msg)

        with pytest.raises(RuntimeError):
            fail_inside_the_scope()

        assert current_loop_body_scope.get() is None

    @pytest.mark.asyncio
    async def test_tasks_started_inside_see_the_scope_and_their_own_does_not_leak_out(self) -> None:
        outer = loop_body_scope_for({"Body": "Body_1"}, engine_made_node_names=ENGINE_MADE, enclosing_scope=None)
        inner = loop_body_scope_for({"Other": "Other_1"}, engine_made_node_names=ENGINE_MADE, enclosing_scope=None)
        seen_in_task = []

        async def iteration() -> None:
            seen_in_task.append(current_loop_body_scope.get())
            with entered_loop_body_scope(inner):
                await asyncio.sleep(0)

        with entered_loop_body_scope(outer):
            await asyncio.ensure_future(iteration())
            assert current_loop_body_scope.get() is outer

        assert seen_in_task == [outer]
