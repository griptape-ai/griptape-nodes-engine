"""The open workflows: the single-open rule, and opening and closing with the context stack."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from griptape_nodes.retained_mode.runtime.open_workflow_set import OpenWorkflowError, OpenWorkflowSet

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine


class TestOpenWorkflowSet:
    def test_opening_a_second_workflow_is_refused(self) -> None:
        workflows = OpenWorkflowSet()
        first = workflows.open("abc")

        with pytest.raises(OpenWorkflowError, match="already open"):
            workflows.open("abc")
        with pytest.raises(OpenWorkflowError, match="'abc' is already open"):
            workflows.open("def")

        assert workflows.all_open() == [first]

    def test_the_test_seam_lets_a_second_workflow_open_beside_the_first(self) -> None:
        workflows = OpenWorkflowSet(allow_several_open=True)
        first = workflows.open("abc")
        second = workflows.open("def")

        with pytest.raises(OpenWorkflowError, match="already open"):
            workflows.open("abc")

        assert workflows.all_open() == [first, second]

    def test_get_and_is_open_find_only_open_workflows(self) -> None:
        workflows = OpenWorkflowSet()
        workflow = workflows.open("abc")

        assert workflows.is_open("abc")
        assert workflows.get("abc") is workflow
        assert workflows.find("abc") is workflow
        assert not workflows.is_open("def")
        assert workflows.find("def") is None
        with pytest.raises(OpenWorkflowError, match="not open"):
            workflows.get("def")

    def test_close_marks_the_runtime_closed(self) -> None:
        workflows = OpenWorkflowSet()
        workflow = workflows.open("abc")
        assert not workflow.closed

        workflows.close(workflow)

        assert workflow.closed
        reopened = workflows.open("abc")
        assert not reopened.closed

    def test_close_refuses_a_runtime_that_is_not_open(self) -> None:
        workflows = OpenWorkflowSet()
        stale = workflows.open("abc")
        workflows.close(stale)
        reopened = workflows.open("abc")

        with pytest.raises(OpenWorkflowError, match="not open"):
            workflows.close(stale)

        assert workflows.all_open() == [reopened]

    def test_move_keeps_the_runtime_under_its_new_id_only(self) -> None:
        workflows = OpenWorkflowSet()
        workflow = workflows.open("abc")

        workflows.move(workflow, "def")

        assert workflow.workflow_id == "def"
        assert workflows.get("def") is workflow
        assert not workflows.is_open("abc")

    def test_move_refuses_an_id_already_open(self) -> None:
        workflows = OpenWorkflowSet()
        workflow = workflows.open("abc")

        with pytest.raises(OpenWorkflowError, match="'abc' is already open"):
            workflows.move(workflow, "abc")

        assert workflows.get("abc") is workflow

    def test_move_refuses_a_runtime_that_is_not_open(self) -> None:
        workflows = OpenWorkflowSet()
        stale = workflows.open("abc")
        workflows.close(stale)

        with pytest.raises(OpenWorkflowError, match="not open"):
            workflows.move(stale, "def")

        assert workflows.all_open() == []


class TestOpenWorkflowsFollowTheContextStack:
    """A workflow is open exactly while the context stack holds it at the bottom."""

    def test_a_push_onto_an_empty_stack_opens_the_pushed_workflow(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="outer")

        [workflow] = engine.open_workflows.all_open()
        assert workflow.workflow_id == engine.context_manager.get_current_workflow_id()

    def test_a_nested_push_and_pop_keep_the_outer_workflow_open(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="outer")
        [outer] = engine.open_workflows.all_open()

        engine.context_manager.push_workflow(workflow_name="inner")
        assert engine.open_workflows.all_open() == [outer]

        engine.context_manager.pop_workflow()
        assert engine.open_workflows.all_open() == [outer]

    def test_popping_the_last_entry_closes_the_workflow(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="outer")
        engine.context_manager.push_workflow(workflow_name="inner")

        engine.context_manager.pop_workflow()
        engine.context_manager.pop_workflow()

        assert engine.open_workflows.all_open() == []

    def test_adopting_an_orchestrators_workflow_opens_nothing(self, engine: Engine) -> None:
        peer_id = "00000000000000000000000000000abc"

        workflow = engine.context_manager.adopt_orchestrator_workflow(peer_id, None, None, None)

        assert engine.worker_workflows.find(peer_id) is workflow
        assert engine.open_workflows.all_open() == []

    def test_moving_the_open_workflow_moves_its_runtime(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="outer")
        [workflow] = engine.open_workflows.all_open()
        copy = engine.workflow_registry.register_unsaved("copy")

        engine.context_manager.move_current_workflow_onto(copy.workflow_id)

        assert engine.open_workflows.all_open() == [workflow]
        assert workflow.workflow_id == copy.workflow_id

    def test_moving_a_nested_workflow_leaves_the_open_one_alone(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="outer")
        [outer] = engine.open_workflows.all_open()
        outer_id = outer.workflow_id
        engine.context_manager.push_workflow(workflow_name="inner")
        copy = engine.workflow_registry.register_unsaved("copy")

        engine.context_manager.move_current_workflow_onto(copy.workflow_id)

        assert engine.open_workflows.all_open() == [outer]
        assert outer.workflow_id == outer_id
