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
    CheckLibraryUpdateRequest,
    CheckLibraryUpdateResultSuccess,
    ListRegisteredLibrariesRequest,
    ListRegisteredLibrariesResultSuccess,
    LoadLibrariesRequest,
    LoadLibrariesResultSuccess,
    SyncLibrariesRequest,
    SyncLibrariesResultSuccess,
    UpdateLibraryRequest,
    UpdateLibraryResultFailure,
    UpdateLibraryResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.library_manager import (
    AgeGateConfig,
    LibraryGitOperationContext,
    LibraryManager,
)
from griptape_nodes.retained_mode.managers.settings import (
    LIBRARY_UPDATE_AGE_GATING_ENABLED_KEY,
    LIBRARY_UPDATE_MIN_AGE_HOURS_KEY,
)
from griptape_nodes.utils.git_utils import GitError
from griptape_nodes.utils.library_utils import LibraryVersionInfo

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

    def test_enabled_unknown_timestamp_logs_fail_open_warning(
        self, griptape_nodes: GriptapeNodes, caplog: pytest.LogCaptureFixture
    ) -> None:
        library_manager = griptape_nodes.LibraryManager()

        with (
            patch.object(
                GriptapeNodes, "ConfigManager", return_value=_config_manager(enabled=True, min_age_hours=MIN_AGE_HOURS)
            ),
            caplog.at_level("WARNING"),
        ):
            decision = library_manager._evaluate_update_age_gate(None)

        assert decision.gated is False
        assert any("could not be determined" in message for message in caplog.messages)

    def test_disabled_unknown_timestamp_does_not_warn(
        self, griptape_nodes: GriptapeNodes, caplog: pytest.LogCaptureFixture
    ) -> None:
        library_manager = griptape_nodes.LibraryManager()

        with (
            patch.object(
                GriptapeNodes, "ConfigManager", return_value=_config_manager(enabled=False, min_age_hours=MIN_AGE_HOURS)
            ),
            caplog.at_level("WARNING"),
        ):
            decision = library_manager._evaluate_update_age_gate(None)

        assert decision.enabled is False
        assert not any("could not be determined" in message for message in caplog.messages)

    def test_explicit_config_is_not_re_read_from_config_manager(self, griptape_nodes: GriptapeNodes) -> None:
        """Passing a pre-read config skips the ConfigManager lookup entirely."""
        library_manager = griptape_nodes.LibraryManager()
        config_mgr = _config_manager(enabled=True, min_age_hours=MIN_AGE_HOURS)
        old_commit = datetime.now(tz=UTC) - timedelta(hours=48)

        with patch.object(GriptapeNodes, "ConfigManager", return_value=config_mgr):
            decision = library_manager._evaluate_update_age_gate(
                old_commit, config=AgeGateConfig(enabled=True, min_age_hours=MIN_AGE_HOURS)
            )

        assert decision.enabled is True
        assert decision.gated is False
        config_mgr.get_config_value.assert_not_called()


class TestReadAgeGateConfig:
    """Test the config-reading helper that both the check and update paths share."""

    def test_reads_both_keys(self, griptape_nodes: GriptapeNodes) -> None:
        library_manager = griptape_nodes.LibraryManager()

        with patch.object(
            GriptapeNodes, "ConfigManager", return_value=_config_manager(enabled=True, min_age_hours=12.0)
        ):
            config = library_manager._read_age_gate_config()

        assert config == AgeGateConfig(enabled=True, min_age_hours=12.0)

    def test_explicit_null_override_falls_back_to_defaults(self, griptape_nodes: GriptapeNodes) -> None:
        """An explicit ``null`` in config resolves to None (bypassing cast_type); coalesce to defaults.

        Reading the config must never raise (e.g. ``float(None)``), otherwise a malformed config
        would wedge every update instead of failing open.
        """
        library_manager = griptape_nodes.LibraryManager()
        null_config_mgr = MagicMock()
        null_config_mgr.get_config_value.return_value = None

        with patch.object(GriptapeNodes, "ConfigManager", return_value=null_config_mgr):
            config = library_manager._read_age_gate_config()

        assert config == AgeGateConfig(enabled=False, min_age_hours=24.0)


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
        """A target commit younger than the minimum age blocks the update without touching git."""
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
        """A target commit older than the minimum age is allowed through to the git update."""
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

    @pytest.mark.asyncio
    async def test_unknown_target_commit_allows_update(self, griptape_nodes: GriptapeNodes) -> None:
        """Fail-open: when the target commit timestamp cannot be read, the update still proceeds."""
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
                GriptapeNodes, "ConfigManager", return_value=_config_manager(enabled=True, min_age_hours=MIN_AGE_HOURS)
            ),
            patch.object(
                library_manager,
                "_get_remote_target_commit_datetime",
                new=AsyncMock(return_value=None),
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
        mock_update_git.assert_called_once()


class TestGetRemoteTargetCommitDatetime:
    """Test the remote target-commit timestamp helper that feeds the update-path age gate."""

    @pytest.mark.asyncio
    async def test_returns_none_when_no_remote(self, griptape_nodes: GriptapeNodes) -> None:
        """A library with no git remote cannot be age-checked, so the helper returns None."""
        library_manager = griptape_nodes.LibraryManager()

        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.get_git_remote", return_value=None),
            patch(f"{LIBRARY_MANAGER_MODULE}.clone_and_get_library_version") as mock_clone,
        ):
            result = await library_manager._get_remote_target_commit_datetime(Path("/var/lib/test_lib"))

        assert result is None
        mock_clone.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_none_on_git_error(self, griptape_nodes: GriptapeNodes) -> None:
        """A git failure while reading the remote commit degrades to None (fail-open)."""
        library_manager = griptape_nodes.LibraryManager()

        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.get_git_remote", return_value="https://example.com/repo.git"),
            patch(f"{LIBRARY_MANAGER_MODULE}.get_current_ref", return_value="main"),
            patch(
                f"{LIBRARY_MANAGER_MODULE}.clone_and_get_library_version",
                side_effect=GitError("boom"),
            ),
        ):
            result = await library_manager._get_remote_target_commit_datetime(Path("/var/lib/test_lib"))

        assert result is None


class TestCheckLibraryUpdateRequestAgeGate:
    """Test that check_library_update_request surfaces the age-gate fields."""

    async def _run_check(
        self,
        library_manager: LibraryManager,
        *,
        commit_datetime: datetime | None,
        enabled: bool,
        has_update: bool = True,
    ) -> CheckLibraryUpdateResultSuccess:
        """Run check_library_update_request with all git/registry access mocked out.

        When has_update is False the remote reports the same version and commit as local, so the
        handler reports no update and skips the age-gate evaluation entirely.
        """
        library_dir = Path("/var/lib/test_lib")
        current_version = "1.0.0"
        latest_version = "2.0.0" if has_update else current_version
        local_commit = "localsha"
        remote_commit = "remotesha" if has_update else local_commit
        library = MagicMock()
        library.get_metadata.return_value = MagicMock(library_version=current_version)
        library_info = MagicMock()
        library_info.library_path = str(library_dir / "griptape_nodes_library.json")
        version_info = LibraryVersionInfo(
            library_version=latest_version,
            commit_sha=remote_commit,
            engine_version="",
            commit_datetime=commit_datetime,
        )

        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", return_value=library),
            patch.object(library_manager, "get_library_info_by_library_name", return_value=library_info),
            patch(f"{LIBRARY_MANAGER_MODULE}.is_monorepo", return_value=False),
            patch(f"{LIBRARY_MANAGER_MODULE}.get_git_remote", return_value="https://example.com/repo.git"),
            patch(f"{LIBRARY_MANAGER_MODULE}.get_current_ref", return_value="main"),
            patch(f"{LIBRARY_MANAGER_MODULE}.get_local_commit_sha", return_value=local_commit),
            patch(f"{LIBRARY_MANAGER_MODULE}.remote_ref_exists", return_value=True),
            patch(f"{LIBRARY_MANAGER_MODULE}.clone_and_get_library_version", return_value=version_info),
            patch.object(library_manager, "_check_engine_version_compatibility", return_value=(True, "1.0.0")),
            patch.object(
                GriptapeNodes,
                "ConfigManager",
                return_value=_config_manager(enabled=enabled, min_age_hours=MIN_AGE_HOURS),
            ),
        ):
            result = await library_manager.check_library_update_request(
                CheckLibraryUpdateRequest(library_name="test_lib")
            )

        assert isinstance(result, CheckLibraryUpdateResultSuccess)
        return result

    @pytest.mark.asyncio
    async def test_gated_update_reports_age_fields(self, griptape_nodes: GriptapeNodes) -> None:
        """A young target commit yields has_update=True with update_gated_by_age=True and the age/min fields."""
        library_manager = griptape_nodes.LibraryManager()
        young_commit = datetime.now(tz=UTC) - timedelta(hours=2)

        result = await self._run_check(library_manager, commit_datetime=young_commit, enabled=True)

        assert result.has_update is True
        assert result.update_gated_by_age is True
        assert result.target_commit_age_hours is not None
        assert result.target_commit_age_hours == pytest.approx(2.0, abs=0.1)
        assert result.update_min_age_hours == MIN_AGE_HOURS

    @pytest.mark.asyncio
    async def test_ungated_update_reports_age_fields(self, griptape_nodes: GriptapeNodes) -> None:
        """An old target commit is not gated, but the age and min fields are still populated when enabled."""
        library_manager = griptape_nodes.LibraryManager()
        old_commit = datetime.now(tz=UTC) - timedelta(hours=48)

        result = await self._run_check(library_manager, commit_datetime=old_commit, enabled=True)

        assert result.has_update is True
        assert result.update_gated_by_age is False
        assert result.target_commit_age_hours is not None
        assert result.target_commit_age_hours == pytest.approx(48.0, abs=0.1)
        assert result.update_min_age_hours == MIN_AGE_HOURS

    @pytest.mark.asyncio
    async def test_disabled_leaves_min_age_none(self, griptape_nodes: GriptapeNodes) -> None:
        """With gating disabled, an available update is never gated and reports no configured minimum."""
        library_manager = griptape_nodes.LibraryManager()
        young_commit = datetime.now(tz=UTC) - timedelta(hours=2)

        result = await self._run_check(library_manager, commit_datetime=young_commit, enabled=False)

        assert result.has_update is True
        assert result.update_gated_by_age is False
        assert result.update_min_age_hours is None

    @pytest.mark.asyncio
    async def test_no_update_leaves_age_fields_default(self, griptape_nodes: GriptapeNodes) -> None:
        """When there is no update, the age gate is not evaluated and its fields stay at defaults."""
        library_manager = griptape_nodes.LibraryManager()
        young_commit = datetime.now(tz=UTC) - timedelta(hours=2)

        result = await self._run_check(
            library_manager,
            commit_datetime=young_commit,
            enabled=True,
            has_update=False,
        )

        assert result.has_update is False
        assert result.update_gated_by_age is False
        assert result.target_commit_age_hours is None
        assert result.update_min_age_hours is None


class TestSyncLibrariesRequestAgeGate:
    """Test that sync_libraries_request defers age-gated updates rather than applying them."""

    @staticmethod
    def _check_result(
        *, has_update: bool, gated: bool = False, latest_version: str = "2.0.0"
    ) -> CheckLibraryUpdateResultSuccess:
        return CheckLibraryUpdateResultSuccess(
            has_update=has_update,
            current_version="1.0.0",
            latest_version=latest_version if has_update else "1.0.0",
            git_remote="https://example.com/repo.git",
            git_ref="main",
            local_commit="localsha",
            remote_commit="remotesha" if has_update else "localsha",
            update_gated_by_age=gated,
            target_commit_age_hours=2.0 if gated else None,
            update_min_age_hours=MIN_AGE_HOURS if gated else None,
            result_details="checked",
        )

    @pytest.mark.asyncio
    async def test_deferred_library_counted_and_not_updated(self, griptape_nodes: GriptapeNodes) -> None:
        """A gated library is counted in libraries_deferred, summarized, and skipped by the update pass."""
        library_manager = griptape_nodes.LibraryManager()
        check_results = {
            "up_to_date": self._check_result(has_update=False),
            "gated": self._check_result(has_update=True, gated=True, latest_version="2.0.0"),
            "updatable": self._check_result(has_update=True, gated=False),
        }
        update_calls: list[str] = []

        def dispatch(request: object) -> object:
            if isinstance(request, LoadLibrariesRequest):
                return LoadLibrariesResultSuccess(result_details="loaded")
            if isinstance(request, ListRegisteredLibrariesRequest):
                return ListRegisteredLibrariesResultSuccess(libraries=list(check_results), result_details="listed")
            if isinstance(request, CheckLibraryUpdateRequest):
                return check_results[request.library_name]
            if isinstance(request, UpdateLibraryRequest):
                update_calls.append(request.library_name)
                return UpdateLibraryResultSuccess(old_version="1.0.0", new_version="2.0.0", result_details="updated")
            error = f"Unexpected request routed through ahandle_request: {request!r}"
            raise AssertionError(error)

        with (
            patch.object(
                GriptapeNodes,
                "ConfigManager",
                return_value=_config_manager(enabled=True, min_age_hours=MIN_AGE_HOURS),
            ),
            patch.object(GriptapeNodes, "ahandle_request", new=AsyncMock(side_effect=dispatch)),
        ):
            result = await library_manager.sync_libraries_request(SyncLibrariesRequest())

        assert isinstance(result, SyncLibrariesResultSuccess)
        assert result.libraries_checked == len(check_results)
        assert result.libraries_deferred == 1
        assert result.libraries_updated == 1
        # The gated library must never reach the update pass.
        assert update_calls == ["updatable"]
        # The deferred summary records the pending target version, not the current one.
        assert result.update_summary["gated"] == {
            "old_version": "1.0.0",
            "new_version": "2.0.0",
            "status": "deferred_age_gate",
        }
        assert result.update_summary["updatable"]["status"] == "updated"
        assert "up_to_date" not in result.update_summary

    @pytest.mark.asyncio
    async def test_update_pass_age_gate_refusal_counts_as_deferred(self, griptape_nodes: GriptapeNodes) -> None:
        """A library that passes the check but is refused by the update pass age gate is deferred, not failed.

        This models a newer commit landing on the remote between the check and update passes: the
        update request comes back as UpdateLibraryResultFailure(age_gated=True) rather than success.
        """
        library_manager = griptape_nodes.LibraryManager()
        check_results = {
            "raced": self._check_result(has_update=True, gated=False),
        }

        def dispatch(request: object) -> object:
            if isinstance(request, LoadLibrariesRequest):
                return LoadLibrariesResultSuccess(result_details="loaded")
            if isinstance(request, ListRegisteredLibrariesRequest):
                return ListRegisteredLibrariesResultSuccess(libraries=list(check_results), result_details="listed")
            if isinstance(request, CheckLibraryUpdateRequest):
                return check_results[request.library_name]
            if isinstance(request, UpdateLibraryRequest):
                return UpdateLibraryResultFailure(result_details="too young", age_gated=True)
            error = f"Unexpected request routed through ahandle_request: {request!r}"
            raise AssertionError(error)

        with (
            patch.object(
                GriptapeNodes,
                "ConfigManager",
                return_value=_config_manager(enabled=True, min_age_hours=MIN_AGE_HOURS),
            ),
            patch.object(GriptapeNodes, "ahandle_request", new=AsyncMock(side_effect=dispatch)),
        ):
            result = await library_manager.sync_libraries_request(SyncLibrariesRequest())

        assert isinstance(result, SyncLibrariesResultSuccess)
        assert result.libraries_updated == 0
        assert result.libraries_deferred == 1
        assert result.update_summary["raced"] == {
            "old_version": "1.0.0",
            "new_version": "2.0.0",
            "status": "deferred_age_gate",
        }
