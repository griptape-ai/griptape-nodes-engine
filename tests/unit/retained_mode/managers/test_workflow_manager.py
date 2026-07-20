import ast
import asyncio
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple
from unittest.mock import AsyncMock, MagicMock, Mock, call, create_autospec, patch

import anyio
import pytest

if TYPE_CHECKING:
    from collections.abc import Generator

    from griptape_nodes.node_library.workflow_registry import WorkflowMetadata
    from griptape_nodes.retained_mode.events.node_events import SerializedNodeCommands

from griptape_nodes.exe_types.core_types import Parameter
from griptape_nodes.exe_types.node_types import NodeDependencies
from griptape_nodes.node_library.workflow_registry import Workflow, WorkflowMetadata, WorkflowRegistry, WorkflowShape
from griptape_nodes.retained_mode.events.base_events import ResultDetails
from griptape_nodes.retained_mode.events.flow_events import SerializedFlowCommands
from griptape_nodes.retained_mode.events.workflow_events import (
    CreateWorkflowFromTemplateRequest,
    CreateWorkflowFromTemplateResultFailure,
    CreateWorkflowFromTemplateResultSuccess,
    DeleteWorkflowRequest,
    DeleteWorkflowResultSuccess,
    GetWorkflowInfoRequest,
    GetWorkflowInfoResultFailure,
    GetWorkflowInfoResultSuccess,
    GetWorkflowMetadataRequest,
    GetWorkflowMetadataResultFailure,
    GetWorkflowMetadataResultSuccess,
    ImportWorkflowAsReferencedSubFlowRequest,
    ImportWorkflowAsReferencedSubFlowResultSuccess,
    ImportWorkflowRequest,
    ImportWorkflowResultFailure,
    ImportWorkflowResultSuccess,
    ListAllWorkflowInfoRequest,
    ListAllWorkflowInfoResultFailure,
    ListAllWorkflowInfoResultSuccess,
    LoadWorkflowMetadataResultFailure,
    LoadWorkflowMetadataResultSuccess,
    MoveWorkflowRequest,
    MoveWorkflowResultFailure,
    MoveWorkflowResultSuccess,
    RegisterWorkflowResultFailure,
    RegisterWorkflowResultSuccess,
    SetWorkflowMetadataRequest,
    SetWorkflowMetadataResultSuccess,
    WorkflowDependencyInfo,
    WorkflowDependencyStatus,
    WorkflowInfoSummary,
    WorkflowStatus,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.context_manager import ContextManager
from griptape_nodes.retained_mode.managers.flow_manager import FlowManager
from griptape_nodes.retained_mode.managers.object_manager import ObjectManager
from griptape_nodes.retained_mode.managers.workflow_manager import WorkflowManager


def _register_unsaved_workflow(key: str, name: str) -> None:
    metadata = WorkflowMetadata(
        name=name,
        schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
        engine_version_created_with="",
        node_libraries_referenced=[],
        creation_date=datetime.now(UTC),
    )
    WorkflowRegistry.generate_new_workflow(registry_key=key, metadata=metadata, file_path=None)


class TestWorkflowManager:
    """Test WorkflowManager functionality including parameter serialization."""

    def test_workflow_metadata_is_internal_round_trip(self) -> None:
        """is_internal defaults to False, parses from headers, and survives model_dump()."""
        base_kwargs = {
            "name": "wf",
            "schema_version": WorkflowMetadata.LATEST_SCHEMA_VERSION,
            "engine_version_created_with": "",
            "node_libraries_referenced": [],
        }

        # Absent key -> default False (old headers without the line stay visible).
        assert WorkflowMetadata(**base_kwargs).is_internal is False

        # Parsed from a header table (mirrors the TOML [tool.griptape-nodes] path).
        parsed = WorkflowMetadata.model_validate({**base_kwargs, "is_internal": True})
        assert parsed.is_internal is True

        # Survives model_dump() so it reaches list_workflows() -> the GUI.
        assert parsed.model_dump()["is_internal"] is True

    def test_convert_parameter_to_minimal_dict_serializes_settable_correctly(self) -> None:
        """Test that _convert_parameter_to_minimal_dict properly serializes settable as boolean."""
        # Create a test parameter
        param = Parameter(
            name="test_param",
            tooltip="Test parameter",
            type="str",
            default_value="test_value",
            settable=True,
            user_defined=False,
        )

        # Call the method under test
        result = WorkflowManager._convert_parameter_to_minimal_dict(param)

        # Assert that settable is properly serialized as a boolean
        assert "settable" in result
        assert isinstance(result["settable"], bool)
        assert result["settable"] is True

        # Assert that is_user_defined is properly serialized as a boolean
        assert "is_user_defined" in result
        assert isinstance(result["is_user_defined"], bool)

        # Assert that other expected fields are present
        assert result["name"] == "test_param"
        assert result["tooltip"] == "Test parameter"
        assert result["type"] == "str"
        assert result["default_value"] == "test_value"

    def test_convert_parameter_to_minimal_dict_handles_false_settable(self) -> None:
        """Test that _convert_parameter_to_minimal_dict handles settable=False correctly."""
        # Create a test parameter with settable=False
        param = Parameter(
            name="readonly_param", tooltip="Read-only parameter", type="int", settable=False, user_defined=True
        )

        # Call the method under test
        result = WorkflowManager._convert_parameter_to_minimal_dict(param)

        # Assert that settable is properly serialized as False
        assert "settable" in result
        assert isinstance(result["settable"], bool)
        assert result["settable"] is False

        # Assert that is_user_defined is properly serialized as True
        assert "is_user_defined" in result
        assert isinstance(result["is_user_defined"], bool)
        assert result["is_user_defined"] is True

    def test_on_import_workflow_request_success(self, griptape_nodes: GriptapeNodes) -> None:
        """Test successful workflow import."""
        workflow_manager = griptape_nodes.WorkflowManager()
        request = ImportWorkflowRequest(file_path="/path/to/workflow.py")

        mock_metadata = MagicMock()
        mock_metadata.name = "test_workflow"

        with (
            patch.object(
                workflow_manager,
                "on_load_workflow_metadata_request",
                AsyncMock(
                    return_value=LoadWorkflowMetadataResultSuccess(metadata=mock_metadata, result_details="Success")
                ),
            ),
            patch.object(WorkflowRegistry, "has_workflow_with_name", return_value=False),
            patch.object(
                workflow_manager,
                "on_register_workflow_request",
                # Registry key is the file path (minus extension), independent of metadata.name.
                return_value=RegisterWorkflowResultSuccess(workflow_name="workflow", result_details="Success"),
            ),
            patch.object(WorkflowRegistry, "get_complete_file_path", return_value="/full/path/to/workflow.py"),
        ):
            result = asyncio.run(workflow_manager.on_import_workflow_request(request))

            assert isinstance(result, ImportWorkflowResultSuccess)
            # Registry key is derived from the file path (minus extension), not from metadata.name.
            assert result.workflow_name == "workflow"

    def test_on_import_workflow_request_already_registered(self, griptape_nodes: GriptapeNodes) -> None:
        """Test import when workflow is already registered."""
        workflow_manager = griptape_nodes.WorkflowManager()
        request = ImportWorkflowRequest(file_path="/path/to/workflow.py")

        mock_metadata = MagicMock()
        mock_metadata.name = "test_workflow"

        with (
            patch.object(
                workflow_manager,
                "on_load_workflow_metadata_request",
                AsyncMock(
                    return_value=LoadWorkflowMetadataResultSuccess(metadata=mock_metadata, result_details="Success")
                ),
            ),
            patch.object(WorkflowRegistry, "has_workflow_with_name", return_value=True),
        ):
            result = asyncio.run(workflow_manager.on_import_workflow_request(request))

            assert isinstance(result, ImportWorkflowResultSuccess)
            # Registry key is derived from the file path (minus extension), not from metadata.name.
            assert result.workflow_name == "/path/to/workflow"

    def test_on_import_workflow_request_metadata_load_failure(self, griptape_nodes: GriptapeNodes) -> None:
        """Test import when metadata loading fails."""
        workflow_manager = griptape_nodes.WorkflowManager()
        request = ImportWorkflowRequest(file_path="/path/to/workflow.py")

        with patch.object(
            workflow_manager,
            "on_load_workflow_metadata_request",
            AsyncMock(return_value=LoadWorkflowMetadataResultFailure(result_details="Failed to load metadata")),
        ):
            result = asyncio.run(workflow_manager.on_import_workflow_request(request))

            assert isinstance(result, ImportWorkflowResultFailure)
            assert isinstance(result.result_details, ResultDetails)
            assert result.result_details.result_details[0].message == "Failed to load metadata"

    def test_on_import_workflow_request_registration_failure(self, griptape_nodes: GriptapeNodes) -> None:
        """Test import when registration fails."""
        workflow_manager = griptape_nodes.WorkflowManager()
        request = ImportWorkflowRequest(file_path="/path/to/workflow.py")

        mock_metadata = MagicMock()
        mock_metadata.name = "test_workflow"

        with (
            patch.object(
                workflow_manager,
                "on_load_workflow_metadata_request",
                AsyncMock(
                    return_value=LoadWorkflowMetadataResultSuccess(metadata=mock_metadata, result_details="Success")
                ),
            ),
            patch.object(WorkflowRegistry, "has_workflow_with_name", return_value=False),
            patch.object(
                workflow_manager,
                "on_register_workflow_request",
                return_value=RegisterWorkflowResultFailure(result_details="Registration failed"),
            ),
        ):
            result = asyncio.run(workflow_manager.on_import_workflow_request(request))

            assert isinstance(result, ImportWorkflowResultFailure)
            assert isinstance(result.result_details, ResultDetails)
            assert result.result_details.result_details[0].message == "Registration failed"

    def test_get_workflow_metadata_success(self, griptape_nodes: GriptapeNodes) -> None:
        """Ensure GetWorkflowMetadataRequest returns workflow.metadata directly."""
        workflow_manager = griptape_nodes.WorkflowManager()
        request = GetWorkflowMetadataRequest(workflow_name="my_workflow")

        mock_metadata = MagicMock()
        mock_workflow = MagicMock()
        mock_workflow.metadata = mock_metadata

        with patch.object(WorkflowRegistry, "get_workflow_by_name", return_value=mock_workflow):
            result = workflow_manager.on_get_workflow_metadata_request(request)

        assert isinstance(result, GetWorkflowMetadataResultSuccess)
        assert result.workflow_metadata is mock_metadata

    def test_get_workflow_metadata_not_found(self, griptape_nodes: GriptapeNodes) -> None:
        """Ensure GetWorkflowMetadataRequest returns failure when workflow missing."""
        workflow_manager = griptape_nodes.WorkflowManager()
        request = GetWorkflowMetadataRequest(workflow_name="missing_workflow")

        with patch.object(WorkflowRegistry, "get_workflow_by_name", side_effect=KeyError("not found")):
            result = workflow_manager.on_get_workflow_metadata_request(request)

        assert isinstance(result, GetWorkflowMetadataResultFailure)

    def test_set_workflow_metadata_success(self, griptape_nodes: GriptapeNodes) -> None:
        """Ensure SetWorkflowMetadataRequest replaces metadata and persists header."""
        workflow_manager = griptape_nodes.WorkflowManager()
        workflow_manager._workflows_loading_complete.set()  # type: ignore[attr-defined]

        # Provide a full metadata object (mock is fine as we stub header replacement)
        mock_new_metadata = MagicMock()
        mock_new_metadata.name = "my_workflow"
        request = SetWorkflowMetadataRequest(workflow_name="my_workflow", workflow_metadata=mock_new_metadata)

        mock_workflow = MagicMock()
        mock_workflow.file_path = "workflows/my_workflow.py"
        existing_content = "# /// script\n# [tool]\n# ///\nprint('body')\n"

        with (
            patch.object(WorkflowRegistry, "get_workflow_by_name", return_value=mock_workflow),
            patch.object(WorkflowRegistry, "get_complete_file_path", return_value="/workspace/my_workflow.py"),
            patch.object(Path, "is_file", return_value=True),
            patch.object(anyio.Path, "read_text", AsyncMock(return_value=existing_content)),
            patch.object(workflow_manager, "_replace_workflow_metadata_header", return_value="updated"),
            patch.object(
                workflow_manager,
                "_write_workflow_file",
                return_value=WorkflowManager.WriteWorkflowFileResult(success=True, error_details=""),
            ) as write_mock,
        ):
            result = asyncio.run(workflow_manager.on_set_workflow_metadata_request(request))  # type: ignore[attr-defined]

        assert isinstance(result, SetWorkflowMetadataResultSuccess)
        write_mock.assert_called_once()

    def test_on_create_workflow_from_template_request_success(self, griptape_nodes: GriptapeNodes) -> None:
        """Test successful create workflow from template (Griptape or user-provided)."""
        workflow_manager = griptape_nodes.WorkflowManager()
        request = CreateWorkflowFromTemplateRequest(template_name="my_template")

        mock_template = MagicMock()
        mock_template.file_path = "libraries/lib/workflows/templates/my_template.py"
        mock_template.metadata = MagicMock()
        mock_template.metadata.is_template = True
        mock_template.metadata.schema_version = "0.16.0"
        mock_template.metadata.engine_version_created_with = "1.0.0"
        mock_template.metadata.node_libraries_referenced = []
        mock_template.metadata.node_types_used = set()
        mock_template.metadata.workflows_referenced = None
        mock_template.metadata.description = "A template"
        mock_template.metadata.image = None
        mock_template.metadata.last_modified_date = None

        template_content = "# /// script\n# [tool]\n# ///\nprint('body')\n"
        new_full_path = "/workspace/my_template_1.py"

        def get_complete_file_path(relative_path: str) -> str:
            if "templates" in relative_path:
                return "/lib/path/my_template.py"
            return new_full_path

        with (
            patch.object(
                WorkflowRegistry,
                "get_workflow_by_name",
                return_value=mock_template,
            ),
            patch.object(
                WorkflowRegistry,
                "get_complete_file_path",
                side_effect=get_complete_file_path,
            ),
            patch.object(Path, "is_file", return_value=True),
            patch.object(Path, "read_text", return_value=template_content),
            patch.object(
                workflow_manager,
                "_generate_unique_filename",
                return_value="my_template_1",
            ),
            patch.object(
                workflow_manager,
                "_replace_workflow_metadata_header",
                return_value="updated_content",
            ),
            patch.object(Path, "write_text"),
            patch.object(WorkflowRegistry, "generate_new_workflow"),
        ):
            result = workflow_manager.on_create_workflow_from_template_request(request)

        assert isinstance(result, CreateWorkflowFromTemplateResultSuccess)
        assert result.workflow_name == "my_template_1"
        assert result.file_path == new_full_path

    def test_on_create_workflow_from_template_request_absolute_file_path(self, griptape_nodes: GriptapeNodes) -> None:
        """Test that templates with absolute file paths save the new workflow in the workspace, not at the template path."""
        workflow_manager = griptape_nodes.WorkflowManager()
        request = CreateWorkflowFromTemplateRequest(
            template_name="/some/external/library/workflows/templates/my_template"
        )

        mock_template = MagicMock()
        mock_template.file_path = "/some/external/library/workflows/templates/my_template.py"
        mock_template.metadata = MagicMock()
        mock_template.metadata.is_template = True
        mock_template.metadata.schema_version = "0.16.0"
        mock_template.metadata.engine_version_created_with = "1.0.0"
        mock_template.metadata.node_libraries_referenced = []
        mock_template.metadata.node_types_used = set()
        mock_template.metadata.workflows_referenced = None
        mock_template.metadata.description = "A template"
        mock_template.metadata.image = None
        mock_template.metadata.last_modified_date = None

        template_content = "# /// script\n# [tool]\n# ///\nprint('body')\n"
        new_full_path = "/workspace/my_template.py"

        generate_unique_filename_calls = []

        def capture_generate_unique_filename(base_name: str) -> str:
            generate_unique_filename_calls.append(base_name)
            return "my_template"

        with (
            patch.object(
                WorkflowRegistry,
                "get_workflow_by_name",
                return_value=mock_template,
            ),
            patch.object(
                WorkflowRegistry,
                "get_complete_file_path",
                return_value=new_full_path,
            ),
            patch.object(Path, "is_file", return_value=True),
            patch.object(Path, "read_text", return_value=template_content),
            patch.object(
                workflow_manager,
                "_generate_unique_filename",
                side_effect=capture_generate_unique_filename,
            ),
            patch.object(
                workflow_manager,
                "_replace_workflow_metadata_header",
                return_value="updated_content",
            ),
            patch.object(Path, "write_text"),
            patch.object(WorkflowRegistry, "generate_new_workflow"),
        ):
            result = workflow_manager.on_create_workflow_from_template_request(request)

        assert isinstance(result, CreateWorkflowFromTemplateResultSuccess)
        # The base name passed to _generate_unique_filename must be just the stem,
        # not the full absolute path, so the file is saved in the workspace.
        assert generate_unique_filename_calls == ["my_template"]

    def test_on_create_workflow_from_template_request_template_not_found(self, griptape_nodes: GriptapeNodes) -> None:
        """Test create from template when template is not in registry."""
        workflow_manager = griptape_nodes.WorkflowManager()
        request = CreateWorkflowFromTemplateRequest(template_name="missing_template")

        with patch.object(
            WorkflowRegistry,
            "get_workflow_by_name",
            side_effect=KeyError("not found"),
        ):
            result = workflow_manager.on_create_workflow_from_template_request(request)

        assert isinstance(result, CreateWorkflowFromTemplateResultFailure)
        assert "missing_template" in str(result.result_details)

    def test_on_create_workflow_from_template_request_not_a_template(self, griptape_nodes: GriptapeNodes) -> None:
        """Test create from template when workflow is not marked as template."""
        workflow_manager = griptape_nodes.WorkflowManager()
        request = CreateWorkflowFromTemplateRequest(template_name="regular_workflow")

        mock_workflow = MagicMock()
        mock_workflow.file_path = "workflows/regular_workflow.py"
        mock_workflow.metadata = MagicMock()
        mock_workflow.metadata.is_template = False

        with patch.object(
            WorkflowRegistry,
            "get_workflow_by_name",
            return_value=mock_workflow,
        ):
            result = workflow_manager.on_create_workflow_from_template_request(request)

        assert isinstance(result, CreateWorkflowFromTemplateResultFailure)
        assert "not marked as a template" in str(result.result_details)

    def test_on_create_workflow_from_template_request_template_file_not_found(
        self, griptape_nodes: GriptapeNodes
    ) -> None:
        """Test create from template when template file does not exist."""
        workflow_manager = griptape_nodes.WorkflowManager()
        request = CreateWorkflowFromTemplateRequest(template_name="my_template")

        mock_template = MagicMock()
        mock_template.file_path = "libraries/lib/workflows/templates/my_template.py"
        mock_template.metadata = MagicMock()
        mock_template.metadata.is_template = True

        with (
            patch.object(
                WorkflowRegistry,
                "get_workflow_by_name",
                return_value=mock_template,
            ),
            patch.object(WorkflowRegistry, "get_complete_file_path", return_value="/missing/path.py"),
            patch.object(Path, "is_file", return_value=False),
        ):
            result = workflow_manager.on_create_workflow_from_template_request(request)

        assert isinstance(result, CreateWorkflowFromTemplateResultFailure)
        assert "does not exist" in str(result.result_details)

    # Removed tests for invalid keys/types; metadata is replaced as a whole object

    def test_on_move_workflow_request_workflow_not_found(self, griptape_nodes: GriptapeNodes) -> None:
        workflow_manager = griptape_nodes.WorkflowManager()
        request = MoveWorkflowRequest(workflow_name="nonexistent", target_directory="subdir")

        with patch.object(WorkflowRegistry, "get_workflow_by_name", side_effect=KeyError("not found")):
            result = workflow_manager.on_move_workflow_request(request)

        assert isinstance(result, MoveWorkflowResultFailure)
        assert "nonexistent" in str(result.result_details)

    def test_on_move_workflow_request_source_file_missing(self, griptape_nodes: GriptapeNodes) -> None:
        workflow_manager = griptape_nodes.WorkflowManager()
        request = MoveWorkflowRequest(workflow_name="my_workflow", target_directory="subdir")

        mock_workflow = MagicMock()
        mock_workflow.file_path = "my_workflow.py"

        with (
            patch.object(WorkflowRegistry, "get_workflow_by_name", return_value=mock_workflow),
            patch.object(WorkflowRegistry, "get_complete_file_path", return_value="/workspace/my_workflow.py"),
            patch.object(Path, "exists", return_value=False),
        ):
            result = workflow_manager.on_move_workflow_request(request)

        assert isinstance(result, MoveWorkflowResultFailure)
        assert "/workspace/my_workflow.py" in str(result.result_details)

    def test_on_move_workflow_request_target_already_exists(self, griptape_nodes: GriptapeNodes) -> None:
        workflow_manager = griptape_nodes.WorkflowManager()
        request = MoveWorkflowRequest(workflow_name="my_workflow", target_directory="subdir")

        mock_workflow = MagicMock()
        mock_workflow.file_path = "my_workflow.py"

        with (
            patch.object(WorkflowRegistry, "get_workflow_by_name", return_value=mock_workflow),
            patch.object(WorkflowRegistry, "get_complete_file_path", return_value="/workspace/my_workflow.py"),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "mkdir"),
        ):
            result = workflow_manager.on_move_workflow_request(request)

        assert isinstance(result, MoveWorkflowResultFailure)
        assert "already exists" in str(result.result_details)

    def test_on_move_workflow_request_success_directory_change(self, griptape_nodes: GriptapeNodes) -> None:
        workflow_manager = griptape_nodes.WorkflowManager()
        request = MoveWorkflowRequest(workflow_name="my_workflow", target_directory="subdir")

        mock_workflow = MagicMock()
        mock_workflow.file_path = "my_workflow.py"

        config_mgr = griptape_nodes.ConfigManager()
        with (
            patch.object(WorkflowRegistry, "get_workflow_by_name", return_value=mock_workflow),
            patch.object(WorkflowRegistry, "get_complete_file_path", return_value="/workspace/my_workflow.py"),
            patch.object(Path, "exists", side_effect=[True, False]),
            patch.object(Path, "mkdir"),
            patch.object(Path, "rename"),
            patch.object(WorkflowRegistry, "rekey_workflow") as mock_rekey,
            patch.object(config_mgr, "delete_user_workflow"),
        ):
            result = workflow_manager.on_move_workflow_request(request)

        assert isinstance(result, MoveWorkflowResultSuccess)
        assert result.moved_file_path == "subdir/my_workflow.py"
        assert result.new_workflow_name == "subdir/my_workflow"
        mock_rekey.assert_called_once_with("my_workflow", "subdir/my_workflow")

    def test_on_move_workflow_request_no_rekey_same_directory(self, griptape_nodes: GriptapeNodes) -> None:
        """Moving within the same directory level produces the same registry key; no rekey occurs."""
        workflow_manager = griptape_nodes.WorkflowManager()
        # Workflow already in "subdir", moving target is also "subdir" — key stays the same.
        request = MoveWorkflowRequest(workflow_name="subdir/my_workflow", target_directory="subdir")

        mock_workflow = MagicMock()
        mock_workflow.file_path = "subdir/my_workflow.py"

        config_mgr = griptape_nodes.ConfigManager()
        with (
            patch.object(WorkflowRegistry, "get_workflow_by_name", return_value=mock_workflow),
            patch.object(WorkflowRegistry, "get_complete_file_path", return_value="/workspace/subdir/my_workflow.py"),
            patch.object(Path, "exists", side_effect=[True, False]),
            patch.object(Path, "mkdir"),
            patch.object(Path, "rename"),
            patch.object(WorkflowRegistry, "rekey_workflow") as mock_rekey,
            patch.object(config_mgr, "delete_user_workflow"),
        ):
            result = workflow_manager.on_move_workflow_request(request)

        assert isinstance(result, MoveWorkflowResultSuccess)
        assert result.new_workflow_name == "subdir/my_workflow"
        mock_rekey.assert_not_called()

    def test_on_move_workflow_request_updates_context_for_current_workflow(self, griptape_nodes: GriptapeNodes) -> None:
        workflow_manager = griptape_nodes.WorkflowManager()
        request = MoveWorkflowRequest(workflow_name="my_workflow", target_directory="subdir")

        mock_workflow = MagicMock()
        mock_workflow.file_path = "my_workflow.py"

        context_mgr = griptape_nodes.ContextManager()
        config_mgr = griptape_nodes.ConfigManager()
        with (
            patch.object(WorkflowRegistry, "get_workflow_by_name", return_value=mock_workflow),
            patch.object(WorkflowRegistry, "get_complete_file_path", return_value="/workspace/my_workflow.py"),
            patch.object(Path, "exists", side_effect=[True, False]),
            patch.object(Path, "mkdir"),
            patch.object(Path, "rename"),
            patch.object(WorkflowRegistry, "rekey_workflow"),
            patch.object(config_mgr, "delete_user_workflow"),
            patch.object(context_mgr, "has_current_workflow", return_value=True),
            patch.object(context_mgr, "get_current_workflow_name", return_value="my_workflow"),
            patch.object(context_mgr, "set_current_workflow_name") as mock_set_name,
        ):
            result = workflow_manager.on_move_workflow_request(request)

        assert isinstance(result, MoveWorkflowResultSuccess)
        mock_set_name.assert_called_once_with("subdir/my_workflow")

    def test_on_move_workflow_request_does_not_update_context_for_other_workflow(
        self, griptape_nodes: GriptapeNodes
    ) -> None:
        workflow_manager = griptape_nodes.WorkflowManager()
        request = MoveWorkflowRequest(workflow_name="my_workflow", target_directory="subdir")

        mock_workflow = MagicMock()
        mock_workflow.file_path = "my_workflow.py"

        context_mgr = griptape_nodes.ContextManager()
        config_mgr = griptape_nodes.ConfigManager()
        with (
            patch.object(WorkflowRegistry, "get_workflow_by_name", return_value=mock_workflow),
            patch.object(WorkflowRegistry, "get_complete_file_path", return_value="/workspace/my_workflow.py"),
            patch.object(Path, "exists", side_effect=[True, False]),
            patch.object(Path, "mkdir"),
            patch.object(Path, "rename"),
            patch.object(WorkflowRegistry, "rekey_workflow"),
            patch.object(config_mgr, "delete_user_workflow"),
            patch.object(context_mgr, "has_current_workflow", return_value=True),
            patch.object(context_mgr, "get_current_workflow_name", return_value="other_workflow"),
            patch.object(context_mgr, "set_current_workflow_name") as mock_set_name,
        ):
            result = workflow_manager.on_move_workflow_request(request)

        assert isinstance(result, MoveWorkflowResultSuccess)
        mock_set_name.assert_not_called()

    # --- Save workflow: unsaved -> saved transition ---

    def test_on_save_workflow_rekeys_context_stack_on_first_save(self, griptape_nodes: GriptapeNodes) -> None:
        """First save of an unsaved workflow rekeys the registry entry and updates the context stack in-place."""
        from datetime import UTC, datetime

        from griptape_nodes.node_library.workflow_registry import WorkflowMetadata
        from griptape_nodes.retained_mode.events.flow_events import (
            GetTopLevelFlowResultSuccess,
            SerializedFlowCommands,
            SerializeFlowToCommandsResultSuccess,
        )
        from griptape_nodes.retained_mode.events.workflow_events import (
            SaveWorkflowFileFromSerializedFlowResultSuccess,
            SaveWorkflowRequest,
            SaveWorkflowResultSuccess,
        )

        workflow_manager = griptape_nodes.WorkflowManager()
        context_manager = griptape_nodes.ContextManager()

        unsaved_key = "unsaved:abc-123"
        saved_key = "my_flow"

        with patch.dict(WorkflowRegistry._workflows, {}, clear=True):
            _register_unsaved_workflow(key=unsaved_key, name="Untitled")
            unsaved_workflow = WorkflowRegistry.get_workflow_by_name(unsaved_key)
            context_manager.push_workflow(workflow_name=unsaved_key)

            empty_commands = SerializedFlowCommands(
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

            saved_metadata = WorkflowMetadata(
                name="My Flow",
                schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
                engine_version_created_with="test",
                node_libraries_referenced=[],
                creation_date=datetime.now(UTC),
            )

            async def fake_ahandle_request(req: object) -> object:
                from griptape_nodes.retained_mode.events.flow_events import (
                    GetTopLevelFlowRequest,
                    SerializeFlowToCommandsRequest,
                )

                if isinstance(req, GetTopLevelFlowRequest):
                    return GetTopLevelFlowResultSuccess(flow_name="ControlFlow_1", result_details="ok")
                if isinstance(req, SerializeFlowToCommandsRequest):
                    return SerializeFlowToCommandsResultSuccess(
                        serialized_flow_commands=empty_commands, result_details="ok"
                    )
                msg = f"Unexpected request type in test: {type(req).__name__}"
                raise AssertionError(msg)

            workspace = griptape_nodes.ConfigManager().workspace_path
            saved_full_path = workspace / f"{saved_key}.py"
            save_file_success = SaveWorkflowFileFromSerializedFlowResultSuccess(
                file_path=str(saved_full_path),
                workflow_metadata=saved_metadata,
                result_details="ok",
            )

            try:
                with (
                    patch.object(GriptapeNodes, "ahandle_request", side_effect=fake_ahandle_request),
                    patch.object(
                        workflow_manager,
                        "_save_workflow_file_inline",
                        return_value=save_file_success,
                    ),
                    patch.object(
                        workflow_manager,
                        "extract_workflow_shape",
                        side_effect=ValueError("no shape"),
                    ),
                ):
                    result = asyncio.run(
                        workflow_manager.on_save_workflow_request(SaveWorkflowRequest(file_name=saved_key))
                    )

                assert isinstance(result, SaveWorkflowResultSuccess)
                assert result.workflow_name == saved_key

                # Registry: unsaved entry rekeyed, saved entry points at the same Workflow instance
                assert unsaved_key not in WorkflowRegistry._workflows
                assert saved_key in WorkflowRegistry._workflows
                assert WorkflowRegistry._workflows[saved_key] is unsaved_workflow
                assert unsaved_workflow.file_path == f"{saved_key}.py"

                # Context stack: the active workflow name is the new key
                assert context_manager.get_current_workflow_name() == saved_key
            finally:
                if context_manager.has_current_workflow():
                    context_manager.pop_workflow()

    def test_on_set_workflow_metadata_updates_unsaved_workflow_in_memory(self, griptape_nodes: GriptapeNodes) -> None:
        """SetWorkflowMetadataRequest on an unsaved workflow updates registry metadata without touching disk."""
        workflow_manager = griptape_nodes.WorkflowManager()
        workflow_manager._workflows_loading_complete.set()

        unsaved_key = "unsaved:meta-test"

        with patch.dict(WorkflowRegistry._workflows, {}, clear=True):
            _register_unsaved_workflow(key=unsaved_key, name="Untitled")
            workflow = WorkflowRegistry.get_workflow_by_name(unsaved_key)
            assert workflow.file_path is None

            result = asyncio.run(
                workflow_manager.on_set_workflow_metadata_request(
                    SetWorkflowMetadataRequest(
                        workflow_name=unsaved_key,
                        workflow_metadata={"name": "my_flow", "description": "hello"},  # type: ignore[arg-type]
                    )
                )
            )

            assert isinstance(result, SetWorkflowMetadataResultSuccess)
            assert workflow.metadata.name == "my_flow"
            assert workflow.metadata.description == "hello"
            # Still unsaved — no disk file materialized.
            assert workflow.file_path is None

    def test_first_save_uses_display_name_when_requested_name_is_unsaved_key(
        self, griptape_nodes: GriptapeNodes
    ) -> None:
        """First save of an unsaved workflow derives the filename from metadata.name, not the synthetic key."""
        from griptape_nodes.retained_mode.events.flow_events import (
            GetTopLevelFlowResultSuccess,
            SerializedFlowCommands,
            SerializeFlowToCommandsResultSuccess,
        )
        from griptape_nodes.retained_mode.events.workflow_events import (
            SaveWorkflowFileFromSerializedFlowResultSuccess,
            SaveWorkflowRequest,
            SaveWorkflowResultSuccess,
        )

        workflow_manager = griptape_nodes.WorkflowManager()
        workflow_manager._workflows_loading_complete.set()
        context_manager = griptape_nodes.ContextManager()

        unsaved_key = "unsaved:filename-test"
        display_name = "workflow_25"

        with patch.dict(WorkflowRegistry._workflows, {}, clear=True):
            _register_unsaved_workflow(key=unsaved_key, name=display_name)
            context_manager.push_workflow(workflow_name=unsaved_key)

            empty_commands = SerializedFlowCommands(
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

            saved_metadata = WorkflowMetadata(
                name=display_name,
                schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
                engine_version_created_with="test",
                node_libraries_referenced=[],
                creation_date=datetime.now(UTC),
            )

            async def fake_ahandle_request(req: object) -> object:
                from griptape_nodes.retained_mode.events.flow_events import (
                    GetTopLevelFlowRequest,
                    SerializeFlowToCommandsRequest,
                )

                if isinstance(req, GetTopLevelFlowRequest):
                    return GetTopLevelFlowResultSuccess(flow_name="ControlFlow_1", result_details="ok")
                if isinstance(req, SerializeFlowToCommandsRequest):
                    return SerializeFlowToCommandsResultSuccess(
                        serialized_flow_commands=empty_commands, result_details="ok"
                    )
                msg = f"Unexpected request type in test: {type(req).__name__}"
                raise AssertionError(msg)

            captured: dict[str, object] = {}

            def fake_save_file(**kwargs: object) -> object:
                captured["file_name"] = kwargs.get("file_name")
                captured["display_name"] = kwargs.get("display_name")
                captured["destination"] = kwargs.get("destination")
                fake_destination: object = kwargs.get("destination")
                resolve = getattr(fake_destination, "resolve", None)
                target_path = resolve() if callable(resolve) else str(fake_destination)
                return SaveWorkflowFileFromSerializedFlowResultSuccess(
                    file_path=str(target_path),
                    workflow_metadata=saved_metadata,
                    result_details="ok",
                )

            workspace = griptape_nodes.ConfigManager().workspace_path

            def fake_resolve_destination(file_name: str, situation: str, **_vars: object) -> MagicMock:  # noqa: ARG001
                stub = MagicMock()
                stub.resolve.return_value = str(workspace / file_name)
                return stub

            try:
                with (
                    patch.object(GriptapeNodes, "ahandle_request", side_effect=fake_ahandle_request),
                    patch.object(
                        workflow_manager,
                        "_save_workflow_file_inline",
                        side_effect=fake_save_file,
                    ),
                    patch.object(
                        workflow_manager,
                        "extract_workflow_shape",
                        side_effect=ValueError("no shape"),
                    ),
                    patch(
                        "griptape_nodes.retained_mode.managers.workflow_manager.ProjectFileDestination.from_situation",
                        side_effect=fake_resolve_destination,
                    ),
                ):
                    # Caller passes the synthetic unsaved key as file_name (matches the
                    # frontend saveWorkflowWithoutModal behavior). Backend should strip it
                    # and use metadata.name as the filename stem.
                    result = asyncio.run(
                        workflow_manager.on_save_workflow_request(SaveWorkflowRequest(file_name=unsaved_key))
                    )

                assert isinstance(result, SaveWorkflowResultSuccess)
                assert captured["file_name"] == display_name
                # Regression guard: metadata.name on disk (= display_name kwarg to
                # _save_workflow_file_inline) must NOT be the synthetic unsaved-key. Prior
                # bug read request.file_name directly for the display-name fallback,
                # which leaked "unsaved:<uuid>" straight into metadata.name.
                assert captured["display_name"] == display_name
                assert unsaved_key not in str(captured["display_name"] or "")
                destination_repr = str(captured["destination"]) if "destination" in captured else ""
                assert unsaved_key not in destination_repr
            finally:
                if context_manager.has_current_workflow():
                    context_manager.pop_workflow()

    class _RenameScenario(NamedTuple):
        workflow_name: str
        requested_name: str
        source_file_path: str
        save_file_path: str
        save_workflow_name: str
        source_display_name: str = "source-display-name"

    def _run_rename(
        self,
        workflow_manager: WorkflowManager,
        scenario: "TestWorkflowManager._RenameScenario",
        *,
        request_kwargs: dict[str, Any] | None = None,
    ) -> dict:
        """Drive on_rename_workflow_request with mocked save/delete, capturing the SaveWorkflowRequest.

        ``request_kwargs`` is merged into the RenameWorkflowRequest constructor so individual tests can
        exercise display_name_behavior / display_name without changing the shared scenario tuple.
        """
        from griptape_nodes.retained_mode.events.workflow_events import (
            DeleteWorkflowResultSuccess,
            RenameWorkflowRequest,
            RenameWorkflowResultSuccess,
            SaveWorkflowRequest,
            SaveWorkflowResultSuccess,
        )

        mock_source = MagicMock()
        mock_source.file_path = scenario.source_file_path
        mock_source.metadata.name = scenario.source_display_name
        captured: dict[str, object] = {}

        async def fake_ahandle_request(req: object) -> object:
            if isinstance(req, SaveWorkflowRequest):
                captured["save_file_name"] = req.file_name
                captured["save_display_name"] = req.display_name
                return SaveWorkflowResultSuccess(
                    file_path=scenario.save_file_path,
                    workflow_name=scenario.save_workflow_name,
                    result_details="ok",
                )
            return DeleteWorkflowResultSuccess(result_details="ok")

        with (
            patch.object(WorkflowRegistry, "has_workflow_with_name", return_value=True),
            patch.object(WorkflowRegistry, "get_workflow_by_name", return_value=mock_source),
            patch.object(workflow_manager, "_persist_external_workflow_registration") as mock_persist,
            patch.object(GriptapeNodes, "ahandle_request", side_effect=fake_ahandle_request),
        ):
            result = asyncio.run(
                workflow_manager.on_rename_workflow_request(
                    RenameWorkflowRequest(
                        workflow_name=scenario.workflow_name,
                        requested_name=scenario.requested_name,
                        **(request_kwargs or {}),
                    )
                )
            )

        assert isinstance(result, RenameWorkflowResultSuccess)
        captured["result_new_name"] = result.new_workflow_name
        captured["persist_calls"] = mock_persist.call_args_list
        return captured

    def test_rename_preserves_workspace_subdir(self, griptape_nodes: GriptapeNodes) -> None:
        """Renaming a workflow in a sub-directory keeps it there (bar/workflow -> bar/new_name)."""
        captured = self._run_rename(
            griptape_nodes.WorkflowManager(),
            self._RenameScenario(
                workflow_name="bar/workflow",
                requested_name="new_name",
                source_file_path="bar/workflow.py",
                save_file_path="/workspace/bar/new_name.py",
                save_workflow_name="bar/new_name",
            ),
        )
        assert captured["save_file_name"] == "bar/new_name"

    def test_rename_root_workflow_has_no_directory(self, griptape_nodes: GriptapeNodes) -> None:
        """Renaming a workspace-root workflow has no directory prefix."""
        captured = self._run_rename(
            griptape_nodes.WorkflowManager(),
            self._RenameScenario(
                workflow_name="workflow",
                requested_name="new_name",
                source_file_path="workflow.py",
                save_file_path="/workspace/new_name.py",
                save_workflow_name="new_name",
            ),
        )
        assert captured["save_file_name"] == "new_name"

    def test_rename_preserves_absolute_dir_and_reregisters(self, griptape_nodes: GriptapeNodes) -> None:
        """Renaming an externally-registered (absolute path) workflow keeps it external and re-registers it."""
        captured = self._run_rename(
            griptape_nodes.WorkflowManager(),
            self._RenameScenario(
                workflow_name="/ext/workflow",
                requested_name="new_name",
                source_file_path="/ext/workflow.py",
                save_file_path="/ext/new_name.py",
                save_workflow_name="/ext/new_name",
            ),
        )
        assert captured["save_file_name"] == "/ext/new_name"
        # The new absolute path is handed to the external-registration helper.
        persist_calls = captured["persist_calls"]
        assert persist_calls == [call("/ext/new_name.py")]

    def test_rename_returns_new_registry_key(self, griptape_nodes: GriptapeNodes) -> None:
        """The returned new_workflow_name is the real directory-qualified key, not the bare stem."""
        captured = self._run_rename(
            griptape_nodes.WorkflowManager(),
            self._RenameScenario(
                workflow_name="bar/workflow",
                requested_name="new_name",
                source_file_path="bar/workflow.py",
                save_file_path="/workspace/bar/new_name.py",
                save_workflow_name="bar/new_name",
            ),
        )
        assert captured["result_new_name"] == "bar/new_name"

    def test_rename_default_matches_file_name(self, griptape_nodes: GriptapeNodes) -> None:
        """Default behavior (MATCH_FILE_NAME): display name tracks the new file basename.

        Preserves historical wire behavior — callers who don't set display_name_behavior see the
        same result they did before the enum was introduced.
        """
        captured = self._run_rename(
            griptape_nodes.WorkflowManager(),
            self._RenameScenario(
                workflow_name="my_workflow",
                requested_name="my_workflow_renamed",
                source_file_path="my_workflow.py",
                save_file_path="/workspace/my_workflow_renamed.py",
                save_workflow_name="my_workflow_renamed",
                source_display_name="My Cool Workflow",
            ),
        )
        assert captured["save_display_name"] == "my_workflow_renamed"

    def test_rename_preserve_existing_keeps_display_name(self, griptape_nodes: GriptapeNodes) -> None:
        """PRESERVE_EXISTING opt-in: SaveWorkflowRequest receives the source's metadata.name, not the new file stem.

        Fix for issue #4992 — with this enum value, renaming a workflow whose display name diverges
        from its filename no longer overwrites metadata.name.
        """
        from griptape_nodes.retained_mode.events.workflow_events import RenameDisplayNameBehavior

        captured = self._run_rename(
            griptape_nodes.WorkflowManager(),
            self._RenameScenario(
                workflow_name="my_workflow",
                requested_name="my_workflow_renamed",
                source_file_path="my_workflow.py",
                save_file_path="/workspace/my_workflow_renamed.py",
                save_workflow_name="my_workflow_renamed",
                source_display_name="My Cool Workflow",
            ),
            request_kwargs={"display_name_behavior": RenameDisplayNameBehavior.PRESERVE_EXISTING},
        )
        assert captured["save_display_name"] == "My Cool Workflow"

    def test_rename_override_uses_provided_display_name(self, griptape_nodes: GriptapeNodes) -> None:
        """OVERRIDE forwards the caller-supplied display_name to SaveWorkflowRequest."""
        from griptape_nodes.retained_mode.events.workflow_events import RenameDisplayNameBehavior

        captured = self._run_rename(
            griptape_nodes.WorkflowManager(),
            self._RenameScenario(
                workflow_name="my_workflow",
                requested_name="my_workflow_renamed",
                source_file_path="my_workflow.py",
                save_file_path="/workspace/my_workflow_renamed.py",
                save_workflow_name="my_workflow_renamed",
                source_display_name="My Cool Workflow",
            ),
            request_kwargs={
                "display_name_behavior": RenameDisplayNameBehavior.OVERRIDE,
                "display_name": "Renamed On Purpose",
            },
        )
        assert captured["save_display_name"] == "Renamed On Purpose"

    def test_rename_override_strips_surrounding_whitespace(self, griptape_nodes: GriptapeNodes) -> None:
        """OVERRIDE forwards ``display_name`` stripped so validation and resolution agree on the canonical form.

        Regression coverage: ``_validate_rename_display_name`` gates OVERRIDE on
        ``display_name.strip()`` being truthy, so a padded input like ``"  My Name  "``
        passes validation. Without a matching ``strip()`` in ``_resolve_rename_display_name``,
        the whitespace would leak into ``metadata.name``.
        """
        from griptape_nodes.retained_mode.events.workflow_events import RenameDisplayNameBehavior

        captured = self._run_rename(
            griptape_nodes.WorkflowManager(),
            self._RenameScenario(
                workflow_name="my_workflow",
                requested_name="my_workflow_renamed",
                source_file_path="my_workflow.py",
                save_file_path="/workspace/my_workflow_renamed.py",
                save_workflow_name="my_workflow_renamed",
                source_display_name="My Cool Workflow",
            ),
            request_kwargs={
                "display_name_behavior": RenameDisplayNameBehavior.OVERRIDE,
                "display_name": "  My Name  ",
            },
        )
        assert captured["save_display_name"] == "My Name"

    @pytest.mark.parametrize(
        "non_override_behavior",
        [
            "PRESERVE_EXISTING",
            "MATCH_FILE_NAME",
        ],
    )
    def test_rename_rejects_display_name_without_override(
        self,
        griptape_nodes: GriptapeNodes,
        non_override_behavior: str,
    ) -> None:
        """Supplying display_name with a non-OVERRIDE behavior fails fast so it can't be silently ignored."""
        from griptape_nodes.retained_mode.events.workflow_events import (
            RenameDisplayNameBehavior,
            RenameWorkflowRequest,
            RenameWorkflowResultFailure,
        )

        workflow_manager = griptape_nodes.WorkflowManager()

        async def fake_ahandle_request(req: object) -> object:
            msg = f"Save/delete must not run when display_name is used with non-OVERRIDE; got {type(req).__name__}"
            raise AssertionError(msg)

        with patch.object(GriptapeNodes, "ahandle_request", side_effect=fake_ahandle_request):
            result = asyncio.run(
                workflow_manager.on_rename_workflow_request(
                    RenameWorkflowRequest(
                        workflow_name="my_workflow",
                        requested_name="my_workflow_renamed",
                        display_name_behavior=RenameDisplayNameBehavior[non_override_behavior],
                        display_name="Silently Ignored",
                    )
                )
            )

        assert isinstance(result, RenameWorkflowResultFailure)
        assert "only consulted when display_name_behavior=OVERRIDE" in str(result.result_details)

    @pytest.mark.parametrize("empty_display_name", [None, "", "   "])
    def test_rename_override_rejects_missing_display_name(
        self,
        griptape_nodes: GriptapeNodes,
        empty_display_name: str | None,
    ) -> None:
        """OVERRIDE without a non-empty display_name fails fast without invoking the save pipeline."""
        from griptape_nodes.retained_mode.events.workflow_events import (
            RenameDisplayNameBehavior,
            RenameWorkflowRequest,
            RenameWorkflowResultFailure,
        )

        workflow_manager = griptape_nodes.WorkflowManager()

        async def fake_ahandle_request(req: object) -> object:
            msg = f"Save/delete must not run when OVERRIDE has no display_name; got {type(req).__name__}"
            raise AssertionError(msg)

        with patch.object(GriptapeNodes, "ahandle_request", side_effect=fake_ahandle_request):
            result = asyncio.run(
                workflow_manager.on_rename_workflow_request(
                    RenameWorkflowRequest(
                        workflow_name="my_workflow",
                        requested_name="my_workflow_renamed",
                        display_name_behavior=RenameDisplayNameBehavior.OVERRIDE,
                        display_name=empty_display_name,
                    )
                )
            )

        assert isinstance(result, RenameWorkflowResultFailure)
        assert "OVERRIDE" in str(result.result_details)

    def test_rename_preserve_existing_falls_back_to_requested_name_when_source_display_name_blank(
        self, griptape_nodes: GriptapeNodes
    ) -> None:
        """PRESERVE_EXISTING with a blank source metadata.name falls back to the requested name.

        The source is registered but its ``metadata.name`` is empty (or whitespace-only). Rather
        than propagating that corrupt-ish value onto the renamed workflow — where the save
        handler would treat "" as an explicit override and persist it — degrade gracefully to the
        requested name so the on-disk file still gets a sensible display name.
        """
        from griptape_nodes.retained_mode.events.workflow_events import RenameDisplayNameBehavior

        captured = self._run_rename(
            griptape_nodes.WorkflowManager(),
            self._RenameScenario(
                workflow_name="my_workflow",
                requested_name="my_workflow_renamed",
                source_file_path="my_workflow.py",
                save_file_path="/workspace/my_workflow_renamed.py",
                save_workflow_name="my_workflow_renamed",
                source_display_name="   ",  # whitespace-only: strip()==""
            ),
            request_kwargs={"display_name_behavior": RenameDisplayNameBehavior.PRESERVE_EXISTING},
        )
        assert captured["save_display_name"] == "my_workflow_renamed"

    def test_rename_preserve_existing_falls_back_to_requested_name_when_source_missing(
        self, griptape_nodes: GriptapeNodes
    ) -> None:
        """PRESERVE_EXISTING with an unregistered source workflow falls back to the requested name.

        Save-As-style path: caller asks to rename a workflow that isn't in the registry (e.g. the
        entry was already deleted, or this is a first-save-as flow). We can't preserve a name we
        don't have, so we hand the requested name through to SaveWorkflowRequest — better than
        letting the save chain fall further and derive from the file stem.
        """
        from griptape_nodes.retained_mode.events.workflow_events import (
            RenameDisplayNameBehavior,
            RenameWorkflowRequest,
            RenameWorkflowResultSuccess,
            SaveWorkflowRequest,
            SaveWorkflowResultSuccess,
        )

        workflow_manager = griptape_nodes.WorkflowManager()
        captured: dict[str, object] = {}

        async def fake_ahandle_request(req: object) -> object:
            if isinstance(req, SaveWorkflowRequest):
                captured["save_display_name"] = req.display_name
                return SaveWorkflowResultSuccess(
                    file_path="/workspace/never_registered_renamed.py",
                    workflow_name="never_registered_renamed",
                    result_details="ok",
                )
            msg = f"Unexpected request in save-as fallback test: {type(req).__name__}"
            raise AssertionError(msg)

        with (
            # Force the source lookup to miss — no registry entry to preserve from.
            patch.object(WorkflowRegistry, "has_workflow_with_name", return_value=False),
            patch.object(workflow_manager, "_persist_external_workflow_registration"),
            patch.object(GriptapeNodes, "ahandle_request", side_effect=fake_ahandle_request),
        ):
            result = asyncio.run(
                workflow_manager.on_rename_workflow_request(
                    RenameWorkflowRequest(
                        workflow_name="never_registered",
                        requested_name="never_registered_renamed",
                        display_name_behavior=RenameDisplayNameBehavior.PRESERVE_EXISTING,
                    )
                )
            )

        assert isinstance(result, RenameWorkflowResultSuccess)
        assert captured["save_display_name"] == "never_registered_renamed"

    def test_rename_surfaces_delete_failure_from_bookkeeping(self, griptape_nodes: GriptapeNodes) -> None:
        """When the post-save delete of the old registry entry fails, rename surfaces RenameWorkflowResultFailure.

        Save succeeds, but the follow-up DeleteWorkflowRequest for the old key comes back as
        DeleteWorkflowResultFailure. The bookkeeping helper must translate that into a rename
        failure so the caller doesn't see a bogus success while the registry is half-migrated.
        """
        from griptape_nodes.retained_mode.events.workflow_events import (
            DeleteWorkflowRequest,
            DeleteWorkflowResultFailure,
            RenameWorkflowRequest,
            RenameWorkflowResultFailure,
            SaveWorkflowRequest,
            SaveWorkflowResultSuccess,
        )

        workflow_manager = griptape_nodes.WorkflowManager()
        mock_source = MagicMock()
        mock_source.file_path = "old.py"
        mock_source.metadata.name = "Old Display"

        async def fake_ahandle_request(req: object) -> object:
            if isinstance(req, SaveWorkflowRequest):
                return SaveWorkflowResultSuccess(
                    file_path="/workspace/new.py",
                    workflow_name="new",
                    result_details="ok",
                )
            if isinstance(req, DeleteWorkflowRequest):
                return DeleteWorkflowResultFailure(result_details="registry locked")
            msg = f"Unexpected request in delete-failure test: {type(req).__name__}"
            raise AssertionError(msg)

        with (
            patch.object(WorkflowRegistry, "has_workflow_with_name", return_value=True),
            patch.object(WorkflowRegistry, "get_workflow_by_name", return_value=mock_source),
            patch.object(workflow_manager, "_persist_external_workflow_registration"),
            patch.object(GriptapeNodes, "ahandle_request", side_effect=fake_ahandle_request),
        ):
            result = asyncio.run(
                workflow_manager.on_rename_workflow_request(
                    RenameWorkflowRequest(workflow_name="old", requested_name="new")
                )
            )

        assert isinstance(result, RenameWorkflowResultFailure)
        # Match the quoted-name form the failure message renders so the assertions
        # don't pass on incidental substrings ("renew", "olden", ...).
        assert "'old'" in str(result.result_details)
        assert "'new'" in str(result.result_details)

    def test_resolve_named_save_path_absolute_skips_sub_dirs(self, griptape_nodes: GriptapeNodes) -> None:
        """An absolute requested name routes the full path to _build_workflow_save_path with no sub_dirs."""
        workflow_manager = griptape_nodes.WorkflowManager()
        # Anchor to the current filesystem root so the path is absolute on Windows
        # (which needs a drive letter) as well as POSIX.
        abs_requested = Path(Path.cwd().anchor) / "ext" / "new_name"
        abs_path = abs_requested.with_suffix(".py")
        fake_destination = MagicMock()

        with patch.object(
            workflow_manager,
            "_build_workflow_save_path",
            return_value=WorkflowManager.WorkflowSavePath(
                destination=fake_destination, relative_file_path=str(abs_path)
            ),
        ) as mock_build:
            resolved = workflow_manager._resolve_named_save_path(str(abs_requested))

        mock_build.assert_called_once_with(f"{abs_requested}.py", situation_name="save_workflow")
        assert resolved.file_name == "new_name"
        assert resolved.relative_file_path == str(abs_path)

    def test_resolve_named_save_path_relative_passes_sub_dirs(self, griptape_nodes: GriptapeNodes) -> None:
        """A relative requested name splits into stem + sub_dirs (unchanged behavior)."""
        workflow_manager = griptape_nodes.WorkflowManager()
        fake_destination = MagicMock()

        with patch.object(
            workflow_manager,
            "_build_workflow_save_path",
            return_value=WorkflowManager.WorkflowSavePath(
                destination=fake_destination, relative_file_path=str(Path("team") / "new_name.py")
            ),
        ) as mock_build:
            resolved = workflow_manager._resolve_named_save_path("team/new_name")

        mock_build.assert_called_once_with("new_name.py", sub_dirs="team", situation_name="save_workflow")
        assert resolved.file_name == "new_name"

    def test_delete_active_workflow_clears_context_stack(self, griptape_nodes: GriptapeNodes) -> None:
        """Deleting the active workflow tears down its flows and pops the context stack.

        Regression guard for the "phantom workflow" bug: a frontend that reloads after
        a delete used to see a stale context pointing at the dead registry key.
        """
        from griptape_nodes.exe_types.flow import ControlFlow
        from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess

        workflow_manager = griptape_nodes.WorkflowManager()
        workflow_manager._workflows_loading_complete.set()
        context_manager = griptape_nodes.ContextManager()
        object_manager = griptape_nodes.ObjectManager()

        workflow_key = "unsaved:delete-active"

        with patch.dict(WorkflowRegistry._workflows, {}, clear=True):
            _register_unsaved_workflow(key=workflow_key, name="Untitled")
            context_manager.push_workflow(workflow_name=workflow_key)

            # Create a top-level flow under the active workflow so there's state to tear down.
            create_result = griptape_nodes.handle_request(CreateFlowRequest(parent_flow_name=None))
            assert isinstance(create_result, CreateFlowResultSuccess)
            flow_name = create_result.flow_name
            assert object_manager.has_object_with_name(flow_name)

            try:
                result = asyncio.run(
                    workflow_manager.on_delete_workflows_request(DeleteWorkflowRequest(name=workflow_key))
                )

                assert isinstance(result, DeleteWorkflowResultSuccess)
                # Context stack is fully torn down.
                assert not context_manager.has_current_workflow()
                # Child flow was deleted too; no orphans left in the ObjectManager.
                assert not object_manager.has_object_with_name(flow_name)
                assert not object_manager.get_filtered_subset(type=ControlFlow)
                # Registry entry is gone.
                assert workflow_key not in WorkflowRegistry._workflows
            finally:
                if context_manager.has_current_workflow():
                    context_manager.pop_workflow()

    def test_delete_non_active_workflow_leaves_context_untouched(self, griptape_nodes: GriptapeNodes) -> None:
        """Deleting a workflow that isn't the active one must not touch the context stack.

        Covers the published-workflow subprocess cleanup path, which deletes by key without
        expecting the context stack to change.
        """
        workflow_manager = griptape_nodes.WorkflowManager()
        workflow_manager._workflows_loading_complete.set()
        context_manager = griptape_nodes.ContextManager()

        active_key = "unsaved:keep-me"
        other_key = "unsaved:delete-me"

        with patch.dict(WorkflowRegistry._workflows, {}, clear=True):
            _register_unsaved_workflow(key=active_key, name="Active")
            _register_unsaved_workflow(key=other_key, name="Other")
            context_manager.push_workflow(workflow_name=active_key)

            try:
                result = asyncio.run(
                    workflow_manager.on_delete_workflows_request(DeleteWorkflowRequest(name=other_key))
                )

                assert isinstance(result, DeleteWorkflowResultSuccess)
                # The active workflow is still active.
                assert context_manager.has_current_workflow()
                assert context_manager.get_current_workflow_name() == active_key
                # Only the non-active workflow was removed.
                assert other_key not in WorkflowRegistry._workflows
                assert active_key in WorkflowRegistry._workflows
            finally:
                if context_manager.has_current_workflow():
                    context_manager.pop_workflow()

    @pytest.mark.asyncio
    async def test_startup_scan_skips_unsaved_prefix_files(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        """Leaked `unsaved:<uuid>.py` files on disk must be skipped during the workspace scan.

        Pre-fix saves wrote these files; `_determine_save_target` no longer does, but any
        previously-leaked file must not trip the scanner with `Failed to register workflow`.
        """
        workflow_manager = griptape_nodes.WorkflowManager()

        header = WorkflowManager.WORKFLOW_METADATA_HEADER
        metadata_block = "\n".join(
            [
                f"# /// {header}",
                '# name = "leaked"',
                '# schema_version = "0.7.0"',
                '# engine_version_created_with = "0.0.0"',
                "# node_libraries_referenced = []",
                "# ///",
                "",
            ]
        )

        leaked_path = tmp_path / "unsaved:abc-123.py"
        leaked_path.write_text(metadata_block, encoding="utf-8")
        good_path = tmp_path / "regular_workflow.py"
        good_path.write_text(metadata_block, encoding="utf-8")

        result = await workflow_manager._process_workflows_for_registration([str(tmp_path)])

        scanned_names = {Path(name).name for name in result.succeeded + result.failed}
        assert leaked_path.name not in scanned_names
        # Sanity check: a regular file in the same directory still reaches the processor.
        assert good_path.name in scanned_names

    # --- WorkflowInfo payload helpers ---

    def test_build_workflow_info_key_uses_workspace_join(self, griptape_nodes: GriptapeNodes) -> None:
        """_build_workflow_info_key matches the key construction used when storing info (no symlink resolution)."""
        workflow_manager = griptape_nodes.WorkflowManager()
        workspace = griptape_nodes.ConfigManager().workspace_path

        key = workflow_manager._build_workflow_info_key("workflows/my_workflow.py")

        assert key == str(workspace / "workflows/my_workflow.py")

    def test_build_workflow_info_payload_good_status_no_problems(self, griptape_nodes: GriptapeNodes) -> None:
        """_build_workflow_info_payload produces correct payload for a GOOD workflow with no problems."""
        from griptape_nodes.retained_mode.managers.workflow_manager import WorkflowManager

        workflow_manager = griptape_nodes.WorkflowManager()
        wf_info = WorkflowManager.WorkflowInfo(
            status=WorkflowStatus.GOOD,
            workflow_path="/workspace/workflows/my_workflow.py",
            workflow_name="my_workflow",
        )

        payload = workflow_manager._build_workflow_info_payload(wf_info)

        assert isinstance(payload, WorkflowInfoSummary)
        assert payload.status == "GOOD"
        assert payload.workflow_name == "my_workflow"
        assert payload.workflow_path == "/workspace/workflows/my_workflow.py"
        assert payload.problems == []
        assert payload.workflow_dependencies == []

    def test_build_workflow_info_payload_collates_problems(self, griptape_nodes: GriptapeNodes) -> None:
        """_build_workflow_info_payload calls collate_problems_for_display on each problem type."""
        from griptape_nodes.retained_mode.managers.fitness_problems.workflows.library_not_registered_problem import (
            LibraryNotRegisteredProblem,
        )
        from griptape_nodes.retained_mode.managers.workflow_manager import WorkflowManager

        workflow_manager = griptape_nodes.WorkflowManager()
        wf_info = WorkflowManager.WorkflowInfo(
            status=WorkflowStatus.UNUSABLE,
            workflow_path="/workspace/workflows/broken.py",
            workflow_name="broken",
            problems=[
                LibraryNotRegisteredProblem(library_name="lib-a"),
                LibraryNotRegisteredProblem(library_name="lib-b"),
            ],
        )

        payload = workflow_manager._build_workflow_info_payload(wf_info)

        assert len(payload.problems) == 1
        assert "lib-a" in payload.problems[0]
        assert "lib-b" in payload.problems[0]

    def test_build_workflow_info_payload_includes_dependencies(self, griptape_nodes: GriptapeNodes) -> None:
        """_build_workflow_info_payload passes WorkflowDependencyInfo instances through directly."""
        from griptape_nodes.retained_mode.managers.workflow_manager import WorkflowManager

        workflow_manager = griptape_nodes.WorkflowManager()
        wf_info = WorkflowManager.WorkflowInfo(
            status=WorkflowStatus.FLAWED,
            workflow_path="/workspace/workflows/flawed.py",
            workflow_name="flawed",
            workflow_dependencies=[
                WorkflowDependencyInfo(
                    library_name="my-lib",
                    version_requested="1.0.0",
                    version_present="1.1.0",
                    status=WorkflowDependencyStatus.CAUTION,
                )
            ],
        )

        payload = workflow_manager._build_workflow_info_payload(wf_info)

        assert len(payload.workflow_dependencies) == 1
        dep = payload.workflow_dependencies[0]
        assert isinstance(dep, WorkflowDependencyInfo)
        assert dep.library_name == "my-lib"
        assert dep.version_requested == "1.0.0"
        assert dep.version_present == "1.1.0"
        assert dep.status == "CAUTION"

    # --- GetWorkflowInfoRequest ---

    def test_on_get_workflow_info_request_workflow_not_in_registry_fails(self, griptape_nodes: GriptapeNodes) -> None:
        """GetWorkflowInfoRequest with unknown workflow_name returns failure."""
        workflow_manager = griptape_nodes.WorkflowManager()
        request = GetWorkflowInfoRequest(workflow_name="missing_workflow")

        with patch.object(WorkflowRegistry, "get_workflow_by_name", side_effect=KeyError("not found")):
            result = workflow_manager.on_get_workflow_info_request(request)

        assert isinstance(result, GetWorkflowInfoResultFailure)
        assert "missing_workflow" in str(result.result_details)

    def test_on_get_workflow_info_request_no_info_for_path_fails(self, griptape_nodes: GriptapeNodes) -> None:
        """GetWorkflowInfoRequest returns failure when no WorkflowInfo is stored for the resolved path."""
        workflow_manager = griptape_nodes.WorkflowManager()
        request = GetWorkflowInfoRequest(workflow_name="my_workflow")

        mock_workflow = MagicMock()
        mock_workflow.file_path = "workflows/my_workflow.py"

        with patch.object(WorkflowRegistry, "get_workflow_by_name", return_value=mock_workflow):
            # _workflow_file_path_to_info is empty, so no info will be found
            result = workflow_manager.on_get_workflow_info_request(request)

        assert isinstance(result, GetWorkflowInfoResultFailure)

    def test_on_get_workflow_info_request_success(self, griptape_nodes: GriptapeNodes) -> None:
        """GetWorkflowInfoRequest succeeds when WorkflowInfo exists for the workflow."""
        from griptape_nodes.retained_mode.managers.workflow_manager import WorkflowManager

        workflow_manager = griptape_nodes.WorkflowManager()
        request = GetWorkflowInfoRequest(workflow_name="my_workflow")

        mock_workflow = MagicMock()
        mock_workflow.file_path = "workflows/my_workflow.py"

        workspace = griptape_nodes.ConfigManager().workspace_path
        info_key = str(workspace / "workflows/my_workflow.py")
        wf_info = WorkflowManager.WorkflowInfo(
            status=WorkflowStatus.GOOD,
            workflow_path=info_key,
            workflow_name="my_workflow",
        )
        workflow_manager._workflow_file_path_to_info[info_key] = wf_info

        with patch.object(WorkflowRegistry, "get_workflow_by_name", return_value=mock_workflow):
            result = workflow_manager.on_get_workflow_info_request(request)

        assert isinstance(result, GetWorkflowInfoResultSuccess)
        assert result.status == "GOOD"
        assert result.workflow_name == "my_workflow"
        assert result.problems == []
        assert result.workflow_dependencies == []

    # --- ListAllWorkflowInfoRequest ---

    def test_on_list_all_workflow_info_request_registry_failure(self, griptape_nodes: GriptapeNodes) -> None:
        """ListAllWorkflowInfoRequest returns failure when listing workflows raises."""
        workflow_manager = griptape_nodes.WorkflowManager()
        request = ListAllWorkflowInfoRequest()

        with patch.object(WorkflowRegistry, "list_workflows", side_effect=Exception("registry error")):
            result = workflow_manager.on_list_all_workflow_info_request(request)

        assert isinstance(result, ListAllWorkflowInfoResultFailure)
        assert "registry error" in str(result.result_details)

    def test_on_list_all_workflow_info_request_success(self, griptape_nodes: GriptapeNodes) -> None:
        """ListAllWorkflowInfoRequest returns info for every workflow that has a stored WorkflowInfo."""
        from griptape_nodes.retained_mode.managers.workflow_manager import WorkflowManager

        workflow_manager = griptape_nodes.WorkflowManager()
        request = ListAllWorkflowInfoRequest()

        workspace = griptape_nodes.ConfigManager().workspace_path
        info_key = str(workspace / "workflows/my_workflow.py")
        wf_info = WorkflowManager.WorkflowInfo(
            status=WorkflowStatus.GOOD,
            workflow_path=info_key,
            workflow_name="my_workflow",
        )
        workflow_manager._workflow_file_path_to_info[info_key] = wf_info

        mock_workflow = MagicMock()
        mock_workflow.file_path = "workflows/my_workflow.py"

        with (
            patch.object(WorkflowRegistry, "list_workflows", return_value=["my_workflow"]),
            patch.object(WorkflowRegistry, "get_workflow_by_name", return_value=mock_workflow),
        ):
            result = workflow_manager.on_list_all_workflow_info_request(request)

        assert isinstance(result, ListAllWorkflowInfoResultSuccess)
        assert "my_workflow" in result.workflow_infos
        assert result.workflow_infos["my_workflow"].status == "GOOD"

    def test_on_list_all_workflow_info_request_skips_workflows_without_info(
        self, griptape_nodes: GriptapeNodes
    ) -> None:
        """ListAllWorkflowInfoRequest omits workflows that have no stored WorkflowInfo."""
        workflow_manager = griptape_nodes.WorkflowManager()
        request = ListAllWorkflowInfoRequest()

        mock_workflow = MagicMock()
        mock_workflow.file_path = "workflows/my_workflow.py"

        with (
            patch.object(WorkflowRegistry, "list_workflows", return_value=["my_workflow"]),
            patch.object(WorkflowRegistry, "get_workflow_by_name", return_value=mock_workflow),
        ):
            # _workflow_file_path_to_info is empty, so the workflow is skipped
            result = workflow_manager.on_list_all_workflow_info_request(request)

        assert isinstance(result, ListAllWorkflowInfoResultSuccess)
        assert result.workflow_infos == {}

    def test_on_list_all_workflow_info_request_skips_unknown_registry_keys(self, griptape_nodes: GriptapeNodes) -> None:
        """ListAllWorkflowInfoRequest skips registry keys that can't be looked up."""
        workflow_manager = griptape_nodes.WorkflowManager()
        request = ListAllWorkflowInfoRequest()

        with (
            patch.object(WorkflowRegistry, "list_workflows", return_value=["ghost_workflow"]),
            patch.object(WorkflowRegistry, "get_workflow_by_name", side_effect=KeyError("not found")),
        ):
            result = workflow_manager.on_list_all_workflow_info_request(request)

        assert isinstance(result, ListAllWorkflowInfoResultSuccess)
        assert result.workflow_infos == {}

    # --- _build_workflow_save_path ---

    def test_build_workflow_save_path_returns_destination_from_situation(self, griptape_nodes: GriptapeNodes) -> None:
        """The destination from the save_workflow situation is returned verbatim — no upstream macro resolution.

        Resolving the macro upstream would strip the seed-and-retry context needed
        for unresolved required ``{x:NN}`` slots inside OSManager (issue #4941).
        """
        workflow_manager = griptape_nodes.WorkflowManager()

        fake_destination = MagicMock()

        with patch(
            "griptape_nodes.retained_mode.managers.workflow_manager.ProjectFileDestination.from_situation",
            return_value=fake_destination,
        ) as mock_from_situation:
            save_path = workflow_manager._build_workflow_save_path("my_workflow.py")

        mock_from_situation.assert_called_once_with("my_workflow.py", "save_workflow")
        assert save_path.destination is fake_destination
        # No `.resolve()` is called upstream — the macro stays intact for OSManager.
        fake_destination.resolve.assert_not_called()
        assert save_path.relative_file_path == "my_workflow.py"

    def test_build_workflow_save_path_preserves_sub_dirs(self, griptape_nodes: GriptapeNodes) -> None:
        """sub_dirs flow through as macro variables and into the registry-relative display string."""
        workflow_manager = griptape_nodes.WorkflowManager()

        fake_destination = MagicMock()

        with patch(
            "griptape_nodes.retained_mode.managers.workflow_manager.ProjectFileDestination.from_situation",
            return_value=fake_destination,
        ) as mock_from_situation:
            save_path = workflow_manager._build_workflow_save_path("my_workflow.py", sub_dirs="team")

        mock_from_situation.assert_called_once_with("my_workflow.py", "save_workflow", sub_dirs="team")
        assert save_path.destination is fake_destination
        assert save_path.relative_file_path == str(Path("team") / "my_workflow.py")

    # --- _generate_workflow_file_content (save output) ---

    @staticmethod
    def _empty_serialized_flow_commands() -> SerializedFlowCommands:
        """Build a shape-free SerializedFlowCommands with no nodes/connections/values."""
        return SerializedFlowCommands(
            flow_initialization_command=None,
            serialized_node_commands=[],
            serialized_connections=[],
            unique_parameter_uuid_to_values={},
            set_parameter_value_commands={},
            set_lock_commands_per_node={},
            sub_flows_commands=[],
            node_dependencies=NodeDependencies(),
            node_types_used=set(),
        )

    @staticmethod
    def _minimal_workflow_metadata(*, with_shape: bool = False) -> WorkflowMetadata:
        return WorkflowMetadata(
            name="test_workflow",
            schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
            engine_version_created_with="1.0.0",
            node_libraries_referenced=[],
            workflow_shape=WorkflowShape(inputs={}, outputs={}) if with_shape else None,
        )

    def _generate(self, griptape_nodes: GriptapeNodes, *, with_shape: bool = False) -> str:
        workflow_manager = griptape_nodes.WorkflowManager()
        return workflow_manager._generate_workflow_file_content(
            serialized_flow_commands=self._empty_serialized_flow_commands(),
            workflow_metadata=self._minimal_workflow_metadata(with_shape=with_shape),
        )

    def test_generate_workflow_file_content_wraps_graph_building_in_async_build_workflow(
        self, griptape_nodes: GriptapeNodes
    ) -> None:
        """Saved workflows must wrap graph-building statements in `async def build_workflow()`."""
        content = self._generate(griptape_nodes)

        # The file must declare build_workflow as an async function.
        module = ast.parse(content)
        build_workflow_defs = [
            node for node in module.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "build_workflow"
        ]
        assert len(build_workflow_defs) == 1, "build_workflow must be defined exactly once as async"

    def test_generate_workflow_file_content_is_inert_at_import(self, griptape_nodes: GriptapeNodes) -> None:
        """A shape-free saved workflow must contain no module-level side effects.

        Only function/class definitions and imports should appear at the top level so that
        `exec()`-ing the module does not mutate engine state until build_workflow() is awaited.
        """
        content = self._generate(griptape_nodes)
        module = ast.parse(content)

        allowed_top_level = (
            ast.Import,
            ast.ImportFrom,
            ast.FunctionDef,
            ast.AsyncFunctionDef,
            ast.ClassDef,
            ast.Expr,  # docstring-style literal; still inert
        )
        for node in module.body:
            assert isinstance(node, allowed_top_level), (
                f"Unexpected top-level statement of type {type(node).__name__} in shape-free workflow"
            )

    def test_generate_workflow_file_content_prereq_lives_inside_build_workflow(
        self, griptape_nodes: GriptapeNodes
    ) -> None:
        """Prerequisite code (context_manager setup) must live inside build_workflow, not at module scope."""
        content = self._generate(griptape_nodes)
        module = ast.parse(content)

        build_workflow = next(
            node for node in module.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "build_workflow"
        )
        body_src = "\n".join(ast.unparse(stmt) for stmt in build_workflow.body)
        assert "context_manager = GriptapeNodes.ContextManager()" in body_src
        assert "context_manager.push_workflow(file_path=__file__)" in body_src

    def test_generate_workflow_file_content_registers_declared_libraries_in_build_workflow(
        self, griptape_nodes: GriptapeNodes
    ) -> None:
        """build_workflow() must register every library named in node_libraries_referenced.

        Regression test for https://github.com/griptape-ai/griptape-nodes/issues/4584:
        when a generated workflow is run as a standalone script via LocalWorkflowExecutor,
        no engine-side library bootstrap runs before build_workflow(), so the file itself
        must register its declared libraries to avoid every CreateNodeRequest collapsing
        into an ErrorProxyNode.
        """
        from griptape_nodes.node_library.library_registry import LibraryNameAndVersion

        workflow_manager = griptape_nodes.WorkflowManager()
        metadata = WorkflowMetadata(
            name="test_workflow",
            schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
            engine_version_created_with="1.0.0",
            node_libraries_referenced=[
                LibraryNameAndVersion(library_name="Griptape Nodes Library", library_version="0.1.0"),
                LibraryNameAndVersion(library_name="Other Library", library_version="0.2.0"),
            ],
            workflow_shape=None,
        )
        content = workflow_manager._generate_workflow_file_content(
            serialized_flow_commands=self._empty_serialized_flow_commands(),
            workflow_metadata=metadata,
        )

        module = ast.parse(content)

        # Each declared library must appear as an awaited RegisterLibraryFromFileRequest inside
        # build_workflow(), with perform_discovery_if_not_found=True so the engine can locate
        # the library JSON via the standard config-driven discovery path.
        build_workflow = next(
            node for node in module.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "build_workflow"
        )
        body_src = "\n".join(ast.unparse(stmt) for stmt in build_workflow.body)
        for library_name in ("Griptape Nodes Library", "Other Library"):
            assert (
                f"RegisterLibraryFromFileRequest(library_name='{library_name}', perform_discovery_if_not_found=True)"
            ) in body_src, f"build_workflow() must register library {library_name!r}; got body:\n{body_src}"

        # The corresponding import must also be present at module scope, since build_workflow()
        # references RegisterLibraryFromFileRequest by name.
        top_level_imports = [
            f"{alias.name}" for stmt in module.body if isinstance(stmt, ast.ImportFrom) for alias in stmt.names
        ]
        assert "RegisterLibraryFromFileRequest" in top_level_imports

    def test_generate_workflow_file_content_omits_register_calls_when_no_libraries_declared(
        self, griptape_nodes: GriptapeNodes
    ) -> None:
        """With an empty node_libraries_referenced list, no register calls are emitted.

        Keeps the import set tight for shape-free workflows that don't actually reference
        a library, and makes sure the empty-loop branch in _generate_workflow_run_prerequisite_code
        does not regress to emitting stray RegisterLibraryFromFileRequest noise.
        """
        content = self._generate(griptape_nodes)
        assert "RegisterLibraryFromFileRequest" not in content

    def test_generated_build_workflow_registers_libraries_before_creating_nodes(
        self, griptape_nodes: GriptapeNodes
    ) -> None:
        """build_workflow() must dispatch RegisterLibraryFromFileRequest before any CreateNodeRequest.

        Captures the runtime contract for issue #4584: when a generated workflow runs as a
        standalone script (no engine bootstrap, no _ensure_libraries_for_workflow), the file
        itself must register every declared library before issuing CreateNodeRequest, or every
        node collapses into an ErrorProxyNode. Verified by exec()ing the generated source and
        recording the request order via a stub ahandle_request, which is cheap and does not
        require a real library on disk.
        """
        from griptape_nodes.node_library.library_registry import LibraryNameAndVersion
        from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest as _CreateFlowRequest
        from griptape_nodes.retained_mode.events.library_events import RegisterLibraryFromFileRequest
        from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, SerializedNodeCommands

        workflow_manager = griptape_nodes.WorkflowManager()

        flow = SerializedFlowCommands(
            flow_initialization_command=_CreateFlowRequest(
                parent_flow_name=None, flow_name="ControlFlow_1", set_as_new_context=False, metadata={}
            ),
            serialized_node_commands=[
                SerializedNodeCommands(
                    create_node_command=CreateNodeRequest(
                        node_type="Note",
                        specific_library_name="Foo Library",
                        node_name="Note_1",
                        initial_setup=True,
                    ),
                    element_modification_commands=[],
                    node_dependencies=NodeDependencies(),
                ),
            ],
            serialized_connections=[],
            unique_parameter_uuid_to_values={},
            set_parameter_value_commands={},
            set_lock_commands_per_node={},
            sub_flows_commands=[],
            node_dependencies=NodeDependencies(),
            node_types_used=set(),
        )
        metadata = WorkflowMetadata(
            name="runtime_order_test",
            schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
            engine_version_created_with="1.0.0",
            node_libraries_referenced=[
                LibraryNameAndVersion(library_name="Foo Library", library_version="0.1.0"),
                LibraryNameAndVersion(library_name="Bar Library", library_version="0.2.0"),
            ],
            workflow_shape=None,
        )
        script_source = workflow_manager._generate_workflow_file_content(
            serialized_flow_commands=flow,
            workflow_metadata=metadata,
        )

        dispatched: list[object] = []

        original_ahandle = GriptapeNodes.ahandle_request

        async def recording_ahandle_request(request: object) -> object:
            dispatched.append(request)
            return await original_ahandle(request)  # type: ignore[arg-type]

        exec_globals: dict[str, object] = {"__file__": "runtime_order_test.py"}
        exec(compile(script_source, "<runtime_order_test>", "exec"), exec_globals)  # noqa: S102
        with patch.object(GriptapeNodes, "ahandle_request", side_effect=recording_ahandle_request):
            asyncio.run(exec_globals["build_workflow"]())  # type: ignore[operator]

        register_indices = [i for i, req in enumerate(dispatched) if isinstance(req, RegisterLibraryFromFileRequest)]
        register_names = {dispatched[i].library_name for i in register_indices}  # type: ignore[attr-defined]
        create_indices = [i for i, req in enumerate(dispatched) if isinstance(req, CreateNodeRequest)]

        assert register_names == {"Foo Library", "Bar Library"}, (
            f"build_workflow must register every declared library; got {register_names!r}"
        )
        assert create_indices, "build_workflow must dispatch the serialized CreateNodeRequest"
        assert max(register_indices) < min(create_indices), (
            "every RegisterLibraryFromFileRequest must precede every CreateNodeRequest, otherwise"
            " CreateNodeRequest will fall back to ErrorProxyNode in standalone runs;"
            f" got order {[type(r).__name__ for r in dispatched]}"
        )

    def test_generate_workflow_file_content_empty_build_workflow_has_pass_body(
        self, griptape_nodes: GriptapeNodes
    ) -> None:
        """If we somehow produce no graph-building statements, build_workflow should still be valid Python."""
        workflow_manager = griptape_nodes.WorkflowManager()
        # Stub out the two generators so main_body ends up empty and the `or [ast.Pass()]` branch runs.
        with (
            patch.object(workflow_manager, "_generate_workflow_run_prerequisite_code", return_value=[]),
            patch.object(
                workflow_manager,
                "_generate_unique_values_code",
                return_value=ast.Module(body=[], type_ignores=[]),
            ),
        ):
            content = workflow_manager._generate_workflow_file_content(
                serialized_flow_commands=self._empty_serialized_flow_commands(),
                workflow_metadata=self._minimal_workflow_metadata(),
            )

        module = ast.parse(content)
        build_workflow = next(
            node for node in module.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "build_workflow"
        )
        assert len(build_workflow.body) == 1
        assert isinstance(build_workflow.body[0], ast.Pass)

    def test_generate_workflow_file_content_aexecute_awaits_build_workflow_first(
        self, griptape_nodes: GriptapeNodes
    ) -> None:
        """aexecute_workflow must `await build_workflow()` before running the executor.

        Shape-bearing workflows emit execute_workflow + aexecute_workflow, and the async
        version is expected to construct the graph before invoking the executor.
        """
        content = self._generate(griptape_nodes, with_shape=True)
        module = ast.parse(content)

        aexecute = next(
            node for node in module.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "aexecute_workflow"
        )
        first_stmt = aexecute.body[0]
        # Expected shape: `await build_workflow()`
        assert isinstance(first_stmt, ast.Expr)
        assert isinstance(first_stmt.value, ast.Await)
        call = first_stmt.value.value
        assert isinstance(call, ast.Call)
        assert isinstance(call.func, ast.Name)
        assert call.func.id == "build_workflow"

    def test_generate_workflow_file_content_ensure_context_is_async(self, griptape_nodes: GriptapeNodes) -> None:
        """_ensure_workflow_context is now async and must await ahandle_request."""
        content = self._generate(griptape_nodes, with_shape=True)
        module = ast.parse(content)

        ensure = next(
            node
            for node in module.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_ensure_workflow_context"
        )
        body_src = "\n".join(ast.unparse(stmt) for stmt in ensure.body)
        assert "await GriptapeNodes.ahandle_request" in body_src
        # Sanity check: the old sync variant is gone.
        assert "GriptapeNodes.handle_request(" not in body_src

    def test_generate_workflow_file_content_is_valid_python(self, griptape_nodes: GriptapeNodes) -> None:
        """Generated content must parse cleanly (no smuggled-string comments left behind)."""
        content = self._generate(griptape_nodes, with_shape=True)
        # ast.parse raises SyntaxError if rewrite_string_comments left bad output behind.
        ast.parse(content)

    def test_collect_object_imports_routes_dynamic_module_to_deferred(self, griptape_nodes: GriptapeNodes) -> None:
        """Dynamic library class imports must go into deferred_imports, not import_recorder.

        Regression for #4738: _collect_object_imports previously routed all imports through
        import_recorder, which put them at module top level. In headless mode this causes
        ModuleNotFoundError because the library isn't on sys.path until build_workflow() calls
        RegisterLibraryFromFileRequest.
        """
        from griptape_nodes.retained_mode.managers.workflow_manager import ImportRecorder

        workflow_manager = griptape_nodes.WorkflowManager()
        fake_class = type("FakeClass", (), {})
        fake_module = MagicMock()
        fake_module.__name__ = "gtn_dynamic_module_foo_py_123"

        import_recorder = ImportRecorder()
        deferred_imports: dict[str, set[str]] = {}

        with (
            patch(
                "griptape_nodes.retained_mode.managers.workflow_manager.getmodule",
                return_value=fake_module,
            ),
            patch.object(griptape_nodes.LibraryManager(), "is_dynamic_module", return_value=True),
            patch.object(
                griptape_nodes.LibraryManager(),
                "get_stable_namespace_for_dynamic_module",
                return_value="my_lib.foo",
            ),
        ):
            workflow_manager._collect_object_imports(fake_class(), import_recorder, set(), deferred_imports)

        assert "my_lib.foo" in deferred_imports, "Dynamic library import must land in deferred_imports"
        assert "FakeClass" in deferred_imports["my_lib.foo"]
        assert "my_lib.foo" not in import_recorder.from_imports, (
            "Dynamic library import must NOT be in import_recorder (would appear at module top level)"
        )


class TestWorkflowVariablePersistence:
    """Round-trip tests: variables created in a flow must survive save + load."""

    def _fresh_metadata(self, name: str = "test_workflow") -> "WorkflowMetadata":
        from griptape_nodes.node_library.workflow_registry import WorkflowMetadata

        return WorkflowMetadata(
            name=name,
            schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
            engine_version_created_with="0.0.0",
            node_libraries_referenced=[],
        )

    def test_generate_create_variable_code_emits_expected_call(self, griptape_nodes: GriptapeNodes) -> None:
        """The AST helper should produce a single CreateVariableRequest call per command."""
        import ast

        from griptape_nodes.retained_mode.events.flow_events import SerializedFlowCommands
        from griptape_nodes.retained_mode.events.node_events import SerializedNodeCommands
        from griptape_nodes.retained_mode.events.variable_events import CreateVariableRequest
        from griptape_nodes.retained_mode.managers.workflow_manager import ImportRecorder

        workflow_manager = griptape_nodes.WorkflowManager()
        import_recorder = ImportRecorder()

        serialized_command = SerializedFlowCommands.SerializedVariableCommand(
            create_variable_command=CreateVariableRequest(
                name="my_var",
                type="str",
                is_global=False,
                value=None,
                owning_flow="ControlFlow_1",
                initial_setup=True,
            ),
            unique_value_uuid=SerializedNodeCommands.UniqueParameterValueUUID("abc-uuid"),
        )

        stmts = workflow_manager._generate_create_variable_code(
            serialized_variable_commands=[serialized_command],
            unique_values_dict_name="top_level_unique_values_dict",
            import_recorder=import_recorder,
        )

        assert len(stmts) == 1
        rendered = ast.unparse(stmts[0])
        assert "CreateVariableRequest(" in rendered
        assert "name='my_var'" in rendered
        assert "type='str'" in rendered
        assert "is_global=False" in rendered
        assert "owning_flow='ControlFlow_1'" in rendered
        assert "initial_setup=True" in rendered
        assert "top_level_unique_values_dict['abc-uuid']" in rendered

        # Import recorder should have captured the CreateVariableRequest import.
        imports_text = import_recorder.generate_imports()
        assert "CreateVariableRequest" in imports_text

    def _push_clean_flow_context(self, griptape_nodes: GriptapeNodes, flow_name: str = "ControlFlow_1") -> str:
        """Clear state, push a workflow context, and create a single empty flow. Returns the flow name."""
        from griptape_nodes.retained_mode.events.flow_events import (
            CreateFlowRequest,
            CreateFlowResultSuccess,
        )

        variables_manager = griptape_nodes.VariablesManager()
        context_manager = griptape_nodes.ContextManager()

        if context_manager.has_current_workflow():
            GriptapeNodes.clear_current_workflow_data()
        variables_manager.clear_object_state()

        context_manager.push_workflow(workflow_name="round_trip_workflow")

        flow_result = GriptapeNodes.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name=flow_name, set_as_new_context=False)
        )
        assert isinstance(flow_result, CreateFlowResultSuccess)
        return flow_result.flow_name

    def test_declared_variable_gets_serialized(self, griptape_nodes: GriptapeNodes) -> None:
        """A flow-scoped variable that is declared via a VariableReference should be serialized."""
        from griptape_nodes.exe_types.node_types import VariableReference
        from griptape_nodes.retained_mode.events.variable_events import (
            CreateVariableRequest,
            CreateVariableResultSuccess,
        )
        from griptape_nodes.retained_mode.variable_types import VariableScope

        flow_manager = griptape_nodes.FlowManager()
        flow_name = self._push_clean_flow_context(griptape_nodes)

        assert isinstance(
            GriptapeNodes.handle_request(
                CreateVariableRequest(
                    name="declared_var", type="str", is_global=False, value="dog", owning_flow=flow_name
                )
            ),
            CreateVariableResultSuccess,
        )

        unique_values: dict[SerializedNodeCommands.UniqueParameterValueUUID, object] = {}
        commands = flow_manager._serialize_variables_for_flow(
            flow_name=flow_name,
            unique_parameter_uuid_to_values=unique_values,
            variable_references={VariableReference(name="declared_var", scope=VariableScope.CURRENT_FLOW_ONLY)},
        )

        assert {cmd.create_variable_command.name for cmd in commands} == {"declared_var"}
        assert len(unique_values) == 1

    def test_orphan_variable_is_dropped(self, griptape_nodes: GriptapeNodes) -> None:
        """A variable in engine state with no declared reference must not be serialized."""
        from griptape_nodes.retained_mode.events.variable_events import (
            CreateVariableRequest,
            CreateVariableResultSuccess,
        )

        flow_manager = griptape_nodes.FlowManager()
        flow_name = self._push_clean_flow_context(griptape_nodes)

        assert isinstance(
            GriptapeNodes.handle_request(
                CreateVariableRequest(
                    name="orphan_var", type="str", is_global=False, value="cat", owning_flow=flow_name
                )
            ),
            CreateVariableResultSuccess,
        )

        unique_values: dict[SerializedNodeCommands.UniqueParameterValueUUID, object] = {}
        commands = flow_manager._serialize_variables_for_flow(
            flow_name=flow_name,
            unique_parameter_uuid_to_values=unique_values,
            variable_references=set(),
        )

        assert commands == []
        assert unique_values == {}

    def test_declared_but_missing_variable_is_dropped(self, griptape_nodes: GriptapeNodes) -> None:
        """A reference to a variable that does not exist in the flow should not produce a command."""
        from griptape_nodes.exe_types.node_types import VariableReference
        from griptape_nodes.retained_mode.variable_types import VariableScope

        flow_manager = griptape_nodes.FlowManager()
        flow_name = self._push_clean_flow_context(griptape_nodes)

        unique_values: dict[SerializedNodeCommands.UniqueParameterValueUUID, object] = {}
        commands = flow_manager._serialize_variables_for_flow(
            flow_name=flow_name,
            unique_parameter_uuid_to_values=unique_values,
            variable_references={VariableReference(name="ghost", scope=VariableScope.CURRENT_FLOW_ONLY)},
        )

        assert commands == []

    def test_global_only_scope_is_skipped(self, griptape_nodes: GriptapeNodes) -> None:
        """GLOBAL_ONLY references are deferred for now and must not produce a command."""
        from griptape_nodes.exe_types.node_types import VariableReference
        from griptape_nodes.retained_mode.events.variable_events import (
            CreateVariableRequest,
            CreateVariableResultSuccess,
        )
        from griptape_nodes.retained_mode.variable_types import VariableScope

        flow_manager = griptape_nodes.FlowManager()
        flow_name = self._push_clean_flow_context(griptape_nodes)

        # Create a flow-scoped variable with the same name as a pretend-global. It should not match,
        # because the GLOBAL_ONLY scope is unsupported for serialization and must be skipped.
        assert isinstance(
            GriptapeNodes.handle_request(
                CreateVariableRequest(
                    name="shared_name", type="str", is_global=False, value="local", owning_flow=flow_name
                )
            ),
            CreateVariableResultSuccess,
        )

        unique_values: dict[SerializedNodeCommands.UniqueParameterValueUUID, object] = {}
        commands = flow_manager._serialize_variables_for_flow(
            flow_name=flow_name,
            unique_parameter_uuid_to_values=unique_values,
            variable_references={VariableReference(name="shared_name", scope=VariableScope.GLOBAL_ONLY)},
        )

        assert commands == []

    def test_hierarchical_reference_only_serializes_at_owning_flow(self, griptape_nodes: GriptapeNodes) -> None:
        """A HIERARCHICAL reference resolved against a child flow must not serialize an ancestor-owned variable."""
        from griptape_nodes.exe_types.node_types import VariableReference
        from griptape_nodes.retained_mode.events.flow_events import (
            CreateFlowRequest,
            CreateFlowResultSuccess,
        )
        from griptape_nodes.retained_mode.events.variable_events import (
            CreateVariableRequest,
            CreateVariableResultSuccess,
        )
        from griptape_nodes.retained_mode.variable_types import VariableScope

        flow_manager = griptape_nodes.FlowManager()
        parent_flow_name = self._push_clean_flow_context(griptape_nodes, flow_name="ParentFlow")

        child_flow_result = GriptapeNodes.handle_request(
            CreateFlowRequest(parent_flow_name=parent_flow_name, flow_name="ChildFlow", set_as_new_context=False)
        )
        assert isinstance(child_flow_result, CreateFlowResultSuccess)
        child_flow_name = child_flow_result.flow_name

        # Variable lives on the parent.
        assert isinstance(
            GriptapeNodes.handle_request(
                CreateVariableRequest(
                    name="ancestor_var",
                    type="str",
                    is_global=False,
                    value="from_parent",
                    owning_flow=parent_flow_name,
                )
            ),
            CreateVariableResultSuccess,
        )

        ref = VariableReference(name="ancestor_var", scope=VariableScope.HIERARCHICAL)

        # Child flow should not claim the parent-owned variable.
        child_unique_values: dict[SerializedNodeCommands.UniqueParameterValueUUID, object] = {}
        child_commands = flow_manager._serialize_variables_for_flow(
            flow_name=child_flow_name,
            unique_parameter_uuid_to_values=child_unique_values,
            variable_references={ref},
        )
        assert child_commands == []

        # Parent flow should claim it.
        parent_unique_values: dict[SerializedNodeCommands.UniqueParameterValueUUID, object] = {}
        parent_commands = flow_manager._serialize_variables_for_flow(
            flow_name=parent_flow_name,
            unique_parameter_uuid_to_values=parent_unique_values,
            variable_references={ref},
        )
        assert {cmd.create_variable_command.name for cmd in parent_commands} == {"ancestor_var"}

    def test_save_load_preserves_flow_scoped_variables(self, griptape_nodes: GriptapeNodes) -> None:
        """Round-trip: declare a flow-scoped variable, serialize, clear, exec, confirm it is restored."""
        from griptape_nodes.exe_types.node_types import NodeDependencies, VariableReference
        from griptape_nodes.retained_mode.events.flow_events import (
            SerializeFlowToCommandsRequest,
            SerializeFlowToCommandsResultSuccess,
        )
        from griptape_nodes.retained_mode.events.variable_events import (
            CreateVariableRequest,
            CreateVariableResultSuccess,
            GetVariableValueRequest,
            GetVariableValueResultSuccess,
        )
        from griptape_nodes.retained_mode.variable_types import VariableScope

        workflow_manager = griptape_nodes.WorkflowManager()
        variables_manager = griptape_nodes.VariablesManager()
        flow_name = self._push_clean_flow_context(griptape_nodes)

        assert isinstance(
            GriptapeNodes.handle_request(
                CreateVariableRequest(
                    name="flow_scoped_var", type="str", is_global=False, value="dog", owning_flow=flow_name
                )
            ),
            CreateVariableResultSuccess,
        )

        # Normally a node declares the reference via get_node_dependencies(); for this test we
        # inject the declaration directly onto the flow's aggregated NodeDependencies after
        # serialization gathers them. We do that by patching _aggregate_flow_dependencies to append
        # a VariableReference for our variable.
        from griptape_nodes.retained_mode.events.flow_events import SerializedFlowCommands
        from griptape_nodes.retained_mode.events.node_events import SerializedNodeCommands

        flow_manager = griptape_nodes.FlowManager()
        original_aggregate = flow_manager._aggregate_flow_dependencies

        def aggregate_with_declared_ref(
            serialized_node_commands: list[SerializedNodeCommands],
            sub_flows_commands: list[SerializedFlowCommands],
        ) -> NodeDependencies:
            deps = original_aggregate(serialized_node_commands, sub_flows_commands)
            deps.variable_references.add(
                VariableReference(name="flow_scoped_var", scope=VariableScope.CURRENT_FLOW_ONLY)
            )
            return deps

        with patch.object(
            flow_manager,
            "_aggregate_flow_dependencies",
            side_effect=aggregate_with_declared_ref,
        ):
            serialize_result = GriptapeNodes.handle_request(SerializeFlowToCommandsRequest(flow_name=flow_name))

        assert isinstance(serialize_result, SerializeFlowToCommandsResultSuccess)
        serialized_commands = serialize_result.serialized_flow_commands

        names_serialized = {
            cmd.create_variable_command.name for cmd in serialized_commands.serialized_variable_commands
        }
        assert names_serialized == {"flow_scoped_var"}

        # Generate the workflow script.
        metadata = self._fresh_metadata(name="test_round_trip")
        script_source = workflow_manager._generate_workflow_file_content(
            serialized_flow_commands=serialized_commands,
            workflow_metadata=metadata,
        )

        # Script must reference CreateVariableRequest for the flow-scoped variable.
        assert "CreateVariableRequest(" in script_source
        assert "name='flow_scoped_var'" in script_source

        # Clear everything, then exec the script and confirm the variable is rebuilt.
        GriptapeNodes.clear_current_workflow_data()
        variables_manager.clear_object_state()

        exec_globals: dict[str, object] = {"__file__": "test_workflow.py"}
        exec(compile(script_source, "<round_trip_test>", "exec"), exec_globals)  # noqa: S102

        # Graph-building requests now live inside `async def build_workflow()`; await it so the
        # flow/variable are actually materialized before we query them.
        build_workflow = exec_globals["build_workflow"]
        asyncio.run(build_workflow())  # type: ignore[operator]

        flow_value = GriptapeNodes.handle_request(
            GetVariableValueRequest(
                name="flow_scoped_var", starting_flow=flow_name, lookup_scope=VariableScope.CURRENT_FLOW_ONLY
            )
        )
        assert isinstance(flow_value, GetVariableValueResultSuccess)
        assert flow_value.value == "dog"

    def test_save_drops_orphan_variables_end_to_end(self, griptape_nodes: GriptapeNodes) -> None:
        """The var.py scenario: a variable with no declaring node must not survive serialization."""
        from griptape_nodes.retained_mode.events.flow_events import (
            SerializeFlowToCommandsRequest,
            SerializeFlowToCommandsResultSuccess,
        )
        from griptape_nodes.retained_mode.events.variable_events import (
            CreateVariableRequest,
            CreateVariableResultSuccess,
        )

        flow_name = self._push_clean_flow_context(griptape_nodes)

        # Simulate the bug: a variable was created (via some now-deleted SetVariable node) but no
        # node currently declares it.
        assert isinstance(
            GriptapeNodes.handle_request(
                CreateVariableRequest(
                    name="orphan_var", type="str", is_global=False, value="stale", owning_flow=flow_name
                )
            ),
            CreateVariableResultSuccess,
        )

        serialize_result = GriptapeNodes.handle_request(SerializeFlowToCommandsRequest(flow_name=flow_name))
        assert isinstance(serialize_result, SerializeFlowToCommandsResultSuccess)
        assert serialize_result.serialized_flow_commands.serialized_variable_commands == []


class TestVariableReferenceAccess:
    """Tests for the access field on VariableReference."""

    def test_default_access_is_read_write(self) -> None:
        """Omitting ``access`` yields READ_WRITE — the safe default when a node's pattern is mixed."""
        from griptape_nodes.exe_types.node_types import VariableAccess, VariableReference
        from griptape_nodes.retained_mode.variable_types import VariableScope

        ref = VariableReference(name="foo", scope=VariableScope.HIERARCHICAL)

        assert ref.access is VariableAccess.READ_WRITE

    def test_access_participates_in_equality_and_hash(self) -> None:
        """Different access values on the same (name, scope) produce distinct, coexisting set members."""
        from griptape_nodes.exe_types.node_types import VariableAccess, VariableReference
        from griptape_nodes.retained_mode.variable_types import VariableScope

        read_ref = VariableReference(name="foo", scope=VariableScope.HIERARCHICAL, access=VariableAccess.READ)
        write_ref = VariableReference(name="foo", scope=VariableScope.HIERARCHICAL, access=VariableAccess.WRITE)
        read_ref_twin = VariableReference(name="foo", scope=VariableScope.HIERARCHICAL, access=VariableAccess.READ)

        assert read_ref != write_ref
        assert hash(read_ref) != hash(write_ref)
        assert read_ref == read_ref_twin
        assert {read_ref, write_ref, read_ref_twin} == {read_ref, write_ref}

    def test_aggregate_from_preserves_distinct_access_entries(self) -> None:
        """Aggregating two NodeDependencies that name the same variable with different access retains both."""
        from griptape_nodes.exe_types.node_types import NodeDependencies, VariableAccess, VariableReference
        from griptape_nodes.retained_mode.variable_types import VariableScope

        reader = NodeDependencies()
        reader.variable_references.add(
            VariableReference(name="foo", scope=VariableScope.HIERARCHICAL, access=VariableAccess.READ)
        )
        writer = NodeDependencies()
        writer.variable_references.add(
            VariableReference(name="foo", scope=VariableScope.HIERARCHICAL, access=VariableAccess.READ_WRITE)
        )

        reader.aggregate_from(writer)

        assert reader.variable_references == {
            VariableReference(name="foo", scope=VariableScope.HIERARCHICAL, access=VariableAccess.READ),
            VariableReference(name="foo", scope=VariableScope.HIERARCHICAL, access=VariableAccess.READ_WRITE),
        }

    def test_serializer_ignores_access(self, griptape_nodes: GriptapeNodes) -> None:
        """Serialization filtering is access-agnostic: any declared reference keeps the variable."""
        from griptape_nodes.exe_types.node_types import VariableAccess, VariableReference
        from griptape_nodes.retained_mode.events.variable_events import (
            CreateVariableRequest,
            CreateVariableResultSuccess,
        )
        from griptape_nodes.retained_mode.variable_types import VariableScope

        flow_manager = griptape_nodes.FlowManager()
        persistence = TestWorkflowVariablePersistence()
        flow_name = persistence._push_clean_flow_context(griptape_nodes)

        assert isinstance(
            GriptapeNodes.handle_request(
                CreateVariableRequest(name="only_read", type="str", is_global=False, value="cat", owning_flow=flow_name)
            ),
            CreateVariableResultSuccess,
        )

        unique_values: dict[SerializedNodeCommands.UniqueParameterValueUUID, object] = {}
        commands = flow_manager._serialize_variables_for_flow(
            flow_name=flow_name,
            unique_parameter_uuid_to_values=unique_values,
            variable_references={
                VariableReference(name="only_read", scope=VariableScope.CURRENT_FLOW_ONLY, access=VariableAccess.READ)
            },
        )

        # Access being READ does not exclude the variable from save-to-disk serialization.
        assert {cmd.create_variable_command.name for cmd in commands} == {"only_read"}


class TestLibraryResolutionOnLoad:
    """run_workflow resolves declared libraries before exec via the metadata header."""

    def test_ensure_libraries_dispatches_ahandle_request_per_library(self, griptape_nodes: GriptapeNodes) -> None:
        """_ensure_libraries_for_workflow dispatches one RegisterLibraryFromFileRequest per declared library."""
        from griptape_nodes.node_library.library_registry import LibraryNameAndVersion
        from griptape_nodes.node_library.workflow_registry import WorkflowMetadata
        from griptape_nodes.retained_mode.events.library_events import (
            RegisterLibraryFromFileRequest,
            RegisterLibraryFromFileResultSuccess,
        )
        from griptape_nodes.retained_mode.events.workflow_events import LoadWorkflowMetadataResultSuccess

        workflow_manager = griptape_nodes.WorkflowManager()
        metadata = WorkflowMetadata(
            name="t",
            schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
            engine_version_created_with="0.0.0",
            node_libraries_referenced=[
                LibraryNameAndVersion(library_name="Example Library", library_version="0.1.0"),
                LibraryNameAndVersion(library_name="Other Library", library_version="0.2.0"),
            ],
        )
        load_result = LoadWorkflowMetadataResultSuccess(metadata=metadata, result_details="ok")

        dispatched: list[RegisterLibraryFromFileRequest] = []

        async def fake_ahandle_request(request: object) -> object:
            dispatched.append(request)  # type: ignore[arg-type]
            return RegisterLibraryFromFileResultSuccess(
                library_name=request.library_name,  # type: ignore[attr-defined]
                result_details="ok",
            )

        with (
            patch.object(workflow_manager, "on_load_workflow_metadata_request", AsyncMock(return_value=load_result)),
            patch.object(GriptapeNodes, "ahandle_request", side_effect=fake_ahandle_request),
        ):
            result = asyncio.run(
                workflow_manager._ensure_libraries_for_workflow(
                    relative_file_path="whatever.py",
                    complete_file_path=Path("whatever.py"),
                )
            )

        assert result is None
        assert [r.library_name for r in dispatched] == ["Example Library", "Other Library"]
        assert all(r.perform_discovery_if_not_found for r in dispatched)

    def test_ensure_libraries_returns_failure_when_registration_fails(self, griptape_nodes: GriptapeNodes) -> None:
        """A failed library registration short-circuits with a WorkflowExecutionResult failure."""
        from griptape_nodes.node_library.library_registry import LibraryNameAndVersion
        from griptape_nodes.node_library.workflow_registry import WorkflowMetadata
        from griptape_nodes.retained_mode.events.library_events import RegisterLibraryFromFileResultFailure
        from griptape_nodes.retained_mode.events.workflow_events import LoadWorkflowMetadataResultSuccess

        workflow_manager = griptape_nodes.WorkflowManager()
        metadata = WorkflowMetadata(
            name="t",
            schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
            engine_version_created_with="0.0.0",
            node_libraries_referenced=[
                LibraryNameAndVersion(library_name="Missing Library", library_version="0.1.0"),
            ],
        )
        load_result = LoadWorkflowMetadataResultSuccess(metadata=metadata, result_details="ok")

        with (
            patch.object(workflow_manager, "on_load_workflow_metadata_request", AsyncMock(return_value=load_result)),
            patch.object(
                GriptapeNodes,
                "ahandle_request",
                AsyncMock(return_value=RegisterLibraryFromFileResultFailure(result_details="not found")),
            ),
        ):
            result = asyncio.run(
                workflow_manager._ensure_libraries_for_workflow(
                    relative_file_path="whatever.py",
                    complete_file_path=Path("whatever.py"),
                )
            )

        assert result is not None
        assert result.execution_successful is False
        assert "Missing Library" in result.execution_details

    def test_ensure_libraries_failure_message_uses_filename_and_renders_semver(
        self, griptape_nodes: GriptapeNodes
    ) -> None:
        """Failure message uses the workflow file name (not full path) and renders v<version> for semver values."""
        import logging as _logging

        from griptape_nodes.node_library.library_registry import LibraryNameAndVersion
        from griptape_nodes.node_library.workflow_registry import WorkflowMetadata
        from griptape_nodes.retained_mode.events.library_events import (
            RegisterLibraryFromFileRequest,
            RegisterLibraryFromFileResultFailure,
        )
        from griptape_nodes.retained_mode.events.workflow_events import LoadWorkflowMetadataResultSuccess

        workflow_manager = griptape_nodes.WorkflowManager()
        metadata = WorkflowMetadata(
            name="t",
            schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
            engine_version_created_with="0.0.0",
            node_libraries_referenced=[
                LibraryNameAndVersion(library_name="Missing Library", library_version="1.2.3"),
            ],
        )
        load_result = LoadWorkflowMetadataResultSuccess(metadata=metadata, result_details="ok")

        dispatched: list[RegisterLibraryFromFileRequest] = []

        async def fake_ahandle_request(request: object) -> object:
            dispatched.append(request)  # type: ignore[arg-type]
            return RegisterLibraryFromFileResultFailure(result_details="not found")

        with (
            patch.object(workflow_manager, "on_load_workflow_metadata_request", AsyncMock(return_value=load_result)),
            patch.object(GriptapeNodes, "ahandle_request", side_effect=fake_ahandle_request),
        ):
            result = asyncio.run(
                workflow_manager._ensure_libraries_for_workflow(
                    relative_file_path="nested/dir/corridorKey.py",
                    complete_file_path=Path("/abs/path/to/nested/dir/corridorKey.py"),
                )
            )

        assert result is not None
        assert result.execution_successful is False
        # Filename only, not the absolute path
        assert "corridorKey.py" in result.execution_details
        assert "/abs/path/to" not in result.execution_details
        # Semver version renders with v-prefix
        assert "v1.2.3" in result.execution_details
        assert "Missing Library" in result.execution_details
        # Inner request is suppressed at DEBUG so the GUI doesn't double-toast.
        assert len(dispatched) == 1
        assert dispatched[0].failure_log_level == _logging.DEBUG

    def test_ensure_libraries_failure_message_omits_non_semver_version(self, griptape_nodes: GriptapeNodes) -> None:
        """Non-semver `library_version` values (e.g. unavailable-library placeholder) are not rendered as v<...>."""
        from griptape_nodes.node_library.library_registry import LibraryNameAndVersion
        from griptape_nodes.node_library.workflow_registry import WorkflowMetadata
        from griptape_nodes.retained_mode.events.library_events import RegisterLibraryFromFileResultFailure
        from griptape_nodes.retained_mode.events.workflow_events import LoadWorkflowMetadataResultSuccess

        workflow_manager = griptape_nodes.WorkflowManager()
        placeholder = "<version unavailable; workflow was saved when library was unable to be loaded>"
        metadata = WorkflowMetadata(
            name="t",
            schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
            engine_version_created_with="0.0.0",
            node_libraries_referenced=[
                LibraryNameAndVersion(library_name="Missing Library", library_version=placeholder),
            ],
        )
        load_result = LoadWorkflowMetadataResultSuccess(metadata=metadata, result_details="ok")

        with (
            patch.object(workflow_manager, "on_load_workflow_metadata_request", AsyncMock(return_value=load_result)),
            patch.object(
                GriptapeNodes,
                "ahandle_request",
                AsyncMock(return_value=RegisterLibraryFromFileResultFailure(result_details="not found")),
            ),
        ):
            result = asyncio.run(
                workflow_manager._ensure_libraries_for_workflow(
                    relative_file_path="corridorKey.py",
                    complete_file_path=Path("corridorKey.py"),
                )
            )

        assert result is not None
        assert result.execution_successful is False
        assert "Missing Library" in result.execution_details
        # Placeholder must not leak into the user-facing message in any form
        assert placeholder not in result.execution_details
        assert " v" not in result.execution_details.split("Missing Library", 1)[1]

    def test_ensure_libraries_failure_message_omits_empty_version(self, griptape_nodes: GriptapeNodes) -> None:
        """An empty `library_version` falls through the semver check and renders no version suffix."""
        from griptape_nodes.node_library.library_registry import LibraryNameAndVersion
        from griptape_nodes.node_library.workflow_registry import WorkflowMetadata
        from griptape_nodes.retained_mode.events.library_events import RegisterLibraryFromFileResultFailure
        from griptape_nodes.retained_mode.events.workflow_events import LoadWorkflowMetadataResultSuccess

        workflow_manager = griptape_nodes.WorkflowManager()
        metadata = WorkflowMetadata(
            name="t",
            schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
            engine_version_created_with="0.0.0",
            node_libraries_referenced=[
                LibraryNameAndVersion(library_name="Missing Library", library_version=""),
            ],
        )
        load_result = LoadWorkflowMetadataResultSuccess(metadata=metadata, result_details="ok")

        with (
            patch.object(workflow_manager, "on_load_workflow_metadata_request", AsyncMock(return_value=load_result)),
            patch.object(
                GriptapeNodes,
                "ahandle_request",
                AsyncMock(return_value=RegisterLibraryFromFileResultFailure(result_details="not found")),
            ),
        ):
            result = asyncio.run(
                workflow_manager._ensure_libraries_for_workflow(
                    relative_file_path="corridorKey.py",
                    complete_file_path=Path("corridorKey.py"),
                )
            )

        assert result is not None
        assert result.execution_successful is False
        assert "Missing Library" in result.execution_details
        assert " v" not in result.execution_details.split("Missing Library", 1)[1]

    def test_ensure_libraries_is_noop_when_metadata_missing(self, griptape_nodes: GriptapeNodes) -> None:
        """If metadata can't be loaded, _ensure_libraries_for_workflow returns None (tolerant fallback)."""
        from griptape_nodes.retained_mode.events.workflow_events import LoadWorkflowMetadataResultFailure

        workflow_manager = griptape_nodes.WorkflowManager()
        load_result = LoadWorkflowMetadataResultFailure(result_details="no metadata")

        ahandle_spy = AsyncMock()
        with (
            patch.object(workflow_manager, "on_load_workflow_metadata_request", AsyncMock(return_value=load_result)),
            patch.object(GriptapeNodes, "ahandle_request", ahandle_spy),
        ):
            result = asyncio.run(
                workflow_manager._ensure_libraries_for_workflow(
                    relative_file_path="whatever.py",
                    complete_file_path=Path("whatever.py"),
                )
            )

        assert result is None
        ahandle_spy.assert_not_awaited()


class TestWorkflowsLoadingGate:
    """Gated handlers must not deadlock when invoked during library load (issue #4470)."""

    def test_workflows_loading_complete_is_set_on_init(self, griptape_nodes: GriptapeNodes) -> None:
        """The gate starts as set so handlers invoked before first refresh return immediately.

        The hazard is: a node __init__ fires a workflow query during library load, but
        the task that would set the gate is higher up the same call stack. If the gate
        started unset, the handler would block forever. Starting set means handlers
        see an empty registry (the truth during startup) and return a clean empty result.
        """
        workflow_manager = griptape_nodes.WorkflowManager()

        assert workflow_manager._workflows_loading_complete.is_set()

    def test_list_all_workflows_returns_immediately_before_first_refresh(self, griptape_nodes: GriptapeNodes) -> None:
        """on_list_all_workflows_request does not hang when invoked before refresh_workflow_registry."""
        from griptape_nodes.retained_mode.events.workflow_events import (
            ListAllWorkflowsRequest,
            ListAllWorkflowsResultSuccess,
        )

        workflow_manager = griptape_nodes.WorkflowManager()

        async def gated() -> object:
            return await asyncio.wait_for(
                workflow_manager.on_list_all_workflows_request(ListAllWorkflowsRequest()),
                timeout=1.0,
            )

        result = asyncio.run(gated())

        assert isinstance(result, ListAllWorkflowsResultSuccess)


class TestWorkflowMetadataTransitiveDeps:
    """node_libraries_referenced in saved workflow metadata includes transitive library_dependencies."""

    def test_transitive_library_dep_included_in_metadata(self, griptape_nodes: GriptapeNodes) -> None:
        """When lib-a has library_dependency on lib-b, generated metadata lists both in node_libraries_referenced."""
        from datetime import UTC, datetime

        from griptape_nodes.node_library.library_declarations import LibraryDependencyDeclaration
        from griptape_nodes.node_library.library_registry import LibraryNameAndVersion
        from griptape_nodes.retained_mode.managers.library_manager import LibraryManager

        workflow_manager = griptape_nodes.WorkflowManager()
        lib_mgr = griptape_nodes.LibraryManager()

        commands = SerializedFlowCommands(
            flow_initialization_command=None,
            serialized_node_commands=[],
            serialized_connections=[],
            unique_parameter_uuid_to_values={},
            set_parameter_value_commands={},
            set_lock_commands_per_node={},
            sub_flows_commands=[],
            node_dependencies=NodeDependencies(),
            node_types_used=set(),
        )
        commands.node_dependencies.libraries.add(LibraryNameAndVersion("lib-a", "1.0.0"))

        dep_b = LibraryDependencyDeclaration(url="griptape-ai/lib-b@v1.0.0", required=True)
        lib_a_mock = MagicMock()
        lib_a_mock.get_library_data.return_value.metadata.declarations = [dep_b]
        lib_b_mock = MagicMock()
        lib_b_mock.get_library_data.return_value.metadata.declarations = []
        info_b = LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
            library_path="/workspace/libraries/lib-b/griptape_nodes_library.json",
            is_sandbox=False,
            library_name="lib-b",
            library_version="1.0.0",
            fitness=LibraryManager.LibraryFitness.GOOD,
            problems=[],
        )

        with (
            patch(
                "griptape_nodes.node_library.library_registry.LibraryRegistry.get_library",
                side_effect=lambda name: {"lib-a": lib_a_mock, "lib-b": lib_b_mock}[name],
            ),
            patch.object(
                lib_mgr, "get_library_info_by_library_name", side_effect=lambda n: info_b if n == "lib-b" else None
            ),
        ):
            metadata = workflow_manager._generate_workflow_metadata_from_commands(
                serialized_flow_commands=commands,
                file_name="test_workflow.py",
                creation_date=datetime.now(UTC),
            )

        names = {lib.library_name for lib in metadata.node_libraries_referenced}
        assert "lib-a" in names
        assert "lib-b" in names, "Transitive library dependency must appear in node_libraries_referenced"


class TestWorkflowSaveSituationMacro:
    """Regression coverage for #4941: the save_workflow situation macro is honored.

    When a project customizes the ``save_workflow`` situation to use
    ``create_new`` with a padded `{_index:03}` slot, the workflow save path
    must thread the unresolved ``ProjectFileDestination`` through to OSManager
    so the seed-and-retry contract for missing required ``{x:NN}`` slots fires.
    Pre-resolving the macro upstream (the bug fixed here) strips that context
    and either fails the save or silently writes to the wrong location.
    """

    @pytest.fixture
    def temp_dir(self, tmp_path: Path) -> Path:
        return tmp_path.resolve()

    @pytest.fixture(autouse=True)
    def setup_versioned_save_workflow_project(
        self, temp_dir: Path, griptape_nodes: GriptapeNodes
    ) -> "Generator[None, None, None]":
        """Load a project that overrides save_workflow to CREATE_NEW with a `{_index:03}` slot.

        Mirrors the fixture in TestCreateNewMacroIndexSeed: the project is loaded
        and activated BEFORE workspace_path is forced, so SetCurrentProjectRequest
        does not re-derive workspace_path from the project's config layers.
        """
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.common.project_templates.situation import (
            SituationFilePolicy,
            SituationPolicy,
            SituationTemplate,
        )
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
            SetCurrentProjectRequest,
        )

        original_workspace = griptape_nodes.ConfigManager().workspace_path

        versioned_save_workflow = SituationTemplate(
            name="save_workflow",
            description="Versioned workflow save: {_index:03} required, CREATE_NEW policy.",
            macro="{workspace_dir}/{sub_dirs?:/}{file_name_base}_v{_index:03}.{file_extension}",
            policy=SituationPolicy(on_collision=SituationFilePolicy.CREATE_NEW, create_dirs=True),
            fallback="save_file",
        )
        custom_template = DEFAULT_PROJECT_TEMPLATE.model_copy(
            update={
                "situations": {**DEFAULT_PROJECT_TEMPLATE.situations, "save_workflow": versioned_save_workflow},
            }
        )

        project_yml = temp_dir / "project_template.yml"
        project_yml.write_text(custom_template.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE))
        load_result = GriptapeNodes.handle_request(LoadProjectTemplateRequest(project_path=project_yml))
        assert isinstance(load_result, LoadProjectTemplateResultSuccess)
        GriptapeNodes.handle_request(SetCurrentProjectRequest(project_id=load_result.project_id))

        griptape_nodes.ConfigManager().workspace_path = temp_dir

        yield

        GriptapeNodes.handle_request(SetCurrentProjectRequest(project_id=None))
        griptape_nodes.ConfigManager().workspace_path = original_workspace

    @staticmethod
    def _empty_commands() -> SerializedFlowCommands:
        return SerializedFlowCommands(
            flow_initialization_command=None,
            serialized_node_commands=[],
            serialized_connections=[],
            unique_parameter_uuid_to_values={},
            set_parameter_value_commands={},
            set_lock_commands_per_node={},
            sub_flows_commands=[],
            node_dependencies=NodeDependencies(),
            node_types_used=set(),
        )

    def _save(self, griptape_nodes: GriptapeNodes, file_name: str) -> str:
        """Drive _save_workflow_file_inline against the versioned save_workflow situation."""
        workflow_manager = griptape_nodes.WorkflowManager()
        destination, _relative = workflow_manager._build_workflow_save_path(f"{file_name}.py")

        result = workflow_manager._save_workflow_file_inline(
            destination=destination,
            serialized_flow_commands=self._empty_commands(),
            file_name=file_name,
            creation_date=datetime.now(UTC),
            display_name=None,
            image_path=None,
            description=None,
            is_template=None,
            branched_from=None,
            workflow_shape=None,
            pickle_control_flow_result=False,
        )
        from griptape_nodes.retained_mode.events.workflow_events import (
            SaveWorkflowFileFromSerializedFlowResultSuccess,
        )

        assert isinstance(result, SaveWorkflowFileFromSerializedFlowResultSuccess), (
            f"Expected success, got {result.result_details}"
        )
        return result.file_path

    def test_first_save_writes_v001(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Bug #4941: the first save with `{_index:03}` must produce v001 (not fail with MISSING_REQUIRED)."""
        saved_path = self._save(griptape_nodes, "my_workflow")

        assert Path(saved_path) == temp_dir / "my_workflow_v001.py"
        assert (temp_dir / "my_workflow_v001.py").exists()

    def test_successive_saves_increment_padded_index(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """Saving the same workflow three times produces v001, v002, v003 (padding preserved)."""
        for _ in range(3):
            self._save(griptape_nodes, "my_workflow")

        assert (temp_dir / "my_workflow_v001.py").exists()
        assert (temp_dir / "my_workflow_v002.py").exists()
        assert (temp_dir / "my_workflow_v003.py").exists()

    def test_sub_dirs_route_into_subdirectory(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """A sub-directory in the requested name routes into `{sub_dirs?:/}` and still picks v001."""
        workflow_manager = griptape_nodes.WorkflowManager()
        destination, relative = workflow_manager._build_workflow_save_path("my_workflow.py", sub_dirs="episode")
        assert relative == str(Path("episode") / "my_workflow.py")

        result = workflow_manager._save_workflow_file_inline(
            destination=destination,
            serialized_flow_commands=self._empty_commands(),
            file_name="my_workflow",
            creation_date=datetime.now(UTC),
            display_name=None,
            image_path=None,
            description=None,
            is_template=None,
            branched_from=None,
            workflow_shape=None,
            pickle_control_flow_result=False,
        )
        from griptape_nodes.retained_mode.events.workflow_events import (
            SaveWorkflowFileFromSerializedFlowResultSuccess,
        )

        assert isinstance(result, SaveWorkflowFileFromSerializedFlowResultSuccess)
        assert Path(result.file_path) == temp_dir / "episode" / "my_workflow_v001.py"
        assert (temp_dir / "episode" / "my_workflow_v001.py").exists()


class TestCreateVersionedWorkflow:
    """Regression coverage for #4945: ``create_versioned=True`` produces a fresh version every save.

    Drives ``on_save_workflow_request`` end-to-end (rather than the inner
    ``_save_workflow_file_inline``) so we exercise the dispatch logic in
    ``_determine_save_target`` — that's where the OVERWRITE_EXISTING vs.
    CREATE_VERSIONED choice happens.
    """

    @pytest.fixture
    def temp_dir(self, tmp_path: Path) -> Path:
        return tmp_path.resolve()

    @pytest.fixture(autouse=True)
    def setup_default_project(self, temp_dir: Path, griptape_nodes: GriptapeNodes) -> "Generator[None, None, None]":
        """Load the default project template (which ships create_versioned_workflow).

        Same fixture ordering as TestWorkflowSaveSituationMacro: load + activate
        first, then force workspace_path so SetCurrentProjectRequest's internal
        re-derivation doesn't clobber the test workspace.
        """
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
            SetCurrentProjectRequest,
        )

        original_workspace = griptape_nodes.ConfigManager().workspace_path

        project_yml = temp_dir / "project_template.yml"
        project_yml.write_text(DEFAULT_PROJECT_TEMPLATE.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE))
        load_result = GriptapeNodes.handle_request(LoadProjectTemplateRequest(project_path=project_yml))
        assert isinstance(load_result, LoadProjectTemplateResultSuccess)
        GriptapeNodes.handle_request(SetCurrentProjectRequest(project_id=load_result.project_id))

        griptape_nodes.ConfigManager().workspace_path = temp_dir

        yield

        WorkflowRegistry._workflows.clear()
        GriptapeNodes.handle_request(SetCurrentProjectRequest(project_id=None))
        griptape_nodes.ConfigManager().workspace_path = original_workspace

    @staticmethod
    def _determine(
        griptape_nodes: GriptapeNodes,
        *,
        requested_file_name: str | None,
        current_workflow_name: str | None,
        create_versioned: bool,
    ) -> WorkflowManager.SaveWorkflowTargetInfo:
        return griptape_nodes.WorkflowManager()._determine_save_target(
            requested_file_name=requested_file_name,
            current_workflow_name=current_workflow_name,
            create_versioned=create_versioned,
        )

    @staticmethod
    def _register_saved_workflow(temp_dir: Path, *, registry_key: str, file_name: str, display_name: str) -> None:
        """Materialize a fake saved workflow on disk + in registry.

        Workflow.from_disk verifies the file exists, so we touch a stub file.
        Real save tests need the file to genuinely be the prior save's content;
        for dispatch-logic tests, an empty stub suffices.
        """
        (temp_dir / file_name).write_text("# stub")
        metadata = WorkflowMetadata(
            name=display_name,
            schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
            engine_version_created_with="test",
            node_libraries_referenced=[],
            creation_date=datetime.now(UTC),
        )
        WorkflowRegistry.generate_new_workflow(registry_key=registry_key, metadata=metadata, file_path=file_name)

    def test_create_versioned_short_circuits_overwrite_existing(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """Even when the workflow is already saved, create_versioned=True routes through the versioned situation.

        Without this fix, the OVERWRITE_EXISTING branch would win on the second
        save and write back to the same file_path — the user's reported symptom
        ("always get v001 / overwrite in place"). With it, we get a
        CREATE_VERSIONED scenario whose destination carries the unresolved macro.
        """
        with patch.dict(WorkflowRegistry._workflows, {}, clear=True):
            self._register_saved_workflow(
                temp_dir, registry_key="my_flow_v001", file_name="my_flow_v001.py", display_name="my_flow"
            )

            target = self._determine(
                griptape_nodes,
                requested_file_name="my_flow_v001",
                current_workflow_name="my_flow_v001",
                create_versioned=True,
            )

            assert target.scenario == WorkflowManager.SaveWorkflowScenario.CREATE_VERSIONED
            # The destination carries the create_versioned_workflow macro (unresolved
            # sequence-slot marker — `{_index:NN}` in legacy templates or `{###}` /
            # `{###?}` in the modern default), so OSManager's seed walks past existing
            # v001 and lands at v002 on write.
            assert target.destination is not None
            macro = target.destination._file.location
            assert "_index" in macro or "#" in macro
            # The OVERWRITE_EXISTING path-mode is NOT taken.
            assert target.file_path is None

    def test_create_versioned_match_path_passes_full_matched_dict_through(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """Saving over `my_flow_v001` produces a destination whose MacroPath carries every matched variable.

        The destination's MacroPath has every variable the situation's macro
        identified in the existing file's name — *including* the bound
        ``_index``. The macro resolves to ``my_flow_v001.py`` on the first
        attempt; OSManager's CREATE_NEW collision-walk detects the existing
        file and steps the padded slot from 1 to 2, landing the next save at
        ``my_flow_v002.py``.
        """
        with patch.dict(WorkflowRegistry._workflows, {}, clear=True):
            self._register_saved_workflow(
                temp_dir,
                registry_key="my_flow_v001",
                file_name="my_flow_v001.py",
                display_name="",
            )

            target = self._determine(
                griptape_nodes,
                requested_file_name=None,
                current_workflow_name="my_flow_v001",
                create_versioned=True,
            )

            # Macro match succeeded: every variable the macro identified is
            # bound in the destination's MacroPath. OSManager's collision-walk
            # bumps _index from 1 → 2 on write.
            assert target.destination is not None
            macro_vars = target.destination._file._file_path.variables  # type: ignore[union-attr]
            assert macro_vars.get("file_name_base") == "my_flow"
            assert macro_vars.get("file_extension") == "py"
            assert macro_vars.get("_index") == 1

    def test_create_versioned_match_recovers_index_when_stem_contains_v(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """Reverse-match handles base names that themselves contain the ``_v`` anchor lookalike.

        Regression coverage for the cjkindel review on #4989: previously the
        leftmost ``path.find("_v", ...)`` landed on the ``_v`` inside
        ``my_v1_report`` (position 2), not on the version marker ``_v007``.
        ``file_name_base`` swallowed the whole stem and ``_index`` was
        dropped — the next save would produce ``my_v1_report_v007_1.py``
        instead of ``my_v1_report_v008.py``, reintroducing the exact bug
        #4956/#5025 set out to fix. The fix uses ``path.rfind`` for
        leading-separator anchors.
        """
        with patch.dict(WorkflowRegistry._workflows, {}, clear=True):
            self._register_saved_workflow(
                temp_dir,
                registry_key="my_v1_report_v007",
                file_name="my_v1_report_v007.py",
                display_name="",
            )

            target = self._determine(
                griptape_nodes,
                requested_file_name=None,
                current_workflow_name="my_v1_report_v007",
                create_versioned=True,
            )

            assert target.destination is not None
            macro_vars = target.destination._file._file_path.variables  # type: ignore[union-attr]
            # file_name_base stops at the LAST `_v`, not the first — the
            # `_v` inside `my_v1` is a lookalike, the trailing `_v` is the marker.
            assert macro_vars.get("file_name_base") == "my_v1_report"
            assert macro_vars.get("_index") == 7  # noqa: PLR2004 - literal version from setup
            assert macro_vars.get("file_extension") == "py"

    def test_create_versioned_with_requested_name_matching_existing_workflow_runs_match(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """The UI re-sends the open workflow's key as ``file_name``; we still reverse-match.

        Pins the fix for the manual-test bug where saving ``workflow_115_v002``
        produced ``workflow_115_v002_v001.py`` instead of bumping to
        ``workflow_115_v003.py``. The UI passes the loaded workflow's registry
        key as ``requested_file_name`` even though the user didn't type a new
        name; if Step 1 treats that as a true Save As, it starts a fresh
        ``_v001`` series under the old name. The fix: short-circuit Step 1
        only when the requested name names a workflow we DON'T already know
        about. When it resolves to an existing registry entry, drop into the
        reverse-match so the macro advances the version.
        """
        with patch.dict(WorkflowRegistry._workflows, {}, clear=True):
            self._register_saved_workflow(
                temp_dir,
                registry_key="my_flow_v002",
                file_name="my_flow_v002.py",
                display_name="my_flow",
            )

            # UI sends the loaded workflow's registry key as requested_file_name.
            target = self._determine(
                griptape_nodes,
                requested_file_name="my_flow_v002",
                current_workflow_name="my_flow_v002",
                create_versioned=True,
            )

            # Reverse-match path taken, NOT a fresh _resolve_named_save_path.
            # The destination's MacroPath carries _index=2 from the existing file;
            # OSManager's collision-walk bumps it to 3 on write.
            assert target.destination is not None
            macro_vars = target.destination._file._file_path.variables  # type: ignore[union-attr]
            assert macro_vars.get("file_name_base") == "my_flow"
            assert macro_vars.get("_index") == 2  # noqa: PLR2004 - literal version from setup
            # Negative guard: if Step 1 had short-circuited, file_name_base would be
            # the whole prior stem ("my_flow_v002") and _index would be unbound.
            assert macro_vars.get("file_name_base") != "my_flow_v002"

    def test_create_versioned_false_preserves_overwrite_existing(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """create_versioned=False against a saved workflow keeps the standard OVERWRITE_EXISTING path."""
        with patch.dict(WorkflowRegistry._workflows, {}, clear=True):
            self._register_saved_workflow(
                temp_dir, registry_key="my_flow", file_name="my_flow.py", display_name="my_flow"
            )

            target = self._determine(
                griptape_nodes,
                requested_file_name="my_flow",
                current_workflow_name="my_flow",
                create_versioned=False,
            )

            assert target.scenario == WorkflowManager.SaveWorkflowScenario.OVERWRITE_EXISTING
            assert target.destination is None
            assert target.file_path is not None
            assert target.file_path.name == "my_flow.py"

    def test_warning_logged_when_save_workflow_customized_to_create_new(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A project that flips `save_workflow` to create_new triggers a warning on non-versioned saves.

        The save still completes (the configuration isn't broken), but the user
        sees a heads-up that their `save_workflow` situation now auto-indexes
        and they may have meant to set `create_versioned=True` instead.
        """
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.common.project_templates.situation import (
            SituationFilePolicy,
            SituationPolicy,
            SituationTemplate,
        )
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
            SetCurrentProjectRequest,
        )

        # Replace the in-memory project's save_workflow with a CREATE_NEW variant.
        flipped = SituationTemplate(
            name="save_workflow",
            description="Customized to create_new",
            macro="{workspace_dir}/{file_name_base}_v{_index:03}.{file_extension}",
            policy=SituationPolicy(on_collision=SituationFilePolicy.CREATE_NEW, create_dirs=True),
            fallback="save_file",
        )
        custom = DEFAULT_PROJECT_TEMPLATE.model_copy(
            update={"situations": {**DEFAULT_PROJECT_TEMPLATE.situations, "save_workflow": flipped}}
        )
        project_yml = temp_dir / "project_template.yml"
        project_yml.write_text(custom.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE))
        load_result = GriptapeNodes.handle_request(LoadProjectTemplateRequest(project_path=project_yml))
        assert isinstance(load_result, LoadProjectTemplateResultSuccess)
        GriptapeNodes.handle_request(SetCurrentProjectRequest(project_id=load_result.project_id))

        with patch.dict(WorkflowRegistry._workflows, {}, clear=True), caplog.at_level("WARNING"):
            self._determine(
                griptape_nodes,
                requested_file_name="my_flow",
                current_workflow_name=None,
                create_versioned=False,
            )

        assert any("uses 'create_new' policy" in record.message for record in caplog.records), (
            f"Expected warning about save_workflow policy mismatch, got: {[r.message for r in caplog.records]}"
        )

    def test_warning_logged_when_create_versioned_workflow_customized_to_overwrite(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Inverse mismatch: create_versioned=True against a `create_versioned_workflow` flipped to overwrite.

        A project author who keeps `save_workflow` at its default but flips
        `create_versioned_workflow` to `overwrite` has broken the contract of
        the per-save flag — `create_versioned=True` saves will overwrite in
        place. Surface a warning so the misconfiguration doesn't sit silently.
        """
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.common.project_templates.situation import (
            BuiltInSituation,
            SituationFilePolicy,
            SituationPolicy,
            SituationTemplate,
        )
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
            SetCurrentProjectRequest,
        )

        flipped = SituationTemplate(
            name=BuiltInSituation.CREATE_VERSIONED_WORKFLOW,
            description="Customized to overwrite",
            macro="{workspace_dir}/{file_name_base}.{file_extension}",
            policy=SituationPolicy(on_collision=SituationFilePolicy.OVERWRITE, create_dirs=True),
            fallback=BuiltInSituation.SAVE_FILE,
        )
        custom = DEFAULT_PROJECT_TEMPLATE.model_copy(
            update={
                "situations": {
                    **DEFAULT_PROJECT_TEMPLATE.situations,
                    BuiltInSituation.CREATE_VERSIONED_WORKFLOW: flipped,
                }
            }
        )
        project_yml = temp_dir / "project_template.yml"
        project_yml.write_text(custom.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE))
        load_result = GriptapeNodes.handle_request(LoadProjectTemplateRequest(project_path=project_yml))
        assert isinstance(load_result, LoadProjectTemplateResultSuccess)
        GriptapeNodes.handle_request(SetCurrentProjectRequest(project_id=load_result.project_id))

        with patch.dict(WorkflowRegistry._workflows, {}, clear=True), caplog.at_level("WARNING"):
            self._determine(
                griptape_nodes,
                requested_file_name="my_flow",
                current_workflow_name=None,
                create_versioned=True,
            )

        assert any("Versioned save requested" in record.message for record in caplog.records), (
            f"Expected warning about create_versioned_workflow policy mismatch, got: {[r.message for r in caplog.records]}"
        )

    def test_create_versioned_workflow_in_default_template(self) -> None:
        """``BuiltInSituation.CREATE_VERSIONED_WORKFLOW`` must be present in the default template.

        Sanity guard: if the enum value gets out of sync with the default
        ``DEFAULT_PROJECT_TEMPLATE.situations`` dict, every `create_versioned=True`
        save against an unmodified project would silently fall through and warn,
        not save versioned.
        """
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.common.project_templates.situation import BuiltInSituation, SituationFilePolicy

        situation = DEFAULT_PROJECT_TEMPLATE.situations.get(BuiltInSituation.CREATE_VERSIONED_WORKFLOW)
        assert situation is not None, "create_versioned_workflow missing from DEFAULT_PROJECT_TEMPLATE"
        assert situation.policy.on_collision == SituationFilePolicy.CREATE_NEW
        # A sequence-slot marker (legacy `{_index:NN}` or modern `{###}` / `{###?}`)
        # is what makes the seed-and-retry produce v001/v002/...
        assert "_index" in situation.macro or "#" in situation.macro

    def test_first_versioned_save_with_no_registry_entry(self, griptape_nodes: GriptapeNodes) -> None:
        """create_versioned=True on a brand-new workflow uses the requested name and lands at CREATE_VERSIONED."""
        with patch.dict(WorkflowRegistry._workflows, {}, clear=True):
            target = self._determine(
                griptape_nodes,
                requested_file_name="brand_new_flow",
                current_workflow_name=None,
                create_versioned=True,
            )

            assert target.scenario == WorkflowManager.SaveWorkflowScenario.CREATE_VERSIONED
            assert target.destination is not None
            # Destination carries the create_versioned_workflow macro with an
            # unresolved sequence-slot marker (`{_index:NN}` legacy or `{###}` modern).
            macro = target.destination._file.location
            assert "_index" in macro or "#" in macro
            # Base name comes from the explicit request; relative_file_path reflects
            # the unresolved (pre-seed) form.
            assert target.relative_file_path == "brand_new_flow.py"
            assert target.file_path is None

    # --- #4956 helper: Step 2 (match) / Step 3 (no-match) / Step 1b (unsaved) -----------

    def test_step2_match_path_with_customized_versioned_macro(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """Customizing the versioned situation's macro still round-trips: matched dict goes through verbatim.

        Regression coverage for #4956 — confirms we don't hardcode any variable
        names in workflow_manager. The macro is the only thing that says which
        variables exist and what values they have.
        """
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.common.project_templates.situation import (
            BuiltInSituation,
            SituationFilePolicy,
            SituationPolicy,
            SituationTemplate,
        )
        from griptape_nodes.retained_mode.events.project_events import (
            LoadProjectTemplateRequest,
            LoadProjectTemplateResultSuccess,
            SetCurrentProjectRequest,
        )

        # Customize create_versioned_workflow to a dot-separated 4-digit suffix.
        custom_versioned = SituationTemplate(
            name=BuiltInSituation.CREATE_VERSIONED_WORKFLOW,
            description="Dot-separated versioning",
            macro="{workspace_dir}/{sub_dirs?:/}{file_name_base}.{_index:04}.{file_extension}",
            policy=SituationPolicy(on_collision=SituationFilePolicy.CREATE_NEW, create_dirs=True),
            fallback=BuiltInSituation.SAVE_FILE,
        )
        custom = DEFAULT_PROJECT_TEMPLATE.model_copy(
            update={
                "situations": {
                    **DEFAULT_PROJECT_TEMPLATE.situations,
                    BuiltInSituation.CREATE_VERSIONED_WORKFLOW: custom_versioned,
                }
            }
        )
        project_yml = temp_dir / "project_template.yml"
        project_yml.write_text(custom.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE))
        load_result = GriptapeNodes.handle_request(LoadProjectTemplateRequest(project_path=project_yml))
        assert isinstance(load_result, LoadProjectTemplateResultSuccess)
        GriptapeNodes.handle_request(SetCurrentProjectRequest(project_id=load_result.project_id))
        # SetCurrentProjectRequest re-derives workspace_path; force it back.
        griptape_nodes.ConfigManager().workspace_path = temp_dir

        with patch.dict(WorkflowRegistry._workflows, {}, clear=True):
            # Workflow that was previously saved under this customized macro
            # (so its on-disk name ends in `.0017.py`, not the default `_v017`).
            self._register_saved_workflow(
                temp_dir,
                registry_key="my_flow.0017",
                file_name="my_flow.0017.py",
                display_name="my_flow",
            )

            target = self._determine(
                griptape_nodes,
                requested_file_name=None,
                current_workflow_name="my_flow.0017",
                create_versioned=True,
            )

            # Macro reverse-match identified `.0017` as the version component
            # (per the customized macro). Every variable the macro extracted
            # rides in the destination's MacroPath, including bound `_index=17`.
            assert target.destination is not None
            macro_vars = target.destination._file._file_path.variables  # type: ignore[union-attr]
            assert macro_vars.get("file_name_base") == "my_flow"
            assert macro_vars.get("_index") == 17  # noqa: PLR2004 - literal version from setup
            assert macro_vars.get("file_extension") == "py"

    def test_step3_no_match_path_falls_back_to_file_stem(self, griptape_nodes: GriptapeNodes, temp_dir: Path) -> None:
        """A workflow saved under a NON-versioned situation produces a new versioned series on create_versioned.

        The file `random_name.py` doesn't match the versioned situation's macro
        (no `_v###` suffix). The helper falls through to
        ``_resolve_named_save_path("random_name", ...)`` which is the same
        plumbing every non-versioned SAVE_AS uses. The first versioned save
        produces ``random_name_v001.py`` alongside the original; the original
        file is left untouched (no in-place overwrite).
        """
        with patch.dict(WorkflowRegistry._workflows, {}, clear=True):
            self._register_saved_workflow(
                temp_dir,
                registry_key="random_name",
                file_name="random_name.py",  # No _v### suffix → won't match
                display_name="random_name",
            )

            target = self._determine(
                griptape_nodes,
                requested_file_name=None,
                current_workflow_name="random_name",
                create_versioned=True,
            )

            # Step 3 path: destination uses random_name as file_name_base;
            # _index is unbound so the seed-and-retry assigns 1 on write.
            assert target.scenario == WorkflowManager.SaveWorkflowScenario.CREATE_VERSIONED
            assert target.destination is not None
            macro_vars = target.destination._file._file_path.variables  # type: ignore[union-attr]
            assert macro_vars.get("file_name_base") == "random_name"
            assert "_index" not in macro_vars, (
                "Step 3 (no-match) must leave _index unbound so the seed-and-retry assigns 1 on write."
            )

    def test_step1b_unsaved_workflow_uses_display_name(self, griptape_nodes: GriptapeNodes) -> None:
        """create_versioned=True on an unsaved workflow uses display_name as the base.

        The unsaved workflow has no file_path → Step 1b kicks in →
        display name (metadata.name) is sanitized and fed through
        ``_resolve_named_save_path``. The first versioned save will land
        at ``<sanitized_display>_v001.py`` via the seed-and-retry.
        """
        unsaved_key = "unsaved:step1b-test"
        with patch.dict(WorkflowRegistry._workflows, {}, clear=True):
            _register_unsaved_workflow(key=unsaved_key, name="My Pretty Flow")

            target = self._determine(
                griptape_nodes,
                requested_file_name=None,
                current_workflow_name=unsaved_key,
                create_versioned=True,
            )

            assert target.scenario == WorkflowManager.SaveWorkflowScenario.CREATE_VERSIONED
            assert target.destination is not None
            macro_vars = target.destination._file._file_path.variables  # type: ignore[union-attr]
            # Display name sanitized: "My Pretty Flow" → "My_Pretty_Flow".
            assert macro_vars.get("file_name_base") == "My_Pretty_Flow"
            # No index assigned yet — the seed handles that on write.
            assert "_index" not in macro_vars

    def test_create_versioned_invalid_situation_macro_raises_value_error(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """A malformed situation macro surfaces as a clear ValueError, not an opaque crash.

        ``SituationTemplate`` validates macro syntax at template load, so
        production never reaches this branch. The defensive guard catches
        any case where a bad macro slips past validation (e.g. tests, future
        refactors, dynamic situation construction). Pins that the error
        message identifies which situation is broken.
        """
        with patch.dict(WorkflowRegistry._workflows, {}, clear=True):
            self._register_saved_workflow(
                temp_dir, registry_key="my_flow_v001", file_name="my_flow_v001.py", display_name="my_flow"
            )
            from griptape_nodes.common.macro_parser import MacroSyntaxError

            def raise_bad_macro(_template: str) -> None:
                msg = "synthetic malformed macro for test"
                raise MacroSyntaxError(msg)

            with (
                patch(
                    "griptape_nodes.retained_mode.managers.workflow_manager.ParsedMacro",
                    side_effect=raise_bad_macro,
                ),
                pytest.raises(ValueError, match="create_versioned_workflow"),
            ):
                self._determine(
                    griptape_nodes,
                    requested_file_name=None,
                    current_workflow_name="my_flow_v001",
                    create_versioned=True,
                )

    def test_create_versioned_missing_situation_raises_value_error(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """When the active project lacks the versioned situation, the reverse-match raises with the situation name.

        Pins the GetSituationResultSuccess guard at the top of
        ``_try_match_versioned_destination``. Drives the path where the
        match attempt happens (a registered workflow with a file_path) so
        the helper runs end-to-end, then patches ``handle_request`` to
        simulate the situation being absent.
        """
        from griptape_nodes.retained_mode.events.base_events import RequestPayload
        from griptape_nodes.retained_mode.events.project_events import (
            GetSituationRequest,
            GetSituationResultFailure,
        )

        with patch.dict(WorkflowRegistry._workflows, {}, clear=True):
            self._register_saved_workflow(
                temp_dir, registry_key="my_flow_v001", file_name="my_flow_v001.py", display_name="my_flow"
            )

            real_handle = GriptapeNodes.handle_request

            def fake_handle(req: RequestPayload) -> object:
                if isinstance(req, GetSituationRequest):
                    return GetSituationResultFailure(result_details="not found")
                return real_handle(req)

            with (
                patch.object(GriptapeNodes, "handle_request", side_effect=fake_handle),
                pytest.raises(ValueError, match="not found in the active project template"),
            ):
                self._determine(
                    griptape_nodes,
                    requested_file_name=None,
                    current_workflow_name="my_flow_v001",
                    create_versioned=True,
                )

    def test_create_versioned_match_dispatch_failure_surfaces_underlying_cause(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """When the match handler crashes, the error message includes the underlying exception.

        Pins the better-error-message branch in ``_try_match_versioned_destination``:
        if ``GriptapeNodes.handle_request`` catches an exception in the match
        handler and returns a generic ResultPayloadFailure, the wrapper raises
        ``ValueError`` with both the result_details and the underlying
        exception type and message so future debugging starts with the real
        cause, not an opaque dispatch wrapper.
        """
        from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayloadFailure
        from griptape_nodes.retained_mode.events.project_events import (
            AttemptMatchPathAgainstMacroRequest,
        )

        with patch.dict(WorkflowRegistry._workflows, {}, clear=True):
            self._register_saved_workflow(
                temp_dir, registry_key="my_flow_v001", file_name="my_flow_v001.py", display_name="my_flow"
            )

            real_handle = GriptapeNodes.handle_request
            synthetic_exc = RuntimeError("synthetic match-handler crash for test")

            def fake_handle(req: RequestPayload) -> object:
                if isinstance(req, AttemptMatchPathAgainstMacroRequest):
                    return ResultPayloadFailure(
                        exception=synthetic_exc,
                        result_details="dispatch caught an exception",
                    )
                return real_handle(req)

            with (
                patch.object(GriptapeNodes, "handle_request", side_effect=fake_handle),
                pytest.raises(ValueError, match="synthetic match-handler crash") as exc_info,
            ):
                self._determine(
                    griptape_nodes,
                    requested_file_name=None,
                    current_workflow_name="my_flow_v001",
                    create_versioned=True,
                )

            # The message includes both the underlying exception's class name and
            # its text — future debuggers shouldn't have to chase the original cause.
            assert "RuntimeError" in str(exc_info.value)
            assert "dispatch caught an exception" in str(exc_info.value)

    def test_create_versioned_timestamp_fallback_when_no_candidate_workflow(
        self,
        griptape_nodes: GriptapeNodes,
    ) -> None:
        """Step 5: no current AND no target workflow → timestamp-derived filename.

        Pins the last-resort branch of ``_resolve_versioned_save_target``:
        when there's nothing in scope to derive a base name from, the
        helper falls back to a ``%d.%m_%H.%M`` timestamp routed through
        the standard ``_resolve_named_save_path`` plumbing.
        """
        with patch.dict(WorkflowRegistry._workflows, {}, clear=True):
            # Empty registry + no current workflow + no target → Step 5.
            target = self._determine(
                griptape_nodes,
                requested_file_name=None,
                current_workflow_name=None,
                create_versioned=True,
            )

            assert target.scenario == WorkflowManager.SaveWorkflowScenario.CREATE_VERSIONED
            assert target.destination is not None
            macro_vars = target.destination._file._file_path.variables  # type: ignore[union-attr]
            # The base name is a timestamp matching DD.MM_HH.MM; the index is
            # unbound so the seed-and-retry assigns 001 on write.
            base = macro_vars.get("file_name_base")
            assert isinstance(base, str)
            assert re.fullmatch(r"\d{2}\.\d{2}_\d{2}\.\d{2}", base), (
                f"Expected timestamp pattern DD.MM_HH.MM but got {base!r}"
            )
            assert "_index" not in macro_vars


class TestSaveWorkflowDisplayNameFallback:
    """Pin the display-name fallback chain in ``on_save_workflow_request``.

    Four branches with strict precedence (high → low):
    1. ``request.display_name`` — explicit caller intent always wins.
    2. ``existing.display_name`` — preserves human-readable label across re-saves.
    3. The local ``file_name`` produced by ``_determine_save_target`` — either the user's
       typed Save-As stem (so "a" stays "a" and doesn't become "a_v001"), or the sanitized
       metadata-derived stem for a first-save-of-unsaved-workflow (so "unsaved:<uuid>"
       never leaks into ``metadata.name``).
    4. ``None`` → metadata generator's last-resort fallback uses resolved file_name.

    Drives ``on_save_workflow_request`` end-to-end with mocks; intercepts
    ``_save_workflow_file_inline`` to capture the ``display_name`` argument
    that the chain produced.
    """

    @staticmethod
    def _capture_display_name(
        griptape_nodes: GriptapeNodes,
        *,
        request_file_name: str | None,
        request_display_name: str | None,
        current_workflow_name: str | None,
    ) -> str | None:
        """Drive on_save_workflow_request and return the display_name handed to _save_workflow_file_inline."""
        from datetime import UTC, datetime

        from griptape_nodes.node_library.workflow_registry import WorkflowMetadata
        from griptape_nodes.retained_mode.events.flow_events import (
            GetTopLevelFlowRequest,
            GetTopLevelFlowResultSuccess,
            SerializedFlowCommands,
            SerializeFlowToCommandsRequest,
            SerializeFlowToCommandsResultSuccess,
        )
        from griptape_nodes.retained_mode.events.workflow_events import (
            SaveWorkflowFileFromSerializedFlowResultSuccess,
            SaveWorkflowRequest,
        )

        workflow_manager = griptape_nodes.WorkflowManager()
        context_manager = griptape_nodes.ContextManager()

        empty_commands = SerializedFlowCommands(
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
        saved_metadata = WorkflowMetadata(
            name="placeholder",
            schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
            engine_version_created_with="test",
            node_libraries_referenced=[],
            creation_date=datetime.now(UTC),
        )
        captured: dict[str, str | None] = {}

        async def fake_ahandle_request(req: object) -> object:
            if isinstance(req, GetTopLevelFlowRequest):
                return GetTopLevelFlowResultSuccess(flow_name="ControlFlow_1", result_details="ok")
            if isinstance(req, SerializeFlowToCommandsRequest):
                return SerializeFlowToCommandsResultSuccess(
                    serialized_flow_commands=empty_commands, result_details="ok"
                )
            msg = f"Unexpected request type in test: {type(req).__name__}"
            raise AssertionError(msg)

        def capture_inline(**kwargs: object) -> SaveWorkflowFileFromSerializedFlowResultSuccess:
            # display_name is what we're testing — record it then return success.
            captured["display_name"] = kwargs.get("display_name")  # type: ignore[assignment]
            workspace = griptape_nodes.ConfigManager().workspace_path
            file_name = kwargs.get("file_name", "stub")
            return SaveWorkflowFileFromSerializedFlowResultSuccess(
                file_path=str(workspace / f"{file_name}.py"),
                workflow_metadata=saved_metadata,
                result_details="ok",
            )

        pushed_context = False
        if current_workflow_name is not None:
            context_manager.push_workflow(workflow_name=current_workflow_name)
            pushed_context = True

        try:
            with (
                patch.object(GriptapeNodes, "ahandle_request", side_effect=fake_ahandle_request),
                patch.object(workflow_manager, "_save_workflow_file_inline", side_effect=capture_inline),
                patch.object(workflow_manager, "extract_workflow_shape", side_effect=ValueError("no shape")),
            ):
                asyncio.run(
                    workflow_manager.on_save_workflow_request(
                        SaveWorkflowRequest(file_name=request_file_name, display_name=request_display_name)
                    )
                )
        finally:
            if pushed_context and context_manager.has_current_workflow():
                context_manager.pop_workflow()

        return captured.get("display_name")

    def test_explicit_request_display_name_wins(self, griptape_nodes: GriptapeNodes) -> None:
        """Branch 1: ``request.display_name`` is explicit caller intent and always wins.

        Even when the registry has an existing display_name AND request.file_name
        is set, the explicit request.display_name still takes precedence.
        """
        unsaved_key = "unsaved:branch1-test"
        with patch.dict(WorkflowRegistry._workflows, {}, clear=True):
            _register_unsaved_workflow(key=unsaved_key, name="In-Memory Label")

            captured = TestSaveWorkflowDisplayNameFallback._capture_display_name(
                griptape_nodes,
                request_file_name="some_file_name",
                request_display_name="Caller-Specified Label",
                current_workflow_name=unsaved_key,
            )

            assert captured == "Caller-Specified Label"

    def test_existing_registry_display_name_used_when_request_omits(self, griptape_nodes: GriptapeNodes) -> None:
        """Branch 2: when request.display_name is None, the registry's existing display_name wins over the file_name.

        Pins the "preserve human-readable label across re-saves" rule — a
        second save of an already-registered workflow keeps its existing
        display name rather than resetting it from the typed/resolved
        file_name.
        """
        from griptape_nodes.node_library.workflow_registry import WorkflowMetadata

        registry_key = "preserved_display_name_test"
        workspace = griptape_nodes.ConfigManager().workspace_path
        stub_path = workspace / f"{registry_key}.py"
        with patch.dict(WorkflowRegistry._workflows, {}, clear=True):
            stub_path.write_text("# stub")
            try:
                metadata = WorkflowMetadata(
                    name="Preserved Display Name",
                    schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
                    engine_version_created_with="test",
                    node_libraries_referenced=[],
                    creation_date=datetime.now(UTC),
                )
                WorkflowRegistry.generate_new_workflow(
                    registry_key=registry_key, metadata=metadata, file_path=f"{registry_key}.py"
                )

                captured = TestSaveWorkflowDisplayNameFallback._capture_display_name(
                    griptape_nodes,
                    request_file_name=registry_key,
                    request_display_name=None,
                    current_workflow_name=registry_key,
                )

                # Registry value wins over the typed file_name fallback.
                assert captured == "Preserved Display Name"
            finally:
                stub_path.unlink(missing_ok=True)

    def test_no_request_display_name_no_existing_metadata_uses_resolved_file_name_stem(
        self, griptape_nodes: GriptapeNodes
    ) -> None:
        """Branch 3: fresh Save As with no registry entry — the resolved stem becomes the display name.

        This is the case that the manual-test bug surfaced: typing "a" should
        produce metadata.name="a" even though the macro resolves the on-disk
        name to "a_v001". The fallback uses ``_determine_save_target``'s
        resolved local ``file_name``, which strips any leading sub-directory
        so a typed "episode/my_wf" yields the leaf "my_wf".
        """
        unsaved_key = "unsaved:branch3-test"
        with patch.dict(WorkflowRegistry._workflows, {}, clear=True):
            _register_unsaved_workflow(key=unsaved_key, name="ignored - we use resolved file_name")

            captured = TestSaveWorkflowDisplayNameFallback._capture_display_name(
                griptape_nodes,
                request_file_name="episode/my_wf",
                request_display_name=None,
                current_workflow_name=unsaved_key,
            )

            # _resolve_named_save_path returns parts.stem — "my_wf", not "episode/my_wf".
            assert captured == "my_wf"

    def test_first_save_of_unsaved_uses_metadata_derived_stem_not_synthetic_key(
        self, griptape_nodes: GriptapeNodes
    ) -> None:
        """Fresh save of an unsaved workflow must not surface "unsaved:<uuid>" as the display name.

        Regression guard for the bug where branch 3 read ``Path(request.file_name).stem``
        and returned the whole "unsaved:<uuid>" string verbatim (no dot). Fix: branch 3
        reads the local ``file_name`` from ``_determine_save_target``, which already
        stripped the prefix and rebuilt the stem from ``metadata.name``.
        """
        unsaved_key = "unsaved:branch3-regression"
        with patch.dict(WorkflowRegistry._workflows, {}, clear=True):
            _register_unsaved_workflow(key=unsaved_key, name="My Cool Workflow")

            captured = TestSaveWorkflowDisplayNameFallback._capture_display_name(
                griptape_nodes,
                request_file_name=unsaved_key,
                request_display_name=None,
                current_workflow_name=unsaved_key,
            )

            assert captured is not None
            assert not captured.startswith("unsaved:"), f"synthetic key leaked into display name: {captured!r}"
            # The resolved stem is the sanitized metadata.name.
            assert captured == "My_Cool_Workflow"

    def test_typed_save_as_name_survives_versioned_resolution(self, griptape_nodes: GriptapeNodes) -> None:
        """The user typed "a" — display name stays "a" even when the macro resolves the file to "a_v001".

        Pins the versioned-save UI re-save fix from commit 5c3f0bf3: branch 3 must feed off
        the ``_determine_save_target`` local (which is the user's typed stem), not the
        macro-resolved on-disk name. Without this, the display name would jump to
        "a_v001" the moment a versioned save landed.
        """
        unsaved_key = "unsaved:typed-save-as"
        with patch.dict(WorkflowRegistry._workflows, {}, clear=True):
            _register_unsaved_workflow(key=unsaved_key, name="ignored - user typed a new name")

            captured = TestSaveWorkflowDisplayNameFallback._capture_display_name(
                griptape_nodes,
                request_file_name="a",
                request_display_name=None,
                current_workflow_name=unsaved_key,
            )

            assert captured == "a"

    def test_metadata_derived_stem_used_when_no_file_name_and_no_existing(self, griptape_nodes: GriptapeNodes) -> None:
        """Branch 3 (no request.file_name path): the metadata-derived stem still fills the display name.

        Prior expectation for this scenario was ``None`` (rung 4), because rung 3 keyed off
        ``request.file_name`` alone. After the fix, rung 3 uses the resolved local
        ``file_name`` that ``_determine_save_target`` synthesizes from the unsaved workflow's
        ``metadata.name`` — so the chain resolves before falling through to ``None``.
        """
        unsaved_key = "unsaved:branch4-legacy"
        with patch.dict(WorkflowRegistry._workflows, {}, clear=True):
            _register_unsaved_workflow(key=unsaved_key, name="Used Only For Filename Derivation")

            captured = TestSaveWorkflowDisplayNameFallback._capture_display_name(
                griptape_nodes,
                request_file_name=None,
                request_display_name=None,
                current_workflow_name=unsaved_key,
            )

            assert captured == "Used_Only_For_Filename_Derivation"


class TestScrubForAstConstant:
    """Regression coverage for griptape-nodes-engine#5013.

    A Button trait object placed inside a parameter's ``ui_options`` dict (instead of
    attached via ``traits=``) survives serialization and, when emitted by codegen via
    ``ast.Constant``, is rendered as its ``repr()`` (``<function ... at 0x...>``), which
    is invalid Python and prevents the saved workflow from reopening.
    """

    def test_primitive_values_are_safe(self) -> None:
        for value in ["s", b"b", True, 1, 1.5, None]:
            assert WorkflowManager._is_ast_constant_safe(value) is True

    def test_nested_container_of_primitives_is_safe(self) -> None:
        value = {"a": [1, 2, {"b": ("x", None)}], "c": {1, 2}}
        assert WorkflowManager._is_ast_constant_safe(value) is True

    def test_callable_leaf_is_unsafe(self) -> None:
        assert WorkflowManager._is_ast_constant_safe(lambda: None) is False

    def test_scrub_leaves_safe_value_untouched(self) -> None:
        value = {"display_name": "X", "hide": True, "nums": [1, 2]}
        result = WorkflowManager._scrub_for_ast_constant(value)
        assert result.dropped is False
        assert result.value == value

    def test_scrub_drops_button_in_ui_options_traits(self) -> None:
        from griptape_nodes.traits.button import Button

        button = Button(size="icon", icon="audio-lines", tooltip="Search", button_link="https://example.com")
        ui_options = {
            "display_name": "Custom Voice ID",
            "hide": True,
            "placeholder_text": "e.g., 21m00Tcm4TlvDq8ikWAM",
            "traits": [button],
        }

        result = WorkflowManager._scrub_for_ast_constant(ui_options)

        assert result.dropped is True
        # Safe keys are preserved; the unsafe Button is removed, leaving an empty traits list.
        assert result.value == {
            "display_name": "Custom Voice ID",
            "hide": True,
            "placeholder_text": "e.g., 21m00Tcm4TlvDq8ikWAM",
            "traits": [],
        }

    def test_keyword_from_field_value_emits_valid_python(self) -> None:
        from griptape_nodes.traits.button import Button

        button = Button(icon="key", tooltip="Open", button_link="https://example.com")
        ui_options = {"display_name": "X", "traits": [button]}

        keyword = WorkflowManager._keyword_from_field_value("ui_options", ui_options, object())
        call_node = ast.Call(
            func=ast.Name(id="Req", ctx=ast.Load()),
            args=[],
            keywords=[keyword],
        )
        source = ast.unparse(ast.fix_missing_locations(call_node))

        # The rendered source must be free of the invalid function repr and must parse.
        assert "0x" not in source
        assert "<function" not in source
        assert "Button(" not in source
        ast.parse(source)

    def test_scrub_namedtuple_with_unsafe_leaf_does_not_crash(self) -> None:
        """A namedtuple (tuple subclass) containing an unsafe leaf must not raise.

        ``type(value)(scrubbed_items)`` would call the namedtuple's positional
        ``__new__`` with a single list arg and raise TypeError, turning graceful
        degradation into a hard crash of the whole save. It must degrade to a plain
        tuple instead.
        """

        class Point(NamedTuple):
            x: int
            handler: object

        value = Point(1, lambda: None)
        result = WorkflowManager._scrub_for_ast_constant(value)

        assert result.dropped is True
        # Reconstructed as a plain tuple with the unsafe leaf removed.
        assert result.value == (1,)
        assert type(result.value) is tuple

    def test_generate_workflow_file_content_scrubs_button_in_ui_options(self, griptape_nodes: GriptapeNodes) -> None:
        """End-to-end regression for #5013: a Button in ui_options must not break the saved file.

        Drives the real generator (``_generate_workflow_file_content``) with a node whose
        AlterParameterDetailsRequest carries a Button inside ``ui_options`` (the exact shape
        the standalone ElevenLabs library produced). The emitted module must be valid,
        reopenable Python with no function repr leaking through.
        """
        from griptape_nodes.node_library.library_registry import LibraryNameAndVersion
        from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, SerializedNodeCommands
        from griptape_nodes.retained_mode.events.parameter_events import AlterParameterDetailsRequest
        from griptape_nodes.traits.button import Button

        button = Button(size="icon", icon="audio-lines", tooltip="Search", button_link="https://example.com")
        alter_request = AlterParameterDetailsRequest(
            parameter_name="custom_voice_id",
            ui_options={
                "display_name": "Custom Voice ID",
                "hide": False,
                "traits": [button],
            },
            initial_setup=True,
        )
        node_command = SerializedNodeCommands(
            create_node_command=CreateNodeRequest(
                node_type="SomeNode",
                specific_library_name="Some Library",
                node_name="Node_1",
            ),
            element_modification_commands=[alter_request],
            node_dependencies=NodeDependencies(),
        )
        flow_commands = SerializedFlowCommands(
            flow_initialization_command=None,
            serialized_node_commands=[node_command],
            serialized_connections=[],
            unique_parameter_uuid_to_values={},
            set_parameter_value_commands={},
            set_lock_commands_per_node={},
            sub_flows_commands=[],
            node_dependencies=NodeDependencies(
                libraries={LibraryNameAndVersion(library_name="Some Library", library_version="0.1.0")}
            ),
            node_types_used=set(),
        )

        metadata = WorkflowMetadata(
            name="test_workflow",
            schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
            engine_version_created_with="1.0.0",
            node_libraries_referenced=[],
            workflow_shape=None,
        )
        content = griptape_nodes.WorkflowManager()._generate_workflow_file_content(
            serialized_flow_commands=flow_commands,
            workflow_metadata=metadata,
        )

        # The generated file must be valid, reopenable Python with no leaked function repr,
        # and the surviving ui_options must keep its safe keys with an emptied traits list.
        assert "<function" not in content
        assert "at 0x" not in content
        assert "_create_button_link_handler" not in content
        ast.parse(content)
        assert "'traits': []" in content
        assert "'display_name': 'Custom Voice ID'" in content


class TestSelectTopLevelImportedFlow:
    """WorkflowManager._select_top_level_imported_flow picks the top-level imported flow."""

    def _flow_manager_with_parents(self, monkeypatch: pytest.MonkeyPatch, parents: dict[str, str]) -> None:
        """Stub GriptapeNodes.FlowManager().get_parent_flow via the given name->parent mapping."""
        flow_manager = Mock(spec=FlowManager)
        flow_manager.get_parent_flow.side_effect = lambda flow: parents[flow]
        monkeypatch.setattr(GriptapeNodes, "FlowManager", lambda: flow_manager)

    def test_selects_flow_parented_to_import_target(
        self, griptape_nodes: GriptapeNodes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workflow_manager = griptape_nodes.WorkflowManager()
        # Top-level flow is parented to the target; the group's body flow is parented to the top-level.
        self._flow_manager_with_parents(monkeypatch, {"ControlFlow_2": "ParentFlow", "Group_subflow": "ControlFlow_2"})

        selected = workflow_manager._select_top_level_imported_flow(
            {"ControlFlow_2", "Group_subflow"}, "ParentFlow", "grouped_inner_workflow"
        )

        assert selected == "ControlFlow_2"

    def test_falls_back_deterministically_when_none_parented_to_target(
        self, griptape_nodes: GriptapeNodes, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        workflow_manager = griptape_nodes.WorkflowManager()
        self._flow_manager_with_parents(monkeypatch, {"Zeta_flow": "Other", "Alpha_flow": "Other"})

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            selected = workflow_manager._select_top_level_imported_flow({"Zeta_flow", "Alpha_flow"}, "ParentFlow", "wf")

        # No flow parented to the target: deterministic sorted fallback, never hash-ordered.
        assert selected == "Alpha_flow"
        assert any(
            record.levelno == logging.WARNING and "expected exactly one top-level flow" in record.getMessage()
            for record in caplog.records
        ), f"expected a fallback warning, got: {[r.getMessage() for r in caplog.records]}"

    def test_falls_back_deterministically_when_multiple_parented_to_target(
        self, griptape_nodes: GriptapeNodes, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        workflow_manager = griptape_nodes.WorkflowManager()
        self._flow_manager_with_parents(monkeypatch, {"Zeta_flow": "ParentFlow", "Alpha_flow": "ParentFlow"})

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            selected = workflow_manager._select_top_level_imported_flow({"Zeta_flow", "Alpha_flow"}, "ParentFlow", "wf")

        # More than one candidate parented to the target: deterministic sorted fallback.
        assert selected == "Alpha_flow"
        assert any(
            record.levelno == logging.WARNING and "expected exactly one top-level flow" in record.getMessage()
            for record in caplog.records
        ), f"expected a fallback warning, got: {[r.getMessage() for r in caplog.records]}"


class TestExecuteWorkflowImport:
    """WorkflowManager._execute_workflow_import tests."""

    @pytest.mark.asyncio
    async def test_returns_top_level_imported_flow(
        self, griptape_nodes: GriptapeNodes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workflow_manager = griptape_nodes.WorkflowManager()

        request = ImportWorkflowAsReferencedSubFlowRequest(
            workflow_name="wf", flow_name="ParentFlow", imported_flow_metadata=None, track_as_referenced=False
        )
        workflow = Mock(spec=Workflow)
        workflow.file_path = "workflows/wf.py"

        # The import creates two new flows (a top-level flow + a group's body flow). The dict
        # values are never inspected -- only the keys form the new-flows set.
        object_manager = Mock(spec=ObjectManager)
        object_manager.get_filtered_subset.side_effect = [
            {"ParentFlow": object()},
            {"ParentFlow": object(), "ControlFlow_2": object(), "Group_subflow": object()},
        ]
        monkeypatch.setattr(GriptapeNodes, "ObjectManager", lambda: object_manager)

        # ContextManager().flow(...) is used purely as a context manager wrapping the import.
        context_manager = MagicMock(spec=ContextManager)
        monkeypatch.setattr(GriptapeNodes, "ContextManager", lambda: context_manager)

        monkeypatch.setattr(
            workflow_manager,
            "run_workflow",
            AsyncMock(
                return_value=WorkflowManager.WorkflowExecutionResult(execution_successful=True, execution_details="ok")
            ),
        )

        # Spy the selection helper (its own logic is covered by TestSelectTopLevelImportedFlow).
        select_mock = create_autospec(workflow_manager._select_top_level_imported_flow, return_value="ControlFlow_2")
        monkeypatch.setattr(workflow_manager, "_select_top_level_imported_flow", select_mock)

        result = await workflow_manager._execute_workflow_import(request, workflow, "ParentFlow")

        # It selects via the helper (passing the new-flows set, target flow, and workflow name)...
        select_mock.assert_called_once_with({"ControlFlow_2", "Group_subflow"}, "ParentFlow", "wf")
        # ...and returns exactly what the helper chose.
        assert isinstance(result, ImportWorkflowAsReferencedSubFlowResultSuccess)
        assert result.created_flow_name == "ControlFlow_2"
