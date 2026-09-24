"""Tests for rez handling when WorkerManager spawns library workers."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.retained_mode.managers.worker_manager import WorkerManager

WORKER_MANAGER_MODULE = "griptape_nodes.retained_mode.managers.worker_manager"
SESSION = "sess-rez"
LIBRARY = "Demo Library"
FAMILY = "griptape_nodes_library_demo"
REZ_PREFIX = ["rez", "env", FAMILY, "--"]


@pytest.fixture
def worker_manager() -> WorkerManager:
    """A WorkerManager over a mock engine, enough to exercise spawning without a real process."""
    engine = MagicMock()
    engine.get_session_id.return_value = SESSION
    engine.config_manager.get_config_value.side_effect = lambda _key, default, cast_type=float: cast_type(default)
    engine.project_manager.get_pre_project_environ.return_value = {}
    engine.library_manager.execution_env_failure_reason.return_value = None
    engine.library_manager.execution_site_packages.return_value = None
    engine.library_manager.wait_for_execution_env = AsyncMock()
    engine.library_manager.get_library_info_by_library_name.return_value = SimpleNamespace(
        library_path="/libs/demo/griptape_nodes_library.json"
    )
    return WorkerManager(engine=engine, event_manager=MagicMock())


def _base_args() -> list[str]:
    return [sys.executable, "-m", "griptape_nodes_app", "engine", "--session-id", SESSION, "--library-name", LIBRARY]


class TestBuildRezWorkerArgs:
    def test_wraps_base_args_with_rez_env_prefix(self, worker_manager: WorkerManager) -> None:
        with (
            patch(f"{WORKER_MANAGER_MODULE}.library_file_path_to_rez_family", return_value=FAMILY) as mock_family,
            patch(f"{WORKER_MANAGER_MODULE}.resolve_and_log_rez_context", return_value=[]) as mock_resolve,
            patch(f"{WORKER_MANAGER_MODULE}.build_rez_env_prefix", return_value=REZ_PREFIX) as mock_prefix,
        ):
            wrapped = worker_manager._build_rez_worker_args(LIBRARY, _base_args())

        assert wrapped == [*REZ_PREFIX, *_base_args()]
        assert str(mock_family.call_args.args[0]).endswith("griptape_nodes_library.json")
        mock_resolve.assert_called_once_with([FAMILY])
        mock_prefix.assert_called_once_with([FAMILY])

    @pytest.mark.parametrize("library_info", [None, SimpleNamespace(library_path=None)])
    def test_leaves_args_alone_without_a_library_path(
        self, worker_manager: WorkerManager, library_info: SimpleNamespace | None
    ) -> None:
        worker_manager.engine.library_manager.get_library_info_by_library_name.return_value = library_info  # type: ignore[union-attr]

        with patch(f"{WORKER_MANAGER_MODULE}.build_rez_env_prefix") as mock_prefix:
            result = worker_manager._build_rez_worker_args(LIBRARY, _base_args())

        assert result == _base_args()
        mock_prefix.assert_not_called()


class TestSpawnWhenSessionReady:
    @pytest.mark.asyncio
    async def test_rez_library_spawns_wrapped(self, worker_manager: WorkerManager) -> None:
        with (
            patch(f"{WORKER_MANAGER_MODULE}.is_rez_enabled", return_value=True),
            patch(f"{WORKER_MANAGER_MODULE}.is_library_rez_package_available", return_value=True),
            patch.object(worker_manager, "_build_rez_worker_args", return_value=["wrapped"]) as mock_wrap,
            patch.object(worker_manager, "spawn_worker", AsyncMock()) as mock_spawn,
        ):
            await worker_manager._spawn_when_session_ready(LIBRARY)

        mock_wrap.assert_called_once_with(LIBRARY, _base_args())
        mock_spawn.assert_awaited_once_with(["wrapped"], LIBRARY)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("rez_enabled", "package_available"), [(False, True), (True, False)])
    async def test_non_rez_library_spawns_unwrapped(
        self, worker_manager: WorkerManager, *, rez_enabled: bool, package_available: bool
    ) -> None:
        with (
            patch(f"{WORKER_MANAGER_MODULE}.is_rez_enabled", return_value=rez_enabled),
            patch(f"{WORKER_MANAGER_MODULE}.is_library_rez_package_available", return_value=package_available),
            patch.object(worker_manager, "_build_rez_worker_args") as mock_wrap,
            patch.object(worker_manager, "spawn_worker", AsyncMock()) as mock_spawn,
        ):
            await worker_manager._spawn_when_session_ready(LIBRARY)

        mock_wrap.assert_not_called()
        mock_spawn.assert_awaited_once_with(_base_args(), LIBRARY)


class TestSpawnWorkerRezEnvironment:
    async def _spawn_env(self, worker_manager: WorkerManager, *, rez_enabled: bool) -> dict[str, str]:
        proc = MagicMock()
        proc.wait = AsyncMock()
        with (
            patch(f"{WORKER_MANAGER_MODULE}.is_rez_enabled", return_value=rez_enabled),
            patch.object(worker_manager, "_orchestrator_static_server_base_url", AsyncMock(return_value=None)),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as mock_exec,
        ):
            await worker_manager.spawn_worker(["gtn", "engine"], LIBRARY)
        return mock_exec.call_args.kwargs["env"]

    @pytest.mark.asyncio
    async def test_forwards_rez_variables_when_enabled(
        self, worker_manager: WorkerManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REZ_CONFIG_FILE", "/studio/rezconfig.py")
        monkeypatch.setenv("GTN_REZ_ROOT", "/studio")
        monkeypatch.setenv("UNRELATED_TEST_VAR", "x")

        env = await self._spawn_env(worker_manager, rez_enabled=True)

        assert env["REZ_CONFIG_FILE"] == "/studio/rezconfig.py"
        assert env["GTN_REZ_ROOT"] == "/studio"
        # Only rez variables are copied from the live environment; the rest comes from the
        # pre-project environ, which is empty here.
        assert "UNRELATED_TEST_VAR" not in env

    @pytest.mark.asyncio
    async def test_does_not_forward_rez_variables_when_disabled(
        self, worker_manager: WorkerManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REZ_CONFIG_FILE", "/studio/rezconfig.py")
        monkeypatch.setenv("GTN_REZ_ROOT", "/studio")

        env = await self._spawn_env(worker_manager, rez_enabled=False)

        assert "REZ_CONFIG_FILE" not in env
        assert "GTN_REZ_ROOT" not in env
