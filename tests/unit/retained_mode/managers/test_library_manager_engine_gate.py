"""Tests that a library update never moves to a version the running engine cannot load."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.retained_mode.events.library_events import (
    CheckLibraryUpdateRequest,
    CheckLibraryUpdateResultSuccess,
    UpdateLibraryRequest,
    UpdateLibraryResultFailure,
    UpdateLibraryResultSuccess,
)
from griptape_nodes.retained_mode.managers.library.git_operations import LibraryGitOperationContext
from griptape_nodes.utils.git_utils import GitError
from griptape_nodes.utils.library_utils import LibraryVersionInfo

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine

GIT_OPERATIONS_MODULE = "griptape_nodes.retained_mode.managers.library.git_operations"
LIBRARY_DIR = Path("/var/lib/test_lib")
TOO_NEW_ENGINE = "999.0.0"
OLD_ENGINE = "0.0.1"


def _remote_version(*, engine_version: str) -> LibraryVersionInfo:
    """Build what the remote reports for the commit an update would move to."""
    return LibraryVersionInfo(library_version="2.0.0", commit_sha="remotesha", engine_version=engine_version)


def _validation_context() -> LibraryGitOperationContext:
    """Build the pre-flight result update_library_request works from."""
    return LibraryGitOperationContext(
        old_version="1.0.0",
        library_file_path=str(LIBRARY_DIR / "griptape_nodes_library.json"),
        library_dir=LIBRARY_DIR,
    )


class TestCheckLibraryUpdateRequestEngineGate:
    """Test how check_library_update_request reports an update that needs a newer engine."""

    @pytest.mark.asyncio
    async def test_update_needing_a_newer_engine_reports_no_update(self, engine: Engine) -> None:
        """A check that finds only an update this engine cannot run succeeds with no update.

        A failure would leave a client showing the update from its last successful check.
        """
        library_manager = engine.library_manager
        library = MagicMock()
        library.get_metadata.return_value = MagicMock(library_version="1.0.0")
        library_info = MagicMock()
        library_info.library_path = str(LIBRARY_DIR / "griptape_nodes_library.json")

        with (
            patch(f"{GIT_OPERATIONS_MODULE}.LibraryRegistry.get_library", return_value=library),
            patch.object(library_manager, "get_library_info_by_library_name", return_value=library_info),
            patch(f"{GIT_OPERATIONS_MODULE}.is_monorepo", return_value=False),
            patch(f"{GIT_OPERATIONS_MODULE}.get_git_remote", return_value="https://example.com/repo.git"),
            patch(f"{GIT_OPERATIONS_MODULE}.get_current_ref", return_value="main"),
            patch(f"{GIT_OPERATIONS_MODULE}.get_local_commit_sha", return_value="localsha"),
            patch(f"{GIT_OPERATIONS_MODULE}.remote_ref_exists", return_value=True),
            patch(
                f"{GIT_OPERATIONS_MODULE}.clone_and_get_library_version",
                return_value=_remote_version(engine_version=TOO_NEW_ENGINE),
            ),
        ):
            result = await library_manager.git_operations.check_library_update_request(
                CheckLibraryUpdateRequest(library_name="test_lib")
            )

        assert isinstance(result, CheckLibraryUpdateResultSuccess)
        assert result.has_update is False
        assert result.latest_version == "2.0.0"
        assert TOO_NEW_ENGINE in str(result.result_details)


class TestUpdateLibraryRequestEngineGate:
    """Test that update_library_request checks the engine version of the commit it moves to."""

    @pytest.mark.asyncio
    async def test_target_needing_a_newer_engine_blocks_update(self, engine: Engine) -> None:
        """An update whose target needs a newer engine fails without touching the working tree."""
        library_manager = engine.library_manager

        with (
            patch.object(
                library_manager.git_operations,
                "_validate_and_prepare_library_for_git_operation",
                new=AsyncMock(return_value=_validation_context()),
            ),
            patch(f"{GIT_OPERATIONS_MODULE}.is_monorepo", return_value=False),
            patch(f"{GIT_OPERATIONS_MODULE}.get_git_remote", return_value="https://example.com/repo.git"),
            patch(f"{GIT_OPERATIONS_MODULE}.get_current_ref", return_value="stable"),
            patch(
                f"{GIT_OPERATIONS_MODULE}.clone_and_get_library_version",
                return_value=_remote_version(engine_version=TOO_NEW_ENGINE),
            ),
            patch(f"{GIT_OPERATIONS_MODULE}.update_library_git") as mock_update_git,
        ):
            result = await library_manager.git_operations.update_library_request(
                UpdateLibraryRequest(library_name="test_lib", overwrite_existing=False)
            )

        assert isinstance(result, UpdateLibraryResultFailure)
        assert TOO_NEW_ENGINE in str(result.result_details)
        mock_update_git.assert_not_called()

    @pytest.mark.asyncio
    async def test_target_this_engine_can_run_allows_update(self, engine: Engine) -> None:
        """An update whose target runs on this engine reaches the git update."""
        library_manager = engine.library_manager

        with (
            patch.object(
                library_manager.git_operations,
                "_validate_and_prepare_library_for_git_operation",
                new=AsyncMock(return_value=_validation_context()),
            ),
            patch(f"{GIT_OPERATIONS_MODULE}.is_monorepo", return_value=False),
            patch(f"{GIT_OPERATIONS_MODULE}.get_git_remote", return_value="https://example.com/repo.git"),
            patch(f"{GIT_OPERATIONS_MODULE}.get_current_ref", return_value="stable"),
            patch(
                f"{GIT_OPERATIONS_MODULE}.clone_and_get_library_version",
                return_value=_remote_version(engine_version=OLD_ENGINE),
            ),
            patch(f"{GIT_OPERATIONS_MODULE}.update_library_git") as mock_update_git,
            patch(f"{GIT_OPERATIONS_MODULE}.is_on_tag", return_value=False),
            patch.object(
                library_manager.git_operations,
                "_reload_library_after_git_operation",
                new=AsyncMock(return_value="2.0.0"),
            ),
        ):
            result = await library_manager.git_operations.update_library_request(
                UpdateLibraryRequest(library_name="test_lib", overwrite_existing=False)
            )

        assert isinstance(result, UpdateLibraryResultSuccess)
        mock_update_git.assert_called_once()

    @pytest.mark.asyncio
    async def test_unreadable_target_blocks_update(self, engine: Engine) -> None:
        """An update whose target engine version cannot be read fails rather than updating unchecked."""
        library_manager = engine.library_manager

        with (
            patch.object(
                library_manager.git_operations,
                "_validate_and_prepare_library_for_git_operation",
                new=AsyncMock(return_value=_validation_context()),
            ),
            patch(f"{GIT_OPERATIONS_MODULE}.is_monorepo", return_value=False),
            patch(f"{GIT_OPERATIONS_MODULE}.get_git_remote", return_value="https://example.com/repo.git"),
            patch(f"{GIT_OPERATIONS_MODULE}.get_current_ref", return_value="stable"),
            patch(f"{GIT_OPERATIONS_MODULE}.clone_and_get_library_version", side_effect=GitError("boom")),
            patch(f"{GIT_OPERATIONS_MODULE}.update_library_git") as mock_update_git,
        ):
            result = await library_manager.git_operations.update_library_request(
                UpdateLibraryRequest(library_name="test_lib", overwrite_existing=False)
            )

        assert isinstance(result, UpdateLibraryResultFailure)
        assert "test_lib" in str(result.result_details)
        mock_update_git.assert_not_called()
