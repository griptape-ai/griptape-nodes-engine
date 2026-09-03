"""Tests for library worker configuration."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from griptape_nodes.node_library.library_registry import Dependencies, LibraryMetadata
from griptape_nodes.retained_mode.events.app_events import LibraryLoadedNotification
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager


def _make_metadata(**kwargs: Any) -> LibraryMetadata:
    return LibraryMetadata(
        author="test",
        description="test library",
        library_version="1.0.0",
        engine_version="1.0.0",
        tags=[],
        **kwargs,
    )


def _make_library_manager() -> LibraryManager:
    return LibraryManager(event_manager=MagicMock(), worker_manager=MagicMock())


class TestLibraryInfoRequiresWorker:
    def test_defaults_to_false(self) -> None:
        info = LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.DISCOVERED,
            fitness=LibraryManager.LibraryFitness.NOT_EVALUATED,
            library_path="/some/path.json",
            is_sandbox=False,
        )

        assert info.requires_worker is False

    def test_can_be_set_true(self) -> None:
        info = LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
            fitness=LibraryManager.LibraryFitness.GOOD,
            library_path="/some/path.json",
            is_sandbox=False,
            requires_worker=True,
        )

        assert info.requires_worker is True


class TestGetWorkerForLibrary:
    def test_returns_none_for_none_library_name(self) -> None:
        mgr = _make_library_manager()

        result = mgr.get_worker_for_library(None)

        assert result is None

    def test_returns_worker_when_registered(self) -> None:
        mgr = _make_library_manager()
        worker_engine_id = "eng-xyz"
        worker_request_topic = "sessions/s/workers/eng-xyz/request"
        lib_info = LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
            fitness=LibraryManager.LibraryFitness.GOOD,
            library_path="/some/path.json",
            is_sandbox=False,
            library_name="my_lib",
            requires_worker=True,
            executes_in_worker=True,
        )

        cast("MagicMock", mgr._worker_manager).get_worker_for_key.return_value = (
            worker_engine_id,
            worker_request_topic,
        )
        mgr._library_file_path_to_info["/some/path.json"] = lib_info
        result = mgr.get_worker_for_library("my_lib")

        assert result == (worker_engine_id, worker_request_topic)

    def test_returns_none_when_no_worker_and_not_required(self) -> None:
        mgr = _make_library_manager()
        lib_info = LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
            fitness=LibraryManager.LibraryFitness.GOOD,
            library_path="/some/path.json",
            is_sandbox=False,
            library_name="my_lib",
            requires_worker=False,
        )

        cast("MagicMock", mgr._worker_manager).get_worker_for_key.return_value = None
        mgr._library_file_path_to_info["/some/path.json"] = lib_info
        result = mgr.get_worker_for_library("my_lib")

        assert result is None

    def test_raises_when_library_requires_worker_but_none_registered(self) -> None:
        mgr = _make_library_manager()
        lib_info = LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
            fitness=LibraryManager.LibraryFitness.GOOD,
            library_path="/some/path.json",
            is_sandbox=False,
            library_name="my_lib",
            requires_worker=True,
            executes_in_worker=True,
        )

        cast("MagicMock", mgr._worker_manager).get_worker_for_key.return_value = None
        mgr._library_file_path_to_info["/some/path.json"] = lib_info

        with pytest.raises(RuntimeError, match="requires a dedicated worker"):
            mgr.get_worker_for_library("my_lib")


class TestOnLibraryLoadedNotification:
    def _make_lib_info(self, library_name: str) -> LibraryManager.LibraryInfo:
        """A legacy worker-mode library awaiting its worker's verdict.

        `requires_worker` is what puts a library in WORKER_PENDING in the first place (see
        `_start_workers`), so a fixture in that state without the flag describes something the
        engine never produces.
        """
        return LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.WORKER_PENDING,
            fitness=LibraryManager.LibraryFitness.NOT_EVALUATED,
            library_path="/some/path.json",
            is_sandbox=False,
            library_name=library_name,
            requires_worker=True,
            executes_in_worker=True,
        )

    def _make_exec_deps_lib_info(self, library_name: str) -> LibraryManager.LibraryInfo:
        """An execution-dependency library that already loaded its real nodes locally."""
        return LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
            fitness=LibraryManager.LibraryFitness.FLAWED,
            library_path="/some/exec-deps.json",
            is_sandbox=False,
            library_name=library_name,
            requires_worker=False,
            executes_in_worker=True,
        )

    @pytest.mark.asyncio
    async def test_updates_fitness_and_lifecycle_to_loaded(self) -> None:
        mgr = _make_library_manager()
        lib_info = self._make_lib_info("my_lib")
        mgr._library_file_path_to_info["/some/path.json"] = lib_info

        await mgr._on_library_loaded_notification(LibraryLoadedNotification(library_name="my_lib", fitness="GOOD"))

        assert lib_info.lifecycle_state == LibraryManager.LibraryLifecycleState.LOADED
        assert lib_info.fitness == LibraryManager.LibraryFitness.GOOD

    @pytest.mark.asyncio
    async def test_a_worker_does_not_overwrite_a_locally_derived_fitness(self) -> None:
        """An exec-deps library's fitness is the orchestrator's own finding, not the worker's.

        It loaded real node classes here, so its FLAWED verdict came from doing that -- an
        edit-time dependency that failed, a node module that would not import. The worker only
        knows whether ITS copy came up. Taking the worker's answer would paint over a broken node
        that is sitting on the canvas, and the reason would not travel with it.
        """
        mgr = _make_library_manager()
        lib_info = self._make_exec_deps_lib_info("exec_deps_lib")
        mgr._library_file_path_to_info["/some/exec-deps.json"] = lib_info

        await mgr._on_library_loaded_notification(
            LibraryLoadedNotification(library_name="exec_deps_lib", fitness="GOOD")
        )

        assert lib_info.fitness == LibraryManager.LibraryFitness.FLAWED
        assert lib_info.lifecycle_state == LibraryManager.LibraryLifecycleState.LOADED

    @pytest.mark.asyncio
    async def test_accepts_flawed_fitness(self) -> None:
        mgr = _make_library_manager()
        lib_info = self._make_lib_info("my_lib")
        mgr._library_file_path_to_info["/some/path.json"] = lib_info

        await mgr._on_library_loaded_notification(
            LibraryLoadedNotification(library_name="my_lib", fitness="FLAWED", problem_details="some issue")
        )

        assert lib_info.lifecycle_state == LibraryManager.LibraryLifecycleState.LOADED
        assert lib_info.fitness == LibraryManager.LibraryFitness.FLAWED

    @pytest.mark.asyncio
    async def test_does_nothing_for_unknown_library(self) -> None:
        mgr = _make_library_manager()

        await mgr._on_library_loaded_notification(LibraryLoadedNotification(library_name="unknown_lib", fitness="GOOD"))


class TestRegisterPreReloadCallback:
    def test_callback_is_appended(self) -> None:
        mgr = _make_library_manager()
        callback = MagicMock()

        mgr.register_pre_reload_callback(callback)

        assert callback in mgr._pre_reload_callbacks

    def test_multiple_callbacks_registered_in_order(self) -> None:
        mgr = _make_library_manager()
        baseline = list(mgr._pre_reload_callbacks)
        first, second = MagicMock(), MagicMock()

        mgr.register_pre_reload_callback(first)
        mgr.register_pre_reload_callback(second)

        assert mgr._pre_reload_callbacks == [*baseline, first, second]


class TestResolveExecutesInWorker:
    """Execution placement is a fact about dependencies, derived not declared."""

    def _metadata(self, *, exec_deps: list[str] | None) -> LibraryMetadata:
        dependencies = None
        if exec_deps is not None:
            dependencies = Dependencies(pip_dependencies=["pillow"], pip_dependencies_exec=exec_deps)
        return LibraryMetadata(
            author="t",
            description="d",
            library_version="1.0.0",
            engine_version="0.0.0",
            tags=[],
            dependencies=dependencies,
        )

    def test_exec_dependencies_require_a_worker(self) -> None:
        result = LibraryManager._resolve_executes_in_worker(
            requires_worker=False, metadata=self._metadata(exec_deps=["torch"])
        )
        assert result is True

    def test_no_dependencies_section_means_no_worker(self) -> None:
        result = LibraryManager._resolve_executes_in_worker(
            requires_worker=False, metadata=self._metadata(exec_deps=None)
        )
        assert result is False

    def test_empty_exec_dependencies_mean_no_worker(self) -> None:
        result = LibraryManager._resolve_executes_in_worker(
            requires_worker=False, metadata=self._metadata(exec_deps=[])
        )
        assert result is False

    def test_legacy_worker_mode_still_executes_in_a_worker(self) -> None:
        """The two reasons are independent: declaring worker mode is sufficient alone."""
        result = LibraryManager._resolve_executes_in_worker(
            requires_worker=True, metadata=self._metadata(exec_deps=None)
        )
        assert result is True


class TestExecuteWaitsForTheWorkerLibraryLoad:
    """Routing must gate on the worker's LibraryLoadedNotification, not on registration.

    A worker registers at process start, BEFORE it has loaded its library -- with eager
    execution-module imports, seconds before. A node routed in that window failed in the
    worker with "Library 'X' not found". The old worker system never had this window:
    stub nodes did not exist until the worker confirmed the load, so nothing could execute.
    """

    def _spawned_info(self, *, loaded: bool) -> LibraryManager.LibraryInfo:
        info = LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
            fitness=LibraryManager.LibraryFitness.GOOD,
            library_path="/some/path.json",
            is_sandbox=False,
            library_name="Lib",
            executes_in_worker=True,
        )
        info.worker_ready = asyncio.Event()
        if loaded:
            info.worker_ready.set()
        return info

    @pytest.mark.asyncio
    async def test_the_wait_releases_when_the_notification_arrives(self) -> None:
        mgr = _make_library_manager()
        info = self._spawned_info(loaded=False)
        mgr._library_file_path_to_info["/some/path.json"] = info
        order: list[str] = []

        async def executes() -> None:
            await mgr.wait_for_worker_library_load("Lib")
            order.append("routed")

        async def worker_finishes_loading() -> None:
            order.append("loaded")
            await mgr._on_library_loaded_notification(LibraryLoadedNotification(library_name="Lib", fitness="GOOD"))

        await asyncio.gather(executes(), worker_finishes_loading())

        assert order == ["loaded", "routed"], "execution routed before the worker reported the library loaded"

    @pytest.mark.asyncio
    async def test_an_already_loaded_library_does_not_wait(self) -> None:
        mgr = _make_library_manager()
        mgr._library_file_path_to_info["/some/path.json"] = self._spawned_info(loaded=True)

        await mgr.wait_for_worker_library_load("Lib")

    @pytest.mark.asyncio
    async def test_a_library_with_no_spawned_worker_does_not_wait(self) -> None:
        mgr = _make_library_manager()
        info = self._spawned_info(loaded=False)
        info.worker_ready = None
        mgr._library_file_path_to_info["/some/path.json"] = info

        await mgr.wait_for_worker_library_load("Lib")

    @pytest.mark.asyncio
    async def test_the_timeout_names_the_library_and_the_ceiling(self) -> None:
        mgr = _make_library_manager()
        mgr._library_file_path_to_info["/some/path.json"] = self._spawned_info(loaded=False)
        mgr._engine = MagicMock()  # type: ignore[assignment]
        mgr._engine.config_manager.get_config_value.return_value = 0.01

        with pytest.raises(RuntimeError, match="Lib"):
            await mgr.wait_for_worker_library_load("Lib")

    @pytest.mark.asyncio
    async def test_an_eviction_during_the_wait_releases_it(self) -> None:
        """The waiter must not hold on for the full grace for a worker that is already gone."""
        mgr = _make_library_manager()
        info = self._spawned_info(loaded=False)
        mgr._library_file_path_to_info["/some/path.json"] = info
        order: list[str] = []

        async def executes() -> None:
            await mgr.wait_for_worker_library_load("Lib")
            order.append("released")

        async def worker_dies() -> None:
            order.append("evicted")
            mgr.on_worker_evicted("worker-1", "Lib")

        await asyncio.gather(executes(), worker_dies())

        assert order == ["evicted", "released"]
        assert info.lifecycle_state is LibraryManager.LibraryLifecycleState.LOADED, (
            "an exec-deps library must stay editable through an eviction"
        )


class TestOnWorkerEvicted:
    """What an evicted worker leaves behind, per library kind.

    An exec-dependencies library loaded real node classes on the orchestrator before its worker
    ever spawned, so losing the worker must cost EXECUTION and nothing else: still LOADED, still
    editable, with a reason the next run can report. A legacy worker-mode library has only stubs
    until its worker confirms, so losing the worker is a load failure. Neither was covered, and
    the difference is the whole point of the split.
    """

    def _info(self, *, requires_worker: bool, lifecycle: LibraryManager.LibraryLifecycleState) -> Any:
        return LibraryManager.LibraryInfo(
            lifecycle_state=lifecycle,
            fitness=LibraryManager.LibraryFitness.GOOD,
            library_path="/some/path.json",
            is_sandbox=False,
            library_name="Lib",
            requires_worker=requires_worker,
            executes_in_worker=True,
        )

    def test_exec_deps_library_stays_loaded_and_records_why(self) -> None:
        manager = _make_library_manager()
        info = self._info(requires_worker=False, lifecycle=LibraryManager.LibraryLifecycleState.LOADED)
        manager._library_file_path_to_info["/some/path.json"] = info

        manager.on_worker_evicted("worker-1", "Lib")

        assert info.lifecycle_state is LibraryManager.LibraryLifecycleState.LOADED
        assert info.fitness is LibraryManager.LibraryFitness.GOOD
        assert info.execution_unavailable_reason is not None
        assert "stopped responding" in info.execution_unavailable_reason

    def test_legacy_worker_pending_library_becomes_failure(self) -> None:
        manager = _make_library_manager()
        info = self._info(requires_worker=True, lifecycle=LibraryManager.LibraryLifecycleState.WORKER_PENDING)
        manager._library_file_path_to_info["/some/path.json"] = info

        manager.on_worker_evicted("worker-1", "Lib")

        assert info.lifecycle_state is LibraryManager.LibraryLifecycleState.FAILURE
        assert info.fitness is LibraryManager.LibraryFitness.UNUSABLE

    def test_unknown_library_name_is_a_no_op(self) -> None:
        manager = _make_library_manager()
        manager.on_worker_evicted("worker-1", "Never Registered")
        manager.on_worker_evicted("worker-1", None)
