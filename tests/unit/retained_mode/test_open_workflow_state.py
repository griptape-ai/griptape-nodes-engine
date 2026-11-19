"""An open workflow's unsaved changes and runs, as `OpenWorkflowInfo` reports them and `WorkflowStateChangedEvent` announces."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
from griptape_nodes.retained_mode.events.app_events import (
    OpenWorkflowState,
    WorkflowRunState,
    WorkflowStateChangedEvent,
)
from griptape_nodes.retained_mode.events.base_events import AppEvent, RequestPayload
from griptape_nodes.retained_mode.events.context_events import (
    CloseWorkflowRequest,
    EnsureWorkflowAndFlowRequest,
    EnsureWorkflowAndFlowResultSuccess,
    ListOpenWorkflowsRequest,
    ListOpenWorkflowsResultSuccess,
    OpenWorkflowRequest,
    OpenWorkflowResultSuccess,
)
from griptape_nodes.retained_mode.events.execution_events import StartFlowResultFailure, StartFlowResultSuccess
from griptape_nodes.retained_mode.events.flow_events import SetFlowMetadataRequest
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.project_events import (
    LoadProjectTemplateRequest,
    LoadProjectTemplateResultSuccess,
    SetCurrentProjectRequest,
)
from griptape_nodes.retained_mode.events.workflow_events import SaveWorkflowRequest, SaveWorkflowResultSuccess
from griptape_nodes.retained_mode.managers.event_manager import ResultContext
from griptape_nodes.retained_mode.managers.run_queue import RunSpec

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime


@pytest.fixture
def queue(engine: Engine) -> asyncio.Queue:
    """The engine's outgoing event queue, which clients read."""
    events: asyncio.Queue = asyncio.Queue()
    engine.event_manager.initialize_queue(events)
    return events


@pytest.fixture
def workspace(engine: Engine, tmp_path: Path) -> Iterator[Path]:
    """A fresh workspace with the default project, so workflows can be saved."""
    workspace = tmp_path.resolve() / "workspace"
    workspace.mkdir()
    project_yml = workspace / "project_template.yml"
    project_yml.write_text(DEFAULT_PROJECT_TEMPLATE.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE))
    load_result = engine.handle_request(LoadProjectTemplateRequest(project_path=project_yml))
    assert isinstance(load_result, LoadProjectTemplateResultSuccess)
    engine.handle_request(SetCurrentProjectRequest(project_id=load_result.project_id))
    engine.config_manager.workspace_path = workspace
    yield workspace
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    engine.handle_request(SetCurrentProjectRequest(project_id=None))


def _state_events(queue: asyncio.Queue) -> list[WorkflowStateChangedEvent]:
    """The state changes clients were told of since the last call, in order."""
    events = []
    while not queue.empty():
        event = queue.get_nowait()
        if isinstance(event, AppEvent) and isinstance(event.payload, WorkflowStateChangedEvent):
            events.append(event.payload)
    return events


def _open(engine: Engine, display_name: str) -> WorkflowRuntime:
    result = engine.handle_request(EnsureWorkflowAndFlowRequest(display_name=display_name))
    assert isinstance(result, EnsureWorkflowAndFlowResultSuccess), result.result_details
    assert result.workflow_id is not None
    return engine.open_workflows.get(result.workflow_id)


def _send(engine: Engine, request: RequestPayload, workflow: WorkflowRuntime) -> Any:
    """Dispatch `request` as a client would, naming `workflow` on its envelope, and return its result."""
    context = ResultContext(open_workflow_id=workflow.workflow_id)
    return engine.event_manager.handle_request(request, result_context=context).result


async def _send_async(engine: Engine, request: RequestPayload, workflow: WorkflowRuntime) -> Any:
    context = ResultContext(open_workflow_id=workflow.workflow_id)
    return (await engine.event_manager.ahandle_request(request, result_context=context)).result


def _edit(engine: Engine, workflow: WorkflowRuntime) -> None:
    """Change `workflow` the way a client's edit does."""
    result = _send(engine, SetFlowMetadataRequest(metadata={"edited": True}), workflow)
    assert result.succeeded(), result.result_details
    assert result.altered_workflow_state


class TestUnsavedChanges:
    def test_a_workflow_just_opened_has_none(self, engine: Engine) -> None:
        workflow = _open(engine, "fresh")

        assert engine.open_workflow_info(workflow).state == OpenWorkflowState()

    @pytest.mark.usefixtures("workspace")
    def test_a_saved_workflow_opens_with_none(self, engine: Engine) -> None:
        opened = asyncio.run(engine.ahandle_request(OpenWorkflowRequest(display_name="saved", create_flow=True)))
        assert isinstance(opened, OpenWorkflowResultSuccess), opened.result_details
        workflow = engine.open_workflows.get(opened.open_workflow.workflow_id)
        saved = asyncio.run(_send_async(engine, SaveWorkflowRequest(), workflow))
        asyncio.run(engine.ahandle_request(CloseWorkflowRequest(workflow_id=workflow.workflow_id)))

        reopened = asyncio.run(engine.ahandle_request(OpenWorkflowRequest(workflow_id=saved.workflow_id)))

        assert isinstance(reopened, OpenWorkflowResultSuccess), reopened.result_details
        assert reopened.open_workflow.state == OpenWorkflowState()

    def test_an_edit_marks_it_and_clients_are_told_once(self, engine: Engine, queue: asyncio.Queue) -> None:
        workflow = _open(engine, "edited")
        _state_events(queue)

        _edit(engine, workflow)
        _edit(engine, workflow)

        changed = OpenWorkflowState(has_unsaved_changes=True)
        assert _state_events(queue) == [WorkflowStateChangedEvent(workflow_id=workflow.workflow_id, state=changed)]
        assert engine.open_workflow_info(workflow).state == changed

    def test_ensuring_the_flow_of_a_workflow_that_has_one_changes_nothing(self, engine: Engine) -> None:
        workflow = _open(engine, "ready")

        result = _send(engine, EnsureWorkflowAndFlowRequest(), workflow)

        assert isinstance(result, EnsureWorkflowAndFlowResultSuccess), result.result_details
        assert not result.created_flow
        assert not engine.open_workflow_info(workflow).state.has_unsaved_changes

    def test_an_edit_marks_only_the_workflow_it_changed(self, engine: Engine) -> None:
        edited = _open(engine, "edited")
        other = _open(engine, "other")

        _edit(engine, edited)

        assert engine.open_workflow_info(edited).state.has_unsaved_changes
        assert not engine.open_workflow_info(other).state.has_unsaved_changes

    def test_listing_the_open_workflows_reports_them(self, engine: Engine) -> None:
        workflow = _open(engine, "edited")
        _edit(engine, workflow)

        result = engine.handle_request(ListOpenWorkflowsRequest())

        assert isinstance(result, ListOpenWorkflowsResultSuccess)
        assert [info.state.has_unsaved_changes for info in result.open_workflows] == [True]

    @pytest.mark.usefixtures("workspace")
    def test_saving_to_its_own_file_clears_them(self, engine: Engine, queue: asyncio.Queue) -> None:
        workflow = _open(engine, "saved")
        _edit(engine, workflow)
        _state_events(queue)

        result = asyncio.run(_send_async(engine, SaveWorkflowRequest(), workflow))

        assert isinstance(result, SaveWorkflowResultSuccess), result.result_details
        assert result.open_workflow is not None
        assert not result.open_workflow.state.has_unsaved_changes
        assert _state_events(queue) == [
            WorkflowStateChangedEvent(workflow_id=workflow.workflow_id, state=OpenWorkflowState())
        ]

    @pytest.mark.usefixtures("workspace")
    def test_a_save_as_that_leaves_it_on_its_own_file_keeps_them(self, engine: Engine) -> None:
        workflow = _open(engine, "original")
        asyncio.run(_send_async(engine, SaveWorkflowRequest(), workflow))
        _edit(engine, workflow)

        result = asyncio.run(_send_async(engine, SaveWorkflowRequest(file_name="the_copy"), workflow))

        assert isinstance(result, SaveWorkflowResultSuccess), result.result_details
        assert result.workflow_id != workflow.workflow_id
        assert engine.open_workflow_info(workflow).state.has_unsaved_changes

    @pytest.mark.usefixtures("workspace")
    def test_a_save_as_that_continues_in_the_copy_clears_them(self, engine: Engine) -> None:
        workflow = _open(engine, "original")
        asyncio.run(_send_async(engine, SaveWorkflowRequest(), workflow))
        _edit(engine, workflow)

        result = asyncio.run(
            _send_async(engine, SaveWorkflowRequest(file_name="the_copy", continue_in_copy=True), workflow)
        )

        assert isinstance(result, SaveWorkflowResultSuccess), result.result_details
        assert not engine.open_workflow_info(workflow).state.has_unsaved_changes


@dataclass
class _Spec(RunSpec):
    """A run that executes `body`, standing in for a request's run."""

    flow_name: str
    body: Callable[[], Awaitable[Any]]

    async def execute(self, engine: Engine) -> Any:  # noqa: ARG002
        return await self.body()

    def failure(self, details: str) -> Any:
        return StartFlowResultFailure(validation_exceptions=[], result_details=details)


class TestRuns:
    @pytest.mark.asyncio
    async def test_clients_are_told_a_run_started_waited_and_ended(self, engine: Engine, queue: asyncio.Queue) -> None:
        workflow = _open(engine, "runs")
        _state_events(queue)
        first_may_end = asyncio.Event()

        async def first() -> Any:
            await first_may_end.wait()
            return StartFlowResultSuccess(result_details="first ran")

        async def second() -> Any:
            return StartFlowResultSuccess(result_details="second ran")

        with engine.bind_workflow(workflow):
            first_run = engine.flow_manager.submit_run(_Spec("flow", first))
            second_run = engine.flow_manager.submit_run(_Spec("flow", second))
        first_may_end.set()
        await asyncio.wait_for(asyncio.gather(first_run.ended, second_run.ended), timeout=1.0)

        assert [event.state for event in _state_events(queue)] == [
            OpenWorkflowState(run_state=WorkflowRunState.RUNNING),
            OpenWorkflowState(run_state=WorkflowRunState.RUNNING, queued_run_count=1),
            OpenWorkflowState(run_state=WorkflowRunState.RUNNING),
            OpenWorkflowState(),
        ]

    @pytest.mark.asyncio
    async def test_a_failed_run_reports_why_until_the_next_run_starts(self, engine: Engine) -> None:
        workflow = _open(engine, "runs")
        next_may_end = asyncio.Event()

        async def fails() -> Any:
            return StartFlowResultFailure(validation_exceptions=[], result_details="Node 'Text One' failed.")

        async def holds() -> Any:
            await next_may_end.wait()
            return StartFlowResultSuccess(result_details="ran")

        with engine.bind_workflow(workflow):
            failed = engine.flow_manager.submit_run(_Spec("flow", fails))
        await asyncio.wait_for(failed.ended, timeout=1.0)
        assert engine.open_workflow_info(workflow).state == OpenWorkflowState(last_run_error="Node 'Text One' failed.")

        with engine.bind_workflow(workflow):
            following = engine.flow_manager.submit_run(_Spec("flow", holds))
        assert engine.open_workflow_info(workflow).state == OpenWorkflowState(run_state=WorkflowRunState.RUNNING)
        next_may_end.set()
        await asyncio.wait_for(following.ended, timeout=1.0)

    @pytest.mark.asyncio
    async def test_a_paused_run_whose_node_fails_while_stepping_reports_why(self, engine: Engine) -> None:
        workflow = _open(engine, "runs")
        flows = workflow.flows
        flow_manager = engine.flow_manager
        flow_running = {"value": False}

        async def pauses() -> Any:
            flow_running["value"] = True
            return StartFlowResultSuccess(result_details="paused at a breakpoint")

        machine = MagicMock(is_advancing=False)
        machine.resolution_machine.is_advancing = False
        machine.resolution_machine.is_errored.return_value = True
        machine.resolution_machine.get_error_message.return_value = "Node 'Text One' failed."
        with patch.object(flow_manager, "_flow_is_running", side_effect=lambda _flows: flow_running["value"]):
            with engine.bind_workflow(workflow):
                run = flow_manager.submit_run(_Spec("flow", pauses))
            await asyncio.wait_for(run.outcome, timeout=1.0)

            flows.global_control_flow_machine = machine
            flow_running["value"] = False
            flow_manager._finish_run_if_over(flows)
            await asyncio.wait_for(run.ended, timeout=1.0)

        assert engine.open_workflow_info(workflow).state.last_run_error == (
            "Attempted to run flow 'flow'. Failed due to: Node 'Text One' failed."
        )

    @pytest.mark.asyncio
    async def test_a_run_that_raises_reports_the_error(self, engine: Engine) -> None:
        workflow = _open(engine, "runs")

        async def raises() -> Any:
            msg = "out of memory"
            raise RuntimeError(msg)

        with engine.bind_workflow(workflow):
            run = engine.flow_manager.submit_run(_Spec("flow", raises))
        await asyncio.wait_for(run.ended, timeout=1.0)

        assert engine.open_workflow_info(workflow).state.last_run_error == (
            "Attempted to run flow 'flow'. Failed due to: out of memory"
        )

    @pytest.mark.asyncio
    async def test_a_cancelled_waiting_run_leaves_the_line(self, engine: Engine, queue: asyncio.Queue) -> None:
        workflow = _open(engine, "runs")
        first_may_end = asyncio.Event()

        async def holds() -> Any:
            await first_may_end.wait()
            return StartFlowResultSuccess(result_details="ran")

        with engine.bind_workflow(workflow):
            first = engine.flow_manager.submit_run(_Spec("flow", holds))
            waiting = engine.flow_manager.submit_run(_Spec("flow", holds))
        _state_events(queue)

        assert await engine.flow_manager.cancel_run_in(workflow, waiting.run_id)

        assert [event.state for event in _state_events(queue)] == [
            OpenWorkflowState(run_state=WorkflowRunState.RUNNING)
        ]
        first_may_end.set()
        await asyncio.wait_for(first.ended, timeout=1.0)
