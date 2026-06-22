"""Tests for WorkflowRegistry functionality."""

import os
import platform
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from griptape_nodes.files.path_utils import derive_registry_key
from griptape_nodes.node_library.workflow_registry import Workflow, WorkflowRegistry
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes


class TestDeriveRegistryKey:
    def test_flat_workflow(self) -> None:
        assert derive_registry_key("my_workflow.py") == "my_workflow"

    def test_subdirectory(self) -> None:
        assert derive_registry_key("subdir/my_workflow.py") == "subdir/my_workflow"

    def test_nested_subdirectory(self) -> None:
        assert derive_registry_key("a/b/c/my_workflow.py") == "a/b/c/my_workflow"

    def test_backslash_normalization(self) -> None:
        assert derive_registry_key("subdir\\my_workflow.py") == "subdir/my_workflow"

    def test_no_extension(self) -> None:
        assert derive_registry_key("my_workflow") == "my_workflow"

    def test_dot_prefix_normalized(self) -> None:
        assert derive_registry_key("./my_workflow.py") == "my_workflow"

    @pytest.mark.parametrize(
        ("input_path", "expected"),
        [
            ("my_workflow.py", "my_workflow"),
            ("subdir/my_workflow.py", "subdir/my_workflow"),
            ("a/b/deep_workflow.py", "a/b/deep_workflow"),
            ("windows\\path\\workflow.py", "windows/path/workflow"),
        ],
    )
    def test_known_inputs(self, input_path: str, expected: str) -> None:
        assert derive_registry_key(input_path) == expected


class TestWorkflowRegistry:
    """Test suite for WorkflowRegistry functionality."""

    def test_get_complete_file_path_with_absolute_path(self) -> None:
        """Test that get_complete_file_path returns absolute paths as-is."""
        # Use a platform-appropriate absolute path
        if os.name == "nt":  # Windows
            absolute_path = "C:\\absolute\\path\\to\\workflow.py"
        else:  # Unix-like
            absolute_path = "/absolute/path/to/workflow.py"

        result = WorkflowRegistry.get_complete_file_path(absolute_path)

        # On Windows, paths starting with / are not considered absolute
        # so they get treated as relative paths
        if os.name == "nt" and absolute_path.startswith("/"):
            # On Windows, Unix-style paths are relative
            assert Path(result).is_absolute()
        else:
            assert result == absolute_path

    def test_get_complete_file_path_with_unix_style_on_windows(self) -> None:
        """Test Unix-style paths on Windows (treated as relative)."""
        unix_style_path = "/absolute/path/to/workflow.py"
        result = WorkflowRegistry.get_complete_file_path(unix_style_path)

        if os.name == "nt":  # Windows
            # Unix-style paths are treated as relative on Windows
            # The result should be the workspace path + the Unix path
            assert result.endswith("\\absolute\\path\\to\\workflow.py")
            # Verify it's been made absolute
            assert Path(result).is_absolute()
        else:
            # On Unix, this is an absolute path
            assert result == unix_style_path

    def test_get_complete_file_path_with_absolute_windows_path(self) -> None:
        """Test that get_complete_file_path handles Windows absolute paths."""
        windows_path = "C:\\Users\\test\\workflow.py"

        result = WorkflowRegistry.get_complete_file_path(windows_path)

        # On Windows, it should be returned as-is
        # On Unix systems, Path.is_absolute() returns False for Windows paths,
        # so it will be treated as relative
        if platform.system() == "Windows":
            assert result == windows_path
        else:
            # On Unix, Windows paths are treated as relative
            assert result.endswith("C:\\Users\\test\\workflow.py")

    def test_get_complete_file_path_with_relative_path(self, griptape_nodes: GriptapeNodes) -> None:
        """Test that get_complete_file_path resolves relative paths to workspace."""
        relative_path = "workflows/my_workflow.py"

        # Get the actual workspace path from the config manager
        workspace_path = griptape_nodes.ConfigManager().workspace_path

        result = WorkflowRegistry.get_complete_file_path(relative_path)

        expected = str(workspace_path / relative_path)
        assert result == expected

    def test_get_complete_file_path_with_home_expansion(self) -> None:
        """Test that get_complete_file_path handles paths with home directory expansion."""
        home_path = "~/workflows/my_workflow.py"

        # Home paths starting with ~ are NOT considered absolute by Path.is_absolute()
        # so they will be treated as relative paths
        result = WorkflowRegistry.get_complete_file_path(home_path)

        # Should be treated as relative and appended to workspace
        if os.name == "nt":  # Windows
            # On Windows, ~ is just a regular character in the path
            assert result.endswith("~\\workflows\\my_workflow.py")
        else:
            # On Unix, ~ is also treated as relative (not expanded)
            assert result.endswith("~/workflows/my_workflow.py")

    def test_get_complete_file_path_with_current_dir_relative(self, griptape_nodes: GriptapeNodes) -> None:
        """Test that get_complete_file_path handles current directory relative paths."""
        current_dir_path = "./my_workflow.py"

        # Get the actual workspace path from the config manager
        workspace_path = griptape_nodes.ConfigManager().workspace_path

        result = WorkflowRegistry.get_complete_file_path(current_dir_path)

        expected = str(workspace_path / current_dir_path)
        assert result == expected

    def test_get_complete_file_path_with_parent_dir_relative(self, griptape_nodes: GriptapeNodes) -> None:
        """Test that get_complete_file_path handles parent directory relative paths."""
        parent_dir_path = "../external/my_workflow.py"

        # Get the actual workspace path from the config manager
        workspace_path = griptape_nodes.ConfigManager().workspace_path

        result = WorkflowRegistry.get_complete_file_path(parent_dir_path)

        # resolve_workspace_path normalizes the path by resolving .. components
        expected = str((workspace_path / parent_dir_path).resolve())
        assert result == expected


class TestWorkflowRegistryOperations:
    """Tests for WorkflowRegistry CRUD operations."""

    def test_rekey_workflow_updates_registry_key(self) -> None:
        mock_workflow = MagicMock()
        with patch.dict(WorkflowRegistry._workflows, {"old_key": mock_workflow}, clear=True):
            WorkflowRegistry.rekey_workflow("old_key", "new_key")

            assert "new_key" in WorkflowRegistry._workflows
            assert "old_key" not in WorkflowRegistry._workflows
            assert WorkflowRegistry._workflows["new_key"] is mock_workflow

    def test_rekey_workflow_missing_key_raises(self) -> None:
        with patch.dict(WorkflowRegistry._workflows, {}, clear=True), pytest.raises(KeyError, match="not_there"):
            WorkflowRegistry.rekey_workflow("not_there", "new_key")

    def test_generate_new_workflow_uses_caller_supplied_key(self) -> None:
        mock_metadata = MagicMock()
        with (
            patch.dict(WorkflowRegistry._workflows, {}, clear=True),
            patch.object(WorkflowRegistry, "get_complete_file_path", return_value="/workspace/my_workflow.py"),
            patch.object(Path, "is_file", return_value=True),
        ):
            workflow = WorkflowRegistry.generate_new_workflow(
                registry_key="my_workflow", metadata=mock_metadata, file_path="my_workflow.py"
            )

            assert "my_workflow" in WorkflowRegistry._workflows
            assert WorkflowRegistry._workflows["my_workflow"] is workflow

    def test_generate_new_workflow_same_key_different_paths_collide(self) -> None:
        mock_metadata = MagicMock()
        with (
            patch.dict(WorkflowRegistry._workflows, {}, clear=True),
            patch.object(WorkflowRegistry, "get_complete_file_path", return_value="/workspace/some/path.py"),
            patch.object(Path, "is_file", return_value=True),
        ):
            WorkflowRegistry.generate_new_workflow(
                registry_key="subdir_a/my_workflow", metadata=mock_metadata, file_path="subdir_a/my_workflow.py"
            )
            WorkflowRegistry.generate_new_workflow(
                registry_key="subdir_b/my_workflow", metadata=mock_metadata, file_path="subdir_b/my_workflow.py"
            )

            assert "subdir_a/my_workflow" in WorkflowRegistry._workflows
            assert "subdir_b/my_workflow" in WorkflowRegistry._workflows

    def test_generate_new_workflow_duplicate_key_raises(self) -> None:
        mock_metadata = MagicMock()
        with (
            patch.dict(WorkflowRegistry._workflows, {}, clear=True),
            patch.object(WorkflowRegistry, "get_complete_file_path", return_value="/workspace/my_workflow.py"),
            patch.object(Path, "is_file", return_value=True),
        ):
            WorkflowRegistry.generate_new_workflow(
                registry_key="my_workflow", metadata=mock_metadata, file_path="my_workflow.py"
            )

            with pytest.raises(KeyError, match="my_workflow"):
                WorkflowRegistry.generate_new_workflow(
                    registry_key="my_workflow", metadata=mock_metadata, file_path="my_workflow.py"
                )

    def test_generate_new_workflow_unsaved_key_rejects_file_path(self) -> None:
        mock_metadata = MagicMock()
        with (
            patch.dict(WorkflowRegistry._workflows, {}, clear=True),
            pytest.raises(ValueError, match="cannot be paired with a file_path"),
        ):
            WorkflowRegistry.generate_new_workflow(
                registry_key="unsaved:abc", metadata=mock_metadata, file_path="my_workflow.py"
            )

    def test_generate_new_workflow_saved_key_requires_file_path(self) -> None:
        mock_metadata = MagicMock()
        with (
            patch.dict(WorkflowRegistry._workflows, {}, clear=True),
            pytest.raises(ValueError, match="requires a file_path"),
        ):
            WorkflowRegistry.generate_new_workflow(registry_key="my_workflow", metadata=mock_metadata)


class TestGetWorkflowMetadata:
    """Test Workflow.get_workflow_metadata path optimization.

    When synced_path and workspace_path are pre-computed by list_workflows(),
    get_workflow_metadata must use them directly instead of calling is_synced,
    which would instantiate ConfigManager per workflow.
    """

    def _make_workflow(self, file_path: str | None = "workflows/test.json") -> Workflow:
        mock_metadata = MagicMock()
        mock_metadata.model_dump.return_value = {"name": "test"}
        return Workflow(
            registry_key=WorkflowRegistry._RegistryKey(),
            metadata=mock_metadata,
            file_path=file_path,
        )

    def test_uses_precomputed_paths_instead_of_is_synced_property(self) -> None:
        workflow = self._make_workflow()
        synced_path = Path("/synced")
        workspace_path = Path("/workspace")

        with (
            patch.object(
                type(workflow),
                "is_synced",
                new_callable=lambda: property(
                    lambda _: (_ for _ in ()).throw(
                        AssertionError("is_synced must not be called when paths are pre-computed")
                    )
                ),
            ),
            patch(
                "griptape_nodes.node_library.workflow_registry.resolve_workspace_path",
                return_value=Path("/workspace/workflows/test.json"),
            ),
        ):
            result = workflow.get_workflow_metadata(synced_path=synced_path, workspace_path=workspace_path)

        assert "is_synced" in result

    def test_falls_back_to_is_synced_when_paths_not_provided(self) -> None:
        workflow = self._make_workflow()

        with patch.object(type(workflow), "is_synced", new_callable=lambda: property(lambda _: True)):
            result = workflow.get_workflow_metadata()

        assert result["is_synced"] is True

    def test_falls_back_to_is_synced_when_file_path_is_none(self) -> None:
        workflow = self._make_workflow(file_path=None)
        synced_path = Path("/synced")
        workspace_path = Path("/workspace")

        with patch.object(type(workflow), "is_synced", new_callable=lambda: property(lambda _: False)):
            result = workflow.get_workflow_metadata(synced_path=synced_path, workspace_path=workspace_path)

        assert result["is_synced"] is False
