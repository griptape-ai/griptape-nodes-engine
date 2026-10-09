"""Tests for resetting a library's Python environments.

A reset deletes the library's `.venv` and `.venv-exec` and registers it again. When this process
already imports from the `.venv`, or a directory cannot be deleted, the removal is recorded and
finishes at the next engine start instead.
"""

from __future__ import annotations

import json
import sysconfig
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import anyio
import pytest

from griptape_nodes.node_library.library_registry import (
    CategoryDefinition,
    LibraryMetadata,
    LibraryRegistry,
    LibrarySchema,
    NodeDefinition,
    NodeMetadata,
)
from griptape_nodes.retained_mode.events.app_events import AppInitializationComplete
from griptape_nodes.retained_mode.events.library_events import (
    LoadLibraryMetadataFromFileResultSuccess,
    LoadMetadataForAllLibrariesRequest,
    LoadMetadataForAllLibrariesResultSuccess,
    ResetLibraryRequest,
    ResetLibraryResultFailure,
    ResetLibraryResultSuccess,
)
from griptape_nodes.retained_mode.managers.fitness_problems.libraries import DependencyInstallationFailedProblem
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager
from griptape_nodes.retained_mode.managers.settings import LIBRARIES_TO_REGISTER_KEY

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.managers.library.reset import LibraryReset

LIBRARY_NAME = "Test Library"


class _StopInitialization(Exception):  # noqa: N818 (a control-flow sentinel, not an error)
    """Raised by a patched step to end app initialization once the order under test has run."""


def _write_manifest(directory: Path) -> Path:
    schema = LibrarySchema(
        name=LIBRARY_NAME,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="test",
            description="test manifest",
            library_version="1.0.0",
            engine_version="0.98.0",
            tags=[],
        ),
        categories=[{"Test": CategoryDefinition(title="Test", description="test", color="#000", icon="Folder")}],
        nodes=[
            NodeDefinition(
                class_name="TestNode",
                file_path="node.py",
                metadata=NodeMetadata(category="Test", description="test node", display_name="Test"),
            )
        ],
    )
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "griptape_nodes_library.json"
    manifest.write_text(json.dumps(schema.model_dump(mode="json")), encoding="utf-8")
    return manifest


def _make_env(env_path: Path) -> Path:
    """Create a directory standing in for a built environment, with a file in it."""
    env_path.mkdir(parents=True)
    (env_path / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    return env_path


def _pending_record(reset: LibraryReset) -> object:
    return json.loads(reset.pending_resets_path().read_text(encoding="utf-8"))


def _site_packages(env_path: Path) -> str:
    return sysconfig.get_path("purelib", vars={"base": str(env_path), "platbase": str(env_path)})


def _track(
    library_manager: LibraryManager,
    manifest: Path,
    *,
    lifecycle_state: LibraryManager.LibraryLifecycleState = LibraryManager.LibraryLifecycleState.FAILURE,
    is_sandbox: bool = False,
) -> LibraryManager.LibraryInfo:
    library_info = LibraryManager.LibraryInfo(
        lifecycle_state=lifecycle_state,
        fitness=LibraryManager.LibraryFitness.UNUSABLE,
        library_path=str(manifest),
        is_sandbox=is_sandbox,
        library_name=LIBRARY_NAME,
        library_version="1.0.0",
    )
    library_manager._library_file_path_to_info[str(manifest)] = library_info
    return library_info


@pytest.fixture(autouse=True)
def _clean_registry() -> Generator[None, None, None]:
    LibraryRegistry._clear()
    yield
    LibraryRegistry._clear()


@pytest.fixture
def library_dir(tmp_path: Path) -> Path:
    """The directory holding the library's manifest, and its environments next to it."""
    return tmp_path / "test_library"


@pytest.fixture
def manifest(library_dir: Path) -> Path:
    """The library's manifest on disk."""
    return _write_manifest(library_dir)


@pytest.fixture
def reload_mock(engine: Engine) -> Generator[AsyncMock, None, None]:
    """Stand in for registering the library again, and for its worker, which the reset drives."""
    library_manager = engine.library_manager
    reload = AsyncMock(return_value="1.0.0")
    with (
        patch.object(library_manager.git_operations, "_reload_library_after_git_operation", new=reload),
        patch.object(library_manager.workers, "stop_worker_for_library", new=AsyncMock()),
        patch.object(library_manager.workers, "start_worker_for_library", new=AsyncMock()),
    ):
        yield reload


class TestResetRefusals:
    @pytest.mark.asyncio
    async def test_unknown_library_is_refused(self, engine: Engine) -> None:
        result = await engine.ahandle_request(ResetLibraryRequest(library_name=LIBRARY_NAME))

        assert isinstance(result, ResetLibraryResultFailure)
        assert LIBRARY_NAME in str(result.result_details)

    @pytest.mark.asyncio
    async def test_disabled_library_is_refused(self, engine: Engine, manifest: Path, library_dir: Path) -> None:
        """Registering it again would turn it back on for the session."""
        _track(engine.library_manager, manifest, lifecycle_state=LibraryManager.LibraryLifecycleState.DISABLED)
        _make_env(library_dir / ".venv")

        result = await engine.ahandle_request(ResetLibraryRequest(library_name=LIBRARY_NAME))

        assert isinstance(result, ResetLibraryResultFailure)
        assert "disabled" in str(result.result_details)
        assert await anyio.Path(library_dir / ".venv").exists()

    @pytest.mark.asyncio
    async def test_sandbox_library_is_refused(self, engine: Engine, manifest: Path) -> None:
        _track(engine.library_manager, manifest, is_sandbox=True)

        result = await engine.ahandle_request(ResetLibraryRequest(library_name=LIBRARY_NAME))

        assert isinstance(result, ResetLibraryResultFailure)

    @pytest.mark.asyncio
    async def test_environment_mode_refuses_and_touches_nothing(
        self, engine: Engine, manifest: Path, library_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The environment owns the library's packages, including a venv left from an earlier run."""
        monkeypatch.setenv("GTN_CONFIG_LIBRARY__PROVISIONED_BY", "environment")
        engine.config_manager.load_configs()
        _track(engine.library_manager, manifest)
        _make_env(library_dir / ".venv")

        result = await engine.ahandle_request(ResetLibraryRequest(library_name=LIBRARY_NAME))

        assert isinstance(result, ResetLibraryResultFailure)
        assert "environment" in str(result.result_details)
        assert await anyio.Path(library_dir / ".venv").exists()
        assert not await anyio.Path(engine.library_manager.reset.pending_resets_path()).exists()


class TestResetNow:
    @pytest.mark.asyncio
    async def test_removes_both_environments_and_registers_again(
        self, engine: Engine, manifest: Path, library_dir: Path, reload_mock: AsyncMock
    ) -> None:
        library_manager = engine.library_manager
        _track(library_manager, manifest)
        edit_env = _make_env(library_dir / ".venv")
        exec_env = _make_env(library_dir / ".venv-exec")

        result = await engine.ahandle_request(ResetLibraryRequest(library_name=LIBRARY_NAME))

        assert isinstance(result, ResetLibraryResultSuccess)
        assert result.restart_required is False
        assert result.removed_paths == [str(edit_env), str(exec_env)]
        assert not await anyio.Path(edit_env).exists()
        assert not await anyio.Path(exec_env).exists()
        # The library's own files are kept.
        assert await anyio.Path(manifest).exists()
        reload_mock.assert_awaited_once()
        assert reload_mock.await_args is not None
        assert reload_mock.await_args.args == (LIBRARY_NAME, str(manifest))

    @pytest.mark.asyncio
    async def test_stops_the_worker_before_removing_and_starts_it_after(
        self, engine: Engine, manifest: Path, library_dir: Path, reload_mock: AsyncMock
    ) -> None:
        """The worker holds `.venv-exec` on its import path, so it has to be gone first."""
        library_manager = engine.library_manager
        _track(library_manager, manifest)
        exec_env = _make_env(library_dir / ".venv-exec")
        exec_env_present_at_stop: list[bool] = []

        async def record_stop(_library_name: str) -> None:
            exec_env_present_at_stop.append(await anyio.Path(exec_env).exists())

        library_manager.workers.stop_worker_for_library.side_effect = record_stop  # type: ignore[attr-defined]

        await engine.ahandle_request(ResetLibraryRequest(library_name=LIBRARY_NAME))

        assert exec_env_present_at_stop == [True]
        library_manager.workers.start_worker_for_library.assert_awaited_once_with(LIBRARY_NAME)  # type: ignore[attr-defined]
        reload_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_library_without_environments_still_registers_again(
        self, engine: Engine, manifest: Path, reload_mock: AsyncMock
    ) -> None:
        _track(engine.library_manager, manifest)

        result = await engine.ahandle_request(ResetLibraryRequest(library_name=LIBRARY_NAME))

        assert isinstance(result, ResetLibraryResultSuccess)
        assert result.removed_paths == []
        reload_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_directory_that_cannot_be_removed_finishes_at_next_start(
        self, engine: Engine, manifest: Path, library_dir: Path, reload_mock: AsyncMock
    ) -> None:
        """A file still in use (Windows, antivirus) defers that directory instead of failing the reset."""
        library_manager = engine.library_manager
        _track(library_manager, manifest)
        edit_env = _make_env(library_dir / ".venv")
        exec_env = _make_env(library_dir / ".venv-exec")

        async def locked_exec_env(env_path: Path) -> str | None:
            if env_path == exec_env:
                return "the file is in use"
            edit_env.joinpath("pyvenv.cfg").unlink()
            edit_env.rmdir()
            return None

        with patch.object(library_manager.reset, "_remove_env_dir", side_effect=locked_exec_env):
            result = await engine.ahandle_request(ResetLibraryRequest(library_name=LIBRARY_NAME))

        assert isinstance(result, ResetLibraryResultSuccess)
        assert result.restart_required is True
        assert result.removed_paths == [str(edit_env)]
        pending = _pending_record(library_manager.reset)
        assert pending == {"env_paths": [str(exec_env)]}
        reload_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_failing_to_load_again_reports_the_problems_of_the_library(
        self, engine: Engine, manifest: Path, library_dir: Path, reload_mock: AsyncMock
    ) -> None:
        library_manager = engine.library_manager
        library_info = _track(library_manager, manifest)
        _make_env(library_dir / ".venv")

        async def fail_to_load(*_args: object, **_kwargs: object) -> ResetLibraryResultFailure:
            library_info.problems.append(
                DependencyInstallationFailedProblem(error_details="no wheel for this platform")
            )
            return ResetLibraryResultFailure(result_details="Failed to reload Library after git operation.")

        reload_mock.side_effect = fail_to_load

        result = await engine.ahandle_request(ResetLibraryRequest(library_name=LIBRARY_NAME))

        assert isinstance(result, ResetLibraryResultFailure)
        assert "no wheel for this platform" in str(result.result_details)
        assert "git operation" not in str(result.result_details)
        library_manager.workers.start_worker_for_library.assert_not_awaited()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_failing_to_load_after_a_locked_directory_still_asks_for_a_restart(
        self, engine: Engine, manifest: Path, library_dir: Path, reload_mock: AsyncMock
    ) -> None:
        """A directory left for the next start is still there, so the failure must not claim it was removed."""
        library_manager = engine.library_manager
        _track(library_manager, manifest)
        exec_env = _make_env(library_dir / ".venv-exec")
        reload_mock.return_value = ResetLibraryResultFailure(result_details="Failed to reload Library.")

        async def locked(_env_path: Path) -> str | None:
            return "the file is in use"

        with patch.object(library_manager.reset, "_remove_env_dir", side_effect=locked):
            result = await engine.ahandle_request(ResetLibraryRequest(library_name=LIBRARY_NAME))

        assert isinstance(result, ResetLibraryResultFailure)
        assert "were removed" not in str(result.result_details)
        assert "restart the engine" in str(result.result_details)
        assert _pending_record(library_manager.reset) == {"env_paths": [str(exec_env)]}


class TestResetAtNextStart:
    @pytest.mark.asyncio
    async def test_an_environment_this_process_imports_from_is_left_for_the_next_start(
        self,
        engine: Engine,
        manifest: Path,
        library_dir: Path,
        reload_mock: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Deleting it now would leave old modules in memory over new files, and Windows refuses it."""
        library_manager = engine.library_manager
        _track(library_manager, manifest, lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED)
        edit_env = _make_env(library_dir / ".venv")
        exec_env = _make_env(library_dir / ".venv-exec")
        monkeypatch.syspath_prepend(_site_packages(edit_env))

        result = await engine.ahandle_request(ResetLibraryRequest(library_name=LIBRARY_NAME))

        assert isinstance(result, ResetLibraryResultSuccess)
        assert result.restart_required is True
        assert result.removed_paths == []
        assert await anyio.Path(edit_env).exists()
        assert await anyio.Path(exec_env).exists()
        pending = _pending_record(library_manager.reset)
        assert pending == {"env_paths": [str(edit_env), str(exec_env)]}
        reload_mock.assert_not_awaited()
        library_manager.workers.stop_worker_for_library.assert_not_awaited()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_resetting_twice_records_each_directory_once(
        self, engine: Engine, manifest: Path, library_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        library_manager = engine.library_manager
        _track(library_manager, manifest, lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED)
        edit_env = _make_env(library_dir / ".venv")
        monkeypatch.syspath_prepend(_site_packages(edit_env))

        await engine.ahandle_request(ResetLibraryRequest(library_name=LIBRARY_NAME))
        await engine.ahandle_request(ResetLibraryRequest(library_name=LIBRARY_NAME))

        pending = _pending_record(library_manager.reset)
        assert pending == {"env_paths": [str(edit_env), str(library_dir / ".venv-exec")]}


class TestApplyPendingResets:
    @pytest.mark.asyncio
    async def test_removes_recorded_environments_and_clears_the_record(self, engine: Engine, library_dir: Path) -> None:
        reset = engine.library_manager.reset
        edit_env = _make_env(library_dir / ".venv")
        exec_env = _make_env(library_dir / ".venv-exec")
        reset.schedule_removal([edit_env, exec_env])

        await reset.apply_pending_resets()

        assert not await anyio.Path(edit_env).exists()
        assert not await anyio.Path(exec_env).exists()
        assert not await anyio.Path(reset.pending_resets_path()).exists()

    @pytest.mark.asyncio
    async def test_never_removes_a_directory_that_is_not_a_library_environment(
        self, engine: Engine, library_dir: Path
    ) -> None:
        """The record is a plain file, so an entry that names anything else is ignored."""
        reset = engine.library_manager.reset
        await anyio.Path(library_dir).mkdir(parents=True)
        reset.schedule_removal([library_dir])

        await reset.apply_pending_resets()

        assert await anyio.Path(library_dir).exists()
        assert not await anyio.Path(reset.pending_resets_path()).exists()

    @pytest.mark.asyncio
    async def test_a_directory_that_still_cannot_be_removed_stays_recorded(
        self, engine: Engine, library_dir: Path
    ) -> None:
        reset = engine.library_manager.reset
        edit_env = _make_env(library_dir / ".venv")
        exec_env = _make_env(library_dir / ".venv-exec")
        reset.schedule_removal([edit_env, exec_env])

        async def locked_exec_env(env_path: Path) -> str | None:
            if env_path == exec_env:
                return "the file is in use"
            return None

        with patch.object(reset, "_remove_env_dir", side_effect=locked_exec_env):
            await reset.apply_pending_resets()

        pending = _pending_record(reset)
        assert pending == {"env_paths": [str(exec_env)]}

    @pytest.mark.asyncio
    async def test_environment_mode_keeps_the_record_and_the_directories(
        self, engine: Engine, library_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reset = engine.library_manager.reset
        edit_env = _make_env(library_dir / ".venv")
        reset.schedule_removal([edit_env])
        monkeypatch.setenv("GTN_CONFIG_LIBRARY__PROVISIONED_BY", "environment")
        engine.config_manager.load_configs()

        await reset.apply_pending_resets()

        assert await anyio.Path(edit_env).exists()
        assert await anyio.Path(reset.pending_resets_path()).exists()

    @pytest.mark.asyncio
    async def test_an_unreadable_record_removes_nothing(self, engine: Engine, library_dir: Path) -> None:
        reset = engine.library_manager.reset
        edit_env = _make_env(library_dir / ".venv")
        await anyio.Path(reset.pending_resets_path().parent).mkdir(parents=True, exist_ok=True)
        await anyio.Path(reset.pending_resets_path()).write_text("not json", encoding="utf-8")

        await reset.apply_pending_resets()

        assert await anyio.Path(edit_env).exists()

    @pytest.mark.asyncio
    async def test_orchestrator_applies_them_before_loading_libraries(self, engine: Engine) -> None:
        library_manager = engine.library_manager
        calls: list[str] = []

        async def record_apply() -> None:
            calls.append("apply")

        async def record_load(**_kwargs: object) -> list[str]:
            calls.append("load")
            raise _StopInitialization

        with (
            patch.object(library_manager.discovery, "migrate_old_xdg_library_paths"),
            patch.object(library_manager.provisioning, "ensure_libraries_from_config", new=AsyncMock()),
            patch.object(library_manager.reset, "apply_pending_resets", side_effect=record_apply),
            patch.object(library_manager, "load_all_libraries_from_config", side_effect=record_load),
            pytest.raises(_StopInitialization),
        ):
            await library_manager._run_app_initialization(AppInitializationComplete())

        assert calls == ["apply", "load"]

    @pytest.mark.asyncio
    async def test_a_worker_leaves_them_to_the_orchestrator(self, engine: Engine) -> None:
        library_manager = engine.library_manager
        apply = AsyncMock()

        with (
            patch.object(library_manager.discovery, "migrate_old_xdg_library_paths"),
            patch.object(library_manager.provisioning, "ensure_libraries_from_config", new=AsyncMock()),
            patch.object(library_manager.reset, "apply_pending_resets", new=apply),
            patch.object(library_manager, "load_all_libraries_from_config", side_effect=_StopInitialization),
            pytest.raises(_StopInitialization),
        ):
            await library_manager._run_app_initialization(
                AppInitializationComplete(is_worker=True, libraries_to_register=[LIBRARY_NAME])
            )

        apply.assert_not_awaited()


class TestMetadataSaysWhetherResetWaits:
    """A client reads this to say a restart is needed before the artist confirms, not after."""

    @pytest.mark.asyncio
    async def test_true_when_this_engine_imports_from_the_environment(
        self, engine: Engine, manifest: Path, library_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _register_loaded(engine, manifest, tmp_path)
        monkeypatch.syspath_prepend(_site_packages(_make_env(library_dir / ".venv")))

        entry = await _metadata_entry(engine, manifest)

        assert entry.reset_requires_restart is True

    @pytest.mark.asyncio
    async def test_false_when_the_reset_would_finish_now(
        self, engine: Engine, manifest: Path, library_dir: Path, tmp_path: Path
    ) -> None:
        _register_loaded(engine, manifest, tmp_path)
        _make_env(library_dir / ".venv")

        entry = await _metadata_entry(engine, manifest)

        assert entry.reset_requires_restart is False


def _register_loaded(engine: Engine, manifest: Path, tmp_path: Path) -> None:
    config = engine.config_manager
    config.set_config_value(LIBRARIES_TO_REGISTER_KEY, [str(manifest)])
    config.set_config_value("sandbox_library_directory", str(tmp_path / "sandbox"))
    _track(engine.library_manager, manifest, lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED)


async def _metadata_entry(engine: Engine, manifest: Path) -> LoadLibraryMetadataFromFileResultSuccess:
    result = await engine.ahandle_request(LoadMetadataForAllLibrariesRequest())
    assert isinstance(result, LoadMetadataForAllLibrariesResultSuccess)
    [entry] = [entry for entry in result.successful_libraries if entry.file_path == str(manifest)]
    return entry
