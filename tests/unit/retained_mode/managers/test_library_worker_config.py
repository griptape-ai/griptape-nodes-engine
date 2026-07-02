"""Tests for library worker configuration."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from griptape_nodes.node_library.library_registry import LibraryMetadata
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
        )

        with patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gtn:
            mock_gtn.WorkerManager.return_value.get_worker_for_key.return_value = (
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

        with patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gtn:
            mock_gtn.WorkerManager.return_value.get_worker_for_key.return_value = None
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
        )

        with patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gtn:
            mock_gtn.WorkerManager.return_value.get_worker_for_key.return_value = None
            mgr._library_file_path_to_info["/some/path.json"] = lib_info

            with pytest.raises(RuntimeError, match="requires a dedicated worker"):
                mgr.get_worker_for_library("my_lib")


class TestOnLibraryLoadedNotification:
    def _make_lib_info(self, library_name: str) -> LibraryManager.LibraryInfo:
        return LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.WORKER_PENDING,
            fitness=LibraryManager.LibraryFitness.NOT_EVALUATED,
            library_path="/some/path.json",
            is_sandbox=False,
            library_name=library_name,
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
