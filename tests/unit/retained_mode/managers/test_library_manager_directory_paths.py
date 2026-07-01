"""Tests for absolute and relative directory path resolution in LibraryManager."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.retained_mode.events.library_events import (
    DownloadLibraryResultFailure,
    UpdateLibraryRequest,
    UpdateLibraryResultFailure,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.library_manager import LibraryGitOperationContext
from griptape_nodes.utils.git_utils import GitCloneError, GitPullError


class TestGetSandboxDirectory:
    """Test _get_sandbox_directory resolves absolute and relative paths."""

    def test_relative_path(self, griptape_nodes: GriptapeNodes) -> None:
        """A relative sandbox_library_directory is resolved against the workspace."""
        library_manager = griptape_nodes.LibraryManager()
        config_mgr = MagicMock()
        config_mgr.get_config_value.return_value = "sandbox_library"
        config_mgr.workspace_path = Path("/workspace")

        with (
            patch.object(GriptapeNodes, "ConfigManager", return_value=config_mgr),
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.resolve_workspace_path",
                return_value=Path("/workspace/sandbox_library"),
            ) as mock_resolve,
            patch.object(Path, "exists", return_value=True),
        ):
            result = library_manager._get_sandbox_directory()

        mock_resolve.assert_called_once_with(Path("sandbox_library"), Path("/workspace"))
        assert result == Path("/workspace/sandbox_library")

    def test_absolute_path(self, griptape_nodes: GriptapeNodes) -> None:
        """An absolute sandbox_library_directory is used as-is."""
        library_manager = griptape_nodes.LibraryManager()
        config_mgr = MagicMock()
        config_mgr.get_config_value.return_value = "/opt/sandbox"
        config_mgr.workspace_path = Path("/workspace")

        with (
            patch.object(GriptapeNodes, "ConfigManager", return_value=config_mgr),
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.resolve_workspace_path",
                return_value=Path("/opt/sandbox"),
            ) as mock_resolve,
            patch.object(Path, "exists", return_value=True),
        ):
            result = library_manager._get_sandbox_directory()

        mock_resolve.assert_called_once_with(Path("/opt/sandbox"), Path("/workspace"))
        assert result == Path("/opt/sandbox")

    def test_not_configured_returns_none(self, griptape_nodes: GriptapeNodes) -> None:
        """When sandbox_library_directory is empty, returns None without resolving."""
        library_manager = griptape_nodes.LibraryManager()
        config_mgr = MagicMock()
        config_mgr.get_config_value.return_value = ""
        config_mgr.workspace_path = Path("/workspace")

        with patch.object(GriptapeNodes, "ConfigManager", return_value=config_mgr):
            result = library_manager._get_sandbox_directory()

        assert result is None

    def test_nonexistent_directory_returns_none(self, griptape_nodes: GriptapeNodes) -> None:
        """When the resolved directory does not exist, returns None."""
        library_manager = griptape_nodes.LibraryManager()
        config_mgr = MagicMock()
        config_mgr.get_config_value.return_value = "sandbox_library"
        config_mgr.workspace_path = Path("/workspace")

        with (
            patch.object(GriptapeNodes, "ConfigManager", return_value=config_mgr),
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.resolve_workspace_path",
                return_value=Path("/workspace/sandbox_library"),
            ),
            patch.object(Path, "exists", return_value=False),
        ):
            result = library_manager._get_sandbox_directory()

        assert result is None


class TestDownloadLibrariesFromGitUrlsPath:
    """Test _download_libraries_from_git_urls resolves absolute and relative paths."""

    @pytest.mark.asyncio
    async def test_uses_resolved_libraries_root(self, griptape_nodes: GriptapeNodes) -> None:
        """The download root comes from ConfigManager.resolved_libraries_root (own/inherited or default)."""
        library_manager = griptape_nodes.LibraryManager()
        config_mgr = MagicMock()
        config_mgr.resolved_libraries_root.return_value = Path("/workspace/libraries")

        with patch.object(GriptapeNodes, "ConfigManager", return_value=config_mgr):
            result = await library_manager._download_libraries_from_git_urls([])

        config_mgr.resolved_libraries_root.assert_called_once_with()
        assert result == {}


class TestDownloadLibraryRequestPath:
    """Test download_library_request resolves absolute and relative paths."""

    @pytest.mark.asyncio
    async def test_uses_resolved_libraries_root(self, griptape_nodes: GriptapeNodes) -> None:
        """The download root comes from ConfigManager.resolved_libraries_root."""
        library_manager = griptape_nodes.LibraryManager()
        config_mgr = MagicMock()
        config_mgr.resolved_libraries_root.return_value = Path("/workspace/libraries")

        request = MagicMock()
        request.git_url = "https://github.com/user/repo.git"
        request.branch_tag_commit = None
        request.target_directory_name = None
        request.download_directory = None

        with (
            patch.object(GriptapeNodes, "ConfigManager", return_value=config_mgr),
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.normalize_github_url",
                return_value="https://github.com/user/repo.git",
            ),
            patch("anyio.Path.mkdir"),
            patch("anyio.Path.exists", return_value=False),
            patch.object(asyncio, "to_thread", side_effect=GitCloneError("stop test here")),
        ):
            result = await library_manager.download_library_request(request)

        config_mgr.resolved_libraries_root.assert_called_once_with()
        assert isinstance(result, DownloadLibraryResultFailure)

    @pytest.mark.asyncio
    async def test_custom_download_directory_skips_config(self, griptape_nodes: GriptapeNodes) -> None:
        """When download_directory is provided, it is used directly without resolving config."""
        library_manager = griptape_nodes.LibraryManager()
        config_mgr = MagicMock()
        config_mgr.workspace_path = Path("/workspace")

        request = MagicMock()
        request.git_url = "https://github.com/user/repo.git"
        request.branch_tag_commit = None
        request.target_directory_name = None
        request.download_directory = "/custom/dir"

        with (
            patch.object(GriptapeNodes, "ConfigManager", return_value=config_mgr),
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.normalize_github_url",
                return_value="https://github.com/user/repo.git",
            ),
            patch("anyio.Path.mkdir"),
            patch("anyio.Path.exists", return_value=False),
            patch.object(asyncio, "to_thread", side_effect=GitCloneError("stop test here")),
        ):
            await library_manager.download_library_request(request)

        config_mgr.resolved_libraries_root.assert_not_called()

    @pytest.mark.asyncio
    async def test_existing_target_dir_sets_existing_path(self, griptape_nodes: GriptapeNodes) -> None:
        """Existing target directory failure carries the path in ``existing_path``.

        When the target directory already exists and fail_on_exists is True, the failure
        carries the absolute target path in the structured ``existing_path`` field so clients
        can render it without having to parse the human-readable error message (which is
        unreliable for paths containing ``:``, e.g. Windows drive letters).
        """
        library_manager = griptape_nodes.LibraryManager()
        config_mgr = MagicMock()
        config_mgr.resolved_libraries_root.return_value = Path("/opt/libraries")

        request = MagicMock()
        request.git_url = "https://github.com/user/repo.git"
        request.branch_tag_commit = None
        request.target_directory_name = "repo"
        request.download_directory = None
        request.overwrite_existing = False
        request.fail_on_exists = True

        with (
            patch.object(GriptapeNodes, "ConfigManager", return_value=config_mgr),
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.normalize_github_url",
                return_value="https://github.com/user/repo.git",
            ),
            patch("anyio.Path.mkdir"),
            patch("anyio.Path.exists", return_value=True),
        ):
            result = await library_manager.download_library_request(request)

        assert isinstance(result, DownloadLibraryResultFailure)
        assert result.retryable is True
        assert result.existing_path == str(Path("/opt/libraries/repo"))


class TestUpdateLibraryRequestExistingPath:
    """Test update_library_request reports the dirty library directory in ``existing_path``."""

    @pytest.mark.asyncio
    async def test_uncommitted_changes_sets_existing_path(self, griptape_nodes: GriptapeNodes) -> None:
        """Uncommitted-changes update failure carries the library directory in ``existing_path``.

        Without this, clients on Windows cannot recover the path from the error message because
        the drive-letter colon collides with the ``<path>: <reason>`` separator in the
        human-readable message.
        """
        library_manager = griptape_nodes.LibraryManager()
        library_dir = Path("/var/lib/test_lib")

        validation_context = LibraryGitOperationContext(
            library=MagicMock(),
            old_version="1.0.0",
            library_file_path=str(library_dir / "griptape_nodes_library.json"),
            library_dir=library_dir,
        )

        with (
            patch.object(
                library_manager,
                "_validate_and_prepare_library_for_git_operation",
                new=AsyncMock(return_value=validation_context),
            ),
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.is_monorepo",
                return_value=False,
            ),
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.update_library_git",
                side_effect=GitPullError(
                    f"Cannot update library at {library_dir}: You have uncommitted changes. "
                    "Use overwrite_existing=True to discard them."
                ),
            ),
        ):
            result = await library_manager.update_library_request(
                UpdateLibraryRequest(library_name="test_lib", overwrite_existing=False)
            )

        assert isinstance(result, UpdateLibraryResultFailure)
        assert result.retryable is True
        assert result.existing_path == str(library_dir)
