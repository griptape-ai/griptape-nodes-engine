"""The open workflows: the single-open rule, and opening and closing with the Workflow context."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from griptape_nodes.retained_mode.runtime.open_workflow_set import OpenWorkflowError, OpenWorkflowSet

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine


class TestOpenWorkflowSet:
    def test_opening_a_second_workflow_is_refused(self) -> None:
        workflows = OpenWorkflowSet(MagicMock())
        first = workflows.open("abc")

        with pytest.raises(OpenWorkflowError, match="already open"):
            workflows.open("abc")
        with pytest.raises(OpenWorkflowError, match="'abc' is already open"):
            workflows.open("def")

        assert workflows.all_open() == [first]

    def test_the_test_seam_lets_a_second_workflow_open_beside_the_first(self) -> None:
        workflows = OpenWorkflowSet(MagicMock(), allow_several_open=True)
        first = workflows.open("abc")
        second = workflows.open("def")

        with pytest.raises(OpenWorkflowError, match="already open"):
            workflows.open("abc")

        assert workflows.all_open() == [first, second]

    def test_get_and_is_open_find_only_open_workflows(self) -> None:
        workflows = OpenWorkflowSet(MagicMock())
        workflow = workflows.open("abc")

        assert workflows.is_open("abc")
        assert workflows.get("abc") is workflow
        assert workflows.find("abc") is workflow
        assert not workflows.is_open("def")
        assert workflows.find("def") is None
        with pytest.raises(OpenWorkflowError, match="not open"):
            workflows.get("def")

    def test_close_marks_the_runtime_closed(self) -> None:
        workflows = OpenWorkflowSet(MagicMock())
        workflow = workflows.open("abc")
        assert not workflow.closed

        workflows.close(workflow)

        assert workflow.closed
        reopened = workflows.open("abc")
        assert not reopened.closed

    def test_close_refuses_a_runtime_that_is_not_open(self) -> None:
        workflows = OpenWorkflowSet(MagicMock())
        stale = workflows.open("abc")
        workflows.close(stale)
        reopened = workflows.open("abc")

        with pytest.raises(OpenWorkflowError, match="not open"):
            workflows.close(stale)

        assert workflows.all_open() == [reopened]

    def test_move_keeps_the_runtime_under_its_new_id_only(self) -> None:
        workflows = OpenWorkflowSet(MagicMock())
        workflow = workflows.open("abc")

        workflows.move(workflow, "def")

        assert workflow.workflow_id == "def"
        assert workflows.get("def") is workflow
        assert not workflows.is_open("abc")

    def test_a_moved_workflow_is_still_found_by_its_old_id_when_following_moves(self) -> None:
        workflows = OpenWorkflowSet(MagicMock())
        workflow = workflows.open("abc")

        workflows.move(workflow, "def")
        workflows.move(workflow, "ghi")

        assert workflows.find("abc") is None
        assert workflows.find_following_moves("abc") is workflow
        assert workflows.find_following_moves("def") is workflow

    def test_closing_a_moved_workflow_forgets_its_old_ids(self) -> None:
        workflows = OpenWorkflowSet(MagicMock())
        workflow = workflows.open("abc")
        workflows.move(workflow, "def")

        workflows.close(workflow)

        assert workflows.find_following_moves("abc") is None

    def test_an_old_id_opened_again_names_the_workflow_opened_under_it(self) -> None:
        workflows = OpenWorkflowSet(MagicMock(), allow_several_open=True)
        moved = workflows.open("abc")
        workflows.move(moved, "def")

        reopened = workflows.open("abc")

        assert workflows.find_following_moves("abc") is reopened
        assert workflows.find_following_moves("def") is moved

    def test_moving_onto_an_old_id_takes_it_back(self) -> None:
        workflows = OpenWorkflowSet(MagicMock(), allow_several_open=True)
        first = workflows.open("abc")
        workflows.move(first, "def")
        second = workflows.open("xyz")

        workflows.move(second, "abc")
        workflows.close(second)

        assert workflows.find_following_moves("abc") is None
        assert workflows.find_following_moves("def") is first

    def test_move_refuses_an_id_already_open(self) -> None:
        workflows = OpenWorkflowSet(MagicMock())
        workflow = workflows.open("abc")

        with pytest.raises(OpenWorkflowError, match="'abc' is already open"):
            workflows.move(workflow, "abc")

        assert workflows.get("abc") is workflow

    def test_move_refuses_a_runtime_that_is_not_open(self) -> None:
        workflows = OpenWorkflowSet(MagicMock())
        stale = workflows.open("abc")
        workflows.close(stale)

        with pytest.raises(OpenWorkflowError, match="not open"):
            workflows.move(stale, "def")

        assert workflows.all_open() == []


class TestOpenWorkflowsFollowTheContext:
    """A workflow is open exactly while it is the Workflow context."""

    def test_a_push_onto_an_empty_stack_opens_the_pushed_workflow(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="outer")

        [workflow] = engine.open_workflows.all_open()
        assert workflow.workflow_id == engine.context_manager.get_current_workflow_id()

    def test_popping_the_workflow_closes_it(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="outer")

        engine.context_manager.pop_workflow()

        assert engine.open_workflows.all_open() == []

    def test_adopting_an_orchestrators_workflow_opens_nothing(self, engine: Engine) -> None:
        peer_id = "00000000000000000000000000000abc"

        workflow = engine.context_manager.adopt_orchestrator_workflow(peer_id, None, None, None)

        assert engine.worker_workflows.find(peer_id) is workflow
        assert engine.open_workflows.all_open() == []
