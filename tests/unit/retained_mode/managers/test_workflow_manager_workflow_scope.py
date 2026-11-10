"""Workflow managers' handlers act on the workflow dispatch hands them, or on the saved workflow they name.

The request envelope names the tab's open workflow; a body `workflow_id` or `workflow_name` names the
saved workflow a request acts on. Most tests open a second workflow beside the first, so a handler that fell back to
"the only open workflow" would fail instead of passing by luck.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
from griptape_nodes.node_library.workflow_registry import WorkflowMetadata, WorkflowShape, read_workflow_metadata
from griptape_nodes.retained_mode.events.base_events import ResultDetails
from griptape_nodes.retained_mode.events.context_events import (
    CloseWorkflowRequest,
    EnsureWorkflowAndFlowRequest,
    EnsureWorkflowAndFlowResultSuccess,
    GetWorkflowContextRequest,
)
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    GetTopLevelFlowRequest,
    GetTopLevelFlowResultSuccess,
    ImportWorkflowAsReferencedSubFlowRequest,
    SerializedFlowCommands,
    SerializeFlowToCommandsRequest,
    SerializeFlowToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.generic_events import WorkflowNotNamedResultFailure
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.os_events import (
    DeleteFileRequest,
    DeleteFileResultFailure,
    FileIOFailureReason,
)
from griptape_nodes.retained_mode.events.project_events import (
    LoadProjectTemplateRequest,
    LoadProjectTemplateResultSuccess,
    SetCurrentProjectRequest,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    BranchWorkflowRequest,
    BranchWorkflowResultFailure,
    BranchWorkflowResultSuccess,
    CompareWorkflowsRequest,
    CompareWorkflowsResultFailure,
    CompareWorkflowsResultSuccess,
    DeleteWorkflowRequest,
    DeleteWorkflowResultFailure,
    DeleteWorkflowResultSuccess,
    GetPublishOptionsRequest,
    GetPublishOptionsResultSuccess,
    GetVariableSubstitutionEnabledRequest,
    GetWorkflowInfoRequest,
    GetWorkflowInfoResultFailure,
    GetWorkflowInfoResultSuccess,
    GetWorkflowMetadataRequest,
    GetWorkflowMetadataResultFailure,
    GetWorkflowMetadataResultSuccess,
    GetWorkflowRunCommandRequest,
    GetWorkflowRunCommandResultFailure,
    GetWorkflowRunCommandResultSuccess,
    GetWorkflowShapeRequest,
    GetWorkflowShapeResultFailure,
    ImportWorkflowAsReferencedSubFlowResultFailure,
    ImportWorkflowAsReferencedSubFlowResultSuccess,
    MergeWorkflowBranchRequest,
    MergeWorkflowBranchResultFailure,
    MergeWorkflowBranchResultSuccess,
    MoveWorkflowRequest,
    MoveWorkflowResultFailure,
    MoveWorkflowResultSuccess,
    PublishWorkflowRegisteredEventData,
    PublishWorkflowRequest,
    PublishWorkflowResultFailure,
    PublishWorkflowResultSuccess,
    RenameDisplayNameBehavior,
    RenameWorkflowRequest,
    RenameWorkflowResultFailure,
    RenameWorkflowResultSuccess,
    ResetWorkflowBranchRequest,
    ResetWorkflowBranchResultFailure,
    ResetWorkflowBranchResultSuccess,
    RunWorkflowWithCurrentStateRequest,
    SaveSubflowToWorkflowRequest,
    SaveSubflowToWorkflowResultSuccess,
    SaveWorkflowFileFromSerializedFlowResultSuccess,
    SaveWorkflowRequest,
    SaveWorkflowResultFailure,
    SaveWorkflowResultSuccess,
    SetVariableSubstitutionEnabledRequest,
    SetWorkflowMetadataRequest,
    SetWorkflowMetadataResultFailure,
    SetWorkflowMetadataResultSuccess,
    WorkflowStatus,
)
from griptape_nodes.retained_mode.managers.context_manager import ContextManager
from griptape_nodes.retained_mode.managers.event_manager import ResultContext
from griptape_nodes.retained_mode.managers.library.common import RegisteredEventHandler
from griptape_nodes.retained_mode.managers.workflow.branching import WorkflowBranching
from griptape_nodes.retained_mode.managers.workflow.catalog import WorkflowCatalog
from griptape_nodes.retained_mode.managers.workflow.file_operations import WorkflowFileOperations
from griptape_nodes.retained_mode.managers.workflow.publishing import WorkflowPublishing
from griptape_nodes.retained_mode.managers.workflow.referenced_import import ReferencedWorkflowImport
from griptape_nodes.retained_mode.managers.workflow.running import WorkflowExecutionResult, WorkflowRunner
from griptape_nodes.retained_mode.managers.workflow.saving import WorkflowSaver
from griptape_nodes.retained_mode.managers.workflow.variable_substitution import VariableSubstitution
from griptape_nodes.retained_mode.managers.workflow_manager import WorkflowManager
from griptape_nodes.retained_mode.request_handlers import RequestScope, handled_request_types
from griptape_nodes.retained_mode.runtime.binding import bound_workflow
from griptape_nodes.retained_mode.runtime.open_workflow_set import OpenWorkflowSet

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from griptape_nodes.node_library.workflow_registry import Workflow
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

_HANDLER_CLASSES = (
    ContextManager,
    WorkflowManager,
    WorkflowBranching,
    WorkflowCatalog,
    WorkflowFileOperations,
    WorkflowPublishing,
    ReferencedWorkflowImport,
    WorkflowRunner,
    WorkflowSaver,
    VariableSubstitution,
)
# They act on the tab's open workflow: they read or change its graph.
_WORKFLOW_REQUESTS = {
    SaveWorkflowRequest,
    SaveSubflowToWorkflowRequest,
    ImportWorkflowAsReferencedSubFlowRequest,
    GetVariableSubstitutionEnabledRequest,
    SetVariableSubstitutionEnabledRequest,
    PublishWorkflowRequest,
    GetWorkflowShapeRequest,
}
# They use the tab's open workflow when there is one and work without it.
_OPTIONAL_WORKFLOW_REQUESTS = {
    EnsureWorkflowAndFlowRequest,
    GetPublishOptionsRequest,
    GetWorkflowRunCommandRequest,
    GetWorkflowContextRequest,
    RunWorkflowWithCurrentStateRequest,
}


def _allow_several_open(engine: Engine) -> None:
    engine._open_workflows = OpenWorkflowSet(engine, allow_several_open=True)


def _open(engine: Engine, display_name: str) -> WorkflowRuntime:
    """Open a new unsaved workflow next to whatever is open, as an editor tab would."""
    workflow = engine.workflow_registry.register_unsaved(display_name, listed=False)
    return engine.open_workflow(workflow.workflow_id)


def _name(engine: Engine, workflow: WorkflowRuntime) -> str:
    return engine.workflow_registry.get(workflow.workflow_id).name


def _send(engine: Engine, request: RequestPayload, workflow: WorkflowRuntime | None) -> ResultPayload:
    """Dispatch `request` as a client would, with the envelope naming `workflow` (or nothing)."""
    workflow_id = None if workflow is None else workflow.workflow_id
    return engine.event_manager.handle_request(
        request, result_context=ResultContext(open_workflow_id=workflow_id)
    ).result


def _send_async(engine: Engine, request: RequestPayload, workflow: WorkflowRuntime | None) -> ResultPayload:
    workflow_id = None if workflow is None else workflow.workflow_id
    return asyncio.run(
        engine.event_manager.ahandle_request(request, result_context=ResultContext(open_workflow_id=workflow_id))
    ).result


def _create_flow(engine: Engine, workflow: WorkflowRuntime, flow_name: str) -> str:
    result = _send(engine, CreateFlowRequest(parent_flow_name=None, flow_name=flow_name), workflow)
    assert isinstance(result, CreateFlowResultSuccess), result.result_details
    return result.flow_name


def _open_with_flow(engine: Engine, display_name: str) -> WorkflowRuntime:
    """Open a new unsaved workflow with a top-level flow, so it can be saved."""
    ensured = engine.handle_request(EnsureWorkflowAndFlowRequest(display_name=display_name))
    assert isinstance(ensured, EnsureWorkflowAndFlowResultSuccess), ensured.result_details
    assert ensured.workflow_id is not None
    return engine.open_workflows.get(ensured.workflow_id)


class _SavedWorkflow(NamedTuple):
    workflow_id: str
    workflow_name: str
    file_path: Path


def _save_and_close(engine: Engine, display_name: str) -> _SavedWorkflow:
    """Create, save and close a workflow."""
    workflow = _open_with_flow(engine, display_name)
    saved = _send_async(engine, SaveWorkflowRequest(), workflow)
    assert isinstance(saved, SaveWorkflowResultSuccess), saved.result_details
    _send_async(engine, CloseWorkflowRequest(workflow_id=workflow.workflow_id), None)
    return _SavedWorkflow(
        workflow_id=workflow.workflow_id, workflow_name=saved.workflow_name, file_path=Path(saved.file_path)
    )


def _body(path: Path) -> str:
    """A workflow file's content after its metadata header."""
    return path.read_text(encoding="utf-8").split("\n# ///\n", 1)[1]


def _write_saved_workflow(engine: Engine, path: Path, *, workflow_shape: WorkflowShape | None = None) -> Workflow:
    """Register a saved workflow whose file holds only a placeholder body."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# a saved workflow\n", encoding="utf-8")
    metadata = WorkflowMetadata(
        name=path.stem,
        schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
        engine_version_created_with="0.0.0",
        node_libraries_referenced=[],
        workflow_shape=workflow_shape,
    )
    return engine.workflow_registry.register_file(path, metadata)


class _RecordedRequests:
    """Stands in for `engine.ahandle_request`, answering flow requests and recording the workflow each is bound to."""

    def __init__(self, flow_name: str) -> None:
        self.flow_name = flow_name
        self.bound: list[tuple[type, Any]] = []

    async def __call__(self, request: RequestPayload) -> ResultPayload:
        self.bound.append((type(request), bound_workflow.get()))
        if isinstance(request, GetTopLevelFlowRequest):
            return GetTopLevelFlowResultSuccess(flow_name=self.flow_name, result_details="ok")
        if isinstance(request, SerializeFlowToCommandsRequest):
            commands = SerializedFlowCommands(
                flow_initialization_command=None,
                serialized_node_commands=[],
                serialized_connections=[],
                unique_parameter_uuid_to_values={},
                set_parameter_value_commands={},
                set_lock_commands_per_node={},
                sub_flows_commands=[],
                node_dependencies=MagicMock(),
                node_types_used=set(),
            )
            return SerializeFlowToCommandsResultSuccess(serialized_flow_commands=commands, result_details="ok")
        msg = f"Unexpected request in test: {type(request).__name__}"
        raise AssertionError(msg)


@pytest.fixture
def project_engine(engine: Engine, tmp_path: Path) -> Iterator[Engine]:
    """The engine on the default project, with an empty workspace, so saves write real files."""
    workspace = tmp_path.resolve() / "workspace"
    workspace.mkdir()
    project_yml = workspace / "project_template.yml"
    project_yml.write_text(DEFAULT_PROJECT_TEMPLATE.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE))
    load_result = engine.handle_request(LoadProjectTemplateRequest(project_path=project_yml))
    assert isinstance(load_result, LoadProjectTemplateResultSuccess)
    engine.handle_request(SetCurrentProjectRequest(project_id=load_result.project_id))
    engine.config_manager.workspace_path = workspace
    yield engine
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    engine.handle_request(SetCurrentProjectRequest(project_id=None))


@pytest.fixture(autouse=True)
def close_everything(engine: Engine) -> Iterator[None]:
    """Close what each test opened."""
    yield
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))


class TestWorkflowManagerScopes:
    def test_every_handler_declares_its_scope(self, engine: Engine) -> None:
        scopes = {
            request_type: engine.event_manager.get_scope_for_request_type(request_type)
            for handler_class in _HANDLER_CLASSES
            for attr in vars(handler_class).values()
            for request_type in handled_request_types(attr)
        }

        assert len(scopes) > len(_WORKFLOW_REQUESTS) + len(_OPTIONAL_WORKFLOW_REQUESTS)
        for request_type, scope in scopes.items():
            expected = RequestScope.HOST
            if request_type in _WORKFLOW_REQUESTS:
                expected = RequestScope.WORKFLOW
            if request_type in _OPTIONAL_WORKFLOW_REQUESTS:
                expected = RequestScope.OPTIONAL_WORKFLOW
            assert scope is expected, f"{request_type.__name__} is {scope}, expected {expected}"


class TestSavedWorkflowRequestsActOnTheWorkflowTheyName:
    def test_deleting_the_open_workflow_closes_it(self, project_engine: Engine) -> None:
        ensured = project_engine.handle_request(EnsureWorkflowAndFlowRequest(display_name="tab"))
        assert isinstance(ensured, EnsureWorkflowAndFlowResultSuccess), ensured.result_details
        assert ensured.workflow_id is not None
        tab = project_engine.open_workflows.get(ensured.workflow_id)
        saved = _send_async(project_engine, SaveWorkflowRequest(), tab)
        assert isinstance(saved, SaveWorkflowResultSuccess), saved.result_details

        deleted = _send_async(project_engine, DeleteWorkflowRequest(name=saved.workflow_name), None)

        assert isinstance(deleted, DeleteWorkflowResultSuccess), deleted.result_details
        assert not project_engine.open_workflows.is_open(tab.workflow_id)
        assert project_engine.workflow_registry.find_by_id(tab.workflow_id) is None
        assert not Path(saved.file_path).exists()

    def test_deleting_another_workflow_leaves_the_open_one_open(self, project_engine: Engine) -> None:
        other = _write_saved_workflow(project_engine, project_engine.config_manager.workspace_path / "other.py")
        tab = _open(project_engine, "tab")

        deleted = _send_async(project_engine, DeleteWorkflowRequest(name=other.name), tab)

        assert isinstance(deleted, DeleteWorkflowResultSuccess), deleted.result_details
        assert project_engine.open_workflows.is_open(tab.workflow_id)
        assert project_engine.workflow_registry.find_by_id(other.workflow_id) is None

    def test_renaming_a_closed_workflow_from_another_tab_moves_its_own_file(self, project_engine: Engine) -> None:
        closed = _save_and_close(project_engine, "closed")
        closed_body = _body(closed.file_path)
        tab = _open_with_flow(project_engine, "tab")

        renamed = _send_async(
            project_engine, RenameWorkflowRequest(workflow_name=closed.workflow_name, requested_name="renamed"), tab
        )

        assert isinstance(renamed, RenameWorkflowResultSuccess), renamed.result_details
        assert renamed.workflow_id == closed.workflow_id
        renamed_path = closed.file_path.with_name("renamed.py")
        assert _body(renamed_path) == closed_body
        assert read_workflow_metadata(renamed_path).name == "renamed"
        assert not closed.file_path.exists()
        assert project_engine.workflow_registry.get(closed.workflow_id).absolute_path == renamed_path
        assert not project_engine.workflow_registry.get(tab.workflow_id).is_saved
        assert project_engine.open_workflows.is_open(tab.workflow_id)

    def test_renaming_a_closed_workflow_whose_old_file_cannot_be_deleted_succeeds_with_a_warning(
        self, project_engine: Engine
    ) -> None:
        closed = _save_and_close(project_engine, "closed")
        handle_request = project_engine.ahandle_request

        async def refuse_deletes(request: RequestPayload) -> ResultPayload:
            if isinstance(request, DeleteFileRequest):
                return DeleteFileResultFailure(
                    failure_reason=FileIOFailureReason.PERMISSION_DENIED, result_details="file locked"
                )
            return await handle_request(request)

        with patch.object(project_engine, "ahandle_request", side_effect=refuse_deletes):
            renamed = _send_async(
                project_engine, RenameWorkflowRequest(workflow_id=closed.workflow_id, requested_name="renamed"), None
            )

        assert isinstance(renamed, RenameWorkflowResultSuccess), renamed.result_details
        assert isinstance(renamed.result_details, ResultDetails)
        warnings = [detail for detail in renamed.result_details.result_details if detail.level == logging.WARNING]
        assert len(warnings) == 1
        assert str(closed.file_path) in warnings[0].message
        renamed_path = closed.file_path.with_name("renamed.py")
        assert project_engine.workflow_registry.get(closed.workflow_id).absolute_path == renamed_path
        assert renamed_path.is_file()
        assert closed.file_path.is_file()

    def test_renaming_a_closed_workflow_keeps_it_in_its_folder(self, project_engine: Engine) -> None:
        closed = _save_and_close(project_engine, "closed")
        moved = _send_async(
            project_engine, MoveWorkflowRequest(workflow_id=closed.workflow_id, target_directory="shots"), None
        )
        assert isinstance(moved, MoveWorkflowResultSuccess), moved.result_details

        renamed = _send_async(
            project_engine, RenameWorkflowRequest(workflow_id=closed.workflow_id, requested_name="b"), None
        )

        assert isinstance(renamed, RenameWorkflowResultSuccess), renamed.result_details
        assert renamed.new_workflow_name == "shots/b"
        assert (project_engine.config_manager.workspace_path / "shots" / "b.py").is_file()

    def test_renaming_a_closed_workflow_to_its_own_name_rewrites_only_its_display_name(
        self, project_engine: Engine
    ) -> None:
        closed = _save_and_close(project_engine, "closed")
        closed_body = _body(closed.file_path)

        renamed = _send_async(
            project_engine,
            RenameWorkflowRequest(
                workflow_id=closed.workflow_id,
                requested_name=closed.file_path.stem,
                display_name_behavior=RenameDisplayNameBehavior.OVERRIDE,
                display_name="Shot 010",
            ),
            None,
        )

        assert isinstance(renamed, RenameWorkflowResultSuccess), renamed.result_details
        assert renamed.new_workflow_name == closed.workflow_name
        assert read_workflow_metadata(closed.file_path).name == "Shot 010"
        assert _body(closed.file_path) == closed_body
        assert project_engine.workflow_registry.get(closed.workflow_id).metadata.name == "Shot 010"

    def test_renaming_a_closed_workflow_onto_another_workflows_file_is_refused(self, project_engine: Engine) -> None:
        closed = _save_and_close(project_engine, "closed")
        taken = _save_and_close(project_engine, "taken")
        closed_content = closed.file_path.read_text(encoding="utf-8")
        taken_content = taken.file_path.read_text(encoding="utf-8")

        renamed = _send_async(
            project_engine,
            RenameWorkflowRequest(workflow_id=closed.workflow_id, requested_name=taken.file_path.stem),
            None,
        )

        assert isinstance(renamed, RenameWorkflowResultFailure)
        assert "already exists" in str(renamed.result_details)
        assert closed.file_path.read_text(encoding="utf-8") == closed_content
        assert taken.file_path.read_text(encoding="utf-8") == taken_content
        assert project_engine.workflow_registry.get(closed.workflow_id).name == closed.workflow_name
        assert project_engine.workflow_registry.get(taken.workflow_id).name == taken.workflow_name

    def test_renaming_an_open_workflow_onto_another_workflows_file_is_refused(self, project_engine: Engine) -> None:
        taken = _save_and_close(project_engine, "taken")
        taken_content = taken.file_path.read_text(encoding="utf-8")
        tab = _open_with_flow(project_engine, "tab")
        saved = _send_async(project_engine, SaveWorkflowRequest(), tab)
        assert isinstance(saved, SaveWorkflowResultSuccess), saved.result_details

        renamed = _send_async(
            project_engine,
            RenameWorkflowRequest(workflow_id=tab.workflow_id, requested_name=taken.file_path.stem),
            None,
        )

        assert isinstance(renamed, RenameWorkflowResultFailure)
        assert "Failed because another workflow already has that name." in str(renamed.result_details)
        assert taken.file_path.read_text(encoding="utf-8") == taken_content
        assert Path(saved.file_path).is_file()
        assert project_engine.workflow_registry.get(taken.workflow_id).name == taken.workflow_name

    def test_a_failed_save_of_an_open_rename_says_why(self, project_engine: Engine) -> None:
        tab = _open_with_flow(project_engine, "tab")
        failed_save = SaveWorkflowResultFailure(result_details="the disk is full")

        with patch.object(project_engine.workflow_manager.saver, "save_open_workflow", return_value=failed_save):
            renamed = _send_async(
                project_engine, RenameWorkflowRequest(workflow_id=tab.workflow_id, requested_name="renamed"), None
            )

        assert isinstance(renamed, RenameWorkflowResultFailure)
        assert "Failed because saving it there failed: the disk is full." in str(renamed.result_details)

    def test_renaming_a_closed_library_workflow_is_refused(self, project_engine: Engine) -> None:
        closed = _save_and_close(project_engine, "library")
        project_engine.workflow_registry.get(closed.workflow_id).metadata.is_griptape_provided = True

        renamed = _send_async(
            project_engine, RenameWorkflowRequest(workflow_id=closed.workflow_id, requested_name="mine"), None
        )

        assert isinstance(renamed, RenameWorkflowResultFailure)
        assert "a library provides it" in str(renamed.result_details)
        assert closed.file_path.is_file()

    def test_renaming_an_open_workflow_by_id_from_another_tab_saves_its_own_graph(self, project_engine: Engine) -> None:
        _allow_several_open(project_engine)
        target = _open_with_flow(project_engine, "target")
        saved = _send_async(project_engine, SaveWorkflowRequest(), target)
        assert isinstance(saved, SaveWorkflowResultSuccess), saved.result_details
        target_body = _body(Path(saved.file_path))
        tab = _open(project_engine, "tab")

        renamed = _send_async(
            project_engine, RenameWorkflowRequest(workflow_id=target.workflow_id, requested_name="renamed"), tab
        )

        assert isinstance(renamed, RenameWorkflowResultSuccess), renamed.result_details
        assert renamed.workflow_id == target.workflow_id
        renamed_path = Path(saved.file_path).with_name("renamed.py")
        assert _body(renamed_path) == target_body
        assert not Path(saved.file_path).exists()
        assert project_engine.open_workflows.is_open(target.workflow_id)
        assert not project_engine.workflow_registry.get(tab.workflow_id).is_saved

    def test_moving_a_closed_workflow_from_another_tab_moves_its_own_file(self, project_engine: Engine) -> None:
        closed = _save_and_close(project_engine, "closed")
        closed_content = closed.file_path.read_text(encoding="utf-8")
        tab = _open_with_flow(project_engine, "tab")

        moved = _send_async(
            project_engine, MoveWorkflowRequest(workflow_id=closed.workflow_id, target_directory="moved"), tab
        )

        assert isinstance(moved, MoveWorkflowResultSuccess), moved.result_details
        assert moved.workflow_id == closed.workflow_id
        moved_path = project_engine.config_manager.workspace_path / moved.moved_file_path
        assert moved_path.read_text(encoding="utf-8") == closed_content
        assert not closed.file_path.exists()
        assert project_engine.open_workflows.is_open(tab.workflow_id)
        assert not project_engine.workflow_registry.get(tab.workflow_id).is_saved

    def test_deleting_a_closed_workflow_by_id_from_another_tab_leaves_the_tab_alone(
        self, project_engine: Engine
    ) -> None:
        closed = _save_and_close(project_engine, "closed")
        tab = _open_with_flow(project_engine, "tab")

        deleted = _send_async(project_engine, DeleteWorkflowRequest(workflow_id=closed.workflow_id), tab)

        assert isinstance(deleted, DeleteWorkflowResultSuccess), deleted.result_details
        assert not closed.file_path.exists()
        assert project_engine.workflow_registry.find_by_id(closed.workflow_id) is None
        assert project_engine.open_workflows.is_open(tab.workflow_id)

    def test_renaming_an_open_workflow_from_another_tab_saves_that_workflow(self, engine: Engine) -> None:
        _allow_several_open(engine)
        tab = _open(engine, "tab")
        target = _open(engine, "target")
        saved_runtimes: list[WorkflowRuntime] = []

        async def save_open_workflow(workflow: WorkflowRuntime, request: SaveWorkflowRequest, **_: Any) -> Any:
            saved_runtimes.append(workflow)
            return SaveWorkflowResultSuccess(
                file_path=f"{request.file_name}.py", workflow_name=str(request.file_name), result_details="ok"
            )

        with (
            patch.object(engine.workflow_manager.saver, "save_open_workflow", side_effect=save_open_workflow),
            patch.object(engine.workflow_manager, "persist_external_workflow_registration"),
        ):
            renamed = _send_async(
                engine, RenameWorkflowRequest(workflow_name=_name(engine, target), requested_name="renamed"), tab
            )

        assert isinstance(renamed, RenameWorkflowResultSuccess), renamed.result_details
        assert saved_runtimes == [target]

    def test_renaming_a_name_no_workflow_has_fails_and_saves_nothing(self, engine: Engine) -> None:
        tab = _open(engine, "tab")
        save = AsyncMock()

        with patch.object(engine.workflow_manager.saver, "save_open_workflow", save):
            renamed = _send_async(engine, RenameWorkflowRequest(workflow_name="gone", requested_name="renamed"), tab)

        assert isinstance(renamed, RenameWorkflowResultFailure)
        assert "no workflow has that name" in str(renamed.result_details)
        save.assert_not_called()

    def test_publishing_saves_and_publishes_the_workflow_it_is_handed(self, engine: Engine, tmp_path: Path) -> None:
        _allow_several_open(engine)
        _open(engine, "beside")
        target = engine.open_workflow(_write_saved_workflow(engine, tmp_path.resolve() / "target.py").workflow_id)
        saved_in: list[WorkflowRuntime] = []
        published_in: list[Any] = []

        async def save(workflow: WorkflowRuntime, _request: SaveWorkflowRequest, **_: Any) -> Any:
            saved_in.append(workflow)
            return SaveWorkflowResultSuccess(file_path="", workflow_name="", result_details="ok")

        def publish(_request: PublishWorkflowRequest) -> PublishWorkflowResultSuccess:
            # The publisher reads the graph through the bound workflow, in its own thread.
            published_in.append(bound_workflow.get())
            return PublishWorkflowResultSuccess(
                published_workflow_file_path="", skip_published_workflow_registration=True, result_details="ok"
            )

        with (
            patch.object(
                engine.library_manager, "get_registered_event_handlers", return_value={"lib": _publisher(publish)}
            ),
            patch.object(engine.workflow_manager.saver, "save_open_workflow", side_effect=save),
        ):
            published = _send_async(
                engine, PublishWorkflowRequest(workflow_name=_name(engine, target), publisher_name="lib"), target
            )

        assert isinstance(published, PublishWorkflowResultSuccess), published.result_details
        assert saved_in == [target]
        assert published_in == [target]

    def test_publishing_another_open_workflow_by_name_is_refused(self, engine: Engine) -> None:
        _allow_several_open(engine)
        tab = _open(engine, "tab")
        other = _open(engine, "other")
        publish = MagicMock()

        with patch.object(
            engine.library_manager, "get_registered_event_handlers", return_value={"lib": _publisher(publish)}
        ):
            published = _send_async(
                engine, PublishWorkflowRequest(workflow_name=_name(engine, other), publisher_name="lib"), tab
            )

        assert isinstance(published, PublishWorkflowResultFailure)
        assert "from another workflow's tab" in str(published.result_details)
        publish.assert_not_called()


@dataclass(frozen=True)
class _AddressedRequest:
    """A saved-workflow request, built to name its workflow by the address fields a test passes."""

    build: Callable[[dict[str, Any]], RequestPayload]
    success: type[ResultPayload]
    failure: type[ResultPayload]
    # The workflow id the success reports for the workflow the request named, or None if it reports none.
    reported_id: Callable[[Any], str | None] | None = None
    # The field that holds the name: "workflow_name", or "name" for DeleteWorkflowRequest.
    name_field: str = "workflow_name"
    # Merge and reset act on a branch.
    targets_branch: bool = False


_ADDRESSED_REQUESTS = {
    "rename": _AddressedRequest(
        build=lambda address: RenameWorkflowRequest(requested_name="renamed", **address),
        success=RenameWorkflowResultSuccess,
        failure=RenameWorkflowResultFailure,
        reported_id=lambda result: result.workflow_id,
    ),
    "move": _AddressedRequest(
        build=lambda address: MoveWorkflowRequest(target_directory="moved", **address),
        success=MoveWorkflowResultSuccess,
        failure=MoveWorkflowResultFailure,
        reported_id=lambda result: result.workflow_id,
    ),
    "delete": _AddressedRequest(
        build=lambda address: DeleteWorkflowRequest(**address),
        success=DeleteWorkflowResultSuccess,
        failure=DeleteWorkflowResultFailure,
        reported_id=lambda result: result.workflow_id,
        name_field="name",
    ),
    "branch": _AddressedRequest(
        build=lambda address: BranchWorkflowRequest(**address),
        success=BranchWorkflowResultSuccess,
        failure=BranchWorkflowResultFailure,
        reported_id=lambda result: result.original_workflow_id,
    ),
    "merge": _AddressedRequest(
        build=lambda address: MergeWorkflowBranchRequest(**address),
        success=MergeWorkflowBranchResultSuccess,
        failure=MergeWorkflowBranchResultFailure,
        targets_branch=True,
    ),
    "reset": _AddressedRequest(
        build=lambda address: ResetWorkflowBranchRequest(**address),
        success=ResetWorkflowBranchResultSuccess,
        failure=ResetWorkflowBranchResultFailure,
        reported_id=lambda result: result.workflow_id,
        targets_branch=True,
    ),
    "compare": _AddressedRequest(
        build=lambda address: CompareWorkflowsRequest(compare_workflow_name="other", **address),
        success=CompareWorkflowsResultSuccess,
        failure=CompareWorkflowsResultFailure,
        reported_id=lambda result: result.workflow_id,
    ),
    "get_metadata": _AddressedRequest(
        build=lambda address: GetWorkflowMetadataRequest(**address),
        success=GetWorkflowMetadataResultSuccess,
        failure=GetWorkflowMetadataResultFailure,
    ),
    "set_metadata": _AddressedRequest(
        build=lambda address: SetWorkflowMetadataRequest(
            workflow_metadata={"description": "described"},  # type: ignore[arg-type]
            **address,
        ),
        success=SetWorkflowMetadataResultSuccess,
        failure=SetWorkflowMetadataResultFailure,
    ),
    "get_info": _AddressedRequest(
        build=lambda address: GetWorkflowInfoRequest(**address),
        success=GetWorkflowInfoResultSuccess,
        failure=GetWorkflowInfoResultFailure,
        reported_id=lambda result: result.workflow_id,
    ),
}


class TestSavedWorkflowRequestsResolveTheirAddress:
    """Each request names its workflow by id, by name, or by both when they agree, whatever the envelope names."""

    @pytest.fixture
    def addressed(self, project_engine: Engine) -> dict[str, Any]:
        """A saved, closed "source" workflow with a branch, a saved "other" one, and an open tab beside them."""
        source = _save_and_close(project_engine, "source")
        other = _save_and_close(project_engine, "other")
        branch = _send_async(project_engine, BranchWorkflowRequest(workflow_id=source.workflow_id), None)
        assert isinstance(branch, BranchWorkflowResultSuccess), branch.result_details
        assert branch.branched_workflow_id is not None
        registry = project_engine.workflow_registry
        # Info is reported for files a registry scan has loaded the metadata of.
        info_key = str(source.file_path)
        project_engine.workflow_manager._workflow_file_path_to_info[info_key] = WorkflowManager.WorkflowInfo(
            status=WorkflowStatus.GOOD, workflow_path=info_key, workflow_name="source"
        )
        return {
            "source": registry.get(source.workflow_id),
            "branch": registry.get(branch.branched_workflow_id),
            "other": registry.get(other.workflow_id),
            "tab": _open_with_flow(project_engine, "tab"),
        }

    @pytest.mark.parametrize("address_by", ["id", "name", "both"])
    @pytest.mark.parametrize("request_kind", list(_ADDRESSED_REQUESTS))
    def test_an_id_a_name_or_both_reach_the_workflow(
        self, project_engine: Engine, addressed: dict[str, Any], request_kind: str, address_by: str
    ) -> None:
        spec = _ADDRESSED_REQUESTS[request_kind]
        target = addressed["branch"] if spec.targets_branch else addressed["source"]
        address: dict[str, Any] = {}
        if address_by in ("id", "both"):
            address["workflow_id"] = target.workflow_id
        if address_by in ("name", "both"):
            address[spec.name_field] = target.name

        result = _send_async(project_engine, spec.build(address), addressed["tab"])

        assert isinstance(result, spec.success), result.result_details
        if spec.reported_id is not None:
            assert spec.reported_id(result) == target.workflow_id
        assert project_engine.open_workflows.is_open(addressed["tab"].workflow_id)

    @pytest.mark.parametrize(
        ("address_by", "expected"),
        [
            ("disagreeing", "belongs to"),
            ("stale_name", "belongs to"),
            ("neither", "names no workflow"),
            ("unknown_id", "no workflow has that id"),
            ("unknown_name", "no workflow has that name"),
        ],
    )
    @pytest.mark.parametrize("request_kind", list(_ADDRESSED_REQUESTS))
    def test_an_address_naming_no_single_workflow_fails(
        self, project_engine: Engine, addressed: dict[str, Any], request_kind: str, address_by: str, expected: str
    ) -> None:
        spec = _ADDRESSED_REQUESTS[request_kind]
        target = addressed["branch"] if spec.targets_branch else addressed["source"]
        address: dict[str, Any] = {}
        if address_by == "disagreeing":
            address = {"workflow_id": target.workflow_id, spec.name_field: addressed["other"].name}
        if address_by == "stale_name":
            address = {"workflow_id": target.workflow_id, spec.name_field: "gone"}
        if address_by == "unknown_id":
            address = {"workflow_id": "0" * 32}
        if address_by == "unknown_name":
            address = {spec.name_field: "gone"}
        listed_before = project_engine.workflow_registry.list_workflows()

        result = _send_async(project_engine, spec.build(address), addressed["tab"])

        assert isinstance(result, spec.failure)
        assert expected in str(result.result_details)
        assert project_engine.workflow_registry.list_workflows() == listed_before

    @pytest.mark.parametrize(
        "compare_address",
        [
            {"compare_workflow_id": "0" * 32},
            {"compare_workflow_name": "gone"},
            {"compare_workflow_id": "0" * 32, "compare_workflow_name": "gone"},
        ],
        ids=["unknown_id", "unknown_name", "both_unknown"],
    )
    def test_comparing_with_a_workflow_nothing_names_any_more_reports_no_source(
        self, project_engine: Engine, addressed: dict[str, Any], compare_address: dict[str, str]
    ) -> None:
        source = addressed["source"]

        result = _send_async(
            project_engine,
            CompareWorkflowsRequest(
                workflow_id=source.workflow_id,
                compare_workflow_id=compare_address.get("compare_workflow_id"),
                compare_workflow_name=compare_address.get("compare_workflow_name"),
            ),
            None,
        )

        assert isinstance(result, CompareWorkflowsResultSuccess), result.result_details
        assert result.status == "no_source"
        assert result.compare_workflow_id is None

    @pytest.mark.parametrize(
        ("compare_address_by", "expected"),
        [
            ("stale_name", "belongs to"),
            ("unknown_id_known_name", "though one has that name"),
            ("disagreeing", "belongs to"),
        ],
    )
    def test_comparing_with_an_id_and_a_name_that_disagree_fails(
        self, project_engine: Engine, addressed: dict[str, Any], compare_address_by: str, expected: str
    ) -> None:
        source = addressed["source"]
        other = addressed["other"]
        compare_address = {"compare_workflow_id": other.workflow_id, "compare_workflow_name": "gone"}
        if compare_address_by == "unknown_id_known_name":
            compare_address = {"compare_workflow_id": "0" * 32, "compare_workflow_name": other.name}
        if compare_address_by == "disagreeing":
            compare_address = {"compare_workflow_id": other.workflow_id, "compare_workflow_name": source.name}

        result = _send_async(
            project_engine,
            CompareWorkflowsRequest(
                workflow_id=source.workflow_id,
                compare_workflow_id=compare_address.get("compare_workflow_id"),
                compare_workflow_name=compare_address.get("compare_workflow_name"),
            ),
            None,
        )

        assert isinstance(result, CompareWorkflowsResultFailure)
        assert expected in str(result.result_details)


def _publisher(handler: Any, get_publish_options: Any = None) -> RegisteredEventHandler:
    return RegisteredEventHandler(
        handler=handler,
        library_data=MagicMock(),
        event_data=PublishWorkflowRegisteredEventData(
            start_flow_node_type="Start",
            start_flow_node_library_name="lib",
            end_flow_node_type="End",
            end_flow_node_library_name="lib",
            get_publish_options=get_publish_options,
        ),
    )


class TestPublishOptions:
    def test_the_callback_reads_the_only_open_workflow_when_the_request_names_none(self, engine: Engine) -> None:
        """Publishers read the open graph for their saved settings."""
        workflow = _open(engine, "open")
        seen: list[WorkflowRuntime | None] = []

        def get_publish_options(_request: GetPublishOptionsRequest) -> GetPublishOptionsResultSuccess:
            seen.append(engine.current_workflow_or_none())
            return GetPublishOptionsResultSuccess(fields=[], result_details="options")

        with patch.object(
            engine.library_manager,
            "get_registered_event_handlers",
            return_value={"lib": _publisher(MagicMock(), get_publish_options)},
        ):
            result = engine.handle_request(GetPublishOptionsRequest(workflow_name="open", publisher_name="lib"))

        assert isinstance(result, GetPublishOptionsResultSuccess), result.result_details
        assert seen == [workflow]


class TestRequestsAboutOneMoreWorkflowTakeAnId:
    """Requests that name a saved or imported workflow take its id too, resolved as the saved-workflow requests do."""

    @pytest.mark.parametrize("address_by", ["id", "both"])
    def test_a_run_command_reaches_the_workflow_by_id(self, engine: Engine, tmp_path: Path, address_by: str) -> None:
        workspace = tmp_path.resolve()
        engine.config_manager.workspace_path = workspace
        saved = _write_saved_workflow(
            engine, workspace / "run_me.py", workflow_shape=WorkflowShape(inputs={}, outputs={})
        )
        name = None
        if address_by == "both":
            name = saved.name

        result = _send_async(
            engine, GetWorkflowRunCommandRequest(workflow_id=saved.workflow_id, workflow_name=name), None
        )

        assert isinstance(result, GetWorkflowRunCommandResultSuccess), result.result_details
        assert "run_me.py" in result.run_command

    def test_a_run_command_for_an_id_and_a_name_that_disagree_fails(self, engine: Engine, tmp_path: Path) -> None:
        workspace = tmp_path.resolve()
        engine.config_manager.workspace_path = workspace
        first = _write_saved_workflow(engine, workspace / "first.py")
        second = _write_saved_workflow(engine, workspace / "second.py")

        result = _send_async(
            engine, GetWorkflowRunCommandRequest(workflow_id=first.workflow_id, workflow_name=second.name), None
        )

        assert isinstance(result, GetWorkflowRunCommandResultFailure)
        assert "belongs to" in str(result.result_details)

    def test_publishing_by_the_tabs_own_id_hands_the_publisher_its_name(self, engine: Engine, tmp_path: Path) -> None:
        tab = engine.open_workflow(_write_saved_workflow(engine, tmp_path.resolve() / "tab.py").workflow_id)
        published_names: list[str | None] = []

        def publish(request: PublishWorkflowRequest) -> PublishWorkflowResultSuccess:
            published_names.append(request.workflow_name)
            return PublishWorkflowResultSuccess(
                published_workflow_file_path="", skip_published_workflow_registration=True, result_details="ok"
            )

        async def save(_workflow: WorkflowRuntime, _request: SaveWorkflowRequest, **_: Any) -> Any:
            return SaveWorkflowResultSuccess(file_path="", workflow_name="", result_details="ok")

        with (
            patch.object(
                engine.library_manager, "get_registered_event_handlers", return_value={"lib": _publisher(publish)}
            ),
            patch.object(engine.workflow_manager.saver, "save_open_workflow", side_effect=save),
        ):
            published = _send_async(
                engine, PublishWorkflowRequest(workflow_id=tab.workflow_id, publisher_name="lib"), tab
            )

        assert isinstance(published, PublishWorkflowResultSuccess), published.result_details
        assert published_names == [_name(engine, tab)]

    def test_publishing_another_workflows_id_is_refused(self, engine: Engine) -> None:
        _allow_several_open(engine)
        tab = _open(engine, "tab")
        other = _open(engine, "other")
        publish = MagicMock()

        with patch.object(
            engine.library_manager, "get_registered_event_handlers", return_value={"lib": _publisher(publish)}
        ):
            published = _send_async(
                engine, PublishWorkflowRequest(workflow_id=other.workflow_id, publisher_name="lib"), tab
            )

        assert isinstance(published, PublishWorkflowResultFailure)
        assert "isn't the workflow open in this tab" in str(published.result_details)
        publish.assert_not_called()

    def test_publish_options_by_id_hand_the_publisher_its_name(self, engine: Engine) -> None:
        tab = _open(engine, "tab")
        seen_names: list[str | None] = []

        def get_publish_options(request: GetPublishOptionsRequest) -> GetPublishOptionsResultSuccess:
            seen_names.append(request.workflow_name)
            return GetPublishOptionsResultSuccess(fields=[], result_details="options")

        with patch.object(
            engine.library_manager,
            "get_registered_event_handlers",
            return_value={"lib": _publisher(MagicMock(), get_publish_options)},
        ):
            result = _send(engine, GetPublishOptionsRequest(workflow_id=tab.workflow_id, publisher_name="lib"), tab)

        assert isinstance(result, GetPublishOptionsResultSuccess), result.result_details
        assert seen_names == [_name(engine, tab)]

    def test_a_workflow_shape_names_the_workflow_its_id_names(self, engine: Engine, tmp_path: Path) -> None:
        tab = _open(engine, "tab")
        imported = _write_saved_workflow(engine, tmp_path.resolve() / "imported.py")

        with patch.object(engine.workflow_manager, "extract_workflow_shape_in", side_effect=ValueError("no shape")):
            result = _send(engine, GetWorkflowShapeRequest(workflow_id=imported.workflow_id), tab)
            extract_calls = engine.workflow_manager.extract_workflow_shape_in.call_args_list  # type: ignore[attr-defined]

        assert isinstance(result, GetWorkflowShapeResultFailure)
        assert [call.args[1] for call in extract_calls] == [imported.name]
        assert f"workflow '{imported.name}'" in str(result.result_details)

    def test_a_workflow_shape_for_an_unknown_id_fails(self, engine: Engine) -> None:
        tab = _open(engine, "tab")

        result = _send(engine, GetWorkflowShapeRequest(workflow_id="0" * 32), tab)

        assert isinstance(result, GetWorkflowShapeResultFailure)
        assert "no workflow has that id" in str(result.result_details)


class TestWorkflowRequestsActOnTheWorkflowTheyAreHanded:
    def test_save_serializes_and_records_the_workflow_it_is_handed(self, engine: Engine) -> None:
        _allow_several_open(engine)
        _open(engine, "tab")
        target = _open(engine, "target")
        recorded = _RecordedRequests(flow_name="target_canvas")
        record_saved_file = MagicMock(return_value=MagicMock(name="saved", workflow_id="saved"))

        with (
            patch.object(engine, "ahandle_request", new=recorded),
            patch.object(engine.workflow_manager, "extract_workflow_shape_in", side_effect=ValueError("no shape")),
            patch.object(
                engine.workflow_manager.saver,
                "_save_workflow_file_inline",
                return_value=SaveWorkflowFileFromSerializedFlowResultSuccess(
                    file_path="target.py", workflow_metadata=MagicMock(), result_details="ok"
                ),
            ),
            patch.object(engine.workflow_manager.saver, "_record_saved_file", record_saved_file),
        ):
            result = asyncio.run(
                engine.workflow_manager.saver.on_save_workflow_request(SaveWorkflowRequest(file_name="target"), target)
            )
            extract_calls = engine.workflow_manager.extract_workflow_shape_in.call_args_list  # type: ignore[attr-defined]

        assert isinstance(result, SaveWorkflowResultSuccess), result.result_details
        assert recorded.bound == [(GetTopLevelFlowRequest, target), (SerializeFlowToCommandsRequest, target)]
        assert [call.args[0] for call in extract_calls] == [target]
        assert record_saved_file.call_args.args == (target,)

    def test_save_subflow_serializes_the_handed_workflow_into_the_named_workflows_file(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        _allow_several_open(engine)
        _open(engine, "tab")
        source = _open(engine, "subflow source")
        saved_target = _write_saved_workflow(engine, tmp_path.resolve() / "target.py")
        recorded = _RecordedRequests(flow_name="unused")
        written_paths: list[str | None] = []

        async def save_file(request: Any) -> SaveWorkflowFileFromSerializedFlowResultSuccess:
            written_paths.append(request.file_path)
            return SaveWorkflowFileFromSerializedFlowResultSuccess(
                file_path=str(request.file_path), workflow_metadata=saved_target.metadata, result_details="ok"
            )

        with (
            patch.object(engine, "ahandle_request", new=recorded),
            patch.object(engine.workflow_manager, "extract_workflow_shape_in", side_effect=ValueError("no shape")),
            patch.object(
                engine.workflow_manager.saver,
                "on_save_workflow_file_from_serialized_flow_request",
                side_effect=save_file,
            ),
        ):
            result = asyncio.run(
                engine.workflow_manager.saver.on_save_subflow_to_workflow(
                    SaveSubflowToWorkflowRequest(flow_name="Sub", workflow_id=saved_target.workflow_id), source
                )
            )

        assert isinstance(result, SaveSubflowToWorkflowResultSuccess), result.result_details
        assert recorded.bound == [(SerializeFlowToCommandsRequest, source)]
        assert written_paths == [str(saved_target.absolute_path)]

    def test_referenced_import_creates_its_flow_in_the_workflow_it_is_handed(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        _allow_several_open(engine)
        tab = _open(engine, "tab")
        _create_flow(engine, tab, "tab_canvas")
        importing = _open(engine, "importing")
        canvas = _create_flow(engine, importing, "importing_canvas")
        imported = _write_saved_workflow(engine, tmp_path.resolve() / "imported.py")

        async def run_workflow(_relative_file_path: str, workflow: WorkflowRuntime | None) -> WorkflowExecutionResult:
            assert workflow is importing
            # A workflow file creates its flow under the flow it runs in, in the workflow it is bound to.
            created = engine.handle_request(CreateFlowRequest(parent_flow_name=canvas, flow_name="imported_flow"))
            assert isinstance(created, CreateFlowResultSuccess), created.result_details
            return WorkflowExecutionResult(execution_successful=True, execution_details="ok")

        with patch.object(engine.workflow_manager.runner, "run_workflow", side_effect=run_workflow):
            result = _send_async(
                engine,
                ImportWorkflowAsReferencedSubFlowRequest(workflow_name=imported.name, flow_name=canvas),
                importing,
            )
            into_tab_flow = _send_async(
                engine,
                ImportWorkflowAsReferencedSubFlowRequest(workflow_id=imported.workflow_id, flow_name="tab_canvas"),
                importing,
            )

        assert isinstance(result, ImportWorkflowAsReferencedSubFlowResultSuccess), result.result_details
        assert result.created_flow_name == "imported_flow"
        assert importing.flows.name_to_parent_name["imported_flow"] == canvas
        assert "imported_flow" not in tab.flows.name_to_parent_name
        # The tab's flow is not in the workflow the import was handed.
        assert isinstance(into_tab_flow, ImportWorkflowAsReferencedSubFlowResultFailure)
        assert "target flow does not exist" in str(into_tab_flow.result_details)

    def test_ensure_creates_its_flow_in_the_workflow_it_is_handed(self, engine: Engine) -> None:
        _allow_several_open(engine)
        tab = _open(engine, "tab")
        target = _open(engine, "target")

        result = _send(engine, EnsureWorkflowAndFlowRequest(flow_name="canvas"), target)

        assert isinstance(result, EnsureWorkflowAndFlowResultSuccess), result.result_details
        assert result.workflow_id == target.workflow_id
        assert result.created_workflow is False
        assert result.created_flow is True
        assert target.context.get_current_flow().name == result.flow_name
        assert tab.flows.name_to_parent_name == {}

    def test_ensure_naming_no_workflow_reuses_the_only_open_one(self, engine: Engine) -> None:
        tab = _open(engine, "tab")

        result = _send(engine, EnsureWorkflowAndFlowRequest(flow_name="canvas"), None)

        assert isinstance(result, EnsureWorkflowAndFlowResultSuccess), result.result_details
        assert result.workflow_id == tab.workflow_id
        assert result.created_workflow is False

    def test_ensure_naming_no_workflow_while_several_are_open_fails(self, engine: Engine) -> None:
        _allow_several_open(engine)
        tab = _open(engine, "tab")
        beside = _open(engine, "beside")

        result = _send(engine, EnsureWorkflowAndFlowRequest(flow_name="canvas"), None)

        assert isinstance(result, WorkflowNotNamedResultFailure)
        assert "did not name one" in str(result.result_details)
        assert engine.open_workflows.all_open() == [tab, beside]

    def test_run_command_naming_nothing_answers_for_the_workflow_it_is_handed(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        workspace = tmp_path.resolve()
        engine.config_manager.workspace_path = workspace
        _allow_several_open(engine)
        saved = _write_saved_workflow(engine, workspace / "tab.py", workflow_shape=WorkflowShape(inputs={}, outputs={}))
        tab = engine.open_workflow(saved.workflow_id)
        _open(engine, "beside")

        result = _send_async(engine, GetWorkflowRunCommandRequest(), tab)

        assert isinstance(result, GetWorkflowRunCommandResultSuccess), result.result_details
        assert "tab.py" in result.run_command

    def test_run_command_naming_nothing_with_no_workflow_fails(self, engine: Engine) -> None:
        result = _send_async(engine, GetWorkflowRunCommandRequest(), None)

        assert isinstance(result, GetWorkflowRunCommandResultFailure)
        assert "no workflow is loaded" in str(result.result_details)
