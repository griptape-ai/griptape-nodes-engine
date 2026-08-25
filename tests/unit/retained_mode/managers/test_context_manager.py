"""Tests for ContextManager.push_workflow."""

import ast
import logging
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from griptape_nodes.node_library.workflow_registry import WorkflowMetadata, WorkflowRegistry
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.context_events import (
    SetWorkflowContextFailure,
    SetWorkflowContextRequest,
    SetWorkflowContextSuccess,
)


class TestPushWorkflow:
    """Tests for ContextManager.push_workflow."""

    def test_push_workflow_with_name(self, griptape_nodes: Engine) -> None:
        """workflow_name is used directly as the registry key."""
        context_manager = griptape_nodes.ContextManager()
        result = context_manager.push_workflow(workflow_name="my_workflow")

        assert result == "my_workflow"
        assert context_manager.get_current_workflow_name() == "my_workflow"

        context_manager.pop_workflow()

    def test_push_workflow_with_file_path_inside_workspace(self, griptape_nodes: Engine) -> None:
        """file_path inside workspace produces a workspace-relative registry key."""
        context_manager = griptape_nodes.ContextManager()
        config_manager = griptape_nodes.ConfigManager()

        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            original = config_manager.workspace_path
            config_manager.workspace_path = workspace
            try:
                workflow_file = workspace / "subdir" / "my_flow.py"
                result = context_manager.push_workflow(file_path=str(workflow_file))
            finally:
                config_manager.workspace_path = original

        assert result == "subdir/my_flow"
        assert context_manager.get_current_workflow_name() == "subdir/my_flow"

        context_manager.pop_workflow()

    def test_push_workflow_with_file_path_outside_workspace(self, griptape_nodes: Engine) -> None:
        """file_path outside workspace uses the absolute path as the registry key."""
        context_manager = griptape_nodes.ContextManager()
        config_manager = griptape_nodes.ConfigManager()

        with tempfile.TemporaryDirectory() as workspace_dir, tempfile.TemporaryDirectory() as other_dir:
            workspace = Path(workspace_dir)
            original = config_manager.workspace_path
            config_manager.workspace_path = workspace
            try:
                workflow_file = Path(other_dir) / "my_flow.py"
                result = context_manager.push_workflow(file_path=str(workflow_file))
            finally:
                config_manager.workspace_path = original

        expected = (Path(other_dir).resolve() / "my_flow").as_posix()
        assert result == expected
        assert context_manager.get_current_workflow_name() == expected

        context_manager.pop_workflow()

    def test_push_workflow_retains_file_path_independent_of_workspace(self, griptape_nodes: Engine) -> None:
        """The pushed file path is retained verbatim and does NOT move with the workspace.

        Regression guard: the registry key is derived against whatever workspace is active at
        push time, so switching projects re-registers workflows under a new key and the name
        goes stale. The retained path is what lets `workflow_dir` keep answering afterwards.
        """
        context_manager = griptape_nodes.ContextManager()
        config_manager = griptape_nodes.ConfigManager()

        with tempfile.TemporaryDirectory() as workspace_dir, tempfile.TemporaryDirectory() as other_dir:
            original = config_manager.workspace_path
            config_manager.workspace_path = Path(workspace_dir)
            try:
                workflow_file = Path(other_dir) / "my_flow.py"
                context_manager.push_workflow(file_path=str(workflow_file))

                assert context_manager.get_current_workflow_file_path() == str(workflow_file)

                # Simulate a project switch: the workspace moves under the open workflow.
                config_manager.workspace_path = Path(other_dir)
                assert context_manager.get_current_workflow_file_path() == str(workflow_file)
            finally:
                config_manager.workspace_path = original
                context_manager.pop_workflow()

    def test_push_workflow_by_name_captures_path_while_key_is_valid(self, griptape_nodes: Engine) -> None:
        """Entering by key resolves the path immediately, not lazily at read time.

        A key only resolves against the workspace active right now, so deferring the lookup
        would leave nothing to answer with once the workspace changes. An unregistered name
        legitimately has no path.
        """
        context_manager = griptape_nodes.ContextManager()

        context_manager.push_workflow(workflow_name="not_registered_anywhere")
        try:
            assert context_manager.get_current_workflow_file_path() is None
        finally:
            context_manager.pop_workflow()

    def test_push_workflow_by_name_resolves_registered_path_and_keeps_it(self, griptape_nodes: Engine) -> None:
        """A registered workflow entered by key gets its complete path cached, and keeps it.

        This is the branch the interactive open-workflow flow takes for every already-saved
        workflow (`SetWorkflowContextRequest` -> `push_workflow(workflow_name=...)`). Resolving
        at push time is the whole point: the key only resolves against the workspace that is
        active right now, so the workspace switch below would otherwise leave nothing to answer
        with.
        """
        context_manager = griptape_nodes.ContextManager()
        config_manager = griptape_nodes.ConfigManager()

        with tempfile.TemporaryDirectory() as workspace_dir, tempfile.TemporaryDirectory() as other_dir:
            workspace = Path(workspace_dir)
            workflow_file = workspace / "subdir" / "my_flow.py"
            workflow_file.parent.mkdir()
            # Workflow.from_disk verifies the file exists; a stub is enough here.
            workflow_file.write_text("# stub")
            metadata = WorkflowMetadata(
                name="my_flow",
                schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
                engine_version_created_with="test",
                node_libraries_referenced=[],
                creation_date=datetime.now(UTC),
            )
            original = config_manager.workspace_path
            config_manager.workspace_path = workspace
            try:
                with patch.dict(WorkflowRegistry._workflows, {}, clear=True):
                    # Registered workspace-RELATIVE, so resolving the key genuinely depends on
                    # which workspace is active.
                    WorkflowRegistry.generate_new_workflow(
                        registry_key="subdir/my_flow", metadata=metadata, file_path="subdir/my_flow.py"
                    )
                    context_manager.push_workflow(workflow_name="subdir/my_flow")
                    try:
                        expected = str(workflow_file.resolve())
                        assert context_manager.get_current_workflow_file_path() == expected

                        # Simulate a project switch: the key is now meaningless, the path is not.
                        config_manager.workspace_path = Path(other_dir)
                        assert context_manager.get_current_workflow_file_path() == expected
                    finally:
                        context_manager.pop_workflow()
            finally:
                config_manager.workspace_path = original

    def test_get_current_workflow_file_path_requires_a_workflow(self, griptape_nodes: Engine) -> None:
        """Asking outside a workflow context is an error, matching get_current_workflow_name."""
        context_manager = griptape_nodes.ContextManager()

        with pytest.raises(context_manager.NoActiveWorkflowError):
            context_manager.get_current_workflow_file_path()

    def test_push_workflow_raises_when_both_provided(self, griptape_nodes: Engine) -> None:
        """Raises ValueError when both workflow_name and file_path are given."""
        context_manager = griptape_nodes.ContextManager()

        with pytest.raises(ValueError, match="not both"):
            context_manager.push_workflow(workflow_name="my_workflow", file_path="/some/path.py")

    def test_push_workflow_raises_when_neither_provided(self, griptape_nodes: Engine) -> None:
        """Raises ValueError when neither workflow_name nor file_path is given."""
        context_manager = griptape_nodes.ContextManager()

        with pytest.raises(ValueError, match="must be provided"):
            context_manager.push_workflow()

    def test_push_workflow_strips_extension(self, griptape_nodes: Engine) -> None:
        """Registry key derived from file_path has no file extension."""
        context_manager = griptape_nodes.ContextManager()
        config_manager = griptape_nodes.ConfigManager()

        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            original = config_manager.workspace_path
            config_manager.workspace_path = workspace
            try:
                workflow_file = workspace / "my_workflow.py"
                result = context_manager.push_workflow(file_path=str(workflow_file))
            finally:
                config_manager.workspace_path = original

        assert result == "my_workflow"
        assert "." not in result

        context_manager.pop_workflow()


class TestWorkflowWorkingDirectory:
    """Tests for the folder an unsaved workflow belongs to.

    A workflow that has never been saved has no file, so `{workflow_dir}` -- and every project
    directory built on it -- has nothing to anchor to and degrades to a workspace-relative
    path. Files generated before the first save land at the workspace root rather than in the
    folder the user created the workflow in. `working_directory` is what the caller passes so
    the engine can answer with the intended folder in the meantime.
    """

    def _resolve_outputs(self, griptape_nodes: Engine) -> Path:
        """Resolve `{workflow_dir?:/}outputs/img.png` the way a saving node would."""
        from griptape_nodes.common.macro_parser import ParsedMacro
        from griptape_nodes.retained_mode.events.project_events import (
            GetPathForMacroRequest,
            GetPathForMacroResultSuccess,
        )

        result = griptape_nodes.handle_request(
            GetPathForMacroRequest(parsed_macro=ParsedMacro("{workflow_dir?:/}outputs/img.png"), variables={})
        )
        assert isinstance(result, GetPathForMacroResultSuccess)
        return result.absolute_path

    def test_unsaved_workflow_outputs_land_in_the_supplied_folder(self, griptape_nodes: Engine) -> None:
        """The whole point: an unsaved workflow's outputs resolve under the folder it was created in."""
        context_manager = griptape_nodes.ContextManager()
        config_manager = griptape_nodes.ConfigManager()

        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir).resolve()
            browsed = workspace / "shots" / "sh010"
            browsed.mkdir(parents=True)
            original = config_manager.workspace_path
            config_manager.workspace_path = workspace
            try:
                result = griptape_nodes.handle_request(
                    SetWorkflowContextRequest(display_name="Untitled", working_directory=str(browsed))
                )
                assert isinstance(result, SetWorkflowContextSuccess)
                # The folder is NOT the registry key: the workflow is still unsaved.
                assert result.workflow_name.startswith(WorkflowRegistry.UNSAVED_KEY_PREFIX)
                assert context_manager.get_current_workflow_file_path() is None

                assert self._resolve_outputs(griptape_nodes) == browsed / "outputs" / "img.png"
            finally:
                config_manager.workspace_path = original
                while context_manager.has_current_workflow():
                    context_manager.pop_workflow()

    def test_omitting_the_folder_keeps_the_workspace_root_fallback(self, griptape_nodes: Engine) -> None:
        """The field is optional, so the pre-existing behavior has to survive untouched.

        Only one of the places the editor creates a workflow has a folder to offer; the others
        pass a display name and nothing else.
        """
        context_manager = griptape_nodes.ContextManager()
        config_manager = griptape_nodes.ConfigManager()

        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir).resolve()
            original = config_manager.workspace_path
            config_manager.workspace_path = workspace
            try:
                result = griptape_nodes.handle_request(SetWorkflowContextRequest(display_name="Untitled"))
                assert isinstance(result, SetWorkflowContextSuccess)
                assert context_manager.get_current_workflow_working_directory() is None

                assert self._resolve_outputs(griptape_nodes) == workspace / "outputs" / "img.png"
            finally:
                config_manager.workspace_path = original
                while context_manager.has_current_workflow():
                    context_manager.pop_workflow()

    def test_relative_folder_is_anchored_to_the_workspace(self, griptape_nodes: Engine) -> None:
        """A relative folder resolves against the workspace, not the process working directory.

        The path a caller holds is often built from the project base directory, which sits a
        level above the workspace that macro resolution is relative to, so the value is
        normalized on the way in rather than being trusted as-is.
        """
        context_manager = griptape_nodes.ContextManager()
        config_manager = griptape_nodes.ConfigManager()

        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir).resolve()
            original = config_manager.workspace_path
            config_manager.workspace_path = workspace
            try:
                result = griptape_nodes.handle_request(
                    SetWorkflowContextRequest(display_name="Untitled", working_directory="shots/sh020")
                )
                assert isinstance(result, SetWorkflowContextSuccess)

                expected = workspace / "shots" / "sh020"
                assert context_manager.get_current_workflow_working_directory() == str(expected)
                assert self._resolve_outputs(griptape_nodes) == expected / "outputs" / "img.png"
            finally:
                config_manager.workspace_path = original
                while context_manager.has_current_workflow():
                    context_manager.pop_workflow()

    def test_saved_location_wins_over_the_supplied_folder(self, griptape_nodes: Engine) -> None:
        """Saving elsewhere repoints `{workflow_dir}`; the creation folder does not linger.

        The folder is only an answer for a workflow with no file of its own. Once there is a
        file -- and the user may well have saved it somewhere other than where they started --
        that file's own directory is the better answer.
        """
        context_manager = griptape_nodes.ContextManager()
        config_manager = griptape_nodes.ConfigManager()

        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir).resolve()
            browsed = workspace / "shots" / "sh010"
            browsed.mkdir(parents=True)
            original = config_manager.workspace_path
            config_manager.workspace_path = workspace
            try:
                result = griptape_nodes.handle_request(
                    SetWorkflowContextRequest(display_name="Untitled", working_directory=str(browsed))
                )
                assert isinstance(result, SetWorkflowContextSuccess)
                assert self._resolve_outputs(griptape_nodes) == browsed / "outputs" / "img.png"

                # What the save path does once the workflow gets a file.
                saved_to = workspace / "elsewhere" / "my_flow.py"
                context_manager.set_current_workflow_file_path(str(saved_to))

                assert self._resolve_outputs(griptape_nodes) == saved_to.parent / "outputs" / "img.png"
            finally:
                config_manager.workspace_path = original
                while context_manager.has_current_workflow():
                    context_manager.pop_workflow()

    def test_supplying_a_file_as_the_folder_is_refused(self, griptape_nodes: Engine) -> None:
        """A file where a folder was meant fails loudly rather than writing beside the file.

        The value becomes the parent of everything the workflow writes, so accepting a file
        would silently put outputs next to it -- a plausible location, which is exactly what
        makes it hard to notice.
        """
        context_manager = griptape_nodes.ContextManager()
        config_manager = griptape_nodes.ConfigManager()

        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            not_a_folder = workspace / "notes.txt"
            not_a_folder.write_text("not a folder")
            original = config_manager.workspace_path
            config_manager.workspace_path = workspace
            try:
                result = griptape_nodes.handle_request(
                    SetWorkflowContextRequest(display_name="Untitled", working_directory=str(not_a_folder))
                )

                assert isinstance(result, SetWorkflowContextFailure)
                # Rejected before the context was pushed, so there is nothing to unwind.
                assert not context_manager.has_current_workflow()
            finally:
                config_manager.workspace_path = original
                while context_manager.has_current_workflow():
                    context_manager.pop_workflow()

    def test_folder_need_not_exist_yet(self, griptape_nodes: Engine) -> None:
        """A folder that has not been created yet is accepted.

        Project directories are created on demand at write time, so requiring the folder to
        exist up front would reject the ordinary case of creating a workflow in a new folder.
        """
        context_manager = griptape_nodes.ContextManager()
        config_manager = griptape_nodes.ConfigManager()

        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir).resolve()
            not_yet = workspace / "shots" / "sh030"
            original = config_manager.workspace_path
            config_manager.workspace_path = workspace
            try:
                result = griptape_nodes.handle_request(
                    SetWorkflowContextRequest(display_name="Untitled", working_directory=str(not_yet))
                )

                assert isinstance(result, SetWorkflowContextSuccess)
                assert self._resolve_outputs(griptape_nodes) == not_yet / "outputs" / "img.png"
            finally:
                config_manager.workspace_path = original
                while context_manager.has_current_workflow():
                    context_manager.pop_workflow()

    def test_folder_silences_the_unresolved_workflow_dir_warning(
        self, griptape_nodes: Engine, caplog: pytest.LogCaptureFixture
    ) -> None:
        """With a folder to answer with, `{workflow_dir?:/}` no longer degrades.

        The degradation is warned about precisely because the result is a plausible path rather
        than an obvious error, so the absence of the warning is the signal that nothing was
        dropped.
        """
        context_manager = griptape_nodes.ContextManager()
        config_manager = griptape_nodes.ConfigManager()

        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            browsed = workspace / "shots" / "sh010"
            browsed.mkdir(parents=True)
            original = config_manager.workspace_path
            config_manager.workspace_path = workspace
            try:
                result = griptape_nodes.handle_request(
                    SetWorkflowContextRequest(display_name="Untitled", working_directory=str(browsed))
                )
                assert isinstance(result, SetWorkflowContextSuccess)

                with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
                    self._resolve_outputs(griptape_nodes)

                dropped = [
                    r.message
                    for r in caplog.records
                    if r.levelno == logging.WARNING and "dropping it from the path" in r.message
                ]
                assert dropped == []
            finally:
                config_manager.workspace_path = original
                while context_manager.has_current_workflow():
                    context_manager.pop_workflow()

    def test_get_working_directory_requires_a_workflow(self, griptape_nodes: Engine) -> None:
        """Asking outside a workflow context is an error, matching the other context accessors."""
        context_manager = griptape_nodes.ContextManager()

        with pytest.raises(context_manager.NoActiveWorkflowError):
            context_manager.get_current_workflow_working_directory()


class TestGeneratedWorkflowCode:
    """Tests that generated workflow code uses push_workflow(file_path=__file__)."""

    def test_generated_code_uses_file_path_not_workflow_name(self, griptape_nodes: Engine) -> None:
        """_generate_workflow_run_prerequisite_code emits push_workflow(file_path=__file__)."""
        from griptape_nodes.retained_mode.managers.workflow_manager import ImportRecorder

        workflow_manager = griptape_nodes.WorkflowManager()
        import_recorder = ImportRecorder()
        code_blocks = workflow_manager._generate_workflow_run_prerequisite_code(
            import_recorder=import_recorder,
            library_names=[],
        )

        module = ast.Module(body=[n for n in code_blocks if isinstance(n, ast.stmt)], type_ignores=[])
        ast.fix_missing_locations(module)
        source = ast.unparse(module)

        assert "push_workflow(file_path=__file__)" in source
        assert "workflow_name=" not in source
        assert "workflow_name=" not in source


class TestEnsureWorkflowAndFlowRequest:
    """Tests for ContextManager.on_ensure_workflow_and_flow_request."""

    def _cleanup(self, griptape_nodes: Engine) -> None:
        """Tear down any workflow/flow state left over between tests."""
        from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest

        griptape_nodes.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

    def test_creates_workflow_and_flow_from_cold_start(self, griptape_nodes: Engine) -> None:
        from griptape_nodes.retained_mode.events.context_events import (
            EnsureWorkflowAndFlowRequest,
            EnsureWorkflowAndFlowResultSuccess,
        )

        self._cleanup(griptape_nodes)
        context_manager = griptape_nodes.ContextManager()
        assert not context_manager.has_current_workflow()
        assert not context_manager.has_current_flow()

        result = context_manager.on_ensure_workflow_and_flow_request(
            EnsureWorkflowAndFlowRequest(workflow_name="my_workflow", flow_name="my_flow")
        )

        assert isinstance(result, EnsureWorkflowAndFlowResultSuccess)
        assert result.workflow_name == "my_workflow"
        assert result.flow_name == "my_flow"
        assert result.created_workflow is True
        assert result.created_flow is True
        assert context_manager.has_current_workflow()
        assert context_manager.has_current_flow()

        self._cleanup(griptape_nodes)

    def test_reuses_existing_workflow_and_flow(self, griptape_nodes: Engine) -> None:
        from griptape_nodes.retained_mode.events.context_events import (
            EnsureWorkflowAndFlowRequest,
            EnsureWorkflowAndFlowResultSuccess,
        )

        self._cleanup(griptape_nodes)
        context_manager = griptape_nodes.ContextManager()

        # Bootstrap once.
        first = context_manager.on_ensure_workflow_and_flow_request(
            EnsureWorkflowAndFlowRequest(workflow_name="scratch", flow_name="canvas")
        )
        assert isinstance(first, EnsureWorkflowAndFlowResultSuccess)

        # Calling again with different names should be a no-op: existing context wins.
        second = context_manager.on_ensure_workflow_and_flow_request(
            EnsureWorkflowAndFlowRequest(workflow_name="ignored", flow_name="also_ignored")
        )

        assert isinstance(second, EnsureWorkflowAndFlowResultSuccess)
        assert second.workflow_name == "scratch"
        assert second.flow_name == "canvas"
        assert second.created_workflow is False
        assert second.created_flow is False

        self._cleanup(griptape_nodes)

    def test_auto_generates_workflow_name_when_none_given(self, griptape_nodes: Engine) -> None:
        from griptape_nodes.node_library.workflow_registry import WorkflowRegistry
        from griptape_nodes.retained_mode.events.context_events import (
            EnsureWorkflowAndFlowRequest,
            EnsureWorkflowAndFlowResultSuccess,
        )

        self._cleanup(griptape_nodes)
        context_manager = griptape_nodes.ContextManager()

        result = context_manager.on_ensure_workflow_and_flow_request(EnsureWorkflowAndFlowRequest())

        assert isinstance(result, EnsureWorkflowAndFlowResultSuccess)
        assert result.workflow_name.startswith(WorkflowRegistry.UNSAVED_KEY_PREFIX)
        assert WorkflowRegistry.has_workflow_with_name(result.workflow_name)
        assert result.created_workflow is True
        assert result.created_flow is True

        self._cleanup(griptape_nodes)
