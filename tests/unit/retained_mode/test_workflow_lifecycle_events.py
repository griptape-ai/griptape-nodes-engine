"""`WorkflowOpenedEvent` and `WorkflowClosedEvent`, and the workflow open and close results name."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
from griptape_nodes.retained_mode.events.app_events import (
    OpenWorkflowInfo,
    WorkflowClosedEvent,
    WorkflowOpenedEvent,
)
from griptape_nodes.retained_mode.events.base_events import (
    AppEvent,
    EventResult,
    EventResultFailure,
    EventResultSuccess,
    GriptapeNodeEvent,
    RequestPayload,
    ResultPayload,
)
from griptape_nodes.retained_mode.events.context_events import (
    CloseWorkflowRequest,
    EnsureWorkflowAndFlowRequest,
    EnsureWorkflowAndFlowResultSuccess,
    SetWorkflowContextRequest,
    SetWorkflowContextSuccess,
)
from griptape_nodes.retained_mode.events.library_events import ReloadAllLibrariesRequest
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.project_events import (
    LoadProjectTemplateRequest,
    LoadProjectTemplateResultSuccess,
    SetCurrentProjectRequest,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    RunWorkflowFromRegistryRequest,
    RunWorkflowFromRegistryResultFailure,
    RunWorkflowFromRegistryResultSuccess,
    RunWorkflowFromScratchRequest,
    RunWorkflowFromScratchResultSuccess,
    RunWorkflowWithCurrentStateRequest,
    RunWorkflowWithCurrentStateResultSuccess,
    SaveWorkflowRequest,
    SaveWorkflowResultSuccess,
    WorkflowStatus,
)
from griptape_nodes.retained_mode.managers.event_manager import ResultContext
from griptape_nodes.retained_mode.managers.workflow.running import WorkflowExecutionResult, WorkflowRunner
from griptape_nodes.servers.mcp import _handle_in_named_workflow

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

LifecycleEvent = WorkflowOpenedEvent | WorkflowClosedEvent

PEER_ID = "00000000000000000000000000000abc"


@dataclass(kw_only=True)
class _RelayedOpenRequest(RequestPayload):
    pass


@pytest.fixture
def queue(engine: Engine) -> asyncio.Queue:
    """The engine's outgoing event queue, which clients read."""
    events: asyncio.Queue = asyncio.Queue()
    engine.event_manager.initialize_queue(events)
    return events


def _drain(queue: asyncio.Queue) -> list[object]:
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


def _lifecycle_events(events: list[object]) -> list[LifecycleEvent]:
    """The workflow lifecycle events among `events`, in the order clients get them."""
    return [
        event.payload
        for event in events
        if isinstance(event, AppEvent) and isinstance(event.payload, (WorkflowOpenedEvent, WorkflowClosedEvent))
    ]


def _result_events_for(events: list[object], request_type: type[RequestPayload]) -> list[EventResult]:
    """The result events among `events` for requests of `request_type`, leaving out the requests they sent."""
    return [
        event.wrapped_event
        for event in events
        if isinstance(event, GriptapeNodeEvent) and isinstance(event.wrapped_event.request, request_type)
    ]


def _send(
    engine: Engine, request: RequestPayload, open_workflow_id: str | None
) -> EventResultSuccess | EventResultFailure:
    """Dispatch `request` as a client would, with its envelope naming `open_workflow_id`."""
    return engine.event_manager.handle_request(request, result_context=ResultContext(open_workflow_id=open_workflow_id))


async def _send_async(
    engine: Engine, request: RequestPayload, open_workflow_id: str | None
) -> EventResultSuccess | EventResultFailure:
    return await engine.event_manager.ahandle_request(
        request, result_context=ResultContext(open_workflow_id=open_workflow_id)
    )


def _open_new_workflow(engine: Engine, display_name: str) -> WorkflowRuntime:
    """Open a new workflow with its flow the way a client does, which announces it."""
    result = engine.handle_request(EnsureWorkflowAndFlowRequest(display_name=display_name))
    assert isinstance(result, EnsureWorkflowAndFlowResultSuccess), result.result_details
    assert result.workflow_id is not None
    return engine.open_workflows.get(result.workflow_id)


def _use_default_project(engine: Engine, workspace: Path) -> None:
    project_yml = workspace / "project_template.yml"
    project_yml.write_text(DEFAULT_PROJECT_TEMPLATE.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE))
    load_result = engine.handle_request(LoadProjectTemplateRequest(project_path=project_yml))
    assert isinstance(load_result, LoadProjectTemplateResultSuccess)
    engine.handle_request(SetCurrentProjectRequest(project_id=load_result.project_id))
    engine.config_manager.workspace_path = workspace


@pytest.fixture
def saved_workflow_name(engine: Engine, tmp_path: Path) -> Iterator[str]:
    """A saved workflow file in a fresh workspace, closed again."""
    workspace = tmp_path.resolve() / "workspace"
    workspace.mkdir()
    _use_default_project(engine, workspace)
    opened = engine.handle_request(EnsureWorkflowAndFlowRequest(display_name="saved"))
    assert isinstance(opened, EnsureWorkflowAndFlowResultSuccess), opened.result_details
    saved = asyncio.run(engine.ahandle_request(SaveWorkflowRequest()))
    assert isinstance(saved, SaveWorkflowResultSuccess), saved.result_details
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    yield saved.workflow_name
    engine.handle_request(SetCurrentProjectRequest(project_id=None))


def _saved_file_path(engine: Engine, saved_workflow_name: str) -> str:
    file_path = engine.workflow_registry.get_workflow_by_name(saved_workflow_name).file_path
    assert file_path is not None
    return file_path


class TestSetWorkflowContext:
    def test_announces_the_workflow_it_opened_and_names_it_on_the_result(
        self, engine: Engine, queue: asyncio.Queue
    ) -> None:
        result_event = _send(engine, SetWorkflowContextRequest(display_name="new"), None)

        assert isinstance(result_event.result, SetWorkflowContextSuccess)
        opened = result_event.result.open_workflow
        assert opened is not None
        assert _lifecycle_events(_drain(queue)) == [WorkflowOpenedEvent(open_workflow=opened)]
        assert result_event.open_workflow_id == opened.workflow_id

    def test_fails_while_another_workflow_is_open_and_announces_nothing(
        self, engine: Engine, queue: asyncio.Queue
    ) -> None:
        open_workflow = _open_new_workflow(engine, "open")
        _drain(queue)

        result_event = _send(engine, SetWorkflowContextRequest(display_name="next"), open_workflow.workflow_id)

        assert isinstance(result_event, EventResultFailure)
        assert engine.open_workflows.all_open() == [open_workflow]
        assert _lifecycle_events(_drain(queue)) == []

    def test_ensure_announces_the_workflow_it_opened_once(self, engine: Engine, queue: asyncio.Queue) -> None:
        result = engine.handle_request(EnsureWorkflowAndFlowRequest(display_name="new"))

        assert isinstance(result, EnsureWorkflowAndFlowResultSuccess), result.result_details
        announced = _lifecycle_events(_drain(queue))
        assert len(announced) == 1
        assert isinstance(announced[0], WorkflowOpenedEvent)
        assert announced[0].open_workflow.workflow_id == result.workflow_id


class TestRunWorkflowFromRegistry:
    def test_announces_the_workflow_once_its_file_has_built_it(
        self, engine: Engine, saved_workflow_name: str, queue: asyncio.Queue
    ) -> None:
        result_event = asyncio.run(
            _send_async(engine, RunWorkflowFromRegistryRequest(workflow_name=saved_workflow_name), None)
        )

        assert isinstance(result_event.result, RunWorkflowFromRegistryResultSuccess), result_event.result.result_details
        opened = result_event.result.open_workflow
        assert opened is not None
        assert _lifecycle_events(_drain(queue)) == [WorkflowOpenedEvent(open_workflow=opened)]
        # Built: the file's flow is in place when clients hear of the workflow.
        assert opened.has_active_flow
        assert result_event.open_workflow_id == opened.workflow_id

    def test_announces_neither_open_nor_close_for_a_load_that_fails(
        self, engine: Engine, saved_workflow_name: str, queue: asyncio.Queue
    ) -> None:
        failed_run = WorkflowExecutionResult(
            execution_successful=False, execution_details="broken file", status=WorkflowStatus.UNUSABLE
        )

        with patch.object(WorkflowRunner, "run_workflow", return_value=failed_run):
            result_event = asyncio.run(
                _send_async(engine, RunWorkflowFromRegistryRequest(workflow_name=saved_workflow_name), None)
            )

        assert isinstance(result_event.result, RunWorkflowFromRegistryResultFailure)
        assert engine.open_workflows.all_open() == []
        assert _lifecycle_events(_drain(queue)) == []

    def test_a_clean_slate_load_closes_the_open_workflow_before_announcing_its_own(
        self, engine: Engine, saved_workflow_name: str, queue: asyncio.Queue
    ) -> None:
        replaced = _open_new_workflow(engine, "replaced")
        _drain(queue)
        request = RunWorkflowFromRegistryRequest(workflow_name=saved_workflow_name, run_with_clean_slate=True)

        result_event = asyncio.run(_send_async(engine, request, replaced.workflow_id))

        assert isinstance(result_event.result, RunWorkflowFromRegistryResultSuccess), result_event.result.result_details
        opened = result_event.result.open_workflow
        assert opened is not None
        assert _lifecycle_events(_drain(queue)) == [
            WorkflowClosedEvent(open_workflow_id=replaced.workflow_id),
            WorkflowOpenedEvent(open_workflow=opened),
        ]
        assert result_event.open_workflow_id == opened.workflow_id


class TestRunWorkflowFromFile:
    def test_from_scratch_closes_the_open_workflow_then_announces_the_file(
        self, engine: Engine, saved_workflow_name: str, queue: asyncio.Queue
    ) -> None:
        replaced = _open_new_workflow(engine, "replaced")
        _drain(queue)
        request = RunWorkflowFromScratchRequest(file_path=_saved_file_path(engine, saved_workflow_name))

        result_event = asyncio.run(_send_async(engine, request, replaced.workflow_id))

        assert isinstance(result_event.result, RunWorkflowFromScratchResultSuccess), result_event.result.result_details
        opened = result_event.result.open_workflow
        assert opened is not None
        assert _lifecycle_events(_drain(queue)) == [
            WorkflowClosedEvent(open_workflow_id=replaced.workflow_id),
            WorkflowOpenedEvent(open_workflow=opened),
        ]
        assert result_event.open_workflow_id == opened.workflow_id

    def test_with_current_state_announces_the_file_it_opened(
        self, engine: Engine, saved_workflow_name: str, queue: asyncio.Queue
    ) -> None:
        request = RunWorkflowWithCurrentStateRequest(file_path=_saved_file_path(engine, saved_workflow_name))

        result_event = asyncio.run(_send_async(engine, request, None))

        assert isinstance(result_event.result, RunWorkflowWithCurrentStateResultSuccess), (
            result_event.result.result_details
        )
        opened = result_event.result.open_workflow
        assert opened is not None
        assert _lifecycle_events(_drain(queue)) == [WorkflowOpenedEvent(open_workflow=opened)]
        assert result_event.open_workflow_id == opened.workflow_id


class TestClosing:
    def test_close_announces_the_workflow_it_closed_and_names_it_on_the_result(
        self, engine: Engine, queue: asyncio.Queue
    ) -> None:
        workflow = _open_new_workflow(engine, "closing")
        _drain(queue)

        result_event = asyncio.run(_send_async(engine, CloseWorkflowRequest(), workflow.workflow_id))

        assert result_event.succeeded(), result_event.result.result_details
        assert _lifecycle_events(_drain(queue)) == [WorkflowClosedEvent(open_workflow_id=workflow.workflow_id)]
        assert result_event.open_workflow_id == workflow.workflow_id

    def test_an_mcp_close_with_no_envelope_names_the_workflow_it_closed(
        self, engine: Engine, queue: asyncio.Queue
    ) -> None:
        workflow = _open_new_workflow(engine, "closing")
        _drain(queue)

        result = asyncio.run(_handle_in_named_workflow(CloseWorkflowRequest(), workflow.workflow_id))

        assert result.succeeded(), result.result_details
        events = _drain(queue)
        assert _lifecycle_events(events) == [WorkflowClosedEvent(open_workflow_id=workflow.workflow_id)]
        close_results = _result_events_for(events, CloseWorkflowRequest)
        assert [event.open_workflow_id for event in close_results] == [workflow.workflow_id]

    def test_clear_all_announces_each_workflow_it_closed_and_names_none(
        self, engine: Engine, queue: asyncio.Queue
    ) -> None:
        workflow = _open_new_workflow(engine, "cleared")
        _drain(queue)

        result_event = asyncio.run(
            _send_async(engine, ClearAllObjectStateRequest(i_know_what_im_doing=True), workflow.workflow_id)
        )

        assert result_event.succeeded(), result_event.result.result_details
        assert _lifecycle_events(_drain(queue)) == [WorkflowClosedEvent(open_workflow_id=workflow.workflow_id)]
        assert result_event.open_workflow_id is None

    def test_a_library_reload_announces_the_workflow_it_closed(self, engine: Engine, queue: asyncio.Queue) -> None:
        workflow = _open_new_workflow(engine, "reloaded")
        _drain(queue)

        _send(engine, ReloadAllLibrariesRequest(), workflow.workflow_id)

        assert engine.open_workflows.all_open() == []
        assert WorkflowClosedEvent(open_workflow_id=workflow.workflow_id) in _lifecycle_events(_drain(queue))

    def test_a_project_switch_to_another_workspace_announces_the_workflow_it_closed(
        self, engine: Engine, queue: asyncio.Queue
    ) -> None:
        workflow = _open_new_workflow(engine, "switched")
        _drain(queue)

        failure = asyncio.run(
            engine.project_manager._reload_after_project_switch(
                "another-project", workspace_changed=True, library_config_changed=False
            )
        )

        assert failure is None
        assert _lifecycle_events(_drain(queue)) == [WorkflowClosedEvent(open_workflow_id=workflow.workflow_id)]

    def test_a_workflow_never_announced_closes_without_an_event(self, engine: Engine, queue: asyncio.Queue) -> None:
        unannounced = engine.open_workflow(engine.workflow_registry.register_unsaved("quiet", listed=False).workflow_id)

        engine.close_workflow(unannounced)

        assert _lifecycle_events(_drain(queue)) == []


class TestWorkers:
    @pytest.fixture
    def worker_engine(self, engine: Engine) -> Engine:
        engine.library_manager._is_worker = True
        return engine

    def test_a_workflow_a_worker_keeps_for_its_orchestrator_is_never_announced(
        self, worker_engine: Engine, queue: asyncio.Queue
    ) -> None:
        workflow = worker_engine.context_manager.adopt_orchestrator_workflow(PEER_ID, None, None, None)
        with worker_engine.run_in_worker_workflow(workflow):
            pass

        worker_engine.close_worker_workflow(workflow)

        assert _lifecycle_events(_drain(queue)) == []

    def test_a_worker_relaying_an_open_result_announces_nothing(
        self, worker_engine: Engine, queue: asyncio.Queue
    ) -> None:
        worker_engine.context_manager.adopt_orchestrator_workflow(PEER_ID, None, None, None)
        relayed = OpenWorkflowInfo(workflow_id=PEER_ID, name="peer", workflow_file_path=None, has_active_flow=True)

        def relay_open(_request: _RelayedOpenRequest) -> ResultPayload:
            return SetWorkflowContextSuccess(workflow_id=PEER_ID, open_workflow=relayed, result_details="opened")

        worker_engine.event_manager.assign_manager_to_request_type(_RelayedOpenRequest, relay_open)

        result_event = _send(worker_engine, _RelayedOpenRequest(), None)

        assert result_event.succeeded()
        assert _lifecycle_events(_drain(queue)) == []
