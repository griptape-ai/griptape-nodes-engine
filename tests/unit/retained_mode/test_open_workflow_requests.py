"""`ListOpenWorkflowsRequest`, `OpenWorkflowRequest`, and the older open requests that share its code."""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
from griptape_nodes.retained_mode.events.app_events import WorkflowClosedEvent, WorkflowOpenedEvent
from griptape_nodes.retained_mode.events.base_events import (
    AppEvent,
    EventResultFailure,
    EventResultSuccess,
    RequestPayload,
)
from griptape_nodes.retained_mode.events.context_events import (
    AnotherWorkflowOpenResultFailure,
    EnsureWorkflowAndFlowRequest,
    EnsureWorkflowAndFlowResultSuccess,
    ListOpenWorkflowsRequest,
    ListOpenWorkflowsResultSuccess,
    OpenWorkflowRequest,
    OpenWorkflowResultFailure,
    OpenWorkflowResultSuccess,
    WorkflowNotFoundResultFailure,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.project_events import (
    LoadProjectTemplateRequest,
    LoadProjectTemplateResultSuccess,
    SetCurrentProjectRequest,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    RunWorkflowFromRegistryRequest,
    RunWorkflowFromRegistryResultSuccess,
    SaveWorkflowRequest,
    SaveWorkflowResultSuccess,
    WorkflowStatus,
)
from griptape_nodes.retained_mode.managers.event_manager import ResultContext
from griptape_nodes.retained_mode.managers.workflow.running import WorkflowExecutionResult, WorkflowRunner
from griptape_nodes.retained_mode.runtime.open_workflow_set import OpenWorkflowSet
from griptape_nodes.servers.mcp import SUPPORTED_REQUEST_EVENTS

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime


@pytest.fixture
def queue(engine: Engine) -> asyncio.Queue:
    """The engine's outgoing event queue, which clients read."""
    events: asyncio.Queue = asyncio.Queue()
    engine.event_manager.initialize_queue(events)
    return events


def _lifecycle_events(queue: asyncio.Queue) -> list[WorkflowOpenedEvent | WorkflowClosedEvent]:
    """The workflow lifecycle events clients got since the last call, in order."""
    events = []
    while not queue.empty():
        event = queue.get_nowait()
        if isinstance(event, AppEvent) and isinstance(event.payload, (WorkflowOpenedEvent, WorkflowClosedEvent)):
            events.append(event.payload)
    return events


def _send(
    engine: Engine, request: RequestPayload, open_workflow_id: str | None = None
) -> EventResultSuccess | EventResultFailure:
    """Dispatch `request` as a client would, with its envelope naming `open_workflow_id`."""
    return asyncio.run(
        engine.event_manager.ahandle_request(request, result_context=ResultContext(open_workflow_id=open_workflow_id))
    )


def _allow_several_open(engine: Engine) -> None:
    engine._open_workflows = OpenWorkflowSet(engine, allow_several_open=True)


def _open_new(engine: Engine, display_name: str) -> WorkflowRuntime:
    """Open a new unsaved workflow with its flow, as an editor tab would."""
    result = _send(engine, OpenWorkflowRequest(display_name=display_name, create_flow=True)).result
    assert isinstance(result, OpenWorkflowResultSuccess), result.result_details
    return engine.open_workflows.get(result.open_workflow.workflow_id)


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


def _saved_workflow_id(engine: Engine, saved_workflow_name: str) -> str:
    return engine.workflow_registry.get_workflow_by_name(saved_workflow_name).workflow_id


class TestListOpenWorkflows:
    def test_lists_none_while_no_workflow_is_open(self, engine: Engine) -> None:
        result = _send(engine, ListOpenWorkflowsRequest()).result

        assert isinstance(result, ListOpenWorkflowsResultSuccess)
        assert result.open_workflows == []
        assert result.engine_run_id == engine.engine_run_id

    def test_lists_the_open_workflow(self, engine: Engine) -> None:
        workflow = _open_new(engine, "only")

        result = _send(engine, ListOpenWorkflowsRequest()).result

        assert isinstance(result, ListOpenWorkflowsResultSuccess)
        assert result.open_workflows == [engine.open_workflow_info(workflow)]
        assert result.open_workflows[0].has_active_flow

    def test_lists_every_open_workflow_in_the_order_they_opened(self, engine: Engine) -> None:
        _allow_several_open(engine)
        first = _open_new(engine, "first")
        second = _open_new(engine, "second")

        result = _send(engine, ListOpenWorkflowsRequest()).result

        assert isinstance(result, ListOpenWorkflowsResultSuccess)
        assert result.open_workflows == [engine.open_workflow_info(first), engine.open_workflow_info(second)]

    def test_ignores_an_envelope_naming_a_workflow_that_is_not_open(self, engine: Engine) -> None:
        result_event = _send(engine, ListOpenWorkflowsRequest(), uuid.uuid4().hex)

        assert isinstance(result_event.result, ListOpenWorkflowsResultSuccess)
        assert result_event.open_workflow_id is None

    def test_mcp_clients_get_it_as_a_tool(self) -> None:
        assert SUPPORTED_REQUEST_EVENTS["ListOpenWorkflowsRequest"] is ListOpenWorkflowsRequest


class TestOpenWorkflowNew:
    def test_opens_a_new_unsaved_workflow_and_announces_it(self, engine: Engine, queue: asyncio.Queue) -> None:
        result_event = _send(engine, OpenWorkflowRequest(display_name="fresh"))

        result = result_event.result
        assert isinstance(result, OpenWorkflowResultSuccess), result.result_details
        assert not result.already_open
        assert result.open_workflow.workflow_file_path is None
        assert not result.open_workflow.is_saved
        assert not result.open_workflow.has_active_flow
        assert engine.workflow_registry.get(result.open_workflow.workflow_id).metadata.name == "fresh"
        assert _lifecycle_events(queue) == [WorkflowOpenedEvent(open_workflow=result.open_workflow)]
        assert result_event.open_workflow_id == result.open_workflow.workflow_id

    def test_creates_its_flow_when_asked(self, engine: Engine) -> None:
        result = _send(engine, OpenWorkflowRequest(create_flow=True)).result

        assert isinstance(result, OpenWorkflowResultSuccess), result.result_details
        assert result.open_workflow.has_active_flow
        assert engine.open_workflows.get(result.open_workflow.workflow_id).context.has_current_flow()

    def test_opens_beside_the_others_when_several_may_be_open(self, engine: Engine) -> None:
        _allow_several_open(engine)
        first = _open_new(engine, "first")

        result = _send(engine, OpenWorkflowRequest(display_name="second")).result

        assert isinstance(result, OpenWorkflowResultSuccess), result.result_details
        assert engine.open_workflows.all_open() == [first, engine.open_workflows.get(result.open_workflow.workflow_id)]


class TestOpenWorkflowSaved:
    def test_opens_a_saved_workflow_by_id_and_builds_its_file(
        self, engine: Engine, saved_workflow_name: str, queue: asyncio.Queue
    ) -> None:
        workflow_id = _saved_workflow_id(engine, saved_workflow_name)

        result = _send(engine, OpenWorkflowRequest(workflow_id=workflow_id)).result

        assert isinstance(result, OpenWorkflowResultSuccess), result.result_details
        assert not result.already_open
        assert result.status == WorkflowStatus.GOOD
        assert result.open_workflow.workflow_id == workflow_id
        assert result.open_workflow.workflow_file_path is not None
        assert result.open_workflow.is_saved
        # Built: the file's flow is in place.
        assert result.open_workflow.has_active_flow
        assert _lifecycle_events(queue) == [WorkflowOpenedEvent(open_workflow=result.open_workflow)]

    def test_opens_a_saved_workflow_by_name(self, engine: Engine, saved_workflow_name: str) -> None:
        result = _send(engine, OpenWorkflowRequest(workflow_name=saved_workflow_name)).result

        assert isinstance(result, OpenWorkflowResultSuccess), result.result_details
        assert result.open_workflow.workflow_id == _saved_workflow_id(engine, saved_workflow_name)
        assert result.open_workflow.has_active_flow

    def test_a_file_that_fails_to_build_closes_the_workflow_and_announces_nothing(
        self, engine: Engine, saved_workflow_name: str, queue: asyncio.Queue
    ) -> None:
        failed_run = WorkflowExecutionResult(
            execution_successful=False, execution_details="broken file", status=WorkflowStatus.UNUSABLE
        )

        with patch.object(WorkflowRunner, "run_workflow", return_value=failed_run):
            result = _send(engine, OpenWorkflowRequest(workflow_name=saved_workflow_name)).result

        assert type(result) is OpenWorkflowResultFailure
        assert engine.open_workflows.all_open() == []
        assert _lifecycle_events(queue) == []


class TestOpenWorkflowAlreadyOpen:
    def test_returns_it_unchanged_and_announces_nothing(
        self, engine: Engine, saved_workflow_name: str, queue: asyncio.Queue
    ) -> None:
        workflow_id = _saved_workflow_id(engine, saved_workflow_name)
        _send(engine, OpenWorkflowRequest(workflow_id=workflow_id))
        workflow = engine.open_workflows.get(workflow_id)
        objects_before = set(workflow.objects.get_filtered_subset())
        _lifecycle_events(queue)

        result_event = _send(engine, OpenWorkflowRequest(workflow_id=workflow_id, create_flow=True))

        result = result_event.result
        assert isinstance(result, OpenWorkflowResultSuccess), result.result_details
        assert result.already_open
        assert result.open_workflow == engine.open_workflow_info(workflow)
        assert engine.open_workflows.all_open() == [workflow]
        assert set(workflow.objects.get_filtered_subset()) == objects_before
        assert _lifecycle_events(queue) == []
        assert result_event.open_workflow_id == workflow_id

    def test_an_unsaved_workflow_is_returned_by_its_id_without_a_flow_added(self, engine: Engine) -> None:
        opened = _send(engine, OpenWorkflowRequest(display_name="draft")).result
        assert isinstance(opened, OpenWorkflowResultSuccess)

        result = _send(engine, OpenWorkflowRequest(workflow_id=opened.open_workflow.workflow_id, create_flow=True))

        assert isinstance(result.result, OpenWorkflowResultSuccess)
        assert result.result.already_open
        assert not result.result.open_workflow.has_active_flow

    def test_closes_none_of_the_others_when_several_are_open(self, engine: Engine) -> None:
        _allow_several_open(engine)
        first = _open_new(engine, "first")
        second = _open_new(engine, "second")

        result = _send(engine, OpenWorkflowRequest(workflow_id=first.workflow_id)).result

        assert isinstance(result, OpenWorkflowResultSuccess)
        assert result.already_open
        assert engine.open_workflows.all_open() == [first, second]


class TestOpenWorkflowRefused:
    def test_another_open_workflow_refuses_it_and_names_that_workflow(
        self, engine: Engine, queue: asyncio.Queue
    ) -> None:
        open_workflow = _open_new(engine, "open")
        _lifecycle_events(queue)

        result_event = _send(engine, OpenWorkflowRequest(display_name="refused"), open_workflow.workflow_id)

        result = result_event.result
        assert isinstance(result, AnotherWorkflowOpenResultFailure)
        assert result.blocking_workflow == engine.open_workflow_info(open_workflow)
        assert engine.open_workflows.all_open() == [open_workflow]
        assert _lifecycle_events(queue) == []
        # Refused before it registered the new workflow.
        assert all(entry["name"] != "refused" for entry in engine.workflow_registry.list_workflows().values())
        # An open acts on no workflow, so the failure names none.
        assert result_event.open_workflow_id is None

    def test_an_id_no_workflow_has_is_not_found(self, engine: Engine) -> None:
        missing_id = uuid.uuid4().hex

        result = _send(engine, OpenWorkflowRequest(workflow_id=missing_id)).result

        assert isinstance(result, WorkflowNotFoundResultFailure)
        assert engine.open_workflows.all_open() == []
        assert engine.workflow_registry.find_by_id(missing_id) is None

    def test_a_name_no_workflow_has_is_not_found(self, engine: Engine) -> None:
        result = _send(engine, OpenWorkflowRequest(workflow_name="shots/sh010_typo")).result

        assert isinstance(result, WorkflowNotFoundResultFailure)
        assert engine.open_workflows.all_open() == []

    def test_the_unsaved_name_of_a_lost_workflow_is_not_found(self, engine: Engine) -> None:
        lost_name = f"unsaved:{uuid.uuid4().hex}"

        result = _send(engine, OpenWorkflowRequest(workflow_name=lost_name)).result

        assert isinstance(result, WorkflowNotFoundResultFailure)
        assert engine.workflow_registry.find_by_name(lost_name) is None

    def test_an_id_and_a_name_together_are_refused(self, engine: Engine, saved_workflow_name: str) -> None:
        result = _send(
            engine,
            OpenWorkflowRequest(
                workflow_id=_saved_workflow_id(engine, saved_workflow_name), workflow_name=saved_workflow_name
            ),
        ).result

        assert type(result) is OpenWorkflowResultFailure
        assert engine.open_workflows.all_open() == []


class TestOlderOpenRequestsShareTheCode:
    def test_ensure_naming_a_workflow_already_open_uses_it_and_creates_its_flow(self, engine: Engine) -> None:
        _allow_several_open(engine)
        opened = _send(engine, OpenWorkflowRequest(display_name="draft")).result
        assert isinstance(opened, OpenWorkflowResultSuccess)
        workflow_name = opened.open_workflow.name

        result = engine.handle_request(EnsureWorkflowAndFlowRequest(workflow_name=workflow_name))

        assert isinstance(result, EnsureWorkflowAndFlowResultSuccess), result.result_details
        assert not result.created_workflow
        assert result.created_flow
        assert result.open_workflow is None
        assert result.workflow_id == opened.open_workflow.workflow_id

    def test_a_clean_slate_registry_run_of_the_open_workflow_reloads_it_from_its_file(
        self, engine: Engine, saved_workflow_name: str
    ) -> None:
        first = _send(engine, RunWorkflowFromRegistryRequest(workflow_name=saved_workflow_name)).result
        assert isinstance(first, RunWorkflowFromRegistryResultSuccess), first.result_details
        assert first.workflow_id is not None
        before = engine.open_workflows.get(first.workflow_id)

        again = _send(engine, RunWorkflowFromRegistryRequest(workflow_name=saved_workflow_name)).result

        assert isinstance(again, RunWorkflowFromRegistryResultSuccess), again.result_details
        assert again.workflow_id == first.workflow_id
        assert before.closed
        assert engine.open_workflows.get(first.workflow_id) is not before
