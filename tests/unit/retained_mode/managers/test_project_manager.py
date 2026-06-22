"""Tests for ProjectManager macro event handlers."""

import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest

if TYPE_CHECKING:
    from griptape_nodes.common.project_templates.directory import PerPlatformPathMacro

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
        """A file read failure leaves system defaults as current project."""
        from griptape_nodes.files.file import FileLoadError
        from griptape_nodes.retained_mode.events.os_events import FileIOFailureReason
        from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY, WORKSPACE_PROJECT_FILE

        self._setup_system_defaults(pm, str(tmp_path))

        # Create the file so the existence check passes
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
            mock_file_instance.aread_text = AsyncMock(
                side_effect=FileLoadError(
                    failure_reason=FileIOFailureReason.FILE_NOT_FOUND,
                    result_details="permission denied",
                )
            )
            mock_file_cls.return_value = mock_file_instance

            await pm._load_workspace_project()

        assert pm._current_project_id == SYSTEM_DEFAULTS_KEY

    @pytest.mark.asyncio
    async def test_load_workspace_project_invalid_yaml_keeps_defaults(self, pm: ProjectManager, tmp_path: Path) -> None:
        """Invalid YAML in project file leaves system defaults as current project."""
        from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY, WORKSPACE_PROJECT_FILE

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
            mock_file_instance.aread_text = AsyncMock(return_value="not: valid: yaml: ][")
            mock_file_cls.return_value = mock_file_instance

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

        pm = self._pm_with_project_workspaces({})
        decision = pm.decide_workspace(project_file, project_config={}, env_config={})

        assert decision.workspace_dir == project_file.parent
        assert decision.apply_override is True


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
                "outputs": {"name": "outputs", "path_macro": "outputs2"},
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
                "outputs": {"name": "outputs", "path_macro": "outputs2"},  # inherited
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
                "outputs": {"name": "outputs", "path_macro": "outputs2"},  # inherited
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
                "outputs": {"name": "outputs", "path_macro": "outputs2"},  # inherited transitively
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
        """A project file that resolves but fails to load returns Failure (still on system defaults)."""
        from griptape_nodes.files.file import FileLoadError
        from griptape_nodes.retained_mode.events.os_events import FileIOFailureReason
        from griptape_nodes.retained_mode.events.project_events import (
            ActivateWorkspaceProjectRequest,
            ActivateWorkspaceProjectResultFailure,
        )
        from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY, WORKSPACE_PROJECT_FILE

        self._setup_system_defaults(pm, str(tmp_path))

        # File exists so the path resolves, but the read fails so activation cannot take.
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
            mock_file_instance.aread_text = AsyncMock(
                side_effect=FileLoadError(
                    failure_reason=FileIOFailureReason.FILE_NOT_FOUND,
                    result_details="permission denied",
                )
            )
            mock_file_cls.return_value = mock_file_instance

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

        with patch("griptape_nodes.retained_mode.managers.project_manager.File") as mock_file_cls:
            mock_file_instance = Mock()
            mock_file_instance.aread_text = AsyncMock(return_value="not: valid: yaml: : :\n  - broken")
            mock_file_cls.return_value = mock_file_instance

            result = await pm.on_activate_workspace_project_request(ActivateWorkspaceProjectRequest())

        assert isinstance(result, ActivateWorkspaceProjectResultFailure)
        assert str(workspace_project_path) in str(result.result_details)
        assert "Failed because" in str(result.result_details)


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
    async def test_system_defaults_bypasses_checkpoint(
        self, mock_griptape_nodes: Mock, project_manager: ProjectManager
    ) -> None:
        from griptape_nodes.retained_mode.events.project_events import (
            SetCurrentProjectRequest,
            SetCurrentProjectResultSuccess,
        )
        from griptape_nodes.retained_mode.managers.project_manager import _ProjectActivationOutcome

        outcome = _ProjectActivationOutcome(failure=None, workspace_changed=False)
        with patch.object(project_manager, "_activate_project", new=AsyncMock(return_value=outcome)):
            result = await project_manager.on_set_current_project_request(SetCurrentProjectRequest(project_id=None))

        assert isinstance(result, SetCurrentProjectResultSuccess)
        # The rest state is always allowed; the checkpoint is never consulted.
        mock_griptape_nodes.EventManager.return_value.evaluate_authorization_checkpoint.assert_not_called()
