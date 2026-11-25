"""An open workflow's details, as `OpenWorkflowInfo` reports them and `WorkflowDetailsChangedEvent` announces."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
from griptape_nodes.retained_mode.events.app_events import WorkflowDetailsChangedEvent
from griptape_nodes.retained_mode.events.base_events import AppEvent
from griptape_nodes.retained_mode.events.context_events import (
    EnsureWorkflowAndFlowRequest,
    EnsureWorkflowAndFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import SetFlowMetadataRequest
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.project_events import (
    LoadProjectTemplateRequest,
    LoadProjectTemplateResultSuccess,
    SetCurrentProjectRequest,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    RenameWorkflowRequest,
    RenameWorkflowResultSuccess,
    SaveWorkflowRequest,
    SaveWorkflowResultSuccess,
    SetWorkflowMetadataRequest,
    SetWorkflowMetadataResultSuccess,
)
from griptape_nodes.retained_mode.managers.event_manager import ResultContext

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


def _details_events(queue: asyncio.Queue) -> list[WorkflowDetailsChangedEvent]:
    """The details changes clients were told of since the last call, in order."""
    events = []
    while not queue.empty():
        event = queue.get_nowait()
        if isinstance(event, AppEvent) and isinstance(event.payload, WorkflowDetailsChangedEvent):
            events.append(event.payload)
    return events


def _open(engine: Engine, display_name: str) -> WorkflowRuntime:
    result = engine.handle_request(EnsureWorkflowAndFlowRequest(display_name=display_name))
    assert isinstance(result, EnsureWorkflowAndFlowResultSuccess), result.result_details
    assert result.workflow_id is not None
    return engine.open_workflows.get(result.workflow_id)


async def _send_async(engine: Engine, request: Any, workflow: WorkflowRuntime) -> Any:
    """Dispatch `request` as a client would, naming `workflow` on its envelope, and return its result."""
    context = ResultContext(open_workflow_id=workflow.workflow_id)
    return (await engine.event_manager.ahandle_request(request, result_context=context)).result


def _set_metadata(engine: Engine, workflow: WorkflowRuntime, **metadata: Any) -> None:
    """Change `workflow`'s metadata the way the editor does, naming it in the request, not the envelope."""
    current = engine.workflow_registry.get(workflow.workflow_id).metadata
    request = SetWorkflowMetadataRequest(
        workflow_id=workflow.workflow_id, workflow_metadata=current.model_copy(update=metadata)
    )
    result = asyncio.run(engine.ahandle_request(request))
    assert isinstance(result, SetWorkflowMetadataResultSuccess), result.result_details


class TestOpenWorkflowInfo:
    def test_a_workflow_with_no_details_reports_none(self, engine: Engine) -> None:
        info = engine.open_workflow_info(_open(engine, "plain"))

        assert info.description is None
        assert info.image is None
        assert info.is_template is False

    def test_reports_the_description_thumbnail_and_template_flag(self, engine: Engine) -> None:
        workflow = _open(engine, "detailed")

        _set_metadata(engine, workflow, description="Makes a cat", image="cat.png", is_template=True)

        info = engine.open_workflow_info(workflow)
        assert info.description == "Makes a cat"
        assert info.image == "cat.png"
        assert info.is_template is True


class TestDetailsChangedEvent:
    def test_opening_a_workflow_sends_none(self, engine: Engine, queue: asyncio.Queue) -> None:
        _open(engine, "fresh")

        assert _details_events(queue) == []

    def test_setting_its_metadata_tells_clients_once(self, engine: Engine, queue: asyncio.Queue) -> None:
        workflow = _open(engine, "templated")
        _details_events(queue)

        _set_metadata(engine, workflow, description="Makes a cat", image="cat.png", is_template=True)
        _set_metadata(engine, workflow, description="Makes a cat", image="cat.png", is_template=True)

        assert _details_events(queue) == [
            WorkflowDetailsChangedEvent(open_workflow=engine.open_workflow_info(workflow))
        ]

    def test_an_edit_that_only_changes_its_state_sends_none(self, engine: Engine, queue: asyncio.Queue) -> None:
        workflow = _open(engine, "edited")
        _details_events(queue)

        context = ResultContext(open_workflow_id=workflow.workflow_id)
        engine.event_manager.handle_request(SetFlowMetadataRequest(metadata={"edit": 1}), result_context=context)

        assert workflow.has_unsaved_changes
        assert _details_events(queue) == []

    @pytest.mark.usefixtures("workspace")
    def test_saving_it_to_a_file_tells_clients(self, engine: Engine, queue: asyncio.Queue) -> None:
        workflow = _open(engine, "unsaved")
        _details_events(queue)

        result = asyncio.run(_send_async(engine, SaveWorkflowRequest(), workflow))

        assert isinstance(result, SaveWorkflowResultSuccess), result.result_details
        (event,) = _details_events(queue)
        assert event.open_workflow.workflow_id == workflow.workflow_id
        assert event.open_workflow.is_saved
        assert event.open_workflow.workflow_file_path == result.file_path

    @pytest.mark.usefixtures("workspace")
    def test_renaming_it_tells_clients(self, engine: Engine, queue: asyncio.Queue) -> None:
        workflow = _open(engine, "before")
        asyncio.run(_send_async(engine, SaveWorkflowRequest(), workflow))
        _details_events(queue)

        result = asyncio.run(
            engine.ahandle_request(RenameWorkflowRequest(workflow_id=workflow.workflow_id, requested_name="after"))
        )

        assert isinstance(result, RenameWorkflowResultSuccess), result.result_details
        events = _details_events(queue)
        assert events
        assert events[-1].open_workflow == engine.open_workflow_info(workflow)
        assert events[-1].open_workflow.display_name == "after"
