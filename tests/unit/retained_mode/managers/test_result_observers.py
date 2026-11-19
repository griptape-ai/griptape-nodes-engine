"""`EventManager.add_result_observer`: every result, inline, with the workflow its handler took."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from griptape_nodes.retained_mode.events.context_events import (
    EnsureWorkflowAndFlowRequest,
    EnsureWorkflowAndFlowResultSuccess,
    ListOpenWorkflowsRequest,
)
from griptape_nodes.retained_mode.events.flow_events import SetFlowMetadataRequest
from griptape_nodes.retained_mode.managers.event_manager import ResultContext

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime


def _open(engine: Engine) -> WorkflowRuntime:
    result = engine.handle_request(EnsureWorkflowAndFlowRequest(display_name="observed"))
    assert isinstance(result, EnsureWorkflowAndFlowResultSuccess), result.result_details
    assert result.workflow_id is not None
    return engine.open_workflows.get(result.workflow_id)


class TestResultObservers:
    def test_an_observer_sees_the_workflow_a_handler_took(self, engine: Engine) -> None:
        workflow = _open(engine)
        seen: list[tuple[str, WorkflowRuntime | None]] = []

        def observe(result: ResultPayload, observed: WorkflowRuntime | None) -> None:
            seen.append((type(result).__name__, observed))

        engine.event_manager.add_result_observer(observe)
        try:
            engine.event_manager.handle_request(
                SetFlowMetadataRequest(metadata={}), result_context=ResultContext(open_workflow_id=workflow.workflow_id)
            )
            engine.event_manager.handle_request(ListOpenWorkflowsRequest())
        finally:
            engine.event_manager.remove_result_observer(observe)

        assert seen == [("SetFlowMetadataResultSuccess", workflow), ("ListOpenWorkflowsResultSuccess", None)]

    def test_a_change_an_observer_makes_reaches_the_result_event(self, engine: Engine) -> None:
        workflow = _open(engine)

        def quiet(result: ResultPayload, _workflow: WorkflowRuntime | None) -> None:
            result.result_details = "observed"

        engine.event_manager.add_result_observer(quiet)
        try:
            result_event = engine.event_manager.handle_request(
                SetFlowMetadataRequest(metadata={}), result_context=ResultContext(open_workflow_id=workflow.workflow_id)
            )
        finally:
            engine.event_manager.remove_result_observer(quiet)

        assert str(result_event.result.result_details) == "observed"

    def test_a_raising_observer_leaves_the_result_and_the_other_observers(self, engine: Engine) -> None:
        workflow = _open(engine)
        seen: list[str] = []

        def raises(_result: ResultPayload, _workflow: WorkflowRuntime | None) -> None:
            msg = "observer broke"
            raise RuntimeError(msg)

        def records(result: ResultPayload, _workflow: WorkflowRuntime | None) -> None:
            seen.append(type(result).__name__)

        engine.event_manager.add_result_observer(raises)
        engine.event_manager.add_result_observer(records)
        try:
            result_event = engine.event_manager.handle_request(
                SetFlowMetadataRequest(metadata={}), result_context=ResultContext(open_workflow_id=workflow.workflow_id)
            )
        finally:
            engine.event_manager.remove_result_observer(raises)
            engine.event_manager.remove_result_observer(records)

        assert result_event.result.succeeded()
        assert seen == ["SetFlowMetadataResultSuccess"]

    def test_observers_that_compare_equal_are_registered_and_removed_apart(self, engine: Engine) -> None:
        workflow = _open(engine)
        calls: list[str] = []

        @dataclass
        class _Observer:
            name: str

            def __call__(self, _result: ResultPayload, _workflow: WorkflowRuntime | None) -> None:
                calls.append(self.name)

        first, second = _Observer("same"), _Observer("same")
        engine.event_manager.add_result_observer(first)
        engine.event_manager.add_result_observer(second)
        engine.event_manager.remove_result_observer(first)
        try:
            engine.event_manager.handle_request(
                SetFlowMetadataRequest(metadata={}), result_context=ResultContext(open_workflow_id=workflow.workflow_id)
            )
        finally:
            engine.event_manager.remove_result_observer(second)

        assert calls == ["same"]

    def test_squelching_a_load_reports_no_change_and_marks_nothing(self, engine: Engine) -> None:
        workflow = _open(engine)

        with engine.workflow_manager.squelch_workflow_altered():
            result_event = engine.event_manager.handle_request(
                SetFlowMetadataRequest(metadata={}), result_context=ResultContext(open_workflow_id=workflow.workflow_id)
            )

        assert not result_event.result.altered_workflow_state
        assert not workflow.has_unsaved_changes
