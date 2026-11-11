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
    EnsureWorkflowAndFlowRequest,
    EnsureWorkflowAndFlowResultFailure,
    EnsureWorkflowAndFlowResultSuccess,
    ListOpenWorkflowsRequest,
    ListOpenWorkflowsResultSuccess,
    OpenWorkflowRequest,
    OpenWorkflowResultFailure,
    OpenWorkflowResultSuccess,
    SetWorkflowContextRequest,
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
from griptape_nodes.retained_mode.runtime.binding import NO_WORKFLOW
from griptape_nodes.servers.mcp import SUPPORTED_REQUEST_EVENTS

ORIGINAL_RUN_WORKFLOW = WorkflowRunner.run_workflow

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
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
        first = _open_new(engine, "first")
        second = _open_new(engine, "second")

        result = _send(engine, ListOpenWorkflowsRequest()).result

        assert isinstance(result, ListOpenWorkflowsResultSuccess)
        assert result.open_workflows == [engine.open_workflow_info(first), engine.open_workflow_info(second)]

    def test_leaves_out_a_workflow_still_opening(self, engine: Engine) -> None:
        """Clients hear of it once its build ends, and never if the build fails."""
        with engine.context_manager.opening_requested_workflow(display_name="building"):
            result = _send(engine, ListOpenWorkflowsRequest()).result

        assert isinstance(result, ListOpenWorkflowsResultSuccess)
        assert result.open_workflows == []

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
        assert result_event.open_workflow_id is None

    def test_creates_its_flow_when_asked(self, engine: Engine) -> None:
        result = _send(engine, OpenWorkflowRequest(create_flow=True)).result

        assert isinstance(result, OpenWorkflowResultSuccess), result.result_details
        assert result.open_workflow.has_active_flow
        assert engine.open_workflows.get(result.open_workflow.workflow_id).context.has_current_flow()

    def test_opens_beside_the_others_when_several_may_be_open(self, engine: Engine) -> None:
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
        assert result_event.open_workflow_id is None

    def test_an_unsaved_workflow_is_returned_by_its_id_without_a_flow_added(self, engine: Engine) -> None:
        opened = _send(engine, OpenWorkflowRequest(display_name="draft")).result
        assert isinstance(opened, OpenWorkflowResultSuccess)

        result = _send(engine, OpenWorkflowRequest(workflow_id=opened.open_workflow.workflow_id, create_flow=True))

        assert isinstance(result.result, OpenWorkflowResultSuccess)
        assert result.result.already_open
        assert not result.result.open_workflow.has_active_flow

    def test_arguments_for_a_new_workflow_are_not_checked(self, engine: Engine, tmp_path: Path) -> None:
        opened = _send(engine, OpenWorkflowRequest(display_name="draft")).result
        assert isinstance(opened, OpenWorkflowResultSuccess)
        not_a_folder = tmp_path / "file.txt"
        not_a_folder.write_text("x")

        result = _send(
            engine,
            OpenWorkflowRequest(workflow_id=opened.open_workflow.workflow_id, working_directory=str(not_a_folder)),
        ).result

        assert isinstance(result, OpenWorkflowResultSuccess), result.result_details
        assert result.already_open

    def test_closes_none_of_the_others_when_several_are_open(self, engine: Engine) -> None:
        first = _open_new(engine, "first")
        second = _open_new(engine, "second")

        result = _send(engine, OpenWorkflowRequest(workflow_id=first.workflow_id)).result

        assert isinstance(result, OpenWorkflowResultSuccess)
        assert result.already_open
        assert engine.open_workflows.all_open() == [first, second]


class TestOpenWorkflowRefused:
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
        opened = _send(engine, OpenWorkflowRequest(display_name="draft")).result
        assert isinstance(opened, OpenWorkflowResultSuccess)
        workflow_name = opened.open_workflow.name

        # Bound to no workflow, so Ensure opens by the name rather than using the caller's workflow.
        with engine.bind_workflow(NO_WORKFLOW):
            result = engine.handle_request(EnsureWorkflowAndFlowRequest(workflow_name=workflow_name))

        assert isinstance(result, EnsureWorkflowAndFlowResultSuccess), result.result_details
        assert not result.created_workflow
        assert result.created_flow
        assert result.open_workflow is not None
        assert result.open_workflow.workflow_id == result.workflow_id == opened.open_workflow.workflow_id

    def test_ensure_refuses_a_workflow_another_request_is_still_opening(self, engine: Engine) -> None:
        with engine.context_manager.opening_requested_workflow(display_name="building") as opened:
            workflow_name = engine.workflow_registry.get(opened.workflow.workflow_id).name

            with engine.bind_workflow(NO_WORKFLOW):
                result = engine.handle_request(EnsureWorkflowAndFlowRequest(workflow_name=workflow_name))

            assert isinstance(result, EnsureWorkflowAndFlowResultFailure)
            assert not opened.workflow.context.has_current_flow()

    @pytest.mark.parametrize(
        "make_request",
        [
            lambda name: EnsureWorkflowAndFlowRequest(workflow_name=name),
            lambda name: SetWorkflowContextRequest(workflow_name=name),
        ],
        ids=["ensure", "set_context"],
    )
    def test_refuses_a_saved_workflow_that_is_not_open(
        self, engine: Engine, saved_workflow_name: str, make_request: Callable[[str], RequestPayload]
    ) -> None:
        """Neither builds the file, so the workflow would show empty and its next save would erase the file."""
        result = engine.handle_request(make_request(saved_workflow_name))

        assert result.failed()
        assert "OpenWorkflowRequest" in str(result.result_details)
        assert engine.open_workflows.all_open() == []


def _clean_slate_run(
    engine: Engine, saved_workflow_name: str, request_type: str
) -> EventResultSuccess | EventResultFailure:
    """Run the saved workflow with a clean slate, from the registry or from scratch."""
    request: RequestPayload = RunWorkflowFromRegistryRequest(workflow_name=saved_workflow_name)
    if request_type == "scratch":
        file_path = engine.workflow_registry.get_workflow_by_name(saved_workflow_name).file_path
        assert file_path is not None
        request = RunWorkflowFromScratchRequest(file_path=file_path)
    return _send(engine, request)


@pytest.mark.parametrize("request_type", ["registry", "scratch"])
class TestACleanSlateStartsTheWorkflowOver:
    """A clean slate run of an open workflow reloads it from its file."""

    def test_a_clean_slate_run_of_the_open_workflow_reloads_it_from_its_file(
        self, engine: Engine, request_type: str, saved_workflow_name: str
    ) -> None:
        first = _clean_slate_run(engine, saved_workflow_name, request_type).result
        assert first.succeeded(), first.result_details
        assert isinstance(first, (RunWorkflowFromRegistryResultSuccess, RunWorkflowFromScratchResultSuccess))
        assert first.workflow_id is not None
        before = engine.open_workflows.get(first.workflow_id)

        again = _clean_slate_run(engine, saved_workflow_name, request_type).result

        assert again.succeeded(), again.result_details
        assert isinstance(again, (RunWorkflowFromRegistryResultSuccess, RunWorkflowFromScratchResultSuccess))
        assert again.workflow_id == first.workflow_id
        assert before.closed
        assert [workflow.workflow_id for workflow in engine.open_workflows.all_open()] == [first.workflow_id]
        assert engine.open_workflows.get(first.workflow_id) is not before

    def test_a_clean_slate_run_of_a_library_workflow_replaces_its_open_copy(
        self, engine: Engine, request_type: str, saved_workflow_name: str
    ) -> None:
        engine.workflow_registry.get_workflow_by_name(saved_workflow_name).metadata.is_griptape_provided = True
        first = _clean_slate_run(engine, saved_workflow_name, request_type).result
        assert isinstance(first, (RunWorkflowFromRegistryResultSuccess, RunWorkflowFromScratchResultSuccess)), (
            first.result_details
        )
        assert first.workflow_id is not None
        assert first.workflow_id != _saved_workflow_id(engine, saved_workflow_name)
        first_copy = engine.open_workflows.get(first.workflow_id)

        again = _clean_slate_run(engine, saved_workflow_name, request_type).result

        assert isinstance(again, (RunWorkflowFromRegistryResultSuccess, RunWorkflowFromScratchResultSuccess)), (
            again.result_details
        )
        assert first_copy.closed
        assert [workflow.workflow_id for workflow in engine.open_workflows.all_open()] == [again.workflow_id]

    def test_a_clean_slate_run_of_a_library_workflow_leaves_a_copy_a_tab_opened(
        self, engine: Engine, request_type: str, saved_workflow_name: str
    ) -> None:
        engine.workflow_registry.get_workflow_by_name(saved_workflow_name).metadata.is_griptape_provided = True
        tab = _send(engine, OpenWorkflowRequest(workflow_name=saved_workflow_name)).result
        assert isinstance(tab, OpenWorkflowResultSuccess), tab.result_details
        tab_copy = engine.open_workflows.get(tab.open_workflow.workflow_id)

        run = _clean_slate_run(engine, saved_workflow_name, request_type).result

        assert run.succeeded(), run.result_details
        assert not tab_copy.closed

    def test_a_clean_slate_run_of_a_library_workflow_leaves_its_copy_once_saved(
        self, engine: Engine, request_type: str, saved_workflow_name: str
    ) -> None:
        engine.workflow_registry.get_workflow_by_name(saved_workflow_name).metadata.is_griptape_provided = True
        first = _clean_slate_run(engine, saved_workflow_name, request_type).result
        assert isinstance(first, (RunWorkflowFromRegistryResultSuccess, RunWorkflowFromScratchResultSuccess)), (
            first.result_details
        )
        assert first.workflow_id is not None
        first_copy = engine.open_workflows.get(first.workflow_id)
        saved = _send(engine, SaveWorkflowRequest(), first.workflow_id).result
        assert isinstance(saved, SaveWorkflowResultSuccess), saved.result_details

        again = _clean_slate_run(engine, saved_workflow_name, request_type).result

        assert again.succeeded(), again.result_details
        assert not first_copy.closed

    def test_it_leaves_the_other_workflows_open(
        self, engine: Engine, saved_workflow_name: str, request_type: str
    ) -> None:
        first = _clean_slate_run(engine, saved_workflow_name, request_type).result
        assert isinstance(first, (RunWorkflowFromRegistryResultSuccess, RunWorkflowFromScratchResultSuccess))
        assert first.workflow_id is not None
        before = engine.open_workflows.get(first.workflow_id)
        other = _open_new(engine, "other")

        again = _clean_slate_run(engine, saved_workflow_name, request_type).result

        assert again.succeeded(), again.result_details
        assert before.closed
        assert not other.closed
        assert [workflow.workflow_id for workflow in engine.open_workflows.all_open()] == [
            other.workflow_id,
            first.workflow_id,
        ]


class TestOlderOpensOpenBesideTheOthers:
    """The requests that opened a workflow before `OpenWorkflowRequest` open it beside the open ones."""

    @pytest.mark.parametrize("run_with_clean_slate", [True, False])
    def test_a_registry_run_opens_beside_the_open_workflow(
        self, engine: Engine, saved_workflow_name: str, *, run_with_clean_slate: bool
    ) -> None:
        open_workflow = _open_new(engine, "open")

        result = _send(
            engine,
            RunWorkflowFromRegistryRequest(
                workflow_name=saved_workflow_name, run_with_clean_slate=run_with_clean_slate
            ),
        ).result

        assert isinstance(result, RunWorkflowFromRegistryResultSuccess), result.result_details
        assert not open_workflow.closed
        assert [workflow.workflow_id for workflow in engine.open_workflows.all_open()] == [
            open_workflow.workflow_id,
            result.workflow_id,
        ]

    def test_a_run_from_scratch_opens_beside_the_open_workflow(self, engine: Engine, saved_workflow_name: str) -> None:
        saved_file = engine.workflow_registry.get_workflow_by_name(saved_workflow_name).file_path
        assert saved_file is not None
        open_workflow = _open_new(engine, "open")

        result = _send(engine, RunWorkflowFromScratchRequest(file_path=saved_file)).result

        assert isinstance(result, RunWorkflowFromScratchResultSuccess), result.result_details
        assert not open_workflow.closed
        assert [workflow.workflow_id for workflow in engine.open_workflows.all_open()] == [
            open_workflow.workflow_id,
            result.workflow_id,
        ]


class TestOpenWorkflowWhileAnotherRequestOpensIt:
    """A second open of a workflow still building waits for the build instead of returning an empty graph."""

    @staticmethod
    def _gated_run(gate: asyncio.Event, entered: asyncio.Event, run: WorkflowExecutionResult | None) -> object:
        """`run_workflow` that holds each build into an open workflow until `gate` is set, then runs it or returns `run`."""

        async def gated(runner: WorkflowRunner, relative_file_path: str, workflow: WorkflowRuntime | None) -> object:
            # With None, run_workflow opens the file first and comes back here with the workflow.
            if workflow is None:
                return await ORIGINAL_RUN_WORKFLOW(runner, relative_file_path=relative_file_path, workflow=None)
            entered.set()
            await gate.wait()
            if run is not None:
                return run
            return await ORIGINAL_RUN_WORKFLOW(runner, relative_file_path=relative_file_path, workflow=workflow)

        return gated

    @staticmethod
    async def _race(
        engine: Engine,
        first_request: RequestPayload,
        second_request: RequestPayload,
        *,
        run: WorkflowExecutionResult | None = None,
        cancel_first: bool = False,
    ) -> list[EventResultSuccess | EventResultFailure | BaseException]:
        """Send `first_request`, then `second_request` while the first is building, then let the build end or cancel it."""
        gate = asyncio.Event()
        entered = asyncio.Event()
        gated = TestOpenWorkflowWhileAnotherRequestOpensIt._gated_run(gate, entered, run)
        with patch.object(WorkflowRunner, "run_workflow", gated):
            first = asyncio.create_task(
                engine.event_manager.ahandle_request(first_request, result_context=ResultContext())
            )
            await asyncio.wait_for(entered.wait(), 5)
            second = asyncio.create_task(
                engine.event_manager.ahandle_request(second_request, result_context=ResultContext())
            )
            await asyncio.sleep(0.05)
            assert not second.done()
            if cancel_first:
                first.cancel()
            else:
                gate.set()
            return list(await asyncio.gather(first, second, return_exceptions=True))

    def test_the_second_open_returns_the_workflow_once_built_and_it_is_announced_once(
        self, engine: Engine, saved_workflow_name: str, queue: asyncio.Queue
    ) -> None:
        request = OpenWorkflowRequest(workflow_id=_saved_workflow_id(engine, saved_workflow_name))

        first, second = asyncio.run(self._race(engine, request, request))

        assert isinstance(first, EventResultSuccess)
        assert isinstance(second, EventResultSuccess)
        assert isinstance(first.result, OpenWorkflowResultSuccess), first.result.result_details
        assert isinstance(second.result, OpenWorkflowResultSuccess), second.result.result_details
        assert not first.result.already_open
        assert second.result.already_open
        assert second.result.open_workflow.has_active_flow
        assert second.result.open_workflow == first.result.open_workflow
        assert _lifecycle_events(queue) == [WorkflowOpenedEvent(open_workflow=first.result.open_workflow)]

    def test_the_second_open_fails_when_the_build_fails_and_nothing_is_announced(
        self, engine: Engine, saved_workflow_name: str, queue: asyncio.Queue
    ) -> None:
        request = OpenWorkflowRequest(workflow_id=_saved_workflow_id(engine, saved_workflow_name))
        failed_run = WorkflowExecutionResult(
            execution_successful=False, execution_details="broken file", status=WorkflowStatus.UNUSABLE
        )

        first, second = asyncio.run(self._race(engine, request, request, run=failed_run))

        assert isinstance(first, EventResultFailure)
        assert isinstance(second, EventResultFailure)
        assert type(first.result) is OpenWorkflowResultFailure
        assert type(second.result) is OpenWorkflowResultFailure
        assert "did not finish building it" in str(second.result.result_details)
        assert engine.open_workflows.all_open() == []
        assert _lifecycle_events(queue) == []

    def test_a_cancelled_build_closes_the_workflow_before_the_second_open_stops_waiting(
        self, engine: Engine, saved_workflow_name: str, queue: asyncio.Queue
    ) -> None:
        request = OpenWorkflowRequest(workflow_id=_saved_workflow_id(engine, saved_workflow_name))

        first, second = asyncio.run(self._race(engine, request, request, cancel_first=True))

        assert isinstance(first, asyncio.CancelledError)
        assert isinstance(second, EventResultFailure)
        assert type(second.result) is OpenWorkflowResultFailure
        assert engine.open_workflows.all_open() == []
        assert _lifecycle_events(queue) == []

    def test_an_open_waits_for_a_run_with_current_state_that_is_opening_the_file(
        self, engine: Engine, saved_workflow_name: str, queue: asyncio.Queue
    ) -> None:
        entry = engine.workflow_registry.get_workflow_by_name(saved_workflow_name)
        assert entry.file_path is not None
        run_request = RunWorkflowWithCurrentStateRequest(file_path=entry.file_path)
        open_request = OpenWorkflowRequest(workflow_id=entry.workflow_id)

        first, second = asyncio.run(self._race(engine, run_request, open_request))

        assert isinstance(first, EventResultSuccess)
        assert isinstance(second, EventResultSuccess)
        assert isinstance(first.result, RunWorkflowWithCurrentStateResultSuccess), first.result.result_details
        assert isinstance(second.result, OpenWorkflowResultSuccess), second.result.result_details
        assert second.result.already_open
        assert second.result.open_workflow.workflow_id == entry.workflow_id
        assert second.result.open_workflow.has_active_flow
        assert _lifecycle_events(queue) == [WorkflowOpenedEvent(open_workflow=second.result.open_workflow)]
