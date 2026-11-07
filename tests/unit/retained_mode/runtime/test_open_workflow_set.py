"""The open workflows: the single-open rule, and opening and closing with the Workflow context."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from griptape_nodes.retained_mode.beta_features import MULTIPLE_OPEN_WORKFLOWS
from griptape_nodes.retained_mode.runtime.open_workflow_set import OpenWorkflowError, OpenWorkflowSet

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine


@pytest.mark.usefixtures("multiple_open_workflows_off")
class TestOpenWorkflowSet:
    def test_opening_a_second_workflow_is_refused(self, engine: Engine) -> None:
        workflows = OpenWorkflowSet(engine)
        first = workflows.open("abc")

        with pytest.raises(OpenWorkflowError, match="already open"):
            workflows.open("abc")
        with pytest.raises(OpenWorkflowError, match="'abc' is already open"):
            workflows.open("def")

        assert workflows.all_open() == [first]

    def test_get_and_is_open_find_only_open_workflows(self, engine: Engine) -> None:
        workflows = OpenWorkflowSet(engine)
        workflow = workflows.open("abc")

        assert workflows.is_open("abc")
        assert workflows.get("abc") is workflow
        assert workflows.find("abc") is workflow
        assert not workflows.is_open("def")
        assert workflows.find("def") is None
        with pytest.raises(OpenWorkflowError, match="not open"):
            workflows.get("def")

    def test_close_marks_the_runtime_closed(self, engine: Engine) -> None:
        workflows = OpenWorkflowSet(engine)
        workflow = workflows.open("abc")
        assert not workflow.closed

        workflows.close(workflow)

        assert workflow.closed
        reopened = workflows.open("abc")
        assert not reopened.closed

    def test_close_refuses_a_runtime_that_is_not_open(self, engine: Engine) -> None:
        workflows = OpenWorkflowSet(engine)
        stale = workflows.open("abc")
        workflows.close(stale)
        reopened = workflows.open("abc")

        with pytest.raises(OpenWorkflowError, match="not open"):
            workflows.close(stale)

        assert workflows.all_open() == [reopened]


class TestOpenWorkflowSetFollowsTheBetaFeature:
    @pytest.mark.usefixtures("multiple_open_workflows")
    def test_the_beta_feature_lets_a_second_workflow_open_beside_the_first(self, engine: Engine) -> None:
        workflows = OpenWorkflowSet(engine)
        first = workflows.open("abc")
        second = workflows.open("def")

        with pytest.raises(OpenWorkflowError, match="already open"):
            workflows.open("abc")

        assert workflows.all_open() == [first, second]

    def test_turning_the_beta_feature_off_refuses_the_next_open_and_names_how_many_are_open(
        self, engine: Engine
    ) -> None:
        workflows = OpenWorkflowSet(engine)
        engine.config_manager.set_config_value(MULTIPLE_OPEN_WORKFLOWS.config_key, value=True)
        first = workflows.open("abc")
        second = workflows.open("def")

        engine.config_manager.set_config_value(MULTIPLE_OPEN_WORKFLOWS.config_key, value=False)

        with pytest.raises(OpenWorkflowError, match="2 workflows are open and only one can be"):
            workflows.open("ghi")
        assert workflows.all_open() == [first, second]


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
