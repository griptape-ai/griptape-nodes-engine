"""Tests for ProjectManager macro event handlers."""

import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest

if TYPE_CHECKING:
    from griptape_nodes.common.project_templates import ProjectValidationInfo
    from griptape_nodes.common.project_templates.directory import PerPlatformPathMacro
    from griptape_nodes.common.project_templates.loader import ProjectOverlayData
    from griptape_nodes.common.project_templates.project_path import PerPlatformProjectPath
    from griptape_nodes.retained_mode.events.project_events import LoadProjectTemplateResultFailure

from griptape_nodes.common.macro_parser import MacroMatchFailureReason
from griptape_nodes.common.project_templates import DEFAULT_PROJECT_TEMPLATE
from griptape_nodes.files.path_utils import canonicalize_for_identity
from griptape_nodes.retained_mode.events.project_events import (
    AttemptMapAbsolutePathToProjectRequest,
    AttemptMapAbsolutePathToProjectResultSuccess,
    AttemptMatchPathAgainstMacroRequest,
    AttemptMatchPathAgainstMacroResultSuccess,
    GetPathForMacroRequest,
    GetPathForMacroResultFailure,
    GetPathForMacroResultSuccess,
    GetStateForMacroRequest,
    GetStateForMacroResultFailure,
    GetStateForMacroResultSuccess,
    PathResolutionFailureReason,
)
from griptape_nodes.retained_mode.managers.project_manager import ProjectManager


class TestProjectManagerMacroHandlers:
    """Test ProjectManager macro-related event handlers."""

    @pytest.fixture
    def project_manager(self) -> ProjectManager:
        """Create a ProjectManager instance for testing."""
        mock_config = Mock()
        mock_secrets = Mock()
        mock_event_manager = Mock()
        return ProjectManager(mock_event_manager, mock_config, mock_secrets)

    def test_match_path_against_macro_success(self, project_manager: ProjectManager) -> None:
        """Test AttemptMatchPathAgainstMacro successfully matches path."""
        from griptape_nodes.common.macro_parser import ParsedMacro

        parsed_macro = ParsedMacro("{inputs}/{file_name}.{ext}")

        request = AttemptMatchPathAgainstMacroRequest(
            parsed_macro=parsed_macro,
            file_path="inputs/render.png",
            known_variables={"inputs": "inputs"},
        )

        result = project_manager.on_match_path_against_macro_request(request)

        assert isinstance(result, AttemptMatchPathAgainstMacroResultSuccess)
        assert result.match_failure is None
        assert result.extracted_variables == {"inputs": "inputs", "file_name": "render", "ext": "png"}

    def test_match_path_mismatch(self, project_manager: ProjectManager) -> None:
        """Test that AttemptMatchPathAgainstMacro returns success with match_failure when path doesn't match."""
        from griptape_nodes.common.macro_parser import ParsedMacro

        known_vars: dict[str, str | int] = {"inputs": "outputs", "ext": "png"}
        parsed_macro = ParsedMacro("{inputs}/{file_name}.{ext}")

        request = AttemptMatchPathAgainstMacroRequest(
            parsed_macro=parsed_macro,
            file_path="wrong_folder/render.png",
            known_variables=known_vars,
        )

        result = project_manager.on_match_path_against_macro_request(request)

        assert isinstance(result, AttemptMatchPathAgainstMacroResultSuccess)
        assert result.match_failure is not None
        assert result.extracted_variables is None
        assert result.match_failure.failure_reason == MacroMatchFailureReason.STATIC_TEXT_MISMATCH
        assert result.match_failure.known_variables_used == known_vars

    def test_match_path_empty_known_variables(self, project_manager: ProjectManager) -> None:
        """Test AttemptMatchPathAgainstMacro with empty known_variables."""
        from griptape_nodes.common.macro_parser import ParsedMacro

        parsed_macro = ParsedMacro("{file_name}")

        request = AttemptMatchPathAgainstMacroRequest(
            parsed_macro=parsed_macro,
            file_path="test.txt",
            known_variables={},
        )

        result = project_manager.on_match_path_against_macro_request(request)

        assert isinstance(result, AttemptMatchPathAgainstMacroResultSuccess)
        assert result.match_failure is None
        assert result.extracted_variables == {"file_name": "test.txt"}

    def test_match_path_auto_resolve_off_treats_builtins_as_unknowns(self, project_manager: ProjectManager) -> None:
        """Default ``auto_resolve_builtins=False`` keeps the handler from anchoring builtins.

        ``extract_variables`` is greedy: without an anchor value for
        ``workspace_dir``, the matcher consumes it as the empty string and
        ``file_name_base`` absorbs the entire leading path. The whole-string
        match technically "succeeds", but the resulting dict is structurally
        wrong — the workspace boundary was lost. This is precisely why
        workflow_manager opts into auto-resolution, and why the default has
        to leave existing callers' strict contract untouched.
        """
        from griptape_nodes.common.macro_parser import ParsedMacro

        parsed_macro = ParsedMacro("{workspace_dir}/{file_name_base}.{file_extension}")

        request = AttemptMatchPathAgainstMacroRequest(
            parsed_macro=parsed_macro,
            file_path="/projects/demo/my_workflow.py",
            known_variables={},
            # auto_resolve_builtins defaults to False — handler must NOT inject builtins.
        )

        result = project_manager.on_match_path_against_macro_request(request)

        assert isinstance(result, AttemptMatchPathAgainstMacroResultSuccess)
        assert result.match_failure is None
        # Greedy match with no workspace anchor: workspace_dir defaults to empty,
        # file_name_base absorbs the leading path. Documents the wrong-but-syntactically-valid
        # extraction that the auto-resolve flag exists to prevent.
        assert result.extracted_variables == {
            "workspace_dir": "",
            "file_name_base": "projects/demo/my_workflow",
            "file_extension": "py",
        }

    def test_match_path_auto_resolve_on_supplies_builtin_anchors(self, tmp_path: Path) -> None:
        """``auto_resolve_builtins=True`` lets the handler resolve ``{workspace_dir}`` itself.

        Drives the handler through ``handle_request`` against a real loaded project
        so the builtin resolver can actually return a workspace value. Without the
        flag, this same macro/path pair fails (see ``test_match_path_auto_resolve_off``);
        with it, the handler injects ``workspace_dir`` and the match succeeds.
        """
        from griptape_nodes.common.macro_parser import ParsedMacro
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
            SetCurrentProjectRequest,
        )
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        workspace = tmp_path.resolve()
        original_workspace = GriptapeNodes.ConfigManager().workspace_path
        project_yml = workspace / "project_template.yml"
        project_yml.write_text(DEFAULT_PROJECT_TEMPLATE.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE))
        load_result = GriptapeNodes.handle_request(LoadProjectTemplateRequest(project_path=project_yml))
        assert isinstance(load_result, LoadProjectTemplateResultSuccess)
        GriptapeNodes.handle_request(SetCurrentProjectRequest(project_id=load_result.project_id))
        # SetCurrentProjectRequest re-derives workspace_path; force it back.
        GriptapeNodes.ConfigManager().workspace_path = workspace

        try:
            # Macro templates use forward-slash separators (the cross-platform
            # convention). On Windows the OS-native absolute path comes back
            # with backslashes; normalize to POSIX so the static-text
            # comparison between `{workspace_dir}` and the next segment lines
            # up regardless of OS. The match handler POSIX-normalizes its
            # auto-resolved directory builtins, so both sides agree on
            # separator without the caller having to inject workspace_dir.
            parsed_macro = ParsedMacro("{workspace_dir}/{file_name_base}.{file_extension}")
            absolute_path = (workspace / "my_workflow.py").as_posix()

            request = AttemptMatchPathAgainstMacroRequest(
                parsed_macro=parsed_macro,
                file_path=absolute_path,
                known_variables={},
                auto_resolve_builtins=True,
            )

            result = GriptapeNodes.handle_request(request)

            assert isinstance(result, AttemptMatchPathAgainstMacroResultSuccess)
            assert result.match_failure is None
            assert result.extracted_variables is not None
            assert result.extracted_variables.get("file_name_base") == "my_workflow"
            assert result.extracted_variables.get("file_extension") == "py"
            # workspace_dir was supplied by the handler — auto-resolution made the match possible.
            assert "workspace_dir" in result.extracted_variables
        finally:
            GriptapeNodes.handle_request(SetCurrentProjectRequest(project_id=None))
            GriptapeNodes.ConfigManager().workspace_path = original_workspace

    def test_match_path_auto_resolve_rejects_conflicting_caller_override(self, tmp_path: Path) -> None:
        """Caller-supplied builtin overrides that disagree with the project are rejected.

        Pins the shared "no silent override of builtins" policy: every handler
        that mixes user-supplied variables with project-derived builtins refuses
        to let the caller silently shadow the builtin. The match handler hits the
        same conflict-detection helper as ``GetPathForMacroRequest`` and
        ``GetStateForMacroRequest``, so the rejection shape is consistent across
        the public API surface.
        """
        from griptape_nodes.common.macro_parser import ParsedMacro
        from griptape_nodes.retained_mode.events.project_events import (
            AttemptMatchPathAgainstMacroResultFailure,
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
            SetCurrentProjectRequest,
        )
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        workspace = tmp_path.resolve()
        original_workspace = GriptapeNodes.ConfigManager().workspace_path
        project_yml = workspace / "project_template.yml"
        project_yml.write_text(DEFAULT_PROJECT_TEMPLATE.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE))
        load_result = GriptapeNodes.handle_request(LoadProjectTemplateRequest(project_path=project_yml))
        assert isinstance(load_result, LoadProjectTemplateResultSuccess)
        GriptapeNodes.handle_request(SetCurrentProjectRequest(project_id=load_result.project_id))
        GriptapeNodes.ConfigManager().workspace_path = workspace

        try:
            # Caller asserts workspace_dir is "/elsewhere" — different from the real workspace.
            # Under the unified policy this is a contract violation, not a silent override.
            parsed_macro = ParsedMacro("{workspace_dir}/{file_name_base}.{file_extension}")
            request = AttemptMatchPathAgainstMacroRequest(
                parsed_macro=parsed_macro,
                file_path="/elsewhere/my_workflow.py",
                known_variables={"workspace_dir": "/elsewhere"},
                auto_resolve_builtins=True,
            )

            result = GriptapeNodes.handle_request(request)

            # Hard failure (not a match-failure result_success): caller violated
            # the "no override of builtins" contract that all macro handlers share.
            assert isinstance(result, AttemptMatchPathAgainstMacroResultFailure)
            assert "workspace_dir" in str(result.result_details)
        finally:
            GriptapeNodes.handle_request(SetCurrentProjectRequest(project_id=None))
            GriptapeNodes.ConfigManager().workspace_path = original_workspace

    @patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes")
    def test_match_path_auto_resolve_supplies_non_directory_builtin_verbatim(
        self,
        mock_griptape_nodes: Mock,
        project_manager: ProjectManager,
        tmp_path: Path,
    ) -> None:
        """Auto-resolved non-directory builtins pass through without POSIX normalization.

        ``on_attempt_match_path_against_macro_request`` POSIX-normalizes
        auto-resolved *directory* builtins so reverse-match works on Windows
        (macros use forward-slash separators). Non-directory builtins like
        ``workflow_name`` are just strings; the normalization loop gates on
        ``builtin_info.is_directory`` and skips them, so their value reaches
        the extractor verbatim.

        Existing coverage exercises directory builtins only
        (``test_match_path_auto_resolve_on_supplies_builtin_anchors``,
        ``test_match_path_auto_resolve_rejects_conflicting_caller_override``).
        This test locks the non-directory branch — a future "always
        normalize" simplification would silently corrupt workflow names.
        """
        from griptape_nodes.common.macro_parser import ParsedMacro
        from griptape_nodes.common.project_templates.validation import (
            ProjectValidationInfo,
            ProjectValidationStatus,
        )
        from griptape_nodes.retained_mode.managers.project_manager import (
            DEFAULT_PROJECT_TEMPLATE as DEFAULT_TEMPLATE_FROM_MODULE,
        )
        from griptape_nodes.retained_mode.managers.project_manager import (
            SYSTEM_DEFAULTS_KEY,
            ProjectInfo,
        )

        # A current workflow named `My Cool Workflow` — the space is deliberate:
        # a POSIX-normalization pass would leave the space alone, but if a future
        # refactor pushed non-directory builtins through `Path(...).as_posix()`
        # by mistake, backslash handling could mangle the value. Distinctive
        # enough to catch that class of regression.
        mock_context_manager = Mock()
        mock_context_manager.has_current_workflow.return_value = True
        mock_context_manager.get_current_workflow_name.return_value = "My Cool Workflow"
        mock_griptape_nodes.ContextManager.return_value = mock_context_manager

        cast("Mock", project_manager._config_manager).workspace_path = tmp_path.resolve()

        # Register a synthetic project so `on_get_current_project_request` succeeds
        # and the auto-resolve branch actually runs.
        project_info = ProjectInfo(
            project_id=SYSTEM_DEFAULTS_KEY,
            template=DEFAULT_TEMPLATE_FROM_MODULE,
            validation=ProjectValidationInfo(status=ProjectValidationStatus.GOOD),
            project_file_path=tmp_path / "synthetic.yml",
            project_base_dir=tmp_path,
            parsed_situation_schemas={},
            parsed_directory_schemas={},
        )
        project_manager._successfully_loaded_project_templates[SYSTEM_DEFAULTS_KEY] = project_info
        project_manager._current_project_id = SYSTEM_DEFAULTS_KEY

        parsed_macro = ParsedMacro("{workflow_name}/render.png")
        request = AttemptMatchPathAgainstMacroRequest(
            parsed_macro=parsed_macro,
            file_path="My Cool Workflow/render.png",
            known_variables={},
            auto_resolve_builtins=True,
        )

        result = project_manager.on_match_path_against_macro_request(request)

        assert isinstance(result, AttemptMatchPathAgainstMacroResultSuccess)
        assert result.match_failure is None
        assert result.extracted_variables is not None
        # workflow_name was supplied by auto-resolve AND passed through unchanged —
        # the POSIX-normalization loop's `is_directory` gate skipped it.
        assert result.extracted_variables["workflow_name"] == "My Cool Workflow"

    def test_resolve_builtins_into_bag_flags_directory_path_conflict(
        self, project_manager: ProjectManager, tmp_path: Path
    ) -> None:
        """Directory builtins compare via ``resolve_path_safely``; disagreeing paths conflict.

        Companion to
        ``test_resolve_builtins_into_bag_flags_non_directory_string_conflict``
        below. Directory builtins (``workspace_dir``, ``project_dir``, …) run
        both sides through ``resolve_path_safely`` before comparing, so
        different spellings of the same path don't false-alarm. This test
        pins the branch where the paths are genuinely different, which the
        rule *must* catch.

        Currently this branch is only hit transitively via
        ``test_match_path_auto_resolve_rejects_conflicting_caller_override``;
        the direct-unit test locks it against a "just use string compare for
        everything" simplification.
        """
        from griptape_nodes.common.project_templates.validation import (
            ProjectValidationInfo,
            ProjectValidationStatus,
        )
        from griptape_nodes.retained_mode.managers.project_manager import (
            DEFAULT_PROJECT_TEMPLATE as DEFAULT_TEMPLATE_FROM_MODULE,
        )
        from griptape_nodes.retained_mode.managers.project_manager import (
            SYSTEM_DEFAULTS_KEY,
            ProjectInfo,
        )

        # The resolved builtin for `workspace_dir` is a real path so the
        # `resolve_path_safely` comparison has something concrete to work with.
        cast("Mock", project_manager._config_manager).workspace_path = tmp_path.resolve()

        project_info = ProjectInfo(
            project_id=SYSTEM_DEFAULTS_KEY,
            template=DEFAULT_TEMPLATE_FROM_MODULE,
            validation=ProjectValidationInfo(status=ProjectValidationStatus.GOOD),
            project_file_path=tmp_path / "synthetic.yml",
            project_base_dir=tmp_path,
            parsed_situation_schemas={},
            parsed_directory_schemas={},
        )

        # Caller asserts a directory that isn't the workspace.
        bag = cast("dict[str, Any]", {"workspace_dir": "/completely/different/path"})
        result = project_manager._resolve_builtins_into_bag(bag, ["workspace_dir"], project_info)

        # `resolve_path_safely` normalizes both sides and picks up the disagreement.
        assert "workspace_dir" in result.conflicts
        assert result.unavailable == {}

    @patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes")
    def test_resolve_builtins_into_bag_records_unavailable_when_no_workflow(
        self,
        mock_griptape_nodes: Mock,
        project_manager: ProjectManager,
        tmp_path: Path,
    ) -> None:
        """Builtins whose resolver raises are recorded in ``unavailable``, not treated as conflicts.

        ``_get_builtin_variable_value`` raises ``RuntimeError('No current
        workflow')`` for ``workflow_name`` / ``workflow_dir`` when no
        workflow context is active. The helper must catch that and add the
        name to ``unavailable`` (with the exception attached) so callers can
        report the missing precondition. Currently this branch is untested.
        """
        from griptape_nodes.common.project_templates.validation import (
            ProjectValidationInfo,
            ProjectValidationStatus,
        )
        from griptape_nodes.retained_mode.managers.project_manager import (
            DEFAULT_PROJECT_TEMPLATE as DEFAULT_TEMPLATE_FROM_MODULE,
        )
        from griptape_nodes.retained_mode.managers.project_manager import (
            SYSTEM_DEFAULTS_KEY,
            ProjectInfo,
        )

        # No current workflow → workflow_name / workflow_dir resolvers raise RuntimeError.
        mock_context_manager = Mock()
        mock_context_manager.has_current_workflow.return_value = False
        mock_griptape_nodes.ContextManager.return_value = mock_context_manager

        cast("Mock", project_manager._config_manager).workspace_path = tmp_path.resolve()

        project_info = ProjectInfo(
            project_id=SYSTEM_DEFAULTS_KEY,
            template=DEFAULT_TEMPLATE_FROM_MODULE,
            validation=ProjectValidationInfo(status=ProjectValidationStatus.GOOD),
            project_file_path=tmp_path / "synthetic.yml",
            project_base_dir=tmp_path,
            parsed_situation_schemas={},
            parsed_directory_schemas={},
        )

        bag = cast("dict[str, Any]", {})
        result = project_manager._resolve_builtins_into_bag(
            bag,
            ["workflow_name", "workflow_dir", "workspace_dir"],
            project_info,
        )

        # workflow_* resolvers both raised — recorded as unavailable with the exception attached.
        assert "workflow_name" in result.unavailable
        assert "workflow_dir" in result.unavailable
        assert isinstance(result.unavailable["workflow_name"], RuntimeError)
        assert "No current workflow" in str(result.unavailable["workflow_name"])
        # workspace_dir resolves fine → not in unavailable, and injected into the bag.
        assert "workspace_dir" not in result.unavailable
        assert bag["workspace_dir"] == str(tmp_path.resolve())
        # Unavailable is not a conflict — callers distinguish the two.
        assert result.conflicts == set()

    def test_resolve_builtins_into_bag_flags_non_directory_string_conflict(
        self, project_manager: ProjectManager, tmp_path: Path
    ) -> None:
        """Non-directory builtins compare as strings; mismatched values are flagged as conflicts.

        Directory builtins (``workspace_dir`` etc.) compare via
        ``resolve_path_safely`` so two spellings of the same path don't
        false-alarm; non-directory builtins (``workflow_name``,
        ``project_name``, etc.) compare verbatim. Pins the string-compare
        branch of ``_resolve_builtins_into_bag``: a caller-supplied
        ``static_files_dir`` that disagrees with the resolved value
        is reported as a conflict.
        """
        from griptape_nodes.common.project_templates.validation import (
            ProjectValidationInfo,
            ProjectValidationStatus,
        )
        from griptape_nodes.retained_mode.managers.project_manager import (
            DEFAULT_PROJECT_TEMPLATE as DEFAULT_TEMPLATE_FROM_MODULE,
        )
        from griptape_nodes.retained_mode.managers.project_manager import (
            SYSTEM_DEFAULTS_KEY,
            ProjectInfo,
        )

        # The mock config from the fixture returns a Mock for any config key —
        # we need a concrete string so the conflict comparison is meaningful.
        cast("Mock", project_manager._config_manager).get_config_value.return_value = "staticfiles"

        project_info = ProjectInfo(
            project_id=SYSTEM_DEFAULTS_KEY,
            template=DEFAULT_TEMPLATE_FROM_MODULE,
            validation=ProjectValidationInfo(status=ProjectValidationStatus.GOOD),
            project_file_path=tmp_path / "synthetic.yml",
            project_base_dir=tmp_path,
            parsed_situation_schemas={},
            parsed_directory_schemas={},
        )

        # Caller asserts a value that disagrees with the resolved builtin.
        bag = cast("dict[str, Any]", {"static_files_dir": "user-overridden-value"})
        result = project_manager._resolve_builtins_into_bag(bag, ["static_files_dir"], project_info)

        # String compare picked up the disagreement → conflict recorded.
        assert "static_files_dir" in result.conflicts
        # No values were available beyond what was supplied / the unresolved branch.
        assert result.unavailable == {}


class TestProjectManagerInitialization:
    """Test ProjectManager initialization and state."""

    def test_project_manager_initializes_with_system_defaults(self) -> None:
        """System defaults loaded eagerly in __init__.

        Project-aware requests must work before AppInitializationComplete fires
        because CLI workflow scripts construct nodes at module import time,
        before the event is broadcast.
        """
        from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY

        mock_config = Mock()
        mock_secrets = Mock()
        mock_event_manager = Mock()

        pm = ProjectManager(mock_event_manager, mock_config, mock_secrets)

        assert pm._registered_template_status == {}
        assert pm._current_project_id == SYSTEM_DEFAULTS_KEY
        assert SYSTEM_DEFAULTS_KEY in pm._successfully_loaded_project_templates

    def test_project_manager_stores_manager_references(self) -> None:
        """Test ProjectManager stores config and secrets manager references."""
        mock_config = Mock()
        mock_secrets = Mock()
        mock_event_manager = Mock()

        pm = ProjectManager(mock_event_manager, mock_config, mock_secrets)

        assert pm._config_manager is mock_config
        assert pm._secrets_manager is mock_secrets


class TestProjectManagerBuiltinVariables:
    """Test ProjectManager builtin variable resolution."""

    @pytest.fixture
    def project_manager_with_template(self) -> ProjectManager:
        """Create a ProjectManager with system defaults loaded."""
        from griptape_nodes.common.project_templates import ProjectValidationInfo, ProjectValidationStatus
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.retained_mode.managers.project_manager import ProjectInfo

        mock_config = Mock()
        mock_config.workspace_path = Path("/workspace")
        mock_secrets = Mock()
        mock_event_manager = Mock()
        pm = ProjectManager(mock_event_manager, mock_config, mock_secrets)

        project_path = Path("/test/project.yml")
        project_id = str(project_path)

        # Parse macros first
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        situation_schemas = pm._parse_situation_macros(DEFAULT_PROJECT_TEMPLATE.situations, validation)
        directory_schemas = pm._parse_directory_macros(DEFAULT_PROJECT_TEMPLATE.directories, validation)

        # Create ProjectInfo with fully populated caches
        project_info = ProjectInfo(
            project_id=project_id,
            project_file_path=project_path,
            project_base_dir=project_path.parent,
            template=DEFAULT_PROJECT_TEMPLATE,
            validation=validation,
            parsed_situation_schemas=situation_schemas,
            parsed_directory_schemas=directory_schemas,
        )

        # Set up new consolidated dict
        pm._successfully_loaded_project_templates[project_id] = project_info
        pm._current_project_id = project_id

        return pm

    def test_builtin_project_dir_resolves_correctly(self, project_manager_with_template: ProjectManager) -> None:
        """Test that {project_dir} builtin resolves to project_path.parent."""
        from griptape_nodes.common.macro_parser import ParsedMacro

        parsed_macro = ParsedMacro("{project_dir}/output.txt")

        request = GetPathForMacroRequest(parsed_macro=parsed_macro, variables={})

        result = project_manager_with_template.on_get_path_for_macro_request(request)

        assert isinstance(result, GetPathForMacroResultSuccess)
        assert result.resolved_path == Path("/test/output.txt")

    def test_builtin_workspace_dir_resolves_correctly(self, project_manager_with_template: ProjectManager) -> None:
        """Test that {workspace_dir} builtin resolves from ConfigManager."""
        from griptape_nodes.common.macro_parser import ParsedMacro

        cast("Mock", project_manager_with_template._config_manager).workspace_path = Path("/workspace")

        parsed_macro = ParsedMacro("{workspace_dir}/output.txt")

        request = GetPathForMacroRequest(parsed_macro=parsed_macro, variables={})

        result = project_manager_with_template.on_get_path_for_macro_request(request)

        assert isinstance(result, GetPathForMacroResultSuccess)
        assert result.resolved_path == Path("/workspace/output.txt")

    @patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes")
    def test_builtin_workflow_name_resolves_correctly(
        self, mock_griptape_nodes: Mock, project_manager_with_template: ProjectManager
    ) -> None:
        """Test that {workflow_name} builtin resolves from ContextManager."""
        from griptape_nodes.common.macro_parser import ParsedMacro

        cast("Mock", project_manager_with_template._config_manager).workspace_path = Path("/workspace")

        mock_context_manager = Mock()
        mock_context_manager.has_current_workflow.return_value = True
        mock_context_manager.get_current_workflow_name.return_value = "my_workflow"
        mock_griptape_nodes.ContextManager.return_value = mock_context_manager

        parsed_macro = ParsedMacro("{workflow_name}_output.txt")

        request = GetPathForMacroRequest(parsed_macro=parsed_macro, variables={})

        result = project_manager_with_template.on_get_path_for_macro_request(request)

        assert isinstance(result, GetPathForMacroResultSuccess)
        assert result.resolved_path == Path("my_workflow_output.txt")
        mock_context_manager.has_current_workflow.assert_called_once()
        mock_context_manager.get_current_workflow_name.assert_called_once()

    @patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes")
    def test_builtin_workflow_name_no_current_workflow(
        self, mock_griptape_nodes: Mock, project_manager_with_template: ProjectManager
    ) -> None:
        """Test that {workflow_name} raises RuntimeError when no current workflow."""
        from griptape_nodes.common.macro_parser import ParsedMacro

        mock_context_manager = Mock()
        mock_context_manager.has_current_workflow.return_value = False
        mock_griptape_nodes.ContextManager.return_value = mock_context_manager

        parsed_macro = ParsedMacro("{workflow_name}_output.txt")

        request = GetPathForMacroRequest(parsed_macro=parsed_macro, variables={})

        result = project_manager_with_template.on_get_path_for_macro_request(request)

        assert isinstance(result, GetPathForMacroResultFailure)
        assert result.failure_reason == PathResolutionFailureReason.MACRO_RESOLUTION_ERROR
        from griptape_nodes.retained_mode.events.base_events import ResultDetails

        assert isinstance(result.result_details, ResultDetails)
        assert "No current workflow" in str(result.result_details)

    def test_builtin_project_name_not_implemented(self, project_manager_with_template: ProjectManager) -> None:
        """Test that {project_name} raises NotImplementedError."""
        from griptape_nodes.common.macro_parser import ParsedMacro

        parsed_macro = ParsedMacro("{project_name}/output.txt")

        request = GetPathForMacroRequest(parsed_macro=parsed_macro, variables={})

        result = project_manager_with_template.on_get_path_for_macro_request(request)

        assert isinstance(result, GetPathForMacroResultFailure)
        assert result.failure_reason == PathResolutionFailureReason.MACRO_RESOLUTION_ERROR
        from griptape_nodes.retained_mode.events.base_events import ResultDetails

        assert isinstance(result.result_details, ResultDetails)
        assert "project_name not yet implemented" in str(result.result_details)

    @patch("griptape_nodes.retained_mode.managers.project_manager.WorkflowRegistry")
    @patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes")
    def test_builtin_workflow_dir_resolves_correctly(
        self,
        mock_griptape_nodes: Mock,
        mock_workflow_registry: Mock,
        project_manager_with_template: ProjectManager,
    ) -> None:
        """Test that {workflow_dir} resolves to the workflow file's parent directory."""
        from griptape_nodes.common.macro_parser import ParsedMacro

        mock_context_manager = Mock()
        mock_context_manager.has_current_workflow.return_value = True
        mock_context_manager.get_current_workflow_name.return_value = "my_workflow"
        mock_griptape_nodes.ContextManager.return_value = mock_context_manager

        mock_workflow = Mock()
        mock_workflow.file_path = "my_project/my_workflow.json"
        mock_workflow_registry.get_workflow_by_name.return_value = mock_workflow
        mock_workflow_registry.get_complete_file_path.return_value = "/workspace/my_project/my_workflow.json"

        parsed_macro = ParsedMacro("{workflow_dir}/output.txt")
        request = GetPathForMacroRequest(parsed_macro=parsed_macro, variables={})

        result = project_manager_with_template.on_get_path_for_macro_request(request)

        assert isinstance(result, GetPathForMacroResultSuccess)
        assert result.resolved_path == Path("/workspace/my_project/output.txt")

    @patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes")
    def test_builtin_workflow_dir_no_current_workflow(
        self, mock_griptape_nodes: Mock, project_manager_with_template: ProjectManager
    ) -> None:
        """Test that required {workflow_dir} fails when there is no current workflow."""
        from griptape_nodes.common.macro_parser import ParsedMacro

        mock_context_manager = Mock()
        mock_context_manager.has_current_workflow.return_value = False
        mock_griptape_nodes.ContextManager.return_value = mock_context_manager

        parsed_macro = ParsedMacro("{workflow_dir}/output.txt")
        request = GetPathForMacroRequest(parsed_macro=parsed_macro, variables={})

        result = project_manager_with_template.on_get_path_for_macro_request(request)

        assert isinstance(result, GetPathForMacroResultFailure)
        assert result.failure_reason == PathResolutionFailureReason.MACRO_RESOLUTION_ERROR
        from griptape_nodes.retained_mode.events.base_events import ResultDetails

        assert isinstance(result.result_details, ResultDetails)
        assert "No current workflow" in str(result.result_details)

    @patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes")
    def test_builtin_workflow_dir_optional_skipped_when_no_workflow(
        self, mock_griptape_nodes: Mock, project_manager_with_template: ProjectManager
    ) -> None:
        """Test that optional {workflow_dir?:/} is skipped (not an error) when no current workflow."""
        from griptape_nodes.common.macro_parser import ParsedMacro

        cast("Mock", project_manager_with_template._config_manager).workspace_path = Path("/workspace")

        mock_context_manager = Mock()
        mock_context_manager.has_current_workflow.return_value = False
        mock_griptape_nodes.ContextManager.return_value = mock_context_manager

        parsed_macro = ParsedMacro("{workflow_dir?:/}staticfiles/output.txt")
        request = GetPathForMacroRequest(parsed_macro=parsed_macro, variables={})

        result = project_manager_with_template.on_get_path_for_macro_request(request)

        assert isinstance(result, GetPathForMacroResultSuccess)
        assert result.resolved_path == Path("staticfiles/output.txt")

    @patch("griptape_nodes.retained_mode.managers.project_manager.WorkflowRegistry")
    @patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes")
    def test_builtin_workflow_dir_unregistered_workflow_fails(
        self,
        mock_griptape_nodes: Mock,
        mock_workflow_registry: Mock,
        project_manager_with_template: ProjectManager,
    ) -> None:
        """Test that required {workflow_dir} fails when the workflow exists but is not registered (unsaved)."""
        from griptape_nodes.common.macro_parser import ParsedMacro

        mock_context_manager = Mock()
        mock_context_manager.has_current_workflow.return_value = True
        mock_context_manager.get_current_workflow_name.return_value = "workflow_5"
        mock_griptape_nodes.ContextManager.return_value = mock_context_manager

        mock_workflow_registry.get_workflow_by_name.side_effect = KeyError("workflow_5")

        parsed_macro = ParsedMacro("{workflow_dir}/output.txt")
        request = GetPathForMacroRequest(parsed_macro=parsed_macro, variables={})

        result = project_manager_with_template.on_get_path_for_macro_request(request)

        assert isinstance(result, GetPathForMacroResultFailure)
        assert result.failure_reason == PathResolutionFailureReason.MACRO_RESOLUTION_ERROR
        from griptape_nodes.retained_mode.events.base_events import ResultDetails

        assert isinstance(result.result_details, ResultDetails)
        assert "workflow_5" in str(result.result_details)

    @patch("griptape_nodes.retained_mode.managers.project_manager.WorkflowRegistry")
    @patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes")
    def test_builtin_workflow_dir_optional_skipped_when_workflow_unregistered(
        self,
        mock_griptape_nodes: Mock,
        mock_workflow_registry: Mock,
        project_manager_with_template: ProjectManager,
    ) -> None:
        """Test that optional {workflow_dir?:/} falls back gracefully when the workflow is not registered (unsaved)."""
        from griptape_nodes.common.macro_parser import ParsedMacro

        cast("Mock", project_manager_with_template._config_manager).workspace_path = Path("/workspace")

        mock_context_manager = Mock()
        mock_context_manager.has_current_workflow.return_value = True
        mock_context_manager.get_current_workflow_name.return_value = "workflow_5"
        mock_griptape_nodes.ContextManager.return_value = mock_context_manager

        mock_workflow_registry.get_workflow_by_name.side_effect = KeyError("workflow_5")

        parsed_macro = ParsedMacro("{workflow_dir?:/}staticfiles/output.txt")
        request = GetPathForMacroRequest(parsed_macro=parsed_macro, variables={})

        result = project_manager_with_template.on_get_path_for_macro_request(request)

        assert isinstance(result, GetPathForMacroResultSuccess)
        assert result.resolved_path == Path("staticfiles/output.txt")

    def test_builtin_static_files_dir_resolves_from_config(self, project_manager_with_template: ProjectManager) -> None:
        """Test that {static_files_dir} resolves to the configured static_files_directory setting."""
        from griptape_nodes.common.macro_parser import ParsedMacro

        cast("Mock", project_manager_with_template._config_manager).get_config_value.return_value = "my_static"
        cast("Mock", project_manager_with_template._config_manager).workspace_path = Path("/test")

        parsed_macro = ParsedMacro("{static_files_dir}/output.png")
        request = GetPathForMacroRequest(parsed_macro=parsed_macro, variables={})

        result = project_manager_with_template.on_get_path_for_macro_request(request)

        assert isinstance(result, GetPathForMacroResultSuccess)
        assert result.resolved_path == Path("my_static/output.png")

    def test_builtin_override_matching_value_allowed(self, project_manager_with_template: ProjectManager) -> None:
        """Test that providing matching value for builtin variable is allowed."""
        from griptape_nodes.common.macro_parser import ParsedMacro

        parsed_macro = ParsedMacro("{project_dir}/output.txt")

        request = GetPathForMacroRequest(
            parsed_macro=parsed_macro,
            variables={"project_dir": "/test"},
        )

        result = project_manager_with_template.on_get_path_for_macro_request(request)

        assert isinstance(result, GetPathForMacroResultSuccess)
        assert result.resolved_path == Path("/test/output.txt")

    def test_builtin_override_different_value_rejected(self, project_manager_with_template: ProjectManager) -> None:
        """Test that providing different value for builtin variable is rejected."""
        from griptape_nodes.common.macro_parser import ParsedMacro

        parsed_macro = ParsedMacro("{project_dir}/output.txt")

        request = GetPathForMacroRequest(
            parsed_macro=parsed_macro,
            variables={"project_dir": "/different"},
        )

        result = project_manager_with_template.on_get_path_for_macro_request(request)

        assert isinstance(result, GetPathForMacroResultFailure)
        assert result.failure_reason == PathResolutionFailureReason.RESERVED_NAME_COLLISION
        assert result.conflicting_variables == {"project_dir"}
        from griptape_nodes.retained_mode.events.base_events import ResultDetails

        assert isinstance(result.result_details, ResultDetails)
        assert "cannot override builtin variables" in str(result.result_details)


class TestUnresolvedSequenceSlotBehavior:
    """Test the ``unresolved_sequence_slot_behavior`` flag on ``GetPathForMacroRequest``.

    Each behavior is tested against a required ``{###}`` slot with no bound
    value. The default (``FAIL``) is the write-path contract; the other three
    behaviors are opt-ins for previewers.
    """

    @pytest.fixture
    def project_manager_with_template(self) -> ProjectManager:
        """Same fixture as ``TestProjectManagerBuiltinVariables`` — a PM with system defaults loaded."""
        from griptape_nodes.common.project_templates import ProjectValidationInfo, ProjectValidationStatus
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.retained_mode.managers.project_manager import ProjectInfo

        mock_config = Mock()
        mock_config.workspace_path = Path("/workspace")
        mock_secrets = Mock()
        mock_event_manager = Mock()
        pm = ProjectManager(mock_event_manager, mock_config, mock_secrets)

        project_path = Path("/test/project.yml")
        project_id = str(project_path)

        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        situation_schemas = pm._parse_situation_macros(DEFAULT_PROJECT_TEMPLATE.situations, validation)
        directory_schemas = pm._parse_directory_macros(DEFAULT_PROJECT_TEMPLATE.directories, validation)

        project_info = ProjectInfo(
            project_id=project_id,
            project_file_path=project_path,
            project_base_dir=project_path.parent,
            template=DEFAULT_PROJECT_TEMPLATE,
            validation=validation,
            parsed_situation_schemas=situation_schemas,
            parsed_directory_schemas=directory_schemas,
        )

        pm._successfully_loaded_project_templates[project_id] = project_info
        pm._current_project_id = project_id

        return pm

    def test_fail_returns_missing_required_for_required_sequence_slot(
        self, project_manager_with_template: ProjectManager
    ) -> None:
        """Default FAIL behavior surfaces MISSING_REQUIRED_VARIABLES so the write-path seed step can fire."""
        from griptape_nodes.common.macro_parser import ParsedMacro
        from griptape_nodes.retained_mode.events.project_events import UnresolvedSequenceSlotBehavior

        parsed_macro = ParsedMacro("render_v{###}.png")

        request = GetPathForMacroRequest(
            parsed_macro=parsed_macro,
            variables={},
            unresolved_sequence_slot_behavior=UnresolvedSequenceSlotBehavior.FAIL,
        )

        result = project_manager_with_template.on_get_path_for_macro_request(request)

        assert isinstance(result, GetPathForMacroResultFailure)
        assert result.failure_reason == PathResolutionFailureReason.MISSING_REQUIRED_VARIABLES
        assert result.missing_variables == {"_index"}

    def test_render_sequence_pattern_renders_bare_hashes(self, project_manager_with_template: ProjectManager) -> None:
        """RENDER_SEQUENCE_PATTERN emits ``###`` (no braces) into the resolved path."""
        from griptape_nodes.common.macro_parser import ParsedMacro
        from griptape_nodes.retained_mode.events.project_events import UnresolvedSequenceSlotBehavior

        parsed_macro = ParsedMacro("render_v{###}.png")

        request = GetPathForMacroRequest(
            parsed_macro=parsed_macro,
            variables={},
            unresolved_sequence_slot_behavior=UnresolvedSequenceSlotBehavior.RENDER_SEQUENCE_PATTERN,
        )

        result = project_manager_with_template.on_get_path_for_macro_request(request)

        assert isinstance(result, GetPathForMacroResultSuccess)
        assert str(result.resolved_path) == "render_v###.png"

    def test_render_sequence_pattern_matches_source_width(self, project_manager_with_template: ProjectManager) -> None:
        """A ``{#####}`` slot renders as ``#####`` — width flows through, still bare hashes."""
        from griptape_nodes.common.macro_parser import ParsedMacro
        from griptape_nodes.retained_mode.events.project_events import UnresolvedSequenceSlotBehavior

        parsed_macro = ParsedMacro("frame_{#####}.exr")

        request = GetPathForMacroRequest(
            parsed_macro=parsed_macro,
            variables={},
            unresolved_sequence_slot_behavior=UnresolvedSequenceSlotBehavior.RENDER_SEQUENCE_PATTERN,
        )

        result = project_manager_with_template.on_get_path_for_macro_request(request)

        assert isinstance(result, GetPathForMacroResultSuccess)
        assert str(result.resolved_path) == "frame_#####.exr"

    def test_start_at_zero_seeds_index_zero(self, project_manager_with_template: ProjectManager) -> None:
        """START_AT_ZERO seeds ``_index = 0`` — a ``{###}`` slot renders as ``000``."""
        from griptape_nodes.common.macro_parser import ParsedMacro
        from griptape_nodes.retained_mode.events.project_events import UnresolvedSequenceSlotBehavior

        parsed_macro = ParsedMacro("render_v{###}.png")

        request = GetPathForMacroRequest(
            parsed_macro=parsed_macro,
            variables={},
            unresolved_sequence_slot_behavior=UnresolvedSequenceSlotBehavior.START_AT_ZERO,
        )

        result = project_manager_with_template.on_get_path_for_macro_request(request)

        assert isinstance(result, GetPathForMacroResultSuccess)
        assert str(result.resolved_path) == "render_v000.png"

    def test_start_at_one_seeds_index_one(self, project_manager_with_template: ProjectManager) -> None:
        """START_AT_ONE matches the write-path seed — a ``{###}`` slot renders as ``001``."""
        from griptape_nodes.common.macro_parser import ParsedMacro
        from griptape_nodes.retained_mode.events.project_events import UnresolvedSequenceSlotBehavior

        parsed_macro = ParsedMacro("render_v{###}.png")

        request = GetPathForMacroRequest(
            parsed_macro=parsed_macro,
            variables={},
            unresolved_sequence_slot_behavior=UnresolvedSequenceSlotBehavior.START_AT_ONE,
        )

        result = project_manager_with_template.on_get_path_for_macro_request(request)

        assert isinstance(result, GetPathForMacroResultSuccess)
        assert str(result.resolved_path) == "render_v001.png"

    def test_optional_sequence_slot_is_omitted_regardless_of_behavior(
        self, project_manager_with_template: ProjectManager
    ) -> None:
        """Optional ``{###?}`` slots are always omitted when unbound — the flag doesn't matter.

        FAIL is the interesting case: optional slots must NOT trigger MISSING_REQUIRED_VARIABLES.
        The three non-FAIL behaviors must not substitute into an optional slot either — the
        contract is "only required slots are affected."
        """
        from griptape_nodes.common.macro_parser import ParsedMacro
        from griptape_nodes.retained_mode.events.project_events import UnresolvedSequenceSlotBehavior

        parsed_macro = ParsedMacro("render_v{###?}.png")

        for behavior in UnresolvedSequenceSlotBehavior:
            request = GetPathForMacroRequest(
                parsed_macro=parsed_macro,
                variables={},
                unresolved_sequence_slot_behavior=behavior,
            )
            result = project_manager_with_template.on_get_path_for_macro_request(request)

            assert isinstance(result, GetPathForMacroResultSuccess), (
                f"Behavior {behavior} unexpectedly failed on optional slot"
            )
            # Optional slot is omitted → the ``_v`` separator survives but no digits/hashes are emitted.
            assert str(result.resolved_path) == "render_v.png", (
                f"Behavior {behavior} altered an optional slot; got {result.resolved_path!r}"
            )

    def test_bound_sequence_variable_ignores_behavior_flag(self, project_manager_with_template: ProjectManager) -> None:
        """When the caller binds ``_index`` explicitly, the flag is a no-op."""
        from griptape_nodes.common.macro_parser import ParsedMacro
        from griptape_nodes.retained_mode.events.project_events import UnresolvedSequenceSlotBehavior

        parsed_macro = ParsedMacro("render_v{###}.png")

        request = GetPathForMacroRequest(
            parsed_macro=parsed_macro,
            variables={"_index": 42},
            unresolved_sequence_slot_behavior=UnresolvedSequenceSlotBehavior.RENDER_SEQUENCE_PATTERN,
        )

        result = project_manager_with_template.on_get_path_for_macro_request(request)

        assert isinstance(result, GetPathForMacroResultSuccess)
        assert str(result.resolved_path) == "render_v042.png"

    def test_no_sequence_slot_ignores_behavior_flag(self, project_manager_with_template: ProjectManager) -> None:
        """Macros with no sequence slot are unaffected by the flag."""
        from griptape_nodes.common.macro_parser import ParsedMacro
        from griptape_nodes.retained_mode.events.project_events import UnresolvedSequenceSlotBehavior

        parsed_macro = ParsedMacro("plain/{file_name}.txt")

        request = GetPathForMacroRequest(
            parsed_macro=parsed_macro,
            variables={"file_name": "hello"},
            unresolved_sequence_slot_behavior=UnresolvedSequenceSlotBehavior.RENDER_SEQUENCE_PATTERN,
        )

        result = project_manager_with_template.on_get_path_for_macro_request(request)

        assert isinstance(result, GetPathForMacroResultSuccess)
        # `resolved_path` is a `pathlib.Path`; compare with a `Path` so the
        # assertion is platform-agnostic (Windows uses `\`, POSIX uses `/`).
        assert result.resolved_path == Path("plain/hello.txt")


class TestProjectManagerGetStateForMacro:
    """Test ProjectManager GetStateForMacro request handler."""

    @pytest.fixture
    def project_manager_with_current_project(self) -> ProjectManager:
        """Create a ProjectManager with current project set."""
        from griptape_nodes.common.project_templates import ProjectValidationInfo, ProjectValidationStatus
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.retained_mode.managers.project_manager import ProjectInfo

        mock_config = Mock()
        mock_secrets = Mock()
        mock_event_manager = Mock()
        pm = ProjectManager(mock_event_manager, mock_config, mock_secrets)

        project_path = Path("/test/project.yml")
        project_id = str(project_path)

        # Parse macros first
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        situation_schemas = pm._parse_situation_macros(DEFAULT_PROJECT_TEMPLATE.situations, validation)
        directory_schemas = pm._parse_directory_macros(DEFAULT_PROJECT_TEMPLATE.directories, validation)

        # Create ProjectInfo with fully populated caches
        project_info = ProjectInfo(
            project_id=project_id,
            project_file_path=project_path,
            project_base_dir=project_path.parent,
            template=DEFAULT_PROJECT_TEMPLATE,
            validation=validation,
            parsed_situation_schemas=situation_schemas,
            parsed_directory_schemas=directory_schemas,
        )

        # Set up new consolidated dict
        pm._successfully_loaded_project_templates[project_id] = project_info
        pm._current_project_id = project_id

        return pm

    def test_get_state_for_macro_no_current_project(self) -> None:
        """Test GetStateForMacro fails when current project ID does not resolve to a loaded template."""
        from griptape_nodes.common.macro_parser import ParsedMacro

        mock_config = Mock()
        mock_secrets = Mock()
        mock_event_manager = Mock()
        pm = ProjectManager(mock_event_manager, mock_config, mock_secrets)
        # Drop the system defaults loaded in __init__ so the current project ID
        # no longer resolves -- simulating the same failure as "no project set".
        pm._successfully_loaded_project_templates.clear()

        parsed_macro = ParsedMacro("{file_name}.txt")

        request = GetStateForMacroRequest(parsed_macro=parsed_macro, variables={})

        result = pm.on_get_state_for_macro_request(request)

        assert isinstance(result, GetStateForMacroResultFailure)
        from griptape_nodes.retained_mode.events.base_events import ResultDetails

        assert isinstance(result.result_details, ResultDetails)
        assert "no current project is set" in str(result.result_details)

    def test_get_state_for_macro_all_variables_satisfied(
        self, project_manager_with_current_project: ProjectManager
    ) -> None:
        """Test GetStateForMacro when all variables are satisfied."""
        from griptape_nodes.common.macro_parser import ParsedMacro

        parsed_macro = ParsedMacro("{file_name}.{ext}")

        request = GetStateForMacroRequest(parsed_macro=parsed_macro, variables={"file_name": "output", "ext": "txt"})

        result = project_manager_with_current_project.on_get_state_for_macro_request(request)

        assert isinstance(result, GetStateForMacroResultSuccess)
        var_names = {v.name for v in result.all_variables}
        assert var_names == {"file_name", "ext"}
        assert result.satisfied_variables == {"file_name", "ext"}
        assert result.missing_required_variables == set()
        assert result.conflicting_variables == set()
        assert result.can_resolve is True

    def test_get_state_for_macro_missing_required_variables(
        self, project_manager_with_current_project: ProjectManager
    ) -> None:
        """Test GetStateForMacro when required variables are missing."""
        from griptape_nodes.common.macro_parser import ParsedMacro

        parsed_macro = ParsedMacro("{file_name}.{ext}")

        request = GetStateForMacroRequest(parsed_macro=parsed_macro, variables={"file_name": "output"})

        result = project_manager_with_current_project.on_get_state_for_macro_request(request)

        assert isinstance(result, GetStateForMacroResultSuccess)
        assert result.satisfied_variables == {"file_name"}
        assert result.missing_required_variables == {"ext"}
        assert result.can_resolve is False

    def test_get_state_for_macro_conflicting_variables_directory(
        self, project_manager_with_current_project: ProjectManager
    ) -> None:
        """Test GetStateForMacro when user provides directory name."""
        from griptape_nodes.common.macro_parser import ParsedMacro

        parsed_macro = ParsedMacro("{inputs}/{file_name}.txt")

        request = GetStateForMacroRequest(
            parsed_macro=parsed_macro, variables={"inputs": "custom_inputs", "file_name": "output"}
        )

        result = project_manager_with_current_project.on_get_state_for_macro_request(request)

        assert isinstance(result, GetStateForMacroResultSuccess)
        assert "inputs" in result.conflicting_variables
        assert result.can_resolve is False

    @patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes")
    def test_get_state_for_macro_builtin_variable_satisfied(
        self, mock_griptape_nodes: Mock, project_manager_with_current_project: ProjectManager
    ) -> None:
        """Test GetStateForMacro with satisfied builtin variable."""
        from griptape_nodes.common.macro_parser import ParsedMacro

        mock_config_manager = Mock()
        mock_config_manager.get_config_value.return_value = "/workspace"
        mock_griptape_nodes.ConfigManager.return_value = mock_config_manager

        parsed_macro = ParsedMacro("{workspace_dir}/output.txt")

        request = GetStateForMacroRequest(parsed_macro=parsed_macro, variables={})

        result = project_manager_with_current_project.on_get_state_for_macro_request(request)

        assert isinstance(result, GetStateForMacroResultSuccess)
        assert result.satisfied_variables == {"workspace_dir"}
        assert result.can_resolve is True

    @patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes")
    def test_get_state_for_macro_builtin_variable_fails(
        self, mock_griptape_nodes: Mock, project_manager_with_current_project: ProjectManager
    ) -> None:
        """Test GetStateForMacro fails when builtin variable cannot be resolved."""
        from griptape_nodes.common.macro_parser import ParsedMacro

        mock_context_manager = Mock()
        mock_context_manager.has_current_workflow.return_value = False
        mock_griptape_nodes.ContextManager.return_value = mock_context_manager

        parsed_macro = ParsedMacro("{workflow_name}_output.txt")

        request = GetStateForMacroRequest(parsed_macro=parsed_macro, variables={})

        result = project_manager_with_current_project.on_get_state_for_macro_request(request)

        assert isinstance(result, GetStateForMacroResultFailure)
        from griptape_nodes.retained_mode.events.base_events import ResultDetails

        assert isinstance(result.result_details, ResultDetails)
        assert "workflow_name" in str(result.result_details)
        assert "cannot be resolved" in str(result.result_details)

    def test_get_state_for_macro_conflicting_builtin_override(
        self, project_manager_with_current_project: ProjectManager
    ) -> None:
        """Test GetStateForMacro when user tries to override builtin with different value."""
        from griptape_nodes.common.macro_parser import ParsedMacro

        parsed_macro = ParsedMacro("{project_dir}/output.txt")

        request = GetStateForMacroRequest(parsed_macro=parsed_macro, variables={"project_dir": "/different"})

        result = project_manager_with_current_project.on_get_state_for_macro_request(request)

        assert isinstance(result, GetStateForMacroResultSuccess)
        assert "project_dir" in result.conflicting_variables
        assert result.can_resolve is False


class TestProjectManagerGetCurrentProject:
    """Test ProjectManager GetCurrentProject request handler."""

    def test_get_current_project_no_project_set(self) -> None:
        """Test GetCurrentProject fails when current project ID does not resolve to a loaded template."""
        from griptape_nodes.retained_mode.events.project_events import (
            GetCurrentProjectRequest,
            GetCurrentProjectResultFailure,
        )

        mock_config = Mock()
        mock_secrets = Mock()
        mock_event_manager = Mock()
        pm = ProjectManager(mock_event_manager, mock_config, mock_secrets)
        # Drop the system defaults loaded in __init__ so the current project ID
        # no longer resolves to a loaded template.
        pm._successfully_loaded_project_templates.clear()

        request = GetCurrentProjectRequest()
        result = pm.on_get_current_project_request(request)

        assert isinstance(result, GetCurrentProjectResultFailure)
        assert "project not found" in str(result.result_details)

    def test_get_current_project_returns_project_info(self) -> None:
        """Test GetCurrentProject returns complete ProjectInfo."""
        from griptape_nodes.common.project_templates import ProjectValidationInfo, ProjectValidationStatus
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.retained_mode.events.project_events import (
            GetCurrentProjectRequest,
            GetCurrentProjectResultSuccess,
        )
        from griptape_nodes.retained_mode.managers.project_manager import ProjectInfo

        mock_config = Mock()
        mock_secrets = Mock()
        mock_event_manager = Mock()
        pm = ProjectManager(mock_event_manager, mock_config, mock_secrets)

        project_path = Path("/test/project.yml")
        project_id = str(project_path)

        # Parse macros first
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        situation_schemas = pm._parse_situation_macros(DEFAULT_PROJECT_TEMPLATE.situations, validation)
        directory_schemas = pm._parse_directory_macros(DEFAULT_PROJECT_TEMPLATE.directories, validation)

        # Create ProjectInfo
        project_info = ProjectInfo(
            project_id=project_id,
            project_file_path=project_path,
            project_base_dir=project_path.parent,
            template=DEFAULT_PROJECT_TEMPLATE,
            validation=validation,
            parsed_situation_schemas=situation_schemas,
            parsed_directory_schemas=directory_schemas,
        )

        pm._successfully_loaded_project_templates[project_id] = project_info
        pm._current_project_id = project_id

        request = GetCurrentProjectRequest()
        result = pm.on_get_current_project_request(request)

        assert isinstance(result, GetCurrentProjectResultSuccess)
        assert result.project_info == project_info
        assert result.project_info.project_id == project_id
        assert result.project_info.template == DEFAULT_PROJECT_TEMPLATE
        assert result.project_info.project_base_dir == Path("/test")
        assert result.project_info.validation.status == ProjectValidationStatus.GOOD

    def test_get_current_project_id_not_found_in_templates(self) -> None:
        """Test GetCurrentProject fails when current project ID is not in loaded templates."""
        from griptape_nodes.retained_mode.events.project_events import (
            GetCurrentProjectRequest,
            GetCurrentProjectResultFailure,
        )

        mock_config = Mock()
        mock_secrets = Mock()
        mock_event_manager = Mock()
        pm = ProjectManager(mock_event_manager, mock_config, mock_secrets)

        # Set current project ID but don't add to loaded templates
        pm._current_project_id = "missing_project"

        request = GetCurrentProjectRequest()
        result = pm.on_get_current_project_request(request)

        assert isinstance(result, GetCurrentProjectResultFailure)
        assert "project not found" in str(result.result_details)


class TestProjectManagerListProjectTemplates:
    """Test ProjectManager ListProjectTemplates request handler."""

    def test_list_project_templates_empty(self) -> None:
        """Test ListProjectTemplates with no projects loaded."""
        from griptape_nodes.retained_mode.events.project_events import (
            ListProjectTemplatesRequest,
            ListProjectTemplatesResultSuccess,
        )

        mock_config = Mock()
        mock_secrets = Mock()
        mock_event_manager = Mock()
        pm = ProjectManager(mock_event_manager, mock_config, mock_secrets)

        request = ListProjectTemplatesRequest(include_system_builtins=False)
        result = pm.on_list_project_templates_request(request)

        assert isinstance(result, ListProjectTemplatesResultSuccess)
        assert result.successfully_loaded == []
        assert result.failed_to_load == []

    def test_list_project_templates_successfully_loaded(self) -> None:
        """Test ListProjectTemplates returns successfully loaded projects."""
        from griptape_nodes.common.project_templates import ProjectValidationInfo, ProjectValidationStatus
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.retained_mode.events.project_events import (
            ListProjectTemplatesRequest,
            ListProjectTemplatesResultSuccess,
        )
        from griptape_nodes.retained_mode.managers.project_manager import ProjectInfo

        mock_config = Mock()
        mock_config.workspace_path = Path("/workspace")
        mock_config.read_config_file_value.return_value = None
        mock_secrets = Mock()
        mock_event_manager = Mock()
        pm = ProjectManager(mock_event_manager, mock_config, mock_secrets)

        # Add two successfully loaded projects
        project1_path = Path("/test/project1.yml")
        project1_id = str(project1_path)
        validation1 = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        situation_schemas = pm._parse_situation_macros(DEFAULT_PROJECT_TEMPLATE.situations, validation1)
        directory_schemas = pm._parse_directory_macros(DEFAULT_PROJECT_TEMPLATE.directories, validation1)

        project_info1 = ProjectInfo(
            project_id=project1_id,
            project_file_path=project1_path,
            project_base_dir=project1_path.parent,
            template=DEFAULT_PROJECT_TEMPLATE,
            validation=validation1,
            parsed_situation_schemas=situation_schemas,
            parsed_directory_schemas=directory_schemas,
        )

        project2_path = Path("/test/project2.yml")
        project2_id = str(project2_path)
        validation2 = ProjectValidationInfo(status=ProjectValidationStatus.FLAWED)
        situation_schemas2 = pm._parse_situation_macros(DEFAULT_PROJECT_TEMPLATE.situations, validation2)
        directory_schemas2 = pm._parse_directory_macros(DEFAULT_PROJECT_TEMPLATE.directories, validation2)

        project_info2 = ProjectInfo(
            project_id=project2_id,
            project_file_path=project2_path,
            project_base_dir=project2_path.parent,
            template=DEFAULT_PROJECT_TEMPLATE,
            validation=validation2,
            parsed_situation_schemas=situation_schemas2,
            parsed_directory_schemas=directory_schemas2,
        )

        pm._successfully_loaded_project_templates[project1_id] = project_info1
        pm._successfully_loaded_project_templates[project2_id] = project_info2

        request = ListProjectTemplatesRequest(include_system_builtins=False)
        result = pm.on_list_project_templates_request(request)

        assert isinstance(result, ListProjectTemplatesResultSuccess)
        assert result.failed_to_load == []

        # Verify both projects are in successfully_loaded
        project_ids = {info.project_id for info in result.successfully_loaded}
        assert project_ids == {project1_id, project2_id}

    def test_list_project_templates_with_failures(self) -> None:
        """Test ListProjectTemplates returns failed projects."""
        from griptape_nodes.common.project_templates import ProjectValidationInfo, ProjectValidationStatus
        from griptape_nodes.retained_mode.events.project_events import (
            ListProjectTemplatesRequest,
            ListProjectTemplatesResultSuccess,
        )

        mock_config = Mock()
        mock_secrets = Mock()
        mock_event_manager = Mock()
        pm = ProjectManager(mock_event_manager, mock_config, mock_secrets)

        # Add a failed project to registered_template_status
        failed_path = Path("/test/failed.yml")
        failed_validation = ProjectValidationInfo(status=ProjectValidationStatus.UNUSABLE)
        failed_validation.add_error("template", "Invalid YAML")

        pm._registered_template_status[failed_path] = failed_validation

        request = ListProjectTemplatesRequest(include_system_builtins=False)
        result = pm.on_list_project_templates_request(request)

        assert isinstance(result, ListProjectTemplatesResultSuccess)
        assert result.successfully_loaded == []
        assert len(result.failed_to_load) == 1
        assert result.failed_to_load[0].project_id == str(failed_path)
        assert result.failed_to_load[0].validation.status == ProjectValidationStatus.UNUSABLE

    def test_list_project_templates_filters_system_builtins(self) -> None:
        """Test ListProjectTemplates filters system builtins when requested."""
        from griptape_nodes.common.project_templates import ProjectValidationInfo, ProjectValidationStatus
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.retained_mode.events.project_events import (
            ListProjectTemplatesRequest,
            ListProjectTemplatesResultSuccess,
        )
        from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY, ProjectInfo

        mock_config = Mock()
        mock_secrets = Mock()
        mock_event_manager = Mock()
        pm = ProjectManager(mock_event_manager, mock_config, mock_secrets)

        # Add system defaults
        validation_sys = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        situation_schemas = pm._parse_situation_macros(DEFAULT_PROJECT_TEMPLATE.situations, validation_sys)
        directory_schemas = pm._parse_directory_macros(DEFAULT_PROJECT_TEMPLATE.directories, validation_sys)

        system_info = ProjectInfo(
            project_id=SYSTEM_DEFAULTS_KEY,
            project_file_path=None,
            project_base_dir=Path("/workspace"),
            template=DEFAULT_PROJECT_TEMPLATE,
            validation=validation_sys,
            parsed_situation_schemas=situation_schemas,
            parsed_directory_schemas=directory_schemas,
        )

        pm._successfully_loaded_project_templates[SYSTEM_DEFAULTS_KEY] = system_info

        # Test with include_system_builtins=False (default)
        request_no_builtins = ListProjectTemplatesRequest(include_system_builtins=False)
        result_no_builtins = pm.on_list_project_templates_request(request_no_builtins)

        assert isinstance(result_no_builtins, ListProjectTemplatesResultSuccess)
        assert result_no_builtins.successfully_loaded == []

        # Test with include_system_builtins=True
        request_with_builtins = ListProjectTemplatesRequest(include_system_builtins=True)
        result_with_builtins = pm.on_list_project_templates_request(request_with_builtins)

        assert isinstance(result_with_builtins, ListProjectTemplatesResultSuccess)
        assert len(result_with_builtins.successfully_loaded) == 1
        assert result_with_builtins.successfully_loaded[0].project_id == SYSTEM_DEFAULTS_KEY

    def test_list_project_templates_mixed_state(self) -> None:
        """Test ListProjectTemplates with mix of successful and failed projects."""
        from griptape_nodes.common.project_templates import ProjectValidationInfo, ProjectValidationStatus
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.retained_mode.events.project_events import (
            ListProjectTemplatesRequest,
            ListProjectTemplatesResultSuccess,
        )
        from griptape_nodes.retained_mode.managers.project_manager import ProjectInfo

        mock_config = Mock()
        mock_config.workspace_path = Path("/workspace")
        mock_config.read_config_file_value.return_value = None
        mock_secrets = Mock()
        mock_event_manager = Mock()
        pm = ProjectManager(mock_event_manager, mock_config, mock_secrets)

        # Add successful project
        success_path = Path("/test/success.yml")
        success_id = str(success_path)
        validation_success = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        situation_schemas = pm._parse_situation_macros(DEFAULT_PROJECT_TEMPLATE.situations, validation_success)
        directory_schemas = pm._parse_directory_macros(DEFAULT_PROJECT_TEMPLATE.directories, validation_success)

        success_info = ProjectInfo(
            project_id=success_id,
            project_file_path=success_path,
            project_base_dir=success_path.parent,
            template=DEFAULT_PROJECT_TEMPLATE,
            validation=validation_success,
            parsed_situation_schemas=situation_schemas,
            parsed_directory_schemas=directory_schemas,
        )

        pm._successfully_loaded_project_templates[success_id] = success_info
        pm._registered_template_status[success_path] = validation_success

        # Add failed project
        failed_path = Path("/test/failed.yml")
        failed_validation = ProjectValidationInfo(status=ProjectValidationStatus.UNUSABLE)
        failed_validation.add_error("template", "Parse error")

        pm._registered_template_status[failed_path] = failed_validation

        request = ListProjectTemplatesRequest(include_system_builtins=False)
        result = pm.on_list_project_templates_request(request)

        assert isinstance(result, ListProjectTemplatesResultSuccess)
        assert len(result.successfully_loaded) == 1
        assert len(result.failed_to_load) == 1
        assert result.successfully_loaded[0].project_id == success_id
        assert result.failed_to_load[0].project_id == str(failed_path)

    def test_list_marks_incompatible_engine_version(self) -> None:
        """A project whose adjacent config pins an unsatisfiable engine_version is flagged incompatible."""
        from griptape_nodes.common.project_templates import ProjectValidationInfo, ProjectValidationStatus
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.retained_mode.events.project_events import (
            ListProjectTemplatesRequest,
            ListProjectTemplatesResultSuccess,
        )
        from griptape_nodes.retained_mode.managers.project_manager import ProjectInfo

        mock_config = Mock()
        mock_config.workspace_path = Path("/workspace")
        # The running engine cannot satisfy this pin, so the verdict must be incompatible.
        mock_config.read_config_file_value.return_value = ">=99"
        mock_secrets = Mock()
        mock_event_manager = Mock()
        pm = ProjectManager(mock_event_manager, mock_config, mock_secrets)

        project_path = Path("/test/pinned.yml")
        project_id = str(project_path)
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        situation_schemas = pm._parse_situation_macros(DEFAULT_PROJECT_TEMPLATE.situations, validation)
        directory_schemas = pm._parse_directory_macros(DEFAULT_PROJECT_TEMPLATE.directories, validation)

        pm._successfully_loaded_project_templates[project_id] = ProjectInfo(
            project_id=project_id,
            project_file_path=project_path,
            project_base_dir=project_path.parent,
            template=DEFAULT_PROJECT_TEMPLATE,
            validation=validation,
            parsed_situation_schemas=situation_schemas,
            parsed_directory_schemas=directory_schemas,
        )

        result = pm.on_list_project_templates_request(ListProjectTemplatesRequest(include_system_builtins=False))

        assert isinstance(result, ListProjectTemplatesResultSuccess)
        info = result.successfully_loaded[0]
        assert info.engine_version_compatible is False
        assert info.required_engine_version == ">=99"
        assert info.engine_version_reason is not None


class TestProjectManagerAttemptMapAbsolutePathToProject:
    """Test ProjectManager AttemptMapAbsolutePathToProject event handler."""

    @pytest.fixture
    def project_manager(self) -> ProjectManager:
        """Create a ProjectManager instance for testing."""
        mock_config = Mock()
        mock_secrets = Mock()
        mock_event_manager = Mock()
        return ProjectManager(mock_event_manager, mock_config, mock_secrets)

    def test_attempt_map_path_inside_project_directory(self, project_manager: ProjectManager) -> None:
        """Test mapping an absolute path that's inside a project directory."""
        from griptape_nodes.common.macro_parser import ParsedMacro
        from griptape_nodes.common.project_templates import (
            ProjectValidationInfo,
            ProjectValidationStatus,
        )
        from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY, ProjectInfo

        # Set up project with outputs directory
        project_base = Path("/Users/test/project")

        # Parse directory macros
        directory_schemas = {}
        for dir_name, dir_def in DEFAULT_PROJECT_TEMPLATE.directories.items():
            assert isinstance(dir_def.path_macro, str)
            directory_schemas[dir_name] = ParsedMacro(dir_def.path_macro)

        project_info = ProjectInfo(
            project_id=SYSTEM_DEFAULTS_KEY,
            project_file_path=project_base / "project.yml",
            project_base_dir=project_base,
            template=DEFAULT_PROJECT_TEMPLATE,
            validation=ProjectValidationInfo(status=ProjectValidationStatus.GOOD),
            parsed_situation_schemas={},
            parsed_directory_schemas=directory_schemas,
        )

        project_manager._successfully_loaded_project_templates[SYSTEM_DEFAULTS_KEY] = project_info
        project_manager._current_project_id = SYSTEM_DEFAULTS_KEY

        # Mock secrets manager
        project_manager._secrets_manager = Mock()
        project_manager._secrets_manager.resolve.return_value = "test_value"

        cast("Mock", project_manager._config_manager).workspace_path = project_base

        # Mock GriptapeNodes.ContextManager()
        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_context = Mock()
            mock_context.has_current_workflow.return_value = False  # No workflow needed for this test
            mock_gn.ContextManager.return_value = mock_context

            # Test path inside outputs directory
            absolute_path = project_base / "outputs" / "renders" / "file.png"

            request = AttemptMapAbsolutePathToProjectRequest(absolute_path=absolute_path)
            result = project_manager.on_attempt_map_absolute_path_to_project_request(request)

            assert isinstance(result, AttemptMapAbsolutePathToProjectResultSuccess)
            assert result.mapped_path == "{outputs}/renders/file.png"

    def test_attempt_map_path_outside_project_directories(self, project_manager: ProjectManager) -> None:
        """Test mapping an absolute path that's outside all project directories."""
        from griptape_nodes.common.macro_parser import ParsedMacro
        from griptape_nodes.common.project_templates import (
            ProjectValidationInfo,
            ProjectValidationStatus,
        )
        from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY, ProjectInfo

        # Set up project
        project_base = Path("/Users/test/project")

        # Parse directory macros
        directory_schemas = {}
        for dir_name, dir_def in DEFAULT_PROJECT_TEMPLATE.directories.items():
            assert isinstance(dir_def.path_macro, str)
            directory_schemas[dir_name] = ParsedMacro(dir_def.path_macro)

        project_info = ProjectInfo(
            project_id=SYSTEM_DEFAULTS_KEY,
            project_file_path=project_base / "project.yml",
            project_base_dir=project_base,
            template=DEFAULT_PROJECT_TEMPLATE,
            validation=ProjectValidationInfo(status=ProjectValidationStatus.GOOD),
            parsed_situation_schemas={},
            parsed_directory_schemas=directory_schemas,
        )

        project_manager._successfully_loaded_project_templates[SYSTEM_DEFAULTS_KEY] = project_info
        project_manager._current_project_id = SYSTEM_DEFAULTS_KEY

        # Mock secrets manager
        project_manager._secrets_manager = Mock()
        project_manager._secrets_manager.resolve.return_value = "test_value"

        cast("Mock", project_manager._config_manager).workspace_path = project_base

        # Mock GriptapeNodes.ConfigManager() and ContextManager()
        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_context = Mock()
            mock_context.has_current_workflow.return_value = False  # No workflow needed for this test
            mock_gn.ContextManager.return_value = mock_context

            # Test path outside project
            absolute_path = Path("/Users/test/Downloads/file.png")

            request = AttemptMapAbsolutePathToProjectRequest(absolute_path=absolute_path)
            result = project_manager.on_attempt_map_absolute_path_to_project_request(request)

            assert isinstance(result, AttemptMapAbsolutePathToProjectResultSuccess)
            assert result.mapped_path is None

    def test_attempt_map_path_no_current_project(self, project_manager: ProjectManager) -> None:
        """Test mapping when current project ID does not resolve to a loaded template (returns failure)."""
        from griptape_nodes.retained_mode.events.project_events import AttemptMapAbsolutePathToProjectResultFailure

        # Clear the system defaults loaded in __init__ to simulate no resolvable project
        project_manager._successfully_loaded_project_templates.clear()

        absolute_path = Path("/Users/test/project/outputs/file.png")

        request = AttemptMapAbsolutePathToProjectRequest(absolute_path=absolute_path)
        result = project_manager.on_attempt_map_absolute_path_to_project_request(request)

        # Should return failure because no current project (cannot perform operation)
        assert isinstance(result, AttemptMapAbsolutePathToProjectResultFailure)
        assert "no current project" in str(result.result_details).lower()

    def test_attempt_map_path_longest_prefix_matching(self, project_manager: ProjectManager) -> None:
        """Test that longest prefix matching works correctly for nested directories."""
        from griptape_nodes.common.macro_parser import ParsedMacro
        from griptape_nodes.common.project_templates import (
            ProjectValidationInfo,
            ProjectValidationStatus,
        )
        from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY, ProjectInfo

        # Set up project
        project_base = Path("/Users/test/project")

        # Parse directory macros
        directory_schemas = {}
        for dir_name, dir_def in DEFAULT_PROJECT_TEMPLATE.directories.items():
            assert isinstance(dir_def.path_macro, str)
            directory_schemas[dir_name] = ParsedMacro(dir_def.path_macro)

        project_info = ProjectInfo(
            project_id=SYSTEM_DEFAULTS_KEY,
            project_file_path=project_base / "project.yml",
            project_base_dir=project_base,
            template=DEFAULT_PROJECT_TEMPLATE,
            validation=ProjectValidationInfo(status=ProjectValidationStatus.GOOD),
            parsed_situation_schemas={},
            parsed_directory_schemas=directory_schemas,
        )

        project_manager._successfully_loaded_project_templates[SYSTEM_DEFAULTS_KEY] = project_info
        project_manager._current_project_id = SYSTEM_DEFAULTS_KEY

        # Mock secrets manager
        project_manager._secrets_manager = Mock()
        project_manager._secrets_manager.resolve.return_value = "test_value"

        cast("Mock", project_manager._config_manager).workspace_path = project_base

        # Mock GriptapeNodes.ConfigManager() and ContextManager()
        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_context = Mock()
            mock_context.has_current_workflow.return_value = False  # No workflow needed for this test
            mock_gn.ContextManager.return_value = mock_context

            # Test path inside outputs/inputs subdirectory (should match outputs, not inputs)
            absolute_path = project_base / "outputs" / "inputs" / "file.png"

            request = AttemptMapAbsolutePathToProjectRequest(absolute_path=absolute_path)
            result = project_manager.on_attempt_map_absolute_path_to_project_request(request)

            assert isinstance(result, AttemptMapAbsolutePathToProjectResultSuccess)
            assert result.mapped_path == "{outputs}/inputs/file.png"

    def test_attempt_map_path_at_directory_root(self, project_manager: ProjectManager) -> None:
        """Test mapping a path that's exactly at a directory root (no subdirectories)."""
        from griptape_nodes.common.macro_parser import ParsedMacro
        from griptape_nodes.common.project_templates import (
            ProjectValidationInfo,
            ProjectValidationStatus,
        )
        from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY, ProjectInfo

        # Set up project
        project_base = Path("/Users/test/project")

        # Parse directory macros
        directory_schemas = {}
        for dir_name, dir_def in DEFAULT_PROJECT_TEMPLATE.directories.items():
            assert isinstance(dir_def.path_macro, str)
            directory_schemas[dir_name] = ParsedMacro(dir_def.path_macro)

        project_info = ProjectInfo(
            project_id=SYSTEM_DEFAULTS_KEY,
            project_file_path=project_base / "project.yml",
            project_base_dir=project_base,
            template=DEFAULT_PROJECT_TEMPLATE,
            validation=ProjectValidationInfo(status=ProjectValidationStatus.GOOD),
            parsed_situation_schemas={},
            parsed_directory_schemas=directory_schemas,
        )

        project_manager._successfully_loaded_project_templates[SYSTEM_DEFAULTS_KEY] = project_info
        project_manager._current_project_id = SYSTEM_DEFAULTS_KEY

        # Mock secrets manager
        project_manager._secrets_manager = Mock()
        project_manager._secrets_manager.resolve.return_value = "test_value"

        cast("Mock", project_manager._config_manager).workspace_path = project_base

        # Mock GriptapeNodes.ConfigManager() and ContextManager()
        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_context = Mock()
            mock_context.has_current_workflow.return_value = False  # No workflow needed for this test
            mock_gn.ContextManager.return_value = mock_context

            # Test path exactly at outputs directory
            absolute_path = project_base / "outputs"

            request = AttemptMapAbsolutePathToProjectRequest(absolute_path=absolute_path)
            result = project_manager.on_attempt_map_absolute_path_to_project_request(request)

            assert isinstance(result, AttemptMapAbsolutePathToProjectResultSuccess)
            assert result.mapped_path == "{outputs}"

    def test_attempt_map_path_fallback_to_project_dir(self, project_manager: ProjectManager) -> None:
        """Test that paths not in defined directories fall back to {project_dir}."""
        from griptape_nodes.common.macro_parser import ParsedMacro
        from griptape_nodes.common.project_templates import (
            ProjectValidationInfo,
            ProjectValidationStatus,
        )
        from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY, ProjectInfo

        # Set up project
        project_base = Path("/Users/test/project")

        # Parse directory macros
        directory_schemas = {}
        for dir_name, dir_def in DEFAULT_PROJECT_TEMPLATE.directories.items():
            assert isinstance(dir_def.path_macro, str)
            directory_schemas[dir_name] = ParsedMacro(dir_def.path_macro)

        project_info = ProjectInfo(
            project_id=SYSTEM_DEFAULTS_KEY,
            project_file_path=project_base / "project.yml",
            project_base_dir=project_base,
            template=DEFAULT_PROJECT_TEMPLATE,
            validation=ProjectValidationInfo(status=ProjectValidationStatus.GOOD),
            parsed_situation_schemas={},
            parsed_directory_schemas=directory_schemas,
        )

        project_manager._successfully_loaded_project_templates[SYSTEM_DEFAULTS_KEY] = project_info
        project_manager._current_project_id = SYSTEM_DEFAULTS_KEY
        project_manager._secrets_manager = Mock()
        project_manager._secrets_manager.resolve.return_value = "test_value"

        cast("Mock", project_manager._config_manager).workspace_path = project_base

        # Mock GriptapeNodes.ConfigManager() and ContextManager()
        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_context = Mock()
            mock_context.has_current_workflow.return_value = False
            mock_gn.ContextManager.return_value = mock_context

            # Test path inside project_base_dir but not in any defined directory
            absolute_path = project_base / "random_folder" / "file.txt"

            request = AttemptMapAbsolutePathToProjectRequest(absolute_path=absolute_path)
            result = project_manager.on_attempt_map_absolute_path_to_project_request(request)

            assert isinstance(result, AttemptMapAbsolutePathToProjectResultSuccess)
            assert result.mapped_path == "{project_dir}/random_folder/file.txt"

    def test_attempt_map_path_with_unresolvable_builtin_variable(self, project_manager: ProjectManager) -> None:
        """Test that if a directory macro needs an unresolvable builtin, returns failure."""
        from griptape_nodes.common.macro_parser import ParsedMacro
        from griptape_nodes.common.project_templates import (
            DirectoryDefinition,
            ProjectTemplate,
            ProjectValidationInfo,
            ProjectValidationStatus,
        )
        from griptape_nodes.retained_mode.events.project_events import AttemptMapAbsolutePathToProjectResultFailure
        from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY, ProjectInfo

        # Create a custom template with a directory that uses workflow_name (will fail without workflow)
        custom_template = ProjectTemplate(
            project_template_schema_version="0.1.0",
            name="test_project",
            directories={
                "outputs": DirectoryDefinition(name="outputs", path_macro="{workflow_name}_outputs"),
            },
            situations={},
        )

        project_base = Path("/Users/test/project")

        # Parse directory macros
        directory_schemas = {}
        for dir_name, dir_def in custom_template.directories.items():
            assert isinstance(dir_def.path_macro, str)
            directory_schemas[dir_name] = ParsedMacro(dir_def.path_macro)

        project_info = ProjectInfo(
            project_id=SYSTEM_DEFAULTS_KEY,
            project_file_path=project_base / "project.yml",
            project_base_dir=project_base,
            template=custom_template,
            validation=ProjectValidationInfo(status=ProjectValidationStatus.GOOD),
            parsed_situation_schemas={},
            parsed_directory_schemas=directory_schemas,
        )

        project_manager._successfully_loaded_project_templates[SYSTEM_DEFAULTS_KEY] = project_info
        project_manager._current_project_id = SYSTEM_DEFAULTS_KEY
        project_manager._secrets_manager = Mock()
        project_manager._secrets_manager.resolve.return_value = "test_value"

        cast("Mock", project_manager._config_manager).workspace_path = project_base

        # Mock GriptapeNodes - workflow_name will fail because no workflow
        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_config = Mock()
            mock_config.get_config_value.return_value = str(project_base)
            mock_config.workspace_path = project_base
            mock_gn.ConfigManager.return_value = mock_config

            mock_context = Mock()
            mock_context.has_current_workflow.return_value = False  # No workflow available
            mock_gn.ContextManager.return_value = mock_context

            absolute_path = project_base / "outputs" / "file.png"

            request = AttemptMapAbsolutePathToProjectRequest(absolute_path=absolute_path)
            result = project_manager.on_attempt_map_absolute_path_to_project_request(request)

            # Should return failure because workflow_name cannot be resolved (operation cannot complete)
            assert isinstance(result, AttemptMapAbsolutePathToProjectResultFailure)
            result_message = str(result.result_details)
            assert "failed" in result_message.lower()
            assert "workflow" in result_message.lower() or "no current workflow" in result_message.lower()


class TestLoadWorkspaceProject:
    """Test _load_workspace_project and on_app_initialization_complete."""

    VALID_PROJECT_YAML = """\
project_template_schema_version: "0.1.0"
name: Workspace Project
situations:
  save_node_output:
    macro: "{outputs}/custom/{file_name_base}.{file_extension}"
    policy:
      on_collision: create_new
      create_dirs: true
"""

    @pytest.fixture
    def pm(self) -> ProjectManager:
        mock_event_manager = Mock()
        mock_config_manager = Mock()
        mock_config_manager.project_config = {}
        mock_config_manager.env_config = {}
        mock_config_manager.merged_config = {}
        mock_config_manager.get_config_value.return_value = {}
        return ProjectManager(mock_event_manager, mock_config_manager, Mock())

    def _setup_system_defaults(self, pm: ProjectManager, workspace_dir: str = "/workspace") -> None:
        """Load system defaults into pm, mirroring _load_system_defaults."""
        from griptape_nodes.common.project_templates import ProjectValidationInfo, ProjectValidationStatus
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY, ProjectInfo

        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        situation_schemas = pm._parse_situation_macros(DEFAULT_PROJECT_TEMPLATE.situations, validation)
        directory_schemas = pm._parse_directory_macros(DEFAULT_PROJECT_TEMPLATE.directories, validation)

        project_info = ProjectInfo(
            project_id=SYSTEM_DEFAULTS_KEY,
            project_file_path=None,
            project_base_dir=Path(workspace_dir),
            template=DEFAULT_PROJECT_TEMPLATE,
            validation=validation,
            parsed_situation_schemas=situation_schemas,
            parsed_directory_schemas=directory_schemas,
        )
        pm._successfully_loaded_project_templates[SYSTEM_DEFAULTS_KEY] = project_info
        pm._current_project_id = SYSTEM_DEFAULTS_KEY

    @pytest.mark.asyncio
    async def test_load_workspace_project_not_present(self, pm: ProjectManager, tmp_path: Path) -> None:
        """No project file in workspace leaves system defaults as current project."""
        from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY

        self._setup_system_defaults(pm, str(tmp_path))

        def get_config_value_side_effect(key: str, **_: object) -> str | dict | None:
            if key == "project_file":
                return None
            if "project_workspaces" in key:
                return {}
            return str(tmp_path)

        cast("Mock", pm._config_manager).get_config_value.side_effect = get_config_value_side_effect
        cast("Mock", pm._config_manager).workspace_path = tmp_path

        await pm._load_workspace_project()

        assert pm._current_project_id == SYSTEM_DEFAULTS_KEY

    @pytest.mark.asyncio
    async def test_load_workspace_project_loads_and_sets_current(self, pm: ProjectManager, tmp_path: Path) -> None:
        """Valid griptape-nodes-project.yml is loaded and set as current project."""
        from griptape_nodes.retained_mode.managers.project_manager import WORKSPACE_PROJECT_FILE

        self._setup_system_defaults(pm, str(tmp_path))

        workspace_project_path = tmp_path / WORKSPACE_PROJECT_FILE
        workspace_project_path.write_text(self.VALID_PROJECT_YAML)

        def get_config_value_side_effect(key: str, **_: object) -> str | dict | None:
            if key == "project_file":
                return None
            if "project_workspaces" in key:
                return {}
            return str(tmp_path)

        cast("Mock", pm._config_manager).get_config_value.side_effect = get_config_value_side_effect
        cast("Mock", pm._config_manager).workspace_path = tmp_path

        with patch("griptape_nodes.retained_mode.managers.project_manager.File") as mock_file_cls:
            mock_file_instance = Mock()
            mock_file_instance.aread_text = AsyncMock(return_value=self.VALID_PROJECT_YAML)
            mock_file_cls.return_value = mock_file_instance

            await pm._load_workspace_project()

        assert pm._current_project_id == str(workspace_project_path)
        assert str(workspace_project_path) in pm._successfully_loaded_project_templates

    @pytest.mark.asyncio
    async def test_load_project_denied_by_policy_is_not_cached(self, pm: ProjectManager, tmp_path: Path) -> None:
        """A LoadProject denial blocks the load: the project is not cached as usable."""
        from griptape_nodes.retained_mode.events.project_events import LoadProjectTemplateResultFailure
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import (
            AuthorizationCheckpoint,
            CheckpointDenial,
            CheckpointFailure,
        )
        from griptape_nodes.retained_mode.managers.project_manager import WORKSPACE_PROJECT_FILE

        self._setup_system_defaults(pm, str(tmp_path))
        project_path = tmp_path / WORKSPACE_PROJECT_FILE
        project_path.write_text(self.VALID_PROJECT_YAML)

        seen: dict[str, object] = {}

        def deny(checkpoint: AuthorizationCheckpoint) -> CheckpointDenial | None:
            # Gate only the load; resolved facts (id + name) must be present even
            # though the project is not cached yet.
            if checkpoint.action == "LoadProject":
                seen["subject_id"] = checkpoint.subject_id
                seen["name"] = checkpoint.attributes.get("name")
                return CheckpointDenial(failures=(CheckpointFailure(detail="Ask your admin to grant this project."),))
            return None

        event_manager = GriptapeNodes.EventManager()
        event_manager.add_authorization_hook(deny)
        try:
            with patch("griptape_nodes.retained_mode.managers.project_manager.File") as mock_file_cls:
                mock_file_instance = Mock()
                mock_file_instance.aread_text = AsyncMock(return_value=self.VALID_PROJECT_YAML)
                mock_file_cls.return_value = mock_file_instance
                result = await pm._load_and_cache_project_template(project_path, persist_path=False)
        finally:
            event_manager.remove_authorization_hook(deny)

        assert isinstance(result, LoadProjectTemplateResultFailure)
        assert "Ask your admin to grant this project." in str(result.result_details)
        # The denied project is never cached as usable.
        assert str(project_path) not in pm._successfully_loaded_project_templates
        # The gate resolved the project's name from the in-hand template, not the cache.
        assert seen["name"] == "Workspace Project"

    @pytest.mark.asyncio
    async def test_load_workspace_project_merges_with_defaults(self, pm: ProjectManager, tmp_path: Path) -> None:
        """Workspace project merges on top of defaults, preserving unoverridden situations."""
        from griptape_nodes.retained_mode.managers.project_manager import WORKSPACE_PROJECT_FILE

        self._setup_system_defaults(pm, str(tmp_path))

        workspace_project_path = tmp_path / WORKSPACE_PROJECT_FILE
        workspace_project_path.write_text(self.VALID_PROJECT_YAML)

        def get_config_value_side_effect(key: str, **_: object) -> str | dict | None:
            if key == "project_file":
                return None
            if "project_workspaces" in key:
                return {}
            return str(tmp_path)

        cast("Mock", pm._config_manager).get_config_value.side_effect = get_config_value_side_effect
        cast("Mock", pm._config_manager).workspace_path = tmp_path

        with patch("griptape_nodes.retained_mode.managers.project_manager.File") as mock_file_cls:
            mock_file_instance = Mock()
            mock_file_instance.aread_text = AsyncMock(return_value=self.VALID_PROJECT_YAML)
            mock_file_cls.return_value = mock_file_instance

            await pm._load_workspace_project()

        project_info = pm._successfully_loaded_project_templates[str(workspace_project_path)]
        template = project_info.template

        # Overridden situation uses workspace macro
        assert "custom" in template.situations["save_node_output"].macro

        # Default-only situations are still present (inherited from defaults)
        assert "save_file" in template.situations
        assert "save_griptape_nodes_preview" in template.situations
        assert "copy_external_file" in template.situations

    @pytest.mark.asyncio
    async def test_load_workspace_project_read_failure_keeps_defaults(self, pm: ProjectManager, tmp_path: Path) -> None:
        """A file read failure leaves system defaults as current project.

        _load_workspace_project now delegates to _load_and_cache_project_template, which
        reads via ReadFileRequest. Make the seed path a directory: it passes the
        _resolve_project_file_path existence check but fails the file read, so the
        delegated load returns a failure and the seed activation is skipped.
        """
        from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY, WORKSPACE_PROJECT_FILE

        self._setup_system_defaults(pm, str(tmp_path))

        # A directory at the seed path exists but cannot be read as a file.
        workspace_project_path = tmp_path / WORKSPACE_PROJECT_FILE
        workspace_project_path.mkdir()

        def get_config_value_side_effect(key: str, **_: object) -> str | dict | None:
            if key == "project_file":
                return None
            if "project_workspaces" in key:
                return {}
            return str(tmp_path)

        cast("Mock", pm._config_manager).get_config_value.side_effect = get_config_value_side_effect
        cast("Mock", pm._config_manager).workspace_path = tmp_path

        await pm._load_workspace_project()

        assert pm._current_project_id == SYSTEM_DEFAULTS_KEY

    @pytest.mark.asyncio
    async def test_load_workspace_project_invalid_yaml_keeps_defaults(self, pm: ProjectManager, tmp_path: Path) -> None:
        """Invalid YAML in project file leaves system defaults as current project."""
        from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY, WORKSPACE_PROJECT_FILE

        self._setup_system_defaults(pm, str(tmp_path))

        # Invalid YAML on disk: the delegated loader reads it via ReadFileRequest, fails
        # to parse it into an overlay, and returns a failure without activating.
        workspace_project_path = tmp_path / WORKSPACE_PROJECT_FILE
        workspace_project_path.write_text("not: valid: yaml: ][")

        def get_config_value_side_effect(key: str, **_: object) -> str | dict | None:
            if key == "project_file":
                return None
            if "project_workspaces" in key:
                return {}
            return str(tmp_path)

        cast("Mock", pm._config_manager).get_config_value.side_effect = get_config_value_side_effect
        cast("Mock", pm._config_manager).workspace_path = tmp_path

        await pm._load_workspace_project()

        assert pm._current_project_id == SYSTEM_DEFAULTS_KEY

    @pytest.mark.asyncio
    async def test_load_workspace_project_missing_workspace_dir_skips(self, pm: ProjectManager, tmp_path: Path) -> None:
        """Workspace directory without a project file skips loading without error."""
        from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY

        self._setup_system_defaults(pm)

        cast("Mock", pm._config_manager).get_config_value.return_value = None
        # Point workspace_path at an empty directory so the existence check fails.
        cast("Mock", pm._config_manager).workspace_path = tmp_path

        with patch("griptape_nodes.retained_mode.managers.project_manager.File") as mock_file_cls:
            await pm._load_workspace_project()

            mock_file_cls.assert_not_called()

        assert pm._current_project_id == SYSTEM_DEFAULTS_KEY

    @pytest.mark.asyncio
    async def test_app_initialization_complete_loads_workspace_project(
        self, pm: ProjectManager, tmp_path: Path
    ) -> None:
        """on_app_initialization_complete sets workspace project as current when present."""
        from griptape_nodes.retained_mode.events.app_events import AppInitializationComplete
        from griptape_nodes.retained_mode.managers.project_manager import WORKSPACE_PROJECT_FILE
        from griptape_nodes.retained_mode.managers.settings import PROJECTS_TO_REGISTER_KEY

        workspace_project_path = tmp_path / WORKSPACE_PROJECT_FILE
        workspace_project_path.write_text(self.VALID_PROJECT_YAML)

        def get_config_value_side_effect(key: str, **_: object) -> str | dict | list | None:
            if key == "project_file":
                return None
            if key == PROJECTS_TO_REGISTER_KEY:
                return []
            if "project_workspaces" in key:
                return {}
            return str(tmp_path)

        cast("Mock", pm._config_manager).get_config_value.side_effect = get_config_value_side_effect
        cast("Mock", pm._config_manager).workspace_path = tmp_path

        with patch("griptape_nodes.retained_mode.managers.project_manager.File") as mock_file_cls:
            mock_file_instance = Mock()
            mock_file_instance.aread_text = AsyncMock(return_value=self.VALID_PROJECT_YAML)
            mock_file_cls.return_value = mock_file_instance

            await pm.on_app_initialization_complete(AppInitializationComplete())

        assert pm._current_project_id == str(workspace_project_path)

    @pytest.mark.asyncio
    async def test_app_initialization_complete_uses_defaults_when_no_workspace_project(
        self, pm: ProjectManager, tmp_path: Path
    ) -> None:
        """on_app_initialization_complete keeps system defaults when no workspace project file exists."""
        from griptape_nodes.retained_mode.events.app_events import AppInitializationComplete
        from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY
        from griptape_nodes.retained_mode.managers.settings import PROJECTS_TO_REGISTER_KEY

        def get_config_value_side_effect(key: str, **_: object) -> str | dict | list | None:
            if key == "project_file":
                return None
            if key == PROJECTS_TO_REGISTER_KEY:
                return []
            if "project_workspaces" in key:
                return {}
            return str(tmp_path)

        cast("Mock", pm._config_manager).get_config_value.side_effect = get_config_value_side_effect
        cast("Mock", pm._config_manager).workspace_path = tmp_path

        await pm.on_app_initialization_complete(AppInitializationComplete())

        assert pm._current_project_id == SYSTEM_DEFAULTS_KEY

    @pytest.mark.asyncio
    async def test_app_initialization_complete_activates_seed_without_touching_defaults(
        self, pm: ProjectManager, tmp_path: Path
    ) -> None:
        """A seeded boot project is activated directly; the system-defaults gate is never consulted.

        Regression guard for the boot ordering: with a project_file/workspace seed present,
        on_app_initialization_complete activates the seed instead of first activating
        SYSTEM_DEFAULTS_KEY. So a license policy that denies the defaults rest state does not
        block boot -- the ACTIVATE_PROJECT checkpoint is never evaluated for SYSTEM_DEFAULTS_KEY.
        """
        from griptape_nodes.retained_mode.events.app_events import AppInitializationComplete
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import (
            AuthorizationCheckpoint,
            CheckpointAction,
            CheckpointDenial,
            CheckpointFailure,
        )
        from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY, WORKSPACE_PROJECT_FILE
        from griptape_nodes.retained_mode.managers.settings import PROJECTS_TO_REGISTER_KEY

        workspace_project_path = tmp_path / WORKSPACE_PROJECT_FILE
        workspace_project_path.write_text(self.VALID_PROJECT_YAML)

        def get_config_value_side_effect(key: str, **_: object) -> str | dict | list | None:
            if key == "project_file":
                return None
            if key == PROJECTS_TO_REGISTER_KEY:
                return []
            if "project_workspaces" in key:
                return {}
            return str(tmp_path)

        cast("Mock", pm._config_manager).get_config_value.side_effect = get_config_value_side_effect
        cast("Mock", pm._config_manager).workspace_path = tmp_path

        activated_subject_ids: list[str] = []

        def deny_defaults(checkpoint: AuthorizationCheckpoint) -> CheckpointDenial | None:
            if checkpoint.action == CheckpointAction.ACTIVATE_PROJECT:
                activated_subject_ids.append(checkpoint.subject_id)
                if checkpoint.subject_id == SYSTEM_DEFAULTS_KEY:
                    return CheckpointDenial(
                        failures=(CheckpointFailure(detail="No license covers the default project."),)
                    )
            return None

        event_manager = GriptapeNodes.EventManager()
        event_manager.add_authorization_hook(deny_defaults)
        try:
            with patch("griptape_nodes.retained_mode.managers.project_manager.File") as mock_file_cls:
                mock_file_instance = Mock()
                mock_file_instance.aread_text = AsyncMock(return_value=self.VALID_PROJECT_YAML)
                mock_file_cls.return_value = mock_file_instance

                await pm.on_app_initialization_complete(AppInitializationComplete())
        finally:
            event_manager.remove_authorization_hook(deny_defaults)

        assert pm._current_project_id == str(workspace_project_path)
        assert pm._initialization_complete is True
        # The defaults rest state was never activated, so its (denying) gate was never hit.
        assert SYSTEM_DEFAULTS_KEY not in activated_subject_ids

    @pytest.mark.asyncio
    async def test_app_initialization_complete_falls_back_to_defaults_when_seed_fails(
        self, pm: ProjectManager, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A seed that resolves but fails to load falls back to activating system defaults."""
        from griptape_nodes.retained_mode.events.app_events import AppInitializationComplete
        from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY, WORKSPACE_PROJECT_FILE
        from griptape_nodes.retained_mode.managers.settings import PROJECTS_TO_REGISTER_KEY

        # File exists so the seed path resolves, but its YAML cannot be parsed.
        workspace_project_path = tmp_path / WORKSPACE_PROJECT_FILE
        workspace_project_path.write_text("not: valid: yaml: : :\n  - broken")

        def get_config_value_side_effect(key: str, **_: object) -> str | dict | list | None:
            if key == "project_file":
                return None
            if key == PROJECTS_TO_REGISTER_KEY:
                return []
            if "project_workspaces" in key:
                return {}
            return str(tmp_path)

        cast("Mock", pm._config_manager).get_config_value.side_effect = get_config_value_side_effect
        cast("Mock", pm._config_manager).workspace_path = tmp_path

        with (
            patch("griptape_nodes.retained_mode.managers.project_manager.File") as mock_file_cls,
            caplog.at_level(logging.ERROR, logger="griptape_nodes"),
        ):
            mock_file_instance = Mock()
            mock_file_instance.aread_text = AsyncMock(return_value="not: valid: yaml: : :\n  - broken")
            mock_file_cls.return_value = mock_file_instance

            await pm.on_app_initialization_complete(AppInitializationComplete())

        assert pm._current_project_id == SYSTEM_DEFAULTS_KEY
        assert pm._initialization_complete is True
        # The seed was resolved and attempted before the fallback: prove the ordering,
        # not just the endpoint (which a never-attempted seed would also satisfy).
        error_messages = [r.message for r in caplog.records if r.levelno == logging.ERROR]
        assert any(
            str(workspace_project_path) in msg and "Falling back to system defaults" in msg for msg in error_messages
        )

    @pytest.mark.asyncio
    async def test_app_initialization_complete_does_not_complete_when_no_seed_and_defaults_denied(
        self, pm: ProjectManager, tmp_path: Path
    ) -> None:
        """With no seed and a policy that denies system defaults, boot cannot complete.

        This is the genuinely locked-out case: nothing is reachable, so the engine
        surfaces the failure rather than pretending to have activated a project.
        """
        from griptape_nodes.retained_mode.events.app_events import AppInitializationComplete
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import (
            AuthorizationCheckpoint,
            CheckpointAction,
            CheckpointDenial,
            CheckpointFailure,
        )
        from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY
        from griptape_nodes.retained_mode.managers.settings import PROJECTS_TO_REGISTER_KEY

        # No project_file seed and no workspace griptape-nodes-project.yml on disk.
        def get_config_value_side_effect(key: str, **_: object) -> str | dict | list | None:
            if key == "project_file":
                return None
            if key == PROJECTS_TO_REGISTER_KEY:
                return []
            if "project_workspaces" in key:
                return {}
            return str(tmp_path)

        cast("Mock", pm._config_manager).get_config_value.side_effect = get_config_value_side_effect
        cast("Mock", pm._config_manager).workspace_path = tmp_path

        def deny_defaults(checkpoint: AuthorizationCheckpoint) -> CheckpointDenial | None:
            if checkpoint.action == CheckpointAction.ACTIVATE_PROJECT and checkpoint.subject_id == SYSTEM_DEFAULTS_KEY:
                return CheckpointDenial(failures=(CheckpointFailure(detail="No license covers the default project."),))
            return None

        event_manager = GriptapeNodes.EventManager()
        event_manager.add_authorization_hook(deny_defaults)
        try:
            await pm.on_app_initialization_complete(AppInitializationComplete())
        finally:
            event_manager.remove_authorization_hook(deny_defaults)

        # The denied activation left the current project untouched and boot short-circuited.
        assert pm._current_project_id == SYSTEM_DEFAULTS_KEY
        assert pm._initialization_complete is False

    @pytest.mark.asyncio
    async def test_load_workspace_project_uses_project_file_setting(self, pm: ProjectManager, tmp_path: Path) -> None:
        """When project_file config is set, that path is used instead of workspace default."""
        self._setup_system_defaults(pm, str(tmp_path))

        # Project file is outside the workspace directory
        external_project_path = tmp_path / "external" / "my-project.yml"
        external_project_path.parent.mkdir(parents=True)
        external_project_path.write_text(self.VALID_PROJECT_YAML)

        def get_config_value_side_effect(key: str, **_: object) -> str | dict | None:
            if key == "project_file":
                return str(external_project_path)
            if "project_workspaces" in key:
                return {}
            return str(tmp_path)

        cast("Mock", pm._config_manager).get_config_value.side_effect = get_config_value_side_effect
        cast("Mock", pm._config_manager).workspace_path = tmp_path

        with patch("griptape_nodes.retained_mode.managers.project_manager.File") as mock_file_cls:
            mock_file_instance = Mock()
            mock_file_instance.aread_text = AsyncMock(return_value=self.VALID_PROJECT_YAML)
            mock_file_cls.return_value = mock_file_instance

            await pm._load_workspace_project()

        assert pm._current_project_id == str(external_project_path)
        assert str(external_project_path) in pm._successfully_loaded_project_templates

    @pytest.mark.asyncio
    async def test_load_workspace_project_uses_workspace_default_when_no_project_file_setting(
        self, pm: ProjectManager, tmp_path: Path
    ) -> None:
        """When project_file config is None, falls back to workspace/griptape-nodes-project.yml."""
        from griptape_nodes.retained_mode.managers.project_manager import WORKSPACE_PROJECT_FILE

        self._setup_system_defaults(pm, str(tmp_path))

        workspace_project_path = tmp_path / WORKSPACE_PROJECT_FILE
        workspace_project_path.write_text(self.VALID_PROJECT_YAML)

        def get_config_value_side_effect(key: str, **_: object) -> str | dict | None:
            if key == "project_file":
                return None
            if "project_workspaces" in key:
                return {}
            return str(tmp_path)

        cast("Mock", pm._config_manager).get_config_value.side_effect = get_config_value_side_effect
        cast("Mock", pm._config_manager).workspace_path = tmp_path

        with patch("griptape_nodes.retained_mode.managers.project_manager.File") as mock_file_cls:
            mock_file_instance = Mock()
            mock_file_instance.aread_text = AsyncMock(return_value=self.VALID_PROJECT_YAML)
            mock_file_cls.return_value = mock_file_instance

            await pm._load_workspace_project()

        assert pm._current_project_id == str(workspace_project_path)

    @pytest.mark.asyncio
    async def test_load_workspace_project_project_file_setting_nonexistent_falls_back_to_workspace(
        self, pm: ProjectManager, tmp_path: Path
    ) -> None:
        """When project_file config points to a nonexistent file, falls back to the workspace default."""
        from griptape_nodes.retained_mode.managers.project_manager import WORKSPACE_PROJECT_FILE

        self._setup_system_defaults(pm, str(tmp_path))

        nonexistent_path = tmp_path / "does_not_exist.yml"

        # Workspace default exists and should be loaded as fallback
        workspace_project_path = tmp_path / WORKSPACE_PROJECT_FILE
        workspace_project_path.write_text(self.VALID_PROJECT_YAML)

        def get_config_value_side_effect(key: str, **_: object) -> str | dict | None:
            if key == "project_file":
                return str(nonexistent_path)
            if "project_workspaces" in key:
                return {}
            return str(tmp_path)

        cast("Mock", pm._config_manager).get_config_value.side_effect = get_config_value_side_effect
        cast("Mock", pm._config_manager).workspace_path = tmp_path

        with patch("griptape_nodes.retained_mode.managers.project_manager.File") as mock_file_cls:
            mock_file_instance = Mock()
            mock_file_instance.aread_text = AsyncMock(return_value=self.VALID_PROJECT_YAML)
            mock_file_cls.return_value = mock_file_instance

            await pm._load_workspace_project()

        assert pm._current_project_id == str(workspace_project_path)

    @pytest.mark.asyncio
    async def test_seed_child_inherits_parent_situations_and_directories(
        self, pm: ProjectManager, tmp_path: Path
    ) -> None:
        """A child activated as the boot seed inherits its parent's situations/directories.

        Regression for the bug where the seed loader merged only onto system defaults and
        dropped the parent chain (so a child seed lost its parent's situations/directories
        until a manual Reload from Disk). The seed now loads through the shared
        parent-chain-aware loader, so inheritance resolves on boot.
        """
        from griptape_nodes.retained_mode.managers.project_manager import WORKSPACE_PROJECT_FILE

        self._setup_system_defaults(pm, str(tmp_path))

        # Parent (registered elsewhere) defines a situation + directory the child does not.
        parent_dir = tmp_path / "parent"
        parent_dir.mkdir()
        parent_path = parent_dir / "griptape-nodes-project.yml"
        parent_path.write_text(
            "project_template_schema_version: '1.0.0'\n"
            "name: Parent\n"
            "id: parent-abc\n"
            "situations:\n"
            "  save_prompt:\n"
            "    macro: '{prompts}/{file_name_base}.{file_extension}'\n"
            "    policy:\n"
            "      on_collision: create_new\n"
            "      create_dirs: true\n"
            "directories:\n"
            "  prompts:\n"
            "    path_macro: prompts\n"
        )
        await pm._load_and_cache_project_template(parent_path, persist_path=False)

        # Child is the workspace seed and declares only its parent link + an env var.
        child_path = tmp_path / WORKSPACE_PROJECT_FILE
        child_path.write_text(
            "project_template_schema_version: '1.0.0'\n"
            "name: Child\n"
            "id: child-xyz\n"
            "parent_project_id: 'parent-abc'\n"
            "environment:\n"
            "  SHOW: child\n"
        )

        def get_config_value_side_effect(key: str, **_: object) -> str | dict | None:
            if key == "project_file":
                return None
            if "project_workspaces" in key:
                return {}
            return str(tmp_path)

        cast("Mock", pm._config_manager).get_config_value.side_effect = get_config_value_side_effect
        cast("Mock", pm._config_manager).workspace_path = tmp_path
        # Activation resolves the workspace via adjacent project config; return no
        # workspace_directory so the decision falls through to the global default.
        cast("Mock", pm._config_manager).read_config_file.return_value = {}

        await pm._load_workspace_project()

        assert pm._current_project_id == "child-xyz"
        child_info = pm._successfully_loaded_project_templates["child-xyz"]
        # Inherited from the parent (would be absent under the old defaults-only merge).
        assert "save_prompt" in child_info.template.situations
        assert "prompts" in child_info.template.directories
        # The child's own override is still applied.
        assert child_info.template.environment.get("SHOW") == "child"

    @pytest.mark.asyncio
    async def test_seed_child_resolves_id_parent_registered_only_in_config(
        self, pm: ProjectManager, tmp_path: Path
    ) -> None:
        """Boot resolves a child seed's id-parent that lives only in projects_to_register.

        Covers the ordering fix: on_app_initialization_complete builds the boot id-index
        before activating the seed, so an id-based parent that has not been loaded yet
        (only registered in projects_to_register) is still locatable when the seed's
        parent chain resolves.
        """
        from griptape_nodes.retained_mode.events.app_events import AppInitializationComplete
        from griptape_nodes.retained_mode.managers.project_manager import WORKSPACE_PROJECT_FILE
        from griptape_nodes.retained_mode.managers.settings import PROJECTS_TO_REGISTER_KEY

        self._setup_system_defaults(pm, str(tmp_path))

        parent_dir = tmp_path / "parent"
        parent_dir.mkdir()
        parent_path = parent_dir / "griptape-nodes-project.yml"
        parent_path.write_text(
            "project_template_schema_version: '1.0.0'\n"
            "name: Parent\n"
            "id: parent-abc\n"
            "directories:\n"
            "  prompts:\n"
            "    path_macro: prompts\n"
        )

        # Child is the workspace-dir seed; only the parent is in projects_to_register.
        child_path = tmp_path / WORKSPACE_PROJECT_FILE
        child_path.write_text(
            "project_template_schema_version: '1.0.0'\nname: Child\nid: child-xyz\nparent_project_id: 'parent-abc'\n"
        )

        def get_config_value_side_effect(key: str, **_: object) -> object:
            if key == "project_file":
                return None
            if key == PROJECTS_TO_REGISTER_KEY:
                return [str(parent_path)]
            if "project_workspaces" in key:
                return {}
            return str(tmp_path)

        cast("Mock", pm._config_manager).get_config_value.side_effect = get_config_value_side_effect
        cast("Mock", pm._config_manager).workspace_path = tmp_path
        cast("Mock", pm._config_manager).read_config_file.return_value = {}

        await pm.on_app_initialization_complete(AppInitializationComplete())

        assert pm._current_project_id == "child-xyz"
        child_info = pm._successfully_loaded_project_templates["child-xyz"]
        assert "prompts" in child_info.template.directories
        # The boot id-index is boot-only and cleared once loading finishes.
        assert pm._boot_id_to_file_path == {}


class TestLoadSelectsDefaultByMajor:
    """End-to-end: loading a project file merges it onto the default for its OWN major.

    Guards the integration the unit tests for default_template_for_version cannot: that
    the real load path (_load_and_cache_project_template -> _resolve_parent_chain -> merge)
    actually picks the v0 baseline for a v0 project and the v1 baseline for a v1 project.
    A v0 project keeps the legacy workspace-root-relative dirs; a v1 project gets the
    workflow-relative dirs. Both are parentless, so the base is the major-selected default.
    """

    V0_PROJECT_YAML = """\
project_template_schema_version: "0.5.1"
name: Legacy Project
"""

    V1_PROJECT_YAML = """\
project_template_schema_version: "1.0.0"
name: Modern Project
"""

    @pytest.fixture
    def pm(self) -> ProjectManager:
        mock_event_manager = Mock()
        mock_config_manager = Mock()
        mock_config_manager.project_config = {}
        mock_config_manager.env_config = {}
        mock_config_manager.merged_config = {}
        mock_config_manager.get_config_value.return_value = {}
        return ProjectManager(mock_event_manager, mock_config_manager, Mock())

    async def _load_yaml(self, pm: ProjectManager, tmp_path: Path, yaml_text: str):  # noqa: ANN202
        from griptape_nodes.retained_mode.events.project_events import LoadProjectTemplateResultSuccess

        project_path = tmp_path / "griptape-nodes-project.yml"
        project_path.write_text(yaml_text)
        with patch("griptape_nodes.retained_mode.managers.project_manager.File") as mock_file_cls:
            mock_file_instance = Mock()
            mock_file_instance.aread_text = AsyncMock(return_value=yaml_text)
            mock_file_cls.return_value = mock_file_instance
            result = await pm._load_and_cache_project_template(project_path, persist_path=False)
        assert isinstance(result, LoadProjectTemplateResultSuccess)
        return pm._successfully_loaded_project_templates[str(project_path)].template

    @pytest.mark.asyncio
    async def test_v0_project_loads_onto_v0_layout(self, pm: ProjectManager, tmp_path: Path) -> None:
        template = await self._load_yaml(pm, tmp_path, self.V0_PROJECT_YAML)
        # Legacy baseline: dirs are workspace-root relative (not workflow-relative).
        assert template.directories["inputs"].path_macro == "inputs"
        assert "{file_extension_directory" not in template.situations["save_node_output"].macro

    @pytest.mark.asyncio
    async def test_v1_project_loads_onto_v1_layout(self, pm: ProjectManager, tmp_path: Path) -> None:
        template = await self._load_yaml(pm, tmp_path, self.V1_PROJECT_YAML)
        # v1 baseline: workflow-relative dirs and file_extension_directory routing.
        assert template.directories["inputs"].path_macro == "{workflow_dir?:/}inputs"
        assert "{file_extension_directory" in template.situations["save_node_output"].macro

    @pytest.mark.asyncio
    async def test_malformed_version_loads_against_latest_without_crashing(
        self, pm: ProjectManager, tmp_path: Path
    ) -> None:
        # Version strings are user-controlled. The per-major merge-base selection must not raise
        # on a non-semver value (it would crash the load / boot); it falls back to the latest
        # default instead.
        template = await self._load_yaml(
            pm, tmp_path, 'project_template_schema_version: "not-a-version"\nname: Garbage\n'
        )
        # Fell back to the latest (v1) baseline rather than raising.
        assert template.directories["inputs"].path_macro == "{workflow_dir?:/}inputs"


class TestUpgradeProjectSchema:
    """Elective v0 -> v1 schema upgrade: restamp to latest major and re-save."""

    V0_PROJECT_YAML = """\
project_template_schema_version: "0.5.1"
name: Legacy Project
"""

    @pytest.fixture
    def pm(self) -> ProjectManager:
        mock_event_manager = Mock()
        mock_config_manager = Mock()
        mock_config_manager.project_config = {}
        mock_config_manager.env_config = {}
        mock_config_manager.merged_config = {}
        mock_config_manager.get_config_value.return_value = {}
        return ProjectManager(mock_event_manager, mock_config_manager, Mock())

    async def _load(self, pm: ProjectManager, tmp_path: Path, yaml_text: str) -> str:
        """Load a project from disk and return its registry id."""
        from griptape_nodes.retained_mode.events.project_events import LoadProjectTemplateResultSuccess

        project_path = tmp_path / "griptape-nodes-project.yml"
        project_path.write_text(yaml_text)
        with patch("griptape_nodes.retained_mode.managers.project_manager.File") as mock_file_cls:
            mock_file_instance = Mock()
            mock_file_instance.aread_text = AsyncMock(return_value=yaml_text)
            mock_file_cls.return_value = mock_file_instance
            result = await pm._load_and_cache_project_template(project_path, persist_path=False)
        assert isinstance(result, LoadProjectTemplateResultSuccess)
        return next(
            pid
            for pid, info in pm._successfully_loaded_project_templates.items()
            if info.project_file_path == canonicalize_for_identity(project_path)
        )

    @pytest.mark.asyncio
    async def test_upgrade_v0_to_latest_writes_new_major(self, pm: ProjectManager, tmp_path: Path) -> None:
        from griptape_nodes.common.project_templates import ProjectTemplate
        from griptape_nodes.retained_mode.events.project_events import (
            UpgradeProjectSchemaRequest,
            UpgradeProjectSchemaResultSuccess,
        )

        # A minimal v0 project: only name + version, no explicit directory/situation overrides.
        # On upgrade it must ADOPT the v1 layout, not pin the materialized v0 defaults.
        project_id = await self._load(pm, tmp_path, self.V0_PROJECT_YAML)

        written: dict[str, str] = {}
        with patch("griptape_nodes.retained_mode.managers.project_manager.File") as mock_file_cls:
            mock_file_instance = Mock()
            mock_file_instance.write_text = lambda content: written.update(yaml=content)
            mock_file_cls.return_value = mock_file_instance
            result = await pm.on_upgrade_project_schema_request(UpgradeProjectSchemaRequest(project_id=project_id))

        assert isinstance(result, UpgradeProjectSchemaResultSuccess)
        assert result.previous_schema_version == "0.5.1"
        assert result.new_schema_version == ProjectTemplate.LATEST_SCHEMA_VERSION
        # The re-saved file carries the new major version.
        assert f'"project_template_schema_version": "{ProjectTemplate.LATEST_SCHEMA_VERSION}"' in written["yaml"]
        # ADOPTION, not relabel: the project had no explicit directory override, so the upgraded
        # overlay must NOT pin the old v0 "inputs" macro -- it falls through to the v1 default.
        assert '"inputs"' not in written["yaml"]
        assert "directories" not in written["yaml"]

    @pytest.mark.asyncio
    async def test_upgrade_already_latest_is_failure(self, pm: ProjectManager, tmp_path: Path) -> None:
        from griptape_nodes.common.project_templates import ProjectTemplate
        from griptape_nodes.retained_mode.events.project_events import (
            UpgradeProjectSchemaRequest,
            UpgradeProjectSchemaResultFailure,
        )

        latest_yaml = f'project_template_schema_version: "{ProjectTemplate.LATEST_SCHEMA_VERSION}"\nname: Modern\n'
        project_id = await self._load(pm, tmp_path, latest_yaml)

        result = await pm.on_upgrade_project_schema_request(UpgradeProjectSchemaRequest(project_id=project_id))

        assert isinstance(result, UpgradeProjectSchemaResultFailure)
        assert "is not an older major than the latest" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_upgrade_unloaded_project_is_failure(self, pm: ProjectManager) -> None:
        from griptape_nodes.retained_mode.events.project_events import (
            UpgradeProjectSchemaRequest,
            UpgradeProjectSchemaResultFailure,
        )

        result = await pm.on_upgrade_project_schema_request(UpgradeProjectSchemaRequest(project_id="not-loaded"))

        assert isinstance(result, UpgradeProjectSchemaResultFailure)
        assert "not loaded" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_upgrade_future_major_is_refused_not_downgraded(self, pm: ProjectManager, tmp_path: Path) -> None:
        # The load path forward-compat-accepts an unknown future major; the upgrade handler must
        # NOT restamp it DOWN to the (older) latest, which would be a silent schema downgrade.
        from griptape_nodes.retained_mode.events.project_events import (
            UpgradeProjectSchemaRequest,
            UpgradeProjectSchemaResultFailure,
        )

        future_yaml = 'project_template_schema_version: "2.0.0"\nname: FromTheFuture\n'
        project_id = await self._load(pm, tmp_path, future_yaml)

        result = await pm.on_upgrade_project_schema_request(UpgradeProjectSchemaRequest(project_id=project_id))

        assert isinstance(result, UpgradeProjectSchemaResultFailure)
        assert "is not an older major than the latest" in str(result.result_details)
        # The on-disk file is untouched (no downgrade).
        assert "2.0.0" in (tmp_path / "griptape-nodes-project.yml").read_text()

    @pytest.mark.asyncio
    async def test_upgrade_malformed_version_fails_gracefully(self, pm: ProjectManager, tmp_path: Path) -> None:
        # The load path tolerates a malformed version, so the upgrade handler must not raise on
        # one -- it returns a failure result instead of crashing the request dispatch.
        from griptape_nodes.retained_mode.events.project_events import (
            UpgradeProjectSchemaRequest,
            UpgradeProjectSchemaResultFailure,
        )

        bad_yaml = 'project_template_schema_version: "not-a-version"\nname: Garbage\n'
        project_id = await self._load(pm, tmp_path, bad_yaml)

        result = await pm.on_upgrade_project_schema_request(UpgradeProjectSchemaRequest(project_id=project_id))

        assert isinstance(result, UpgradeProjectSchemaResultFailure)

    async def _load_at(self, pm: ProjectManager, project_dir: Path, yaml_text: str) -> str:
        """Load a project from its own directory and return its registry id.

        Distinct from _load (which uses a single fixed path) so a parent and child can be loaded at
        separate dirs and linked by parent_project_id.
        """
        from griptape_nodes.retained_mode.events.project_events import LoadProjectTemplateResultSuccess

        project_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
        project_path = project_dir / "griptape-nodes-project.yml"
        project_path.write_text(yaml_text)
        result = await pm._load_and_cache_project_template(project_path, persist_path=False)
        assert isinstance(result, LoadProjectTemplateResultSuccess)
        return next(
            pid
            for pid, info in pm._successfully_loaded_project_templates.items()
            if info.project_file_path == canonicalize_for_identity(project_path)
        )

    @pytest.mark.asyncio
    async def test_upgrade_child_with_older_major_parent_is_refused(self, pm: ProjectManager, tmp_path: Path) -> None:
        # A child re-stamped to the latest major but merged onto a still-v0 parent would keep the
        # old-major defaults for every un-overridden field while its version label says v1. Refuse it
        # (upgrade the parent first) rather than report a hollow success.
        from griptape_nodes.retained_mode.events.project_events import (
            UpgradeProjectSchemaRequest,
            UpgradeProjectSchemaResultFailure,
        )

        parent_id = await self._load_at(
            pm, tmp_path / "parent", 'project_template_schema_version: "0.5.1"\nname: Parent\nid: parent-id\n'
        )
        child_id = await self._load_at(
            pm,
            tmp_path / "child",
            f'project_template_schema_version: "0.5.1"\nname: Child\nid: child-id\nparent_project_id: "{parent_id}"\n',
        )

        result = await pm.on_upgrade_project_schema_request(UpgradeProjectSchemaRequest(project_id=child_id))

        assert isinstance(result, UpgradeProjectSchemaResultFailure)
        assert "parent" in str(result.result_details).lower()
        # The child's on-disk file is untouched (not re-stamped to a version its layout doesn't match).
        assert '"0.5.1"' in (tmp_path / "child" / "griptape-nodes-project.yml").read_text()

    @pytest.mark.asyncio
    async def test_upgrade_child_succeeds_once_parent_is_new_major(self, pm: ProjectManager, tmp_path: Path) -> None:
        # With the parent already on the latest major, the child's merge base is new-major, so the
        # child CAN adopt the new defaults -- the upgrade succeeds.
        from griptape_nodes.common.project_templates import ProjectTemplate
        from griptape_nodes.retained_mode.events.project_events import (
            UpgradeProjectSchemaRequest,
            UpgradeProjectSchemaResultSuccess,
        )

        latest = ProjectTemplate.LATEST_SCHEMA_VERSION
        parent_id = await self._load_at(
            pm, tmp_path / "parent", f'project_template_schema_version: "{latest}"\nname: Parent\nid: parent-id\n'
        )
        child_id = await self._load_at(
            pm,
            tmp_path / "child",
            f'project_template_schema_version: "0.5.1"\nname: Child\nid: child-id\nparent_project_id: "{parent_id}"\n',
        )

        written: dict[str, str] = {}
        with patch("griptape_nodes.retained_mode.managers.project_manager.File") as mock_file_cls:
            mock_file_instance = Mock()
            mock_file_instance.write_text = lambda content: written.update(yaml=content)
            mock_file_cls.return_value = mock_file_instance
            result = await pm.on_upgrade_project_schema_request(UpgradeProjectSchemaRequest(project_id=child_id))

        assert isinstance(result, UpgradeProjectSchemaResultSuccess)
        assert result.new_schema_version == latest
        assert f'"project_template_schema_version": "{latest}"' in written["yaml"]


class TestLoadSystemDefaults:
    """Test _load_system_defaults uses resolved workspace path for project_base_dir."""

    @pytest.fixture
    def pm(self) -> ProjectManager:
        mock_event_manager = Mock()
        mock_config_manager = Mock()
        mock_config_manager.project_config = {}
        mock_config_manager.env_config = {}
        mock_config_manager.merged_config = {}
        mock_config_manager.get_config_value.return_value = {}
        return ProjectManager(mock_event_manager, mock_config_manager, Mock())

    def test_project_base_dir_uses_resolved_workspace_path(self, pm: ProjectManager) -> None:
        """Test that _load_system_defaults uses config_manager.workspace_path (resolved) for project_base_dir.

        This ensures project_base_dir matches the resolved paths used for macro resolution,
        preventing workspace-internal files from being treated as external.
        """
        from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY

        resolved_path = Path("/Users/testuser/GriptapeNodes")
        cast("Mock", pm._config_manager).workspace_path = resolved_path

        pm._load_system_defaults()

        project_info = pm._successfully_loaded_project_templates[SYSTEM_DEFAULTS_KEY]
        assert project_info.project_base_dir == resolved_path

    def test_project_base_dir_not_raw_config_value(self, pm: ProjectManager) -> None:
        """Test that _load_system_defaults does NOT use the raw config value with ~ for project_base_dir.

        Previously, _load_system_defaults used get_config_value("workspace_directory") which
        returns the raw string (e.g., "~/GriptapeNodes"). This caused a mismatch with resolved
        source paths, making workspace-internal files appear external in preview URL generation.
        """
        from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY

        resolved_path = Path("/Users/testuser/GriptapeNodes")
        cast("Mock", pm._config_manager).workspace_path = resolved_path
        cast("Mock", pm._config_manager).get_config_value.return_value = "~/GriptapeNodes"

        pm._load_system_defaults()

        project_info = pm._successfully_loaded_project_templates[SYSTEM_DEFAULTS_KEY]
        # Should use the resolved path, not the raw config value
        assert project_info.project_base_dir == resolved_path
        assert str(project_info.project_base_dir) != "~/GriptapeNodes"


class TestDecideWorkspace:
    """`decide_workspace` decides a project's workspace-layer dir + override bit read-only.

    Both the live `_activate_project` block and the provisioning preview drive off this
    one decision. `apply_override` is True only for the project_workspaces mapping and the
    auto-default branches (where activation calls set_workspace_override); it is False for
    an env/project-adjacent workspace_directory so the workspace config layer can re-point
    it. The TestProjectManagerProjectWorkspaces matrix guards the live path; this guards the
    decision both paths share.
    """

    @staticmethod
    def _pm_with_project_workspaces(project_workspaces: dict[str, str]) -> ProjectManager:
        mock_config = Mock()
        mock_config.get_config_value.return_value = project_workspaces
        return ProjectManager(Mock(), mock_config, Mock())

    @staticmethod
    def _pm_with_config(
        project_workspaces: dict[str, str],
        configured_root: str | None,
        default_root: str | None = None,
    ) -> ProjectManager:
        """Build a ProjectManager whose config distinguishes the branch-4 reads.

        The single-return _pm_with_project_workspaces helper can't tell apart the
        project_workspaces lookup from the configured-root workspace_directory reads (user_config
        then default_config), which the inheritance branch needs. This keys the mock on
        (key, config_source) instead. `configured_root` is the user_config layer value;
        `default_root` is the default_config fallback the branch reads when user_config is None
        (in production this is always populated by the Settings default).
        """
        mock_config = Mock()

        def fake_get(key: str, *, config_source: str = "merged_config", default: Any = None, **_: Any) -> Any:
            if key == "project_workspaces":
                return project_workspaces
            if key == "workspace_directory" and config_source == "user_config":
                return configured_root
            if key == "workspace_directory" and config_source == "default_config":
                return default_root
            return default

        mock_config.get_config_value.side_effect = fake_get
        return ProjectManager(Mock(), mock_config, Mock())

    @staticmethod
    def _pm_with_chain(
        specs: list[dict[str, Any]],
        *,
        project_workspaces: dict[str, str] | None = None,
        configured_root: str | None = None,
        default_root: str | None = None,
    ) -> ProjectManager:
        """Build a ProjectManager whose registry models an explicit parent chain.

        Each spec is a dict with keys `id` (registry key / parent link target),
        `file` (Path to the project YAML, or None for a file-less project like system
        defaults), `parent_id` (the spec's parent_project_id, or None), and an optional
        `config` dict standing in for that project's adjacent griptape_nodes_config.json.
        The mock `read_config_file` returns a spec's `config` keyed on the directory of
        its file; `get_config_value` resolves project_workspaces and the global
        workspace_directory (user_config then default_config) so the branch-4 walk and
        branch-5 fallback both have realistic inputs.
        """
        from griptape_nodes.common.project_templates import ProjectValidationInfo, ProjectValidationStatus
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.retained_mode.managers.project_manager import ProjectInfo

        project_workspaces = project_workspaces or {}
        mock_config = Mock()

        def fake_get(key: str, *, config_source: str = "merged_config", default: Any = None, **_: Any) -> Any:
            if key == "project_workspaces":
                return project_workspaces
            if key == "workspace_directory" and config_source == "user_config":
                return configured_root
            if key == "workspace_directory" and config_source == "default_config":
                return default_root
            return default

        mock_config.get_config_value.side_effect = fake_get

        dir_to_config: dict[Path, dict] = {
            Path(spec["file"]).parent: spec["config"]
            for spec in specs
            if spec.get("file") is not None and "config" in spec
        }

        def fake_read_config_file(path: Path) -> dict:
            return dir_to_config.get(Path(path).parent, {})

        mock_config.read_config_file.side_effect = fake_read_config_file

        pm = ProjectManager(Mock(), mock_config, Mock())
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        for spec in specs:
            file_path = spec.get("file")
            base_dir = Path(file_path).parent if file_path is not None else Path("/")
            template = DEFAULT_PROJECT_TEMPLATE.model_copy(
                update={
                    "parent_project_id": spec.get("parent_id"),
                    "libraries_dir": spec.get("libraries_dir"),
                    "workspace_dir": spec.get("workspace_dir"),
                }
            )
            pm._successfully_loaded_project_templates[spec["id"]] = ProjectInfo(
                project_id=spec["id"],
                project_file_path=Path(file_path) if file_path is not None else None,
                project_base_dir=base_dir,
                template=template,
                validation=validation,
                parsed_situation_schemas={},
                parsed_directory_schemas={},
            )
        return pm

    def test_project_workspaces_override_wins(self, tmp_path: Path) -> None:
        project_file = tmp_path / "project.yml"
        project_file.touch()
        mapped_workspace = tmp_path / "mapped"

        pm = self._pm_with_project_workspaces({str(project_file): str(mapped_workspace)})
        decision = pm.decide_workspace(
            project_file,
            project_config={"workspace_directory": "/ignored/project"},
            env_config={"workspace_directory": "/ignored/env"},
        )

        assert decision.workspace_dir == Path(str(mapped_workspace))
        assert decision.apply_override is True

    def test_project_workspaces_override_by_id_wins(self, tmp_path: Path) -> None:
        """A project_workspaces key may be the project's opaque ID, not just its path."""
        project_file = tmp_path / "project.yml"
        project_file.touch()
        mapped_workspace = tmp_path / "mapped"

        pm = self._pm_with_chain(
            [{"id": "my-guid", "file": project_file, "parent_id": None}],
            project_workspaces={"my-guid": str(mapped_workspace)},
        )
        decision = pm.decide_workspace(
            project_file,
            project_config={"workspace_directory": "/ignored/project"},
            env_config={"workspace_directory": "/ignored/env"},
        )

        assert decision.workspace_dir == Path(str(mapped_workspace))
        assert decision.apply_override is True

    def test_project_workspaces_unmatched_id_falls_back_to_path_key(self, tmp_path: Path) -> None:
        """A key that is not a loaded ID is still honored as a project file path."""
        project_file = tmp_path / "project.yml"
        project_file.touch()
        mapped_workspace = tmp_path / "mapped"

        pm = self._pm_with_chain(
            [{"id": "my-guid", "file": project_file, "parent_id": None}],
            project_workspaces={str(project_file): str(mapped_workspace)},
        )
        decision = pm.decide_workspace(
            project_file,
            project_config={"workspace_directory": "/ignored/project"},
            env_config={"workspace_directory": "/ignored/env"},
        )

        assert decision.workspace_dir == Path(str(mapped_workspace))
        assert decision.apply_override is True

    def test_env_workspace_wins_over_project_adjacent(self, tmp_path: Path) -> None:
        project_file = tmp_path / "project.yml"
        project_file.touch()

        pm = self._pm_with_project_workspaces({})
        decision = pm.decide_workspace(
            project_file,
            project_config={"workspace_directory": "/from/project"},
            env_config={"workspace_directory": "/from/env"},
        )

        assert decision.workspace_dir == Path("/from/env")
        assert decision.apply_override is False

    def test_project_adjacent_workspace_used_when_no_override_or_env(self, tmp_path: Path) -> None:
        project_file = tmp_path / "project.yml"
        project_file.touch()

        pm = self._pm_with_project_workspaces({})
        decision = pm.decide_workspace(
            project_file,
            project_config={"workspace_directory": "/from/project"},
            env_config={},
        )

        assert decision.workspace_dir == Path("/from/project")
        assert decision.apply_override is False

    def test_auto_defaults_to_project_dir(self, tmp_path: Path) -> None:
        project_file = tmp_path / "project.yml"
        project_file.touch()

        pm = self._pm_with_config(project_workspaces={}, configured_root=None)
        decision = pm.decide_workspace(project_file, project_config={}, env_config={})

        assert decision.workspace_dir == project_file.parent
        assert decision.apply_override is True

    def test_three_level_inherits_nearest_ancestor_workspace(self, tmp_path: Path) -> None:
        """C inherits B's workspace when B (the nearest ancestor) defines one, not A's."""
        a_file = tmp_path / "a" / "griptape-nodes-project.yml"
        b_file = tmp_path / "b" / "griptape-nodes-project.yml"
        c_file = tmp_path / "c" / "griptape-nodes-project.yml"
        for f in (a_file, b_file, c_file):
            f.parent.mkdir(parents=True)
            f.touch()

        pm = self._pm_with_chain(
            [
                {"id": "A", "file": a_file, "parent_id": None, "config": {"workspace_directory": "/ws/a"}},
                {"id": "B", "file": b_file, "parent_id": "A", "config": {"workspace_directory": "/ws/b"}},
                {"id": "C", "file": c_file, "parent_id": "B", "config": {}},
            ],
            configured_root="/global/ws",
        )
        decision = pm.decide_workspace(c_file, project_config={}, env_config={})

        assert decision.workspace_dir == Path("/ws/b")
        assert decision.apply_override is True

    def test_three_level_skips_to_grandparent_when_parent_has_none(self, tmp_path: Path) -> None:
        """C inherits A's workspace when B (its parent) defines none but A does."""
        a_file = tmp_path / "a" / "griptape-nodes-project.yml"
        b_file = tmp_path / "b" / "griptape-nodes-project.yml"
        c_file = tmp_path / "c" / "griptape-nodes-project.yml"
        for f in (a_file, b_file, c_file):
            f.parent.mkdir(parents=True)
            f.touch()

        pm = self._pm_with_chain(
            [
                {"id": "A", "file": a_file, "parent_id": None, "config": {"workspace_directory": "/ws/a"}},
                {"id": "B", "file": b_file, "parent_id": "A", "config": {}},
                {"id": "C", "file": c_file, "parent_id": "B", "config": {}},
            ],
            configured_root="/global/ws",
        )
        decision = pm.decide_workspace(c_file, project_config={}, env_config={})

        assert decision.workspace_dir == Path("/ws/a")
        assert decision.apply_override is True

    def test_inherits_parent_template_workspace_dir_resolved_against_parent(self, tmp_path: Path) -> None:
        """A child with no workspace_dir inherits the parent's workspace_dir TEMPLATE FIELD.

        Regression: previously the parent-chain walk only read an ancestor's project_workspaces
        override / adjacent config, NOT its workspace_dir template field. A parent that declared only
        workspace_dir (the common self-contained "./" case) was therefore not inheritable, so the
        child fell through to the global workspace and scanned the whole workspace tree for workflows.
        The parent's relative "./" must resolve against the PARENT's dir, not the child's.
        """
        parent_file = tmp_path / "parent" / "griptape-nodes-project.yml"
        child_file = tmp_path / "parent" / "child" / "griptape-nodes-project.yml"
        for f in (parent_file, child_file):
            f.parent.mkdir(parents=True)
            f.touch()

        pm = self._pm_with_chain(
            [
                {"id": "P", "file": parent_file, "parent_id": None, "workspace_dir": "./", "config": {}},
                {"id": "C", "file": child_file, "parent_id": "P", "config": {}},
            ],
            configured_root="/global/ws",
        )
        decision = pm.decide_workspace(child_file, project_config={}, env_config={})

        # Parent's "./" resolves to the PARENT's dir -- not the child's, not the global default.
        assert decision.workspace_dir == Path(str(canonicalize_for_identity(parent_file.parent)))
        assert decision.workspace_dir != Path(str(canonicalize_for_identity(child_file.parent)))
        assert decision.apply_override is True

    def test_ancestor_template_workspace_dir_beats_its_adjacent_config(self, tmp_path: Path) -> None:
        """On one ancestor, the workspace_dir template field wins over its adjacent config.

        Mirrors branch-0-beats-branch-3 precedence for the active project: an ancestor resolves its
        own workspace the same way whether it is active or inherited-from.
        """
        parent_file = tmp_path / "parent" / "griptape-nodes-project.yml"
        child_file = tmp_path / "parent" / "child" / "griptape-nodes-project.yml"
        for f in (parent_file, child_file):
            f.parent.mkdir(parents=True)
            f.touch()

        pm = self._pm_with_chain(
            [
                {
                    "id": "P",
                    "file": parent_file,
                    "parent_id": None,
                    "workspace_dir": "./from-template",
                    "config": {"workspace_directory": "/from/adjacent"},
                },
                {"id": "C", "file": child_file, "parent_id": "P", "config": {}},
            ],
            configured_root="/global/ws",
        )
        decision = pm.decide_workspace(child_file, project_config={}, env_config={})

        assert decision.workspace_dir == Path(str(canonicalize_for_identity(parent_file.parent / "from-template")))
        assert decision.apply_override is True

    def test_skips_to_grandparent_template_workspace_dir(self, tmp_path: Path) -> None:
        """C inherits A's workspace_dir field when B (its parent) declares neither field nor config."""
        a_file = tmp_path / "a" / "griptape-nodes-project.yml"
        b_file = tmp_path / "a" / "b" / "griptape-nodes-project.yml"
        c_file = tmp_path / "a" / "b" / "c" / "griptape-nodes-project.yml"
        for f in (a_file, b_file, c_file):
            f.parent.mkdir(parents=True)
            f.touch()

        pm = self._pm_with_chain(
            [
                {"id": "A", "file": a_file, "parent_id": None, "workspace_dir": "./", "config": {}},
                {"id": "B", "file": b_file, "parent_id": "A", "config": {}},
                {"id": "C", "file": c_file, "parent_id": "B", "config": {}},
            ],
            configured_root="/global/ws",
        )
        decision = pm.decide_workspace(c_file, project_config={}, env_config={})

        assert decision.workspace_dir == Path(str(canonicalize_for_identity(a_file.parent)))
        assert decision.apply_override is True

    def test_chain_exhausted_uses_global_default(self, tmp_path: Path) -> None:
        """A project derived from the file-less default inherits the global workspace (Jason's case)."""
        c_file = tmp_path / "test" / "griptape-nodes-project.yml"
        c_file.parent.mkdir(parents=True)
        c_file.touch()

        pm = self._pm_with_chain(
            [
                {"id": "<system-defaults>", "file": None, "parent_id": None},
                {"id": "C", "file": c_file, "parent_id": "<system-defaults>", "config": {}},
            ],
            configured_root="/global/ws",
        )
        decision = pm.decide_workspace(c_file, project_config={}, env_config={})

        assert decision.workspace_dir == Path("/global/ws")
        assert decision.apply_override is True

    def test_ancestor_override_mapping_wins(self, tmp_path: Path) -> None:
        """An ancestor keyed in project_workspaces is inherited over its adjacent config."""
        a_file = tmp_path / "a" / "griptape-nodes-project.yml"
        c_file = tmp_path / "c" / "griptape-nodes-project.yml"
        for f in (a_file, c_file):
            f.parent.mkdir(parents=True)
            f.touch()

        pm = self._pm_with_chain(
            [
                {"id": "A", "file": a_file, "parent_id": None, "config": {"workspace_directory": "/ws/a"}},
                {"id": "C", "file": c_file, "parent_id": "A", "config": {}},
            ],
            project_workspaces={str(a_file): "/mapped/a"},
            configured_root="/global/ws",
        )
        decision = pm.decide_workspace(c_file, project_config={}, env_config={})

        assert decision.workspace_dir == Path("/mapped/a")
        assert decision.apply_override is True

    def test_start_project_sidecar_still_wins(self, tmp_path: Path) -> None:
        """C's own project-adjacent workspace_directory (branch 3) wins and the walk never runs."""
        a_file = tmp_path / "a" / "griptape-nodes-project.yml"
        c_file = tmp_path / "c" / "griptape-nodes-project.yml"
        for f in (a_file, c_file):
            f.parent.mkdir(parents=True)
            f.touch()

        pm = self._pm_with_chain(
            [
                {"id": "A", "file": a_file, "parent_id": None, "config": {"workspace_directory": "/ws/a"}},
                {"id": "C", "file": c_file, "parent_id": "A", "config": {}},
            ],
            configured_root="/global/ws",
        )
        decision = pm.decide_workspace(
            c_file,
            project_config={"workspace_directory": "/explicit/ws"},
            env_config={},
        )

        assert decision.workspace_dir == Path("/explicit/ws")
        assert decision.apply_override is False

    def test_imported_standalone_uses_global_default(self, tmp_path: Path) -> None:
        """A parentless project outside the configured root adopts the global workspace, not its own dir."""
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        project_file = other_dir / "griptape-nodes-project.yml"
        project_file.touch()

        pm = self._pm_with_chain(
            [{"id": "C", "file": project_file, "parent_id": None, "config": {}}],
            configured_root="/global/ws",
        )
        decision = pm.decide_workspace(project_file, project_config={}, env_config={})

        assert decision.workspace_dir == Path("/global/ws")
        assert decision.apply_override is True

    def test_cyclic_chain_falls_back_to_global_default(self, tmp_path: Path) -> None:
        """A cyclic parent chain terminates via the visited set and falls back to the global default."""
        a_file = tmp_path / "a" / "griptape-nodes-project.yml"
        b_file = tmp_path / "b" / "griptape-nodes-project.yml"
        for f in (a_file, b_file):
            f.parent.mkdir(parents=True)
            f.touch()

        pm = self._pm_with_chain(
            [
                {"id": "A", "file": a_file, "parent_id": "B", "config": {}},
                {"id": "B", "file": b_file, "parent_id": "A", "config": {}},
            ],
            configured_root="/global/ws",
        )
        decision = pm.decide_workspace(a_file, project_config={}, env_config={})

        assert decision.workspace_dir == Path("/global/ws")
        assert decision.apply_override is True

    def test_global_default_unset_falls_back_to_own_dir(self, tmp_path: Path) -> None:
        """Chain exhausted AND workspace_directory unset in both layers falls back to the project's own dir."""
        c_file = tmp_path / "c" / "griptape-nodes-project.yml"
        c_file.parent.mkdir(parents=True)
        c_file.touch()

        pm = self._pm_with_chain(
            [{"id": "C", "file": c_file, "parent_id": None, "config": {}}],
            configured_root=None,
            default_root=None,
        )
        decision = pm.decide_workspace(c_file, project_config={}, env_config={})

        assert decision.workspace_dir == c_file.parent
        assert decision.apply_override is True

    def test_template_workspace_dir_beats_map_and_env(self, tmp_path: Path) -> None:
        """The template's workspace_dir (branch 0) wins over the project_workspaces map AND env."""
        project_file = tmp_path / "project.yml"
        project_file.touch()
        template_ws = tmp_path / "from-template"
        mapped_ws = tmp_path / "from-map"

        pm = self._pm_with_project_workspaces({str(project_file): str(mapped_ws)})
        decision = pm.decide_workspace(
            project_file,
            project_config={"workspace_directory": "/ignored/project"},
            env_config={"workspace_directory": "/ignored/env"},
            template_workspace_dir=str(template_ws),
        )

        assert decision.workspace_dir == Path(str(template_ws))
        assert decision.apply_override is True

    def test_template_workspace_dir_beats_env_alone(self, tmp_path: Path) -> None:
        project_file = tmp_path / "project.yml"
        project_file.touch()
        template_ws = tmp_path / "from-template"

        pm = self._pm_with_project_workspaces({})
        decision = pm.decide_workspace(
            project_file,
            project_config={},
            env_config={"workspace_directory": "/ignored/env"},
            template_workspace_dir=str(template_ws),
        )

        assert decision.workspace_dir == Path(str(template_ws))
        assert decision.apply_override is True


class TestResolveTemplateWorkspaceDir:
    """`_resolve_template_workspace_dir` reduces a raw workspace_dir field to an absolute path.

    It mirrors how parent_project_path is resolved: a per-platform mapping is reduced to the
    active platform's value, a relative path resolves against the project YAML's directory, and
    the result is canonicalized. The raw stored value is never mutated; this only produces the
    resolve-time absolute path passed into the decision ladder as branch 0.
    """

    @staticmethod
    def _pm() -> ProjectManager:
        return ProjectManager(Mock(), Mock(), Mock())

    def test_none_returns_none(self, tmp_path: Path) -> None:
        pm = self._pm()
        assert pm._resolve_template_workspace_dir(None, tmp_path / "project.yml") is None

    def test_absolute_string_is_canonicalized(self, tmp_path: Path) -> None:
        pm = self._pm()
        abs_ws = tmp_path / "workspace"

        result = pm._resolve_template_workspace_dir(str(abs_ws), tmp_path / "project.yml")

        assert result == str(canonicalize_for_identity(abs_ws))

    def test_relative_string_resolves_against_yaml_dir(self, tmp_path: Path) -> None:
        """A relative workspace_dir resolves against the project YAML's own directory."""
        project_file = tmp_path / "proj" / "griptape-nodes-project.yml"
        project_file.parent.mkdir(parents=True)

        result = self._pm()._resolve_template_workspace_dir("./workspace", project_file)

        assert result == str(canonicalize_for_identity(project_file.parent / "workspace"))

    def test_parent_relative_string_resolves_against_yaml_dir(self, tmp_path: Path) -> None:
        project_file = tmp_path / "proj" / "griptape-nodes-project.yml"
        project_file.parent.mkdir(parents=True)

        result = self._pm()._resolve_template_workspace_dir("../shared-ws", project_file)

        assert result == str(canonicalize_for_identity(project_file.parent / "../shared-ws"))

    def test_per_platform_selects_active_os(self, tmp_path: Path) -> None:
        from griptape_nodes.common.project_templates.project_path import PerPlatformProjectPath

        abs_ws = tmp_path / "active"
        # Set only the current platform's key; default unset so a wrong-key select would be None.
        if sys.platform.startswith("win"):
            per_platform = PerPlatformProjectPath(windows=str(abs_ws))
        elif sys.platform.startswith("darwin"):
            per_platform = PerPlatformProjectPath(darwin=str(abs_ws))
        else:
            per_platform = PerPlatformProjectPath(linux=str(abs_ws))

        result = self._pm()._resolve_template_workspace_dir(per_platform, tmp_path / "project.yml")

        assert result == str(canonicalize_for_identity(abs_ws))

    def test_per_platform_falls_back_to_default(self, tmp_path: Path) -> None:
        from griptape_nodes.common.project_templates.project_path import PerPlatformProjectPath

        abs_ws = tmp_path / "default-ws"
        # Only `default` set: select() returns it when no active-platform key matches.
        per_platform = PerPlatformProjectPath(default=str(abs_ws))

        result = self._pm()._resolve_template_workspace_dir(per_platform, tmp_path / "project.yml")

        assert result == str(canonicalize_for_identity(abs_ws))


class TestDecideLibrariesRoot:
    """`decide_libraries_root` decides where a project's libraries install/resolve.

    Branch 0 is the project's own libraries_dir; branch 1 inherits the nearest ancestor's
    libraries_dir resolved against THAT ancestor's dir (so children point at the parent's
    libraries/ tree); None means fall back to the workspace-relative default. It consults ONLY
    the template libraries_dir field, never project_workspaces or adjacent config. Reuses the
    TestDecideWorkspace._pm_with_chain helper, which seeds each spec's template libraries_dir.
    """

    def test_own_libraries_dir_wins(self, tmp_path: Path) -> None:
        """A project's own libraries_dir (branch 0) is used verbatim (already resolved)."""
        c_file = tmp_path / "c" / "griptape-nodes-project.yml"
        c_file.parent.mkdir(parents=True)
        c_file.touch()
        own = tmp_path / "own-libs"

        pm = TestDecideWorkspace._pm_with_chain(
            [{"id": "C", "file": c_file, "parent_id": None, "libraries_dir": "./own-libs"}],
        )
        result = pm.decide_libraries_root(c_file, template_libraries_dir=str(own))

        assert result == Path(str(own))

    def test_inherits_parent_libraries_dir_resolved_against_parent(self, tmp_path: Path) -> None:
        """A child with no libraries_dir adopts the parent's, resolved against the PARENT's dir."""
        parent_file = tmp_path / "parent" / "griptape-nodes-project.yml"
        child_file = tmp_path / "child" / "griptape-nodes-project.yml"
        for f in (parent_file, child_file):
            f.parent.mkdir(parents=True)
            f.touch()

        pm = TestDecideWorkspace._pm_with_chain(
            [
                {"id": "P", "file": parent_file, "parent_id": None, "libraries_dir": "./libraries"},
                {"id": "C", "file": child_file, "parent_id": "P"},
            ],
        )
        result = pm.decide_libraries_root(child_file, template_libraries_dir=None)

        # Resolved against the PARENT's dir, not the child's -- this is what makes sharing work.
        assert result == Path(str(canonicalize_for_identity(parent_file.parent / "libraries")))

    def test_inherits_grandparent_when_parent_has_none(self, tmp_path: Path) -> None:
        a_file = tmp_path / "a" / "griptape-nodes-project.yml"
        b_file = tmp_path / "b" / "griptape-nodes-project.yml"
        c_file = tmp_path / "c" / "griptape-nodes-project.yml"
        for f in (a_file, b_file, c_file):
            f.parent.mkdir(parents=True)
            f.touch()

        pm = TestDecideWorkspace._pm_with_chain(
            [
                {"id": "A", "file": a_file, "parent_id": None, "libraries_dir": "./libraries"},
                {"id": "B", "file": b_file, "parent_id": "A"},
                {"id": "C", "file": c_file, "parent_id": "B"},
            ],
        )
        result = pm.decide_libraries_root(c_file, template_libraries_dir=None)

        assert result == Path(str(canonicalize_for_identity(a_file.parent / "libraries")))

    def test_none_when_no_libraries_dir_in_chain(self, tmp_path: Path) -> None:
        """No libraries_dir anywhere -> None, so the caller falls back to the workspace-relative default."""
        parent_file = tmp_path / "parent" / "griptape-nodes-project.yml"
        child_file = tmp_path / "child" / "griptape-nodes-project.yml"
        for f in (parent_file, child_file):
            f.parent.mkdir(parents=True)
            f.touch()

        pm = TestDecideWorkspace._pm_with_chain(
            [
                {"id": "P", "file": parent_file, "parent_id": None},
                {"id": "C", "file": child_file, "parent_id": "P"},
            ],
        )
        assert pm.decide_libraries_root(child_file, template_libraries_dir=None) is None

    def test_cyclic_chain_returns_none(self, tmp_path: Path) -> None:
        a_file = tmp_path / "a" / "griptape-nodes-project.yml"
        b_file = tmp_path / "b" / "griptape-nodes-project.yml"
        for f in (a_file, b_file):
            f.parent.mkdir(parents=True)
            f.touch()

        pm = TestDecideWorkspace._pm_with_chain(
            [
                {"id": "A", "file": a_file, "parent_id": "B"},
                {"id": "B", "file": b_file, "parent_id": "A"},
            ],
        )
        assert pm.decide_libraries_root(a_file, template_libraries_dir=None) is None


class TestResolveWorkspaceDirForProjectId:
    """`resolve_workspace_dir_for_project_id` resolves an UNLOADED project's workspace dir.

    It mirrors decide_workspace (sharing the _decide_workspace_pre/post_inheritance helpers) but
    resolves the id -> path and the parent chain from disk so it works for a project absent from the
    live registry. These tests drive the real _build_unloaded_id_index and
    _inherit_workspace_from_parents_offline, mocking only the _read_overlay I/O seam.
    """

    @staticmethod
    def _resolved(path: str) -> Path:
        """Expand+resolve a path the way resolve_workspace_dir_for_project_id returns it."""
        return Path(path).expanduser().resolve()

    @staticmethod
    def _make_overlay(
        *,
        project_id: str | None,
        parent_id: str | None = None,
        parent_path: str | None = None,
        workspace_dir: "str | PerPlatformProjectPath | None" = None,
        libraries_dir: "str | PerPlatformProjectPath | None" = None,
    ) -> "ProjectOverlayData":
        """Build a minimal ProjectOverlayData carrying only the id / parent-link / workspace fields the walk reads."""
        from griptape_nodes.common.project_templates.loader import ProjectOverlayData, YAMLLineInfo

        return ProjectOverlayData(
            name="test",
            project_template_schema_version="0.3.2",
            situations={},
            directories={},
            environment={},
            file_extension_directories={},
            description=None,
            parent_project_path=parent_path,
            line_info=YAMLLineInfo(),
            id=project_id,
            parent_project_id=parent_id,
            workspace_dir=workspace_dir,
            libraries_dir=libraries_dir,
        )

    @classmethod
    def _build_pm(  # noqa: PLR0913
        cls,
        specs: list[dict[str, Any]],
        *,
        registered: list[str] | None = None,
        loaded: list[str] | None = None,
        project_workspaces: dict[str, str] | None = None,
        configured_root: str | None = None,
        default_root: str | None = None,
        env_workspace: str | None = None,
    ) -> ProjectManager:
        """Build a ProjectManager whose disk is modeled by specs and config by the keyword args.

        Each spec: `id`, `file` (Path), optional `parent_id` / `parent_path` (its parent link), and
        optional `config` (its adjacent griptape_nodes_config.json). `registered` lists the file
        paths exposed via projects_to_register (the disk scan source); `loaded` lists ids seeded into
        the live registry. `_read_overlay` is mocked to return each spec's overlay keyed by canonical
        path. ConfigManager reads (project_workspaces, global workspace_directory, env, adjacent
        configs) are served from the keyword args so all five decide_workspace branches have inputs.
        """
        from griptape_nodes.common.project_templates import ProjectValidationInfo, ProjectValidationStatus
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.files.path_utils import canonicalize_for_identity
        from griptape_nodes.retained_mode.managers.project_manager import (
            PROJECTS_TO_REGISTER_KEY,
            ProjectInfo,
        )

        project_workspaces = project_workspaces or {}
        registered = registered or []
        loaded = loaded or []
        spec_by_id = {spec["id"]: spec for spec in specs}

        mock_config = Mock()

        def fake_get(key: str, *, config_source: str = "merged_config", default: Any = None, **_: Any) -> Any:
            if key == "project_workspaces":
                return project_workspaces
            if key == PROJECTS_TO_REGISTER_KEY:
                return registered
            if key == "workspace_directory" and config_source == "user_config":
                return configured_root
            if key == "workspace_directory" and config_source == "default_config":
                return default_root
            return default

        mock_config.get_config_value.side_effect = fake_get
        mock_config.read_env_config.return_value = (
            {"workspace_directory": env_workspace} if env_workspace is not None else {}
        )

        dir_to_config: dict[Path, dict] = {
            canonicalize_for_identity(Path(spec["file"])).parent: spec.get("config", {}) for spec in specs
        }

        def fake_read_config_file(path: Path) -> dict:
            return dir_to_config.get(Path(path).parent, {})

        mock_config.read_config_file.side_effect = fake_read_config_file

        path_to_overlay = {
            canonicalize_for_identity(Path(spec["file"])): cls._make_overlay(
                project_id=spec["id"],
                parent_id=spec.get("parent_id"),
                parent_path=spec.get("parent_path"),
                workspace_dir=spec.get("workspace_dir"),
                libraries_dir=spec.get("libraries_dir"),
            )
            for spec in specs
        }
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)

        async def fake_read_overlay(
            project_file_path: Path,
            *,
            record_status: bool = True,  # noqa: ARG001  # accepted to mirror the production keyword call; unused by the stub
        ) -> "tuple[ProjectValidationInfo, ProjectOverlayData] | LoadProjectTemplateResultFailure":
            overlay = path_to_overlay.get(canonicalize_for_identity(project_file_path))
            if overlay is None:
                from griptape_nodes.retained_mode.events.project_events import LoadProjectTemplateResultFailure

                return LoadProjectTemplateResultFailure(validation=validation, result_details="not found")
            return validation, overlay

        pm = ProjectManager(Mock(), mock_config, Mock())
        pm._read_overlay = fake_read_overlay  # type: ignore[method-assign]
        pm._resolve_registered_entry_paths = lambda _entries: [  # type: ignore[method-assign]
            canonicalize_for_identity(Path(p)) for p in registered
        ]

        for loaded_id in loaded:
            spec = spec_by_id[loaded_id]
            file_path = canonicalize_for_identity(Path(spec["file"]))
            template = DEFAULT_PROJECT_TEMPLATE.model_copy(
                update={
                    "parent_project_id": spec.get("parent_id"),
                    "workspace_dir": spec.get("workspace_dir"),
                    "libraries_dir": spec.get("libraries_dir"),
                }
            )
            pm._successfully_loaded_project_templates[loaded_id] = ProjectInfo(
                project_id=loaded_id,
                project_file_path=file_path,
                project_base_dir=file_path.parent,
                template=template,
                validation=validation,
                parsed_situation_schemas={},
                parsed_directory_schemas={},
            )
        return pm

    @pytest.mark.asyncio
    async def test_unloaded_no_parent_uses_global_default(self, tmp_path: Path) -> None:
        """An unloaded, parentless project with no explicit workspace adopts the global default."""
        project_file = tmp_path / "c" / "griptape-nodes-project.yml"
        project_file.parent.mkdir(parents=True)
        project_file.touch()

        pm = self._build_pm(
            [{"id": "C", "file": project_file, "config": {}}],
            registered=[str(project_file)],
            configured_root="/global/ws",
        )
        result = await pm.resolve_workspace_dir_for_project_id("C")

        assert result == self._resolved("/global/ws")

    @pytest.mark.asyncio
    async def test_unloaded_project_workspaces_override_wins(self, tmp_path: Path) -> None:
        """A project_workspaces entry keyed on the unloaded project's file path wins (branch 1)."""
        project_file = tmp_path / "c" / "griptape-nodes-project.yml"
        project_file.parent.mkdir(parents=True)
        project_file.touch()
        mapped = tmp_path / "mapped"

        pm = self._build_pm(
            [{"id": "C", "file": project_file, "config": {}}],
            registered=[str(project_file)],
            project_workspaces={str(project_file): str(mapped)},
            configured_root="/global/ws",
        )
        result = await pm.resolve_workspace_dir_for_project_id("C")

        assert result == mapped.expanduser().resolve()

    @pytest.mark.asyncio
    async def test_unloaded_project_adjacent_workspace_wins(self, tmp_path: Path) -> None:
        """The unloaded project's own adjacent workspace_directory (branch 3) wins over the global default."""
        project_file = tmp_path / "c" / "griptape-nodes-project.yml"
        project_file.parent.mkdir(parents=True)
        project_file.touch()
        explicit = tmp_path / "explicit"

        pm = self._build_pm(
            [{"id": "C", "file": project_file, "config": {"workspace_directory": str(explicit)}}],
            registered=[str(project_file)],
            configured_root="/global/ws",
        )
        result = await pm.resolve_workspace_dir_for_project_id("C")

        assert result == explicit.expanduser().resolve()

    @pytest.mark.asyncio
    async def test_unloaded_env_workspace_wins(self, tmp_path: Path) -> None:
        """An env workspace_directory (branch 2) wins over the project-adjacent config."""
        project_file = tmp_path / "c" / "griptape-nodes-project.yml"
        project_file.parent.mkdir(parents=True)
        project_file.touch()

        pm = self._build_pm(
            [{"id": "C", "file": project_file, "config": {"workspace_directory": "/from/project"}}],
            registered=[str(project_file)],
            configured_root="/global/ws",
            env_workspace="/from/env",
        )
        result = await pm.resolve_workspace_dir_for_project_id("C")

        assert result == self._resolved("/from/env")

    @pytest.mark.asyncio
    async def test_unloaded_template_workspace_dir_wins(self, tmp_path: Path) -> None:
        """An unloaded project's own workspace_dir field (branch 0) beats its adjacent config and the map."""
        project_file = tmp_path / "c" / "griptape-nodes-project.yml"
        project_file.parent.mkdir(parents=True)
        project_file.touch()
        template_ws = tmp_path / "from-template"
        mapped = tmp_path / "from-map"

        pm = self._build_pm(
            [
                {
                    "id": "C",
                    "file": project_file,
                    "config": {"workspace_directory": "/ignored/project"},
                    "workspace_dir": str(template_ws),
                }
            ],
            registered=[str(project_file)],
            project_workspaces={str(project_file): str(mapped)},
            configured_root="/global/ws",
            env_workspace="/ignored/env",
        )
        result = await pm.resolve_workspace_dir_for_project_id("C")

        assert result == self._resolved(str(canonicalize_for_identity(template_ws)))

    @pytest.mark.asyncio
    async def test_unloaded_relative_template_workspace_dir_resolves_against_yaml(self, tmp_path: Path) -> None:
        """A relative workspace_dir on an unloaded project resolves against the project YAML's directory."""
        project_file = tmp_path / "c" / "griptape-nodes-project.yml"
        project_file.parent.mkdir(parents=True)
        project_file.touch()

        pm = self._build_pm(
            [{"id": "C", "file": project_file, "config": {}, "workspace_dir": "./workspace"}],
            registered=[str(project_file)],
            configured_root="/global/ws",
        )
        result = await pm.resolve_workspace_dir_for_project_id("C")

        assert result == self._resolved(str(canonicalize_for_identity(project_file.parent / "workspace")))

    @pytest.mark.asyncio
    async def test_unloaded_child_inherits_legacy_path_parent_workspace(self, tmp_path: Path) -> None:
        """An UNLOADED child with a legacy parent_project_path inherits the parent's workspace from disk."""
        parent_file = tmp_path / "a" / "griptape-nodes-project.yml"
        child_file = tmp_path / "c" / "griptape-nodes-project.yml"
        for f in (parent_file, child_file):
            f.parent.mkdir(parents=True)
            f.touch()

        pm = self._build_pm(
            [
                {"id": "A", "file": parent_file, "config": {"workspace_directory": "/ws/a"}},
                {"id": "C", "file": child_file, "parent_path": str(parent_file), "config": {}},
            ],
            registered=[str(parent_file), str(child_file)],
            configured_root="/global/ws",
        )
        result = await pm.resolve_workspace_dir_for_project_id("C")

        assert result == self._resolved("/ws/a")

    @pytest.mark.asyncio
    async def test_unloaded_child_inherits_id_parent_via_registered_scan(self, tmp_path: Path) -> None:
        """An unloaded child with a parent_project_id resolves the parent through the projects_to_register scan."""
        parent_file = tmp_path / "a" / "griptape-nodes-project.yml"
        child_file = tmp_path / "c" / "griptape-nodes-project.yml"
        for f in (parent_file, child_file):
            f.parent.mkdir(parents=True)
            f.touch()

        pm = self._build_pm(
            [
                {"id": "A", "file": parent_file, "config": {"workspace_directory": "/ws/a"}},
                {"id": "C", "file": child_file, "parent_id": "A", "config": {}},
            ],
            registered=[str(parent_file), str(child_file)],
            configured_root="/global/ws",
        )
        result = await pm.resolve_workspace_dir_for_project_id("C")

        assert result == self._resolved("/ws/a")

    @pytest.mark.asyncio
    async def test_unresolvable_id_returns_none(self, tmp_path: Path) -> None:
        """An id present in neither the registry nor projects_to_register, and not a file path, returns None."""
        project_file = tmp_path / "c" / "griptape-nodes-project.yml"
        project_file.parent.mkdir(parents=True)
        project_file.touch()

        pm = self._build_pm(
            [{"id": "C", "file": project_file, "config": {}}],
            registered=[str(project_file)],
            configured_root="/global/ws",
        )
        result = await pm.resolve_workspace_dir_for_project_id("does-not-exist")

        assert result is None

    @pytest.mark.asyncio
    async def test_matches_decide_workspace_for_loaded_project(self, tmp_path: Path) -> None:
        """Parity: for a LOADED project the offline resolver equals decide_workspace's workspace_dir."""
        parent_file = tmp_path / "a" / "griptape-nodes-project.yml"
        child_file = tmp_path / "c" / "griptape-nodes-project.yml"
        for f in (parent_file, child_file):
            f.parent.mkdir(parents=True)
            f.touch()

        pm = self._build_pm(
            [
                {"id": "A", "file": parent_file, "config": {"workspace_directory": "/ws/a"}},
                {"id": "C", "file": child_file, "parent_id": "A", "config": {}},
            ],
            registered=[str(parent_file), str(child_file)],
            loaded=["A", "C"],
            configured_root="/global/ws",
        )

        from griptape_nodes.files.path_utils import canonicalize_for_identity

        child_canonical = canonicalize_for_identity(child_file)
        live_decision = pm.decide_workspace(child_canonical, project_config={}, env_config={})
        offline_result = await pm.resolve_workspace_dir_for_project_id("C")

        assert offline_result == self._resolved(str(live_decision.workspace_dir))
        assert offline_result == self._resolved("/ws/a")

    @pytest.mark.asyncio
    async def test_unloaded_child_inherits_parent_template_workspace_dir(self, tmp_path: Path) -> None:
        """Offline: an unloaded child inherits the parent's workspace_dir TEMPLATE FIELD from disk.

        Offline analogue of the live regression: the parent declares only workspace_dir "./" (no
        adjacent config, no project_workspaces), and the child must inherit it resolved against the
        PARENT's dir, not fall through to the global workspace.
        """
        parent_file = tmp_path / "parent" / "griptape-nodes-project.yml"
        child_file = tmp_path / "parent" / "child" / "griptape-nodes-project.yml"
        for f in (parent_file, child_file):
            f.parent.mkdir(parents=True)
            f.touch()

        pm = self._build_pm(
            [
                {"id": "P", "file": parent_file, "workspace_dir": "./", "config": {}},
                {"id": "C", "file": child_file, "parent_id": "P", "config": {}},
            ],
            registered=[str(parent_file), str(child_file)],
            configured_root="/global/ws",
        )
        result = await pm.resolve_workspace_dir_for_project_id("C")

        assert result == self._resolved(str(canonicalize_for_identity(parent_file.parent)))

    @pytest.mark.asyncio
    async def test_matches_decide_workspace_for_template_field_inheritance(self, tmp_path: Path) -> None:
        """Parity: live and offline agree when the child inherits the parent's workspace_dir field."""
        parent_file = tmp_path / "parent" / "griptape-nodes-project.yml"
        child_file = tmp_path / "parent" / "child" / "griptape-nodes-project.yml"
        for f in (parent_file, child_file):
            f.parent.mkdir(parents=True)
            f.touch()

        pm = self._build_pm(
            [
                {"id": "P", "file": parent_file, "workspace_dir": "./", "config": {}},
                {"id": "C", "file": child_file, "parent_id": "P", "config": {}},
            ],
            registered=[str(parent_file), str(child_file)],
            loaded=["P", "C"],
            configured_root="/global/ws",
        )

        child_canonical = canonicalize_for_identity(child_file)
        live_decision = pm.decide_workspace(child_canonical, project_config={}, env_config={})
        offline_result = await pm.resolve_workspace_dir_for_project_id("C")

        assert offline_result == self._resolved(str(live_decision.workspace_dir))
        assert offline_result == self._resolved(str(canonicalize_for_identity(parent_file.parent)))

    @pytest.mark.asyncio
    async def test_unloaded_skips_to_grandparent_workspace(self, tmp_path: Path) -> None:
        """Offline multi-hop: C inherits A's workspace when B (its parent) declares none but A does."""
        a_file = tmp_path / "a" / "griptape-nodes-project.yml"
        b_file = tmp_path / "b" / "griptape-nodes-project.yml"
        c_file = tmp_path / "c" / "griptape-nodes-project.yml"
        for f in (a_file, b_file, c_file):
            f.parent.mkdir(parents=True)
            f.touch()

        pm = self._build_pm(
            [
                {"id": "A", "file": a_file, "config": {"workspace_directory": "/ws/a"}},
                {"id": "B", "file": b_file, "parent_id": "A", "config": {}},
                {"id": "C", "file": c_file, "parent_id": "B", "config": {}},
            ],
            registered=[str(a_file), str(b_file), str(c_file)],
            configured_root="/global/ws",
        )
        result = await pm.resolve_workspace_dir_for_project_id("C")

        assert result == self._resolved("/ws/a")

    @pytest.mark.asyncio
    async def test_unloaded_cyclic_chain_falls_back_to_global_default(self, tmp_path: Path) -> None:
        """Offline: a cyclic parent chain terminates via the visited set and uses the global default."""
        a_file = tmp_path / "a" / "griptape-nodes-project.yml"
        b_file = tmp_path / "b" / "griptape-nodes-project.yml"
        for f in (a_file, b_file):
            f.parent.mkdir(parents=True)
            f.touch()

        pm = self._build_pm(
            [
                {"id": "A", "file": a_file, "parent_id": "B", "config": {}},
                {"id": "B", "file": b_file, "parent_id": "A", "config": {}},
            ],
            registered=[str(a_file), str(b_file)],
            configured_root="/global/ws",
        )
        result = await pm.resolve_workspace_dir_for_project_id("A")

        assert result == self._resolved("/global/ws")

    @pytest.mark.asyncio
    async def test_unloaded_unreadable_ancestor_fails_closed_to_global_default(self, tmp_path: Path) -> None:
        """Offline: an unreadable ancestor YAML mid-chain stops the walk (fail-closed), using the default.

        C -> B (readable, no workspace) -> A (file exists but overlay unreadable). The walk reads B,
        finds no workspace, follows the legacy link to A, and A's overlay read fails -> the walk
        returns None and the resolver falls back to the global default. An unreadable project YAML is
        not a valid chain link; this pins the accepted fail-closed behavior of the shared walker.
        """
        a_file = tmp_path / "a" / "griptape-nodes-project.yml"
        b_file = tmp_path / "b" / "griptape-nodes-project.yml"
        c_file = tmp_path / "c" / "griptape-nodes-project.yml"
        for f in (a_file, b_file, c_file):
            f.parent.mkdir(parents=True)
            f.touch()

        # A is present on disk (so the legacy link resolves) but absent from specs, so its overlay
        # read returns a failure -- modeling an unreadable/corrupt project YAML.
        pm = self._build_pm(
            [
                {"id": "B", "file": b_file, "parent_path": str(a_file), "config": {}},
                {"id": "C", "file": c_file, "parent_id": "B", "config": {}},
            ],
            registered=[str(b_file), str(c_file)],
            configured_root="/global/ws",
        )
        result = await pm.resolve_workspace_dir_for_project_id("C")

        assert result == self._resolved("/global/ws")

    @pytest.mark.asyncio
    async def test_unloaded_unreadable_ancestor_with_config_workspace_fails_closed(self, tmp_path: Path) -> None:
        """Offline: an unreadable ancestor is skipped even when it declares a workspace via config.

        C -> B (readable, no workspace) -> A (file exists but overlay unreadable, yet A carries a
        project_workspaces override). The shared walker requires A's overlay to be readable to probe
        it, so A is dropped and the resolver falls back to the global default. This differs from the
        pre-dedupe offline workspace walk, which probed A's config workspace (an override read never
        touches the overlay) before requiring A's own overlay to load, and so would have inherited
        A's override. Fail-closed here matches the live walk and the offline libraries walk, which
        already treat an unloadable ancestor as a broken chain link. Pins the accepted behavior.
        """
        a_file = tmp_path / "a" / "griptape-nodes-project.yml"
        b_file = tmp_path / "b" / "griptape-nodes-project.yml"
        c_file = tmp_path / "c" / "griptape-nodes-project.yml"
        for f in (a_file, b_file, c_file):
            f.parent.mkdir(parents=True)
            f.touch()

        # A is present on disk (so the legacy link resolves) and carries a project_workspaces override,
        # but is absent from specs so its overlay read fails -- modeling an unreadable/corrupt YAML.
        pm = self._build_pm(
            [
                {"id": "B", "file": b_file, "parent_path": str(a_file), "config": {}},
                {"id": "C", "file": c_file, "parent_id": "B", "config": {}},
            ],
            registered=[str(b_file), str(c_file)],
            project_workspaces={str(a_file): "/ws/a"},
            configured_root="/global/ws",
        )
        result = await pm.resolve_workspace_dir_for_project_id("C")

        assert result == self._resolved("/global/ws")

    @pytest.mark.asyncio
    async def test_read_overlay_record_status_false_does_not_record_failures(self, tmp_path: Path) -> None:
        """A read-only probe (record_status=False) must not inject phantom failed-load entries.

        _read_overlay's failure branches record into _registered_template_status, which
        ListProjectTemplatesRequest surfaces as failed_to_load. The offline workspace resolver
        probes files it may not be able to read, so it must not pollute that map.
        """
        from unittest.mock import patch

        from griptape_nodes.retained_mode.events.os_events import FileIOFailureReason, ReadFileResultFailure

        missing_file = tmp_path / "gone" / "griptape-nodes-project.yml"

        pm = ProjectManager(Mock(), Mock(), Mock())

        read_failure = ReadFileResultFailure(
            failure_reason=FileIOFailureReason.FILE_NOT_FOUND, result_details="not found"
        )
        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.ahandle_request = AsyncMock(return_value=read_failure)

            probe = await pm._read_overlay(missing_file, record_status=False)
            assert missing_file not in pm._registered_template_status

            recorded = await pm._read_overlay(missing_file)
            assert missing_file in pm._registered_template_status

        from griptape_nodes.retained_mode.events.project_events import LoadProjectTemplateResultFailure

        assert isinstance(probe, LoadProjectTemplateResultFailure)
        assert isinstance(recorded, LoadProjectTemplateResultFailure)


class TestResolveLibrariesRootForProjectId(TestResolveWorkspaceDirForProjectId):
    """`resolve_libraries_root_for_project_id` resolves an UNLOADED project's libraries root.

    Offline analogue of decide_libraries_root, used by the provisioning preview. Unlike the workspace
    resolver it consults ONLY the project-template libraries_dir field (branch 0) and the nearest
    ancestor's libraries_dir walked from disk (branch 1); there is no project_workspaces / env /
    adjacent-config input. Returns None when no libraries_dir is declared anywhere in the chain, so the
    caller falls back to the workspace-relative libraries directory. Reuses the disk/config modeling
    from TestResolveWorkspaceDirForProjectId (the specs now also carry an optional `libraries_dir`),
    driving the real _build_unloaded_id_index and _inherit_libraries_dir_from_parents_offline.
    """

    @staticmethod
    def _canonical(path: Path) -> Path:
        """Canonicalize the way _resolve_template_libraries_dir returns its result."""
        return canonicalize_for_identity(path)

    @pytest.mark.asyncio
    async def test_unloaded_no_libraries_dir_returns_none(self, tmp_path: Path) -> None:
        """A parentless project with no libraries_dir returns None (caller uses the workspace default)."""
        project_file = tmp_path / "c" / "griptape-nodes-project.yml"
        project_file.parent.mkdir(parents=True)
        project_file.touch()

        pm = self._build_pm(
            [{"id": "C", "file": project_file, "config": {}}],
            registered=[str(project_file)],
        )
        result = await pm.resolve_libraries_root_for_project_id("C")

        assert result is None

    @pytest.mark.asyncio
    async def test_unloaded_own_libraries_dir_wins(self, tmp_path: Path) -> None:
        """A project's own libraries_dir field (branch 0) is returned, canonicalized."""
        project_file = tmp_path / "c" / "griptape-nodes-project.yml"
        project_file.parent.mkdir(parents=True)
        project_file.touch()
        own_libs = tmp_path / "my-libs"

        pm = self._build_pm(
            [{"id": "C", "file": project_file, "config": {}, "libraries_dir": str(own_libs)}],
            registered=[str(project_file)],
        )
        result = await pm.resolve_libraries_root_for_project_id("C")

        assert result == self._canonical(own_libs)

    @pytest.mark.asyncio
    async def test_unloaded_relative_libraries_dir_resolves_against_yaml(self, tmp_path: Path) -> None:
        """A relative libraries_dir resolves against the project YAML's directory, not the cwd."""
        project_file = tmp_path / "c" / "griptape-nodes-project.yml"
        project_file.parent.mkdir(parents=True)
        project_file.touch()

        pm = self._build_pm(
            [{"id": "C", "file": project_file, "config": {}, "libraries_dir": "./libraries"}],
            registered=[str(project_file)],
        )
        result = await pm.resolve_libraries_root_for_project_id("C")

        assert result == self._canonical(project_file.parent / "libraries")

    @pytest.mark.asyncio
    async def test_unloaded_child_inherits_id_parent_libraries_dir(self, tmp_path: Path) -> None:
        """A child with no own libraries_dir inherits the parent's (branch 1), resolved against the PARENT dir.

        This is the library-sharing case: the child declares a workspace_dir of its own but points at
        the parent's libraries/ tree, so a library declared on the parent is reused rather than
        re-downloaded per child.
        """
        parent_file = tmp_path / "a" / "griptape-nodes-project.yml"
        child_file = tmp_path / "c" / "griptape-nodes-project.yml"
        for f in (parent_file, child_file):
            f.parent.mkdir(parents=True)
            f.touch()

        pm = self._build_pm(
            [
                {"id": "A", "file": parent_file, "config": {}, "libraries_dir": "./shared-libs"},
                {"id": "C", "file": child_file, "parent_id": "A", "config": {}, "workspace_dir": "./ws"},
            ],
            registered=[str(parent_file), str(child_file)],
        )
        result = await pm.resolve_libraries_root_for_project_id("C")

        assert result == self._canonical(parent_file.parent / "shared-libs")

    @pytest.mark.asyncio
    async def test_unloaded_own_libraries_dir_beats_inherited(self, tmp_path: Path) -> None:
        """A child's own libraries_dir (branch 0) wins over an inherited one (branch 1)."""
        parent_file = tmp_path / "a" / "griptape-nodes-project.yml"
        child_file = tmp_path / "c" / "griptape-nodes-project.yml"
        for f in (parent_file, child_file):
            f.parent.mkdir(parents=True)
            f.touch()

        pm = self._build_pm(
            [
                {"id": "A", "file": parent_file, "config": {}, "libraries_dir": "./parent-libs"},
                {"id": "C", "file": child_file, "parent_id": "A", "config": {}, "libraries_dir": "./child-libs"},
            ],
            registered=[str(parent_file), str(child_file)],
        )
        result = await pm.resolve_libraries_root_for_project_id("C")

        assert result == self._canonical(child_file.parent / "child-libs")

    @pytest.mark.asyncio
    async def test_unresolvable_id_returns_none(self, tmp_path: Path) -> None:
        """An id present nowhere, and not a file path, returns None."""
        project_file = tmp_path / "c" / "griptape-nodes-project.yml"
        project_file.parent.mkdir(parents=True)
        project_file.touch()

        pm = self._build_pm(
            [{"id": "C", "file": project_file, "config": {}, "libraries_dir": "./libs"}],
            registered=[str(project_file)],
        )
        result = await pm.resolve_libraries_root_for_project_id("does-not-exist")

        assert result is None

    @pytest.mark.asyncio
    async def test_matches_decide_libraries_root_for_loaded_project(self, tmp_path: Path) -> None:
        """Parity: for a LOADED child the offline resolver equals decide_libraries_root's value."""
        parent_file = tmp_path / "a" / "griptape-nodes-project.yml"
        child_file = tmp_path / "c" / "griptape-nodes-project.yml"
        for f in (parent_file, child_file):
            f.parent.mkdir(parents=True)
            f.touch()

        pm = self._build_pm(
            [
                {"id": "A", "file": parent_file, "config": {}, "libraries_dir": "./shared-libs"},
                {"id": "C", "file": child_file, "parent_id": "A", "config": {}},
            ],
            registered=[str(parent_file), str(child_file)],
            loaded=["A", "C"],
        )

        child_canonical = canonicalize_for_identity(child_file)
        # The child declares no own libraries_dir, so branch 0 passes None and the live walk (branch 1)
        # resolves the parent's value; the offline resolver must reach the same path.
        live_decision = pm.decide_libraries_root(child_canonical, template_libraries_dir=None)
        offline_result = await pm.resolve_libraries_root_for_project_id("C")

        assert live_decision is not None
        assert offline_result == live_decision
        assert offline_result == self._canonical(parent_file.parent / "shared-libs")

    @pytest.mark.asyncio
    async def test_offline_walk_reads_each_node_overlay_once(self, tmp_path: Path) -> None:
        """The shared offline walker reads each chain node's overlay exactly once.

        Regression guard for the fixed double-read: the previous libraries walk read each parent
        overlay twice per hop (once to probe libraries_dir, once next iteration for its parent link).
        The shared single-read walker must read each node once. This measures the walker in isolation
        (id-index built first, before the counter is installed) so it does not conflate the walk with
        _build_unloaded_id_index's disk scan or the caller's branch-0 own-overlay read. Uses a 3-level
        chain where the top ancestor supplies the libraries_dir, so the walk visits every node.
        """
        grand_file = tmp_path / "g" / "griptape-nodes-project.yml"
        parent_file = tmp_path / "a" / "griptape-nodes-project.yml"
        child_file = tmp_path / "c" / "griptape-nodes-project.yml"
        for f in (grand_file, parent_file, child_file):
            f.parent.mkdir(parents=True)
            f.touch()

        pm = self._build_pm(
            [
                {"id": "G", "file": grand_file, "config": {}, "libraries_dir": "./shared-libs"},
                {"id": "A", "file": parent_file, "parent_id": "G", "config": {}},
                {"id": "C", "file": child_file, "parent_id": "A", "config": {}},
            ],
            registered=[str(grand_file), str(parent_file), str(child_file)],
        )

        # Build the id-index up front so its disk scan is not counted; the counter measures only the
        # walker's own reads.
        id_index = await pm._build_unloaded_id_index()
        child_canonical = canonicalize_for_identity(child_file)

        read_counts: dict[Path, int] = {}
        inner_read_overlay = pm._read_overlay

        async def counting_read_overlay(
            project_file_path: Path,
            *,
            record_status: bool = True,
        ) -> "tuple[ProjectValidationInfo, ProjectOverlayData] | LoadProjectTemplateResultFailure":
            key = canonicalize_for_identity(project_file_path)
            read_counts[key] = read_counts.get(key, 0) + 1
            return await inner_read_overlay(project_file_path, record_status=record_status)

        pm._read_overlay = counting_read_overlay  # type: ignore[method-assign]

        def probe(node_path: Path, overlay: "ProjectOverlayData") -> str | None:
            return pm._resolve_template_libraries_dir(overlay.libraries_dir, node_path)

        result = await pm._nearest_ancestor_value_offline(child_canonical, id_index, probe)

        assert result == str(self._canonical(grand_file.parent / "shared-libs"))
        # The walk visits C (start), A, G -- each overlay read exactly once, none twice.
        assert read_counts, "expected the walk to read at least one overlay"
        assert max(read_counts.values()) == 1, f"a node overlay was read more than once: {read_counts}"

    @pytest.mark.asyncio
    async def test_unloaded_cyclic_chain_returns_none_libraries(self, tmp_path: Path) -> None:
        """Offline libraries: a cyclic parent chain terminates via the visited set and returns None."""
        a_file = tmp_path / "a" / "griptape-nodes-project.yml"
        b_file = tmp_path / "b" / "griptape-nodes-project.yml"
        for f in (a_file, b_file):
            f.parent.mkdir(parents=True)
            f.touch()

        # Neither declares a libraries_dir, and they point at each other: the walk must not loop.
        pm = self._build_pm(
            [
                {"id": "A", "file": a_file, "parent_id": "B", "config": {}},
                {"id": "B", "file": b_file, "parent_id": "A", "config": {}},
            ],
            registered=[str(a_file), str(b_file)],
        )
        result = await pm.resolve_libraries_root_for_project_id("A")

        assert result is None

    @pytest.mark.asyncio
    async def test_unloaded_unreadable_ancestor_returns_none_libraries(self, tmp_path: Path) -> None:
        """Offline libraries: an unreadable ancestor YAML mid-chain stops the walk (fail-closed -> None).

        C -> B (readable, no libraries_dir) -> A (file exists but overlay unreadable). Since no
        readable node in the chain declares a libraries_dir, the resolver returns None and the caller
        falls back to the workspace-relative libraries default.
        """
        a_file = tmp_path / "a" / "griptape-nodes-project.yml"
        b_file = tmp_path / "b" / "griptape-nodes-project.yml"
        c_file = tmp_path / "c" / "griptape-nodes-project.yml"
        for f in (a_file, b_file, c_file):
            f.parent.mkdir(parents=True)
            f.touch()

        # A exists on disk (legacy link resolves) but is absent from specs -> its overlay read fails.
        pm = self._build_pm(
            [
                {"id": "B", "file": b_file, "parent_path": str(a_file), "config": {}},
                {"id": "C", "file": c_file, "parent_id": "B", "config": {}},
            ],
            registered=[str(b_file), str(c_file)],
        )
        result = await pm.resolve_libraries_root_for_project_id("C")

        assert result is None

    @pytest.mark.asyncio
    async def test_unloaded_readable_ancestor_before_unreadable_still_found_libraries(self, tmp_path: Path) -> None:
        """Offline libraries: a nearer readable ancestor's libraries_dir wins before an unreadable one is reached.

        C -> B (readable, declares libraries_dir) -> A (unreadable). The walk finds B's value and
        never needs to read A, so fail-closed does not mask a legitimately inherited value.
        """
        a_file = tmp_path / "a" / "griptape-nodes-project.yml"
        b_file = tmp_path / "b" / "griptape-nodes-project.yml"
        c_file = tmp_path / "c" / "griptape-nodes-project.yml"
        for f in (a_file, b_file, c_file):
            f.parent.mkdir(parents=True)
            f.touch()

        pm = self._build_pm(
            [
                {"id": "B", "file": b_file, "parent_path": str(a_file), "config": {}, "libraries_dir": "./b-libs"},
                {"id": "C", "file": c_file, "parent_id": "B", "config": {}},
            ],
            registered=[str(b_file), str(c_file)],
        )
        result = await pm.resolve_libraries_root_for_project_id("C")

        assert result == self._canonical(b_file.parent / "b-libs")


class TestOnResolveProjectWorkspaceRequest(TestResolveWorkspaceDirForProjectId):
    """on_resolve_project_workspace_request wraps resolve_workspace_dir_for_project_id as an event.

    Reuses the disk/config modeling from TestResolveWorkspaceDirForProjectId so the handler is tested
    against the real resolver, not a stub.
    """

    @pytest.mark.asyncio
    async def test_resolves_fallback_workspace_for_undeclared_project(self, tmp_path: Path) -> None:
        """A project with no declared workspace_dir resolves to the fallback ladder value (success)."""
        from griptape_nodes.retained_mode.events.project_events import (
            ResolveProjectWorkspaceRequest,
            ResolveProjectWorkspaceResultSuccess,
        )

        project_file = tmp_path / "c" / "griptape-nodes-project.yml"
        project_file.parent.mkdir(parents=True)
        project_file.touch()

        pm = self._build_pm(
            [{"id": "C", "file": project_file, "config": {}}],
            registered=[str(project_file)],
            configured_root="/global/ws",
        )
        result = await pm.on_resolve_project_workspace_request(ResolveProjectWorkspaceRequest(project_id="C"))

        assert isinstance(result, ResolveProjectWorkspaceResultSuccess)
        assert result.workspace_dir == str(self._resolved("/global/ws"))

    @pytest.mark.asyncio
    async def test_declared_workspace_dir_flows_through_handler(self, tmp_path: Path) -> None:
        """A declared workspace_dir (branch 0) is the resolved value the handler returns."""
        from griptape_nodes.retained_mode.events.project_events import (
            ResolveProjectWorkspaceRequest,
            ResolveProjectWorkspaceResultSuccess,
        )

        project_file = tmp_path / "c" / "griptape-nodes-project.yml"
        project_file.parent.mkdir(parents=True)
        project_file.touch()
        declared = tmp_path / "declared-ws"

        pm = self._build_pm(
            [{"id": "C", "file": project_file, "workspace_dir": str(declared), "config": {}}],
            registered=[str(project_file)],
            configured_root="/global/ws",
        )
        result = await pm.on_resolve_project_workspace_request(ResolveProjectWorkspaceRequest(project_id="C"))

        assert isinstance(result, ResolveProjectWorkspaceResultSuccess)
        assert result.workspace_dir == str(declared.expanduser().resolve())

    @pytest.mark.asyncio
    async def test_unresolvable_id_returns_success_with_none(self, tmp_path: Path) -> None:
        """An id that maps to no readable file is a success carrying workspace_dir=None (no hint)."""
        from griptape_nodes.retained_mode.events.project_events import (
            ResolveProjectWorkspaceRequest,
            ResolveProjectWorkspaceResultSuccess,
        )

        project_file = tmp_path / "c" / "griptape-nodes-project.yml"
        project_file.parent.mkdir(parents=True)
        project_file.touch()

        pm = self._build_pm(
            [{"id": "C", "file": project_file, "config": {}}],
            registered=[str(project_file)],
            configured_root="/global/ws",
        )
        result = await pm.on_resolve_project_workspace_request(
            ResolveProjectWorkspaceRequest(project_id="does-not-exist")
        )

        assert isinstance(result, ResolveProjectWorkspaceResultSuccess)
        assert result.workspace_dir is None


class TestProjectManagerProjectWorkspaces:
    """Test ProjectManager project_workspaces lookup in on_set_current_project_request."""

    @staticmethod
    def _simulate_library_config_change_on_project_load(mock_config: Mock) -> None:
        """Make activating the project change its merged library config.

        Library reload is now gated on `library_config_changed`: loading the
        project-adjacent config layer is what changes the merged
        `libraries_to_register`, so tests that want the reload to fire must model
        that. `get_config_value` returns a base library list that grows once
        `load_project_config` runs, and passes other keys through with their
        default.
        """
        from griptape_nodes.retained_mode.managers.settings import (
            LIBRARIES_TO_DOWNLOAD_KEY,
            LIBRARIES_TO_REGISTER_KEY,
            REQUIRES_ENGINE_KEY,
        )

        state = {"libraries": ["base-lib"]}

        def get_config_value(key: str, *_args: object, default: object = None, **_kwargs: object) -> object:
            if key == LIBRARIES_TO_REGISTER_KEY:
                return list(state["libraries"])
            if key == LIBRARIES_TO_DOWNLOAD_KEY:
                return []
            if key == REQUIRES_ENGINE_KEY:
                return None
            if key == "workspace_directory":
                return None
            return default if default is not None else {}

        def load_project_config(_project_dir: object) -> None:
            state["libraries"] = ["base-lib", "project-lib"]

        mock_config.get_config_value.side_effect = get_config_value
        mock_config.load_project_config.side_effect = load_project_config

    @staticmethod
    def _config_for_workspace_lookup(
        mock_config: Mock, project_workspaces: dict[str, str], workspace_path: Path
    ) -> None:
        """Configure a mock config for activation tests that exercise workspace lookup.

        Returns `project_workspaces` for that key and each call's `default`
        otherwise, so `_snapshot_library_config` reads a string `libraries_directory`
        (not the project_workspaces dict) and resolves it against a real
        `workspace_path`.
        """

        def get_config_value(key: str, *_args: object, default: object = None, **_kwargs: object) -> object:
            if key == "project_workspaces":
                return project_workspaces
            return default

        mock_config.get_config_value.side_effect = get_config_value
        mock_config.workspace_path = workspace_path

    def _make_project_manager_with_project(self, project_file_path: Path, mock_config: Mock) -> ProjectManager:
        """Create a ProjectManager with a loaded project template at the given path."""
        from griptape_nodes.common.project_templates import ProjectValidationInfo, ProjectValidationStatus
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.retained_mode.managers.project_manager import ProjectInfo

        mock_event_manager = Mock()
        mock_secrets = Mock()
        pm = ProjectManager(mock_event_manager, mock_config, mock_secrets)

        project_id = str(project_file_path)
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        situation_schemas = pm._parse_situation_macros(DEFAULT_PROJECT_TEMPLATE.situations, validation)
        directory_schemas = pm._parse_directory_macros(DEFAULT_PROJECT_TEMPLATE.directories, validation)

        project_info = ProjectInfo(
            project_id=project_id,
            project_file_path=project_file_path,
            project_base_dir=project_file_path.parent,
            template=DEFAULT_PROJECT_TEMPLATE,
            validation=validation,
            parsed_situation_schemas=situation_schemas,
            parsed_directory_schemas=directory_schemas,
        )
        pm._successfully_loaded_project_templates[project_id] = project_info
        return pm

    @pytest.mark.asyncio
    async def test_project_workspaces_overrides_workspace(self, tmp_path: Path) -> None:
        """Test that a matching project_workspaces entry calls set_workspace_override with the mapped value."""
        import tempfile

        project_file = tmp_path / "project.yml"
        project_file.touch()
        workspace_dir = Path(tempfile.mkdtemp())

        mock_config = Mock()
        mock_config.project_config = {}
        mock_config.env_config = {}
        mock_config.merged_config = {}
        self._config_for_workspace_lookup(mock_config, {str(project_file.resolve()): str(workspace_dir)}, tmp_path)

        pm = self._make_project_manager_with_project(project_file, mock_config)

        from griptape_nodes.retained_mode.events.project_events import SetCurrentProjectRequest

        await pm.on_set_current_project_request(SetCurrentProjectRequest(project_id=str(project_file)))

        # Activation first clears all per-activation layers, then applies the mapped value.
        mock_config.clear_project_layers.assert_called_once()
        mock_config.set_workspace_override.assert_called_once_with(Path(str(workspace_dir)))
        mock_config.load_workspace_config.assert_called_once()

    @pytest.mark.asyncio
    async def test_project_workspaces_key_resolved_before_lookup(self, tmp_path: Path) -> None:
        """Test that project_workspaces keys are resolved before matching, so symlinks and relative paths work."""
        import tempfile

        project_file = tmp_path / "project.yml"
        project_file.touch()
        workspace_dir = Path(tempfile.mkdtemp())

        mock_config = Mock()
        mock_config.project_config = {}
        mock_config.env_config = {}
        mock_config.merged_config = {}
        # Key uses the unresolved path; the code must resolve both sides before comparing.
        self._config_for_workspace_lookup(mock_config, {str(project_file): str(workspace_dir)}, tmp_path)

        pm = self._make_project_manager_with_project(project_file, mock_config)

        from griptape_nodes.retained_mode.events.project_events import SetCurrentProjectRequest

        await pm.on_set_current_project_request(SetCurrentProjectRequest(project_id=str(project_file)))

        # Activation first clears all per-activation layers, then applies the mapped value.
        mock_config.clear_project_layers.assert_called_once()
        mock_config.set_workspace_override.assert_called_once_with(Path(str(workspace_dir)))
        mock_config.load_workspace_config.assert_called_once()

    @pytest.mark.asyncio
    async def test_project_workspaces_no_match_falls_back_to_project_dir(self, tmp_path: Path) -> None:
        """Test that when no project_workspaces entry matches, set_workspace_override is called with project dir."""
        project_file = tmp_path / "project.yml"
        project_file.touch()

        mock_config = Mock()
        mock_config.project_config = {}
        mock_config.env_config = {}
        mock_config.merged_config = {}
        self._config_for_workspace_lookup(mock_config, {}, tmp_path)

        pm = self._make_project_manager_with_project(project_file, mock_config)

        from griptape_nodes.retained_mode.events.project_events import SetCurrentProjectRequest

        await pm.on_set_current_project_request(SetCurrentProjectRequest(project_id=str(project_file)))

        # Activation first clears all per-activation layers, then defaults to the project dir.
        mock_config.clear_project_layers.assert_called_once()
        mock_config.set_workspace_override.assert_called_once_with(project_file.parent)
        mock_config.load_workspace_config.assert_called_once()

    @pytest.mark.asyncio
    async def test_project_workspaces_project_adjacent_config_not_overridden_when_set(self, tmp_path: Path) -> None:
        """Test that no override path is applied when project-adjacent config sets workspace_directory.

        Activation still clears all per-activation layers first (via clear_project_layers),
        but because the project-adjacent config supplies workspace_directory, no override
        path is layered on top of it.
        """
        project_file = tmp_path / "project.yml"
        project_file.touch()

        mock_config = Mock()
        mock_config.project_config = {"workspace_directory": "/some/shared/workspace"}
        mock_config.env_config = {}
        mock_config.merged_config = {}
        self._config_for_workspace_lookup(mock_config, {}, tmp_path)

        pm = self._make_project_manager_with_project(project_file, mock_config)

        from griptape_nodes.retained_mode.events.project_events import SetCurrentProjectRequest

        await pm.on_set_current_project_request(SetCurrentProjectRequest(project_id=str(project_file)))

        mock_config.clear_project_layers.assert_called_once()
        mock_config.set_workspace_override.assert_not_called()
        mock_config.load_workspace_config.assert_called_once()

    @pytest.mark.asyncio
    async def test_activation_clears_stale_workspace_override_from_previous_project(self, tmp_path: Path) -> None:
        """Activating a project clears the override a prior activation set via auto-default.

        Regression: a project with no workspace_directory in its config auto-defaults
        the override to its own directory. Switching (or rolling back) to a project
        whose config DOES supply workspace_directory must not inherit that override,
        otherwise the second project loads the first project's workspace config layer.
        Models the lib-fail -> rollback-to-pinned-project sequence.
        """
        from griptape_nodes.common.project_templates import ProjectValidationInfo, ProjectValidationStatus
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.retained_mode.events.project_events import SetCurrentProjectRequest
        from griptape_nodes.retained_mode.managers.project_manager import ProjectInfo

        auto_default_file = tmp_path / "auto_default" / "project.yml"
        auto_default_file.parent.mkdir()
        auto_default_file.touch()
        pinned_workspace_file = tmp_path / "pinned" / "project.yml"
        pinned_workspace_file.parent.mkdir()
        pinned_workspace_file.touch()

        mock_config = Mock()
        # First activation: no workspace_directory in project config -> auto-defaults.
        mock_config.project_config = {}
        mock_config.env_config = {}
        mock_config.merged_config = {}
        self._config_for_workspace_lookup(mock_config, {}, tmp_path)

        pm = self._make_project_manager_with_project(auto_default_file, mock_config)
        # Register the second project (which supplies its own workspace_directory).
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        pinned_id = str(pinned_workspace_file)
        pm._successfully_loaded_project_templates[pinned_id] = ProjectInfo(
            project_id=pinned_id,
            project_file_path=pinned_workspace_file,
            project_base_dir=pinned_workspace_file.parent,
            template=DEFAULT_PROJECT_TEMPLATE,
            validation=validation,
            parsed_situation_schemas=pm._parse_situation_macros(DEFAULT_PROJECT_TEMPLATE.situations, validation),
            parsed_directory_schemas=pm._parse_directory_macros(DEFAULT_PROJECT_TEMPLATE.directories, validation),
        )

        # Activate the auto-default project: clears all layers, then sets the override to its own dir.
        await pm.on_set_current_project_request(SetCurrentProjectRequest(project_id=str(auto_default_file)))
        mock_config.clear_project_layers.assert_called_once()
        mock_config.set_workspace_override.assert_called_once_with(auto_default_file.parent)

        # Now the second project supplies its own workspace_directory.
        mock_config.project_config = {"workspace_directory": str(pinned_workspace_file.parent)}
        mock_config.clear_project_layers.reset_mock()
        mock_config.set_workspace_override.reset_mock()

        await pm.on_set_current_project_request(SetCurrentProjectRequest(project_id=str(pinned_workspace_file)))

        # The stale auto-default override is dropped by clear_project_layers and no path
        # override is re-applied, so the pinned project's own workspace config layer loads.
        mock_config.clear_project_layers.assert_called_once()
        mock_config.set_workspace_override.assert_not_called()

    @pytest.mark.asyncio
    async def test_activation_sets_libraries_root_override_from_inherited_parent(self, tmp_path: Path) -> None:
        """Activating a child pins the libraries root to the parent's dir (the sharing seam).

        End-to-end wiring test for _activate_project's libraries block: activation must call
        decide_libraries_root (which walks the parent chain) and push the result into
        set_libraries_root_override, so resolved_libraries_root() then returns the shared tree. The
        child declares no libraries_dir of its own; the parent declares "./libraries", so the child
        inherits the PARENT's dir. Guards against the override being dropped, ordered wrongly relative
        to clear_project_layers, or fed the wrong value -- none of which the isolated
        decide_libraries_root tests would catch.
        """
        from griptape_nodes.common.project_templates import ProjectValidationInfo, ProjectValidationStatus
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.retained_mode.events.project_events import SetCurrentProjectRequest
        from griptape_nodes.retained_mode.managers.project_manager import ProjectInfo

        parent_file = tmp_path / "parent" / "project.yml"
        parent_file.parent.mkdir()
        parent_file.touch()
        child_file = tmp_path / "parent" / "child" / "project.yml"
        child_file.parent.mkdir()
        child_file.touch()

        mock_config = Mock()
        mock_config.project_config = {}
        mock_config.env_config = {}
        mock_config.merged_config = {}
        self._config_for_workspace_lookup(mock_config, {}, tmp_path)
        # No adjacent config on any chain node: the parent-workspace walk reads each ancestor's
        # griptape_nodes_config.json, so it must return a real dict (not a Mock) for the child's
        # branch-4 workspace inheritance. The child inherits the parent's workspace here (neither
        # declares workspace_dir), which is orthogonal to the libraries_dir seam under test.
        mock_config.read_config_file.return_value = {}

        # Parent declares libraries_dir "./libraries"; child inherits (declares none).
        parent_template = DEFAULT_PROJECT_TEMPLATE.model_copy(update={"id": "P", "libraries_dir": "./libraries"})
        child_template = DEFAULT_PROJECT_TEMPLATE.model_copy(update={"id": "C", "parent_project_id": "P"})
        pm = self._make_project_manager_with_project(child_file, mock_config)
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        # Re-key the child's registry entry to its opaque id (parent link resolves by id), and add parent.
        del pm._successfully_loaded_project_templates[str(child_file)]
        for pid, f, tmpl in [("P", parent_file, parent_template), ("C", child_file, child_template)]:
            pm._successfully_loaded_project_templates[pid] = ProjectInfo(
                project_id=pid,
                project_file_path=f,
                project_base_dir=f.parent,
                template=tmpl,
                validation=validation,
                parsed_situation_schemas=pm._parse_situation_macros(tmpl.situations, validation),
                parsed_directory_schemas=pm._parse_directory_macros(tmpl.directories, validation),
            )

        await pm.on_set_current_project_request(SetCurrentProjectRequest(project_id="C"))

        # The override is set to the PARENT's libraries dir, resolved against the parent -- not the
        # child's own dir, and not None (which would fall back to the workspace-relative default).
        mock_config.set_libraries_root_override.assert_called_once_with(
            Path(str(canonicalize_for_identity(parent_file.parent / "libraries")))
        )

    @pytest.mark.asyncio
    async def test_activation_clears_libraries_root_override_when_none_in_chain(self, tmp_path: Path) -> None:
        """Activating a project with no libraries_dir anywhere pins the override to None (fallback).

        The other half of the seam: when decide_libraries_root returns None (no own or inherited
        libraries_dir), activation must still call set_libraries_root_override(None) so a stale
        override from a previously-active sharing project is dropped and resolved_libraries_root()
        falls back to the workspace-relative default.
        """
        from griptape_nodes.retained_mode.events.project_events import SetCurrentProjectRequest

        project_file = tmp_path / "solo" / "project.yml"
        project_file.parent.mkdir()
        project_file.touch()

        mock_config = Mock()
        mock_config.project_config = {}
        mock_config.env_config = {}
        mock_config.merged_config = {}
        self._config_for_workspace_lookup(mock_config, {}, tmp_path)

        # _make_project_manager_with_project registers a bare DEFAULT_PROJECT_TEMPLATE (no libraries_dir).
        pm = self._make_project_manager_with_project(project_file, mock_config)

        await pm.on_set_current_project_request(SetCurrentProjectRequest(project_id=str(project_file)))

        mock_config.set_libraries_root_override.assert_called_once_with(None)

    @pytest.mark.asyncio
    async def test_switch_to_system_defaults_drops_prior_project_library_pins(self, tmp_path: Path) -> None:
        """Switching from a pinned project to system defaults unloads the project's library pins.

        Regression for the config-layer leak: the project-adjacent config layer (which
        carries the pinned `libraries_to_register`) must be dropped on the switch, so the
        merged library config returns to defaults and the reload fires to unload the pins.
        Modeled by tying the pinned library to the project layer: clear_project_layers()
        drops it, load_project_config() adds it. If the unconditional clear regresses, the
        snapshot would still carry the pin, library_config_changed would be False, and the
        reload that unloads it would never fire (the original bug).
        """
        from unittest.mock import AsyncMock, patch

        from griptape_nodes.common.project_templates import ProjectValidationInfo, ProjectValidationStatus
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.retained_mode.events.library_events import ReloadAllLibrariesResultSuccess
        from griptape_nodes.retained_mode.events.project_events import SetCurrentProjectRequest
        from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY, ProjectInfo
        from griptape_nodes.retained_mode.managers.settings import (
            LIBRARIES_TO_DOWNLOAD_KEY,
            LIBRARIES_TO_REGISTER_KEY,
            REQUIRES_ENGINE_KEY,
        )

        pinned_file = tmp_path / "pinned" / "project.yml"
        pinned_file.parent.mkdir()
        pinned_file.touch()

        # The pinned library is present only while the project layer is active. clear_project_layers
        # drops it; load_project_config (re)adds it. Models the project-adjacent config layer.
        state = {"project_layer_active": False}

        def get_config_value(key: str, *_args: object, default: object = None, **_kwargs: object) -> object:
            if key == LIBRARIES_TO_REGISTER_KEY:
                return ["base-lib", "pinned-lib"] if state["project_layer_active"] else ["base-lib"]
            if key == LIBRARIES_TO_DOWNLOAD_KEY:
                return []
            if key == REQUIRES_ENGINE_KEY:
                return None
            if key == "workspace_directory":
                return None
            return default if default is not None else {}

        mock_config = Mock()
        mock_config.project_config = {}
        mock_config.env_config = {}
        mock_config.merged_config = {}
        mock_config.workspace_path = str(tmp_path)
        mock_config.get_config_value.side_effect = get_config_value
        mock_config.load_project_config.side_effect = lambda _dir: state.update(project_layer_active=True)
        mock_config.clear_project_layers.side_effect = lambda: state.update(project_layer_active=False)

        pm = self._make_project_manager_with_project(pinned_file, mock_config)
        pm._initialization_complete = True

        # Register system defaults (no project file) as a switch target.
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        pm._successfully_loaded_project_templates[SYSTEM_DEFAULTS_KEY] = ProjectInfo(
            project_id=SYSTEM_DEFAULTS_KEY,
            project_file_path=None,
            project_base_dir=tmp_path,
            template=DEFAULT_PROJECT_TEMPLATE,
            validation=validation,
            parsed_situation_schemas=pm._parse_situation_macros(DEFAULT_PROJECT_TEMPLATE.situations, validation),
            parsed_directory_schemas=pm._parse_directory_macros(DEFAULT_PROJECT_TEMPLATE.directories, validation),
        )

        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = AsyncMock(return_value=ReloadAllLibrariesResultSuccess(result_details="ok"))
            mock_gn.WorkflowManager.return_value = Mock()

            # Activate the pinned project: the project layer adds pinned-lib, firing a reload.
            await pm.on_set_current_project_request(SetCurrentProjectRequest(project_id=str(pinned_file)))
            assert state["project_layer_active"] is True

            mock_gn.ahandle_request.reset_mock()

            # Switch to system defaults: clear_project_layers drops the pinned-lib layer.
            await pm.on_set_current_project_request(SetCurrentProjectRequest(project_id=SYSTEM_DEFAULTS_KEY))

        # The prior project's library layer is gone (back to defaults only)...
        assert state["project_layer_active"] is False
        assert mock_config.get_config_value(LIBRARIES_TO_REGISTER_KEY) == ["base-lib"]
        # ...and the reload fired on the switch to actually unload the pinned library.
        mock_gn.ahandle_request.assert_called_once()

    @pytest.mark.asyncio
    async def test_unknown_project_id_remerges_after_clearing_layers(self, tmp_path: Path) -> None:
        """An unknown project id remerges config instead of leaving layers cleared.

        Activation unconditionally calls clear_project_layers() up front. For a
        known project (load_project_config) or system defaults (load_configs) a
        remerge follows; an id with no loaded template must still remerge via
        load_configs(), otherwise config is left in the cleared, unmerged state.
        """
        from unittest.mock import AsyncMock, patch

        from griptape_nodes.retained_mode.events.project_events import SetCurrentProjectRequest

        # No project file created and no template registered: the id is unknown.
        unknown_file = tmp_path / "unknown.yml"

        mock_config = Mock()
        mock_config.project_config = {}
        mock_config.env_config = {}
        mock_config.merged_config = {}
        self._config_for_workspace_lookup(mock_config, {}, tmp_path)

        mock_event_manager = Mock()
        pm = ProjectManager(mock_event_manager, mock_config, Mock())
        pm._initialization_complete = True

        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = AsyncMock()
            await pm.on_set_current_project_request(SetCurrentProjectRequest(project_id=str(unknown_file)))

        mock_config.clear_project_layers.assert_called_once()
        mock_config.load_configs.assert_called_once()
        mock_config.load_project_config.assert_not_called()

    @pytest.mark.asyncio
    async def test_initialization_incomplete_skips_reload(self, tmp_path: Path) -> None:
        """When _initialization_complete is False, no library reload or workflow re-registration occurs."""
        from unittest.mock import AsyncMock, patch

        project_file = tmp_path / "project.yml"
        project_file.touch()

        mock_config = Mock()
        mock_config.project_config = {}
        mock_config.env_config = {}
        mock_config.merged_config = {}
        self._config_for_workspace_lookup(mock_config, {}, tmp_path)

        pm = self._make_project_manager_with_project(project_file, mock_config)
        # _initialization_complete starts False

        from griptape_nodes.retained_mode.events.project_events import SetCurrentProjectRequest

        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = AsyncMock()
            await pm.on_set_current_project_request(SetCurrentProjectRequest(project_id=str(project_file)))
            mock_gn.ahandle_request.assert_not_called()
            mock_gn.WorkflowManager.assert_not_called()

    @pytest.mark.asyncio
    async def test_initialization_complete_same_workspace_reloads_libraries_only(self, tmp_path: Path) -> None:
        """When workspace is unchanged but library config changed, libraries reload without workflow re-registration."""
        from unittest.mock import AsyncMock, patch

        from griptape_nodes.retained_mode.events.library_events import (
            ReloadAllLibrariesResultSuccess,
        )

        project_file = tmp_path / "project.yml"
        project_file.touch()

        mock_config = Mock()
        mock_config.project_config = {}
        mock_config.env_config = {}
        mock_config.merged_config = {}
        self._simulate_library_config_change_on_project_load(mock_config)
        # Same workspace before and after
        mock_config.workspace_path = str(tmp_path)

        pm = self._make_project_manager_with_project(project_file, mock_config)
        pm._initialization_complete = True

        from griptape_nodes.retained_mode.events.project_events import SetCurrentProjectRequest

        mock_workflow_manager = Mock()
        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = AsyncMock(return_value=ReloadAllLibrariesResultSuccess(result_details="ok"))
            mock_gn.WorkflowManager.return_value = mock_workflow_manager

            result = await pm.on_set_current_project_request(SetCurrentProjectRequest(project_id=str(project_file)))

        mock_gn.ahandle_request.assert_called_once()
        mock_workflow_manager.refresh_workflow_registry.assert_not_called()
        assert not result.altered_workflow_state

    @pytest.mark.asyncio
    async def test_initialization_complete_different_workspace_reloads_and_re_registers(self, tmp_path: Path) -> None:
        """When workspace changes, both library reload and workflow re-registration occur."""
        import tempfile
        from unittest.mock import AsyncMock, patch

        from griptape_nodes.retained_mode.events.library_events import (
            ReloadAllLibrariesResultSuccess,
        )

        project_file = tmp_path / "project.yml"
        project_file.touch()
        new_workspace = Path(tempfile.mkdtemp())

        mock_config = Mock()
        mock_config.project_config = {}
        mock_config.env_config = {}
        mock_config.merged_config = {}
        self._simulate_library_config_change_on_project_load(mock_config)

        pm = self._make_project_manager_with_project(project_file, mock_config)
        pm._initialization_complete = True

        # workspace_path returns different values before and after config changes
        old_ws = str(tmp_path / "old_workspace")
        new_ws = str(new_workspace)
        mock_config.workspace_path = old_ws

        from griptape_nodes.retained_mode.events.project_events import SetCurrentProjectRequest

        mock_workflow_manager = Mock()
        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = AsyncMock(return_value=ReloadAllLibrariesResultSuccess(result_details="ok"))
            mock_gn.WorkflowManager.return_value = mock_workflow_manager

            # Simulate workspace changing after config is applied
            def side_effect_set_workspace_override(_: object) -> None:
                mock_config.workspace_path = new_ws

            mock_config.set_workspace_override.side_effect = side_effect_set_workspace_override
            mock_workflow_manager.refresh_workflow_registry = AsyncMock(return_value=None)

            result = await pm.on_set_current_project_request(SetCurrentProjectRequest(project_id=str(project_file)))

        mock_gn.ahandle_request.assert_called_once()
        mock_workflow_manager.refresh_workflow_registry.assert_called_once()
        assert result.altered_workflow_state

    @pytest.mark.asyncio
    async def test_library_reload_failure_returns_failure(self, tmp_path: Path) -> None:
        """When library reload fails, SetCurrentProjectResultFailure is returned."""
        from unittest.mock import AsyncMock, patch

        from griptape_nodes.retained_mode.events.library_events import (
            ReloadAllLibrariesResultFailure,
        )

        project_file = tmp_path / "project.yml"
        project_file.touch()

        mock_config = Mock()
        mock_config.project_config = {}
        mock_config.env_config = {}
        mock_config.merged_config = {}
        self._simulate_library_config_change_on_project_load(mock_config)
        mock_config.workspace_path = str(tmp_path)

        pm = self._make_project_manager_with_project(project_file, mock_config)
        pm._initialization_complete = True

        from griptape_nodes.retained_mode.events.project_events import (
            SetCurrentProjectRequest,
            SetCurrentProjectResultFailure,
        )

        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = AsyncMock(
                return_value=ReloadAllLibrariesResultFailure(result_details="reload failed")
            )

            result = await pm.on_set_current_project_request(SetCurrentProjectRequest(project_id=str(project_file)))

        assert isinstance(result, SetCurrentProjectResultFailure)
        assert "reload failed" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_failed_activation_rolls_back_to_previous_project(self, tmp_path: Path) -> None:
        """A failed interactive switch re-activates the previously active project."""
        from unittest.mock import patch

        from griptape_nodes.retained_mode.events.project_events import (
            SetCurrentProjectRequest,
            SetCurrentProjectResultFailure,
        )
        from griptape_nodes.retained_mode.managers.project_manager import _ProjectActivationOutcome

        target_file = tmp_path / "target.yml"
        target_file.touch()
        previous_file = tmp_path / "previous.yml"
        previous_file.touch()

        mock_config = Mock()
        mock_config.project_config = {}
        mock_config.env_config = {}
        mock_config.merged_config = {}
        mock_config.get_config_value.return_value = {}

        pm = self._make_project_manager_with_project(target_file, mock_config)
        pm._initialization_complete = True
        # A previously active interactive project to roll back to.
        previous_id = str(canonicalize_for_identity(str(previous_file)))
        pm._current_project_id = previous_id

        failure = SetCurrentProjectResultFailure(result_details="engine_version mismatch")
        calls: list[str] = []

        async def fake_activate(project_id: str) -> _ProjectActivationOutcome:
            calls.append(project_id)
            # First call (the requested target) fails; the rollback to previous succeeds.
            if len(calls) == 1:
                return _ProjectActivationOutcome(failure=failure, workspace_changed=False)
            return _ProjectActivationOutcome(failure=None, workspace_changed=False)

        with patch.object(pm, "_activate_project", side_effect=fake_activate):
            result = await pm.on_set_current_project_request(SetCurrentProjectRequest(project_id=str(target_file)))

        target_id = str(canonicalize_for_identity(str(target_file)))
        assert calls == [target_id, previous_id]
        assert isinstance(result, SetCurrentProjectResultFailure)
        assert "engine_version mismatch" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_failed_activation_during_boot_does_not_roll_back(self, tmp_path: Path) -> None:
        """A failure before startup completes returns as-is without re-activating anything."""
        from unittest.mock import patch

        from griptape_nodes.retained_mode.events.project_events import (
            SetCurrentProjectRequest,
            SetCurrentProjectResultFailure,
        )
        from griptape_nodes.retained_mode.managers.project_manager import (
            SYSTEM_DEFAULTS_KEY,
            _ProjectActivationOutcome,
        )

        target_file = tmp_path / "target.yml"
        target_file.touch()

        mock_config = Mock()
        mock_config.project_config = {}
        mock_config.env_config = {}
        mock_config.merged_config = {}
        mock_config.get_config_value.return_value = {}

        pm = self._make_project_manager_with_project(target_file, mock_config)
        # _initialization_complete stays False (boot).
        pm._current_project_id = SYSTEM_DEFAULTS_KEY

        failure = SetCurrentProjectResultFailure(result_details="boot failure")
        calls: list[str] = []

        async def fake_activate(project_id: str) -> _ProjectActivationOutcome:
            calls.append(project_id)
            return _ProjectActivationOutcome(failure=failure, workspace_changed=False)

        with patch.object(pm, "_activate_project", side_effect=fake_activate):
            result = await pm.on_set_current_project_request(SetCurrentProjectRequest(project_id=str(target_file)))

        # Only the target activation runs; no rollback during boot.
        assert len(calls) == 1
        assert isinstance(result, SetCurrentProjectResultFailure)


class TestRegisterProjectPath:
    """Test ProjectManager._register_project_path."""

    @pytest.fixture
    def pm(self) -> ProjectManager:
        mock_event_manager = Mock()
        mock_config_manager = Mock()
        mock_config_manager.project_config = {}
        mock_config_manager.env_config = {}
        mock_config_manager.merged_config = {}
        mock_config_manager.get_config_value.return_value = []
        return ProjectManager(mock_event_manager, mock_config_manager, Mock())

    def test_register_new_path_appends_to_empty_list(self, pm: ProjectManager) -> None:
        """A new project_id is appended when the registered list is empty."""
        from griptape_nodes.retained_mode.managers.settings import PROJECTS_TO_REGISTER_KEY

        cast("Mock", pm._config_manager).get_config_value.return_value = []
        pm._register_project_path("/path/to/project.yml")
        cast("Mock", pm._config_manager).set_config_value.assert_called_once_with(
            PROJECTS_TO_REGISTER_KEY, ["/path/to/project.yml"]
        )

    def test_register_new_path_appends_to_existing_list(self, pm: ProjectManager) -> None:
        """A new project_id is appended alongside existing registered paths."""
        from griptape_nodes.retained_mode.managers.settings import PROJECTS_TO_REGISTER_KEY

        cast("Mock", pm._config_manager).get_config_value.return_value = ["/path/to/other.yml"]
        pm._register_project_path("/path/to/project.yml")
        cast("Mock", pm._config_manager).set_config_value.assert_called_once_with(
            PROJECTS_TO_REGISTER_KEY, ["/path/to/other.yml", "/path/to/project.yml"]
        )

    def test_register_already_present_does_not_modify_list(self, pm: ProjectManager) -> None:
        """If the project_id is already registered, set_config_value is not called."""
        project_id = "/path/to/project.yml"

        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_config = Mock()
            mock_config.get_config_value.return_value = [project_id]
            mock_gn.ConfigManager.return_value = mock_config

            pm._register_project_path(project_id)

        mock_config.set_config_value.assert_not_called()

    def test_register_exception_is_swallowed(self, pm: ProjectManager) -> None:
        """A config manager exception does not propagate out of _register_project_path."""
        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_config = Mock()
            mock_config.get_config_value.side_effect = RuntimeError("config failure")
            mock_gn.ConfigManager.return_value = mock_config

            # Should not raise
            pm._register_project_path("/path/to/project.yml")


class TestLoadRegisteredProjects:
    """Test ProjectManager._load_registered_projects."""

    VALID_PROJECT_YAML = """\
project_template_schema_version: "0.1.0"
name: Registered Project
situations:
  save_node_output:
    macro: "{outputs}/{file_name_base}.{file_extension}"
    policy:
      on_collision: create_new
      create_dirs: true
"""

    @pytest.fixture
    def pm(self) -> ProjectManager:
        mock_event_manager = Mock()
        mock_config_manager = Mock()
        mock_config_manager.project_config = {}
        mock_config_manager.env_config = {}
        mock_config_manager.merged_config = {}
        mock_config_manager.get_config_value.return_value = []
        return ProjectManager(mock_event_manager, mock_config_manager, Mock())

    @pytest.mark.asyncio
    async def test_empty_list_does_nothing(self, pm: ProjectManager) -> None:
        """An empty projects_to_register list results in no load attempts."""
        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_config = Mock()
            mock_config.get_config_value.return_value = []
            mock_gn.ConfigManager.return_value = mock_config

            with patch.object(pm, "on_load_project_template_request", new=AsyncMock()) as mock_load:
                await pm._load_registered_projects()
                mock_load.assert_not_called()

    @pytest.mark.asyncio
    async def test_none_config_return_does_nothing(self, pm: ProjectManager) -> None:
        """None from config (treated as empty via 'or []') results in no load attempts."""
        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_config = Mock()
            mock_config.get_config_value.return_value = None
            mock_gn.ConfigManager.return_value = mock_config

            with patch.object(pm, "on_load_project_template_request", new=AsyncMock()) as mock_load:
                await pm._load_registered_projects()
                mock_load.assert_not_called()

    @pytest.mark.asyncio
    async def test_already_loaded_path_is_skipped(self, pm: ProjectManager, tmp_path: Path) -> None:
        """Paths already in _successfully_loaded_project_templates are not loaded again."""
        from griptape_nodes.common.project_templates import ProjectValidationInfo, ProjectValidationStatus
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.retained_mode.managers.project_manager import ProjectInfo

        existing_path = str(tmp_path / "existing.yml")
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        situation_schemas = pm._parse_situation_macros(DEFAULT_PROJECT_TEMPLATE.situations, validation)
        directory_schemas = pm._parse_directory_macros(DEFAULT_PROJECT_TEMPLATE.directories, validation)
        project_info = ProjectInfo(
            project_id=existing_path,
            project_file_path=Path(existing_path),
            project_base_dir=tmp_path,
            template=DEFAULT_PROJECT_TEMPLATE,
            validation=validation,
            parsed_situation_schemas=situation_schemas,
            parsed_directory_schemas=directory_schemas,
        )
        pm._successfully_loaded_project_templates[existing_path] = project_info

        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_config = Mock()
            mock_config.get_config_value.return_value = [existing_path]
            mock_gn.ConfigManager.return_value = mock_config

            with patch.object(pm, "on_load_project_template_request", new=AsyncMock()) as mock_load:
                await pm._load_registered_projects()
                mock_load.assert_not_called()

    @pytest.mark.asyncio
    async def test_unloaded_path_is_loaded(self, pm: ProjectManager, tmp_path: Path) -> None:
        """A path not already in memory gets loaded and added to the template registry."""
        from griptape_nodes.retained_mode.events.os_events import ReadFileResultSuccess
        from griptape_nodes.retained_mode.managers.settings import PROJECTS_TO_REGISTER_KEY

        project_path = tmp_path / "project.yml"
        yaml_content = self.VALID_PROJECT_YAML

        def get_config_value_side_effect(key: str, **_: object) -> object:
            if key == PROJECTS_TO_REGISTER_KEY:
                return [str(project_path)]
            return []  # for _register_project_path's follow-on call

        cast("Mock", pm._config_manager).get_config_value.side_effect = get_config_value_side_effect

        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = AsyncMock(
                return_value=ReadFileResultSuccess(
                    content=yaml_content,
                    file_size=len(yaml_content),
                    mime_type="text/plain",
                    encoding="utf-8",
                    result_details="ok",
                )
            )

            await pm._load_registered_projects()

        assert str(project_path) in pm._successfully_loaded_project_templates

    @pytest.mark.asyncio
    async def test_directory_entry_loads_nested_projects_without_persisting(
        self, pm: ProjectManager, tmp_path: Path
    ) -> None:
        """A directory entry is recursively scanned; each project file is loaded but not persisted."""
        from griptape_nodes.retained_mode.events.os_events import ReadFileResultSuccess
        from griptape_nodes.retained_mode.managers.project_manager import WORKSPACE_PROJECT_FILE
        from griptape_nodes.retained_mode.managers.settings import PROJECTS_TO_REGISTER_KEY

        top_project = tmp_path / WORKSPACE_PROJECT_FILE
        nested_project = tmp_path / "sub" / WORKSPACE_PROJECT_FILE
        nested_project.parent.mkdir()
        top_project.write_text(self.VALID_PROJECT_YAML)
        nested_project.write_text(self.VALID_PROJECT_YAML)
        yaml_content = self.VALID_PROJECT_YAML

        def get_config_value_side_effect(key: str, **_: object) -> object:
            if key == PROJECTS_TO_REGISTER_KEY:
                return [str(tmp_path)]
            return []

        cast("Mock", pm._config_manager).get_config_value.side_effect = get_config_value_side_effect

        with (
            patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn,
            patch.object(pm, "_register_project_path") as mock_register,
        ):
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = AsyncMock(
                return_value=ReadFileResultSuccess(
                    content=yaml_content,
                    file_size=len(yaml_content),
                    mime_type="text/plain",
                    encoding="utf-8",
                    result_details="ok",
                )
            )

            await pm._load_registered_projects()

        assert str(top_project) in pm._successfully_loaded_project_templates
        assert str(nested_project) in pm._successfully_loaded_project_templates
        # Directory-discovered files are covered by the directory entry, so they
        # must not be persisted individually back into config.
        mock_register.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_directory_entry_is_logged_and_skipped(
        self, pm: ProjectManager, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A directory entry with no project files logs a warning and loads nothing."""
        from griptape_nodes.retained_mode.managers.settings import PROJECTS_TO_REGISTER_KEY

        def get_config_value_side_effect(key: str, **_: object) -> object:
            if key == PROJECTS_TO_REGISTER_KEY:
                return [str(tmp_path)]
            return []

        cast("Mock", pm._config_manager).get_config_value.side_effect = get_config_value_side_effect

        with (
            patch.object(pm, "_load_and_cache_project_template", new=AsyncMock()) as mock_load,
            caplog.at_level(logging.WARNING, logger="griptape_nodes"),
        ):
            await pm._load_registered_projects()
            mock_load.assert_not_called()

        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("contains no" in msg for msg in warning_messages)

    @pytest.mark.asyncio
    async def test_load_failure_is_logged_as_warning(
        self, pm: ProjectManager, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failed load is logged as a warning and does not raise."""
        from griptape_nodes.common.project_templates import ProjectValidationInfo, ProjectValidationStatus
        from griptape_nodes.retained_mode.events.project_events import LoadProjectTemplateResultFailure

        project_path = str(tmp_path / "missing.yml")
        failure = LoadProjectTemplateResultFailure(
            validation=ProjectValidationInfo(status=ProjectValidationStatus.MISSING),
            result_details="file not found",
        )

        cast("Mock", pm._config_manager).get_config_value.return_value = [project_path]

        with (
            patch.object(pm, "on_load_project_template_request", new=AsyncMock(return_value=failure)),
            caplog.at_level(logging.WARNING, logger="griptape_nodes"),
        ):
            await pm._load_registered_projects()

        assert project_path not in pm._successfully_loaded_project_templates
        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("Failed to load registered project" in msg for msg in warning_messages)

    @pytest.mark.asyncio
    async def test_app_initialization_complete_loads_registered_projects(
        self, pm: ProjectManager, tmp_path: Path
    ) -> None:
        """on_app_initialization_complete loads registered projects after the workspace project."""
        from griptape_nodes.retained_mode.events.app_events import AppInitializationComplete
        from griptape_nodes.retained_mode.events.os_events import ReadFileResultSuccess
        from griptape_nodes.retained_mode.managers.settings import PROJECTS_TO_REGISTER_KEY

        registered_path = tmp_path / "registered.yml"
        yaml_content = self.VALID_PROJECT_YAML

        def get_config_value_side_effect(key: str, **_: object) -> object:
            if key == "project_file":
                return None
            if key == PROJECTS_TO_REGISTER_KEY:
                return [str(registered_path)]
            if "project_workspaces" in key:
                return {}
            return str(tmp_path)

        cast("Mock", pm._config_manager).get_config_value.side_effect = get_config_value_side_effect
        cast("Mock", pm._config_manager).workspace_path = tmp_path

        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = AsyncMock(
                return_value=ReadFileResultSuccess(
                    content=yaml_content,
                    file_size=len(yaml_content),
                    mime_type="text/plain",
                    encoding="utf-8",
                    result_details="ok",
                )
            )

            await pm.on_app_initialization_complete(AppInitializationComplete())

        assert str(registered_path) in pm._successfully_loaded_project_templates


class TestValidateProjectTemplate:
    """Test ProjectManager.on_validate_project_template_request."""

    @pytest.fixture
    def pm(self) -> ProjectManager:
        mock_event_manager = Mock()
        mock_config_manager = Mock()
        mock_config_manager.project_config = {}
        mock_config_manager.env_config = {}
        mock_config_manager.merged_config = {}
        mock_config_manager.get_config_value.return_value = []
        return ProjectManager(mock_event_manager, mock_config_manager, Mock())

    @staticmethod
    def _minimal_valid_template() -> dict:
        return {
            "project_template_schema_version": "0.1.0",
            "name": "Test Project",
            "situations": {
                "save_file": {
                    "name": "save_file",
                    "macro": "{file_name_base}.{file_extension}",
                    "policy": {"on_collision": "create_new", "create_dirs": True},
                }
            },
            "directories": {
                "inputs": {"name": "inputs", "path_macro": "inputs"},
            },
        }

    def test_valid_template_returns_good_status(self, pm: ProjectManager) -> None:
        """A fully valid template validates with GOOD status and no problems."""
        from griptape_nodes.common.project_templates import ProjectValidationStatus
        from griptape_nodes.retained_mode.events.project_events import (
            ValidateProjectTemplateRequest,
            ValidateProjectTemplateResultSuccess,
        )

        request = ValidateProjectTemplateRequest(template_data=self._minimal_valid_template())
        result = pm.on_validate_project_template_request(request)

        assert isinstance(result, ValidateProjectTemplateResultSuccess)
        assert result.validation.status == ProjectValidationStatus.GOOD
        assert result.validation.problems == []

    def test_partial_policy_marks_template_unusable(self, pm: ProjectManager) -> None:
        """A situation policy missing on_collision should produce an UNUSABLE result."""
        from griptape_nodes.common.project_templates import ProjectValidationStatus
        from griptape_nodes.retained_mode.events.project_events import (
            ValidateProjectTemplateRequest,
            ValidateProjectTemplateResultSuccess,
        )

        template = self._minimal_valid_template()
        template["situations"]["save_file"]["policy"] = {"create_dirs": False}

        request = ValidateProjectTemplateRequest(template_data=template)
        result = pm.on_validate_project_template_request(request)

        assert isinstance(result, ValidateProjectTemplateResultSuccess)
        assert result.validation.status == ProjectValidationStatus.UNUSABLE
        assert any("situations.save_file.policy" in p.field_path for p in result.validation.problems)

    def test_invalid_directory_macro_marks_template_unusable(self, pm: ProjectManager) -> None:
        """A directory with an unparsable path_macro should produce a problem."""
        from griptape_nodes.common.project_templates import ProjectValidationStatus
        from griptape_nodes.retained_mode.events.project_events import (
            ValidateProjectTemplateRequest,
            ValidateProjectTemplateResultSuccess,
        )

        template = self._minimal_valid_template()
        # Unmatched brace is rejected by the macro parser
        template["directories"]["inputs"]["path_macro"] = "inputs/{unclosed"

        request = ValidateProjectTemplateRequest(template_data=template)
        result = pm.on_validate_project_template_request(request)

        assert isinstance(result, ValidateProjectTemplateResultSuccess)
        assert result.validation.status == ProjectValidationStatus.UNUSABLE
        assert any(p.field_path == "directories.inputs.path_macro" for p in result.validation.problems)

    def test_missing_name_marks_template_unusable(self, pm: ProjectManager) -> None:
        """Missing required `name` field returns UNUSABLE with a structured problem."""
        from griptape_nodes.common.project_templates import ProjectValidationStatus
        from griptape_nodes.retained_mode.events.project_events import (
            ValidateProjectTemplateRequest,
            ValidateProjectTemplateResultSuccess,
        )

        template = self._minimal_valid_template()
        del template["name"]

        request = ValidateProjectTemplateRequest(template_data=template)
        result = pm.on_validate_project_template_request(request)

        assert isinstance(result, ValidateProjectTemplateResultSuccess)
        assert result.validation.status == ProjectValidationStatus.UNUSABLE
        assert any(p.field_path == "name" for p in result.validation.problems)

    def test_pydantic_errors_surface_as_structured_problems(self, pm: ProjectManager) -> None:
        """Pydantic validation errors produce per-field problems, not a stringified exception."""
        from griptape_nodes.retained_mode.events.project_events import (
            ValidateProjectTemplateRequest,
            ValidateProjectTemplateResultSuccess,
        )

        template = self._minimal_valid_template()
        # Provide a bogus policy value to trigger a pydantic validator for the enum
        template["situations"]["save_file"]["policy"] = {
            "on_collision": "not_a_real_value",
            "create_dirs": True,
        }

        request = ValidateProjectTemplateRequest(template_data=template)
        result = pm.on_validate_project_template_request(request)

        assert isinstance(result, ValidateProjectTemplateResultSuccess)
        assert len(result.validation.problems) >= 1
        problem = result.validation.problems[0]
        assert problem.field_path.startswith("situations.save_file.policy")
        assert problem.message  # non-empty message


class TestLoadProjectTemplatePathCanonicalization:
    """Test that on_load_project_template_request canonicalizes project paths.

    Project IDs and validation-map keys must be keyed off the resolved absolute
    path so the same file loaded via different spellings (relative vs absolute,
    with or without trailing components, etc.) collapses to a single entry.
    """

    VALID_PROJECT_YAML = """\
project_template_schema_version: "0.1.0"
name: Canonicalization Test
situations:
  save_node_output:
    macro: "{outputs}/{file_name_base}.{file_extension}"
    policy:
      on_collision: create_new
      create_dirs: true
"""

    @pytest.fixture
    def pm(self) -> ProjectManager:
        mock_event_manager = Mock()
        mock_config_manager = Mock()
        mock_config_manager.project_config = {}
        mock_config_manager.env_config = {}
        mock_config_manager.merged_config = {}
        mock_config_manager.get_config_value.return_value = []
        return ProjectManager(mock_event_manager, mock_config_manager, Mock())

    @pytest.mark.asyncio
    async def test_relative_and_absolute_spellings_share_project_id(self, pm: ProjectManager, tmp_path: Path) -> None:
        """Loading the same file via a relative and an absolute path produces one entry."""
        from griptape_nodes.retained_mode.events.os_events import ReadFileResultSuccess
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )

        absolute_path = (tmp_path / "project.yml").resolve()

        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = AsyncMock(
                return_value=ReadFileResultSuccess(
                    content=self.VALID_PROJECT_YAML,
                    file_size=len(self.VALID_PROJECT_YAML),
                    mime_type="text/plain",
                    encoding="utf-8",
                    result_details="ok",
                )
            )

            cwd = Path.cwd()
            try:
                os.chdir(tmp_path)
                relative_path = Path("project.yml")
                absolute_result = await pm.on_load_project_template_request(
                    LoadProjectTemplateRequest(project_path=absolute_path)
                )
                relative_result = await pm.on_load_project_template_request(
                    LoadProjectTemplateRequest(project_path=relative_path)
                )
            finally:
                os.chdir(cwd)

        assert isinstance(absolute_result, LoadProjectTemplateResultSuccess)
        assert isinstance(relative_result, LoadProjectTemplateResultSuccess)
        assert absolute_result.project_id == relative_result.project_id
        assert absolute_result.project_id == str(absolute_path)
        assert list(pm._successfully_loaded_project_templates.keys()).count(str(absolute_path)) == 1

    @pytest.mark.asyncio
    async def test_registered_template_status_keyed_by_resolved_path(self, pm: ProjectManager, tmp_path: Path) -> None:
        """Validation status is stored under the resolved path, not the raw input."""
        from griptape_nodes.retained_mode.events.project_events import LoadProjectTemplateRequest

        absolute_path = (tmp_path / "missing.yml").resolve()

        cwd = Path.cwd()
        try:
            os.chdir(tmp_path)
            await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=Path("missing.yml")))
        finally:
            os.chdir(cwd)

        assert absolute_path in pm._registered_template_status
        assert Path("missing.yml") not in pm._registered_template_status

    @pytest.mark.asyncio
    async def test_tilde_and_absolute_spellings_share_project_id(
        self, pm: ProjectManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Loading the same file via `~/...` and its absolute path produces one entry."""
        from griptape_nodes.retained_mode.events.os_events import ReadFileResultSuccess
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )

        # Point HOME/USERPROFILE at tmp_path so "~/project.yml" expands to tmp_path / project.yml
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        absolute_path = (tmp_path / "project.yml").resolve()
        tilde_path = Path("~/project.yml")

        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = AsyncMock(
                return_value=ReadFileResultSuccess(
                    content=self.VALID_PROJECT_YAML,
                    file_size=len(self.VALID_PROJECT_YAML),
                    mime_type="text/plain",
                    encoding="utf-8",
                    result_details="ok",
                )
            )

            absolute_result = await pm.on_load_project_template_request(
                LoadProjectTemplateRequest(project_path=absolute_path)
            )
            tilde_result = await pm.on_load_project_template_request(
                LoadProjectTemplateRequest(project_path=tilde_path)
            )

        assert isinstance(absolute_result, LoadProjectTemplateResultSuccess)
        assert isinstance(tilde_result, LoadProjectTemplateResultSuccess)
        assert absolute_result.project_id == tilde_result.project_id
        assert absolute_result.project_id == str(absolute_path)
        assert list(pm._successfully_loaded_project_templates.keys()).count(str(absolute_path)) == 1


class TestRegisterProjectPathCanonicalization:
    """Test that _register_project_path dedupes across path spellings."""

    @pytest.fixture
    def pm(self) -> ProjectManager:
        mock_event_manager = Mock()
        mock_config_manager = Mock()
        mock_config_manager.project_config = {}
        mock_config_manager.env_config = {}
        mock_config_manager.merged_config = {}
        mock_config_manager.get_config_value.return_value = []
        return ProjectManager(mock_event_manager, mock_config_manager, Mock())

    def test_already_registered_under_different_spelling_is_not_reappended(
        self, pm: ProjectManager, tmp_path: Path
    ) -> None:
        """If the same file is already persisted under a different spelling, skip it."""
        absolute_path = (tmp_path / "project.yml").resolve()

        cwd = Path.cwd()
        try:
            os.chdir(tmp_path)
            relative_spelling = str(Path("project.yml").resolve())
            cast("Mock", pm._config_manager).get_config_value.return_value = [relative_spelling]
            pm._register_project_path(str(absolute_path))
        finally:
            os.chdir(cwd)

        cast("Mock", pm._config_manager).set_config_value.assert_not_called()

    def test_tilde_spelling_dedupes_against_absolute_entry(
        self, pm: ProjectManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ~-spelled persisted path is matched against an absolute incoming one."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        absolute_path = (tmp_path / "project.yml").resolve()
        cast("Mock", pm._config_manager).get_config_value.return_value = ["~/project.yml"]

        pm._register_project_path(str(absolute_path))

        cast("Mock", pm._config_manager).set_config_value.assert_not_called()


class TestLoadRegisteredProjectsCanonicalization:
    """Test that _load_registered_projects treats differently-spelled persisted paths as duplicates."""

    @pytest.fixture
    def pm(self) -> ProjectManager:
        mock_event_manager = Mock()
        mock_config_manager = Mock()
        mock_config_manager.project_config = {}
        mock_config_manager.env_config = {}
        mock_config_manager.merged_config = {}
        mock_config_manager.get_config_value.return_value = []
        return ProjectManager(mock_event_manager, mock_config_manager, Mock())

    @pytest.mark.asyncio
    async def test_persisted_unresolved_path_matches_loaded_resolved_entry(
        self, pm: ProjectManager, tmp_path: Path
    ) -> None:
        """A persisted path is matched against _successfully_loaded_project_templates after resolution."""
        from griptape_nodes.common.project_templates import ProjectValidationInfo, ProjectValidationStatus
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.retained_mode.managers.project_manager import ProjectInfo

        resolved_path = (tmp_path / "existing.yml").resolve()
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        situation_schemas = pm._parse_situation_macros(DEFAULT_PROJECT_TEMPLATE.situations, validation)
        directory_schemas = pm._parse_directory_macros(DEFAULT_PROJECT_TEMPLATE.directories, validation)
        project_info = ProjectInfo(
            project_id=str(resolved_path),
            project_file_path=resolved_path,
            project_base_dir=tmp_path,
            template=DEFAULT_PROJECT_TEMPLATE,
            validation=validation,
            parsed_situation_schemas=situation_schemas,
            parsed_directory_schemas=directory_schemas,
        )
        pm._successfully_loaded_project_templates[str(resolved_path)] = project_info

        cwd = Path.cwd()
        try:
            os.chdir(tmp_path)
            cast("Mock", pm._config_manager).get_config_value.return_value = ["existing.yml"]
            with patch.object(pm, "on_load_project_template_request", new=AsyncMock()) as mock_load:
                await pm._load_registered_projects()
        finally:
            os.chdir(cwd)

        mock_load.assert_not_called()


class TestProjectEnvironmentVariableRecursion:
    """Tests for recursive resolution of project template.environment values.

    Env values are parsed as macros and may reference builtins, directory names,
    or other env vars. Values must survive as fully-expanded strings once written
    into os.environ or into the macro resolution bag.
    """

    def _make_pm_with_template(
        self,
        environment: dict[str, str],
        directories: dict[str, str] | None = None,
        *,
        workspace_path: Path = Path("/workspace"),
        project_file_path: Path = Path("/proj/project.yml"),
    ) -> ProjectManager:
        from griptape_nodes.common.project_templates import (
            DirectoryDefinition,
            ProjectTemplate,
            ProjectValidationInfo,
            ProjectValidationStatus,
        )
        from griptape_nodes.retained_mode.managers.project_manager import ProjectInfo

        mock_config = Mock()
        mock_config.workspace_path = workspace_path
        mock_config.get_config_value.return_value = "staticfiles"
        mock_secrets = Mock()
        mock_event_manager = Mock()
        pm = ProjectManager(mock_event_manager, mock_config, mock_secrets)

        template = ProjectTemplate(
            project_template_schema_version="0.1.0",
            name="test_project",
            directories={
                name: DirectoryDefinition(name=name, path_macro=path_macro)
                for name, path_macro in (directories or {"outputs": "outputs"}).items()
            },
            situations={},
            environment=environment,
        )

        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        situation_schemas = pm._parse_situation_macros(template.situations, validation)
        directory_schemas = pm._parse_directory_macros(template.directories, validation)

        project_id = str(project_file_path)
        project_info = ProjectInfo(
            project_id=project_id,
            project_file_path=project_file_path,
            project_base_dir=project_file_path.parent,
            template=template,
            validation=validation,
            parsed_situation_schemas=situation_schemas,
            parsed_directory_schemas=directory_schemas,
        )
        pm._successfully_loaded_project_templates[project_id] = project_info
        pm._current_project_id = project_id
        return pm

    def test_env_value_resolves_literal_string(self) -> None:
        from griptape_nodes.common.macro_parser import ParsedMacro

        pm = self._make_pm_with_template(environment={"FOO": "hello"})
        result = pm.on_get_path_for_macro_request(
            GetPathForMacroRequest(parsed_macro=ParsedMacro("{outputs}/{FOO}/x.png"), variables={})
        )
        assert isinstance(result, GetPathForMacroResultSuccess)
        assert result.resolved_path == Path("outputs/hello/x.png")

    def test_env_value_references_builtin(self) -> None:
        from griptape_nodes.common.macro_parser import ParsedMacro

        pm = self._make_pm_with_template(environment={"WORK": "{workspace_dir}/sub"})
        result = pm.on_get_path_for_macro_request(
            GetPathForMacroRequest(parsed_macro=ParsedMacro("{WORK}/x.png"), variables={})
        )
        assert isinstance(result, GetPathForMacroResultSuccess)
        assert result.resolved_path == Path("/workspace/sub/x.png")

    def test_env_value_references_directory(self) -> None:
        from griptape_nodes.common.macro_parser import ParsedMacro

        pm = self._make_pm_with_template(
            environment={"OUT": "{outputs}/nested"},
            directories={"outputs": "my_outputs"},
        )
        result = pm.on_get_path_for_macro_request(
            GetPathForMacroRequest(parsed_macro=ParsedMacro("{OUT}/x.png"), variables={})
        )
        assert isinstance(result, GetPathForMacroResultSuccess)
        assert result.resolved_path == Path("my_outputs/nested/x.png")

    def test_env_value_references_another_env_var(self) -> None:
        from griptape_nodes.common.macro_parser import ParsedMacro

        pm = self._make_pm_with_template(environment={"BASE": "root", "FULL": "{BASE}/leaf"})
        result = pm.on_get_path_for_macro_request(
            GetPathForMacroRequest(parsed_macro=ParsedMacro("{outputs}/{FULL}/x.png"), variables={})
        )
        assert isinstance(result, GetPathForMacroResultSuccess)
        assert result.resolved_path == Path("outputs/root/leaf/x.png")

    def test_env_value_chain_multiple_hops(self) -> None:
        from griptape_nodes.common.macro_parser import ParsedMacro

        pm = self._make_pm_with_template(
            environment={"A": "{workspace_dir}", "B": "{A}/b", "C": "{B}/c"},
        )
        result = pm.on_get_path_for_macro_request(
            GetPathForMacroRequest(parsed_macro=ParsedMacro("{C}/x.png"), variables={})
        )
        assert isinstance(result, GetPathForMacroResultSuccess)
        assert result.resolved_path == Path("/workspace/b/c/x.png")

    def test_env_value_cycle_detected(self) -> None:
        from griptape_nodes.common.macro_parser import ParsedMacro

        pm = self._make_pm_with_template(environment={"A": "{B}", "B": "{A}"})
        result = pm.on_get_path_for_macro_request(
            GetPathForMacroRequest(parsed_macro=ParsedMacro("{outputs}/{A}/x.png"), variables={})
        )
        assert isinstance(result, GetPathForMacroResultFailure)
        assert result.failure_reason == PathResolutionFailureReason.MACRO_RESOLUTION_ERROR
        from griptape_nodes.retained_mode.events.base_events import ResultDetails

        assert isinstance(result.result_details, ResultDetails)
        assert "cycle" in str(result.result_details).lower()

    def test_env_value_references_unknown_name(self) -> None:
        from griptape_nodes.common.macro_parser import ParsedMacro

        pm = self._make_pm_with_template(environment={"FOO": "{NOT_DEFINED}"})
        result = pm.on_get_path_for_macro_request(
            GetPathForMacroRequest(parsed_macro=ParsedMacro("{outputs}/{FOO}/x.png"), variables={})
        )
        assert isinstance(result, GetPathForMacroResultFailure)
        assert result.failure_reason == PathResolutionFailureReason.MACRO_RESOLUTION_ERROR

    def test_env_value_references_workflow_builtin_without_workflow_fails(self) -> None:
        from griptape_nodes.common.macro_parser import ParsedMacro

        pm = self._make_pm_with_template(environment={"WF": "{workflow_name}_suffix"})
        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_context = Mock()
            mock_context.has_current_workflow.return_value = False
            mock_gn.ContextManager.return_value = mock_context

            result = pm.on_get_path_for_macro_request(
                GetPathForMacroRequest(parsed_macro=ParsedMacro("{outputs}/{WF}/x.png"), variables={})
            )
        assert isinstance(result, GetPathForMacroResultFailure)
        assert result.failure_reason == PathResolutionFailureReason.MACRO_RESOLUTION_ERROR

    def test_directory_optional_workflow_dir_degrades_when_no_workflow(self) -> None:
        """A directory path_macro with optional {workflow_dir?:/} degrades to workspace-relative when unsaved."""
        from griptape_nodes.common.macro_parser import ParsedMacro

        pm = self._make_pm_with_template(
            environment={},
            directories={"inputs": "{workflow_dir?:/}inputs"},
        )
        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_context = Mock()
            mock_context.has_current_workflow.return_value = False
            mock_gn.ContextManager.return_value = mock_context

            result = pm.on_get_path_for_macro_request(
                GetPathForMacroRequest(parsed_macro=ParsedMacro("{inputs}/img.png"), variables={})
            )
        assert isinstance(result, GetPathForMacroResultSuccess)
        assert result.resolved_path == Path("inputs/img.png")

    def test_directory_required_workflow_dir_still_fails_when_no_workflow(self) -> None:
        """A directory path_macro with required {workflow_dir} still fails when unsaved (regression guard)."""
        from griptape_nodes.common.macro_parser import ParsedMacro

        pm = self._make_pm_with_template(
            environment={},
            directories={"inputs": "{workflow_dir}/inputs"},
        )
        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_context = Mock()
            mock_context.has_current_workflow.return_value = False
            mock_gn.ContextManager.return_value = mock_context

            result = pm.on_get_path_for_macro_request(
                GetPathForMacroRequest(parsed_macro=ParsedMacro("{inputs}/img.png"), variables={})
            )
        assert isinstance(result, GetPathForMacroResultFailure)
        assert result.failure_reason == PathResolutionFailureReason.MACRO_RESOLUTION_ERROR

    def test_directory_optional_workflow_dir_resolves_when_workflow_saved(self) -> None:
        """A directory path_macro with optional {workflow_dir?:/} resolves under the workflow dir once saved."""
        from griptape_nodes.common.macro_parser import ParsedMacro

        pm = self._make_pm_with_template(
            environment={},
            directories={"inputs": "{workflow_dir?:/}inputs"},
        )
        with (
            patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn,
            patch("griptape_nodes.retained_mode.managers.project_manager.WorkflowRegistry") as mock_registry,
        ):
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_context = Mock()
            mock_context.has_current_workflow.return_value = True
            mock_context.get_current_workflow_name.return_value = "my_workflow"
            mock_gn.ContextManager.return_value = mock_context

            mock_workflow = Mock()
            mock_workflow.file_path = "my_project/my_workflow.json"
            mock_registry.get_workflow_by_name.return_value = mock_workflow
            mock_registry.get_complete_file_path.return_value = "/workspace/my_project/my_workflow.json"

            result = pm.on_get_path_for_macro_request(
                GetPathForMacroRequest(parsed_macro=ParsedMacro("{inputs}/img.png"), variables={})
            )
        assert isinstance(result, GetPathForMacroResultSuccess)
        assert result.resolved_path == Path("/workspace/my_project/inputs/img.png")

    def test_env_value_optional_workflow_dir_degrades_when_no_workflow(self) -> None:
        """An env value with optional {workflow_dir?:/} degrades gracefully when unsaved."""
        from griptape_nodes.common.macro_parser import ParsedMacro

        pm = self._make_pm_with_template(environment={"WF": "{workflow_dir?:/}sub"})
        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_context = Mock()
            mock_context.has_current_workflow.return_value = False
            mock_gn.ContextManager.return_value = mock_context

            result = pm.on_get_path_for_macro_request(
                GetPathForMacroRequest(parsed_macro=ParsedMacro("{outputs}/{WF}/x.png"), variables={})
            )
        assert isinstance(result, GetPathForMacroResultSuccess)
        assert result.resolved_path == Path("outputs/sub/x.png")

    def test_apply_project_env_writes_resolved_values_to_os_environ(self) -> None:
        pm = self._make_pm_with_template(environment={"FOO": "{workspace_dir}/sub"})
        assert pm._current_project_id is not None
        project_info = pm._successfully_loaded_project_templates[pm._current_project_id]
        # {workspace_dir} is substituted via str(Path(...)), so the platform's native
        # separator appears in the env value. Compare against the same construction
        # rather than hardcoding forward slashes.
        expected = f"{Path('/workspace')}/sub"
        try:
            original = os.environ.get("FOO")
            pm._apply_project_env(project_info)
            assert os.environ["FOO"] == expected
            pm._restore_project_env()
            if original is None:
                assert "FOO" not in os.environ
            else:
                assert os.environ["FOO"] == original
        finally:
            os.environ.pop("FOO", None)

    def test_macro_falls_back_to_shell_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A bare {VAR} reference should resolve from os.environ when not declared elsewhere."""
        from griptape_nodes.common.macro_parser import ParsedMacro

        monkeypatch.setenv("MY_SHELL_VAR", "from_shell")
        pm = self._make_pm_with_template(environment={})
        result = pm.on_get_path_for_macro_request(
            GetPathForMacroRequest(parsed_macro=ParsedMacro("{outputs}/{MY_SHELL_VAR}/x.png"), variables={})
        )
        assert isinstance(result, GetPathForMacroResultSuccess)
        assert result.resolved_path == Path("outputs/from_shell/x.png")

    def test_project_env_wins_over_shell_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If a var exists in both project env and shell env, project env wins."""
        from griptape_nodes.common.macro_parser import ParsedMacro

        monkeypatch.setenv("OVERRIDE_ME", "from_shell")
        pm = self._make_pm_with_template(environment={"OVERRIDE_ME": "from_project"})
        result = pm.on_get_path_for_macro_request(
            GetPathForMacroRequest(parsed_macro=ParsedMacro("{outputs}/{OVERRIDE_ME}/x.png"), variables={})
        )
        assert isinstance(result, GetPathForMacroResultSuccess)
        assert result.resolved_path == Path("outputs/from_project/x.png")

    def test_env_value_references_shell_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A project env value can recursively reference a shell env var."""
        from griptape_nodes.common.macro_parser import ParsedMacro

        monkeypatch.setenv("SHELL_ROOT", "/my/shell/root")
        pm = self._make_pm_with_template(environment={"PROJECT": "{SHELL_ROOT}/sub"})
        result = pm.on_get_path_for_macro_request(
            GetPathForMacroRequest(parsed_macro=ParsedMacro("{PROJECT}/x.png"), variables={})
        )
        assert isinstance(result, GetPathForMacroResultSuccess)
        assert result.resolved_path == Path("/my/shell/root/sub/x.png")

    def test_unknown_var_still_fails_when_not_in_shell(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A referenced var that exists nowhere (not in project env, not in shell) fails as before."""
        from griptape_nodes.common.macro_parser import ParsedMacro

        monkeypatch.delenv("DEFINITELY_NOT_SET", raising=False)
        pm = self._make_pm_with_template(environment={})
        result = pm.on_get_path_for_macro_request(
            GetPathForMacroRequest(parsed_macro=ParsedMacro("{outputs}/{DEFINITELY_NOT_SET}/x.png"), variables={})
        )
        assert isinstance(result, GetPathForMacroResultFailure)
        assert result.failure_reason == PathResolutionFailureReason.MISSING_REQUIRED_VARIABLES

    def test_apply_project_env_skips_on_resolution_failure(self, caplog: pytest.LogCaptureFixture) -> None:
        """If an env value can't be resolved (e.g. cycle), apply is skipped and nothing is written."""
        pm = self._make_pm_with_template(environment={"A": "{B}", "B": "{A}"})
        assert pm._current_project_id is not None
        project_info = pm._successfully_loaded_project_templates[pm._current_project_id]
        os.environ.pop("A", None)
        os.environ.pop("B", None)
        try:
            with caplog.at_level(logging.WARNING):
                pm._apply_project_env(project_info)
            assert "A" not in os.environ
            assert "B" not in os.environ
            assert pm._applied_env_snapshot == {}
        finally:
            os.environ.pop("A", None)
            os.environ.pop("B", None)


class TestProjectDirectoryRecursion:
    """Tests for recursive resolution of directory path_macros.

    A directory's path_macro may reference other directories, builtins, env vars,
    or shell env vars. Those references must resolve through the same machinery
    as project env values so nested directory graphs flatten to final paths.
    """

    def _make_pm_with_directories(
        self,
        directories: "dict[str, str | PerPlatformPathMacro]",
        *,
        environment: dict[str, str] | None = None,
        workspace_path: Path = Path("/workspace"),
        project_file_path: Path = Path("/proj/project.yml"),
    ) -> ProjectManager:
        from griptape_nodes.common.project_templates import (
            DirectoryDefinition,
            ProjectTemplate,
            ProjectValidationInfo,
            ProjectValidationStatus,
        )
        from griptape_nodes.retained_mode.managers.project_manager import ProjectInfo

        mock_config = Mock()
        mock_config.workspace_path = workspace_path
        mock_config.get_config_value.return_value = "staticfiles"
        pm = ProjectManager(Mock(), mock_config, Mock())

        template = ProjectTemplate(
            project_template_schema_version="0.1.0",
            name="test_project",
            directories={
                name: DirectoryDefinition(name=name, path_macro=path_macro) for name, path_macro in directories.items()
            },
            situations={},
            environment=environment or {},
        )
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        situation_schemas = pm._parse_situation_macros(template.situations, validation)
        directory_schemas = pm._parse_directory_macros(template.directories, validation)

        project_id = str(project_file_path)
        pm._successfully_loaded_project_templates[project_id] = ProjectInfo(
            project_id=project_id,
            project_file_path=project_file_path,
            project_base_dir=project_file_path.parent,
            template=template,
            validation=validation,
            parsed_situation_schemas=situation_schemas,
            parsed_directory_schemas=directory_schemas,
        )
        pm._current_project_id = project_id
        return pm

    def test_directory_references_another_directory(self) -> None:
        from griptape_nodes.common.macro_parser import ParsedMacro

        pm = self._make_pm_with_directories(
            {
                "watch_folder": "{workspace_dir}/watch",
                "watch_output": "{watch_folder}/outputs",
            }
        )
        result = pm.on_get_path_for_macro_request(
            GetPathForMacroRequest(parsed_macro=ParsedMacro("{watch_output}/img.png"), variables={})
        )
        assert isinstance(result, GetPathForMacroResultSuccess)
        assert result.resolved_path == Path("/workspace/watch/outputs/img.png")

    def test_directory_references_env_var(self) -> None:
        from griptape_nodes.common.macro_parser import ParsedMacro

        pm = self._make_pm_with_directories(
            directories={"outputs": "{BASE}/outputs"},
            environment={"BASE": "my_base"},
        )
        result = pm.on_get_path_for_macro_request(
            GetPathForMacroRequest(parsed_macro=ParsedMacro("{outputs}/img.png"), variables={})
        )
        assert isinstance(result, GetPathForMacroResultSuccess)
        assert result.resolved_path == Path("my_base/outputs/img.png")

    def test_directory_references_shell_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from griptape_nodes.common.macro_parser import ParsedMacro

        monkeypatch.setenv("SHELL_ROOT", "/from/shell")
        pm = self._make_pm_with_directories({"outputs": "{SHELL_ROOT}/outputs"})
        result = pm.on_get_path_for_macro_request(
            GetPathForMacroRequest(parsed_macro=ParsedMacro("{outputs}/img.png"), variables={})
        )
        assert isinstance(result, GetPathForMacroResultSuccess)
        assert result.resolved_path == Path("/from/shell/outputs/img.png")

    def test_directory_cycle_detected(self) -> None:
        from griptape_nodes.common.macro_parser import ParsedMacro

        pm = self._make_pm_with_directories(
            {
                "a": "{b}/a",
                "b": "{a}/b",
            }
        )
        result = pm.on_get_path_for_macro_request(
            GetPathForMacroRequest(parsed_macro=ParsedMacro("{a}/img.png"), variables={})
        )
        assert isinstance(result, GetPathForMacroResultFailure)
        assert result.failure_reason == PathResolutionFailureReason.MACRO_RESOLUTION_ERROR
        from griptape_nodes.retained_mode.events.base_events import ResultDetails

        assert isinstance(result.result_details, ResultDetails)
        assert "cycle" in str(result.result_details).lower()

    def test_directory_references_unknown_name(self) -> None:
        from griptape_nodes.common.macro_parser import ParsedMacro

        pm = self._make_pm_with_directories({"outputs": "{NOT_DEFINED}/outputs"})
        result = pm.on_get_path_for_macro_request(
            GetPathForMacroRequest(parsed_macro=ParsedMacro("{outputs}/img.png"), variables={})
        )
        assert isinstance(result, GetPathForMacroResultFailure)
        assert result.failure_reason == PathResolutionFailureReason.MACRO_RESOLUTION_ERROR

    @pytest.mark.parametrize(
        ("platform_value", "expected_path"),
        [
            ("linux", Path("/mnt/shared/outputs/img.png")),
            ("darwin", Path("/Volumes/Shared/outputs/img.png")),
            ("win32", Path("C:/Shared/outputs/img.png")),
        ],
    )
    def test_directory_per_platform_picks_active_os(
        self, monkeypatch: pytest.MonkeyPatch, platform_value: str, expected_path: Path
    ) -> None:
        from griptape_nodes.common.macro_parser import ParsedMacro
        from griptape_nodes.common.project_templates.directory import PerPlatformPathMacro

        monkeypatch.setattr(sys, "platform", platform_value)
        pm = self._make_pm_with_directories(
            {
                "outputs": PerPlatformPathMacro(
                    linux="/mnt/shared/outputs",
                    darwin="/Volumes/Shared/outputs",
                    windows="C:/Shared/outputs",
                ),
            }
        )
        result = pm.on_get_path_for_macro_request(
            GetPathForMacroRequest(parsed_macro=ParsedMacro("{outputs}/img.png"), variables={})
        )
        assert isinstance(result, GetPathForMacroResultSuccess)
        assert result.resolved_path == expected_path

    @pytest.mark.parametrize("platform_value", ["linux", "darwin", "win32"])
    def test_directory_per_platform_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch, platform_value: str
    ) -> None:
        from griptape_nodes.common.macro_parser import ParsedMacro
        from griptape_nodes.common.project_templates.directory import PerPlatformPathMacro

        monkeypatch.setattr(sys, "platform", platform_value)
        pm = self._make_pm_with_directories({"outputs": PerPlatformPathMacro(default="{workspace_dir}/fallback")})
        result = pm.on_get_path_for_macro_request(
            GetPathForMacroRequest(parsed_macro=ParsedMacro("{outputs}/img.png"), variables={})
        )
        assert isinstance(result, GetPathForMacroResultSuccess)
        assert result.resolved_path == Path("/workspace/fallback/img.png")

    def test_directory_per_platform_active_missing_no_default_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from griptape_nodes.common.macro_parser import ParsedMacro
        from griptape_nodes.common.project_templates.directory import PerPlatformPathMacro

        monkeypatch.setattr(sys, "platform", "linux")
        # Mapping has only darwin / windows entries; linux engine and no default => failure.
        pm = self._make_pm_with_directories(
            {
                "outputs": PerPlatformPathMacro(
                    darwin="/Volumes/Shared/outputs",
                    windows="C:/Shared/outputs",
                ),
            }
        )
        result = pm.on_get_path_for_macro_request(
            GetPathForMacroRequest(parsed_macro=ParsedMacro("{outputs}/img.png"), variables={})
        )
        assert isinstance(result, GetPathForMacroResultFailure)
        assert result.failure_reason == PathResolutionFailureReason.MACRO_RESOLUTION_ERROR

    def test_directory_per_platform_empty_mapping_rejected(self) -> None:
        from griptape_nodes.common.project_templates.directory import PerPlatformPathMacro

        with pytest.raises(ValueError, match="at least one"):
            PerPlatformPathMacro()

    def test_directory_per_platform_macro_resolves_recursively(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from griptape_nodes.common.macro_parser import ParsedMacro
        from griptape_nodes.common.project_templates.directory import PerPlatformPathMacro

        monkeypatch.setattr(sys, "platform", "darwin")
        # Per-platform value still recurses through {workspace_dir} like the string form.
        pm = self._make_pm_with_directories({"outputs": PerPlatformPathMacro(darwin="{workspace_dir}/mac_outputs")})
        result = pm.on_get_path_for_macro_request(
            GetPathForMacroRequest(parsed_macro=ParsedMacro("{outputs}/img.png"), variables={})
        )
        assert isinstance(result, GetPathForMacroResultSuccess)
        assert result.resolved_path == Path("/workspace/mac_outputs/img.png")

    def test_directory_string_form_still_works(self) -> None:
        # Regression guard: pre-existing string path_macros must keep resolving unchanged.
        from griptape_nodes.common.macro_parser import ParsedMacro

        pm = self._make_pm_with_directories({"outputs": "{workspace_dir}/legacy"})
        result = pm.on_get_path_for_macro_request(
            GetPathForMacroRequest(parsed_macro=ParsedMacro("{outputs}/img.png"), variables={})
        )
        assert isinstance(result, GetPathForMacroResultSuccess)
        assert result.resolved_path == Path("/workspace/legacy/img.png")


class TestProjectParentChain:
    """Tests for `parent_project_path` chained inheritance.

    The handler reads YAML via `ReadFileRequest` routed through
    `GriptapeNodes.ahandle_request`. These tests patch that boundary so the
    project files live entirely in memory, keyed by the on-disk paths the
    handler canonicalizes the request paths into.
    """

    BASE_PROJECT_YAML = """\
project_template_schema_version: "0.3.2"
name: Base Project
directories:
  shared_outputs:
    path_macro: "{workspace_dir}/base_outputs"
"""

    CHILD_PROJECT_YAML_TEMPLATE = """\
project_template_schema_version: "0.3.2"
name: Child Project
parent_project_path: "{parent}"
directories:
  child_outputs:
    path_macro: "{{workspace_dir}}/child_outputs"
"""

    GRANDCHILD_PROJECT_YAML_TEMPLATE = """\
project_template_schema_version: "0.3.2"
name: Grandchild Project
parent_project_path: "{parent}"
directories:
  grandchild_outputs:
    path_macro: "{{workspace_dir}}/grandchild_outputs"
"""

    @pytest.fixture
    def pm(self, tmp_path: Path) -> ProjectManager:
        mock_event_manager = Mock()
        mock_config_manager = Mock()
        mock_config_manager.project_config = {}
        mock_config_manager.env_config = {}
        mock_config_manager.merged_config = {}
        mock_config_manager.get_config_value.return_value = []
        mock_config_manager.read_config_file_value.return_value = None
        mock_config_manager.workspace_path = tmp_path
        return ProjectManager(mock_event_manager, mock_config_manager, Mock())

    @staticmethod
    def _file_router(files: dict[Path, str]) -> AsyncMock:
        """Build an `ahandle_request` mock that returns YAML content based on file path.

        Unknown paths return a failure result so the handler treats them as MISSING.
        """
        from griptape_nodes.retained_mode.events.os_events import (
            FileIOFailureReason,
            ReadFileResultFailure,
            ReadFileResultSuccess,
        )

        async def route(request: object) -> object:
            file_path = Path(getattr(request, "file_path", ""))
            content = files.get(file_path)
            if content is None:
                return ReadFileResultFailure(
                    failure_reason=FileIOFailureReason.FILE_NOT_FOUND,
                    result_details=f"missing: {file_path}",
                )
            return ReadFileResultSuccess(
                content=content,
                file_size=len(content),
                mime_type="text/plain",
                encoding="utf-8",
                result_details="ok",
            )

        return AsyncMock(side_effect=route)

    @pytest.mark.asyncio
    async def test_no_parent_still_merges_with_defaults(self, pm: ProjectManager, tmp_path: Path) -> None:
        """A project without parent_project_path still merges on top of system defaults."""
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )

        child_path = (tmp_path / "child.yml").resolve()
        files = {
            child_path: self.CHILD_PROJECT_YAML_TEMPLATE.replace('parent_project_path: "{parent}"\n', "").format(),
        }
        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = self._file_router(files)
            result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=child_path))

        assert isinstance(result, LoadProjectTemplateResultSuccess)
        # Child's own directory survives.
        assert "child_outputs" in result.template.directories
        # System default directories still inherited.
        assert "outputs" in result.template.directories

    @pytest.mark.asyncio
    async def test_parent_directories_inherited_into_child(self, pm: ProjectManager, tmp_path: Path) -> None:
        """A child with parent_project_path inherits directories declared by the parent."""
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )

        base_path = (tmp_path / "base.yml").resolve()
        child_path = (tmp_path / "child.yml").resolve()
        files = {
            base_path: self.BASE_PROJECT_YAML,
            child_path: self.CHILD_PROJECT_YAML_TEMPLATE.format(parent=base_path.as_posix()),
        }

        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = self._file_router(files)
            result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=child_path))

        assert isinstance(result, LoadProjectTemplateResultSuccess)
        # Inherited from parent.
        assert "shared_outputs" in result.template.directories
        # Declared by child.
        assert "child_outputs" in result.template.directories
        # parent_project_path round-trips on the merged template (so save_overlay can re-emit it).
        # The child YAML stores the parent as a POSIX path (matches what the GUI/engine emit
        # cross-platform), so compare against the POSIX form rather than the platform str().
        assert result.template.parent_project_path == base_path.as_posix()

    @pytest.mark.asyncio
    async def test_multi_level_chain_walks_all_ancestors(self, pm: ProjectManager, tmp_path: Path) -> None:
        """Grandchild inherits from both parent and grandparent in one merged template."""
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )

        base_path = (tmp_path / "base.yml").resolve()
        child_path = (tmp_path / "child.yml").resolve()
        grandchild_path = (tmp_path / "grandchild.yml").resolve()
        files = {
            base_path: self.BASE_PROJECT_YAML,
            child_path: self.CHILD_PROJECT_YAML_TEMPLATE.format(parent=base_path.as_posix()),
            grandchild_path: self.GRANDCHILD_PROJECT_YAML_TEMPLATE.format(parent=child_path.as_posix()),
        }

        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = self._file_router(files)
            result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=grandchild_path))

        assert isinstance(result, LoadProjectTemplateResultSuccess)
        # All three layers present.
        assert "shared_outputs" in result.template.directories
        assert "child_outputs" in result.template.directories
        assert "grandchild_outputs" in result.template.directories

    @pytest.mark.asyncio
    async def test_parent_path_resolves_relative_to_child(self, pm: ProjectManager, tmp_path: Path) -> None:
        """A relative parent_project_path resolves against the child's directory."""
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )

        sub_dir = tmp_path / "sub"
        sub_dir.mkdir()
        base_path = (tmp_path / "base.yml").resolve()
        child_path = (sub_dir / "child.yml").resolve()
        files = {
            base_path: self.BASE_PROJECT_YAML,
            # Relative path: child sits in tmp_path/sub, so ../base.yml resolves to base_path.
            child_path: self.CHILD_PROJECT_YAML_TEMPLATE.format(parent="../base.yml"),
        }

        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = self._file_router(files)
            result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=child_path))

        assert isinstance(result, LoadProjectTemplateResultSuccess)
        assert "shared_outputs" in result.template.directories

    @pytest.mark.asyncio
    async def test_self_reference_detected_as_cycle(self, pm: ProjectManager, tmp_path: Path) -> None:
        """A project that names itself as its own parent fails with a cycle error."""
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultFailure,
        )

        self_path = (tmp_path / "self.yml").resolve()
        files = {self_path: self.CHILD_PROJECT_YAML_TEMPLATE.format(parent=self_path.as_posix())}

        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = self._file_router(files)
            result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=self_path))

        assert isinstance(result, LoadProjectTemplateResultFailure)
        assert any("Cycle" in p.message and p.field_path == "parent_project_path" for p in result.validation.problems)

    @pytest.mark.asyncio
    async def test_two_node_cycle_detected(self, pm: ProjectManager, tmp_path: Path) -> None:
        """A → B → A is detected and reported."""
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultFailure,
        )

        a_path = (tmp_path / "a.yml").resolve()
        b_path = (tmp_path / "b.yml").resolve()
        files = {
            a_path: self.CHILD_PROJECT_YAML_TEMPLATE.format(parent=b_path.as_posix()),
            b_path: self.CHILD_PROJECT_YAML_TEMPLATE.format(parent=a_path.as_posix()),
        }

        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = self._file_router(files)
            result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=a_path))

        assert isinstance(result, LoadProjectTemplateResultFailure)
        assert any("Cycle" in p.message for p in result.validation.problems)

    @pytest.mark.asyncio
    async def test_missing_parent_surfaces_as_validation_error(self, pm: ProjectManager, tmp_path: Path) -> None:
        """A child whose parent file is missing fails to load with a clear error."""
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultFailure,
        )

        missing_parent = (tmp_path / "does_not_exist.yml").resolve()
        child_path = (tmp_path / "child.yml").resolve()
        files = {child_path: self.CHILD_PROJECT_YAML_TEMPLATE.format(parent=missing_parent.as_posix())}

        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = self._file_router(files)
            result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=child_path))

        assert isinstance(result, LoadProjectTemplateResultFailure)
        assert any(
            "could not be loaded" in p.message and p.field_path == "parent_project_path"
            for p in result.validation.problems
        )

    @pytest.mark.asyncio
    async def test_child_overrides_parent_directory(self, pm: ProjectManager, tmp_path: Path) -> None:
        """Child can override a directory declared by its parent."""
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )

        base_path = (tmp_path / "base.yml").resolve()
        child_path = (tmp_path / "child.yml").resolve()

        # Child overrides shared_outputs that the parent declared.
        child_yaml = f"""\
project_template_schema_version: "0.3.2"
name: Child Override
parent_project_path: "{base_path.as_posix()}"
directories:
  shared_outputs:
    path_macro: "{{workspace_dir}}/child_override"
"""

        files = {
            base_path: self.BASE_PROJECT_YAML,
            child_path: child_yaml,
        }

        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = self._file_router(files)
            result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=child_path))

        assert isinstance(result, LoadProjectTemplateResultSuccess)
        merged = result.template.directories["shared_outputs"]
        # Override took effect.
        assert merged.path_macro == "{workspace_dir}/child_override"

    @pytest.mark.asyncio
    async def test_list_canonicalizes_relative_parent_project_path(self, pm: ProjectManager, tmp_path: Path) -> None:
        """ListProjectTemplatesRequest must resolve a legacy relative parent_project_path to the parent's id.

        The GUI tree-build relies on string equality between a child's emitted
        parent_project_id and a peer's project_id. A legacy child stores a
        (possibly relative) parent_project_path; the engine must resolve it to the
        parent's registered id before emitting the list, or the tree linkage
        silently falls apart. The parentless base has no explicit id, so its id is
        its canonical path string (the legacy bridge), which is what the child's
        parent_project_id must equal.
        """
        from griptape_nodes.retained_mode.events.project_events import (
            ListProjectTemplatesRequest,
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )

        sub_dir = tmp_path / "sub"
        sub_dir.mkdir()
        base_path = (tmp_path / "base.yml").resolve()
        child_path = (sub_dir / "child.yml").resolve()
        files = {
            base_path: self.BASE_PROJECT_YAML,
            child_path: self.CHILD_PROJECT_YAML_TEMPLATE.format(parent="../base.yml"),
        }

        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = self._file_router(files)
            base_load = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=base_path))
            child_load = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=child_path))

        assert isinstance(base_load, LoadProjectTemplateResultSuccess)
        assert isinstance(child_load, LoadProjectTemplateResultSuccess)

        list_result = pm.on_list_project_templates_request(ListProjectTemplatesRequest(include_system_builtins=False))
        by_id = {info.project_id: info for info in list_result.successfully_loaded}

        # Child's parent_project_id from the list must match the base's project_id
        # so the GUI can build the tree via direct string lookup.
        child_info = by_id[str(child_path)]
        assert child_info.parent_project_id == str(base_path)

    @pytest.mark.asyncio
    async def test_parent_project_path_relative_resolves_against_child_yaml(
        self, pm: ProjectManager, tmp_path: Path
    ) -> None:
        """`parent_project_path` written as `./base.yml` resolves against the child YAML's directory.

        Relative encoding is the supported portable form: it doesn't depend on
        runtime workspace state, so the same string resolves to the same
        sibling file regardless of which project is currently active.
        """
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )

        base_path = (tmp_path / "base.yml").resolve()
        child_path = (tmp_path / "child.yml").resolve()
        files = {
            base_path: self.BASE_PROJECT_YAML,
            child_path: self.CHILD_PROJECT_YAML_TEMPLATE.format(parent="./base.yml"),
        }

        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = self._file_router(files)
            await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=base_path))
            child_load = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=child_path))

        assert isinstance(child_load, LoadProjectTemplateResultSuccess)
        # Merged template carries the parent's contribution, so the child sees both directories.
        assert "shared_outputs" in child_load.template.directories
        assert "child_outputs" in child_load.template.directories

    @pytest.mark.asyncio
    async def test_overlay_round_trip_preserves_parent_project_path(self, pm: ProjectManager, tmp_path: Path) -> None:
        """Saving a child's overlay YAML must preserve parent_project_path.

        Covers the GUI save path: load merged template -> edit -> to_overlay_yaml ->
        re-load. If the parent linkage is dropped, edits silently flatten the chain.
        """
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )

        base_path = (tmp_path / "base.yml").resolve()
        child_path = (tmp_path / "child.yml").resolve()
        files = {
            base_path: self.BASE_PROJECT_YAML,
            child_path: self.CHILD_PROJECT_YAML_TEMPLATE.format(parent=base_path.as_posix()),
        }

        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = self._file_router(files)
            result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=child_path))

        assert isinstance(result, LoadProjectTemplateResultSuccess)

        # Re-emit the child as overlay YAML against the base parent it was loaded with.
        # The merged result template should still serialize parent_project_path when
        # diffed against a base that doesn't declare one.

        overlay_yaml = result.template.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE)
        # YAML may quote the key; match on the substring, not a strict prefix.
        assert "parent_project_path" in overlay_yaml
        # The path is stored in POSIX form (as written into the child YAML), so compare
        # against the POSIX spelling rather than the platform-native str().
        assert base_path.as_posix() in overlay_yaml

    @pytest.mark.asyncio
    async def test_grandchild_tombstones_grandparent_directory(self, pm: ProjectManager, tmp_path: Path) -> None:
        """A grandchild can null out a directory declared by the grandparent.

        Pins the atomic-overlay merge: tombstones must compose through the chain,
        not just override the immediate parent.
        """
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )

        base_path = (tmp_path / "base.yml").resolve()
        child_path = (tmp_path / "child.yml").resolve()
        grandchild_path = (tmp_path / "grandchild.yml").resolve()

        # Grandchild explicitly tombstones `shared_outputs` (declared by grandparent).
        grandchild_yaml = f"""\
project_template_schema_version: "0.3.2"
name: Grandchild
parent_project_path: "{child_path.as_posix()}"
directories:
  shared_outputs: null
  grandchild_outputs:
    path_macro: "{{workspace_dir}}/grandchild_outputs"
"""

        files = {
            base_path: self.BASE_PROJECT_YAML,
            child_path: self.CHILD_PROJECT_YAML_TEMPLATE.format(parent=base_path.as_posix()),
            grandchild_path: grandchild_yaml,
        }

        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = self._file_router(files)
            result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=grandchild_path))

        assert isinstance(result, LoadProjectTemplateResultSuccess)
        # Tombstoned across two levels of inheritance.
        assert "shared_outputs" not in result.template.directories
        # Grandchild's own additions still present.
        assert "grandchild_outputs" in result.template.directories
        # Intermediate child's directory still present.
        assert "child_outputs" in result.template.directories

    @pytest.mark.asyncio
    async def test_parent_situations_environment_and_file_ext_dirs_inherit(
        self, pm: ProjectManager, tmp_path: Path
    ) -> None:
        """All inheritable sections (situations, environment, file_extension_directories) flow from parent.

        Existing tests only cover directories; this guards against a "merge handler is
        directories-only" regression in the chain walk.
        """
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )

        rich_parent_yaml = """\
project_template_schema_version: "0.3.2"
name: Rich Parent
situations:
  custom_situation:
    name: custom_situation
    macro: "{outputs}/custom/{file_name_base}.{file_extension}"
    policy:
      on_collision: create_new
      create_dirs: true
    fallback: null
environment:
  CUSTOM_VAR: "from_parent"
file_extension_directories:
  xyz: "custom_xyz"
"""

        base_path = (tmp_path / "rich_parent.yml").resolve()
        child_path = (tmp_path / "child.yml").resolve()
        files = {
            base_path: rich_parent_yaml,
            child_path: self.CHILD_PROJECT_YAML_TEMPLATE.format(parent=base_path.as_posix()),
        }

        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = self._file_router(files)
            result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=child_path))

        assert isinstance(result, LoadProjectTemplateResultSuccess)
        assert "custom_situation" in result.template.situations
        assert result.template.environment.get("CUSTOM_VAR") == "from_parent"
        assert result.template.file_extension_directories.get("xyz") == "custom_xyz"

    @pytest.mark.asyncio
    async def test_two_siblings_with_same_parent_load_independently(self, pm: ProjectManager, tmp_path: Path) -> None:
        """Loading sibling A then sibling B (both pointing at the same parent) must both succeed.

        Guards against the visited-set bleeding across top-level loads.
        """
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )

        base_path = (tmp_path / "base.yml").resolve()
        sibling_a_path = (tmp_path / "sibling_a.yml").resolve()
        sibling_b_path = (tmp_path / "sibling_b.yml").resolve()
        files = {
            base_path: self.BASE_PROJECT_YAML,
            sibling_a_path: self.CHILD_PROJECT_YAML_TEMPLATE.format(parent=base_path.as_posix()),
            sibling_b_path: self.CHILD_PROJECT_YAML_TEMPLATE.format(parent=base_path.as_posix()),
        }

        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = self._file_router(files)
            a_result = await pm.on_load_project_template_request(
                LoadProjectTemplateRequest(project_path=sibling_a_path)
            )
            b_result = await pm.on_load_project_template_request(
                LoadProjectTemplateRequest(project_path=sibling_b_path)
            )

        assert isinstance(a_result, LoadProjectTemplateResultSuccess)
        assert isinstance(b_result, LoadProjectTemplateResultSuccess)
        # Both inherit the same parent directory.
        assert "shared_outputs" in a_result.template.directories
        assert "shared_outputs" in b_result.template.directories

    @pytest.mark.asyncio
    async def test_three_node_cycle_detected(self, pm: ProjectManager, tmp_path: Path) -> None:
        """A -> B -> C -> A cycle is reported."""
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultFailure,
        )

        a_path = (tmp_path / "a.yml").resolve()
        b_path = (tmp_path / "b.yml").resolve()
        c_path = (tmp_path / "c.yml").resolve()
        files = {
            a_path: self.CHILD_PROJECT_YAML_TEMPLATE.format(parent=b_path.as_posix()),
            b_path: self.CHILD_PROJECT_YAML_TEMPLATE.format(parent=c_path.as_posix()),
            c_path: self.CHILD_PROJECT_YAML_TEMPLATE.format(parent=a_path.as_posix()),
        }

        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = self._file_router(files)
            result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=a_path))

        assert isinstance(result, LoadProjectTemplateResultFailure)
        assert any("Cycle" in p.message for p in result.validation.problems)

    @pytest.mark.asyncio
    async def test_cycle_through_relative_paths_detected(self, pm: ProjectManager, tmp_path: Path) -> None:
        """A relative-path cycle (./a <-> ./b) must canonicalize before the visited check fires."""
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultFailure,
        )

        a_path = (tmp_path / "a.yml").resolve()
        b_path = (tmp_path / "b.yml").resolve()
        files = {
            a_path: self.CHILD_PROJECT_YAML_TEMPLATE.format(parent="./b.yml"),
            b_path: self.CHILD_PROJECT_YAML_TEMPLATE.format(parent="./a.yml"),
        }

        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = self._file_router(files)
            result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=a_path))

        assert isinstance(result, LoadProjectTemplateResultFailure)
        assert any("Cycle" in p.message for p in result.validation.problems)


class TestSaveProjectTemplate:
    """Tests for `SaveProjectTemplateRequest`'s parent-aware overlay diff.

    The save handler diffs the merged template against either system defaults
    (when there's no parent) or the parent's fully-merged template (when there
    is one). These tests pair a real `tmp_path` write with the `_file_router`
    mock used by `TestProjectParentChain` so the parent can be loaded into the
    registry before the child is saved.
    """

    BASE_PARENT_YAML = """\
project_template_schema_version: "0.3.2"
name: Base Parent
directories:
  outputs:
    path_macro: "outputs2"
"""

    # Description that `outputs` carries via DEFAULT_PROJECT_TEMPLATE -> parent merge.
    # Tests that hand-build a child's `outputs` directory must include this so the
    # child matches the parent's merged value field-for-field; otherwise per-item
    # atomic diff treats the child as divergent on `description`.
    BASE_PARENT_OUTPUTS_DESCRIPTION = "Files generated by nodes during workflow execution."

    @pytest.fixture
    def pm(self, tmp_path: Path) -> ProjectManager:
        mock_event_manager = Mock()
        mock_config_manager = Mock()
        mock_config_manager.project_config = {}
        mock_config_manager.env_config = {}
        mock_config_manager.merged_config = {}
        mock_config_manager.get_config_value.return_value = []
        mock_config_manager.read_config_file_value.return_value = None
        mock_config_manager.workspace_path = tmp_path
        return ProjectManager(mock_event_manager, mock_config_manager, Mock())

    @staticmethod
    def _file_router(files: dict[Path, str]) -> AsyncMock:
        """Build an `ahandle_request` mock that returns YAML content based on file path.

        Mirrors `TestProjectParentChain._file_router`: unknown paths return a
        FILE_NOT_FOUND failure so the load handler treats them as MISSING.
        """
        from griptape_nodes.retained_mode.events.os_events import (
            FileIOFailureReason,
            ReadFileResultFailure,
            ReadFileResultSuccess,
        )

        async def route(request: object) -> object:
            file_path = Path(getattr(request, "file_path", ""))
            content = files.get(file_path)
            if content is None:
                return ReadFileResultFailure(
                    failure_reason=FileIOFailureReason.FILE_NOT_FOUND,
                    result_details=f"missing: {file_path}",
                )
            return ReadFileResultSuccess(
                content=content,
                file_size=len(content),
                mime_type="text/plain",
                encoding="utf-8",
                result_details="ok",
            )

        return AsyncMock(side_effect=route)

    @staticmethod
    def _parse_yaml(yaml_text: str) -> dict:
        """Parse a YAML string back into a dict for assertions."""
        from ruamel.yaml import YAML

        yaml_loader = YAML(typ="safe")
        return yaml_loader.load(yaml_text)

    async def _load_parent(self, pm: ProjectManager, parent_path: Path, parent_yaml: str) -> None:
        """Load `parent_yaml` from `parent_path` into the registry."""
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )

        files = {parent_path: parent_yaml}
        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = self._file_router(files)
            result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=parent_path))
        assert isinstance(result, LoadProjectTemplateResultSuccess)

    def test_save_without_parent_diffs_against_system_defaults(self, pm: ProjectManager, tmp_path: Path) -> None:
        """Regression: with no parent, diff base remains DEFAULT_PROJECT_TEMPLATE."""
        from griptape_nodes.retained_mode.events.project_events import (
            SaveProjectTemplateRequest,
            SaveProjectTemplateResultSuccess,
        )

        child_path = tmp_path / "solo.yml"
        template_data = {
            "project_template_schema_version": "0.3.2",
            "name": "solo",
            "parent_project_path": None,
            "situations": {},
            "directories": {
                "outputs": {"name": "outputs", "path_macro": "custom_outputs"},
            },
        }
        result = pm.on_save_project_template_request(
            SaveProjectTemplateRequest(project_path=child_path, template_data=template_data)
        )
        assert isinstance(result, SaveProjectTemplateResultSuccess)
        # Custom value diverges from default ("outputs"), so it must be emitted.
        parsed = self._parse_yaml(child_path.read_text())
        assert parsed["directories"]["outputs"]["path_macro"] == "custom_outputs"

    @pytest.mark.asyncio
    async def test_save_with_parent_omits_inherited_directory(self, pm: ProjectManager, tmp_path: Path) -> None:
        """Headline bug: child whose merged value matches the parent must omit the directory entirely."""
        from griptape_nodes.retained_mode.events.project_events import (
            SaveProjectTemplateRequest,
            SaveProjectTemplateResultSuccess,
        )

        parent_path = (tmp_path / "parent.yml").resolve()
        await self._load_parent(pm, parent_path, self.BASE_PARENT_YAML)

        child_path = tmp_path / "child.yml"
        template_data = {
            "project_template_schema_version": "0.3.2",
            "name": "child",
            "parent_project_path": str(parent_path),
            "situations": {},
            "directories": {
                "outputs": {
                    "name": "outputs",
                    "path_macro": "outputs2",
                    "description": self.BASE_PARENT_OUTPUTS_DESCRIPTION,
                },
            },
        }
        result = pm.on_save_project_template_request(
            SaveProjectTemplateRequest(project_path=child_path, template_data=template_data)
        )
        assert isinstance(result, SaveProjectTemplateResultSuccess)
        parsed = self._parse_yaml(child_path.read_text())
        # Child inherits the value, so neither `directories` nor `outputs` should appear.
        assert "directories" not in parsed or "outputs" not in (parsed.get("directories") or {})

    @pytest.mark.asyncio
    async def test_save_with_parent_emits_diverging_directory(self, pm: ProjectManager, tmp_path: Path) -> None:
        """A child value that differs from the parent must still be emitted."""
        from griptape_nodes.retained_mode.events.project_events import (
            SaveProjectTemplateRequest,
            SaveProjectTemplateResultSuccess,
        )

        parent_path = (tmp_path / "parent.yml").resolve()
        await self._load_parent(pm, parent_path, self.BASE_PARENT_YAML)

        child_path = tmp_path / "child.yml"
        template_data = {
            "project_template_schema_version": "0.3.2",
            "name": "child",
            "parent_project_path": str(parent_path),
            "situations": {},
            "directories": {
                "outputs": {"name": "outputs", "path_macro": "outputs3"},
            },
        }
        result = pm.on_save_project_template_request(
            SaveProjectTemplateRequest(project_path=child_path, template_data=template_data)
        )
        assert isinstance(result, SaveProjectTemplateResultSuccess)
        parsed = self._parse_yaml(child_path.read_text())
        assert parsed["directories"]["outputs"]["path_macro"] == "outputs3"

    @pytest.mark.asyncio
    async def test_save_with_parent_emits_new_directory_not_in_parent(self, pm: ProjectManager, tmp_path: Path) -> None:
        """A child directory absent from the parent must appear in the overlay."""
        from griptape_nodes.retained_mode.events.project_events import (
            SaveProjectTemplateRequest,
            SaveProjectTemplateResultSuccess,
        )

        parent_path = (tmp_path / "parent.yml").resolve()
        await self._load_parent(pm, parent_path, self.BASE_PARENT_YAML)

        child_path = tmp_path / "child.yml"
        template_data = {
            "project_template_schema_version": "0.3.2",
            "name": "child",
            "parent_project_path": str(parent_path),
            "situations": {},
            "directories": {
                "outputs": {
                    "name": "outputs",
                    "path_macro": "outputs2",
                    "description": self.BASE_PARENT_OUTPUTS_DESCRIPTION,
                },  # inherited
                "scratch": {"name": "scratch", "path_macro": "scratch_dir"},  # new
            },
        }
        result = pm.on_save_project_template_request(
            SaveProjectTemplateRequest(project_path=child_path, template_data=template_data)
        )
        assert isinstance(result, SaveProjectTemplateResultSuccess)
        parsed = self._parse_yaml(child_path.read_text())
        assert parsed["directories"]["scratch"]["path_macro"] == "scratch_dir"
        # Inherited `outputs` should not be re-emitted.
        assert "outputs" not in parsed["directories"]

    @pytest.mark.asyncio
    async def test_save_with_parent_emits_custom_directory_description(
        self, pm: ProjectManager, tmp_path: Path
    ) -> None:
        """A child that customizes only `description` (path inherited) must emit the directory."""
        from griptape_nodes.retained_mode.events.project_events import (
            SaveProjectTemplateRequest,
            SaveProjectTemplateResultSuccess,
        )

        parent_path = (tmp_path / "parent.yml").resolve()
        await self._load_parent(pm, parent_path, self.BASE_PARENT_YAML)

        child_path = tmp_path / "child.yml"
        template_data = {
            "project_template_schema_version": "0.3.2",
            "name": "child",
            "parent_project_path": str(parent_path),
            "situations": {},
            "directories": {
                "outputs": {
                    "name": "outputs",
                    "path_macro": "outputs2",  # matches parent
                    "description": "Renders ready for delivery.",  # diverges from parent
                },
            },
        }
        result = pm.on_save_project_template_request(
            SaveProjectTemplateRequest(project_path=child_path, template_data=template_data)
        )
        assert isinstance(result, SaveProjectTemplateResultSuccess)
        parsed = self._parse_yaml(child_path.read_text())
        # Per-item atomic diff: when a directory differs at all, the full item is emitted.
        assert parsed["directories"]["outputs"]["description"] == "Renders ready for delivery."
        assert parsed["directories"]["outputs"]["path_macro"] == "outputs2"

    @pytest.mark.asyncio
    async def test_save_with_parent_clears_inherited_directory_description(
        self, pm: ProjectManager, tmp_path: Path
    ) -> None:
        """A child that clears an inherited directory description must round-trip the null."""
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
            SaveProjectTemplateRequest,
            SaveProjectTemplateResultSuccess,
        )

        # Parent explicitly sets a description so the child can clear it.
        parent_yaml = """\
project_template_schema_version: "0.3.2"
name: With Description
directories:
  outputs:
    path_macro: "outputs2"
    description: "Parent-level description."
"""
        parent_path = (tmp_path / "parent.yml").resolve()
        await self._load_parent(pm, parent_path, parent_yaml)

        child_path = tmp_path / "child.yml"
        template_data = {
            "project_template_schema_version": "0.3.2",
            "name": "child",
            "parent_project_path": str(parent_path),
            "situations": {},
            "directories": {
                "outputs": {
                    "name": "outputs",
                    "path_macro": "outputs2",  # matches parent
                    "description": None,  # explicit clear
                },
            },
        }
        result = pm.on_save_project_template_request(
            SaveProjectTemplateRequest(project_path=child_path, template_data=template_data)
        )
        assert isinstance(result, SaveProjectTemplateResultSuccess)
        parsed = self._parse_yaml(child_path.read_text())
        # The description diverges from the parent's, so the directory is emitted with description: null.
        assert parsed["directories"]["outputs"]["description"] is None

        # Round-trip: reload the child and confirm the inherited description is cleared.
        files = {parent_path: parent_yaml, child_path: child_path.read_text()}
        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = self._file_router(files)
            child_load = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=child_path))
        assert isinstance(child_load, LoadProjectTemplateResultSuccess)
        assert child_load.template.directories["outputs"].description is None

    @pytest.mark.asyncio
    async def test_save_with_parent_omits_inherited_situation(self, pm: ProjectManager, tmp_path: Path) -> None:
        """A child situation matching the parent's must be omitted."""
        from griptape_nodes.retained_mode.events.project_events import (
            SaveProjectTemplateRequest,
            SaveProjectTemplateResultSuccess,
        )

        parent_yaml = """\
project_template_schema_version: "0.3.2"
name: With Situation
situations:
  save_node_output:
    macro: "{outputs}/{node_name}.{file_extension}"
    policy:
      on_collision: overwrite
      create_dirs: true
    fallback: null
    description: "Custom save"
directories: {}
"""
        parent_path = (tmp_path / "parent.yml").resolve()
        await self._load_parent(pm, parent_path, parent_yaml)

        child_path = tmp_path / "child.yml"
        template_data = {
            "project_template_schema_version": "0.3.2",
            "name": "child",
            "parent_project_path": str(parent_path),
            "situations": {
                "save_node_output": {
                    "name": "save_node_output",
                    "macro": "{outputs}/{node_name}.{file_extension}",
                    "policy": {"on_collision": "overwrite", "create_dirs": True},
                    "fallback": None,
                    "description": "Custom save",
                },
            },
            "directories": {},
        }
        result = pm.on_save_project_template_request(
            SaveProjectTemplateRequest(project_path=child_path, template_data=template_data)
        )
        assert isinstance(result, SaveProjectTemplateResultSuccess)
        parsed = self._parse_yaml(child_path.read_text())
        assert "situations" not in parsed or "save_node_output" not in (parsed.get("situations") or {})

    @pytest.mark.asyncio
    async def test_save_with_parent_omits_inherited_environment(self, pm: ProjectManager, tmp_path: Path) -> None:
        """An environment entry that matches the parent's must be omitted."""
        from griptape_nodes.retained_mode.events.project_events import (
            SaveProjectTemplateRequest,
            SaveProjectTemplateResultSuccess,
        )

        parent_yaml = """\
project_template_schema_version: "0.3.2"
name: With Env
situations: {}
directories: {}
environment:
  FOO: "bar"
"""
        parent_path = (tmp_path / "parent.yml").resolve()
        await self._load_parent(pm, parent_path, parent_yaml)

        child_path = tmp_path / "child.yml"
        template_data = {
            "project_template_schema_version": "0.3.2",
            "name": "child",
            "parent_project_path": str(parent_path),
            "situations": {},
            "directories": {},
            "environment": {"FOO": "bar"},
        }
        result = pm.on_save_project_template_request(
            SaveProjectTemplateRequest(project_path=child_path, template_data=template_data)
        )
        assert isinstance(result, SaveProjectTemplateResultSuccess)
        parsed = self._parse_yaml(child_path.read_text())
        assert "environment" not in parsed or "FOO" not in (parsed.get("environment") or {})

    @pytest.mark.asyncio
    async def test_save_with_parent_emits_tombstone_for_dropped_directory(
        self, pm: ProjectManager, tmp_path: Path
    ) -> None:
        """A directory present on the parent but absent from the child must be tombstoned."""
        from griptape_nodes.retained_mode.events.project_events import (
            SaveProjectTemplateRequest,
            SaveProjectTemplateResultSuccess,
        )

        parent_yaml = """\
project_template_schema_version: "0.3.2"
name: With Scratch
directories:
  scratch:
    path_macro: "scratch_dir"
"""
        parent_path = (tmp_path / "parent.yml").resolve()
        await self._load_parent(pm, parent_path, parent_yaml)

        child_path = tmp_path / "child.yml"
        template_data = {
            "project_template_schema_version": "0.3.2",
            "name": "child",
            "parent_project_path": str(parent_path),
            "situations": {},
            "directories": {},  # `scratch` deliberately dropped
        }
        result = pm.on_save_project_template_request(
            SaveProjectTemplateRequest(project_path=child_path, template_data=template_data)
        )
        assert isinstance(result, SaveProjectTemplateResultSuccess)
        parsed = self._parse_yaml(child_path.read_text())
        assert parsed["directories"]["scratch"] is None

    def test_save_with_parent_not_in_registry_fails(self, pm: ProjectManager, tmp_path: Path) -> None:
        """If the parent isn't loaded, save must fail loudly rather than fall through to defaults."""
        from griptape_nodes.retained_mode.events.project_events import (
            SaveProjectTemplateRequest,
            SaveProjectTemplateResultFailure,
        )

        unloaded_parent = (tmp_path / "ghost.yml").resolve()
        child_path = tmp_path / "child.yml"
        template_data = {
            "project_template_schema_version": "0.3.2",
            "name": "child",
            "parent_project_path": str(unloaded_parent),
            "situations": {},
            "directories": {},
        }
        result = pm.on_save_project_template_request(
            SaveProjectTemplateRequest(project_path=child_path, template_data=template_data)
        )
        assert isinstance(result, SaveProjectTemplateResultFailure)
        details = str(result.result_details)
        assert str(unloaded_parent) in details
        assert "is not loaded" in details
        # File must not have been written.
        assert not child_path.exists()

    @pytest.mark.asyncio
    async def test_save_with_relative_parent_path(self, pm: ProjectManager, tmp_path: Path) -> None:
        """`parent_project_path` of `./parent.yml` must resolve against the child's directory."""
        from griptape_nodes.retained_mode.events.project_events import (
            SaveProjectTemplateRequest,
            SaveProjectTemplateResultSuccess,
        )

        parent_path = (tmp_path / "parent.yml").resolve()
        await self._load_parent(pm, parent_path, self.BASE_PARENT_YAML)

        child_path = tmp_path / "child.yml"
        template_data = {
            "project_template_schema_version": "0.3.2",
            "name": "child",
            "parent_project_path": "./parent.yml",
            "situations": {},
            "directories": {
                "outputs": {
                    "name": "outputs",
                    "path_macro": "outputs2",
                    "description": self.BASE_PARENT_OUTPUTS_DESCRIPTION,
                },  # inherited
            },
        }
        result = pm.on_save_project_template_request(
            SaveProjectTemplateRequest(project_path=child_path, template_data=template_data)
        )
        assert isinstance(result, SaveProjectTemplateResultSuccess)
        parsed = self._parse_yaml(child_path.read_text())
        assert "directories" not in parsed or "outputs" not in (parsed.get("directories") or {})

    @pytest.mark.asyncio
    async def test_save_grandchild_diffs_against_parent_merged_chain(self, pm: ProjectManager, tmp_path: Path) -> None:
        """Grandchild's diff base is the parent's *fully-merged* template (which carries grandparent values)."""
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
            SaveProjectTemplateRequest,
            SaveProjectTemplateResultSuccess,
        )

        grandparent_path = (tmp_path / "grandparent.yml").resolve()
        parent_path = (tmp_path / "parent.yml").resolve()

        grandparent_yaml = self.BASE_PARENT_YAML
        # Parent inherits `outputs: outputs2` silently from grandparent.
        parent_yaml = f"""\
project_template_schema_version: "0.3.2"
name: Middle Parent
parent_project_path: "{grandparent_path.as_posix()}"
"""

        files = {
            grandparent_path: grandparent_yaml,
            parent_path: parent_yaml,
        }
        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = self._file_router(files)
            gp_load = await pm.on_load_project_template_request(
                LoadProjectTemplateRequest(project_path=grandparent_path)
            )
            p_load = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=parent_path))
        assert isinstance(gp_load, LoadProjectTemplateResultSuccess)
        assert isinstance(p_load, LoadProjectTemplateResultSuccess)

        grandchild_path = tmp_path / "grandchild.yml"
        template_data = {
            "project_template_schema_version": "0.3.2",
            "name": "grandchild",
            "parent_project_path": str(parent_path),
            "situations": {},
            "directories": {
                "outputs": {
                    "name": "outputs",
                    "path_macro": "outputs2",
                    "description": self.BASE_PARENT_OUTPUTS_DESCRIPTION,
                },  # inherited transitively
            },
        }
        result = pm.on_save_project_template_request(
            SaveProjectTemplateRequest(project_path=grandchild_path, template_data=template_data)
        )
        assert isinstance(result, SaveProjectTemplateResultSuccess)
        parsed = self._parse_yaml(grandchild_path.read_text())
        # Diff base is parent's merged template, which already provides outputs2 from grandparent.
        assert "directories" not in parsed or "outputs" not in (parsed.get("directories") or {})


class TestValidateProjectTemplateParentChain:
    """Tests for ValidateProjectTemplateRequest's parent-chain checks.

    The validator is path-less and consults only the in-memory template registry,
    so these tests seed `_successfully_loaded_project_templates` directly rather
    than driving disk loads.
    """

    @pytest.fixture
    def pm(self) -> ProjectManager:
        mock_event_manager = Mock()
        mock_config_manager = Mock()
        mock_config_manager.project_config = {}
        mock_config_manager.env_config = {}
        mock_config_manager.merged_config = {}
        mock_config_manager.get_config_value.return_value = []
        return ProjectManager(mock_event_manager, mock_config_manager, Mock())

    @staticmethod
    def _seed_registered_template(pm: ProjectManager, project_id: str, parent_project_path: str | None) -> str:
        r"""Insert a minimal ProjectInfo into the registry for chain-walking purposes.

        Returns the canonical registry key actually used so callers (and the
        edited template's `parent_project_path`) can reference it. The
        manager's chain-walk canonicalizes all parent path lookups, so on
        Windows raw POSIX-style ids like "/a.yml" are normalized to
        "C:\a.yml" before lookup. Mirroring that here keeps the test cross
        -platform.
        """
        from griptape_nodes.common.project_templates import (
            ProjectTemplate,
            ProjectValidationInfo,
            ProjectValidationStatus,
        )
        from griptape_nodes.retained_mode.managers.project_manager import ProjectInfo

        canonical_id = str(canonicalize_for_identity(Path(project_id)))
        canonical_parent: str | None = None
        if parent_project_path is not None:
            canonical_parent = str(canonicalize_for_identity(Path(parent_project_path)))
        template = ProjectTemplate(
            project_template_schema_version="0.3.2",
            name=canonical_id,
            parent_project_path=canonical_parent,
            situations={},
            directories={},
        )
        pm._successfully_loaded_project_templates[canonical_id] = ProjectInfo(
            project_id=canonical_id,
            project_file_path=Path(canonical_id),
            project_base_dir=Path(canonical_id).parent,
            template=template,
            validation=ProjectValidationInfo(status=ProjectValidationStatus.GOOD),
            parsed_situation_schemas={},
            parsed_directory_schemas={},
        )
        return canonical_id

    def test_no_parent_passes(self, pm: ProjectManager) -> None:
        """A template with parent_project_path == None is valid."""
        from griptape_nodes.retained_mode.events.project_events import (
            ValidateProjectTemplateRequest,
            ValidateProjectTemplateResultSuccess,
        )

        template_data = {
            "project_template_schema_version": "0.3.2",
            "name": "child",
            "parent_project_path": None,
            "situations": {},
            "directories": {},
        }
        result = pm.on_validate_project_template_request(ValidateProjectTemplateRequest(template_data=template_data))
        assert isinstance(result, ValidateProjectTemplateResultSuccess)
        assert not any(p.field_path == "parent_project_path" for p in result.validation.problems)

    def test_parent_not_registered_passes(self, pm: ProjectManager) -> None:
        """A parent_project_path that isn't in the registry yet is silently allowed.

        Load will catch a truly missing parent. We don't want validate to fail
        on first-time edits where the user just hasn't registered the parent.
        """
        from griptape_nodes.retained_mode.events.project_events import (
            ValidateProjectTemplateRequest,
            ValidateProjectTemplateResultSuccess,
        )

        template_data = {
            "project_template_schema_version": "0.3.2",
            "name": "child",
            "parent_project_path": "/some/unregistered/parent.yml",
            "situations": {},
            "directories": {},
        }
        result = pm.on_validate_project_template_request(ValidateProjectTemplateRequest(template_data=template_data))
        assert isinstance(result, ValidateProjectTemplateResultSuccess)
        assert not any(p.field_path == "parent_project_path" for p in result.validation.problems)

    def test_deep_chain_in_registry_passes(self, pm: ProjectManager) -> None:
        """A -> B -> C with no cycle is valid even when all three are registered."""
        from griptape_nodes.retained_mode.events.project_events import (
            ValidateProjectTemplateRequest,
            ValidateProjectTemplateResultSuccess,
        )

        c_id = self._seed_registered_template(pm, "/c.yml", parent_project_path=None)
        b_id = self._seed_registered_template(pm, "/b.yml", parent_project_path=c_id)

        template_data = {
            "project_template_schema_version": "0.3.2",
            "name": "a",
            "parent_project_path": b_id,
            "situations": {},
            "directories": {},
        }
        result = pm.on_validate_project_template_request(ValidateProjectTemplateRequest(template_data=template_data))
        assert isinstance(result, ValidateProjectTemplateResultSuccess)
        assert not any(p.field_path == "parent_project_path" for p in result.validation.problems)

    def test_cycle_in_registry_detected(self, pm: ProjectManager) -> None:
        """If the user picks parent B whose chain points back to A, validate fails."""
        from griptape_nodes.retained_mode.events.project_events import (
            ValidateProjectTemplateRequest,
            ValidateProjectTemplateResultSuccess,
        )

        # Registry already has B -> A. User now edits A and picks B as parent.
        a_id = self._seed_registered_template(pm, "/a.yml", parent_project_path=None)
        b_id = self._seed_registered_template(pm, "/b.yml", parent_project_path=a_id)

        template_data = {
            "project_template_schema_version": "0.3.2",
            "name": "a edited",
            "parent_project_path": b_id,
            "situations": {},
            "directories": {},
        }
        # Pass project_id so validator seeds visited set with self.
        result = pm.on_validate_project_template_request(
            ValidateProjectTemplateRequest(template_data=template_data, project_id=a_id)
        )
        assert isinstance(result, ValidateProjectTemplateResultSuccess)
        assert any(p.field_path == "parent_project_path" and "Cycle" in p.message for p in result.validation.problems)


class TestPerPlatformProjectsToRegister:
    """`projects_to_register` accepts per-platform mappings; behavior matches plain strings on the active platform."""

    VALID_PROJECT_YAML = """\
project_template_schema_version: "0.1.0"
name: Per-Platform Project
situations:
  save_node_output:
    macro: "{outputs}/{file_name_base}.{file_extension}"
    policy:
      on_collision: create_new
      create_dirs: true
"""

    @pytest.fixture
    def pm(self) -> ProjectManager:
        mock_event_manager = Mock()
        mock_config_manager = Mock()
        mock_config_manager.project_config = {}
        mock_config_manager.env_config = {}
        mock_config_manager.merged_config = {}
        mock_config_manager.get_config_value.return_value = []
        return ProjectManager(mock_event_manager, mock_config_manager, Mock())

    @pytest.mark.asyncio
    async def test_per_platform_entry_loads_active_platform_path(
        self, pm: ProjectManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A per-platform mapping resolves to the active OS's path and is loaded."""
        from griptape_nodes.retained_mode.events.os_events import ReadFileResultSuccess
        from griptape_nodes.retained_mode.managers.settings import PROJECTS_TO_REGISTER_KEY

        monkeypatch.setattr("sys.platform", "darwin")
        active_path = tmp_path / "darwin_project.yml"
        other_path = tmp_path / "linux_project.yml"
        entry = {
            "darwin": str(active_path),
            "linux": str(other_path),
            "windows": "Z:\\unused.yml",
        }

        def get_config_value_side_effect(key: str, **_: object) -> object:
            if key == PROJECTS_TO_REGISTER_KEY:
                return [entry]
            return []

        cast("Mock", pm._config_manager).get_config_value.side_effect = get_config_value_side_effect

        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = AsyncMock(
                return_value=ReadFileResultSuccess(
                    content=self.VALID_PROJECT_YAML,
                    file_size=len(self.VALID_PROJECT_YAML),
                    mime_type="text/plain",
                    encoding="utf-8",
                    result_details="ok",
                )
            )

            await pm._load_registered_projects()

        assert str(active_path) in pm._successfully_loaded_project_templates
        assert str(other_path) not in pm._successfully_loaded_project_templates

    @pytest.mark.asyncio
    async def test_per_platform_entry_falls_back_to_default(
        self, pm: ProjectManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When no platform key matches, `default` is used."""
        from griptape_nodes.retained_mode.events.os_events import ReadFileResultSuccess
        from griptape_nodes.retained_mode.managers.settings import PROJECTS_TO_REGISTER_KEY

        monkeypatch.setattr("sys.platform", "linux")
        default_path = tmp_path / "default_project.yml"
        entry = {
            "darwin": "/Volumes/unused.yml",
            "default": str(default_path),
        }

        def get_config_value_side_effect(key: str, **_: object) -> object:
            if key == PROJECTS_TO_REGISTER_KEY:
                return [entry]
            return []

        cast("Mock", pm._config_manager).get_config_value.side_effect = get_config_value_side_effect

        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = AsyncMock(
                return_value=ReadFileResultSuccess(
                    content=self.VALID_PROJECT_YAML,
                    file_size=len(self.VALID_PROJECT_YAML),
                    mime_type="text/plain",
                    encoding="utf-8",
                    result_details="ok",
                )
            )

            await pm._load_registered_projects()

        assert str(default_path) in pm._successfully_loaded_project_templates

    @pytest.mark.asyncio
    async def test_per_platform_entry_no_match_no_default_skips_with_warning(
        self,
        pm: ProjectManager,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A per-platform entry with no key for the active OS and no `default` is skipped + logged."""
        from griptape_nodes.retained_mode.managers.settings import PROJECTS_TO_REGISTER_KEY

        # Patch the per-platform selector directly rather than `sys.platform`. The skip
        # path emits a `logger.warning(...)`, which on first call lazily constructs the
        # GriptapeNodes singleton; that init reads the *real* `sys.platform` to set up
        # OS-specific resources, and falls into `os.uname()` on Linux/Mac branches.
        # On a Windows CI host with `sys.platform` faked to "linux", that crashes.
        monkeypatch.setattr(
            "griptape_nodes.common.project_templates.directory._active_platform_key",
            lambda: "linux",
        )
        entry = {
            "darwin": "/Volumes/unused.yml",
            "windows": "Z:\\unused.yml",
        }

        def get_config_value_side_effect(key: str, **_: object) -> object:
            if key == PROJECTS_TO_REGISTER_KEY:
                return [entry]
            return []

        cast("Mock", pm._config_manager).get_config_value.side_effect = get_config_value_side_effect

        with (
            patch.object(pm, "on_load_project_template_request", new=AsyncMock()) as mock_load,
            caplog.at_level(logging.WARNING, logger="griptape_nodes"),
        ):
            await pm._load_registered_projects()
            mock_load.assert_not_called()

        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("no key for the active platform" in msg for msg in warning_messages)

    @pytest.mark.asyncio
    async def test_invalid_per_platform_entry_skipped_with_warning(
        self, pm: ProjectManager, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A dict with unknown keys (e.g., `osx`) fails validation and is skipped + logged."""
        from griptape_nodes.retained_mode.managers.settings import PROJECTS_TO_REGISTER_KEY

        entry = {"osx": "/Volumes/unused.yml"}

        def get_config_value_side_effect(key: str, **_: object) -> object:
            if key == PROJECTS_TO_REGISTER_KEY:
                return [entry]
            return []

        cast("Mock", pm._config_manager).get_config_value.side_effect = get_config_value_side_effect

        with (
            patch.object(pm, "on_load_project_template_request", new=AsyncMock()) as mock_load,
            caplog.at_level(logging.WARNING, logger="griptape_nodes"),
        ):
            await pm._load_registered_projects()
            mock_load.assert_not_called()

        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("invalid per-platform projects_to_register entry" in msg for msg in warning_messages)

    @pytest.mark.asyncio
    async def test_plain_string_entry_still_loads(self, pm: ProjectManager, tmp_path: Path) -> None:
        """Regression: plain-string entries continue to load (no per-platform handling needed)."""
        from griptape_nodes.retained_mode.events.os_events import ReadFileResultSuccess
        from griptape_nodes.retained_mode.managers.settings import PROJECTS_TO_REGISTER_KEY

        project_path = tmp_path / "string_project.yml"

        def get_config_value_side_effect(key: str, **_: object) -> object:
            if key == PROJECTS_TO_REGISTER_KEY:
                return [str(project_path)]
            return []

        cast("Mock", pm._config_manager).get_config_value.side_effect = get_config_value_side_effect

        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = AsyncMock(
                return_value=ReadFileResultSuccess(
                    content=self.VALID_PROJECT_YAML,
                    file_size=len(self.VALID_PROJECT_YAML),
                    mime_type="text/plain",
                    encoding="utf-8",
                    result_details="ok",
                )
            )

            await pm._load_registered_projects()

        assert str(project_path) in pm._successfully_loaded_project_templates


class TestPerPlatformParentProjectPath:
    """`parent_project_path` accepts per-platform mappings; selection happens at every read site."""

    BASE_PROJECT_YAML = """\
project_template_schema_version: "0.3.3"
name: Base Parent
directories:
  shared_outputs:
    path_macro: "{workspace_dir}/base_outputs"
"""

    @pytest.fixture
    def pm(self, tmp_path: Path) -> ProjectManager:
        mock_event_manager = Mock()
        mock_config_manager = Mock()
        mock_config_manager.project_config = {}
        mock_config_manager.env_config = {}
        mock_config_manager.merged_config = {}
        mock_config_manager.get_config_value.return_value = []
        mock_config_manager.read_config_file_value.return_value = None
        mock_config_manager.workspace_path = tmp_path
        return ProjectManager(mock_event_manager, mock_config_manager, Mock())

    @staticmethod
    def _file_router(files: dict[Path, str]) -> AsyncMock:
        """Build an `ahandle_request` mock that returns YAML content based on file path."""
        from griptape_nodes.retained_mode.events.os_events import (
            FileIOFailureReason,
            ReadFileResultFailure,
            ReadFileResultSuccess,
        )

        async def route(request: object) -> object:
            file_path = Path(getattr(request, "file_path", ""))
            content = files.get(file_path)
            if content is None:
                return ReadFileResultFailure(
                    failure_reason=FileIOFailureReason.FILE_NOT_FOUND,
                    result_details=f"missing: {file_path}",
                )
            return ReadFileResultSuccess(
                content=content,
                file_size=len(content),
                mime_type="text/plain",
                encoding="utf-8",
                result_details="ok",
            )

        return AsyncMock(side_effect=route)

    @pytest.mark.asyncio
    async def test_load_child_with_per_platform_parent_resolves_active_platform(
        self, pm: ProjectManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A child storing parent_project_path as a per-platform mapping resolves correctly at load time."""
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )

        monkeypatch.setattr("sys.platform", "darwin")
        base_path = (tmp_path / "base.yml").resolve()
        child_path = (tmp_path / "child.yml").resolve()
        child_yaml = (
            'project_template_schema_version: "0.3.3"\n'
            "name: Child\n"
            "parent_project_path:\n"
            f'  darwin: "{base_path.as_posix()}"\n'
            '  linux: "/mnt/unused.yml"\n'
            "directories:\n"
            "  child_outputs:\n"
            '    path_macro: "{workspace_dir}/child_outputs"\n'
        )
        files = {
            base_path: self.BASE_PROJECT_YAML,
            child_path: child_yaml,
        }

        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = self._file_router(files)
            result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=child_path))

        assert isinstance(result, LoadProjectTemplateResultSuccess)
        assert "shared_outputs" in result.template.directories
        assert "child_outputs" in result.template.directories

    @pytest.mark.asyncio
    async def test_load_child_with_per_platform_parent_no_match_treats_as_no_parent(
        self, pm: ProjectManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When no platform key matches and no default, the child loads against system defaults instead."""
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )

        monkeypatch.setattr("sys.platform", "linux")
        child_path = (tmp_path / "child.yml").resolve()
        child_yaml = (
            'project_template_schema_version: "0.3.3"\n'
            "name: Child\n"
            "parent_project_path:\n"
            '  darwin: "/Volumes/unused.yml"\n'
            '  windows: "Z:\\\\unused.yml"\n'
            "directories:\n"
            "  child_outputs:\n"
            '    path_macro: "{workspace_dir}/child_outputs"\n'
        )
        files = {child_path: child_yaml}

        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = self._file_router(files)
            result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=child_path))

        assert isinstance(result, LoadProjectTemplateResultSuccess)
        # Child's own directory survives.
        assert "child_outputs" in result.template.directories
        # System default directories present (default fallback applied).
        assert "outputs" in result.template.directories

    @pytest.mark.asyncio
    async def test_load_child_with_per_platform_parent_falls_back_to_default(
        self, pm: ProjectManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A `default` key on the per-platform parent is consulted when the active OS key is missing."""
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )

        monkeypatch.setattr("sys.platform", "linux")
        base_path = (tmp_path / "base.yml").resolve()
        child_path = (tmp_path / "child.yml").resolve()
        child_yaml = (
            'project_template_schema_version: "0.3.3"\n'
            "name: Child\n"
            "parent_project_path:\n"
            '  darwin: "/Volumes/unused.yml"\n'
            f'  default: "{base_path.as_posix()}"\n'
            "directories: {}\n"
        )
        files = {base_path: self.BASE_PROJECT_YAML, child_path: child_yaml}

        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = self._file_router(files)
            result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=child_path))

        assert isinstance(result, LoadProjectTemplateResultSuccess)
        assert "shared_outputs" in result.template.directories

    @pytest.mark.asyncio
    async def test_per_platform_parent_round_trips_through_in_memory_template(
        self, pm: ProjectManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The merged template preserves the per-platform mapping (not flattened to active-platform string)."""
        from griptape_nodes.common.project_templates import PerPlatformProjectPath
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )

        monkeypatch.setattr("sys.platform", "darwin")
        base_path = (tmp_path / "base.yml").resolve()
        child_path = (tmp_path / "child.yml").resolve()
        child_yaml = (
            'project_template_schema_version: "0.3.3"\n'
            "name: Child\n"
            "parent_project_path:\n"
            f'  darwin: "{base_path.as_posix()}"\n'
            '  linux: "/mnt/base.yml"\n'
            "directories: {}\n"
        )
        files = {base_path: self.BASE_PROJECT_YAML, child_path: child_yaml}

        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = self._file_router(files)
            result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=child_path))

        assert isinstance(result, LoadProjectTemplateResultSuccess)
        # The in-memory `parent_project_path` is the union object, not a single string.
        assert isinstance(result.template.parent_project_path, PerPlatformProjectPath)
        assert result.template.parent_project_path.darwin == base_path.as_posix()
        assert result.template.parent_project_path.linux == "/mnt/base.yml"


class TestSnapshotLibraryConfig:
    """`_snapshot_library_config` powers conditional reload: same config skips the deep reset."""

    @staticmethod
    def _pm_reading(values: dict[str, object], *, workspace_path: str = "/workspace") -> ProjectManager:
        mock_config_manager = Mock()
        mock_config_manager.get_config_value.side_effect = lambda key, default=None: values.get(key, default)
        mock_config_manager.workspace_path = Path(workspace_path)
        return ProjectManager(Mock(), mock_config_manager, Mock())

    def test_identical_config_snapshots_are_equal(self) -> None:
        from griptape_nodes.retained_mode.managers.settings import (
            LIBRARIES_TO_REGISTER_KEY,
            REQUIRES_ENGINE_KEY,
        )

        pm = self._pm_reading({LIBRARIES_TO_REGISTER_KEY: ["a", "b"], REQUIRES_ENGINE_KEY: ">=0.5,<0.6"})

        assert pm._snapshot_library_config() == pm._snapshot_library_config()

    def test_changed_library_list_changes_snapshot(self) -> None:
        from griptape_nodes.retained_mode.managers.settings import LIBRARIES_TO_REGISTER_KEY

        before = self._pm_reading({LIBRARIES_TO_REGISTER_KEY: ["a"]})._snapshot_library_config()
        after = self._pm_reading({LIBRARIES_TO_REGISTER_KEY: ["a", "b"]})._snapshot_library_config()

        assert before != after

    def test_requires_engine_only_change_changes_snapshot(self) -> None:
        from griptape_nodes.retained_mode.managers.settings import REQUIRES_ENGINE_KEY

        before = self._pm_reading({REQUIRES_ENGINE_KEY: ">=0.5,<0.6"})._snapshot_library_config()
        after = self._pm_reading({REQUIRES_ENGINE_KEY: ">=0.6,<0.7"})._snapshot_library_config()

        assert before != after

    def test_workspace_only_change_changes_snapshot(self) -> None:
        # libraries_directory is workspace-relative, so two projects with identical
        # config strings but different workspaces resolve to different on-disk
        # libraries/ trees and must still reload.
        values: dict[str, object] = {"libraries_directory": "libraries"}
        before = self._pm_reading(values, workspace_path="/ws-a")._snapshot_library_config()
        after = self._pm_reading(values, workspace_path="/ws-b")._snapshot_library_config()

        assert before != after


class TestActivateWorkspaceProject:
    """`on_activate_workspace_project_request`: the app's boot-time pre-activation seam."""

    VALID_PROJECT_YAML = """\
project_template_schema_version: "0.1.0"
name: Workspace Project
situations:
  save_node_output:
    macro: "{outputs}/custom/{file_name_base}.{file_extension}"
    policy:
      on_collision: create_new
      create_dirs: true
"""

    @pytest.fixture
    def pm(self) -> ProjectManager:
        mock_event_manager = Mock()
        mock_config_manager = Mock()
        mock_config_manager.project_config = {}
        mock_config_manager.env_config = {}
        mock_config_manager.merged_config = {}
        mock_config_manager.get_config_value.return_value = {}
        return ProjectManager(mock_event_manager, mock_config_manager, Mock())

    def _setup_system_defaults(self, pm: ProjectManager, workspace_dir: str = "/workspace") -> None:
        from griptape_nodes.common.project_templates import ProjectValidationInfo, ProjectValidationStatus
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY, ProjectInfo

        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        situation_schemas = pm._parse_situation_macros(DEFAULT_PROJECT_TEMPLATE.situations, validation)
        directory_schemas = pm._parse_directory_macros(DEFAULT_PROJECT_TEMPLATE.directories, validation)

        project_info = ProjectInfo(
            project_id=SYSTEM_DEFAULTS_KEY,
            project_file_path=None,
            project_base_dir=Path(workspace_dir),
            template=DEFAULT_PROJECT_TEMPLATE,
            validation=validation,
            parsed_situation_schemas=situation_schemas,
            parsed_directory_schemas=directory_schemas,
        )
        pm._successfully_loaded_project_templates[SYSTEM_DEFAULTS_KEY] = project_info
        pm._current_project_id = SYSTEM_DEFAULTS_KEY

    @pytest.mark.asyncio
    async def test_no_workspace_project_is_success_noop(self, pm: ProjectManager, tmp_path: Path) -> None:
        """No resolvable project file: Success, system defaults stay active (a no-op is not a failure)."""
        from griptape_nodes.retained_mode.events.project_events import (
            ActivateWorkspaceProjectRequest,
            ActivateWorkspaceProjectResultSuccess,
        )
        from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY

        self._setup_system_defaults(pm, str(tmp_path))

        def get_config_value_side_effect(key: str, **_: object) -> str | dict | None:
            if key == "project_file":
                return None
            if "project_workspaces" in key:
                return {}
            return str(tmp_path)

        cast("Mock", pm._config_manager).get_config_value.side_effect = get_config_value_side_effect
        cast("Mock", pm._config_manager).workspace_path = tmp_path

        result = await pm.on_activate_workspace_project_request(ActivateWorkspaceProjectRequest())

        assert isinstance(result, ActivateWorkspaceProjectResultSuccess)
        assert pm._current_project_id == SYSTEM_DEFAULTS_KEY

    @pytest.mark.asyncio
    async def test_resolvable_workspace_project_activates(self, pm: ProjectManager, tmp_path: Path) -> None:
        """A resolvable workspace project is activated and returns Success."""
        from griptape_nodes.retained_mode.events.project_events import (
            ActivateWorkspaceProjectRequest,
            ActivateWorkspaceProjectResultSuccess,
        )
        from griptape_nodes.retained_mode.managers.project_manager import WORKSPACE_PROJECT_FILE

        self._setup_system_defaults(pm, str(tmp_path))

        workspace_project_path = tmp_path / WORKSPACE_PROJECT_FILE
        workspace_project_path.write_text(self.VALID_PROJECT_YAML)

        def get_config_value_side_effect(key: str, **_: object) -> str | dict | None:
            if key == "project_file":
                return None
            if "project_workspaces" in key:
                return {}
            return str(tmp_path)

        cast("Mock", pm._config_manager).get_config_value.side_effect = get_config_value_side_effect
        cast("Mock", pm._config_manager).workspace_path = tmp_path

        with patch("griptape_nodes.retained_mode.managers.project_manager.File") as mock_file_cls:
            mock_file_instance = Mock()
            mock_file_instance.aread_text = AsyncMock(return_value=self.VALID_PROJECT_YAML)
            mock_file_cls.return_value = mock_file_instance

            result = await pm.on_activate_workspace_project_request(ActivateWorkspaceProjectRequest())

        assert isinstance(result, ActivateWorkspaceProjectResultSuccess)
        assert pm._current_project_id == str(workspace_project_path)

    @pytest.mark.asyncio
    async def test_unloadable_workspace_project_is_failure(self, pm: ProjectManager, tmp_path: Path) -> None:
        """A project file that resolves but fails to load returns Failure (still on system defaults).

        The seed loads through _load_and_cache_project_template (ReadFileRequest). A directory
        at the seed path passes the existence check but fails the file read, so activation
        never takes and the handler returns Failure.
        """
        from griptape_nodes.retained_mode.events.project_events import (
            ActivateWorkspaceProjectRequest,
            ActivateWorkspaceProjectResultFailure,
        )
        from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY, WORKSPACE_PROJECT_FILE

        self._setup_system_defaults(pm, str(tmp_path))

        # A directory at the seed path exists (path resolves) but cannot be read as a file.
        workspace_project_path = tmp_path / WORKSPACE_PROJECT_FILE
        workspace_project_path.mkdir()

        def get_config_value_side_effect(key: str, **_: object) -> str | dict | None:
            if key == "project_file":
                return None
            if "project_workspaces" in key:
                return {}
            return str(tmp_path)

        cast("Mock", pm._config_manager).get_config_value.side_effect = get_config_value_side_effect
        cast("Mock", pm._config_manager).workspace_path = tmp_path

        result = await pm.on_activate_workspace_project_request(ActivateWorkspaceProjectRequest())

        assert isinstance(result, ActivateWorkspaceProjectResultFailure)
        assert pm._current_project_id == SYSTEM_DEFAULTS_KEY

    @pytest.mark.asyncio
    async def test_malformed_yaml_workspace_project_is_failure_with_detail(
        self, pm: ProjectManager, tmp_path: Path
    ) -> None:
        """A project file whose YAML can't be parsed returns Failure carrying the detail, never raises.

        Exercises the reworked failure signaling: _load_workspace_project returns a detail
        string (rather than the handler inferring failure from a current-project read-back),
        and the handler surfaces it in the result_details. The read-back inference is gone,
        so this path must not depend on _current_project_id staying on system defaults.
        """
        from griptape_nodes.retained_mode.events.project_events import (
            ActivateWorkspaceProjectRequest,
            ActivateWorkspaceProjectResultFailure,
        )
        from griptape_nodes.retained_mode.managers.project_manager import WORKSPACE_PROJECT_FILE

        self._setup_system_defaults(pm, str(tmp_path))

        # Malformed YAML on disk: the delegated loader reads it via ReadFileRequest and
        # fails to parse it, so the handler surfaces the failure detail.
        workspace_project_path = tmp_path / WORKSPACE_PROJECT_FILE
        workspace_project_path.write_text("not: valid: yaml: : :\n  - broken")

        def get_config_value_side_effect(key: str, **_: object) -> str | dict | None:
            if key == "project_file":
                return None
            if "project_workspaces" in key:
                return {}
            return str(tmp_path)

        cast("Mock", pm._config_manager).get_config_value.side_effect = get_config_value_side_effect
        cast("Mock", pm._config_manager).workspace_path = tmp_path

        result = await pm.on_activate_workspace_project_request(ActivateWorkspaceProjectRequest())

        assert isinstance(result, ActivateWorkspaceProjectResultFailure)
        assert str(workspace_project_path) in str(result.result_details)
        assert "Failed because" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_activate_child_seed_resolves_id_parent_registered_only_in_config(
        self, pm: ProjectManager, tmp_path: Path
    ) -> None:
        """The app-orchestrator seam resolves a child seed's id-parent that is only registered.

        This is the seam GUI boot drives (on_activate_workspace_project_request runs before
        _load_registered_projects, so the live registry is empty). The seed's parent chain
        must still resolve via the boot id-index that _load_workspace_project builds, so a
        child whose parent lives only in projects_to_register inherits the parent's
        directories on boot rather than silently dropping them.
        """
        from griptape_nodes.retained_mode.events.project_events import (
            ActivateWorkspaceProjectRequest,
            ActivateWorkspaceProjectResultSuccess,
        )
        from griptape_nodes.retained_mode.managers.project_manager import WORKSPACE_PROJECT_FILE
        from griptape_nodes.retained_mode.managers.settings import PROJECTS_TO_REGISTER_KEY

        self._setup_system_defaults(pm, str(tmp_path))

        parent_dir = tmp_path / "parent"
        parent_dir.mkdir()
        parent_path = parent_dir / "griptape-nodes-project.yml"
        parent_path.write_text(
            "project_template_schema_version: '1.0.0'\n"
            "name: Parent\n"
            "id: parent-abc\n"
            "directories:\n"
            "  prompts:\n"
            "    path_macro: prompts\n"
        )

        # Child is the workspace-dir seed; only the parent is in projects_to_register, so
        # it is NOT in the live registry when the seam runs.
        child_path = tmp_path / WORKSPACE_PROJECT_FILE
        child_path.write_text(
            "project_template_schema_version: '1.0.0'\nname: Child\nid: child-xyz\nparent_project_id: 'parent-abc'\n"
        )

        def get_config_value_side_effect(key: str, **_: object) -> object:
            if key == "project_file":
                return None
            if key == PROJECTS_TO_REGISTER_KEY:
                return [str(parent_path)]
            if "project_workspaces" in key:
                return {}
            return str(tmp_path)

        cast("Mock", pm._config_manager).get_config_value.side_effect = get_config_value_side_effect
        cast("Mock", pm._config_manager).workspace_path = tmp_path
        cast("Mock", pm._config_manager).read_config_file.return_value = {}

        result = await pm.on_activate_workspace_project_request(ActivateWorkspaceProjectRequest())

        assert isinstance(result, ActivateWorkspaceProjectResultSuccess)
        assert pm._current_project_id == "child-xyz"
        child_info = pm._successfully_loaded_project_templates["child-xyz"]
        assert "prompts" in child_info.template.directories
        # The boot id-index is boot-only and cleared once the seed load finishes.
        assert pm._boot_id_to_file_path == {}


class TestProjectId:
    """Tests for opaque project ids (internal#110) and id-based parent links (engine#4806).

    An explicit `id` in the YAML becomes the registry key; a legacy file with no
    id falls back to its canonical path string. Ids are looked up verbatim (never
    canonicalized), and id-based parent links are located through the registry so
    a shared file embeds no machine-specific path.
    """

    EXPLICIT_ID_YAML = """\
project_template_schema_version: "0.3.3"
id: "11111111-2222-3333-4444-555555555555"
name: Explicit Id Project
directories:
  child_outputs:
    path_macro: "{workspace_dir}/child_outputs"
"""

    LEGACY_NO_ID_YAML = """\
project_template_schema_version: "0.3.3"
name: Legacy Project
directories:
  legacy_outputs:
    path_macro: "{workspace_dir}/legacy_outputs"
"""

    PARENT_WITH_ID_YAML = """\
project_template_schema_version: "0.3.3"
id: "parent-id-aaaa"
name: Parent Project
directories:
  shared_outputs:
    path_macro: "{workspace_dir}/shared_outputs"
"""

    CHILD_BY_PARENT_ID_YAML = """\
project_template_schema_version: "0.3.3"
id: "child-id-bbbb"
name: Child Project
parent_project_id: "parent-id-aaaa"
directories:
  child_outputs:
    path_macro: "{workspace_dir}/child_outputs"
"""

    CHILD_MISSING_PARENT_ID_YAML = """\
project_template_schema_version: "0.3.3"
id: "child-id-cccc"
name: Orphan Child
parent_project_id: "ghost-parent-id"
"""

    @pytest.fixture
    def pm(self, tmp_path: Path) -> ProjectManager:
        mock_event_manager = Mock()
        mock_config_manager = Mock()
        mock_config_manager.project_config = {}
        mock_config_manager.env_config = {}
        mock_config_manager.merged_config = {}

        # Return each call's own `default` so per-key types stay correct: a
        # set-current activation reaches _snapshot_library_config, which reads
        # `libraries_directory` as a string (default="") and the library lists as
        # lists. A blanket return_value would feed Path() a list and crash.
        def get_config_value(_key: str, *_args: object, default: object = None, **_kwargs: object) -> object:
            return default

        mock_config_manager.get_config_value.side_effect = get_config_value
        mock_config_manager.workspace_path = tmp_path
        return ProjectManager(mock_event_manager, mock_config_manager, Mock())

    @staticmethod
    def _file_router(files: dict[Path, str]) -> AsyncMock:
        """Build an `ahandle_request` mock that returns YAML by file path.

        Mirrors `TestProjectParentChain._file_router`: unknown paths return a
        FILE_NOT_FOUND failure so the load handler treats them as MISSING.
        """
        from griptape_nodes.retained_mode.events.os_events import (
            FileIOFailureReason,
            ReadFileResultFailure,
            ReadFileResultSuccess,
        )

        async def route(request: object) -> object:
            file_path = Path(getattr(request, "file_path", ""))
            content = files.get(file_path)
            if content is None:
                return ReadFileResultFailure(
                    failure_reason=FileIOFailureReason.FILE_NOT_FOUND,
                    result_details=f"missing: {file_path}",
                )
            return ReadFileResultSuccess(
                content=content,
                file_size=len(content),
                mime_type="text/plain",
                encoding="utf-8",
                result_details="ok",
            )

        return AsyncMock(side_effect=route)

    @pytest.mark.asyncio
    async def test_explicit_id_keys_registry_by_id(self, pm: ProjectManager, tmp_path: Path) -> None:
        """A project declaring an `id` is keyed in the registry by that id, not its path."""
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )

        project_path = (tmp_path / "explicit.yml").resolve()
        files = {project_path: self.EXPLICIT_ID_YAML}
        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = self._file_router(files)
            result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=project_path))

        assert isinstance(result, LoadProjectTemplateResultSuccess)
        explicit_id = "11111111-2222-3333-4444-555555555555"
        assert result.project_id == explicit_id
        assert explicit_id in pm._successfully_loaded_project_templates
        # The path is now only a locator, carried on the ProjectInfo.
        assert pm._successfully_loaded_project_templates[explicit_id].project_file_path == project_path
        # The path string must NOT be a registry key.
        assert str(project_path) not in pm._successfully_loaded_project_templates
        # Status is still tracked by path.
        assert project_path in pm._registered_template_status

    @pytest.mark.asyncio
    async def test_legacy_no_id_keys_registry_by_canonical_path(self, pm: ProjectManager, tmp_path: Path) -> None:
        """A legacy file with no `id` falls back to its canonical path string as the id."""
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )

        project_path = (tmp_path / "legacy.yml").resolve()
        files = {project_path: self.LEGACY_NO_ID_YAML}
        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = self._file_router(files)
            result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=project_path))

        assert isinstance(result, LoadProjectTemplateResultSuccess)
        assert result.project_id == str(project_path)
        assert str(project_path) in pm._successfully_loaded_project_templates

    @pytest.mark.asyncio
    async def test_duplicate_id_across_two_paths_fails_closed(self, pm: ProjectManager, tmp_path: Path) -> None:
        """Two different files with the same id: the second load fails and names both paths."""
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultFailure,
            LoadProjectTemplateResultSuccess,
        )

        path_a = (tmp_path / "a.yml").resolve()
        path_b = (tmp_path / "b.yml").resolve()
        files = {path_a: self.EXPLICIT_ID_YAML, path_b: self.EXPLICIT_ID_YAML}
        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = self._file_router(files)
            first = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=path_a))
            second = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=path_b))

        assert isinstance(first, LoadProjectTemplateResultSuccess)
        assert isinstance(second, LoadProjectTemplateResultFailure)
        # The failure names BOTH files so the user can find the collision.
        assert str(path_a) in str(second.result_details)
        assert str(path_b) in str(second.result_details)
        # The original entry is untouched: it still points at the first file.
        explicit_id = "11111111-2222-3333-4444-555555555555"
        assert pm._successfully_loaded_project_templates[explicit_id].project_file_path == path_a

    @pytest.mark.asyncio
    async def test_same_file_reload_is_no_op_refresh(self, pm: ProjectManager, tmp_path: Path) -> None:
        """Reloading the SAME file (same id, same path) refreshes rather than colliding."""
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )

        project_path = (tmp_path / "explicit.yml").resolve()
        files = {project_path: self.EXPLICIT_ID_YAML}
        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = self._file_router(files)
            first = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=project_path))
            second = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=project_path))

        assert isinstance(first, LoadProjectTemplateResultSuccess)
        assert isinstance(second, LoadProjectTemplateResultSuccess)
        explicit_id = "11111111-2222-3333-4444-555555555555"
        # Still exactly one registry entry for the id.
        assert list(pm._successfully_loaded_project_templates.keys()).count(explicit_id) == 1

    @pytest.mark.asyncio
    async def test_set_current_with_guid_resolves_verbatim(self, pm: ProjectManager, tmp_path: Path) -> None:
        """Regression: a GUID id is looked up verbatim, not canonicalized as a path."""
        from griptape_nodes.common.project_templates import ProjectValidationInfo, ProjectValidationStatus
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.retained_mode.events.project_events import (
            SetCurrentProjectRequest,
            SetCurrentProjectResultSuccess,
        )
        from griptape_nodes.retained_mode.managers.project_manager import ProjectInfo

        guid = "deadbeef-0000-1111-2222-333344445555"
        project_file = tmp_path / "project.yml"
        project_file.touch()
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        situation_schemas = pm._parse_situation_macros(DEFAULT_PROJECT_TEMPLATE.situations, validation)
        directory_schemas = pm._parse_directory_macros(DEFAULT_PROJECT_TEMPLATE.directories, validation)
        pm._successfully_loaded_project_templates[guid] = ProjectInfo(
            project_id=guid,
            project_file_path=project_file,
            project_base_dir=project_file.parent,
            template=DEFAULT_PROJECT_TEMPLATE,
            validation=validation,
            parsed_situation_schemas=situation_schemas,
            parsed_directory_schemas=directory_schemas,
        )
        cast("Mock", pm._config_manager).get_config_value.return_value = {}

        result = await pm.on_set_current_project_request(SetCurrentProjectRequest(project_id=guid))

        assert isinstance(result, SetCurrentProjectResultSuccess)
        assert pm._current_project_id == guid
        # The registry lookup by GUID hit, so the project's config was loaded by its dir.
        cast("Mock", pm._config_manager).load_project_config.assert_called_once_with(project_file.parent)

    @pytest.mark.asyncio
    async def test_set_current_none_resolves_system_defaults(self, pm: ProjectManager) -> None:
        """`project_id=None` normalizes to SYSTEM_DEFAULTS_KEY and stays resolvable."""
        from griptape_nodes.retained_mode.events.project_events import (
            SetCurrentProjectRequest,
            SetCurrentProjectResultSuccess,
        )
        from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY

        assert SYSTEM_DEFAULTS_KEY in pm._successfully_loaded_project_templates

        result = await pm.on_set_current_project_request(SetCurrentProjectRequest(project_id=None))

        assert isinstance(result, SetCurrentProjectResultSuccess)
        assert pm._current_project_id == SYSTEM_DEFAULTS_KEY

    def test_unregister_guid_clears_registry_status_and_persistence(self, pm: ProjectManager, tmp_path: Path) -> None:
        """Unregistering a GUID-id project clears the id-keyed registry, the path-keyed status, and config."""
        from griptape_nodes.common.project_templates import ProjectValidationInfo, ProjectValidationStatus
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.retained_mode.events.project_events import (
            UnregisterProjectTemplateRequest,
            UnregisterProjectTemplateResultSuccess,
        )
        from griptape_nodes.retained_mode.managers.project_manager import ProjectInfo
        from griptape_nodes.retained_mode.managers.settings import PROJECTS_TO_REGISTER_KEY

        guid = "feedface-9999-8888-7777-666655554444"
        project_file = (tmp_path / "project.yml").resolve()
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        situation_schemas = pm._parse_situation_macros(DEFAULT_PROJECT_TEMPLATE.situations, validation)
        directory_schemas = pm._parse_directory_macros(DEFAULT_PROJECT_TEMPLATE.directories, validation)
        pm._successfully_loaded_project_templates[guid] = ProjectInfo(
            project_id=guid,
            project_file_path=project_file,
            project_base_dir=project_file.parent,
            template=DEFAULT_PROJECT_TEMPLATE,
            validation=validation,
            parsed_situation_schemas=situation_schemas,
            parsed_directory_schemas=directory_schemas,
        )
        pm._registered_template_status[project_file] = validation

        captured: dict[str, object] = {}

        def get_config_value_side_effect(key: str, **_: object) -> object:
            if key == PROJECTS_TO_REGISTER_KEY:
                return [str(project_file)]
            return []

        def set_config_value_side_effect(key: str, value: object) -> None:
            captured[key] = value

        cast("Mock", pm._config_manager).get_config_value.side_effect = get_config_value_side_effect
        cast("Mock", pm._config_manager).set_config_value.side_effect = set_config_value_side_effect

        result = pm.on_unregister_project_template_request(UnregisterProjectTemplateRequest(project_id=guid))

        assert isinstance(result, UnregisterProjectTemplateResultSuccess)
        assert guid not in pm._successfully_loaded_project_templates
        assert project_file not in pm._registered_template_status
        # The path was filtered out of the persisted path-list.
        assert captured[PROJECTS_TO_REGISTER_KEY] == []

    @pytest.mark.asyncio
    async def test_load_workspace_project_honors_overlay_id(self, pm: ProjectManager, tmp_path: Path) -> None:
        """_load_workspace_project keys the registry by the YAML's explicit id."""
        from griptape_nodes.common.project_templates import ProjectValidationInfo, ProjectValidationStatus
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.retained_mode.managers.project_manager import (
            SYSTEM_DEFAULTS_KEY,
            WORKSPACE_PROJECT_FILE,
            ProjectInfo,
        )

        # Seed system defaults so the merge base exists.
        defaults_validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        pm._successfully_loaded_project_templates[SYSTEM_DEFAULTS_KEY] = ProjectInfo(
            project_id=SYSTEM_DEFAULTS_KEY,
            project_file_path=None,
            project_base_dir=tmp_path,
            template=DEFAULT_PROJECT_TEMPLATE,
            validation=defaults_validation,
            parsed_situation_schemas=pm._parse_situation_macros(
                DEFAULT_PROJECT_TEMPLATE.situations, defaults_validation
            ),
            parsed_directory_schemas=pm._parse_directory_macros(
                DEFAULT_PROJECT_TEMPLATE.directories, defaults_validation
            ),
        )
        pm._current_project_id = SYSTEM_DEFAULTS_KEY

        workspace_project_path = (tmp_path / WORKSPACE_PROJECT_FILE).resolve()
        workspace_project_path.write_text(self.EXPLICIT_ID_YAML)

        def get_config_value_side_effect(key: str, **_: object) -> str | dict | None:
            if key == "project_file":
                return None
            if "project_workspaces" in key:
                return {}
            return str(tmp_path)

        cast("Mock", pm._config_manager).get_config_value.side_effect = get_config_value_side_effect
        cast("Mock", pm._config_manager).workspace_path = tmp_path

        with patch("griptape_nodes.retained_mode.managers.project_manager.File") as mock_file_cls:
            mock_file_instance = Mock()
            mock_file_instance.aread_text = AsyncMock(return_value=self.EXPLICIT_ID_YAML)
            mock_file_cls.return_value = mock_file_instance

            await pm._load_workspace_project()

        explicit_id = "11111111-2222-3333-4444-555555555555"
        assert pm._current_project_id == explicit_id
        assert explicit_id in pm._successfully_loaded_project_templates

    @pytest.mark.asyncio
    async def test_save_invalidates_id_keyed_cache(self, pm: ProjectManager, tmp_path: Path) -> None:
        """Saving a loaded project pops its id-keyed registry entry (located by path) and path-keyed status."""
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
            SaveProjectTemplateRequest,
            SaveProjectTemplateResultSuccess,
        )

        project_path = (tmp_path / "explicit.yml").resolve()
        files = {project_path: self.EXPLICIT_ID_YAML}
        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = self._file_router(files)
            load = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=project_path))
        assert isinstance(load, LoadProjectTemplateResultSuccess)

        explicit_id = "11111111-2222-3333-4444-555555555555"
        assert explicit_id in pm._successfully_loaded_project_templates

        template_data = {
            "project_template_schema_version": "0.3.3",
            "id": explicit_id,
            "name": "Explicit Id Project",
            "parent_project_path": None,
            "situations": {},
            "directories": {"child_outputs": {"name": "child_outputs", "path_macro": "{workspace_dir}/child_outputs"}},
        }
        result = pm.on_save_project_template_request(
            SaveProjectTemplateRequest(project_path=project_path, template_data=template_data)
        )

        assert isinstance(result, SaveProjectTemplateResultSuccess)
        # The id-keyed entry (found via its file path) and the path-keyed status are gone.
        assert explicit_id not in pm._successfully_loaded_project_templates
        assert project_path not in pm._registered_template_status

    @pytest.mark.asyncio
    async def test_id_based_parent_located_via_registry(self, pm: ProjectManager, tmp_path: Path) -> None:
        """A child's parent_project_id is resolved through the live registry (runtime single-file load)."""
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )

        parent_path = (tmp_path / "parent.yml").resolve()
        child_path = (tmp_path / "child.yml").resolve()
        files = {parent_path: self.PARENT_WITH_ID_YAML, child_path: self.CHILD_BY_PARENT_ID_YAML}
        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = self._file_router(files)
            parent = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=parent_path))
            child = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=child_path))

        assert isinstance(parent, LoadProjectTemplateResultSuccess)
        assert isinstance(child, LoadProjectTemplateResultSuccess)
        assert child.project_id == "child-id-bbbb"
        # Inherited from the parent located by id.
        assert "shared_outputs" in child.template.directories
        assert "child_outputs" in child.template.directories
        # The portable id link round-trips; no machine-specific path is recorded.
        assert child.template.parent_project_id == "parent-id-aaaa"
        assert child.template.parent_project_path is None

    @pytest.mark.asyncio
    async def test_missing_parent_id_fails_closed(self, pm: ProjectManager, tmp_path: Path) -> None:
        """A child naming an unregistered parent_project_id fails to load and names the id."""
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultFailure,
        )

        child_path = (tmp_path / "orphan.yml").resolve()
        files = {child_path: self.CHILD_MISSING_PARENT_ID_YAML}
        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = self._file_router(files)
            result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=child_path))

        assert isinstance(result, LoadProjectTemplateResultFailure)
        # The missing id is named in the validation problems so the user can register it.
        assert any("ghost-parent-id" in problem.message for problem in result.validation.problems)

    @pytest.mark.asyncio
    async def test_boot_loads_child_before_parent_via_id_index(self, pm: ProjectManager, tmp_path: Path) -> None:
        """Boot resolves an id-based parent even when the child is registered first (id->path pre-pass)."""
        from griptape_nodes.retained_mode.managers.settings import PROJECTS_TO_REGISTER_KEY

        parent_path = (tmp_path / "parent.yml").resolve()
        child_path = (tmp_path / "child.yml").resolve()
        files = {parent_path: self.PARENT_WITH_ID_YAML, child_path: self.CHILD_BY_PARENT_ID_YAML}

        def get_config_value_side_effect(key: str, **_: object) -> object:
            if key == PROJECTS_TO_REGISTER_KEY:
                # Child listed BEFORE its parent on purpose.
                return [str(child_path), str(parent_path)]
            return []

        cast("Mock", pm._config_manager).get_config_value.side_effect = get_config_value_side_effect

        with patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes") as mock_gn:
            mock_gn.EventManager.return_value.evaluate_authorization_checkpoint.return_value = None
            mock_gn.ahandle_request = self._file_router(files)
            await pm._load_registered_projects()

        # Both loaded despite the child-before-parent ordering.
        assert "parent-id-aaaa" in pm._successfully_loaded_project_templates
        assert "child-id-bbbb" in pm._successfully_loaded_project_templates
        child_info = pm._successfully_loaded_project_templates["child-id-bbbb"]
        assert "shared_outputs" in child_info.template.directories
        # The transient boot index is cleared once loading finishes.
        assert pm._boot_id_to_file_path == {}


class TestProjectActivationAuthorizationCheckpoint:
    """The license-policy checkpoint wired into project activation."""

    @pytest.fixture
    def project_manager(self) -> ProjectManager:
        return ProjectManager(Mock(), Mock(), Mock())

    @pytest.mark.asyncio
    @patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes")
    async def test_denied_activation_is_rejected_and_leaves_current_project(
        self, mock_griptape_nodes: Mock, project_manager: ProjectManager
    ) -> None:
        from griptape_nodes.retained_mode.events.project_events import (
            SetCurrentProjectRequest,
            SetCurrentProjectResultFailure,
        )
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure
        from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY

        mock_griptape_nodes.EventManager.return_value.evaluate_authorization_checkpoint.return_value = CheckpointDenial(
            failures=(CheckpointFailure(detail="Ask your admin to grant access to acme-prod."),)
        )
        # A denying activation must roll nowhere: _activate_project never runs.
        activate = AsyncMock()
        with patch.object(project_manager, "_activate_project", new=activate):
            result = await project_manager.on_set_current_project_request(
                SetCurrentProjectRequest(project_id="acme-prod")
            )

        assert isinstance(result, SetCurrentProjectResultFailure)
        assert "acme-prod" in str(result.result_details)
        assert "Ask your admin to grant access to acme-prod." in str(result.result_details)
        assert project_manager._current_project_id == SYSTEM_DEFAULTS_KEY
        activate.assert_not_called()

    @pytest.mark.asyncio
    @patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes")
    async def test_empty_failure_denial_still_yields_a_reason(
        self, mock_griptape_nodes: Mock, project_manager: ProjectManager
    ) -> None:
        # A hook that misuses the contract by returning a denial with no failures
        # (it should return None to allow) must still produce a reason, not an
        # empty "Failed because: " tail.
        from griptape_nodes.retained_mode.events.project_events import (
            SetCurrentProjectRequest,
            SetCurrentProjectResultFailure,
        )
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial

        mock_griptape_nodes.EventManager.return_value.evaluate_authorization_checkpoint.return_value = CheckpointDenial(
            failures=()
        )
        activate = AsyncMock()
        with patch.object(project_manager, "_activate_project", new=activate):
            result = await project_manager.on_set_current_project_request(
                SetCurrentProjectRequest(project_id="acme-prod")
            )

        assert isinstance(result, SetCurrentProjectResultFailure)
        assert "Denied by the license policy." in str(result.result_details)
        activate.assert_not_called()

    @pytest.mark.asyncio
    @patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes")
    async def test_system_defaults_evaluates_checkpoint(
        self, mock_griptape_nodes: Mock, project_manager: ProjectManager
    ) -> None:
        from griptape_nodes.retained_mode.events.project_events import (
            SetCurrentProjectRequest,
            SetCurrentProjectResultSuccess,
        )
        from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY, _ProjectActivationOutcome

        # The engine bakes in no exemption: the rest state is gated like any other
        # project. The consumer allows it (returns None), so activation proceeds.
        checkpoint = mock_griptape_nodes.EventManager.return_value.evaluate_authorization_checkpoint
        checkpoint.return_value = None
        outcome = _ProjectActivationOutcome(failure=None, workspace_changed=False)
        with patch.object(project_manager, "_activate_project", new=AsyncMock(return_value=outcome)):
            result = await project_manager.on_set_current_project_request(SetCurrentProjectRequest(project_id=None))

        assert isinstance(result, SetCurrentProjectResultSuccess)
        # The checkpoint is consulted even for the rest state; the policy decides.
        checkpoint.assert_called_once()
        assert checkpoint.call_args.args[0].subject_id == SYSTEM_DEFAULTS_KEY

    @pytest.mark.asyncio
    @patch("griptape_nodes.retained_mode.managers.project_manager.GriptapeNodes")
    async def test_system_defaults_denial_blocks_activation(
        self, mock_griptape_nodes: Mock, project_manager: ProjectManager
    ) -> None:
        from griptape_nodes.retained_mode.events.project_events import (
            SetCurrentProjectRequest,
            SetCurrentProjectResultFailure,
        )
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure

        # A consumer is free to deny even the rest state; the engine enforces that
        # decision rather than exempting the defaults on its own.
        mock_griptape_nodes.EventManager.return_value.evaluate_authorization_checkpoint.return_value = CheckpointDenial(
            failures=(CheckpointFailure(detail="No license covers the default project."),)
        )
        activate = AsyncMock()
        with patch.object(project_manager, "_activate_project", new=activate):
            result = await project_manager.on_set_current_project_request(SetCurrentProjectRequest(project_id=None))

        assert isinstance(result, SetCurrentProjectResultFailure)
        assert "No license covers the default project." in str(result.result_details)
        activate.assert_not_called()


# A minimal but realistic standalone project template used to seed an on-disk
# project base dir for the export/import round-trip tests.
PACKAGING_PROJECT_YAML = """\
"project_template_schema_version": "0.4.0"
"name": "Packaging Test"
"situations":
  "save_node_output":
    "macro": "{outputs}/{file_name_base}.{file_extension}"
    "policy":
      "on_collision": "create_new"
      "create_dirs": true
"directories":
  "inputs":
    "path_macro": "inputs"
  "outputs":
    "path_macro": "outputs"
"""


def _write_project_base_dir(base_dir: Path, adjacent_config: dict | None = None) -> Path:
    """Write a minimal project base dir (template + adjacent config + an asset).

    Returns the path to the project YAML, ready to hand to
    on_load_project_template_request.
    """
    import json

    base_dir.mkdir(parents=True, exist_ok=True)
    project_yaml = base_dir / "griptape-nodes-project.yml"
    project_yaml.write_text(PACKAGING_PROJECT_YAML, encoding="utf-8")
    (base_dir / "griptape_nodes_config.json").write_text(json.dumps(adjacent_config or {}), encoding="utf-8")
    inputs_dir = base_dir / "inputs"
    inputs_dir.mkdir()
    (inputs_dir / "asset.txt").write_text("asset-contents", encoding="utf-8")
    return project_yaml


def _download_config(git_url: str, version: str, name: str) -> dict:
    """An adjacent-config dict declaring a single libraries_to_download entry."""
    return {
        "app_events": {
            "on_app_initialization_complete": {
                "libraries_to_download": [{"git_url": git_url, "version": version, "name": name}],
                "libraries_to_register": [],
            }
        }
    }


def _register_config(register_path: str) -> dict:
    """An adjacent-config dict declaring a single libraries_to_register entry."""
    return {
        "app_events": {
            "on_app_initialization_complete": {
                "libraries_to_download": [],
                "libraries_to_register": [register_path],
            }
        }
    }


class TestClassifyLibraries:
    """Test classify_libraries partitions download vs register libs."""

    def test_download_entry_is_referenced(self, tmp_path: Path) -> None:
        """A libraries_to_download entry is REFERENCE: pin kept, no source copy."""
        from griptape_nodes.retained_mode.publishing.project_packager import classify_libraries

        config = _download_config("https://example.com/lib.git", "v1.2.3", "remote_lib")
        classification = classify_libraries(config, tmp_path)

        assert len(classification.referenced) == 1
        referenced = classification.referenced[0]
        assert referenced.git_url == "https://example.com/lib.git"
        assert referenced.version == "v1.2.3"
        assert referenced.name == "remote_lib"
        assert classification.copied == []

    def test_register_directory_entry_is_copied(self, tmp_path: Path) -> None:
        """A libraries_to_register dir entry is COPY_LOCAL with its dir recorded."""
        from griptape_nodes.retained_mode.publishing.project_packager import classify_libraries

        lib_dir = tmp_path / "mylib"
        lib_dir.mkdir()
        (lib_dir / "griptape_nodes_library.json").write_text('{"name": "mylib"}', encoding="utf-8")

        classification = classify_libraries(_register_config(str(lib_dir)), tmp_path)

        assert classification.referenced == []
        assert len(classification.copied) == 1
        local = classification.copied[0]
        assert local.containing_dir == lib_dir.resolve()
        # A directory registration copies the dir itself; no file-within-dir.
        assert local.path_within_containing_dir is None

    def test_register_json_file_entry_records_containing_dir(self, tmp_path: Path) -> None:
        """A libraries_to_register JSON-file entry copies its parent dir, tracking the file."""
        from griptape_nodes.retained_mode.publishing.project_packager import classify_libraries

        lib_dir = tmp_path / "mylib"
        lib_dir.mkdir()
        library_json = lib_dir / "griptape_nodes_library.json"
        library_json.write_text('{"name": "mylib"}', encoding="utf-8")

        classification = classify_libraries(_register_config(str(library_json)), tmp_path)

        assert len(classification.copied) == 1
        local = classification.copied[0]
        assert local.containing_dir == lib_dir.resolve()
        assert local.path_within_containing_dir == "griptape_nodes_library.json"

    def test_missing_register_entry_is_skipped_and_reported(self, tmp_path: Path) -> None:
        """A register entry whose source is missing is not copied but is reported."""
        from griptape_nodes.retained_mode.publishing.project_packager import (
            classify_libraries,
            find_missing_local_libraries,
        )

        config = _register_config(str(tmp_path / "does_not_exist.json"))

        classification = classify_libraries(config, tmp_path)
        missing = find_missing_local_libraries(config, tmp_path)

        assert classification.copied == []
        assert missing == [str(tmp_path / "does_not_exist.json")]


class TestExportProject:
    """Test on_export_project_request packages a loaded project to a portable .zip."""

    def test_export_not_loaded_project_fails(self, griptape_nodes: object, tmp_path: Path) -> None:  # noqa: ARG002
        """Exporting an unregistered project id returns a Failure."""
        from griptape_nodes.retained_mode.events.project_events import ExportProjectRequest, ExportProjectResultFailure
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        pm = GriptapeNodes.ProjectManager()
        result = pm.on_export_project_request(
            ExportProjectRequest(project_id="not-a-real-project", destination_path=tmp_path / "out.zip")
        )
        assert isinstance(result, ExportProjectResultFailure)

    def test_export_system_defaults_fails(self, griptape_nodes: object, tmp_path: Path) -> None:  # noqa: ARG002
        """Exporting the file-less system defaults project returns a Failure."""
        from griptape_nodes.retained_mode.events.project_events import ExportProjectRequest, ExportProjectResultFailure
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
        from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY

        pm = GriptapeNodes.ProjectManager()
        result = pm.on_export_project_request(
            ExportProjectRequest(project_id=SYSTEM_DEFAULTS_KEY, destination_path=tmp_path / "out.zip")
        )
        assert isinstance(result, ExportProjectResultFailure)

    @pytest.mark.asyncio
    async def test_export_missing_destination_dir_fails(self, griptape_nodes: object, tmp_path: Path) -> None:  # noqa: ARG002
        """Exporting to a destination whose parent dir is missing returns a Failure."""
        from griptape_nodes.retained_mode.events.project_events import (
            ExportProjectRequest,
            ExportProjectResultFailure,
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        pm = GriptapeNodes.ProjectManager()
        project_yaml = _write_project_base_dir(tmp_path / "proj")
        load_result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=project_yaml))
        assert isinstance(load_result, LoadProjectTemplateResultSuccess)

        result = pm.on_export_project_request(
            ExportProjectRequest(
                project_id=load_result.project_id,
                destination_path=tmp_path / "no_such_dir" / "out.zip",
            )
        )
        assert isinstance(result, ExportProjectResultFailure)

    @pytest.mark.asyncio
    async def test_export_referenced_library_round_trip(self, griptape_nodes: object, tmp_path: Path) -> None:  # noqa: ARG002
        """A download lib is referenced (config only), assets travel, .env never does.

        Also asserts a known secret value never leaks into the archive bytes and
        that required_secret_keys carries KEY NAMES only.
        """
        import zipfile

        from griptape_nodes.retained_mode.events.project_events import (
            ExportProjectRequest,
            ExportProjectResultSuccess,
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
        from griptape_nodes.retained_mode.publishing.project_packager import (
            ADJACENT_CONFIG_FILENAME,
            MANIFEST_FILENAME,
            PROJECT_TEMPLATE_FILENAME,
        )

        pm = GriptapeNodes.ProjectManager()
        base_dir = tmp_path / "proj"
        project_yaml = _write_project_base_dir(
            base_dir, _download_config("https://example.com/lib.git", "v1.2.3", "remote_lib")
        )
        # A secret-bearing .env must never travel in the package.
        (base_dir / ".env").write_text("MY_SECRET=super-secret-value", encoding="utf-8")

        load_result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=project_yaml))
        assert isinstance(load_result, LoadProjectTemplateResultSuccess)

        destination = tmp_path / "out.zip"
        result = pm.on_export_project_request(
            ExportProjectRequest(project_id=load_result.project_id, destination_path=destination)
        )

        assert isinstance(result, ExportProjectResultSuccess)
        assert destination.exists()
        assert result.referenced_libraries == ["remote_lib"]
        assert result.copied_libraries == []
        # KEY NAMES only: the core secrets are present, but no values travel.
        assert "GT_CLOUD_API_KEY" in result.required_secret_keys
        assert "HF_TOKEN" in result.required_secret_keys

        with zipfile.ZipFile(destination) as archive:
            members = set(archive.namelist())
            assert PROJECT_TEMPLATE_FILENAME in members
            assert ADJACENT_CONFIG_FILENAME in members
            assert MANIFEST_FILENAME in members
            assert "inputs/asset.txt" in members
            # .env and the hidden caches must be excluded.
            assert ".env" not in members
            assert not any(name.startswith(".griptape-nodes-") for name in members)
            # Referenced (download) libs ship no source.
            assert not any(name.startswith("libraries/") for name in members)
            # No secret VALUE leaks into the archive bytes.
            archive_bytes = b"".join(archive.read(name) for name in members if not name.endswith("/"))
            assert b"super-secret-value" not in archive_bytes

    @pytest.mark.asyncio
    async def test_export_prunes_downloaded_library_sink_inside_base_dir(
        self,
        griptape_nodes: object,  # noqa: ARG002
        tmp_path: Path,
    ) -> None:
        """A download lib cloned into libraries/ inside the base dir ships no source.

        The engine clones libraries_to_download into the project's
        libraries_directory (default 'libraries'), which sits inside the base
        dir. The plain mirror would bundle that referenced source; the export
        must prune it while keeping unrelated assets.
        """
        import zipfile

        from griptape_nodes.retained_mode.events.project_events import (
            ExportProjectRequest,
            ExportProjectResultSuccess,
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        pm = GriptapeNodes.ProjectManager()
        base_dir = tmp_path / "proj"
        project_yaml = _write_project_base_dir(base_dir, _download_config("owner/remote_lib", "v1.0.0", "remote_lib"))
        # Simulate the engine having cloned the referenced lib into the sink.
        sink = base_dir / "libraries" / "remote_lib"
        sink.mkdir(parents=True)
        (sink / "griptape_nodes_library.json").write_text('{"name": "remote_lib"}', encoding="utf-8")
        (sink / "big_model.bin").write_text("DOWNLOADED-SOURCE-SHOULD-NOT-TRAVEL", encoding="utf-8")

        load_result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=project_yaml))
        assert isinstance(load_result, LoadProjectTemplateResultSuccess)

        destination = tmp_path / "out.zip"
        result = pm.on_export_project_request(
            ExportProjectRequest(project_id=load_result.project_id, destination_path=destination)
        )

        assert isinstance(result, ExportProjectResultSuccess)
        assert result.referenced_libraries == ["remote_lib"]
        assert result.copied_libraries == []

        with zipfile.ZipFile(destination) as archive:
            members = set(archive.namelist())
            # The downloaded sink subtree must be absent (referenced libs ship no source).
            assert not any(name.startswith("libraries/") for name in members)
            # Unrelated assets still travel.
            assert "inputs/asset.txt" in members
            archive_bytes = b"".join(archive.read(name) for name in members if not name.endswith("/"))
            assert b"DOWNLOADED-SOURCE-SHOULD-NOT-TRAVEL" not in archive_bytes

    @pytest.mark.asyncio
    async def test_export_nulls_parent_and_id_in_template(self, griptape_nodes: object, tmp_path: Path) -> None:  # noqa: ARG002
        """The bundled YAML has parent links and id nulled, dirs still macro strings."""
        import zipfile

        from ruamel.yaml import YAML

        from griptape_nodes.retained_mode.events.project_events import (
            ExportProjectRequest,
            ExportProjectResultSuccess,
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
        from griptape_nodes.retained_mode.publishing.project_packager import PROJECT_TEMPLATE_FILENAME

        pm = GriptapeNodes.ProjectManager()
        project_yaml = _write_project_base_dir(tmp_path / "proj")
        load_result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=project_yaml))
        assert isinstance(load_result, LoadProjectTemplateResultSuccess)

        destination = tmp_path / "out.zip"
        result = pm.on_export_project_request(
            ExportProjectRequest(project_id=load_result.project_id, destination_path=destination)
        )
        assert isinstance(result, ExportProjectResultSuccess)

        with zipfile.ZipFile(destination) as archive:
            bundled_yaml = archive.read(PROJECT_TEMPLATE_FILENAME).decode("utf-8")
        parsed = YAML().load(bundled_yaml)
        assert parsed.get("parent_project_path") is None
        assert parsed.get("parent_project_id") is None
        assert parsed.get("id") is None
        # Directory paths stay as macro strings so they re-resolve at import.
        assert parsed["directories"]["outputs"]["path_macro"] == "outputs"

    @pytest.mark.asyncio
    async def test_export_copies_local_library_and_rewrites_config(
        self,
        griptape_nodes: object,  # noqa: ARG002
        tmp_path: Path,
    ) -> None:
        """A register-only local lib is true-copied and its config path is package-relative."""
        import json
        import zipfile

        from griptape_nodes.retained_mode.events.project_events import (
            ExportProjectRequest,
            ExportProjectResultSuccess,
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
        from griptape_nodes.retained_mode.managers.settings import LIBRARIES_TO_REGISTER_KEY
        from griptape_nodes.retained_mode.publishing.project_packager import ADJACENT_CONFIG_FILENAME
        from griptape_nodes.utils.dict_utils import get_dot_value

        pm = GriptapeNodes.ProjectManager()
        # The local library lives OUTSIDE the project base dir (absolute path), the
        # confirmed-real shape that must be copied and rewritten to be portable.
        lib_dir = tmp_path / "external_lib"
        lib_dir.mkdir()
        (lib_dir / "griptape_nodes_library.json").write_text('{"name": "external_lib"}', encoding="utf-8")
        # A secret-bearing .env inside the local library dir must not be true-copied
        # into the package: the copy path shares the base-dir mirror's exclusion set.
        (lib_dir / ".env").write_text("LIB_SECRET=copied-lib-secret-value", encoding="utf-8")
        register_path = str(lib_dir / "griptape_nodes_library.json")
        project_yaml = _write_project_base_dir(tmp_path / "proj", _register_config(register_path))
        load_result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=project_yaml))
        assert isinstance(load_result, LoadProjectTemplateResultSuccess)

        destination = tmp_path / "out.zip"
        result = pm.on_export_project_request(
            ExportProjectRequest(project_id=load_result.project_id, destination_path=destination)
        )
        assert isinstance(result, ExportProjectResultSuccess)
        assert result.copied_libraries == [register_path]

        with zipfile.ZipFile(destination) as archive:
            members = set(archive.namelist())
            assert "libraries/external_lib/griptape_nodes_library.json" in members
            # The local library's .env (and its secret value) never travels.
            assert "libraries/external_lib/.env" not in members
            archive_bytes = b"".join(archive.read(name) for name in members if not name.endswith("/"))
            assert b"copied-lib-secret-value" not in archive_bytes
            bundled_config = json.loads(archive.read(ADJACENT_CONFIG_FILENAME))
        rewritten = get_dot_value(bundled_config, LIBRARIES_TO_REGISTER_KEY)
        assert rewritten == ["libraries/external_lib/griptape_nodes_library.json"]

    @pytest.mark.asyncio
    async def test_import_copied_local_library_resolves_against_new_base_dir(
        self,
        griptape_nodes: object,  # noqa: ARG002
        tmp_path: Path,
    ) -> None:
        """A COPY_LOCAL lib is extracted and its rewritten config path resolves at the target.

        Closes the round-trip for the copied-library disposition: export rewrites
        the register path to a package-relative one, and import must extract that
        source under the new base dir AND leave the imported adjacent config
        pointing at the package-relative path so it resolves at the new location.
        """
        import json

        from griptape_nodes.retained_mode.events.project_events import (
            ExportProjectRequest,
            ImportProjectRequest,
            ImportProjectResultSuccess,
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
        from griptape_nodes.retained_mode.managers.settings import LIBRARIES_TO_REGISTER_KEY
        from griptape_nodes.utils.dict_utils import get_dot_value

        pm = GriptapeNodes.ProjectManager()
        lib_dir = tmp_path / "external_lib"
        lib_dir.mkdir()
        (lib_dir / "griptape_nodes_library.json").write_text('{"name": "external_lib"}', encoding="utf-8")
        register_path = str(lib_dir / "griptape_nodes_library.json")
        project_yaml = _write_project_base_dir(tmp_path / "proj", _register_config(register_path))
        load_result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=project_yaml))
        assert isinstance(load_result, LoadProjectTemplateResultSuccess)

        destination = tmp_path / "out.zip"
        pm.on_export_project_request(
            ExportProjectRequest(project_id=load_result.project_id, destination_path=destination)
        )

        target = tmp_path / "imported"
        result = await pm.on_import_project_request(
            ImportProjectRequest(archive_path=destination, target_directory=target)
        )
        assert isinstance(result, ImportProjectResultSuccess)

        # The copied source landed under the new base dir, and the imported config
        # points at the package-relative path so it resolves at the new location.
        package_relative = "libraries/external_lib/griptape_nodes_library.json"
        assert (target / package_relative).exists()
        imported_config = json.loads((target / "griptape_nodes_config.json").read_text(encoding="utf-8"))
        assert get_dot_value(imported_config, LIBRARIES_TO_REGISTER_KEY) == [package_relative]

    @pytest.mark.asyncio
    async def test_export_drops_self_referential_workspace_directory(
        self,
        griptape_nodes: object,  # noqa: ARG002
        tmp_path: Path,
    ) -> None:
        """A workspace_directory equal to the project's own base dir is dropped on export.

        The source-machine absolute path would otherwise survive the round trip and
        make the importing engine re-download referenced libraries into the source
        workspace instead of the imported project's own libraries/ dir. Dropping it
        lets decide_workspace auto-default the workspace to the import target.
        """
        import json
        import zipfile

        from griptape_nodes.retained_mode.events.project_events import (
            ExportProjectRequest,
            ExportProjectResultSuccess,
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
        from griptape_nodes.retained_mode.publishing.project_packager import (
            ADJACENT_CONFIG_FILENAME,
            WORKSPACE_DIRECTORY_KEY,
        )

        pm = GriptapeNodes.ProjectManager()
        base_dir = tmp_path / "proj"
        # workspace_directory points at the project's own base dir (self-contained).
        project_yaml = _write_project_base_dir(base_dir, {WORKSPACE_DIRECTORY_KEY: str(base_dir)})
        load_result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=project_yaml))
        assert isinstance(load_result, LoadProjectTemplateResultSuccess)

        destination = tmp_path / "out.zip"
        result = pm.on_export_project_request(
            ExportProjectRequest(project_id=load_result.project_id, destination_path=destination)
        )
        assert isinstance(result, ExportProjectResultSuccess)

        with zipfile.ZipFile(destination) as archive:
            bundled_config = json.loads(archive.read(ADJACENT_CONFIG_FILENAME))
        assert WORKSPACE_DIRECTORY_KEY not in bundled_config

    @pytest.mark.asyncio
    async def test_export_preserves_external_workspace_directory(
        self,
        griptape_nodes: object,  # noqa: ARG002
        tmp_path: Path,
    ) -> None:
        """A workspace_directory pointing outside the project's base dir is preserved.

        Such a value names a genuine external/shared workspace dependency we cannot
        relocate, so it must survive export verbatim rather than being silently dropped.
        """
        import json
        import zipfile

        from griptape_nodes.retained_mode.events.project_events import (
            ExportProjectRequest,
            ExportProjectResultSuccess,
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
        from griptape_nodes.retained_mode.publishing.project_packager import (
            ADJACENT_CONFIG_FILENAME,
            WORKSPACE_DIRECTORY_KEY,
        )

        pm = GriptapeNodes.ProjectManager()
        base_dir = tmp_path / "proj"
        external_workspace = tmp_path / "shared_workspace"
        external_workspace.mkdir()
        project_yaml = _write_project_base_dir(base_dir, {WORKSPACE_DIRECTORY_KEY: str(external_workspace)})
        load_result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=project_yaml))
        assert isinstance(load_result, LoadProjectTemplateResultSuccess)

        destination = tmp_path / "out.zip"
        result = pm.on_export_project_request(
            ExportProjectRequest(project_id=load_result.project_id, destination_path=destination)
        )
        assert isinstance(result, ExportProjectResultSuccess)

        with zipfile.ZipFile(destination) as archive:
            bundled_config = json.loads(archive.read(ADJACENT_CONFIG_FILENAME))
        assert bundled_config.get(WORKSPACE_DIRECTORY_KEY) == str(external_workspace)

    @pytest.mark.asyncio
    async def test_export_same_basename_copied_libraries_stay_distinct(
        self,
        griptape_nodes: object,  # noqa: ARG002
        tmp_path: Path,
    ) -> None:
        """Two COPY_LOCAL libs whose containing dirs share a basename keep distinct paths.

        The collision-suffix dirname (shared_lib, shared_lib_2) must flow through to
        BOTH the rewritten config and the manifest's per-lib source_relative_path. A
        basename-keyed manifest lookup would collapse the two onto one path and
        mislabel one lib's provenance; this locks the positional pairing in place.
        """
        import json
        import zipfile

        from griptape_nodes.retained_mode.events.project_events import (
            ExportProjectRequest,
            ExportProjectResultSuccess,
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
        from griptape_nodes.retained_mode.managers.settings import LIBRARIES_TO_REGISTER_KEY
        from griptape_nodes.retained_mode.publishing.project_packager import (
            ADJACENT_CONFIG_FILENAME,
            MANIFEST_FILENAME,
        )
        from griptape_nodes.utils.dict_utils import get_dot_value

        pm = GriptapeNodes.ProjectManager()
        # Two libraries in same-basename containing dirs under different parents.
        lib_dir_a = tmp_path / "a" / "shared_lib"
        lib_dir_b = tmp_path / "b" / "shared_lib"
        lib_dir_a.mkdir(parents=True)
        lib_dir_b.mkdir(parents=True)
        (lib_dir_a / "griptape_nodes_library.json").write_text('{"name": "lib_a"}', encoding="utf-8")
        (lib_dir_b / "griptape_nodes_library.json").write_text('{"name": "lib_b"}', encoding="utf-8")
        register_path_a = str(lib_dir_a / "griptape_nodes_library.json")
        register_path_b = str(lib_dir_b / "griptape_nodes_library.json")
        config = {
            "app_events": {
                "on_app_initialization_complete": {
                    "libraries_to_download": [],
                    "libraries_to_register": [register_path_a, register_path_b],
                }
            }
        }
        project_yaml = _write_project_base_dir(tmp_path / "proj", config)
        load_result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=project_yaml))
        assert isinstance(load_result, LoadProjectTemplateResultSuccess)

        destination = tmp_path / "out.zip"
        result = pm.on_export_project_request(
            ExportProjectRequest(project_id=load_result.project_id, destination_path=destination)
        )
        assert isinstance(result, ExportProjectResultSuccess)

        with zipfile.ZipFile(destination) as archive:
            members = set(archive.namelist())
            bundled_config = json.loads(archive.read(ADJACENT_CONFIG_FILENAME))
            manifest = json.loads(archive.read(MANIFEST_FILENAME))

        # Both sources are copied under distinct, collision-suffixed dirs.
        assert "libraries/shared_lib/griptape_nodes_library.json" in members
        assert "libraries/shared_lib_2/griptape_nodes_library.json" in members

        # The rewritten config preserves order and gives each entry its own path.
        rewritten = get_dot_value(bundled_config, LIBRARIES_TO_REGISTER_KEY)
        assert rewritten == [
            "libraries/shared_lib/griptape_nodes_library.json",
            "libraries/shared_lib_2/griptape_nodes_library.json",
        ]

        # The manifest records a distinct source_relative_path per copied lib; a
        # basename-keyed lookup would have collapsed these to a single path.
        copied_paths = [
            lib["source_relative_path"] for lib in manifest["libraries"] if lib["disposition"] == "COPY_LOCAL"
        ]
        assert copied_paths == [
            "libraries/shared_lib/griptape_nodes_library.json",
            "libraries/shared_lib_2/griptape_nodes_library.json",
        ]


class TestPreviewImportProject:
    """Test on_preview_import_project_request reads a manifest without extracting."""

    def test_preview_missing_archive_fails(self, griptape_nodes: object, tmp_path: Path) -> None:  # noqa: ARG002
        """Previewing a non-existent archive returns a Failure."""
        from griptape_nodes.retained_mode.events.project_events import (
            PreviewImportProjectRequest,
            PreviewImportProjectResultFailure,
        )
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        pm = GriptapeNodes.ProjectManager()
        result = pm.on_preview_import_project_request(
            PreviewImportProjectRequest(archive_path=tmp_path / "missing.zip")
        )
        assert isinstance(result, PreviewImportProjectResultFailure)

    def test_preview_non_zip_fails(self, griptape_nodes: object, tmp_path: Path) -> None:  # noqa: ARG002
        """Previewing a file that is not a zip returns a Failure."""
        from griptape_nodes.retained_mode.events.project_events import (
            PreviewImportProjectRequest,
            PreviewImportProjectResultFailure,
        )
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        not_a_zip = tmp_path / "plain.zip"
        not_a_zip.write_text("this is not a zip archive", encoding="utf-8")

        pm = GriptapeNodes.ProjectManager()
        result = pm.on_preview_import_project_request(PreviewImportProjectRequest(archive_path=not_a_zip))
        assert isinstance(result, PreviewImportProjectResultFailure)

    @pytest.mark.asyncio
    async def test_non_dict_manifest_fails_cleanly(self, griptape_nodes: object, tmp_path: Path) -> None:  # noqa: ARG002
        """A valid-JSON-but-non-dict manifest returns a clean Failure, not a traceback.

        A tampered package whose manifest.json parses to a list/number/string would
        otherwise reach is_manifest_schema_compatible(...).get(...) and raise an
        uncaught AttributeError. read_manifest rejects the non-dict as a
        JSONDecodeError (already in the handlers' caught set) so both preview and
        import surface a Failure instead of crashing.
        """
        import zipfile

        from griptape_nodes.retained_mode.events.project_events import (
            ImportProjectRequest,
            ImportProjectResultFailure,
            PreviewImportProjectRequest,
            PreviewImportProjectResultFailure,
        )
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
        from griptape_nodes.retained_mode.publishing.project_packager import MANIFEST_FILENAME

        bad_manifest_zip = tmp_path / "bad-manifest.zip"
        with zipfile.ZipFile(bad_manifest_zip, "w") as archive:
            archive.writestr(MANIFEST_FILENAME, "[]")

        pm = GriptapeNodes.ProjectManager()

        preview_result = pm.on_preview_import_project_request(
            PreviewImportProjectRequest(archive_path=bad_manifest_zip)
        )
        assert isinstance(preview_result, PreviewImportProjectResultFailure)

        import_result = await pm.on_import_project_request(
            ImportProjectRequest(archive_path=bad_manifest_zip, target_directory=tmp_path / "imported")
        )
        assert isinstance(import_result, ImportProjectResultFailure)

    @pytest.mark.asyncio
    async def test_preview_valid_archive_returns_manifest_and_unset_secrets(
        self,
        griptape_nodes: object,  # noqa: ARG002
        tmp_path: Path,
    ) -> None:
        """A valid package previews its manifest plus the unset required secret keys."""
        from griptape_nodes.retained_mode.events.project_events import (
            ExportProjectRequest,
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
            PreviewImportProjectRequest,
            PreviewImportProjectResultSuccess,
        )
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        pm = GriptapeNodes.ProjectManager()
        project_yaml = _write_project_base_dir(tmp_path / "proj")
        load_result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=project_yaml))
        assert isinstance(load_result, LoadProjectTemplateResultSuccess)
        destination = tmp_path / "out.zip"
        pm.on_export_project_request(
            ExportProjectRequest(project_id=load_result.project_id, destination_path=destination)
        )

        result = pm.on_preview_import_project_request(PreviewImportProjectRequest(archive_path=destination))

        assert isinstance(result, PreviewImportProjectResultSuccess)
        assert result.manifest["manifest_schema_version"].startswith("1.")
        # The manifest carries the required secret KEY names; unset_secret_keys is
        # the subset with no value in this environment (which may or may not have
        # the core secrets set, so assert the relationship rather than membership).
        required = result.manifest["required_secret_keys"]
        assert "GT_CLOUD_API_KEY" in required
        assert "HF_TOKEN" in required
        assert set(result.unset_secret_keys) <= set(required)


class TestImportProject:
    """Test on_import_project_request extracts a package and registers the project."""

    @pytest.mark.asyncio
    async def test_import_registers_new_project_with_assets(self, griptape_nodes: object, tmp_path: Path) -> None:  # noqa: ARG002
        """Importing into a fresh dir registers the project and activates it; macros follow the active workspace."""
        from griptape_nodes.common.macro_parser import ParsedMacro
        from griptape_nodes.retained_mode.events.project_events import (
            ExportProjectRequest,
            GetPathForMacroRequest,
            GetPathForMacroResultSuccess,
            ImportProjectRequest,
            ImportProjectResultSuccess,
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        pm = GriptapeNodes.ProjectManager()
        project_yaml = _write_project_base_dir(tmp_path / "proj")
        load_result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=project_yaml))
        assert isinstance(load_result, LoadProjectTemplateResultSuccess)
        destination = tmp_path / "out.zip"
        pm.on_export_project_request(
            ExportProjectRequest(project_id=load_result.project_id, destination_path=destination)
        )

        target = tmp_path / "imported"
        result = await pm.on_import_project_request(
            ImportProjectRequest(archive_path=destination, target_directory=target, set_as_current=True)
        )

        assert isinstance(result, ImportProjectResultSuccess)
        assert result.project_id in pm._successfully_loaded_project_templates
        # The asset extracted under the new base dir, and the base dir re-points there.
        assert (target / "inputs" / "asset.txt").read_text(encoding="utf-8") == "asset-contents"
        imported_info = pm._successfully_loaded_project_templates[result.project_id]
        assert imported_info.project_base_dir.resolve() == target.resolve()

        # set_as_current took effect: the imported project is the active one.
        assert pm._current_project_id == result.project_id

        # {outputs} resolves against the active project's workspace, proving the
        # macro layer follows the import rather than pointing back at the source
        # dir. A standalone import with no workspace_directory of its own adopts
        # the global configured workspace (decide_workspace branch 5), so the
        # macro anchors there rather than under the export source.
        active_workspace = pm._config_manager.workspace_path
        macro_result = pm.on_get_path_for_macro_request(
            GetPathForMacroRequest(parsed_macro=ParsedMacro("{outputs}/result.txt"), variables={})
        )
        assert isinstance(macro_result, GetPathForMacroResultSuccess)
        assert macro_result.absolute_path.resolve() == (active_workspace / "outputs" / "result.txt").resolve()
        source_dir = (tmp_path / "proj").resolve()
        assert source_dir not in macro_result.absolute_path.resolve().parents

    @pytest.mark.asyncio
    async def test_import_with_new_name_renames_template(self, griptape_nodes: object, tmp_path: Path) -> None:  # noqa: ARG002
        """A new_project_name renames the imported template (duplicate/branch)."""
        from griptape_nodes.retained_mode.events.project_events import (
            ExportProjectRequest,
            ImportProjectRequest,
            ImportProjectResultSuccess,
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        pm = GriptapeNodes.ProjectManager()
        project_yaml = _write_project_base_dir(tmp_path / "proj")
        load_result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=project_yaml))
        assert isinstance(load_result, LoadProjectTemplateResultSuccess)
        destination = tmp_path / "out.zip"
        pm.on_export_project_request(
            ExportProjectRequest(project_id=load_result.project_id, destination_path=destination)
        )

        target = tmp_path / "branch"
        result = await pm.on_import_project_request(
            ImportProjectRequest(archive_path=destination, target_directory=target, new_project_name="Branch X")
        )

        assert isinstance(result, ImportProjectResultSuccess)
        imported_info = pm._successfully_loaded_project_templates[result.project_id]
        assert imported_info.template.name == "Branch X"

    @pytest.mark.asyncio
    async def test_import_two_targets_are_distinct_projects(self, griptape_nodes: object, tmp_path: Path) -> None:  # noqa: ARG002
        """Importing the same package to two dirs yields two distinct registrations."""
        from griptape_nodes.retained_mode.events.project_events import (
            ExportProjectRequest,
            ImportProjectRequest,
            ImportProjectResultSuccess,
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        pm = GriptapeNodes.ProjectManager()
        project_yaml = _write_project_base_dir(tmp_path / "proj")
        load_result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=project_yaml))
        assert isinstance(load_result, LoadProjectTemplateResultSuccess)
        destination = tmp_path / "out.zip"
        pm.on_export_project_request(
            ExportProjectRequest(project_id=load_result.project_id, destination_path=destination)
        )

        first = await pm.on_import_project_request(
            ImportProjectRequest(archive_path=destination, target_directory=tmp_path / "a")
        )
        second = await pm.on_import_project_request(
            ImportProjectRequest(archive_path=destination, target_directory=tmp_path / "b")
        )

        assert isinstance(first, ImportProjectResultSuccess)
        assert isinstance(second, ImportProjectResultSuccess)
        assert first.project_id != second.project_id

    @pytest.mark.asyncio
    async def test_import_same_dir_without_overwrite_fails(self, griptape_nodes: object, tmp_path: Path) -> None:  # noqa: ARG002
        """Re-importing into a dir that already has a project file fails unless overwrite."""
        from griptape_nodes.retained_mode.events.project_events import (
            ExportProjectRequest,
            ImportProjectRequest,
            ImportProjectResultFailure,
            ImportProjectResultSuccess,
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        pm = GriptapeNodes.ProjectManager()
        project_yaml = _write_project_base_dir(tmp_path / "proj")
        load_result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=project_yaml))
        assert isinstance(load_result, LoadProjectTemplateResultSuccess)
        destination = tmp_path / "out.zip"
        pm.on_export_project_request(
            ExportProjectRequest(project_id=load_result.project_id, destination_path=destination)
        )

        target = tmp_path / "imported"
        first = await pm.on_import_project_request(
            ImportProjectRequest(archive_path=destination, target_directory=target)
        )
        assert isinstance(first, ImportProjectResultSuccess)

        second = await pm.on_import_project_request(
            ImportProjectRequest(archive_path=destination, target_directory=target)
        )
        assert isinstance(second, ImportProjectResultFailure)

    @pytest.mark.asyncio
    async def test_import_unset_secret_reported_no_value_written(
        self,
        griptape_nodes: object,  # noqa: ARG002
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A required secret with no value in the target env is reported, never written.

        Uses a synthetic secret key guaranteed absent from the environment so the
        assertion does not depend on whether the dev machine has the core secrets
        set. The export reads required keys from secrets_to_register, so injecting
        the synthetic key there makes it travel in the manifest.
        """
        from griptape_nodes.retained_mode.events.project_events import (
            ExportProjectRequest,
            ImportProjectRequest,
            ImportProjectResultSuccess,
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
        )
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        synthetic_key = "GTN_PACKAGING_TEST_UNSET_SECRET"
        monkeypatch.delenv(synthetic_key, raising=False)

        pm = GriptapeNodes.ProjectManager()
        secrets_manager = GriptapeNodes.SecretsManager()
        monkeypatch.setattr(
            type(secrets_manager),
            "secrets_to_register",
            property(lambda _self: {synthetic_key: ""}),
        )

        project_yaml = _write_project_base_dir(tmp_path / "proj")
        load_result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=project_yaml))
        assert isinstance(load_result, LoadProjectTemplateResultSuccess)
        destination = tmp_path / "out.zip"
        pm.on_export_project_request(
            ExportProjectRequest(project_id=load_result.project_id, destination_path=destination)
        )

        target = tmp_path / "imported"
        result = await pm.on_import_project_request(
            ImportProjectRequest(archive_path=destination, target_directory=target)
        )

        assert isinstance(result, ImportProjectResultSuccess)
        assert result.required_secret_keys == [synthetic_key]
        assert synthetic_key in result.unset_secret_keys
        # Detection must not have created/written the secret value.
        assert secrets_manager.get_secret(synthetic_key, should_error_on_not_found=False) is None

    @pytest.mark.asyncio
    async def test_round_trip_with_string_paths_from_wire(self, griptape_nodes: object, tmp_path: Path) -> None:  # noqa: ARG002
        """Path-typed request fields arriving as wire strings round-trip cleanly.

        project_events declares destination_path/archive_path/target_directory as
        Path. Over the WebSocket they arrive as plain JSON strings. Because
        project_events imports Path at runtime, cattrs coerces those fields to Path
        for the preview/import requests (verified below). ExportProjectRequest is
        the exception: it also carries project_id: ProjectID, a TYPE_CHECKING-only
        forward reference (project_events cannot import project_manager at runtime
        without a cycle), so get_type_hints() raises NameError for the whole class
        and cattrs falls back to a no-coercion structure. destination_path stays a
        str there, so on_export_project_request coerces it at the boundary. Either
        way the handler must not crash on a wire string; this exercises the real
        converter path end to end.
        """
        from griptape_nodes.retained_mode.events.event_converter import converter
        from griptape_nodes.retained_mode.events.project_events import (
            ExportProjectRequest,
            ExportProjectResultSuccess,
            ImportProjectRequest,
            ImportProjectResultSuccess,
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
            PreviewImportProjectRequest,
            PreviewImportProjectResultSuccess,
        )
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        pm = GriptapeNodes.ProjectManager()
        project_yaml = _write_project_base_dir(tmp_path / "proj")
        load_result = await pm.on_load_project_template_request(LoadProjectTemplateRequest(project_path=project_yaml))
        assert isinstance(load_result, LoadProjectTemplateResultSuccess)

        destination = tmp_path / "out.zip"
        export_request = converter.structure(
            {"project_id": load_result.project_id, "destination_path": str(destination)},
            ExportProjectRequest,
        )
        # ProjectID forward ref blocks coercion for this class; the field stays str.
        assert isinstance(export_request.destination_path, str)
        export_result = pm.on_export_project_request(export_request)
        assert isinstance(export_result, ExportProjectResultSuccess)

        preview_request = converter.structure({"archive_path": str(destination)}, PreviewImportProjectRequest)
        assert isinstance(preview_request.archive_path, Path)
        preview_result = pm.on_preview_import_project_request(preview_request)
        assert isinstance(preview_result, PreviewImportProjectResultSuccess)

        target = tmp_path / "imported"
        import_request = converter.structure(
            {"archive_path": str(destination), "target_directory": str(target)},
            ImportProjectRequest,
        )
        assert isinstance(import_request.archive_path, Path)
        assert isinstance(import_request.target_directory, Path)
        import_result = await pm.on_import_project_request(import_request)
        assert isinstance(import_result, ImportProjectResultSuccess)
        assert import_result.project_id in pm._successfully_loaded_project_templates


class TestProjectManagerGetProjectChain:
    """`get_project_chain` resolves a project and its ancestors, leaf-first."""

    @staticmethod
    def _pm() -> ProjectManager:
        return ProjectManager(Mock(), Mock(), Mock())

    @staticmethod
    def _register(
        pm: ProjectManager,
        project_id: str,
        *,
        name: str | None = None,
        parent_id: str | None = None,
        file: Path | None = None,
    ) -> None:
        """Register an id-linked project directly in the in-memory registry.

        The parent link is an explicit `parent_project_id`, so the walk resolves it
        through the registry without touching disk; `name` rides on the template so
        each chain entry can assert a distinct display name.
        """
        from griptape_nodes.common.project_templates import ProjectValidationInfo, ProjectValidationStatus
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.retained_mode.managers.project_manager import ProjectInfo

        update: dict[str, Any] = {"id": project_id, "parent_project_id": parent_id}
        if name is not None:
            update["name"] = name
        template = DEFAULT_PROJECT_TEMPLATE.model_copy(update=update)
        pm._successfully_loaded_project_templates[project_id] = ProjectInfo(
            project_id=project_id,
            project_file_path=file,
            project_base_dir=file.parent if file is not None else Path("/"),
            template=template,
            validation=ProjectValidationInfo(status=ProjectValidationStatus.GOOD),
            parsed_situation_schemas={},
            parsed_directory_schemas={},
        )

    def test_chain_for_parentless_project_is_just_itself(self) -> None:
        from griptape_nodes.retained_mode.managers.project_manager import ProjectChainEntry

        pm = self._pm()
        self._register(pm, "solo", name="Solo")
        pm._current_project_id = "solo"
        assert pm.get_project_chain() == [ProjectChainEntry(id="solo", name="Solo")]

    def test_chain_walks_parents_leaf_first(self) -> None:
        from griptape_nodes.retained_mode.managers.project_manager import ProjectChainEntry

        pm = self._pm()
        self._register(pm, "root", name="Root")
        self._register(pm, "mid", name="Mid", parent_id="root")
        self._register(pm, "leaf", name="Leaf", parent_id="mid")
        pm._current_project_id = "leaf"
        assert pm.get_project_chain() == [
            ProjectChainEntry(id="leaf", name="Leaf"),
            ProjectChainEntry(id="mid", name="Mid"),
            ProjectChainEntry(id="root", name="Root"),
        ]

    def test_chain_accepts_explicit_project_id_overriding_current(self) -> None:
        from griptape_nodes.retained_mode.managers.project_manager import ProjectChainEntry

        pm = self._pm()
        self._register(pm, "root", name="Root")
        self._register(pm, "leaf", name="Leaf", parent_id="root")
        pm._current_project_id = "root"
        assert pm.get_project_chain("leaf") == [
            ProjectChainEntry(id="leaf", name="Leaf"),
            ProjectChainEntry(id="root", name="Root"),
        ]

    def test_chain_breaks_on_cycle_without_repeating(self) -> None:
        from griptape_nodes.retained_mode.managers.project_manager import ProjectChainEntry

        pm = self._pm()
        # a -> b -> a: the walk must terminate at the first repeated id.
        self._register(pm, "a", name="A", parent_id="b")
        self._register(pm, "b", name="B", parent_id="a")
        pm._current_project_id = "a"
        assert pm.get_project_chain() == [
            ProjectChainEntry(id="a", name="A"),
            ProjectChainEntry(id="b", name="B"),
        ]

    def test_chain_surfaces_unregistered_parent_by_id_then_stops(self) -> None:
        from griptape_nodes.retained_mode.managers.project_manager import ProjectChainEntry

        pm = self._pm()
        self._register(pm, "leaf", name="Leaf", parent_id="ghost")
        pm._current_project_id = "leaf"
        # The unregistered parent's id is surfaced (so a policy can still match it),
        # but it has no template to walk further and no resolved name.
        assert pm.get_project_chain() == [
            ProjectChainEntry(id="leaf", name="Leaf"),
            ProjectChainEntry(id="ghost", name=None),
        ]

    def test_chain_for_unregistered_start_is_just_its_id(self) -> None:
        from griptape_nodes.retained_mode.managers.project_manager import ProjectChainEntry

        pm = self._pm()
        pm._current_project_id = "missing"
        assert pm.get_project_chain() == [ProjectChainEntry(id="missing", name=None)]

    def test_chain_defaults_to_current_project(self) -> None:
        from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY

        pm = self._pm()
        # __init__ registers system defaults and points the current id at them.
        chain = pm.get_project_chain()
        assert [entry.id for entry in chain] == [SYSTEM_DEFAULTS_KEY]


class TestProjectVariableResolution:
    """ProjectManager's computed-variable surface: resolve_project_variable + project_computed_names."""

    @staticmethod
    def _pm() -> ProjectManager:
        return ProjectManager(Mock(), Mock(), Mock())

    def test_computed_names_include_builtins_for_current_project(self) -> None:
        pm = self._pm()
        names = pm.project_computed_names(project_id=None)
        assert "workspace_dir" in names
        assert "project_dir" in names
        assert "workflow_dir" in names

    def test_computed_names_unknown_project_is_empty(self) -> None:
        pm = self._pm()
        assert pm.project_computed_names(project_id="not_loaded") == set()

    def test_resolve_project_id_none_maps_to_current(self) -> None:
        from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY

        pm = self._pm()
        assert pm.resolve_project_id(None) == SYSTEM_DEFAULTS_KEY
        assert pm.resolve_project_id("not_loaded") is None

    def test_resolve_builtin_returns_plain_snapshot(self) -> None:
        from griptape_nodes.retained_mode.variable_types import FlowVariable, VariablePermission

        pm = self._pm()
        pm._config_manager.workspace_path = Path("/synthetic/ws")
        variable = pm.resolve_project_variable("workspace_dir", project_id=None)
        assert type(variable) is FlowVariable
        assert variable.value == "/synthetic/ws"
        assert variable.permission is VariablePermission.READ_ONLY

    def test_resolve_unknown_name_raises_value_error(self) -> None:
        pm = self._pm()
        with pytest.raises(ValueError, match="Unknown computed project variable"):
            pm.resolve_project_variable("not_defined_anywhere", project_id=None)

    def test_resolve_unknown_project_raises_value_error(self) -> None:
        pm = self._pm()
        with pytest.raises(ValueError, match="not loaded"):
            pm.resolve_project_variable("workspace_dir", project_id="not_loaded")

    def test_resolve_context_not_ready_propagates(self) -> None:
        """A builtin whose live context isn't ready raises for the caller to handle."""
        pm = self._pm()

        def blow_up(name: str, project_info: object) -> str:  # noqa: ARG001
            msg = "context not ready"
            raise RuntimeError(msg)

        with (
            patch.object(pm, "_get_builtin_variable_value", side_effect=blow_up),
            pytest.raises(RuntimeError, match="context not ready"),
        ):
            pm.resolve_project_variable("workflow_dir", project_id=None)

    def test_resolved_snapshot_serializes_cleanly(self) -> None:
        """Regression: the snapshot must survive cattrs unstructure (no live resolver attached)."""
        from griptape_nodes.retained_mode.events.event_converter import safe_unstructure

        pm = self._pm()
        pm._config_manager.workspace_path = Path("/synthetic/ws")
        variable = pm.resolve_project_variable("workspace_dir", project_id=None)
        serialized = safe_unstructure(variable)
        assert serialized["name"] == "workspace_dir"
        assert serialized["value"] == "/synthetic/ws"
