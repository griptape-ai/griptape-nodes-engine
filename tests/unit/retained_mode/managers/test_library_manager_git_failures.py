"""Tests for how LibraryManager's git-backed request handlers report git failures.

Every git call these handlers make can fail for reasons outside the repository, most notably a
machine with no git installed. These tests pin down that such a failure comes back as a result
failure the editor can display, rather than escaping the handler.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.retained_mode.events.library_events import (
    CheckLibraryUpdateRequest,
    CheckLibraryUpdateResultFailure,
    CheckLibraryUpdateResultSuccess,
    UpdateLibraryRequest,
    UpdateLibraryResultFailure,
)
from griptape_nodes.retained_mode.managers.library_manager import (
    LibraryGitOperationContext,
)
from griptape_nodes.utils.git_utils import GitError, GitNotFoundError

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

LIBRARY_MANAGER_MODULE = "griptape_nodes.retained_mode.managers.library_manager"
LIBRARY_DIR = Path("/var/lib/test_lib")


def _library_info() -> MagicMock:
    """Build the registry lookup result the handlers use to find a library on disk."""
    library_info = MagicMock()
    library_info.library_path = str(LIBRARY_DIR / "griptape_nodes_library.json")
    return library_info


def _library(*, version: str = "1.0.0") -> MagicMock:
    """Build a registered library that reports a version."""
    library = MagicMock()
    library.get_metadata.return_value = MagicMock(library_version=version)
    return library


def _validation_context() -> LibraryGitOperationContext:
    """Build the pre-flight result update_library_request works from."""
    return LibraryGitOperationContext(
        library=MagicMock(),
        old_version="1.0.0",
        library_file_path=str(LIBRARY_DIR / "griptape_nodes_library.json"),
        library_dir=LIBRARY_DIR,
    )


class TestCheckLibraryUpdateRequestGitFailures:
    """Test check_library_update_request when a git call fails."""

    @pytest.mark.asyncio
    async def test_monorepo_check_failure_returns_failure(self, griptape_nodes: GriptapeNodes) -> None:
        """A git failure while reading the repository layout fails the check instead of escaping."""
        library_manager = griptape_nodes.LibraryManager()

        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", return_value=_library()),
            patch.object(library_manager, "get_library_info_by_library_name", return_value=_library_info()),
            patch(f"{LIBRARY_MANAGER_MODULE}.is_monorepo", side_effect=GitNotFoundError("git was not found on PATH")),
        ):
            result = await library_manager.check_library_update_request(
                CheckLibraryUpdateRequest(library_name="test_lib")
            )

        assert isinstance(result, CheckLibraryUpdateResultFailure)
        assert "test_lib" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_local_commit_failure_returns_failure(self, griptape_nodes: GriptapeNodes) -> None:
        """A git failure while reading the local commit fails the check instead of escaping."""
        library_manager = griptape_nodes.LibraryManager()

        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", return_value=_library()),
            patch.object(library_manager, "get_library_info_by_library_name", return_value=_library_info()),
            patch(f"{LIBRARY_MANAGER_MODULE}.is_monorepo", return_value=False),
            patch(f"{LIBRARY_MANAGER_MODULE}.get_git_remote", return_value="https://example.com/repo.git"),
            patch(f"{LIBRARY_MANAGER_MODULE}.get_current_ref", return_value="main"),
            patch(f"{LIBRARY_MANAGER_MODULE}.get_local_commit_sha", side_effect=GitError("boom")),
        ):
            result = await library_manager.check_library_update_request(
                CheckLibraryUpdateRequest(library_name="test_lib")
            )

        assert isinstance(result, CheckLibraryUpdateResultFailure)
        assert "test_lib" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_monorepo_reports_no_update_without_git_details(self, griptape_nodes: GriptapeNodes) -> None:
        """A monorepo library still reports "no update, manage manually" when git details are unavailable.

        The remote and ref in that response are informational, so their absence must not turn a
        successful answer into a failure.
        """
        library_manager = griptape_nodes.LibraryManager()

        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", return_value=_library()),
            patch.object(library_manager, "get_library_info_by_library_name", return_value=_library_info()),
            patch(f"{LIBRARY_MANAGER_MODULE}.is_monorepo", return_value=True),
            patch(f"{LIBRARY_MANAGER_MODULE}.get_git_info", return_value=(None, None)),
        ):
            result = await library_manager.check_library_update_request(
                CheckLibraryUpdateRequest(library_name="test_lib")
            )

        assert isinstance(result, CheckLibraryUpdateResultSuccess)
        assert result.has_update is False
        assert result.git_remote is None
        assert result.git_ref is None


class TestUpdateLibraryRequestGitFailures:
    """Test update_library_request when a git call fails."""

    @pytest.mark.asyncio
    async def test_monorepo_check_failure_returns_failure(self, griptape_nodes: GriptapeNodes) -> None:
        """A git failure while reading the repository layout fails the update before the working tree is touched."""
        library_manager = griptape_nodes.LibraryManager()

        with (
            patch.object(
                library_manager,
                "_validate_and_prepare_library_for_git_operation",
                new=AsyncMock(return_value=_validation_context()),
            ),
            patch(f"{LIBRARY_MANAGER_MODULE}.is_monorepo", side_effect=GitNotFoundError("git was not found on PATH")),
            patch(f"{LIBRARY_MANAGER_MODULE}.update_library_git") as mock_update_git,
        ):
            result = await library_manager.update_library_request(
                UpdateLibraryRequest(library_name="test_lib", overwrite_existing=False)
            )

        assert isinstance(result, UpdateLibraryResultFailure)
        assert "test_lib" in str(result.result_details)
        mock_update_git.assert_not_called()
