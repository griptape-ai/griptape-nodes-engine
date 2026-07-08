"""Tests for the library update age gate in LibraryManager.

The age gate withholds a library update until the commit it would move to is at least
``library.update_min_age_hours`` old, and only when ``library.update_age_gating_enabled`` is set.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.retained_mode.events.library_events import (
    UpdateLibraryRequest,
    UpdateLibraryResultFailure,
    UpdateLibraryResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.library_manager import LibraryGitOperationContext
from griptape_nodes.retained_mode.managers.settings import (
    LIBRARY_UPDATE_AGE_GATING_ENABLED_KEY,
    LIBRARY_UPDATE_MIN_AGE_HOURS_KEY,
)

LIBRARY_MANAGER_MODULE = "griptape_nodes.retained_mode.managers.library_manager"
MIN_AGE_HOURS = 24.0


def _config_manager(*, enabled: bool, min_age_hours: float) -> MagicMock:
    """Build a ConfigManager mock that answers only the two age-gate config keys."""
    config_mgr = MagicMock()

    def get_config_value(key: str, **kwargs: object) -> object:
        if key == LIBRARY_UPDATE_AGE_GATING_ENABLED_KEY:
            return enabled
        if key == LIBRARY_UPDATE_MIN_AGE_HOURS_KEY:
            return min_age_hours
        return kwargs.get("default")

    config_mgr.get_config_value.side_effect = get_config_value
    return config_mgr


class TestEvaluateUpdateAgeGate:
    """Test the pure age-gate decision helper."""

    def test_disabled_never_gates(self, griptape_nodes: GriptapeNodes) -> None:
        library_manager = griptape_nodes.LibraryManager()
        young_commit = datetime.now(tz=UTC) - timedelta(hours=1)

        with patch.object(
            GriptapeNodes, "ConfigManager", return_value=_config_manager(enabled=False, min_age_hours=MIN_AGE_HOURS)
        ):
            decision = library_manager._evaluate_update_age_gate(young_commit)

        assert decision.enabled is False
        assert decision.gated is False

    def test_enabled_gates_commit_younger_than_threshold(self, griptape_nodes: GriptapeNodes) -> None:
        library_manager = griptape_nodes.LibraryManager()
        young_commit = datetime.now(tz=UTC) - timedelta(hours=1)

        with patch.object(
            GriptapeNodes, "ConfigManager", return_value=_config_manager(enabled=True, min_age_hours=MIN_AGE_HOURS)
        ):
            decision = library_manager._evaluate_update_age_gate(young_commit)

        assert decision.enabled is True
        assert decision.gated is True
        assert decision.age_hours is not None
        assert decision.age_hours == pytest.approx(1.0, abs=0.1)
        assert decision.min_age_hours == MIN_AGE_HOURS

    def test_enabled_allows_commit_older_than_threshold(self, griptape_nodes: GriptapeNodes) -> None:
        library_manager = griptape_nodes.LibraryManager()
        old_commit = datetime.now(tz=UTC) - timedelta(hours=48)

        with patch.object(
            GriptapeNodes, "ConfigManager", return_value=_config_manager(enabled=True, min_age_hours=MIN_AGE_HOURS)
        ):
            decision = library_manager._evaluate_update_age_gate(old_commit)

        assert decision.enabled is True
        assert decision.gated is False
        assert decision.age_hours is not None
        assert decision.age_hours == pytest.approx(48.0, abs=0.1)

    def test_enabled_allows_when_commit_datetime_unknown(self, griptape_nodes: GriptapeNodes) -> None:
        """A missing commit timestamp cannot be verified, so the update is allowed (not wedged)."""
        library_manager = griptape_nodes.LibraryManager()

        with patch.object(
            GriptapeNodes, "ConfigManager", return_value=_config_manager(enabled=True, min_age_hours=MIN_AGE_HOURS)
        ):
            decision = library_manager._evaluate_update_age_gate(None)

        assert decision.enabled is True
        assert decision.gated is False
        assert decision.age_hours is None

    def test_naive_commit_datetime_is_treated_as_utc(self, griptape_nodes: GriptapeNodes) -> None:
        library_manager = griptape_nodes.LibraryManager()
        naive_young_commit = datetime.now(tz=UTC).replace(tzinfo=None) - timedelta(hours=1)

        with patch.object(
            GriptapeNodes, "ConfigManager", return_value=_config_manager(enabled=True, min_age_hours=MIN_AGE_HOURS)
        ):
            decision = library_manager._evaluate_update_age_gate(naive_young_commit)

        assert decision.gated is True


class TestUpdateLibraryRequestAgeGate:
    """Test enforcement of the age gate inside update_library_request."""

    def _validation_context(self, library_dir: Path) -> LibraryGitOperationContext:
        return LibraryGitOperationContext(
            library=MagicMock(),
            old_version="1.0.0",
            library_file_path=str(library_dir / "griptape_nodes_library.json"),
            library_dir=library_dir,
        )

    @pytest.mark.asyncio
    async def test_young_target_commit_blocks_update(self, griptape_nodes: GriptapeNodes) -> None:
        """A target commit younger than the soak period blocks the update without touching git."""
        library_manager = griptape_nodes.LibraryManager()
        library_dir = Path("/var/lib/test_lib")
        young_commit = datetime.now(tz=UTC) - timedelta(hours=2)

        with (
            patch.object(
                library_manager,
                "_validate_and_prepare_library_for_git_operation",
                new=AsyncMock(return_value=self._validation_context(library_dir)),
            ),
            patch(f"{LIBRARY_MANAGER_MODULE}.is_monorepo", return_value=False),
            patch.object(
                GriptapeNodes, "ConfigManager", return_value=_config_manager(enabled=True, min_age_hours=MIN_AGE_HOURS)
            ),
            patch.object(
                library_manager,
                "_get_remote_target_commit_datetime",
                new=AsyncMock(return_value=young_commit),
            ),
            patch(f"{LIBRARY_MANAGER_MODULE}.update_library_git") as mock_update_git,
        ):
            result = await library_manager.update_library_request(
                UpdateLibraryRequest(library_name="test_lib", overwrite_existing=False)
            )

        assert isinstance(result, UpdateLibraryResultFailure)
        assert result.age_gated is True
        mock_update_git.assert_not_called()

    @pytest.mark.asyncio
    async def test_old_target_commit_allows_update(self, griptape_nodes: GriptapeNodes) -> None:
        """A target commit older than the soak period is allowed through to the git update."""
        library_manager = griptape_nodes.LibraryManager()
        library_dir = Path("/var/lib/test_lib")
        old_commit = datetime.now(tz=UTC) - timedelta(hours=48)

        with (
            patch.object(
                library_manager,
                "_validate_and_prepare_library_for_git_operation",
                new=AsyncMock(return_value=self._validation_context(library_dir)),
            ),
            patch(f"{LIBRARY_MANAGER_MODULE}.is_monorepo", return_value=False),
            patch.object(
                GriptapeNodes, "ConfigManager", return_value=_config_manager(enabled=True, min_age_hours=MIN_AGE_HOURS)
            ),
            patch.object(
                library_manager,
                "_get_remote_target_commit_datetime",
                new=AsyncMock(return_value=old_commit),
            ),
            patch(f"{LIBRARY_MANAGER_MODULE}.update_library_git") as mock_update_git,
            patch(f"{LIBRARY_MANAGER_MODULE}.is_on_tag", return_value=False),
            patch.object(
                library_manager,
                "_reload_library_after_git_operation",
                new=AsyncMock(return_value="2.0.0"),
            ),
        ):
            result = await library_manager.update_library_request(
                UpdateLibraryRequest(library_name="test_lib", overwrite_existing=False)
            )

        assert isinstance(result, UpdateLibraryResultSuccess)
        assert result.new_version == "2.0.0"
        mock_update_git.assert_called_once()

    @pytest.mark.asyncio
    async def test_disabled_skips_remote_age_lookup(self, griptape_nodes: GriptapeNodes) -> None:
        """When gating is disabled, the update path never pays for the remote age round-trip."""
        library_manager = griptape_nodes.LibraryManager()
        library_dir = Path("/var/lib/test_lib")

        with (
            patch.object(
                library_manager,
                "_validate_and_prepare_library_for_git_operation",
                new=AsyncMock(return_value=self._validation_context(library_dir)),
            ),
            patch(f"{LIBRARY_MANAGER_MODULE}.is_monorepo", return_value=False),
            patch.object(
                GriptapeNodes, "ConfigManager", return_value=_config_manager(enabled=False, min_age_hours=MIN_AGE_HOURS)
            ),
            patch.object(
                library_manager,
                "_get_remote_target_commit_datetime",
                new=AsyncMock(return_value=None),
            ) as mock_remote_age,
            patch(f"{LIBRARY_MANAGER_MODULE}.update_library_git"),
            patch(f"{LIBRARY_MANAGER_MODULE}.is_on_tag", return_value=False),
            patch.object(
                library_manager,
                "_reload_library_after_git_operation",
                new=AsyncMock(return_value="2.0.0"),
            ),
        ):
            result = await library_manager.update_library_request(
                UpdateLibraryRequest(library_name="test_lib", overwrite_existing=False)
            )

        assert isinstance(result, UpdateLibraryResultSuccess)
        mock_remote_age.assert_not_called()
