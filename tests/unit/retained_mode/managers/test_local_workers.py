"""A worker this engine runs for itself: taken into service by its hello, and gone the moment its channel ends.

The worker is played by the other end of a real channel in this process; its process is a stand-in, since
what is under test is what this engine does with the channel.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any, NamedTuple, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from griptape_nodes.retained_mode.events.app_events import GetEngineVersionRequest, ReportLibraryLoadedResultSuccess
from griptape_nodes.retained_mode.events.base_events import EventRequest
from griptape_nodes.retained_mode.events.worker_events import WorkerGoneError
from griptape_nodes.retained_mode.managers.worker_manager import WorkerManager
from griptape_nodes.utils.version_utils import engine_version
from griptape_nodes.worker.channel import Channel, ChannelListener, connect_from_environment
from griptape_nodes.worker.wire import LibraryLoadReport, WorkerHello

if TYPE_CHECKING:
    from collections.abc import Iterator

_TIMEOUT_S = 10.0
_LIBRARY = "Exec Library"
_DEPENDENCY = "Dependency Library"
_WORKER_ID = "0b6f3c1e-worker"
_GENERATION = 3


class _Pair(NamedTuple):
    parent: Channel
    worker: Channel


@pytest.fixture
def pair() -> Iterator[_Pair]:
    """Two connected channel ends, neither started."""
    listener = ChannelListener()
    accepted: list[Channel] = []
    thread = threading.Thread(target=lambda: accepted.append(listener.accept(_TIMEOUT_S)))
    thread.start()
    worker = connect_from_environment(listener.child_environment())
    thread.join(_TIMEOUT_S)
    listener.close()
    yield _Pair(parent=accepted[0], worker=worker)
    accepted[0].close()
    worker.close()


class _Worker:
    """The worker's end: records what it is sent, and holds every request until told to answer."""

    def __init__(self, channel: Channel) -> None:
        self.notifications: list[dict] = []
        self.requests: list[dict] = []
        self.request_arrived = threading.Event()
        self.answer = threading.Event()
        channel.start(on_request=self._on_request, on_notification=self.notifications.append, on_closed=lambda: None)

    def _on_request(self, body: dict) -> dict:
        self.requests.append(body)
        self.request_arrived.set()
        self.answer.wait(_TIMEOUT_S)
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
    """Wait for state that a task scheduled from the channel's own thread writes."""
    async with asyncio.timeout(_TIMEOUT_S):
        while not predicate():  # noqa: ASYNC110 (nothing here to set an Event from)
            await asyncio.sleep(0.01)


def _hello(**overrides: Any) -> WorkerHello:
    fields: dict[str, Any] = {
        "engine_version": engine_version,
        "library_name": _LIBRARY,
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

    @pytest.mark.asyncio
    async def test_the_general_purpose_worker_is_ready_once_it_has_loaded(self, pair: _Pair) -> None:
        manager = _worker_manager()
        _Worker(pair.worker)
        manager.expect_worker(None)
        worker_id, _ = await _connected(manager, None, pair)

        reply = await manager._accept_hello(worker_id, _hello(library_name=None, libraries=[]))

        assert reply["accepted"] is True
        assert manager.has_settled(None)
        assert manager.worker_unavailable_reason(None) is None


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
