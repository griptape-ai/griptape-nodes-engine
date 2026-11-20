"""Switching projects with workflows open: the project applies to the whole engine, so a switch closes them."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest

from griptape_nodes.retained_mode.events.app_events import WorkflowClosedEvent
from griptape_nodes.retained_mode.events.base_events import AppEvent
from griptape_nodes.retained_mode.events.context_events import (
    EnsureWorkflowAndFlowRequest,
    EnsureWorkflowAndFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.execution_events import StartFlowResultFailure, StartFlowResultSuccess
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.project_events import (
    LoadProjectTemplateRequest,
    LoadProjectTemplateResultSuccess,
    SetCurrentProjectRequest,
    SetCurrentProjectResultFailure,
    SetCurrentProjectResultSuccess,
)
from griptape_nodes.retained_mode.managers.run_queue import RunSpec

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime


@dataclass
class _Projects:
    first: str
    second: str
    workspace: Path


@pytest.fixture
def projects(engine: Engine, tmp_path: Path) -> Iterator[_Projects]:
    """Two projects sharing one workspace, the first of them current, with startup complete."""
    workspace = tmp_path.resolve() / "workspace"
    workspace.mkdir()
    ids = []
    for name in ("first", "second"):
        project_dir = tmp_path / name
        project_dir.mkdir()
        project_yml = project_dir / "project_template.yml"
        # JSON is valid YAML, and it escapes the backslashes in a Windows path.
        template = {"project_template_schema_version": "1.0.0", "name": name, "workspace_dir": str(workspace)}
        project_yml.write_text(json.dumps(template))
        loaded = engine.handle_request(LoadProjectTemplateRequest(project_path=project_yml))
        assert isinstance(loaded, LoadProjectTemplateResultSuccess), loaded.result_details
        ids.append(loaded.project_id)
    switched = engine.handle_request(SetCurrentProjectRequest(project_id=ids[0]))
    assert isinstance(switched, SetCurrentProjectResultSuccess), switched.result_details
    engine.project_manager._initialization_complete = True
    yield _Projects(first=ids[0], second=ids[1], workspace=workspace)
    engine.project_manager._initialization_complete = False
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    engine.handle_request(SetCurrentProjectRequest(project_id=None))


@pytest.fixture
def queue(engine: Engine) -> asyncio.Queue:
    """The engine's outgoing event queue, which clients read."""
    events: asyncio.Queue = asyncio.Queue()
    engine.event_manager.initialize_queue(events)
    return events


def _open(engine: Engine, display_name: str) -> WorkflowRuntime:
    result = engine.handle_request(EnsureWorkflowAndFlowRequest(display_name=display_name))
    assert isinstance(result, EnsureWorkflowAndFlowResultSuccess), result.result_details
    assert result.workflow_id is not None
    return engine.open_workflows.get(result.workflow_id)


def _closed_ids(queue: asyncio.Queue) -> list[str]:
    closed = []
    while not queue.empty():
        event = queue.get_nowait()
        if isinstance(event, AppEvent) and isinstance(event.payload, WorkflowClosedEvent):
            closed.append(event.payload.workflow_id)
    return closed


@dataclass
class _Spec(RunSpec):
    flow_name: str
    body: Callable[[], Awaitable[Any]]

    async def execute(self, engine: Engine) -> Any:  # noqa: ARG002
        return await self.body()

    def failure(self, details: str) -> Any:
        return StartFlowResultFailure(validation_exceptions=[], result_details=details)


class TestProjectSwitchWithOpenWorkflows:
    def test_switching_to_another_project_closes_every_open_workflow(
        self, engine: Engine, projects: _Projects, queue: asyncio.Queue
    ) -> None:
        shown = _open(engine, "shown tab")
        other = _open(engine, "other tab")
        _closed_ids(queue)

        result = asyncio.run(engine.ahandle_request(SetCurrentProjectRequest(project_id=projects.second)))

        assert isinstance(result, SetCurrentProjectResultSuccess), result.result_details
        assert result.altered_workflow_state
        assert engine.config_manager.workspace_path == projects.workspace
        assert engine.open_workflows.all_open() == []
        assert sorted(_closed_ids(queue)) == sorted([shown.workflow_id, other.workflow_id])

    def test_setting_the_current_project_again_leaves_them_open(self, engine: Engine, projects: _Projects) -> None:
        workflow = _open(engine, "tab")

        result = asyncio.run(engine.ahandle_request(SetCurrentProjectRequest(project_id=projects.first)))

        assert isinstance(result, SetCurrentProjectResultSuccess), result.result_details
        assert not result.altered_workflow_state
        assert engine.open_workflows.all_open() == [workflow]

    @pytest.mark.asyncio
    async def test_a_switch_while_a_workflow_runs_fails_and_changes_nothing(
        self, engine: Engine, projects: _Projects
    ) -> None:
        running = _open(engine, "rendering shot")
        idle = _open(engine, "idle tab")
        run_may_end = asyncio.Event()

        async def body() -> Any:
            await run_may_end.wait()
            return StartFlowResultSuccess(result_details="ran")

        with engine.bind_workflow(running):
            run = engine.flow_manager.submit_run(_Spec("flow", body), flows=running.flows)

        result = await engine.ahandle_request(SetCurrentProjectRequest(project_id=projects.second))

        run_may_end.set()
        await asyncio.wait_for(run.ended, timeout=1.0)
        assert isinstance(result, SetCurrentProjectResultFailure)
        assert "'rendering shot' is still running" in str(result.result_details)
        assert not result.altered_workflow_state
        assert engine.project_manager._current_project_id == projects.first
        assert engine.open_workflows.all_open() == [running, idle]
