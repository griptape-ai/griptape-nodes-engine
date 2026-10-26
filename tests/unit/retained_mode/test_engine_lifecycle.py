"""Tests for the engine lifecycle state, startup sequence, and ready announcement."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from griptape_nodes.retained_mode.engine_lifecycle import (
    EngineLifecycle,
    EngineLifecycleState,
)
from griptape_nodes.retained_mode.events.app_events import (
    AppInitializationComplete,
    EngineHeartbeatRequest,
    EngineHeartbeatResultSuccess,
    EngineReadyEvent,
    LibraryLoadStatus,
)
from griptape_nodes.retained_mode.events.base_events import AppEvent
from griptape_nodes.retained_mode.events.library_events import (
    ReloadAllLibrariesRequest,
    ReloadAllLibrariesResultSuccess,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine


class TestEngineLifecycleState:
    def test_initializing_before_app_initialization(self) -> None:
        """A fresh engine reports initializing until AppInitializationComplete has been handled."""
        lifecycle = EngineLifecycle(MagicMock())

        assert lifecycle.state is EngineLifecycleState.STARTING
        assert lifecycle.is_initializing() is True

    def test_engine_wires_the_lifecycle_to_app_initialization_complete(self, engine: Engine) -> None:
        listeners = engine.event_manager._app_event_listeners[AppInitializationComplete]

        owners = {getattr(listener, "__self__", None) for listener in listeners}

        assert engine.lifecycle in owners
        assert engine.library_manager not in owners

    def test_reloading_brackets_the_state(self) -> None:
        lifecycle = EngineLifecycle(MagicMock())

        with lifecycle.reloading():
            assert lifecycle.state is EngineLifecycleState.RELOADING
            assert lifecycle.is_initializing() is True

        assert lifecycle.state is EngineLifecycleState.READY
        assert lifecycle.is_initializing() is False

    def test_reloading_restores_ready_on_exception(self) -> None:
        """A failure during reload still reaches READY (finally), so the GUI doesn't hang."""
        lifecycle = EngineLifecycle(MagicMock())

        def fail_inside_reload() -> None:
            with lifecycle.reloading():
                msg = "boom"
                raise RuntimeError(msg)

        with pytest.raises(RuntimeError, match="boom"):
            fail_inside_reload()

        assert lifecycle.is_initializing() is False


class TestStartupHandler:
    @pytest.mark.asyncio
    async def test_is_starting_during_startup_and_ready_after(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lifecycle = engine.lifecycle
        observed: dict[str, EngineLifecycleState] = {}

        async def fake_run(_payload: AppInitializationComplete) -> None:
            observed["during"] = lifecycle.state

        monkeypatch.setattr(lifecycle, "_run_startup", fake_run)

        await lifecycle.on_app_initialization_complete(AppInitializationComplete())

        assert observed["during"] is EngineLifecycleState.STARTING
        assert lifecycle.state is EngineLifecycleState.READY

    @pytest.mark.asyncio
    async def test_is_ready_after_startup_raises(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        lifecycle = engine.lifecycle

        async def boom(_payload: AppInitializationComplete) -> None:
            msg = "boom"
            raise RuntimeError(msg)

        monkeypatch.setattr(lifecycle, "_run_startup", boom)

        with pytest.raises(RuntimeError, match="boom"):
            await lifecycle.on_app_initialization_complete(AppInitializationComplete())

        assert lifecycle.state is EngineLifecycleState.READY

    @pytest.mark.asyncio
    async def test_runs_each_step_in_order_and_announces_ready(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lifecycle = engine.lifecycle
        library_manager = engine.library_manager
        calls: list[str] = []

        async def record_load(_payload: AppInitializationComplete) -> None:
            calls.append("libraries")

        async def record_workers() -> None:
            calls.append("workers")

        def record_secrets() -> None:
            calls.append("secrets")

        async def record_workflows(**_kwargs: object) -> None:
            calls.append("workflows")

        def record_ready(*, is_initial_start: bool) -> None:
            calls.append(f"ready:{is_initial_start}")

        monkeypatch.setattr(library_manager, "load_libraries_for_startup", record_load)
        monkeypatch.setattr(library_manager.workers, "maybe_start_workers_for_existing_session", record_workers)
        monkeypatch.setattr(engine.secrets_manager, "register_all_secrets", record_secrets)
        monkeypatch.setattr(engine.workflow_manager, "refresh_workflow_registry", record_workflows)
        monkeypatch.setattr(lifecycle, "announce_ready", record_ready)

        await lifecycle.on_app_initialization_complete(AppInitializationComplete())

        assert calls == ["libraries", "workers", "secrets", "workflows", "ready:True"]

    @pytest.mark.asyncio
    async def test_skip_library_loading_registers_secrets_and_workflows_only(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lifecycle = engine.lifecycle
        library_manager = engine.library_manager
        load = AsyncMock()
        start_workers = AsyncMock()
        register_secrets = MagicMock()
        refresh_workflows = AsyncMock()
        announce_ready = MagicMock()
        monkeypatch.setattr(library_manager, "load_libraries_for_startup", load)
        monkeypatch.setattr(library_manager.workers, "maybe_start_workers_for_existing_session", start_workers)
        monkeypatch.setattr(engine.secrets_manager, "register_all_secrets", register_secrets)
        monkeypatch.setattr(engine.workflow_manager, "refresh_workflow_registry", refresh_workflows)
        monkeypatch.setattr(lifecycle, "announce_ready", announce_ready)

        await lifecycle.on_app_initialization_complete(
            AppInitializationComplete(skip_library_loading=True, workflows_to_register=["a.py"])
        )

        load.assert_not_awaited()
        start_workers.assert_not_awaited()
        register_secrets.assert_called_once_with()
        refresh_workflows.assert_awaited_once_with(workflows_to_register=["a.py"])
        announce_ready.assert_not_called()
        assert lifecycle.state is EngineLifecycleState.READY


class TestAnnounceReady:
    def test_orchestrator_emits_engine_ready_with_library_statuses(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        statuses = [
            LibraryLoadStatus(
                library_name="Lib", library_version="1.0", library_path="/lib", fitness="GOOD", disabled=False
            )
        ]
        put_event = MagicMock()
        monkeypatch.setattr(engine.event_manager, "put_event", put_event)
        monkeypatch.setattr(engine.library_manager, "collect_library_load_statuses", lambda: statuses)

        engine.lifecycle.announce_ready(is_initial_start=True)

        put_event.assert_called_once()
        event = put_event.call_args.args[0]
        assert isinstance(event, AppEvent)
        assert isinstance(event.payload, EngineReadyEvent)
        assert event.payload.libraries == statuses
        assert event.payload.is_initial_start is True

    def test_worker_does_not_emit(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        put_event = MagicMock()
        monkeypatch.setattr(engine.event_manager, "put_event", put_event)
        monkeypatch.setattr(engine.library_manager, "_is_worker", True)

        engine.lifecycle.announce_ready(is_initial_start=True)

        put_event.assert_not_called()


class TestLibraryManagerDelegation:
    @pytest.mark.asyncio
    async def test_reload_reports_initializing_during_reload(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        library_manager = engine.library_manager
        lifecycle = engine.lifecycle
        lifecycle._state = EngineLifecycleState.READY
        observed: dict[str, EngineLifecycleState] = {}

        async def fake_run(_request: ReloadAllLibrariesRequest) -> ReloadAllLibrariesResultSuccess:
            observed["during"] = lifecycle.state
            return ReloadAllLibrariesResultSuccess(result_details="ok")

        monkeypatch.setattr(library_manager, "_run_reload_libraries", fake_run)

        await library_manager.reload_libraries_request(ReloadAllLibrariesRequest())

        assert observed["during"] is EngineLifecycleState.RELOADING
        assert lifecycle.state is EngineLifecycleState.READY

    @pytest.mark.asyncio
    async def test_reload_restores_ready_on_exception(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        library_manager = engine.library_manager

        async def boom(_request: ReloadAllLibrariesRequest) -> None:
            msg = "boom"
            raise RuntimeError(msg)

        monkeypatch.setattr(library_manager, "_run_reload_libraries", boom)

        with pytest.raises(RuntimeError, match="boom"):
            await library_manager.reload_libraries_request(ReloadAllLibrariesRequest())

        assert engine.lifecycle.state is EngineLifecycleState.READY

    @pytest.mark.asyncio
    async def test_reload_announces_ready_as_not_initial_start(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        library_manager = engine.library_manager
        announce_ready = MagicMock()
        monkeypatch.setattr(library_manager, "load_all_libraries_from_config", AsyncMock(return_value=[]))
        monkeypatch.setattr(library_manager.workers, "maybe_start_workers_for_existing_session", AsyncMock())
        monkeypatch.setattr(engine.lifecycle, "announce_ready", announce_ready)

        result = await library_manager._run_reload_libraries(ReloadAllLibrariesRequest())

        assert isinstance(result, ReloadAllLibrariesResultSuccess)
        announce_ready.assert_called_once_with(is_initial_start=False)

    @pytest.mark.parametrize("state", list(EngineLifecycleState))
    def test_library_manager_is_initializing_delegates(self, engine: Engine, state: EngineLifecycleState) -> None:
        engine.lifecycle._state = state

        assert engine.library_manager.is_initializing() is (state is not EngineLifecycleState.READY)

    @pytest.mark.parametrize("state", list(EngineLifecycleState))
    def test_heartbeat_reads_the_lifecycle(self, engine: Engine, state: EngineLifecycleState) -> None:
        engine.lifecycle._state = state

        result = engine.handle_engine_heartbeat_request(EngineHeartbeatRequest(heartbeat_id="hb"))

        assert isinstance(result, EngineHeartbeatResultSuccess)
        assert result.is_initializing is (state is not EngineLifecycleState.READY)
