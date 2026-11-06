"""`ListOpenWorkflowsRequest`, `OpenWorkflowRequest`, and the older open requests that share its code."""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING

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
    EnsureWorkflowAndFlowRequest,
    EnsureWorkflowAndFlowResultSuccess,
    ListOpenWorkflowsRequest,
    ListOpenWorkflowsResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.project_events import (
    LoadProjectTemplateRequest,
    LoadProjectTemplateResultSuccess,
    SetCurrentProjectRequest,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    SaveWorkflowRequest,
    SaveWorkflowResultSuccess,
)
from griptape_nodes.retained_mode.managers.event_manager import ResultContext
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
    """Open a new unsaved workflow with its flow, beside any that are open."""
    workflow = engine.open_workflow(engine.workflow_registry.register_unsaved(display_name).workflow_id)
    result = workflow.handle_request(CreateFlowRequest(parent_flow_name=None, set_as_new_context=True))
    assert result.succeeded(), result.result_details
    return workflow


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
