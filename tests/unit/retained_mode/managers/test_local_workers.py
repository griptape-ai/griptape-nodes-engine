"""A worker this engine runs for itself: taken into service by its hello, and gone the moment its channel ends.

The worker is played by the other end of a real channel in this process; its process is a stand-in, since
what is under test is what this engine does with the channel.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from typing import TYPE_CHECKING, Any, NamedTuple, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

import griptape_nodes.retained_mode.managers.worker_manager as worker_manager_module
from griptape_nodes.retained_mode.events.app_events import (
    GetEngineVersionRequest,
    ReportLibraryLoadedResultFailure,
    ReportLibraryLoadedResultSuccess,
)
from griptape_nodes.retained_mode.events.base_events import EventRequest
from griptape_nodes.retained_mode.events.worker_events import WorkerGoneError
from griptape_nodes.retained_mode.managers.worker_manager import WorkerManager
from griptape_nodes.utils.version_utils import engine_version
from griptape_nodes.worker.channel import AsyncChannel, PendingWorker, WorkerLink, listen_for_worker
from griptape_nodes.worker.wire import WORKER_HELLO, LibraryLoadReport, WorkerHello, event_body

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_TIMEOUT_S = 10.0
_LIBRARY = "Exec Library"
_DEPENDENCY = "Dependency Library"
_WORKER_ID = "0b6f3c1e-worker"
_GENERATION = 3


class _Pair(NamedTuple):
    parent: AsyncChannel
    worker: WorkerLink


@pytest_asyncio.fixture
async def pair() -> AsyncIterator[_Pair]:
    """Two connected channel ends, neither started."""
    connected = await _connected_pair()
    yield connected
    await _close(connected)


class _Worker:
    """The worker's end: records what it is sent, and holds every request until told to answer."""

    def __init__(self, link: WorkerLink) -> None:
        self.notifications: list[dict] = []
        self.requests: list[dict] = []
        self.request_arrived = threading.Event()
        self.answer = threading.Event()
        self.closed = threading.Event()
        link.start(on_request=self._on_request, on_notification=self.notifications.append, on_closed=self.closed.set)

    async def _on_request(self, body: dict) -> dict:
        self.requests.append(body)
        self.request_arrived.set()
        await asyncio.to_thread(self.answer.wait, _TIMEOUT_S)
        return {}


def _worker_manager() -> WorkerManager:
    engine = MagicMock()
    engine.get_session_id.return_value = None
    engine.config_manager.get_config_value.side_effect = lambda _key, default, cast_type=None: (
        default if cast_type is None else cast_type(default)
    )
    engine.project_manager.committed_project.return_value = ("project-1", _GENERATION)
    engine.library_manager.workers.on_report_library_loaded_request = AsyncMock(
        return_value=ReportLibraryLoadedResultSuccess(result_details="noted")
    )
    return WorkerManager(engine=engine, event_manager=MagicMock())


def _engine(manager: WorkerManager) -> MagicMock:
    return cast("MagicMock", manager.engine)


def _process() -> MagicMock:
    proc = MagicMock(pid=4242, returncode=1)
    proc.wait = AsyncMock(return_value=1)
    return proc


async def _connected(manager: WorkerManager, worker_key: str | None, pair: _Pair) -> tuple[str, MagicMock]:
    """Take `pair.parent` on as `worker_key`'s worker, and return its id and process."""
    proc = _process()
    manager._managed_worker_processes[worker_key] = proc
    await manager._take_on_local_worker(_WORKER_ID, worker_key, proc, pair.parent)
    return _WORKER_ID, proc


async def _until(predicate: Any) -> None:
    """Wait for state that a task the channel starts writes."""
    async with asyncio.timeout(_TIMEOUT_S):
        while not predicate():  # noqa: ASYNC110 (nothing here to set an Event from)
            await asyncio.sleep(0.01)


def _hello(**overrides: Any) -> WorkerHello:
    fields: dict[str, Any] = {
        "engine_version": engine_version,
        "library_names": [_LIBRARY],
        "loaded": True,
        "libraries": [
            LibraryLoadReport(library_name=_DEPENDENCY, fitness="GOOD"),
            LibraryLoadReport(library_name=_LIBRARY, fitness="FLAWED", problem_details="one warning"),
        ],
    }
    fields.update(overrides)
    return WorkerHello(**fields)


class TestHello:
    @pytest.mark.asyncio
    async def test_a_connected_worker_is_told_the_project_and_registered_with_no_topic(self, pair: _Pair) -> None:
        manager = _worker_manager()
        worker = _Worker(pair.worker)

        worker_id, _ = await _connected(manager, _LIBRARY, pair)

        assert manager.get_worker_for_key(_LIBRARY) == (worker_id, "")
        await _until(lambda: worker.notifications)
        activation = worker.notifications[0]["payload"]
        assert activation["request_type"] == "ActivateProjectRequest"
        assert activation["request"]["project_id"] == "project-1"
        assert activation["request"]["generation"] == _GENERATION

    @pytest.mark.asyncio
    async def test_every_library_it_loaded_is_noted_and_its_gate_released(self, pair: _Pair) -> None:
        """The libraries it serves depends on too: a node of one of those may be waiting on its gate."""
        manager = _worker_manager()
        _Worker(pair.worker)
        manager.expect_worker(_LIBRARY)
        worker_id, _ = await _connected(manager, _LIBRARY, pair)

        reply = await manager._accept_hello(worker_id, _hello())

        assert reply["accepted"] is True
        noted = _engine(manager).library_manager.workers.on_report_library_loaded_request
        assert [call.args[0].library_name for call in noted.await_args_list] == [_DEPENDENCY, _LIBRARY]
        assert noted.await_args_list[1].args[0].fitness == "FLAWED"
        assert noted.await_args_list[1].args[0].problem_details == "one warning"

    @pytest.mark.asyncio
    async def test_a_refused_worker_is_cut_off_and_forgotten_even_if_its_process_stays_up(self, pair: _Pair) -> None:
        """Library code can keep a refused worker's process alive, so ending its channel is what ends it."""
        manager = _worker_manager()
        worker = _Worker(pair.worker)
        manager.expect_worker(_LIBRARY)
        await _connected(manager, _LIBRARY, pair)

        reply = await asyncio.to_thread(
            pair.worker.request, event_body(WORKER_HELLO, _hello(engine_version="0.0.1")), _TIMEOUT_S
        )

        assert reply["accepted"] is False
        assert await asyncio.to_thread(worker.closed.wait, _TIMEOUT_S)
        await _until(lambda: manager.get_worker_for_key(_LIBRARY) is None)
        assert _LIBRARY not in manager._managed_worker_processes
        assert "0.0.1" in (manager.worker_unavailable_reason(_LIBRARY) or "")

    @pytest.mark.asyncio
    async def test_a_worker_on_another_engine_version_is_refused_naming_both(self, pair: _Pair) -> None:
        manager = _worker_manager()
        _Worker(pair.worker)
        manager.expect_worker(_LIBRARY)
        worker_id, _ = await _connected(manager, _LIBRARY, pair)

        reply = await manager._accept_hello(worker_id, _hello(engine_version="0.0.1"))

        assert reply["accepted"] is False
        assert reply["details"].startswith(f"Attempted to start the worker process for library '{_LIBRARY}'.")
        reason = manager.worker_unavailable_reason(_LIBRARY)
        assert reason is not None
        assert "0.0.1" in reason
        assert engine_version in reason
        assert manager.has_settled(_LIBRARY)
        _engine(manager).library_manager.workers.on_report_library_loaded_request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_worker_that_could_not_load_its_library_is_refused_with_why(self, pair: _Pair) -> None:
        manager = _worker_manager()
        _Worker(pair.worker)
        manager.expect_worker(_LIBRARY)
        worker_id, _ = await _connected(manager, _LIBRARY, pair)

        reply = await manager._accept_hello(
            worker_id, _hello(loaded=False, libraries=[], failure_reason="the library did not load there.")
        )

        assert reply["accepted"] is False
        assert manager.worker_unavailable_reason(_LIBRARY) == (
            "its worker process could not load it: the library did not load there."
        )


class TestGone:
    @pytest.mark.asyncio
    async def test_a_worker_whose_channel_ends_fails_what_waits_on_it_and_is_stopped(self, pair: _Pair) -> None:
        manager = _worker_manager()
        worker = _Worker(pair.worker)
        worker_id, proc = await _connected(manager, _LIBRARY, pair)
        running = asyncio.create_task(
            manager.route_to_worker(EventRequest(request=GetEngineVersionRequest()), worker_id, "")
        )
        assert await asyncio.to_thread(worker.request_arrived.wait, _TIMEOUT_S)

        pair.worker.close()

        with pytest.raises(WorkerGoneError, match="the worker process running it stopped"):
            await asyncio.wait_for(running, _TIMEOUT_S)
        await _until(lambda: manager.worker_unavailable_reason(_LIBRARY) is not None)
        assert manager.get_worker_for_key(_LIBRARY) is None
        assert _LIBRARY not in manager._managed_worker_processes
        proc.terminate.assert_called_once()
        worker.answer.set()

    @pytest.mark.asyncio
    async def test_a_worker_this_engine_stops_fails_what_waits_on_it_with_why(self, pair: _Pair) -> None:
        manager = _worker_manager()
        worker = _Worker(pair.worker)
        worker_id, _ = await _connected(manager, _LIBRARY, pair)
        running = asyncio.create_task(
            manager.route_to_worker(EventRequest(request=GetEngineVersionRequest()), worker_id, "")
        )
        assert await asyncio.to_thread(worker.request_arrived.wait, _TIMEOUT_S)

        await manager.stop_worker_for_library(_LIBRARY)

        with pytest.raises(WorkerGoneError, match=f"shut down to reset library '{_LIBRARY}'"):
            await asyncio.wait_for(running, _TIMEOUT_S)
        assert manager.get_worker_for_key(_LIBRARY) is None
        worker.answer.set()


class TestRelayedEvents:
    @pytest.mark.asyncio
    async def test_a_workers_app_event_reaches_the_editor_without_this_engine_acting_on_it(self) -> None:
        manager = _worker_manager()
        send_message = AsyncMock()
        manager.attach_transport(
            send_message=send_message,
            subscribe_to_topic=AsyncMock(),
            unsubscribe_from_topic=AsyncMock(),
            request_client=MagicMock(),
        )
        _engine(manager).get_session_id.return_value = "session-1"
        payload = {"event_type": "AppEvent", "payload_type": "CurrentProjectChanged", "payload": {"project_id": "p"}}

        await manager._relay_local_worker_event("AppEvent", payload)

        send_message.assert_awaited_once()
        event_type, _, topic = send_message.await_args_list[0].args
        assert event_type == "app_event"
        assert topic == "sessions/session-1/response"


def _spawning_manager() -> WorkerManager:
    """A manager whose spawns start real processes, in this process's own environment."""
    manager = _worker_manager()
    _engine(manager).project_manager.get_pre_project_environ.return_value = dict(os.environ)
    _engine(manager).library_manager.environment.execution_site_packages.return_value = None
    manager._orchestrator_static_server_base_url = AsyncMock(return_value=None)  # type: ignore[method-assign]
    return manager


def _record_listeners(monkeypatch: pytest.MonkeyPatch) -> list[PendingWorker]:
    """Every listener the manager opens for a worker, so a test can see it stopped listening."""
    opened: list[PendingWorker] = []

    async def listen() -> PendingWorker:
        pending = await listen_for_worker()
        opened.append(pending)
        return pending

    monkeypatch.setattr(worker_manager_module, "listen_for_worker", listen)
    return opened


def _listening(pending: PendingWorker) -> bool:
    return pending._server.is_serving()


@pytest.mark.worker_processes
class TestNeverConnects:
    @pytest.mark.asyncio
    async def test_a_worker_that_exits_before_connecting_is_settled_and_can_be_started_again(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        listeners = _record_listeners(monkeypatch)
        manager = _spawning_manager()
        manager.expect_worker(_LIBRARY)

        await asyncio.wait_for(
            manager._spawn_local_worker([sys.executable, "-c", "raise SystemExit(3)"], _LIBRARY), _TIMEOUT_S
        )

        reason = manager.worker_unavailable_reason(_LIBRARY)
        assert reason is not None
        assert "exit code 3" in reason
        assert not manager.is_worker_running_or_starting(_LIBRARY)

        manager.expect_worker(_LIBRARY)
        await asyncio.wait_for(
            manager._spawn_local_worker([sys.executable, "-c", "raise SystemExit(4)"], _LIBRARY), _TIMEOUT_S
        )

        assert "exit code 4" in (manager.worker_unavailable_reason(_LIBRARY) or "")
        await _until(lambda: not any(_listening(pending) for pending in listeners))

    @pytest.mark.asyncio
    async def test_a_spawn_cancelled_while_it_waits_for_the_worker_stops_waiting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        listeners = _record_listeners(monkeypatch)
        manager = _spawning_manager()
        manager.expect_worker(_LIBRARY)
        spawn = asyncio.create_task(
            manager._spawn_local_worker([sys.executable, "-c", "import time; time.sleep(60)"], _LIBRARY)
        )
        await _until(lambda: _LIBRARY in manager._managed_worker_processes)

        spawn.cancel()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(spawn, _TIMEOUT_S)
        [pending] = listeners
        await _until(lambda: not _listening(pending))
        await manager.reset_workers()


class TestHelloRefusals:
    @pytest.mark.asyncio
    async def test_a_hello_from_a_worker_this_engine_already_stopped_is_refused(self) -> None:
        manager = _worker_manager()

        reply = await manager._accept_hello("a-worker-this-engine-forgot", _hello())

        assert reply["accepted"] is False

    @pytest.mark.asyncio
    async def test_a_worker_that_serves_another_library_is_refused(self, pair: _Pair) -> None:
        manager = _worker_manager()
        _Worker(pair.worker)
        manager.expect_worker(_LIBRARY)
        worker_id, _ = await _connected(manager, _LIBRARY, pair)

        reply = await manager._accept_hello(worker_id, _hello(library_names=["Another Library"]))

        assert reply["accepted"] is False
        assert manager.worker_unavailable_reason(_LIBRARY) == (
            "its worker process reported serving 'Another Library' instead."
        )

    @pytest.mark.asyncio
    async def test_a_library_this_engine_cannot_note_does_not_refuse_the_worker(
        self, pair: _Pair, caplog: pytest.LogCaptureFixture
    ) -> None:
        manager = _worker_manager()
        _engine(manager).library_manager.workers.on_report_library_loaded_request = AsyncMock(
            return_value=ReportLibraryLoadedResultFailure(result_details="unknown library")
        )
        _Worker(pair.worker)
        worker_id, _ = await _connected(manager, _LIBRARY, pair)

        reply = await manager._accept_hello(worker_id, _hello())

        assert reply["accepted"] is True
        assert "unknown library" in caplog.text


class TestStoppingLocalWorkers:
    @pytest.mark.asyncio
    async def test_stopping_one_library_leaves_the_other_running(self) -> None:
        manager = _worker_manager()
        listener_pairs = [await _connected_pair(), await _connected_pair()]
        try:
            for worker_key, pair in zip((_LIBRARY, _DEPENDENCY), listener_pairs, strict=True):
                _Worker(pair.worker)
                proc = _process()
                manager._managed_worker_processes[worker_key] = proc
                await manager._take_on_local_worker(f"worker-{worker_key}", worker_key, proc, pair.parent)

            await manager.stop_worker_for_library(_LIBRARY)

            assert manager.get_worker_for_key(_LIBRARY) is None
            assert manager.get_worker_for_key(_DEPENDENCY) == (f"worker-{_DEPENDENCY}", "")
        finally:
            for pair in listener_pairs:
                await _close(pair)

    @pytest.mark.asyncio
    async def test_the_general_purpose_worker_is_shut_down_with_why(self, pair: _Pair) -> None:
        manager = _worker_manager()
        worker = _Worker(pair.worker)
        worker_id, _ = await _connected(manager, None, pair)
        running = asyncio.create_task(
            manager.route_to_worker(EventRequest(request=GetEngineVersionRequest()), worker_id, "")
        )
        assert await asyncio.to_thread(worker.request_arrived.wait, _TIMEOUT_S)

        await manager.stop_general_purpose_worker("it was restarted.")

        with pytest.raises(WorkerGoneError, match="shut down to restart libraries"):
            await asyncio.wait_for(running, _TIMEOUT_S)
        assert manager.worker_unavailable_reason(None) == "it was restarted."
        worker.answer.set()

    @pytest.mark.asyncio
    async def test_a_send_to_a_worker_that_is_gone_is_dropped(self, pair: _Pair) -> None:
        manager = _worker_manager()
        _Worker(pair.worker)
        worker_id, _ = await _connected(manager, _LIBRARY, pair)
        pair.parent.close()

        await manager.forward_event_to_worker(
            EventRequest(request=GetEngineVersionRequest()), worker_engine_id=worker_id, worker_request_topic=""
        )


class TestRelayedResults:
    @pytest.mark.asyncio
    async def test_a_workers_result_reaches_the_editor_and_needs_an_editor_to_reach(self) -> None:
        manager = _worker_manager()
        payload = {"event_type": "EventResultSuccess", "result_type": "SomeResultSuccess", "result": {}}

        await manager._relay_local_worker_event("EventResult", payload)

        send_message = AsyncMock()
        manager.attach_transport(
            send_message=send_message,
            subscribe_to_topic=AsyncMock(),
            unsubscribe_from_topic=AsyncMock(),
            request_client=MagicMock(),
        )
        await manager._relay_local_worker_event("EventResult", payload)

        send_message.assert_awaited_once()
        assert send_message.await_args_list[0].args[0] == "success_result"


@pytest.mark.worker_processes
class TestConnectTimeout:
    @pytest.mark.asyncio
    async def test_a_worker_that_never_connects_is_stopped_and_says_why(self) -> None:
        manager = _spawning_manager()
        manager.library_load_timeout_s = 0.5
        manager.expect_worker(_LIBRARY)

        await asyncio.wait_for(
            manager._spawn_local_worker([sys.executable, "-c", "import time; time.sleep(60)"], _LIBRARY), _TIMEOUT_S
        )

        assert manager.worker_unavailable_reason(_LIBRARY) == (
            "its worker process did not connect to this engine within 0 seconds."
        )
        assert not manager.is_worker_running_or_starting(_LIBRARY)


async def _connected_pair() -> _Pair:
    pending = await listen_for_worker()
    accepting = asyncio.ensure_future(pending.accept(_TIMEOUT_S))
    worker = await asyncio.to_thread(WorkerLink, pending.child_environment)
    return _Pair(parent=await accepting, worker=worker)


async def _close(pair: _Pair) -> None:
    """Close both ends, and let the engine's end finish reading before this loop ends."""
    pair.parent.close()
    pair.worker.close()
    await asyncio.wait_for(asyncio.gather(*pair.parent._tasks, return_exceptions=True), _TIMEOUT_S)
