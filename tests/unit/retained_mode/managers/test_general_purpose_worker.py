"""Tests for the general-purpose worker: the worker that serves no single library.

It runs the nodes of Isolated Execution groups, so it loads every library -- but it is no library's
worker, so it neither reports their loads nor builds their environments. The orchestrator learns it
is ready from one report of its own.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events import worker_events
from griptape_nodes.retained_mode.events.app_events import (
    AppInitializationComplete,
    ReportGeneralPurposeWorkerReadyRequest,
    ReportGeneralPurposeWorkerReadyResultSuccess,
)
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager
from griptape_nodes.retained_mode.managers.worker_manager import WorkerManager, WorkerRegistration

_SESSION = "sess-abc"
_WORKER = "eng-general"
_WORKER_REQUEST_TOPIC = f"sessions/{_SESSION}/workers/{_WORKER}/request"


def _worker_manager() -> WorkerManager:
    engine = MagicMock()
    engine.get_session_id.return_value = _SESSION
    engine.config_manager.get_config_value.side_effect = lambda _key, default, cast_type=None: (
        default if cast_type is None else cast_type(default)
    )
    engine.project_manager.get_pre_project_environ.return_value = {}
    manager = WorkerManager(engine=engine, event_manager=MagicMock())
    manager.attach_transport(
        send_message=AsyncMock(),
        subscribe_to_topic=AsyncMock(),
        unsubscribe_from_topic=AsyncMock(),
        request_client=MagicMock(fail_requests_by_tag=AsyncMock()),
    )
    manager._orchestrator_static_server_base_url = AsyncMock(return_value=None)  # type: ignore[method-assign]
    return manager


def _managed_proc() -> MagicMock:
    proc = MagicMock()
    proc.wait = AsyncMock()
    return proc


def _library_manager() -> LibraryManager:
    worker_manager = MagicMock()
    worker_manager.worker_unavailable_reason.return_value = None
    manager = LibraryManager(event_manager=MagicMock(), worker_manager=worker_manager)
    engine = MagicMock()
    engine.library_manager = manager
    engine.ahandle_request = AsyncMock()
    manager._engine = engine
    for part in vars(manager).values():
        if isinstance(part, EngineScoped):
            part._engine = engine
    return manager


class TestSpawn:
    @pytest.mark.asyncio
    async def test_starts_without_a_library_or_the_command_prefix(self) -> None:
        """The prefix prepares an environment for one library, and this worker serves none."""
        manager = _worker_manager()
        resolve = MagicMock()

        with (
            patch("asyncio.create_subprocess_exec", return_value=MagicMock()) as create,
            patch("griptape_nodes.retained_mode.managers.worker_manager.resolve_worker_command", resolve),
        ):
            await manager._spawn_general_purpose_worker_when_session_ready()

        assert list(create.call_args.args) == [
            sys.executable,
            "-m",
            "griptape_nodes_app",
            "engine",
            "--session-id",
            _SESSION,
        ]
        resolve.assert_not_called()
        assert None in manager._managed_worker_processes

    @pytest.mark.asyncio
    async def test_gets_no_library_environment(self) -> None:
        manager = _worker_manager()
        environment = cast("MagicMock", manager._engine).library_manager.environment

        with patch("asyncio.create_subprocess_exec", return_value=MagicMock()) as create:
            await manager.spawn_worker(["gtn", "engine"], None)

        environment.execution_site_packages.assert_not_called()
        assert "PYTHONPATH" not in create.call_args.kwargs["env"]

    @pytest.mark.asyncio
    async def test_the_start_request_without_a_library_spawns_it(self) -> None:
        manager = _worker_manager()
        manager._spawn_general_purpose_worker_when_session_ready = AsyncMock()  # type: ignore[method-assign]
        manager._spawn_when_session_ready = AsyncMock()  # type: ignore[method-assign]

        await manager.handle_start_worker_request(worker_events.StartWorkerRequest(library_name=None))
        await asyncio.sleep(0)

        manager._spawn_general_purpose_worker_when_session_ready.assert_awaited_once()
        manager._spawn_when_session_ready.assert_not_awaited()


class TestReadiness:
    def test_the_ready_report_releases_its_gate(self) -> None:
        manager = _worker_manager()
        manager.expect_worker(None)

        result = manager.handle_report_general_purpose_worker_ready_request(ReportGeneralPurposeWorkerReadyRequest())

        assert isinstance(result, ReportGeneralPurposeWorkerReadyResultSuccess)
        assert manager.has_settled(None)
        assert manager.worker_unavailable_reason(None) is None

    def test_a_library_load_does_not_release_it(self) -> None:
        manager = _worker_manager()
        manager.expect_worker(None)

        manager.note_library_loaded("Some Library")

        assert not manager.has_settled(None)


class TestLifecycle:
    """A key of None is a worker like any other: it is terminated, and what waits on it is released."""

    @pytest.mark.asyncio
    async def test_unregister_drops_its_process_and_releases_a_waiter(self) -> None:
        manager = _worker_manager()
        manager.expect_worker(None)
        manager._workers[_WORKER] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key=None)
        manager._managed_worker_processes[None] = _managed_proc()

        await manager.handle_unregister_worker_request(worker_events.UnregisterWorkerRequest(worker_engine_id=_WORKER))

        assert None not in manager._managed_worker_processes
        assert manager.has_settled(None)
        assert manager.worker_unavailable_reason(None) is not None

    @pytest.mark.asyncio
    async def test_evict_terminates_its_process_and_records_why(self) -> None:
        manager = _worker_manager()
        manager.expect_worker(None)
        proc = _managed_proc()
        manager._workers[_WORKER] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key=None)
        manager._managed_worker_processes[None] = proc

        await manager.evict_worker(_WORKER)

        proc.terminate.assert_called_once()
        assert None not in manager._managed_worker_processes
        reason = manager.worker_unavailable_reason(None)
        assert reason is not None
        assert "stopped responding" in reason

    @pytest.mark.asyncio
    async def test_reset_terminates_it(self) -> None:
        manager = _worker_manager()
        proc = _managed_proc()
        manager._managed_worker_processes[None] = proc

        await manager.reset_workers()

        proc.terminate.assert_called_once()


class TestStartRequest:
    """The orchestrator asks for it on a full start of its workers, only while the beta feature is on."""

    async def _start_workers(self, *, beta_enabled: bool = True, only_library_name: str | None = None) -> MagicMock:
        manager = _library_manager()
        manager._worker_manager = _worker_manager()
        with patch("griptape_nodes.retained_mode.managers.library.workers.is_beta_enabled", return_value=beta_enabled):
            await manager.workers._start_workers(only_library_name=only_library_name)
        return cast("MagicMock", manager._engine).ahandle_request

    @staticmethod
    def _requested(ahandle_request: MagicMock) -> bool:
        start = worker_events.StartWorkerRequest(library_name=None)
        return any(call.args == (start,) for call in ahandle_request.await_args_list)

    @pytest.mark.asyncio
    async def test_requested_when_the_beta_feature_is_on(self) -> None:
        assert self._requested(await self._start_workers())

    @pytest.mark.asyncio
    async def test_not_requested_when_the_beta_feature_is_off(self) -> None:
        assert not self._requested(await self._start_workers(beta_enabled=False))

    @pytest.mark.asyncio
    async def test_not_requested_as_one_library_finishes_registering(self) -> None:
        """It loads every library, so starting it before the rest are loaded would have it build their environments."""
        assert not self._requested(await self._start_workers(only_library_name="Some Library"))


class TestStartRequestHandling:
    """WorkerManager declines a second request, as it does for a library's worker."""

    @pytest.mark.asyncio
    async def test_a_request_declares_it_coming(self) -> None:
        manager = _worker_manager()
        manager._spawn_general_purpose_worker_when_session_ready = AsyncMock()  # type: ignore[method-assign]

        await manager.handle_start_worker_request(worker_events.StartWorkerRequest(library_name=None))

        assert not manager.has_settled(None), "routing must wait for the worker it just asked for"
        assert None in manager._requested_spawns

    @pytest.mark.asyncio
    @pytest.mark.parametrize("already", ["registered", "requested", "running"])
    async def test_a_second_request_is_declined(self, already: str) -> None:
        """Declaring it again would reset the gate a run may already be waiting on."""
        manager = _worker_manager()
        spawn = AsyncMock()
        manager._spawn_general_purpose_worker_when_session_ready = spawn  # type: ignore[method-assign]
        if already == "registered":
            manager._workers[_WORKER] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key=None)
        if already == "requested":
            manager._requested_spawns[None] = asyncio.get_running_loop().create_future()  # type: ignore[assignment]
        if already == "running":
            manager._managed_worker_processes[None] = _managed_proc()
        manager.expect_worker = MagicMock()  # type: ignore[method-assign]

        await manager.handle_start_worker_request(worker_events.StartWorkerRequest(library_name=None))
        await asyncio.sleep(0)

        manager.expect_worker.assert_not_called()
        spawn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_requested_again_after_a_reload_kills_it_while_it_loads(self) -> None:
        """The reload kills the worker before its ready report, so its gate is never settled.

        Nothing else settles it either: the registry is already empty when the killed worker
        unregisters. Judged by that gate, the worker would read as still starting and be gone for
        the rest of the session.
        """
        manager = _worker_manager()
        manager._spawn_general_purpose_worker_when_session_ready = AsyncMock()  # type: ignore[method-assign]
        await manager.handle_start_worker_request(worker_events.StartWorkerRequest(library_name=None))
        await asyncio.sleep(0)
        manager._workers[_WORKER] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key=None)
        manager._managed_worker_processes[None] = _managed_proc()

        await manager.reset_workers()
        assert not manager.has_settled(None), "the premise: a reload leaves this gate unsettled"
        manager._spawn_general_purpose_worker_when_session_ready.reset_mock()
        await manager.handle_start_worker_request(worker_events.StartWorkerRequest(library_name=None))
        await asyncio.sleep(0)

        manager._spawn_general_purpose_worker_when_session_ready.assert_awaited_once()


class TestRestartWhenLibrariesChange:
    """The worker loads every library once, so a single library changing mid-session restarts it."""

    async def _running_worker(self) -> tuple[LibraryManager, WorkerManager]:
        manager = _library_manager()
        worker_manager = _worker_manager()
        manager._worker_manager = worker_manager
        # Start requests reach the real handler, which declares the worker coming; only the process is stubbed.
        worker_manager._spawn_general_purpose_worker_when_session_ready = AsyncMock()  # type: ignore[method-assign]

        async def handle(request: Any) -> Any:
            if isinstance(request, worker_events.StartWorkerRequest):
                return await worker_manager.handle_start_worker_request(request)
            return None

        cast("MagicMock", manager._engine).ahandle_request = AsyncMock(side_effect=handle)
        with patch("griptape_nodes.retained_mode.managers.library.workers.is_beta_enabled", return_value=True):
            await manager.workers._start_workers()
        worker_manager._workers[_WORKER] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key=None)
        worker_manager._managed_worker_processes[None] = _managed_proc()
        worker_manager.note_library_loaded(None)
        cast("MagicMock", manager._engine).ahandle_request.reset_mock()
        return manager, worker_manager

    async def _settle(self, manager: LibraryManager) -> None:
        restart = manager.workers._general_purpose_restart
        if restart is not None:
            with patch("griptape_nodes.retained_mode.managers.library.workers.is_beta_enabled", return_value=True):
                await asyncio.wait_for(restart, timeout=5.0)

    def _start_requests(self, manager: LibraryManager) -> int:
        start = worker_events.StartWorkerRequest(library_name=None)
        calls = cast("MagicMock", manager._engine).ahandle_request.await_args_list
        return sum(1 for call in calls if call.args == (start,))

    @pytest.mark.asyncio
    async def test_a_library_change_restarts_the_running_worker(self) -> None:
        manager, worker_manager = await self._running_worker()
        process = worker_manager._managed_worker_processes[None]

        manager.workers.note_library_set_changed()
        await self._settle(manager)

        cast("MagicMock", process).terminate.assert_called_once()
        assert self._start_requests(manager) == 1
        assert not worker_manager.has_settled(None), "routing must wait for the restarted worker to load"

    @pytest.mark.asyncio
    async def test_an_unload_and_a_register_restart_it_once(self) -> None:
        """Updating one library unloads it and registers it again: one change, one restart."""
        manager, _worker_manager = await self._running_worker()

        manager.workers.note_library_set_changed()
        first_restart = manager.workers._general_purpose_restart
        manager.workers.note_library_set_changed()
        assert manager.workers._general_purpose_restart is first_restart, "a full reload notes once per library"
        await self._settle(manager)

        assert self._start_requests(manager) == 1

    @pytest.mark.asyncio
    async def test_waits_for_a_library_load_under_way(self) -> None:
        """A sandbox refresh holds the loading gate closed; restarting inside it would load half a change."""
        manager, _worker_manager = await self._running_worker()
        manager._close_libraries_loading_gate()

        manager.workers.note_library_set_changed()
        await asyncio.sleep(0)
        assert self._start_requests(manager) == 0

        manager._libraries_loading_complete.set()
        await self._settle(manager)
        assert self._start_requests(manager) == 1

    @pytest.mark.asyncio
    async def test_a_full_reload_is_left_to_restart_it_itself(self) -> None:
        """A full reload stops every worker and asks for a new general-purpose one after loading."""
        manager, worker_manager = await self._running_worker()
        manager._close_libraries_loading_gate()

        manager.workers.note_library_set_changed()
        await worker_manager.reset_workers()
        manager._libraries_loading_complete.set()
        await self._settle(manager)

        assert self._start_requests(manager) == 0

    @pytest.mark.asyncio
    async def test_a_worker_started_after_the_change_is_left_alone(self) -> None:
        """It already loaded the changed libraries."""
        manager, worker_manager = await self._running_worker()
        manager._close_libraries_loading_gate()
        manager.workers.note_library_set_changed()
        await worker_manager.reset_workers()
        with patch("griptape_nodes.retained_mode.managers.library.workers.is_beta_enabled", return_value=True):
            await manager.workers._start_workers()
        # The new worker is up before the change is looked at, so only its start count can say it is current.
        worker_manager._workers["eng-new"] = WorkerRegistration(request_topic="sessions/x/new", worker_key=None)
        worker_manager._managed_worker_processes[None] = _managed_proc()
        cast("MagicMock", manager._engine).ahandle_request.reset_mock()

        manager._libraries_loading_complete.set()
        await self._settle(manager)

        assert self._start_requests(manager) == 0

    def test_nothing_happens_without_a_worker(self) -> None:
        manager = _library_manager()
        manager._worker_manager = _worker_manager()

        manager.workers.note_library_set_changed()

        assert manager.workers._general_purpose_restart is None

    def test_a_worker_process_never_restarts_one(self) -> None:
        manager = _library_manager()
        manager._is_worker = True

        manager.workers.note_library_set_changed()

        assert manager.workers._general_purpose_restart is None


def _initializing_library_manager() -> LibraryManager:
    """A LibraryManager whose startup steps around the library load are inert."""
    manager = _library_manager()
    manager.provisioning.ensure_libraries_from_config = AsyncMock()  # type: ignore[method-assign]
    manager.discovery.migrate_old_xdg_library_paths = MagicMock()  # type: ignore[method-assign]
    manager._collect_library_workflow_files = AsyncMock(return_value=[])  # type: ignore[method-assign]
    manager.load_all_libraries_from_config = AsyncMock(return_value=[])  # type: ignore[method-assign]
    manager.workers.report_general_purpose_worker_ready = AsyncMock()  # type: ignore[method-assign]
    return manager


class TestWorkerSideLoading:
    @pytest.mark.asyncio
    async def test_loads_every_library_and_then_reports_ready(self) -> None:
        manager = _initializing_library_manager()

        await manager.load_libraries_for_startup(
            AppInitializationComplete(is_worker=True, is_general_purpose_worker=True)
        )

        cast("AsyncMock", manager.load_all_libraries_from_config).assert_awaited_once_with(target_library_names=None)
        cast("AsyncMock", manager.workers.report_general_purpose_worker_ready).assert_awaited_once()
        assert manager.is_general_purpose_worker

    @pytest.mark.asyncio
    async def test_a_library_worker_still_loads_only_its_library_and_reports_no_readiness(self) -> None:
        manager = _initializing_library_manager()

        await manager.load_libraries_for_startup(
            AppInitializationComplete(is_worker=True, libraries_to_register=["Heavy Library"])
        )

        cast("AsyncMock", manager.load_all_libraries_from_config).assert_awaited_once_with(
            target_library_names=["Heavy Library"]
        )
        cast("AsyncMock", manager.workers.report_general_purpose_worker_ready).assert_not_awaited()
        assert not manager.is_general_purpose_worker

    @pytest.mark.asyncio
    async def test_the_flag_means_nothing_on_the_orchestrator(self) -> None:
        manager = _initializing_library_manager()

        await manager.load_libraries_for_startup(AppInitializationComplete(is_general_purpose_worker=True))

        cast("AsyncMock", manager.workers.report_general_purpose_worker_ready).assert_not_awaited()
        assert not manager.is_general_purpose_worker

    def test_it_is_none_of_its_libraries_worker(self) -> None:
        """So it claims an edit-time environment only where nothing has built one."""
        manager = _library_manager()
        manager._is_worker = True
        manager._is_general_purpose_worker = True

        assert not manager.dependencies._is_one_of_my_target_libraries("Any Library")

    @pytest.mark.asyncio
    async def test_the_ready_report_goes_through_the_load_reporter(self) -> None:
        manager = _library_manager()
        reporter = AsyncMock()
        manager.workers.register_library_load_reporter(reporter)

        await manager.workers.report_general_purpose_worker_ready()

        reporter.assert_awaited_once_with(ReportGeneralPurposeWorkerReadyRequest())
