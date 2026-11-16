"""Path and macro handlers resolve `{workflow_dir}` for the workflow dispatch hands them.

Several tests open a second workflow beside the first and address it, so a handler that fell back to
"the only open workflow" would fail instead of passing by luck. A request that names no workflow
resolves for the only open one until clients name theirs on the request envelope.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from PIL import Image

from griptape_nodes.common.macro_parser import ParsedMacro
from griptape_nodes.files.path_utils import resolve_path_safely
from griptape_nodes.retained_mode.events.artifact_events import (
    GeneratePreviewFromDefaultsRequest,
    GeneratePreviewRequest,
    GetPreviewForArtifactRequest,
    GetPreviewForArtifactResultFailure,
    PreviewGenerationPolicy,
    RegisterArtifactProviderRequest,
)
from griptape_nodes.retained_mode.events.generic_events import WorkflowNotNamedResultFailure
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.os_events import (
    ExistingFilePolicy,
    GetNextVersionIndexRequest,
    ListDirectoryRequest,
    ListDirectoryResultSuccess,
    ListDirectorySequencesRequest,
    ResolveMacroPathRequest,
    ResolveMacroPathResultFailure,
    ScanSequencesRequest,
    WriteFileRequest,
    WriteFileResultSuccess,
    WriteTempFileRequest,
)
from griptape_nodes.retained_mode.events.project_events import (
    AttemptMapAbsolutePathToProjectRequest,
    AttemptMapAbsolutePathToProjectResultSuccess,
    AttemptMatchPathAgainstMacroRequest,
    AttemptMatchPathAgainstMacroResultSuccess,
    GetPathForMacroRequest,
    GetPathForMacroResultFailure,
    GetPathForMacroResultSuccess,
    GetStateForMacroRequest,
    GetStateForMacroResultSuccess,
    MacroPath,
)
from griptape_nodes.retained_mode.events.static_file_events import (
    CreateStaticFileDownloadUrlFromPathRequest,
    CreateStaticFileDownloadUrlRequest,
    CreateStaticFileDownloadUrlResultSuccess,
    CreateStaticFileRequest,
    CreateStaticFileUploadUrlRequest,
)
from griptape_nodes.retained_mode.file_metadata.sidecar_metadata import SidecarContent
from griptape_nodes.retained_mode.managers.artifact_manager import ArtifactManager
from griptape_nodes.retained_mode.managers.artifact_providers import ImageArtifactProvider
from griptape_nodes.retained_mode.managers.event_manager import ResultContext
from griptape_nodes.retained_mode.managers.filesystem.file_writing import FileWriter
from griptape_nodes.retained_mode.managers.filesystem.sequences import FileSequences
from griptape_nodes.retained_mode.managers.os_manager import OSManager
from griptape_nodes.retained_mode.managers.project_manager import ProjectManager
from griptape_nodes.retained_mode.managers.static_files_manager import StaticFilesManager
from griptape_nodes.retained_mode.request_handlers import RequestScope, handled_request_types

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

# They resolve paths or macros for a workflow when there is one, and work on their own otherwise.
_OPTIONAL_WORKFLOW_REQUESTS: set[type[RequestPayload]] = {
    GetPathForMacroRequest,
    AttemptMatchPathAgainstMacroRequest,
    GetStateForMacroRequest,
    AttemptMapAbsolutePathToProjectRequest,
    ListDirectoryRequest,
    ListDirectorySequencesRequest,
    ScanSequencesRequest,
    GetNextVersionIndexRequest,
    WriteFileRequest,
    WriteTempFileRequest,
    ResolveMacroPathRequest,
    CreateStaticFileRequest,
    CreateStaticFileUploadUrlRequest,
    CreateStaticFileDownloadUrlRequest,
    CreateStaticFileDownloadUrlFromPathRequest,
    GeneratePreviewRequest,
    GeneratePreviewFromDefaultsRequest,
    GetPreviewForArtifactRequest,
}
_HANDLER_CLASSES: tuple[type, ...] = (
    ProjectManager,
    OSManager,
    FileSequences,
    FileWriter,
    StaticFilesManager,
    ArtifactManager,
)


def _declared_scopes(
    engine: Engine, handler_classes: tuple[type, ...]
) -> dict[type[RequestPayload], RequestScope | None]:
    return {
        request_type: engine.event_manager.get_scope_for_request_type(request_type)
        for handler_class in handler_classes
        for attr in vars(handler_class).values()
        for request_type in handled_request_types(attr)
    }


def _open(engine: Engine, working_directory: Path) -> WorkflowRuntime:
    """Open an unsaved workflow created in `working_directory`."""
    entry = engine.workflow_registry.register_unsaved(working_directory.name, str(working_directory), listed=False)
    return engine.open_workflow(entry.workflow_id)


def _send(engine: Engine, request: RequestPayload, workflow: WorkflowRuntime | None = None) -> ResultPayload:
    """Dispatch `request` as a client would, with the envelope naming `workflow` (or nothing)."""
    workflow_id = None if workflow is None else workflow.workflow_id
    return engine.event_manager.handle_request(
        request, result_context=ResultContext(open_workflow_id=workflow_id)
    ).result


@contextmanager
def _running_a_node_for(engine: Engine, workflow_id: str, file_path: Path) -> Iterator[None]:
    """Run as a worker running a node for its orchestrator's saved workflow at `file_path`."""
    with patch.object(engine.library_manager, "_is_worker", new=True):
        workflow = engine.context_manager.adopt_orchestrator_workflow(workflow_id, file_path.stem, str(file_path), None)
        with engine.run_in_worker_workflow(workflow):
            yield


@pytest.fixture
def nothing_open(engine: Engine) -> Iterator[Engine]:
    """The engine with no workflow open."""
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    yield engine
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))


@pytest.fixture
def workspace(nothing_open: Engine, tmp_path: Path) -> Iterator[Path]:
    """Make `tmp_path` the workspace, so static files under a workflow's folder stay in that folder."""
    original = nothing_open.config_manager.workspace_path
    nothing_open.config_manager.workspace_path = tmp_path
    yield tmp_path
    nothing_open.config_manager.workspace_path = original


def _folder(path: Path) -> Path:
    return resolve_path_safely(path)


class TestPathHandlerScopes:
    def test_every_handler_declares_its_scope(self, engine: Engine) -> None:
        scopes = _declared_scopes(engine, _HANDLER_CLASSES)

        assert len(scopes) > len(_OPTIONAL_WORKFLOW_REQUESTS)
        for request_type, scope in scopes.items():
            expected = RequestScope.HOST
            if request_type in _OPTIONAL_WORKFLOW_REQUESTS:
                expected = RequestScope.OPTIONAL_WORKFLOW
            assert scope is expected, f"{request_type.__name__} is {scope}, expected {expected}"


class TestMacrosResolveForTheDispatchedWorkflow:
    def test_get_path_for_macro_resolves_the_addressed_workflow_not_the_open_one(
        self, nothing_open: Engine, tmp_path: Path
    ) -> None:
        _open(nothing_open, tmp_path / "open")
        addressed = _open(nothing_open, tmp_path / "addressed")

        result = _send(
            nothing_open, GetPathForMacroRequest(parsed_macro=ParsedMacro("{outputs}/img.png"), variables={}), addressed
        )

        assert isinstance(result, GetPathForMacroResultSuccess), result.result_details
        assert result.absolute_path == _folder(tmp_path / "addressed" / "outputs" / "img.png")

    def test_attempt_map_resolves_directories_for_the_addressed_workflow(
        self, nothing_open: Engine, tmp_path: Path
    ) -> None:
        _open(nothing_open, tmp_path / "open")
        addressed = _open(nothing_open, tmp_path / "addressed")
        written = tmp_path / "addressed" / "outputs" / "img.png"

        result = _send(nothing_open, AttemptMapAbsolutePathToProjectRequest(absolute_path=written), addressed)

        assert isinstance(result, AttemptMapAbsolutePathToProjectResultSuccess), result.result_details
        assert result.mapped_path == "{outputs}/img.png"

    def test_get_state_for_macro_reports_a_directory_resolvable_for_a_workflow(
        self, nothing_open: Engine, tmp_path: Path
    ) -> None:
        workflow = _open(nothing_open, tmp_path)

        result = _send(
            nothing_open, GetStateForMacroRequest(parsed_macro=ParsedMacro("{outputs}/img.png"), variables={}), workflow
        )

        assert isinstance(result, GetStateForMacroResultSuccess), result.result_details
        assert result.can_resolve

    def test_a_request_naming_no_workflow_resolves_for_the_only_open_one(
        self, nothing_open: Engine, tmp_path: Path
    ) -> None:
        _open(nothing_open, tmp_path / "open")

        result = _send(
            nothing_open, GetPathForMacroRequest(parsed_macro=ParsedMacro("{outputs}/img.png"), variables={})
        )

        assert isinstance(result, GetPathForMacroResultSuccess), result.result_details
        assert result.absolute_path == _folder(tmp_path / "open" / "outputs" / "img.png")

    def test_a_request_naming_no_workflow_while_several_are_open_fails(
        self, nothing_open: Engine, tmp_path: Path
    ) -> None:
        _open(nothing_open, tmp_path / "a")
        _open(nothing_open, tmp_path / "b")

        result = _send(
            nothing_open, GetPathForMacroRequest(parsed_macro=ParsedMacro("{outputs}/img.png"), variables={})
        )

        assert not result.succeeded()
        assert "did not name one" in str(result.result_details)

    def test_naming_no_workflow_while_several_are_open_fails_even_without_a_workflow_builtin(
        self, nothing_open: Engine, tmp_path: Path
    ) -> None:
        """Dispatch picks the workflow before the macro is read, so it can't tell the macro needs none."""
        _open(nothing_open, tmp_path / "a")
        _open(nothing_open, tmp_path / "b")

        result = _send(
            nothing_open, GetPathForMacroRequest(parsed_macro=ParsedMacro("{workspace_dir}/img.png"), variables={})
        )

        assert isinstance(result, WorkflowNotNamedResultFailure)


class TestNoWorkflow:
    def test_a_required_workflow_builtin_fails_with_nothing_open(self, nothing_open: Engine) -> None:
        result = _send(
            nothing_open, GetPathForMacroRequest(parsed_macro=ParsedMacro("{workflow_dir}/img.png"), variables={})
        )

        assert isinstance(result, GetPathForMacroResultFailure)
        assert "uses {workflow_dir} and the request has no workflow" in str(result.result_details)

    def test_an_optional_workflow_builtin_is_dropped_with_nothing_open(self, nothing_open: Engine) -> None:
        result = _send(
            nothing_open, GetPathForMacroRequest(parsed_macro=ParsedMacro("{workflow_dir?:/}img.png"), variables={})
        )

        assert isinstance(result, GetPathForMacroResultSuccess), result.result_details
        assert result.resolved_path == Path("img.png")


class TestCallerSuppliedWorkflowValuesWin:
    """A caller that supplies `{workflow_dir}` itself needs no workflow to answer it."""

    def test_attempt_match_uses_the_callers_workflow_dir(self, nothing_open: Engine) -> None:
        result = _send(
            nothing_open,
            AttemptMatchPathAgainstMacroRequest(
                parsed_macro=ParsedMacro("{workflow_dir}/outputs/{file_name}.png"),
                file_path="/shows/my_show/outputs/hero.png",
                known_variables={"workflow_dir": "/shows/my_show"},
            ),
        )

        assert isinstance(result, AttemptMatchPathAgainstMacroResultSuccess), result.result_details
        assert result.extracted_variables == {"workflow_dir": "/shows/my_show", "file_name": "hero"}


class TestAWorkerResolvesForTheWorkflowItRunsANodeFor:
    def test_a_node_request_resolves_the_orchestrators_folder(self, nothing_open: Engine, tmp_path: Path) -> None:
        workflow_file = tmp_path / "shows" / "my_show" / "my_show.py"
        workflow_file.parent.mkdir(parents=True)
        workflow_file.write_text("# stub")

        with _running_a_node_for(nothing_open, "0000000000000000000000000000b001", workflow_file):
            result = nothing_open.handle_request(
                GetPathForMacroRequest(parsed_macro=ParsedMacro("{workflow_dir}/outputs/img.png"), variables={})
            )

        assert isinstance(result, GetPathForMacroResultSuccess), result.result_details
        assert result.absolute_path == _folder(workflow_file.parent / "outputs" / "img.png")


class TestFileHandlersResolveForTheDispatchedWorkflow:
    """OS handlers resolve macro paths, and address the requests they send, to the workflow they are handed."""

    def test_write_file_lands_in_the_addressed_workflows_outputs(self, nothing_open: Engine, tmp_path: Path) -> None:
        _open(nothing_open, tmp_path / "open")
        addressed = _open(nothing_open, tmp_path / "addressed")

        result = _send(
            nothing_open,
            WriteFileRequest(
                file_path=MacroPath(ParsedMacro("{outputs}/notes.txt"), {}), content="hello", create_parents=True
            ),
            addressed,
        )

        assert isinstance(result, WriteFileResultSuccess), result.result_details
        assert Path(result.final_file_path) == _folder(tmp_path / "addressed" / "outputs" / "notes.txt")
        assert not (tmp_path / "open" / "outputs").exists()

    def test_a_sidecar_records_the_addressed_workflow(self, nothing_open: Engine, tmp_path: Path) -> None:
        open_workflow = _open(nothing_open, tmp_path / "open")
        addressed = _open(nothing_open, tmp_path / "addressed")
        written = tmp_path / "addressed" / "notes.txt"

        result = _send(
            nothing_open,
            WriteFileRequest(
                file_path=str(written), content="hello", create_parents=True, file_metadata=SidecarContent()
            ),
            addressed,
        )

        assert isinstance(result, WriteFileResultSuccess), result.result_details
        sidecars = list((tmp_path / "addressed" / ".griptape-nodes-metadata").rglob("notes.txt.json"))
        assert len(sidecars) == 1
        recorded_name = json.loads(sidecars[0].read_text())["workflow"]["name"]
        assert recorded_name == nothing_open.workflow_registry.get(addressed.workflow_id).name
        assert recorded_name != nothing_open.workflow_registry.get(open_workflow.workflow_id).name

    def test_list_directory_resolves_a_macro_for_the_addressed_workflow(
        self, nothing_open: Engine, tmp_path: Path
    ) -> None:
        _open(nothing_open, tmp_path / "open")
        addressed = _open(nothing_open, tmp_path / "addressed")
        (tmp_path / "addressed" / "outputs").mkdir(parents=True)
        (tmp_path / "addressed" / "outputs" / "a.txt").write_text("a")

        result = _send(nothing_open, ListDirectoryRequest(directory_path="{outputs}", workspace_only=False), addressed)

        assert isinstance(result, ListDirectoryResultSuccess), result.result_details
        assert [entry.name for entry in result.entries] == ["a.txt"]

    def test_resolve_macro_path_fails_for_a_required_workflow_builtin_with_nothing_open(
        self, nothing_open: Engine
    ) -> None:
        result = _send(
            nothing_open, ResolveMacroPathRequest(macro_path=MacroPath(ParsedMacro("{workflow_dir}/img.png"), {}))
        )

        assert isinstance(result, ResolveMacroPathResultFailure)


class TestStaticFilesResolveForTheDispatchedWorkflow:
    """Static files and previews go to the folder of the workflow they are for."""

    def test_a_static_file_url_points_into_the_addressed_workflows_folder(
        self, nothing_open: Engine, workspace: Path
    ) -> None:
        _open(nothing_open, workspace / "open")
        addressed = _open(nothing_open, workspace / "addressed")

        result = _send(
            nothing_open,
            CreateStaticFileDownloadUrlRequest(file_name="notes.txt", situation_name="save_static_file"),
            addressed,
        )

        assert isinstance(result, CreateStaticFileDownloadUrlResultSuccess), result.result_details
        assert Path(result.file_url) == workspace / "addressed" / "staticfiles" / "notes.txt"
        assert "/workspace/addressed/staticfiles/notes.txt" in result.url

    def test_a_preview_is_looked_up_in_the_addressed_workflows_preview_folder(
        self, nothing_open: Engine, workspace: Path
    ) -> None:
        """Generating one writes through unaddressed requests, so this checks where it is looked for."""
        nothing_open.handle_request(RegisterArtifactProviderRequest(provider_class=ImageArtifactProvider))
        _open(nothing_open, workspace / "open")
        addressed = _open(nothing_open, workspace / "addressed")
        image_path = workspace / "img.png"
        Image.new("RGB", (64, 64), color="red").save(image_path)

        result = _send(
            nothing_open,
            GetPreviewForArtifactRequest(
                macro_path=MacroPath(ParsedMacro(str(image_path)), {}),
                artifact_provider_name=ImageArtifactProvider.get_friendly_name(),
                preview_generation_policy=PreviewGenerationPolicy.DO_NOT_GENERATE,
            ),
            addressed,
        )

        assert isinstance(result, GetPreviewForArtifactResultFailure)
        assert f"metadata file not found at '{_folder(workspace / 'addressed' / '.griptape-nodes-previews')}" in str(
            result.result_details
        )

    def test_save_workspace_static_file_with_nothing_open_lands_in_the_workspace(
        self, nothing_open: Engine, workspace: Path
    ) -> None:
        url = nothing_open.static_files_manager.save_workspace_static_file(
            b"hello", "notes.txt", ExistingFilePolicy.CREATE_NEW
        )

        assert (workspace / "staticfiles" / "notes.txt").read_bytes() == b"hello"
        assert "/workspace/staticfiles/notes.txt" in url
