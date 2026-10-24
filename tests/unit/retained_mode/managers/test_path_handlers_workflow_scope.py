"""Path and macro handlers resolve `{workflow_dir}` for the workflow dispatch hands them, or fail without one.

Several tests open a second workflow beside the first and address it, so a handler that fell back to
"the only open workflow" would fail instead of passing by luck.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from griptape_nodes.common.macro_parser import ParsedMacro
from griptape_nodes.files.path_utils import resolve_path_safely
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.project_events import (
    AttemptMapAbsolutePathToProjectRequest,
    AttemptMapAbsolutePathToProjectResultFailure,
    AttemptMapAbsolutePathToProjectResultSuccess,
    AttemptMatchPathAgainstMacroRequest,
    GetPathForMacroRequest,
    GetPathForMacroResultFailure,
    GetPathForMacroResultSuccess,
    GetStateForMacroRequest,
    GetStateForMacroResultFailure,
)
from griptape_nodes.retained_mode.managers.project_manager import ProjectManager
from griptape_nodes.retained_mode.request_handlers import RequestScope, handled_request_scope, handled_request_types
from griptape_nodes.retained_mode.runtime.context import WorkflowContextError
from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime
from tests.workflow_context import as_worker_process, push_unsaved_workflow

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import RequestPayload

# They resolve paths or macros for a workflow when there is one, and work on their own otherwise.
_OPTIONAL_WORKFLOW_REQUESTS: set[type[RequestPayload]] = {
    GetPathForMacroRequest,
    AttemptMatchPathAgainstMacroRequest,
    GetStateForMacroRequest,
    AttemptMapAbsolutePathToProjectRequest,
}
_HANDLER_CLASSES: tuple[type, ...] = (ProjectManager,)


def _declared_scopes(handler_classes: tuple[type, ...]) -> dict[type[RequestPayload], RequestScope | None]:
    return {
        request_type: handled_request_scope(attr)
        for handler_class in handler_classes
        for attr in vars(handler_class).values()
        for request_type in handled_request_types(attr)
    }


@contextmanager
def _also_open(engine: Engine, working_directory: Path) -> Iterator[WorkflowRuntime]:
    """Open an unsaved workflow created in `working_directory` beside the open one, and yield its runtime."""
    entry = engine.workflow_registry.register_unsaved("beside", str(working_directory))
    workflow = WorkflowRuntime(entry.workflow_id, engine)
    with patch.dict(engine.open_workflows._by_id, {workflow.workflow_id: workflow}):
        yield workflow


@contextmanager
def _mirroring(engine: Engine, workflow_id: str, file_path: Path) -> Iterator[None]:
    """Run as a worker that mirrors its orchestrator's saved workflow at `file_path`."""
    with as_worker_process(engine):
        engine.context_manager.mirror_workflow_context(workflow_id, file_path.stem, str(file_path), None)
        try:
            yield
        finally:
            engine.context_manager.mirror_workflow_context(None, None, None, None)


@pytest.fixture
def nothing_open(engine: Engine) -> Iterator[Engine]:
    """The engine with no workflow open and none mirrored."""
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    yield engine
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))


def _folder(path: Path) -> Path:
    return resolve_path_safely(path)


class TestPathHandlerScopes:
    def test_every_handler_declares_its_scope(self) -> None:
        scopes = _declared_scopes(_HANDLER_CLASSES)

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
        push_unsaved_workflow(nothing_open, working_directory=str(tmp_path / "open"))

        with _also_open(nothing_open, tmp_path / "addressed") as addressed:
            result = nothing_open.handle_request(
                GetPathForMacroRequest(parsed_macro=ParsedMacro("{outputs}/img.png"), variables={}),
                workflow_id=addressed.workflow_id,
            )

        assert isinstance(result, GetPathForMacroResultSuccess), result.result_details
        assert result.absolute_path == _folder(tmp_path / "addressed" / "outputs" / "img.png")

    def test_attempt_map_resolves_directories_for_the_addressed_workflow(
        self, nothing_open: Engine, tmp_path: Path
    ) -> None:
        push_unsaved_workflow(nothing_open, working_directory=str(tmp_path / "open"))
        written = tmp_path / "addressed" / "outputs" / "img.png"

        with _also_open(nothing_open, tmp_path / "addressed") as addressed:
            result = nothing_open.handle_request(
                AttemptMapAbsolutePathToProjectRequest(absolute_path=written), workflow_id=addressed.workflow_id
            )

        assert isinstance(result, AttemptMapAbsolutePathToProjectResultSuccess), result.result_details
        assert result.mapped_path == "{outputs}/img.png"

    def test_an_unsaved_workflow_with_no_folder_answers_the_default_save_folder(self, nothing_open: Engine) -> None:
        workflow = push_unsaved_workflow(nothing_open)
        workspace = _folder(nothing_open.config_manager.workspace_path)

        result = nothing_open.handle_request(
            GetPathForMacroRequest(parsed_macro=ParsedMacro("{workflow_dir}/notes.txt"), variables={}),
            workflow_id=workflow.workflow_id,
        )

        assert isinstance(result, GetPathForMacroResultSuccess), result.result_details
        assert result.absolute_path == workspace / "notes.txt"


class TestNoWorkflowFailsRatherThanMovingThePath:
    @pytest.mark.parametrize("template", ["{workflow_dir}/img.png", "{workflow_dir?:/}img.png", "{outputs}/img.png"])
    def test_get_path_for_macro_fails_with_nothing_open(self, nothing_open: Engine, template: str) -> None:
        result = nothing_open.handle_request(GetPathForMacroRequest(parsed_macro=ParsedMacro(template), variables={}))

        assert isinstance(result, GetPathForMacroResultFailure)
        assert str(result.result_details).startswith(f"Attempted to resolve '{template}'. Failed because ")
        assert str(result.result_details).endswith("uses {workflow_dir} and no workflow is open.")

    def test_get_path_for_macro_without_a_workflow_builtin_still_resolves(self, nothing_open: Engine) -> None:
        result = nothing_open.handle_request(
            GetPathForMacroRequest(parsed_macro=ParsedMacro("{workspace_dir}/img.png"), variables={})
        )

        assert isinstance(result, GetPathForMacroResultSuccess), result.result_details

    def test_attempt_map_fails_with_nothing_open(self, nothing_open: Engine, tmp_path: Path) -> None:
        result = nothing_open.handle_request(
            AttemptMapAbsolutePathToProjectRequest(absolute_path=tmp_path / "outputs" / "img.png")
        )

        assert isinstance(result, AttemptMapAbsolutePathToProjectResultFailure)
        assert "uses {workflow_dir} and no workflow is open" in str(result.result_details)

    def test_get_state_for_macro_fails_with_nothing_open(self, nothing_open: Engine) -> None:
        result = nothing_open.handle_request(
            GetStateForMacroRequest(parsed_macro=ParsedMacro("{workflow_dir?:/}img.png"), variables={})
        )

        assert isinstance(result, GetStateForMacroResultFailure)


class TestAWorkerResolvesThroughItsMirror:
    def test_a_worker_with_a_mirror_resolves_the_lenders_folder(self, nothing_open: Engine, tmp_path: Path) -> None:
        workflow_file = tmp_path / "shows" / "my_show" / "my_show.py"
        workflow_file.parent.mkdir(parents=True)
        workflow_file.write_text("# stub")

        with _mirroring(nothing_open, "0000000000000000000000000000b001", workflow_file):
            result = nothing_open.handle_request(
                GetPathForMacroRequest(parsed_macro=ParsedMacro("{workflow_dir?:/}outputs/img.png"), variables={})
            )

        assert isinstance(result, GetPathForMacroResultSuccess), result.result_details
        assert result.absolute_path == _folder(workflow_file.parent / "outputs" / "img.png")

    def test_a_worker_with_no_mirror_fails(self, nothing_open: Engine) -> None:
        with as_worker_process(nothing_open):
            result = nothing_open.handle_request(
                GetPathForMacroRequest(parsed_macro=ParsedMacro("{workflow_dir?:/}outputs/img.png"), variables={})
            )

        assert isinstance(result, GetPathForMacroResultFailure)
        assert "no workflow is open" in str(result.result_details)

    def test_a_workflow_and_a_mirror_together_are_refused(self, nothing_open: Engine, tmp_path: Path) -> None:
        workflow_file = tmp_path / "w.py"
        workflow_file.write_text("# stub")
        project_manager = nothing_open.project_manager

        with _mirroring(nothing_open, "0000000000000000000000000000b002", workflow_file):
            entry = nothing_open.workflow_registry.register_unsaved("open")
            workflow = WorkflowRuntime(entry.workflow_id, nothing_open)
            with pytest.raises(WorkflowContextError, match="an engine never has both"):
                project_manager.on_get_path_for_macro_request(
                    GetPathForMacroRequest(parsed_macro=ParsedMacro("{workflow_dir}/x"), variables={}), workflow
                )
