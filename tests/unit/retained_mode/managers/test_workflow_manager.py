import ast
import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple
from unittest.mock import AsyncMock, MagicMock, call, patch

import anyio
import pytest

if TYPE_CHECKING:
    from griptape_nodes.node_library.workflow_registry import WorkflowMetadata
    from griptape_nodes.retained_mode.events.node_events import SerializedNodeCommands

from griptape_nodes.exe_types.core_types import Parameter
from griptape_nodes.exe_types.node_types import NodeDependencies
from griptape_nodes.node_library.workflow_registry import WorkflowMetadata, WorkflowRegistry, WorkflowShape
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

            save_file_success = SaveWorkflowFileFromSerializedFlowResultSuccess(
                file_path=f"/workspace/{saved_key}.py",
                workflow_metadata=saved_metadata,
                result_details="ok",
            )

            try:
                with (
                    patch.object(GriptapeNodes, "ahandle_request", side_effect=fake_ahandle_request),
                    patch.object(
                        workflow_manager,
                        "on_save_workflow_file_from_serialized_flow_request",
                        new=AsyncMock(return_value=save_file_success),
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

            async def fake_save_file(request: object) -> object:
                captured["file_name"] = getattr(request, "file_name", None)
                captured["file_path"] = getattr(request, "file_path", None)
                return SaveWorkflowFileFromSerializedFlowResultSuccess(
                    file_path=str(getattr(request, "file_path", "")),
                    workflow_metadata=saved_metadata,
                    result_details="ok",
                )

            try:
                with (
                    patch.object(GriptapeNodes, "ahandle_request", side_effect=fake_ahandle_request),
                    patch.object(
                        workflow_manager,
                        "on_save_workflow_file_from_serialized_flow_request",
                        new=AsyncMock(side_effect=fake_save_file),
                    ),
                    patch.object(
                        workflow_manager,
                        "extract_workflow_shape",
                        side_effect=ValueError("no shape"),
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
                assert str(captured["file_path"]).endswith(f"{display_name}.py")
                assert unsaved_key not in str(captured["file_path"])
            finally:
                if context_manager.has_current_workflow():
                    context_manager.pop_workflow()

    class _RenameScenario(NamedTuple):
        workflow_name: str
        requested_name: str
        source_file_path: str
        save_file_path: str
        save_workflow_name: str

    def _run_rename(self, workflow_manager: WorkflowManager, scenario: "TestWorkflowManager._RenameScenario") -> dict:
        """Drive on_rename_workflow_request with mocked save/delete, capturing the SaveWorkflowRequest."""
        from griptape_nodes.retained_mode.events.workflow_events import (
            DeleteWorkflowResultSuccess,
            RenameWorkflowRequest,
            RenameWorkflowResultSuccess,
            SaveWorkflowRequest,
            SaveWorkflowResultSuccess,
        )

        mock_source = MagicMock()
        mock_source.file_path = scenario.source_file_path
        captured: dict[str, object] = {}

        async def fake_ahandle_request(req: object) -> object:
            if isinstance(req, SaveWorkflowRequest):
                captured["save_file_name"] = req.file_name
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
                    RenameWorkflowRequest(workflow_name=scenario.workflow_name, requested_name=scenario.requested_name)
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

    def test_resolve_named_save_path_absolute_skips_sub_dirs(self, griptape_nodes: GriptapeNodes) -> None:
        """An absolute requested name routes the full path to _build_workflow_save_path with no sub_dirs."""
        workflow_manager = griptape_nodes.WorkflowManager()
        # Anchor to the current filesystem root so the path is absolute on Windows
        # (which needs a drive letter) as well as POSIX.
        abs_requested = Path(Path.cwd().anchor) / "ext" / "new_name"
        abs_path = abs_requested.with_suffix(".py")

        with patch.object(
            workflow_manager,
            "_build_workflow_save_path",
            return_value=WorkflowManager.WorkflowSavePath(file_path=abs_path, relative_file_path=str(abs_path)),
        ) as mock_build:
            resolved = workflow_manager._resolve_named_save_path(str(abs_requested))

        mock_build.assert_called_once_with(f"{abs_requested}.py")
        assert resolved.file_name == "new_name"
        assert resolved.relative_file_path == str(abs_path)

    def test_resolve_named_save_path_relative_passes_sub_dirs(self, griptape_nodes: GriptapeNodes) -> None:
        """A relative requested name splits into stem + sub_dirs (unchanged behavior)."""
        workflow_manager = griptape_nodes.WorkflowManager()
        workspace = griptape_nodes.ConfigManager().workspace_path
        resolved_path = workspace / "team" / "new_name.py"

        with patch.object(
            workflow_manager,
            "_build_workflow_save_path",
            return_value=WorkflowManager.WorkflowSavePath(
                file_path=resolved_path, relative_file_path=str(Path("team") / "new_name.py")
            ),
        ) as mock_build:
            resolved = workflow_manager._resolve_named_save_path("team/new_name")

        mock_build.assert_called_once_with("new_name.py", sub_dirs="team")
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

    def test_build_workflow_save_path_resolves_via_situation(self, griptape_nodes: GriptapeNodes) -> None:
        """Resolved paths inside the workspace yield a workspace-relative registry key."""
        workflow_manager = griptape_nodes.WorkflowManager()
        workspace = griptape_nodes.ConfigManager().workspace_path
        resolved_path = workspace / "my_workflow.py"

        fake_destination = MagicMock()
        fake_destination.resolve.return_value = str(resolved_path)

        with patch(
            "griptape_nodes.retained_mode.managers.workflow_manager.ProjectFileDestination.from_situation",
            return_value=fake_destination,
        ) as mock_from_situation:
            save_path = workflow_manager._build_workflow_save_path("my_workflow.py")

        mock_from_situation.assert_called_once_with("my_workflow.py", "save_workflow")
        assert save_path.file_path == resolved_path
        assert save_path.relative_file_path == "my_workflow.py"

    def test_build_workflow_save_path_preserves_sub_dirs(self, griptape_nodes: GriptapeNodes) -> None:
        """sub_dirs are passed to the situation and reflected in the resolved workspace-relative key."""
        workflow_manager = griptape_nodes.WorkflowManager()
        workspace = griptape_nodes.ConfigManager().workspace_path
        resolved_path = workspace / "team" / "my_workflow.py"

        fake_destination = MagicMock()
        fake_destination.resolve.return_value = str(resolved_path)

        with patch(
            "griptape_nodes.retained_mode.managers.workflow_manager.ProjectFileDestination.from_situation",
            return_value=fake_destination,
        ) as mock_from_situation:
            save_path = workflow_manager._build_workflow_save_path("my_workflow.py", sub_dirs="team")

        mock_from_situation.assert_called_once_with("my_workflow.py", "save_workflow", sub_dirs="team")
        assert save_path.file_path == resolved_path
        assert save_path.relative_file_path == str(Path("team") / "my_workflow.py")

    def test_build_workflow_save_path_uses_absolute_when_outside_workspace(
        self, griptape_nodes: GriptapeNodes, tmp_path: Path
    ) -> None:
        """Paths outside the workspace fall back to the absolute path as the registry key."""
        workflow_manager = griptape_nodes.WorkflowManager()
        outside_path = tmp_path / "elsewhere" / "my_workflow.py"

        fake_destination = MagicMock()
        fake_destination.resolve.return_value = str(outside_path)

        with patch(
            "griptape_nodes.retained_mode.managers.workflow_manager.ProjectFileDestination.from_situation",
            return_value=fake_destination,
        ):
            save_path = workflow_manager._build_workflow_save_path("my_workflow.py")

        assert save_path.file_path == outside_path
        assert save_path.relative_file_path == str(outside_path)

    def test_build_workflow_save_path_falls_back_on_file_load_error(self, griptape_nodes: GriptapeNodes) -> None:
        """If the situation cannot resolve, we fall back to joining against the workspace path."""
        from griptape_nodes.files.file import FileLoadError
        from griptape_nodes.retained_mode.events.os_events import FileIOFailureReason

        workflow_manager = griptape_nodes.WorkflowManager()
        workspace = griptape_nodes.ConfigManager().workspace_path

        fake_destination = MagicMock()
        fake_destination.resolve.side_effect = FileLoadError(FileIOFailureReason.UNKNOWN, "no project loaded")

        with patch(
            "griptape_nodes.retained_mode.managers.workflow_manager.ProjectFileDestination.from_situation",
            return_value=fake_destination,
        ):
            save_path = workflow_manager._build_workflow_save_path("my_workflow.py", sub_dirs="team")

        assert save_path.file_path == workspace / "team" / "my_workflow.py"
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
        variables_manager.on_clear_object_state()

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
        variables_manager.on_clear_object_state()

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
