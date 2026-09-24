"""Tests for RezManager status and health request handling."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from griptape_nodes.retained_mode.events.app_events import RezStatusNotification
from griptape_nodes.retained_mode.events.rez_events import (
    CheckRezHealthRequest,
    CheckRezHealthResultSuccess,
    GetRezStatusRequest,
    GetRezStatusResultSuccess,
    RezHealthStatus,
)
from griptape_nodes.retained_mode.managers.rez_manager import RezManager
from griptape_nodes.utils.rez_utils import RezHealthResult

REZ_MANAGER_MODULE = "griptape_nodes.retained_mode.managers.rez_manager"
PACKAGE_COUNT = 42
DURATION_MS = 120.0
TIMESTAMP = "2026-09-24T12:00:00+00:00"


def _library_info(name: str, path: str, *, has_rez_package: bool, version: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        library_name=name,
        library_path=path,
        has_rez_package=has_rez_package,
        rez_family=name.lower().replace(" ", "_"),
        rez_version=version,
    )


@pytest.fixture
def engine() -> MagicMock:
    """A mock engine whose library manager tracks a mix of reportable and unreportable libraries."""
    engine = MagicMock()
    engine.library_manager._library_file_path_to_info = {
        "/libs/a.json": _library_info("Lib A", "/libs/a.json", has_rez_package=True, version="1.2.0"),
        "/libs/b.json": _library_info("Lib B", "/libs/b.json", has_rez_package=False, version=None),
        # Entries without a name or path are not reported.
        "/libs/c.json": _library_info("", "/libs/c.json", has_rez_package=False, version=None),
        "unnamed": _library_info("Lib D", "", has_rez_package=False, version=None),
    }
    return engine


@pytest.fixture
def manager(engine: MagicMock) -> RezManager:
    """A RezManager bound to the mock engine."""
    return RezManager(engine=engine)


def _health(*, healthy: bool) -> RezHealthResult:
    return RezHealthResult(
        healthy=healthy, check_duration_ms=DURATION_MS, package_count=PACKAGE_COUNT, timestamp=TIMESTAMP
    )


class TestConstruction:
    def test_registers_request_handlers(self) -> None:
        event_manager = MagicMock()

        manager = RezManager(event_manager, engine=MagicMock())

        registered = {
            call.args[0]: call.args[1] for call in event_manager.assign_manager_to_request_type.call_args_list
        }
        assert registered[GetRezStatusRequest] == manager.handle_get_rez_status
        assert registered[CheckRezHealthRequest] == manager.handle_check_rez_health


class TestStartupHealthCheck:
    @pytest.mark.parametrize("healthy", [True, False])
    def test_caches_health(self, manager: RezManager, *, healthy: bool) -> None:
        with patch(f"{REZ_MANAGER_MODULE}.check_rez_health_detailed", return_value=_health(healthy=healthy)):
            manager.run_startup_health_check()

        assert manager._cached_health == RezHealthStatus(
            healthy=healthy, check_duration_ms=DURATION_MS, package_count=PACKAGE_COUNT, timestamp=TIMESTAMP
        )


class TestGetRezStatus:
    def test_reports_status_and_named_libraries(self, manager: RezManager, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GTN_REZ_ROOT", "/mnt/pipeline")
        path_map = {"linux": "/mnt/pipeline", "windows": "P:"}
        with (
            patch(f"{REZ_MANAGER_MODULE}.is_rez_enabled", return_value=True),
            patch(f"{REZ_MANAGER_MODULE}.get_rez_context_string", return_value="rez-env griptape_launch"),
            patch(f"{REZ_MANAGER_MODULE}.rez_path_map", return_value=path_map),
        ):
            result = manager.handle_get_rez_status(GetRezStatusRequest())

        assert isinstance(result, GetRezStatusResultSuccess)
        assert result.enabled is True
        assert result.context_string == "rez-env griptape_launch"
        assert result.studio_root == "/mnt/pipeline"
        assert result.path_map == path_map
        # No health check has run yet, so an empty status is reported.
        assert result.health == RezHealthStatus()
        assert [s.library_name for s in result.library_statuses] == ["Lib A", "Lib B"]
        lib_a = result.library_statuses[0]
        assert lib_a.has_rez_package is True
        assert lib_a.rez_version == "1.2.0"

    def test_reports_cached_health(self, manager: RezManager) -> None:
        cached = RezHealthStatus(healthy=True, check_duration_ms=1.0, package_count=3, timestamp=TIMESTAMP)
        manager._cached_health = cached
        with patch(f"{REZ_MANAGER_MODULE}.is_rez_enabled", return_value=False):
            result = manager.handle_get_rez_status(GetRezStatusRequest())

        assert isinstance(result, GetRezStatusResultSuccess)
        assert result.enabled is False
        assert result.health == cached


class TestCheckRezHealth:
    @pytest.mark.asyncio
    async def test_runs_check_caches_and_pushes_notification(self, manager: RezManager, engine: MagicMock) -> None:
        with (
            patch(f"{REZ_MANAGER_MODULE}.check_rez_health_detailed", return_value=_health(healthy=True)),
            patch(f"{REZ_MANAGER_MODULE}.is_rez_enabled", return_value=True),
            patch(f"{REZ_MANAGER_MODULE}.get_rez_context_string", return_value="ctx"),
        ):
            result = await manager.handle_check_rez_health(CheckRezHealthRequest())

        assert isinstance(result, CheckRezHealthResultSuccess)
        assert result.healthy is True
        assert result.package_count == PACKAGE_COUNT
        assert manager._cached_health is not None
        assert manager._cached_health.healthy is True

        engine.event_manager.put_event.assert_called_once()
        notification = cast("RezStatusNotification", engine.event_manager.put_event.call_args.args[0].payload)
        assert isinstance(notification, RezStatusNotification)
        assert notification.enabled is True
        assert notification.context_string == "ctx"
        assert notification.health == manager._cached_health
        assert [s.library_name for s in notification.library_statuses] == ["Lib A", "Lib B"]

    @pytest.mark.asyncio
    async def test_reports_failure(self, manager: RezManager) -> None:
        with (
            patch(f"{REZ_MANAGER_MODULE}.check_rez_health_detailed", return_value=_health(healthy=False)),
            patch(f"{REZ_MANAGER_MODULE}.is_rez_enabled", return_value=True),
            patch(f"{REZ_MANAGER_MODULE}.get_rez_context_string", return_value=""),
        ):
            result = await manager.handle_check_rez_health(CheckRezHealthRequest())

        assert isinstance(result, CheckRezHealthResultSuccess)
        assert result.healthy is False
        assert "FAILED" in str(result.result_details)
