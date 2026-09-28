"""Tests for rez handling when WorkerManager spawns library workers."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.retained_mode.managers.worker_manager import WorkerManager
from griptape_nodes.utils.rez_utils import RezContextEnvironment

WORKER_MANAGER_MODULE = "griptape_nodes.retained_mode.managers.worker_manager"
REZ_UTILS_MODULE = "griptape_nodes.utils.rez_utils"
SESSION = "sess-rez"
LIBRARY = "Demo Library"
FAMILY = "griptape_nodes_library_demo"
CONTEXT = RezContextEnvironment(
    environment={"PYTHONPATH": "/store/lib/python", "REZ_USED_RESOLVE": f"{FAMILY}-1.0.0"}, failure=None
)


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


class TestRezWorkerContext:
    def test_reads_the_library_rez_environment(self, worker_manager: WorkerManager) -> None:
        with (
            patch(f"{WORKER_MANAGER_MODULE}.is_library_rez_package_available", return_value=True) as mock_available,
            patch(f"{WORKER_MANAGER_MODULE}.library_rez_request", return_value=FAMILY) as mock_request,
            patch(f"{WORKER_MANAGER_MODULE}.rez_context_environment", return_value=CONTEXT) as mock_context,
        ):
            context = worker_manager._rez_worker_context(LIBRARY)

        assert context == CONTEXT
        # The package check and the request both use the library's path, never its display name.
        assert mock_available.call_args.args[0] == Path("/libs/demo/griptape_nodes_library.json")
        assert mock_request.call_args.args[0] == Path("/libs/demo/griptape_nodes_library.json")
        mock_context.assert_called_once_with([FAMILY])

    def test_no_rez_package_means_no_rez_environment(self, worker_manager: WorkerManager) -> None:
        with (
            patch(f"{WORKER_MANAGER_MODULE}.is_library_rez_package_available", return_value=False),
            patch(f"{WORKER_MANAGER_MODULE}.rez_context_environment") as mock_context,
        ):
            assert worker_manager._rez_worker_context(LIBRARY) is None
        mock_context.assert_not_called()

    def test_finds_repo_named_package_when_display_name_differs(
        self, worker_manager: WorkerManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Real family lookup, no mocks for it: the manifest's display name ("Demo Library")
        # differs from the folder the package was built from.
        library_dir = tmp_path / "griptape-nodes-library-demo"
        library_dir.mkdir()
        manifest = library_dir / "griptape_nodes_library.json"
        manifest.write_text('{"name": "Demo Library"}')
        store = tmp_path / "store"
        version_dir = store / FAMILY / "1.0.0"
        version_dir.mkdir(parents=True)
        (version_dir / "package.py").write_text(f"name = '{FAMILY}'\n")
        monkeypatch.setattr(f"{REZ_UTILS_MODULE}.rez_package_stores", lambda: [store])
        worker_manager.engine.library_manager.get_library_info_by_library_name.return_value = SimpleNamespace(  # type: ignore[union-attr]
            library_path=str(manifest)
        )

        with (
            patch(f"{REZ_UTILS_MODULE}.get_git_repository_root", return_value=None),
            patch(f"{WORKER_MANAGER_MODULE}.rez_context_environment", return_value=CONTEXT) as mock_context,
        ):
            worker_manager._rez_worker_context(LIBRARY)

        mock_context.assert_called_once_with([f"{FAMILY}==1.0.0"])

    def test_pinned_registration_runs_that_version_not_the_newest(
        self, worker_manager: WorkerManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # REZ:<family>-1.0.0 loads the manifest from the 1.0.0 folder; the store also has 1.1.0.
        store = tmp_path / "store"
        for version in ("1.0.0", "1.1.0"):
            python_dir = store / FAMILY / version / "python"
            python_dir.mkdir(parents=True)
            (python_dir.parent / "package.py").write_text(f"name = '{FAMILY}'\n")
            (python_dir / "griptape_nodes_library.json").write_text('{"name": "Demo Library"}')
        monkeypatch.setattr(f"{REZ_UTILS_MODULE}.rez_package_stores", lambda: [store])
        worker_manager.engine.library_manager.get_library_info_by_library_name.return_value = SimpleNamespace(  # type: ignore[union-attr]
            library_path=str(store / FAMILY / "1.0.0" / "python" / "griptape_nodes_library.json")
        )

        with patch(f"{WORKER_MANAGER_MODULE}.rez_context_environment", return_value=CONTEXT) as mock_context:
            worker_manager._rez_worker_context(LIBRARY)

        mock_context.assert_called_once_with([f"{FAMILY}==1.0.0"])

    @pytest.mark.parametrize("library_info", [None, SimpleNamespace(library_path=None)])
    def test_no_library_path_means_no_rez_environment(
        self, worker_manager: WorkerManager, library_info: SimpleNamespace | None
    ) -> None:
        worker_manager.engine.library_manager.get_library_info_by_library_name.return_value = library_info  # type: ignore[union-attr]

        with patch(f"{WORKER_MANAGER_MODULE}.rez_context_environment") as mock_context:
            assert worker_manager._rez_worker_context(LIBRARY) is None
        mock_context.assert_not_called()

    def test_worker_resolve_carries_this_workstations_torch_build(self, worker_manager: WorkerManager) -> None:
        with (
            patch(f"{WORKER_MANAGER_MODULE}.is_library_rez_package_available", return_value=True),
            patch(f"{WORKER_MANAGER_MODULE}.library_rez_request", return_value=FAMILY),
            patch(f"{WORKER_MANAGER_MODULE}.torch_backend_requests", return_value=[".torch_backend-cu118"]),
            patch(f"{WORKER_MANAGER_MODULE}.rez_context_environment", return_value=CONTEXT) as mock_context,
        ):
            worker_manager._rez_worker_context(LIBRARY)

        mock_context.assert_called_once_with([FAMILY, ".torch_backend-cu118"])


class TestSpawnWhenSessionReady:
    @pytest.mark.asyncio
    async def test_rez_worker_starts_plainly_with_the_library_environment(self, worker_manager: WorkerManager) -> None:
        with (
            patch(f"{WORKER_MANAGER_MODULE}.is_rez_enabled", return_value=True),
            patch.object(worker_manager, "_rez_worker_context", return_value=CONTEXT),
            patch.object(worker_manager, "spawn_worker", AsyncMock()) as mock_spawn,
        ):
            await worker_manager._spawn_when_session_ready(LIBRARY)

        # No `rez env` wrapper and nothing run inside the environment: the plain worker command.
        mock_spawn.assert_awaited_once_with(_base_args(), LIBRARY, rez_environ=CONTEXT.environment)

    @pytest.mark.asyncio
    async def test_unresolvable_environment_reports_and_does_not_spawn(self, worker_manager: WorkerManager) -> None:
        failed = RezContextEnvironment(environment=None, failure="The following package conflicts occurred")
        with (
            patch(f"{WORKER_MANAGER_MODULE}.is_rez_enabled", return_value=True),
            patch.object(worker_manager, "_rez_worker_context", return_value=failed),
            patch.object(worker_manager, "note_worker_unavailable") as mock_unavailable,
            patch.object(worker_manager, "spawn_worker", AsyncMock()) as mock_spawn,
        ):
            await worker_manager._spawn_when_session_ready(LIBRARY)

        mock_spawn.assert_not_awaited()
        mock_unavailable.assert_called_once_with(
            LIBRARY, "its rez environment could not be set up: The following package conflicts occurred."
        )

    @pytest.mark.asyncio
    async def test_library_without_rez_package_spawns_without_rez(self, worker_manager: WorkerManager) -> None:
        with (
            patch(f"{WORKER_MANAGER_MODULE}.is_rez_enabled", return_value=True),
            patch.object(worker_manager, "_rez_worker_context", return_value=None),
            patch.object(worker_manager, "spawn_worker", AsyncMock()) as mock_spawn,
        ):
            await worker_manager._spawn_when_session_ready(LIBRARY)

        mock_spawn.assert_awaited_once_with(_base_args(), LIBRARY, rez_environ=None)

    @pytest.mark.asyncio
    async def test_rez_disabled_spawns_without_rez(self, worker_manager: WorkerManager) -> None:
        with (
            patch(f"{WORKER_MANAGER_MODULE}.is_rez_enabled", return_value=False),
            patch.object(worker_manager, "_rez_worker_context") as mock_context,
            patch.object(worker_manager, "spawn_worker", AsyncMock()) as mock_spawn,
        ):
            await worker_manager._spawn_when_session_ready(LIBRARY)

        mock_context.assert_not_called()
        mock_spawn.assert_awaited_once_with(_base_args(), LIBRARY, rez_environ=None)


class TestSpawnWorkerRezEnvironment:
    async def _spawn_env(
        self, worker_manager: WorkerManager, *, rez_enabled: bool, rez_environ: dict[str, str] | None = None
    ) -> dict[str, str]:
        proc = MagicMock()
        proc.wait = AsyncMock()
        with (
            patch(f"{WORKER_MANAGER_MODULE}.is_rez_enabled", return_value=rez_enabled),
            patch.object(worker_manager, "_orchestrator_static_server_base_url", AsyncMock(return_value=None)),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as mock_exec,
        ):
            await worker_manager.spawn_worker(["gtn", "engine"], LIBRARY, rez_environ=rez_environ)
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
    async def test_opt_in_config_reaches_the_worker_layered_on_the_studio_config(
        self, worker_manager: WorkerManager, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        config = tmp_path / "griptape_rezconfig.py"
        monkeypatch.setenv("REZ_CONFIG_FILE", "/studio/rezconfig.py")
        monkeypatch.setenv("GTN_REZ_CONFIG_FILE", str(config))

        env = await self._spawn_env(worker_manager, rez_enabled=True)

        assert env["REZ_CONFIG_FILE"] == os.pathsep.join(["/studio/rezconfig.py", str(config)])

    @pytest.mark.asyncio
    async def test_library_environment_first_then_the_engines(self, worker_manager: WorkerManager) -> None:
        # Inside griptape_launch, PYTHONPATH holds the engine and its dependencies. The worker
        # gets the library's rez environment with that PYTHONPATH after the library's paths.
        worker_manager.engine.project_manager.get_pre_project_environ.return_value = {  # type: ignore[union-attr]
            "PYTHONPATH": "/store/griptape_nodes_engine/python"
        }
        rez_environ = {"PYTHONPATH": "/store/torch/python", "REZ_USED_RESOLVE": f"{FAMILY}-1.0.0", "PATH": "/rez/bin"}

        env = await self._spawn_env(worker_manager, rez_enabled=True, rez_environ=rez_environ)

        assert env["PYTHONPATH"] == os.pathsep.join(["/store/torch/python", "/store/griptape_nodes_engine/python"])
        assert env["REZ_USED_RESOLVE"] == f"{FAMILY}-1.0.0"
        assert env["PATH"] == "/rez/bin"

    @pytest.mark.asyncio
    async def test_without_a_rez_environment_pythonpath_is_untouched(self, worker_manager: WorkerManager) -> None:
        worker_manager.engine.project_manager.get_pre_project_environ.return_value = {  # type: ignore[union-attr]
            "PYTHONPATH": "/somewhere"
        }

        env = await self._spawn_env(worker_manager, rez_enabled=True)

        assert env["PYTHONPATH"] == "/somewhere"
        assert "REZ_USED_RESOLVE" not in env

    @pytest.mark.asyncio
    async def test_does_not_forward_rez_variables_when_disabled(
        self, worker_manager: WorkerManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REZ_CONFIG_FILE", "/studio/rezconfig.py")
        monkeypatch.setenv("GTN_REZ_ROOT", "/studio")

        env = await self._spawn_env(worker_manager, rez_enabled=False)

        assert "REZ_CONFIG_FILE" not in env
        assert "GTN_REZ_ROOT" not in env
