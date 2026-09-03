"""Tests for ContextManager.push_workflow."""

import ast
import logging
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from griptape_nodes.common.macro_parser import ParsedMacro
from griptape_nodes.node_library.workflow_registry import WorkflowMetadata, WorkflowRegistry
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.base_events import AppEvent
from griptape_nodes.retained_mode.events.context_events import (
    CurrentWorkflowChanged,
    SetWorkflowContextFailure,
    SetWorkflowContextRequest,
    SetWorkflowContextSuccess,
)
from griptape_nodes.retained_mode.events.project_events import (
    GetPathForMacroRequest,
    GetPathForMacroResultSuccess,
)


class TestPushWorkflow:
    """Tests for ContextManager.push_workflow."""

    def test_push_workflow_with_name(self, engine: Engine) -> None:
        """workflow_name is used directly as the registry key."""
        context_manager = engine.context_manager
        result = context_manager.push_workflow(workflow_name="my_workflow")

        assert result == "my_workflow"
        assert context_manager.get_current_workflow_name() == "my_workflow"

        context_manager.pop_workflow()

    def test_push_workflow_with_file_path_inside_workspace(self, engine: Engine) -> None:
        """file_path inside workspace produces a workspace-relative registry key."""
        context_manager = engine.context_manager
        config_manager = engine.config_manager

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

    def test_push_workflow_with_file_path_outside_workspace(self, engine: Engine) -> None:
        """file_path outside workspace uses the absolute path as the registry key."""
        context_manager = engine.context_manager
        config_manager = engine.config_manager

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

    def test_push_workflow_retains_file_path_independent_of_workspace(self, engine: Engine) -> None:
        """The pushed file path is retained verbatim and does NOT move with the workspace.

        Regression guard: the registry key is derived against whatever workspace is active at
        push time, so switching projects re-registers workflows under a new key and the name
        goes stale. The retained path is what lets `workflow_dir` keep answering afterwards.
        """
        context_manager = engine.context_manager
        config_manager = engine.config_manager

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

    def test_push_workflow_by_name_captures_path_while_key_is_valid(self, engine: Engine) -> None:
        """Entering by key resolves the path immediately, not lazily at read time.

        A key only resolves against the workspace active right now, so deferring the lookup
        would leave nothing to answer with once the workspace changes. An unregistered name
        legitimately has no path.
        """
        context_manager = engine.context_manager

        context_manager.push_workflow(workflow_name="not_registered_anywhere")
        try:
            assert context_manager.get_current_workflow_file_path() is None
        finally:
            context_manager.pop_workflow()

    def test_push_workflow_by_name_resolves_registered_path_and_keeps_it(self, engine: Engine) -> None:
        """A registered workflow entered by key gets its complete path cached, and keeps it.

        This is the branch the interactive open-workflow flow takes for every already-saved
        workflow (`SetWorkflowContextRequest` -> `push_workflow(workflow_name=...)`). Resolving
        at push time is the whole point: the key only resolves against the workspace that is
        active right now, so the workspace switch below would otherwise leave nothing to answer
        with.
        """
        context_manager = engine.context_manager
        config_manager = engine.config_manager

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

    def test_get_current_workflow_file_path_requires_a_workflow(self, engine: Engine) -> None:
        """Asking outside a workflow context is an error, matching get_current_workflow_name."""
        context_manager = engine.context_manager

        with pytest.raises(context_manager.NoActiveWorkflowError):
            context_manager.get_current_workflow_file_path()

    def test_push_workflow_raises_when_both_provided(self, engine: Engine) -> None:
        """Raises ValueError when both workflow_name and file_path are given."""
        context_manager = engine.context_manager

        with pytest.raises(ValueError, match="not both"):
            context_manager.push_workflow(workflow_name="my_workflow", file_path="/some/path.py")

    def test_push_workflow_raises_when_neither_provided(self, engine: Engine) -> None:
        """Raises ValueError when neither workflow_name nor file_path is given."""
        context_manager = engine.context_manager

        with pytest.raises(ValueError, match="must be provided"):
            context_manager.push_workflow()

    def test_push_workflow_strips_extension(self, engine: Engine) -> None:
        """Registry key derived from file_path has no file extension."""
        context_manager = engine.context_manager
        config_manager = engine.config_manager

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

    def _resolve_outputs(self, engine: Engine) -> Path:
        """Resolve `{workflow_dir?:/}outputs/img.png` the way a saving node would."""
        result = engine.handle_request(
            GetPathForMacroRequest(parsed_macro=ParsedMacro("{workflow_dir?:/}outputs/img.png"), variables={})
        )
        assert isinstance(result, GetPathForMacroResultSuccess)
        return result.absolute_path

    def test_unsaved_workflow_outputs_land_in_the_supplied_folder(self, engine: Engine) -> None:
        """The whole point: an unsaved workflow's outputs resolve under the folder it was created in."""
        context_manager = engine.context_manager
        config_manager = engine.config_manager

        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir).resolve()
            browsed = workspace / "shots" / "sh010"
            browsed.mkdir(parents=True)
            original = config_manager.workspace_path
            config_manager.workspace_path = workspace
            try:
                result = engine.handle_request(
                    SetWorkflowContextRequest(display_name="Untitled", working_directory=str(browsed))
                )
                assert isinstance(result, SetWorkflowContextSuccess)
                # The folder is NOT the registry key: the workflow is still unsaved.
                assert result.workflow_name.startswith(WorkflowRegistry.UNSAVED_KEY_PREFIX)
                assert context_manager.get_current_workflow_file_path() is None

                assert self._resolve_outputs(engine) == browsed / "outputs" / "img.png"
            finally:
                config_manager.workspace_path = original
                while context_manager.has_current_workflow():
                    context_manager.pop_workflow()

    def test_omitting_the_folder_keeps_the_workspace_root_fallback(self, engine: Engine) -> None:
        """The field is optional, so the pre-existing behavior has to survive untouched.

        Only one of the places the editor creates a workflow has a folder to offer; the others
        pass a display name and nothing else.
        """
        context_manager = engine.context_manager
        config_manager = engine.config_manager

        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir).resolve()
            original = config_manager.workspace_path
            config_manager.workspace_path = workspace
            try:
                result = engine.handle_request(SetWorkflowContextRequest(display_name="Untitled"))
                assert isinstance(result, SetWorkflowContextSuccess)
                assert context_manager.get_current_workflow_working_directory() is None

                assert self._resolve_outputs(engine) == workspace / "outputs" / "img.png"
            finally:
                config_manager.workspace_path = original
                while context_manager.has_current_workflow():
                    context_manager.pop_workflow()

    def test_relative_folder_is_anchored_to_the_workspace(self, engine: Engine) -> None:
        """A relative folder resolves against the workspace, not the process working directory.

        The path a caller holds is often built from the project base directory, which sits a
        level above the workspace that macro resolution is relative to, so the value is
        normalized on the way in rather than being trusted as-is.
        """
        context_manager = engine.context_manager
        config_manager = engine.config_manager

        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir).resolve()
            original = config_manager.workspace_path
            config_manager.workspace_path = workspace
            try:
                result = engine.handle_request(
                    SetWorkflowContextRequest(display_name="Untitled", working_directory="shots/sh020")
                )
                assert isinstance(result, SetWorkflowContextSuccess)

                expected = workspace / "shots" / "sh020"
                assert context_manager.get_current_workflow_working_directory() == str(expected)
                assert self._resolve_outputs(engine) == expected / "outputs" / "img.png"
            finally:
                config_manager.workspace_path = original
                while context_manager.has_current_workflow():
                    context_manager.pop_workflow()

    def test_saved_location_wins_over_the_supplied_folder(self, engine: Engine) -> None:
        """Saving elsewhere repoints `{workflow_dir}`; the creation folder does not linger.

        The folder is only an answer for a workflow with no file of its own. Once there is a
        file -- and the user may well have saved it somewhere other than where they started --
        that file's own directory is the better answer.
        """
        context_manager = engine.context_manager
        config_manager = engine.config_manager

        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir).resolve()
            browsed = workspace / "shots" / "sh010"
            browsed.mkdir(parents=True)
            original = config_manager.workspace_path
            config_manager.workspace_path = workspace
            try:
                result = engine.handle_request(
                    SetWorkflowContextRequest(display_name="Untitled", working_directory=str(browsed))
                )
                assert isinstance(result, SetWorkflowContextSuccess)
                assert self._resolve_outputs(engine) == browsed / "outputs" / "img.png"

                # What the save path does once the workflow gets a file.
                saved_to = workspace / "elsewhere" / "my_flow.py"
                context_manager.set_current_workflow_file_path(str(saved_to))

                assert self._resolve_outputs(engine) == saved_to.parent / "outputs" / "img.png"
            finally:
                config_manager.workspace_path = original
                while context_manager.has_current_workflow():
                    context_manager.pop_workflow()

    def test_supplying_a_file_as_the_folder_is_refused(self, engine: Engine) -> None:
        """A file where a folder was meant fails loudly rather than writing beside the file.

        The value becomes the parent of everything the workflow writes, so accepting a file
        would silently put outputs next to it -- a plausible location, which is exactly what
        makes it hard to notice.
        """
        context_manager = engine.context_manager
        config_manager = engine.config_manager

        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            not_a_folder = workspace / "notes.txt"
            not_a_folder.write_text("not a folder")
            original = config_manager.workspace_path
            config_manager.workspace_path = workspace
            try:
                result = engine.handle_request(
                    SetWorkflowContextRequest(display_name="Untitled", working_directory=str(not_a_folder))
                )

                assert isinstance(result, SetWorkflowContextFailure)
                # Rejected before the context was pushed, so there is nothing to unwind.
                assert not context_manager.has_current_workflow()
            finally:
                config_manager.workspace_path = original
                while context_manager.has_current_workflow():
                    context_manager.pop_workflow()

    def test_folder_need_not_exist_yet(self, engine: Engine) -> None:
        """A folder that has not been created yet is accepted.

        Project directories are created on demand at write time, so requiring the folder to
        exist up front would reject the ordinary case of creating a workflow in a new folder.
        """
        context_manager = engine.context_manager
        config_manager = engine.config_manager

        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir).resolve()
            not_yet = workspace / "shots" / "sh030"
            original = config_manager.workspace_path
            config_manager.workspace_path = workspace
            try:
                result = engine.handle_request(
                    SetWorkflowContextRequest(display_name="Untitled", working_directory=str(not_yet))
                )

                assert isinstance(result, SetWorkflowContextSuccess)
                assert self._resolve_outputs(engine) == not_yet / "outputs" / "img.png"
            finally:
                config_manager.workspace_path = original
                while context_manager.has_current_workflow():
                    context_manager.pop_workflow()

    def test_folder_silences_the_unresolved_workflow_dir_warning(
        self, engine: Engine, caplog: pytest.LogCaptureFixture
    ) -> None:
        """With a folder to answer with, `{workflow_dir?:/}` no longer degrades.

        The degradation is warned about precisely because the result is a plausible path rather
        than an obvious error, so the absence of the warning is the signal that nothing was
        dropped.
        """
        context_manager = engine.context_manager
        config_manager = engine.config_manager

        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            browsed = workspace / "shots" / "sh010"
            browsed.mkdir(parents=True)
            original = config_manager.workspace_path
            config_manager.workspace_path = workspace
            try:
                result = engine.handle_request(
                    SetWorkflowContextRequest(display_name="Untitled", working_directory=str(browsed))
                )
                assert isinstance(result, SetWorkflowContextSuccess)

                with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
                    self._resolve_outputs(engine)

                warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
                assert not warnings, f"Expected no degradation warnings but got: {[r.getMessage() for r in warnings]}"
            finally:
                config_manager.workspace_path = original
                while context_manager.has_current_workflow():
                    context_manager.pop_workflow()

    def test_get_working_directory_requires_a_workflow(self, engine: Engine) -> None:
        """Asking outside a workflow context is an error, matching the other context accessors."""
        context_manager = engine.context_manager

        with pytest.raises(context_manager.NoActiveWorkflowError):
            context_manager.get_current_workflow_working_directory()


class TestGeneratedWorkflowCode:
    """Tests that generated workflow code uses push_workflow(file_path=__file__)."""

    def test_generated_code_uses_file_path_not_workflow_name(self, engine: Engine) -> None:
        """_generate_workflow_run_prerequisite_code emits push_workflow(file_path=__file__)."""
        from griptape_nodes.retained_mode.managers.workflow_manager import ImportRecorder

        workflow_manager = engine.workflow_manager
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

    def _cleanup(self, engine: Engine) -> None:
        """Tear down any workflow/flow state left over between tests."""
        from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest

        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

    def test_creates_workflow_and_flow_from_cold_start(self, engine: Engine) -> None:
        from griptape_nodes.retained_mode.events.context_events import (
            EnsureWorkflowAndFlowRequest,
            EnsureWorkflowAndFlowResultSuccess,
        )

        self._cleanup(engine)
        context_manager = engine.context_manager
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

        self._cleanup(engine)

    def test_reuses_existing_workflow_and_flow(self, engine: Engine) -> None:
        from griptape_nodes.retained_mode.events.context_events import (
            EnsureWorkflowAndFlowRequest,
            EnsureWorkflowAndFlowResultSuccess,
        )

        self._cleanup(engine)
        context_manager = engine.context_manager

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

        self._cleanup(engine)

    def test_auto_generates_workflow_name_when_none_given(self, engine: Engine) -> None:
        from griptape_nodes.node_library.workflow_registry import WorkflowRegistry
        from griptape_nodes.retained_mode.events.context_events import (
            EnsureWorkflowAndFlowRequest,
            EnsureWorkflowAndFlowResultSuccess,
        )

        self._cleanup(engine)
        context_manager = engine.context_manager

        result = context_manager.on_ensure_workflow_and_flow_request(EnsureWorkflowAndFlowRequest())

        assert isinstance(result, EnsureWorkflowAndFlowResultSuccess)
        assert result.workflow_name.startswith(WorkflowRegistry.UNSAVED_KEY_PREFIX)
        assert WorkflowRegistry.has_workflow_with_name(result.workflow_name)
        assert result.created_workflow is True
        assert result.created_flow is True

        self._cleanup(engine)


class TestCurrentWorkflowChangedNotification:
    """Tests for the CurrentWorkflowChanged app event ContextManager broadcasts."""

    @staticmethod
    def _notified_workflow_names(put_event: Mock) -> list[str | None]:
        """The workflow_name off every CurrentWorkflowChanged put on the queue, in order."""
        names: list[str | None] = []
        for put_call in put_event.call_args_list:
            event = put_call.args[0]
            if isinstance(event, AppEvent) and isinstance(event.payload, CurrentWorkflowChanged):
                names.append(event.payload.workflow_name)
        return names

    def test_push_workflow_notifies_with_the_new_name(self, engine: Engine) -> None:
        """Entering a workflow tells clients which one is now current."""
        context_manager = engine.context_manager

        with patch.object(engine.event_manager, "put_event", Mock()) as put_event:
            context_manager.push_workflow(workflow_name="opened_workflow")

        assert self._notified_workflow_names(put_event) == ["opened_workflow"]

        context_manager.pop_workflow()

    def test_pop_workflow_notifies_with_none_when_nothing_is_left(self, engine: Engine) -> None:
        """Leaving the last workflow tells clients the engine has none, so no client shows a stale title."""
        context_manager = engine.context_manager
        context_manager.push_workflow(workflow_name="closing_workflow")

        with patch.object(engine.event_manager, "put_event", Mock()) as put_event:
            context_manager.pop_workflow()

        assert self._notified_workflow_names(put_event) == [None]

    def test_pop_workflow_notifies_with_the_workflow_underneath(self, engine: Engine) -> None:
        """Popping a nested workflow reports the one it uncovered, not None."""
        context_manager = engine.context_manager
        context_manager.push_workflow(workflow_name="outer_workflow")
        context_manager.push_workflow(workflow_name="inner_workflow")

        with patch.object(engine.event_manager, "put_event", Mock()) as put_event:
            context_manager.pop_workflow()

        assert self._notified_workflow_names(put_event) == ["outer_workflow"]

        context_manager.pop_workflow()

    def test_re_entering_the_same_workflow_does_not_notify(self, engine: Engine) -> None:
        """Pushing the workflow that is already current is not a change any client acts on."""
        context_manager = engine.context_manager
        context_manager.push_workflow(workflow_name="same_workflow")

        with patch.object(engine.event_manager, "put_event", Mock()) as put_event:
            context_manager.push_workflow(workflow_name="same_workflow")

        assert self._notified_workflow_names(put_event) == []

        context_manager.pop_workflow()
        context_manager.pop_workflow()

    def test_set_current_workflow_name_notifies(self, engine: Engine) -> None:
        """Renaming the open workflow through this primitive reports its new registry key.

        This is the name-only primitive; every handler that renames a workflow also moves the
        file behind it and so goes through `rekey_workflow` instead (pinned below, and at the
        handler level in test_workflow_manager.py). Kept notifying because a name change on its
        own is still a change clients have to hear about.
        """
        context_manager = engine.context_manager
        context_manager.push_workflow(workflow_name="before_rename")

        with patch.object(engine.event_manager, "put_event", Mock()) as put_event:
            context_manager.set_current_workflow_name("after_rename")

        assert self._notified_workflow_names(put_event) == ["after_rename"]

        context_manager.pop_workflow()

    def test_set_current_workflow_name_to_the_same_name_does_not_notify(self, engine: Engine) -> None:
        """Saving over a workflow under its existing key changes nothing clients need to hear about."""
        context_manager = engine.context_manager
        context_manager.push_workflow(workflow_name="unchanged_name")

        with patch.object(engine.event_manager, "put_event", Mock()) as put_event:
            context_manager.set_current_workflow_name("unchanged_name")

        assert self._notified_workflow_names(put_event) == []

        context_manager.pop_workflow()

    def test_set_current_workflow_file_path_does_not_notify(self, engine: Engine) -> None:
        """The retained path is not part of this signal, so moving the file alone stays quiet."""
        context_manager = engine.context_manager
        context_manager.push_workflow(workflow_name="path_only_change")

        with patch.object(engine.event_manager, "put_event", Mock()) as put_event:
            context_manager.set_current_workflow_file_path("/somewhere/else/path_only_change.py")

        assert self._notified_workflow_names(put_event) == []

        context_manager.pop_workflow()

    def test_set_workflow_context_request_notifies(self, engine: Engine) -> None:
        """The request path notifies too, since it pushes through the same primitive."""
        context_manager = engine.context_manager

        with patch.object(engine.event_manager, "put_event", Mock()) as put_event:
            result = context_manager.on_set_workflow_context_request(
                SetWorkflowContextRequest(workflow_name="requested_workflow")
            )

        assert isinstance(result, SetWorkflowContextSuccess)
        assert self._notified_workflow_names(put_event) == ["requested_workflow"]

        context_manager.pop_workflow()

    def test_dedupe_is_against_the_last_notification_not_everything_ever_seen(self, engine: Engine) -> None:
        """Leaving a workflow and coming back is a real change, even though the name repeats.

        Pins the meaning of the dedupe field: it holds the name clients were told last, not a
        set of names already broadcast. Were it the latter, an artist reopening the workflow
        they just closed would get no event and every editor would keep showing an empty canvas.
        """
        context_manager = engine.context_manager

        with patch.object(engine.event_manager, "put_event", Mock()) as put_event:
            context_manager.push_workflow(workflow_name="revisited_workflow")
            context_manager.pop_workflow()
            context_manager.push_workflow(workflow_name="revisited_workflow")

        assert self._notified_workflow_names(put_event) == ["revisited_workflow", None, "revisited_workflow"]

        context_manager.pop_workflow()

    def test_rekey_workflow_notifies_when_the_current_workflow_is_the_one_rekeyed(self, engine: Engine) -> None:
        """The first save of a scratch workflow changes its key, and clients hear the new one."""
        context_manager = engine.context_manager
        context_manager.push_workflow(workflow_name="unsaved:abc-123")

        with patch.object(engine.event_manager, "put_event", Mock()) as put_event:
            context_manager.rekey_workflow(
                old_name="unsaved:abc-123", new_name="my_flow", new_file_path="/workspace/my_flow.py"
            )

        assert self._notified_workflow_names(put_event) == ["my_flow"]
        assert context_manager.get_current_workflow_name() == "my_flow"
        # The retained path moves with the key; `workflow_dir` answers from it.
        assert context_manager.get_current_workflow_file_path() == "/workspace/my_flow.py"

        context_manager.pop_workflow()

    def test_rekey_workflow_stays_quiet_when_only_a_buried_entry_matches(self, engine: Engine) -> None:
        """Saving a workflow that something else is nested on top of does not change what is current."""
        context_manager = engine.context_manager
        context_manager.push_workflow(workflow_name="unsaved:buried")
        context_manager.push_workflow(workflow_name="nested_on_top")

        with patch.object(engine.event_manager, "put_event", Mock()) as put_event:
            context_manager.rekey_workflow(
                old_name="unsaved:buried", new_name="saved_below", new_file_path="/workspace/saved_below.py"
            )

        assert self._notified_workflow_names(put_event) == []
        assert context_manager.get_current_workflow_name() == "nested_on_top"
        # Buried or not, the entry was still rekeyed -- popping back to it uncovers the new name.
        context_manager.pop_workflow()
        assert context_manager.get_current_workflow_name() == "saved_below"

        context_manager.pop_workflow()

    def test_rekey_workflow_matching_nothing_notifies_nothing(self, engine: Engine) -> None:
        """Saving a workflow that is not in context at all is not a context change."""
        context_manager = engine.context_manager
        context_manager.push_workflow(workflow_name="untouched_workflow")

        with patch.object(engine.event_manager, "put_event", Mock()) as put_event:
            context_manager.rekey_workflow(old_name="not_in_context", new_name="whatever", new_file_path=None)

        assert self._notified_workflow_names(put_event) == []
        assert context_manager.get_current_workflow_name() == "untouched_workflow"

        context_manager.pop_workflow()

    def test_a_switch_that_could_not_be_sent_is_still_owed(self, engine: Engine) -> None:
        """An enqueue that fails leaves the transition unsent, so the next one re-sends it.

        `put_event` hands cross-thread events to the engine's event loop, which can be gone by
        the time a context mutation runs. If the dedupe field were advanced before that call, the
        workflow whose event was lost would be the one workflow clients are never told about --
        every later notification would compare against a name that never left the engine.
        """
        context_manager = engine.context_manager

        with (
            patch.object(engine.event_manager, "put_event", Mock(side_effect=RuntimeError("event loop is gone"))),
            pytest.raises(RuntimeError),
        ):
            context_manager.push_workflow(workflow_name="never_announced")

        # The push itself landed; only the announcement was lost.
        assert context_manager.get_current_workflow_name() == "never_announced"

        with patch.object(engine.event_manager, "put_event", Mock()) as put_event:
            context_manager.set_current_workflow_name("never_announced")

        assert self._notified_workflow_names(put_event) == ["never_announced"]

        context_manager.pop_workflow()


class TestSetWorkflowContextAlreadyInContextMessage:
    """The already-in-context refusal has to point callers somewhere that actually works."""

    def test_failure_names_run_workflow_from_registry_as_the_way_to_open(self, engine: Engine) -> None:
        """RunWorkflowFromRegistry is the non-destructive route, so the message leads with it."""
        context_manager = engine.context_manager
        context_manager.push_workflow(workflow_name="already_open")

        result = context_manager.on_set_workflow_context_request(
            SetWorkflowContextRequest(workflow_name="wants_to_open")
        )

        assert isinstance(result, SetWorkflowContextFailure)
        details = str(result.result_details)
        assert "RunWorkflowFromRegistry" in details
        # The destructive route is still named, but it is no longer the only thing on offer.
        assert "ClearAllObjectState" in details
        assert details.index("RunWorkflowFromRegistry") < details.index("ClearAllObjectState")

        context_manager.pop_workflow()
