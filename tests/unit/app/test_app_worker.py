"""Tests for WorkerManager.

Covers registration, heartbeat, eviction, unregistration, the relay
filter that keeps internal health-check results off the GUI topic, and
the route_to_worker / pending-future mechanism.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.api_client.request_client import _PendingRequest
from griptape_nodes.retained_mode.events import worker_events
from griptape_nodes.retained_mode.events.base_events import EventRequest
from griptape_nodes.retained_mode.events.execution_events import (
    ExecuteNodeRequest,
    ExecuteNodeResultSuccess,
)
from griptape_nodes.retained_mode.managers.worker_manager import WorkerManager, WorkerRegistration
from griptape_nodes.utils.version_utils import engine_version

_SESSION = "sess-abc"
_ENGINE = "eng-xyz"
_WORKER_REQUEST_TOPIC = f"sessions/{_SESSION}/workers/{_ENGINE}/request"
_WORKER_RESPONSE_TOPIC = f"sessions/{_SESSION}/workers/{_ENGINE}/response"


class _FakeRequestClient:
    """Minimal RequestClient stand-in for unit tests.

    Implements only the methods WorkerManager calls so tests remain isolated
    from the real Client/WebSocket machinery.
    """

    def __init__(self) -> None:
        self._pending_requests: dict[str, _PendingRequest] = {}

    async def track_request(
        self, request_id: str, tag: str = "", *, resolve_failures_as_payload: bool = False
    ) -> asyncio.Future:
        future: asyncio.Future = asyncio.Future()
        self._pending_requests[request_id] = _PendingRequest(
            future, tag, resolve_failures_as_payload=resolve_failures_as_payload
        )
        return future

    async def cancel_requests_by_tag(self, tag: str) -> None:
        to_cancel = [rid for rid, entry in self._pending_requests.items() if entry.tag == tag]
        for rid in to_cancel:
            entry = self._pending_requests.pop(rid)
            if not entry.future.done():
                entry.future.cancel()


@pytest.fixture
def worker_manager() -> WorkerManager:
    """Construct a WorkerManager with AsyncMock transport callables for isolated testing."""
    gtn = MagicMock()
    gtn.get_session_id.return_value = _SESSION
    gtn.get_engine_id.return_value = _ENGINE
    # WorkerManager reads several float config values at construction; hand back
    # the declared default so asyncio.wait_for / time arithmetic gets a real number.
    gtn._config_manager.get_config_value.side_effect = lambda _key, default, cast_type=float: cast_type(default)
    # spawn_worker builds the child env from the orchestrator's pre-project environ;
    # hand back a real dict so {**base_environ, ...} doesn't choke on a MagicMock.
    gtn.ProjectManager().get_pre_project_environ.return_value = {}
    wm = WorkerManager(griptape_nodes=gtn, event_manager=MagicMock())
    wm.attach_transport(
        ws_outgoing_queue=asyncio.Queue(),
        send_message=AsyncMock(),
        subscribe_to_topic=AsyncMock(),
        unsubscribe_from_topic=AsyncMock(),
        request_client=_FakeRequestClient(),  # type: ignore[arg-type]
    )
    return wm


def _managed_proc_mock() -> MagicMock:
    """Build a mock worker process whose ``wait()`` is awaitable.

    ``_terminate_managed_process`` awaits ``proc.wait()`` after SIGTERM; a bare
    MagicMock returns a non-awaitable, so stand in an AsyncMock for ``wait``.
    """
    proc = MagicMock()
    proc.wait = AsyncMock()
    return proc


class TestHandleRegisterWorkerRequest:
    @pytest.mark.asyncio
    async def test_adds_worker_to_registered_workers(self, worker_manager: WorkerManager) -> None:
        request = worker_events.RegisterWorkerRequest(worker_engine_id=_ENGINE, engine_version=engine_version)

        await worker_manager.handle_register_worker_request(request)

        assert _ENGINE in worker_manager._workers
        assert worker_manager._workers[_ENGINE].request_topic == _WORKER_REQUEST_TOPIC

    @pytest.mark.asyncio
    async def test_seeds_last_seen_timestamp(self, worker_manager: WorkerManager) -> None:
        request = worker_events.RegisterWorkerRequest(worker_engine_id=_ENGINE, engine_version=engine_version)

        await worker_manager.handle_register_worker_request(request)

        assert _ENGINE in worker_manager._worker_last_seen
        assert worker_manager._worker_last_seen[_ENGINE] > 0

    @pytest.mark.asyncio
    async def test_subscribes_to_worker_response_topic(self, worker_manager: WorkerManager) -> None:
        request = worker_events.RegisterWorkerRequest(worker_engine_id=_ENGINE, engine_version=engine_version)

        await worker_manager.handle_register_worker_request(request)

        worker_manager._tx.subscribe_to_topic.assert_called_once_with(_WORKER_RESPONSE_TOPIC)  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_returns_success_with_engine_id(self, worker_manager: WorkerManager) -> None:
        request = worker_events.RegisterWorkerRequest(worker_engine_id=_ENGINE, engine_version=engine_version)

        result = await worker_manager.handle_register_worker_request(request)

        assert isinstance(result, worker_events.RegisterWorkerResultSuccess)
        assert result.worker_engine_id == _ENGINE


class TestHandleRegisterWorkerRequestEngineVersion:
    @pytest.mark.asyncio
    async def test_rejects_mismatched_engine_version(self, worker_manager: WorkerManager) -> None:
        request = worker_events.RegisterWorkerRequest(worker_engine_id=_ENGINE, engine_version="0.0.0-mismatch")

        result = await worker_manager.handle_register_worker_request(request)

        assert isinstance(result, worker_events.RegisterWorkerResultFailure)

    @pytest.mark.asyncio
    async def test_mismatched_version_does_not_register_worker(self, worker_manager: WorkerManager) -> None:
        request = worker_events.RegisterWorkerRequest(worker_engine_id=_ENGINE, engine_version="0.0.0-mismatch")

        await worker_manager.handle_register_worker_request(request)

        assert _ENGINE not in worker_manager._workers
        assert _ENGINE not in worker_manager._worker_last_seen
        worker_manager._tx.subscribe_to_topic.assert_not_called()  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_mismatched_version_failure_details_identify_both_versions(
        self, worker_manager: WorkerManager
    ) -> None:
        request = worker_events.RegisterWorkerRequest(worker_engine_id=_ENGINE, engine_version="0.0.0-mismatch")

        result = await worker_manager.handle_register_worker_request(request)

        assert isinstance(result, worker_events.RegisterWorkerResultFailure)
        details = str(result.result_details)
        assert "0.0.0-mismatch" in details
        assert engine_version in details


class TestHandleWorkerHeartbeatRequest:
    def test_returns_success_echoing_heartbeat_id(self, worker_manager: WorkerManager) -> None:
        request = worker_events.WorkerHeartbeatRequest(heartbeat_id="hb-001")

        result = worker_manager.handle_worker_heartbeat_request(request)

        assert isinstance(result, worker_events.WorkerHeartbeatResultSuccess)
        assert result.heartbeat_id == "hb-001"

    def test_updates_last_received_timestamp(self, worker_manager: WorkerManager) -> None:
        worker_manager._worker_heartbeat_last_received_at = 0.0
        request = worker_events.WorkerHeartbeatRequest(heartbeat_id="hb-002")

        worker_manager.handle_worker_heartbeat_request(request)

        assert worker_manager._worker_heartbeat_last_received_at > 0.0


class TestWorkerHeartbeatMonitor:
    @pytest.mark.asyncio
    async def test_raises_after_timeout(self, worker_manager: WorkerManager, monkeypatch: pytest.MonkeyPatch) -> None:
        """Monitor raises RuntimeError when no heartbeat arrives within the timeout."""
        monkeypatch.setattr(worker_manager, "heartbeat_interval_s", 0.01)
        monkeypatch.setattr(worker_manager, "heartbeat_timeout_s", 0.0)
        monkeypatch.setattr(worker_manager, "heartbeat_startup_grace_s", 0.0)
        worker_manager._worker_heartbeat_last_received_at = 0.0

        with pytest.raises(RuntimeError, match="Orchestrator heartbeat lost"):
            await worker_manager.worker_heartbeat_monitor()

    @pytest.mark.asyncio
    async def test_does_not_raise_while_heartbeats_arrive(
        self, worker_manager: WorkerManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Monitor does not raise when the timestamp is kept current."""
        monkeypatch.setattr(worker_manager, "heartbeat_interval_s", 0.01)
        monkeypatch.setattr(worker_manager, "heartbeat_timeout_s", 60.0)
        monkeypatch.setattr(worker_manager, "heartbeat_startup_grace_s", 0.0)
        worker_manager._worker_heartbeat_last_received_at = time.monotonic()

        task = asyncio.create_task(worker_manager.worker_heartbeat_monitor())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestHandleUnregisterWorkerRequest:
    @pytest.mark.asyncio
    async def test_removes_worker_from_registered_workers(self, worker_manager: WorkerManager) -> None:
        worker_manager._workers[_ENGINE] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key=None)
        worker_manager._worker_last_seen[_ENGINE] = 999.0

        request = worker_events.UnregisterWorkerRequest(worker_engine_id=_ENGINE)
        await worker_manager.handle_unregister_worker_request(request)

        assert _ENGINE not in worker_manager._workers

    @pytest.mark.asyncio
    async def test_removes_worker_from_last_seen(self, worker_manager: WorkerManager) -> None:
        worker_manager._workers[_ENGINE] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key=None)
        worker_manager._worker_last_seen[_ENGINE] = 999.0

        request = worker_events.UnregisterWorkerRequest(worker_engine_id=_ENGINE)
        await worker_manager.handle_unregister_worker_request(request)

        assert _ENGINE not in worker_manager._worker_last_seen

    @pytest.mark.asyncio
    async def test_unsubscribes_from_worker_response_topic(self, worker_manager: WorkerManager) -> None:
        worker_manager._workers[_ENGINE] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key=None)
        worker_manager._worker_last_seen[_ENGINE] = 999.0

        request = worker_events.UnregisterWorkerRequest(worker_engine_id=_ENGINE)
        await worker_manager.handle_unregister_worker_request(request)

        worker_manager._tx.unsubscribe_from_topic.assert_called_once_with(_WORKER_RESPONSE_TOPIC)  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_returns_success_with_engine_id(self, worker_manager: WorkerManager) -> None:
        worker_manager._workers[_ENGINE] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key=None)
        worker_manager._worker_last_seen[_ENGINE] = 999.0

        request = worker_events.UnregisterWorkerRequest(worker_engine_id=_ENGINE)
        result = await worker_manager.handle_unregister_worker_request(request)

        assert isinstance(result, worker_events.UnregisterWorkerResultSuccess)
        assert result.worker_engine_id == _ENGINE

    @pytest.mark.asyncio
    async def test_tolerates_unknown_worker(self, worker_manager: WorkerManager) -> None:
        """Unregistering a worker that is not in the registry must not raise."""
        request = worker_events.UnregisterWorkerRequest(worker_engine_id="ghost-engine")

        result = await worker_manager.handle_unregister_worker_request(request)

        assert isinstance(result, worker_events.UnregisterWorkerResultSuccess)

    @pytest.mark.asyncio
    async def test_removes_managed_process_for_library(self, worker_manager: WorkerManager) -> None:
        proc = MagicMock()
        worker_manager._workers[_ENGINE] = WorkerRegistration(
            request_topic=_WORKER_REQUEST_TOPIC, worker_key="My Library"
        )
        worker_manager._worker_last_seen[_ENGINE] = 999.0
        worker_manager._managed_worker_processes["My Library"] = proc

        await worker_manager.handle_unregister_worker_request(
            worker_events.UnregisterWorkerRequest(worker_engine_id=_ENGINE)
        )

        assert "My Library" not in worker_manager._managed_worker_processes


class TestRelayWorkerResult:
    @pytest.mark.asyncio
    async def test_heartbeat_success_updates_last_seen(self, worker_manager: WorkerManager) -> None:
        # result_type lives at the outer level — set by BaseEvent.dict(), not inside result{}
        payload = {
            "event_type": "EventResultSuccess",
            "result_type": worker_events.WorkerHeartbeatResultSuccess.__name__,
            "result": {"heartbeat_id": "hb-1"},
            "response_topic": _WORKER_RESPONSE_TOPIC,
        }

        await worker_manager.relay_worker_result(payload)

        worker_manager._tx.send_message.assert_not_called()  # type: ignore[union-attr]
        assert _ENGINE in worker_manager._worker_last_seen

    @pytest.mark.asyncio
    async def test_heartbeat_with_malformed_topic_does_not_crash(self, worker_manager: WorkerManager) -> None:
        payload = {
            "event_type": "EventResultSuccess",
            "result_type": worker_events.WorkerHeartbeatResultSuccess.__name__,
            "result": {"heartbeat_id": "hb-1"},
            "response_topic": "bad/topic",
        }

        await worker_manager.relay_worker_result(payload)

        worker_manager._tx.send_message.assert_not_called()  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_non_heartbeat_result_is_forwarded_to_gui(self, worker_manager: WorkerManager) -> None:
        payload = {
            "event_type": "EventResultSuccess",
            "result_type": "SomeOtherResultSuccess",
            "result": {},
            "response_topic": _WORKER_RESPONSE_TOPIC,
        }

        await worker_manager.relay_worker_result(payload)

        worker_manager._tx.send_message.assert_called_once()  # type: ignore[union-attr]


class TestEvictWorker:
    @pytest.mark.asyncio
    async def test_removes_worker_from_state(self, worker_manager: WorkerManager) -> None:
        worker_manager._workers[_ENGINE] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key=None)
        worker_manager._worker_last_seen[_ENGINE] = 100.0

        await worker_manager.evict_worker(_ENGINE)

        assert _ENGINE not in worker_manager._workers
        assert _ENGINE not in worker_manager._worker_last_seen

    @pytest.mark.asyncio
    async def test_calls_unsubscribe_for_response_topic(self, worker_manager: WorkerManager) -> None:
        worker_manager._workers[_ENGINE] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key=None)
        worker_manager._worker_last_seen[_ENGINE] = 100.0

        await worker_manager.evict_worker(_ENGINE)

        worker_manager._tx.unsubscribe_from_topic.assert_called_once_with(_WORKER_RESPONSE_TOPIC)  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_tolerates_unknown_worker(self, worker_manager: WorkerManager) -> None:
        """Evicting a worker not in the registry must not raise."""
        await worker_manager.evict_worker("ghost-engine")

    @pytest.mark.asyncio
    async def test_terminates_managed_subprocess_for_library(self, worker_manager: WorkerManager) -> None:
        proc = _managed_proc_mock()
        worker_manager._workers[_ENGINE] = WorkerRegistration(
            request_topic=_WORKER_REQUEST_TOPIC, worker_key="My Library"
        )
        worker_manager._worker_last_seen[_ENGINE] = 100.0
        worker_manager._managed_worker_processes["My Library"] = proc

        await worker_manager.evict_worker(_ENGINE)

        proc.terminate.assert_called_once()
        assert "My Library" not in worker_manager._managed_worker_processes


class TestRelayWorkerResultPendingFuture:
    # Note: future resolution for tracked requests is now handled by
    # RequestClient._handle_response, not relay_worker_result. The tests
    # below cover relay_worker_result's remaining responsibilities.

    @pytest.mark.asyncio
    async def test_non_pending_result_still_relays_to_gui(self, worker_manager: WorkerManager) -> None:
        payload = {
            "event_type": "EventResultSuccess",
            "result_type": ExecuteNodeResultSuccess.__name__,
            "result": {"parameter_output_values": {}, "result_details": "ok"},
            "request_id": "unknown-id",
        }

        await worker_manager.relay_worker_result(payload)

        worker_manager._tx.send_message.assert_called_once()  # type: ignore[union-attr]


class TestLibraryWorkerRegistration:
    @pytest.mark.asyncio
    async def test_library_name_stored_on_registration(self, worker_manager: WorkerManager) -> None:
        request = worker_events.RegisterWorkerRequest(
            worker_engine_id=_ENGINE, engine_version=engine_version, library_name="My Library"
        )

        await worker_manager.handle_register_worker_request(request)

        assert worker_manager._workers[_ENGINE].worker_key == "My Library"

    @pytest.mark.asyncio
    async def test_general_worker_has_none_library(self, worker_manager: WorkerManager) -> None:
        request = worker_events.RegisterWorkerRequest(worker_engine_id=_ENGINE, engine_version=engine_version)

        await worker_manager.handle_register_worker_request(request)

        assert worker_manager._workers[_ENGINE].worker_key is None


class TestGetWorkerForKey:
    def test_returns_worker_for_registered_library(self, worker_manager: WorkerManager) -> None:
        worker_manager._workers[_ENGINE] = WorkerRegistration(
            request_topic=_WORKER_REQUEST_TOPIC, worker_key="My Library"
        )

        result = worker_manager.get_worker_for_key("My Library")

        assert result == (_ENGINE, _WORKER_REQUEST_TOPIC)

    def test_returns_none_for_unknown_library(self, worker_manager: WorkerManager) -> None:
        result = worker_manager.get_worker_for_key("Unknown Library")

        assert result is None


class TestLibraryWorkerCleanup:
    def _seed(self, worker_manager: WorkerManager) -> None:
        worker_manager._workers[_ENGINE] = WorkerRegistration(
            request_topic=_WORKER_REQUEST_TOPIC, worker_key="My Library"
        )
        worker_manager._worker_last_seen[_ENGINE] = 999.0

    @pytest.mark.asyncio
    async def test_unregister_removes_worker(self, worker_manager: WorkerManager) -> None:
        self._seed(worker_manager)

        await worker_manager.handle_unregister_worker_request(
            worker_events.UnregisterWorkerRequest(worker_engine_id=_ENGINE)
        )

        assert _ENGINE not in worker_manager._workers

    @pytest.mark.asyncio
    async def test_evict_removes_worker(self, worker_manager: WorkerManager) -> None:
        self._seed(worker_manager)

        await worker_manager.evict_worker(_ENGINE)

        assert _ENGINE not in worker_manager._workers


class TestSpawnWorker:
    @pytest.mark.asyncio
    async def test_duplicate_spawn_is_noop(self, worker_manager: WorkerManager) -> None:
        worker_manager._managed_worker_processes["my-key"] = MagicMock()

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            await worker_manager.spawn_worker(["/usr/bin/gtn", "engine"], "my-key")

        mock_exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_spawns_subprocess_with_provided_args(self, worker_manager: WorkerManager) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        args = ["/usr/local/bin/gtn", "engine", "--session-id", "sess-abc", "--library-name", "My Library"]

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await worker_manager.spawn_worker(args, "My Library")

        mock_exec.assert_called_once_with(*args, env=ANY)
        assert worker_manager._managed_worker_processes["My Library"] is mock_proc


class TestResetWorkers:
    @pytest.mark.asyncio
    async def test_terminates_all_processes(self, worker_manager: WorkerManager) -> None:
        proc_a, proc_b = _managed_proc_mock(), _managed_proc_mock()
        worker_manager._managed_worker_processes["Lib A"] = proc_a
        worker_manager._managed_worker_processes["Lib B"] = proc_b

        await worker_manager.reset_workers()

        proc_a.terminate.assert_called_once()
        proc_b.terminate.assert_called_once()

    @pytest.mark.asyncio
    async def test_clears_all_tracking_state(self, worker_manager: WorkerManager) -> None:
        worker_manager._managed_worker_processes["Lib A"] = _managed_proc_mock()
        worker_manager._workers[_ENGINE] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key="Lib A")
        worker_manager._worker_last_seen[_ENGINE] = 999.0

        await worker_manager.reset_workers()

        assert worker_manager._managed_worker_processes == {}
        assert worker_manager._workers == {}
        assert worker_manager._worker_last_seen == {}

    @pytest.mark.asyncio
    async def test_does_not_clear_session_ready_event(self, worker_manager: WorkerManager) -> None:
        worker_manager._session_ready_event.set()

        await worker_manager.reset_workers()

        assert worker_manager._session_ready_event.is_set()

    @pytest.mark.asyncio
    async def test_tolerates_already_exited_process(self, worker_manager: WorkerManager) -> None:
        proc = MagicMock()
        proc.terminate.side_effect = ProcessLookupError
        worker_manager._managed_worker_processes["Lib A"] = proc

        await worker_manager.reset_workers()

        assert worker_manager._managed_worker_processes == {}

    @pytest.mark.asyncio
    async def test_escalates_to_sigkill_when_terminate_times_out(self, worker_manager: WorkerManager) -> None:
        """A worker that ignores SIGTERM must be SIGKILLed after the grace period."""
        proc = _managed_proc_mock()
        worker_manager._managed_worker_processes["Lib A"] = proc

        def _close_and_timeout(awaitable: object, *_args: object, **_kwargs: object) -> None:
            # Close the proc.wait() coroutine we are bypassing so it is not
            # reported as never-awaited, then simulate the SIGTERM grace expiring.
            awaitable.close()  # type: ignore[attr-defined]
            raise TimeoutError

        with patch("asyncio.wait_for", side_effect=_close_and_timeout):
            await worker_manager.reset_workers()

        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_unsubscribes_response_topic_for_each_registered_worker(self, worker_manager: WorkerManager) -> None:
        worker_manager._workers["eng-1"] = WorkerRegistration(
            request_topic=f"sessions/{_SESSION}/workers/eng-1/request", worker_key=None
        )
        worker_manager._workers["eng-2"] = WorkerRegistration(
            request_topic=f"sessions/{_SESSION}/workers/eng-2/request", worker_key=None
        )

        await worker_manager.reset_workers()

        unsubscribed = {call.args[0] for call in worker_manager._tx.unsubscribe_from_topic.call_args_list}  # type: ignore[union-attr]
        assert f"sessions/{_SESSION}/workers/eng-1/response" in unsubscribed
        assert f"sessions/{_SESSION}/workers/eng-2/response" in unsubscribed


class TestTerminateViaSpawnLoop:
    """Cross-loop worker termination.

    The subprocess binds to its spawning loop, but eviction can run on another
    loop. _terminate_via_spawn_loop must hop termination back to the spawning loop
    so proc.wait() never crosses loops, and fall back to a synchronous signal when
    the spawning loop is gone (shutdown).
    """

    @pytest.mark.asyncio
    async def test_hops_termination_to_spawn_loop(self, worker_manager: WorkerManager) -> None:
        """Termination is hopped onto the spawn loop when it differs from the running loop.

        Avoids awaiting proc.wait() across loops, which would raise.
        """
        spawn_loop = asyncio.new_event_loop()
        ready = threading.Event()

        def _run_spawn_loop() -> None:
            asyncio.set_event_loop(spawn_loop)
            ready.set()
            spawn_loop.run_forever()

        thread = threading.Thread(target=_run_spawn_loop, daemon=True)
        thread.start()
        ready.wait()
        try:
            worker_manager._spawn_loop = spawn_loop
            proc = _managed_proc_mock()
            worker_manager._managed_worker_processes["Lib A"] = proc

            # Runs on the test's loop, which is NOT spawn_loop: the hop must engage.
            await worker_manager.reset_workers()

            proc.terminate.assert_called_once()
            assert worker_manager._managed_worker_processes == {}
        finally:
            spawn_loop.call_soon_threadsafe(spawn_loop.stop)
            thread.join(timeout=5)
            spawn_loop.close()

    @pytest.mark.asyncio
    async def test_falls_back_to_sync_signal_when_spawn_loop_closed(self, worker_manager: WorkerManager) -> None:
        """A closed spawn loop forces the synchronous-signal fallback.

        run_coroutine_threadsafe raises on a closed loop; the dispatcher must
        swallow it and signal the worker synchronously.
        """
        closed_loop = asyncio.new_event_loop()
        closed_loop.close()
        worker_manager._spawn_loop = closed_loop
        proc = _managed_proc_mock()
        worker_manager._managed_worker_processes["Lib A"] = proc

        await worker_manager.reset_workers()

        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()
        assert worker_manager._managed_worker_processes == {}

    @pytest.mark.asyncio
    async def test_falls_back_to_sync_signal_when_hop_times_out(self, worker_manager: WorkerManager) -> None:
        """A hop that is scheduled but never completes triggers the sync fallback.

        Simulates the spawning loop closing after the hop is scheduled but before
        it drains: the hopped termination never finishes, so the evicting loop must
        stop waiting at DEFAULT_TERMINATE_HOP_TIMEOUT_S and signal the worker.
        """
        spawn_loop = asyncio.new_event_loop()
        ready = threading.Event()

        def _run_spawn_loop() -> None:
            asyncio.set_event_loop(spawn_loop)
            ready.set()
            spawn_loop.run_forever()

        thread = threading.Thread(target=_run_spawn_loop, daemon=True)
        thread.start()
        ready.wait()
        try:
            worker_manager._spawn_loop = spawn_loop
            proc = _managed_proc_mock()

            async def _never_exits() -> None:
                await asyncio.Event().wait()

            proc.wait = _never_exits  # the hopped termination hangs forever
            worker_manager._managed_worker_processes["Lib A"] = proc

            with patch.object(WorkerManager, "DEFAULT_TERMINATE_HOP_TIMEOUT_S", 0.1):
                await worker_manager.reset_workers()

            # Fallback signalled the worker without awaiting the stuck exit Future.
            proc.kill.assert_called_once()
            assert worker_manager._managed_worker_processes == {}
        finally:
            spawn_loop.call_soon_threadsafe(spawn_loop.stop)
            thread.join(timeout=5)
            spawn_loop.close()

    @pytest.mark.asyncio
    async def test_cancellation_signals_worker_then_propagates(self, worker_manager: WorkerManager) -> None:
        """A cancelled hop must signal the worker AND re-raise the cancellation.

        Both hop callers run under TaskGroup-driven teardown; swallowing the
        CancelledError would let cleanup resume in a context that was meant to
        stop. The fallback kill still fires, but the cancel must propagate.
        """
        spawn_loop = asyncio.new_event_loop()
        ready = threading.Event()

        def _run_spawn_loop() -> None:
            asyncio.set_event_loop(spawn_loop)
            ready.set()
            spawn_loop.run_forever()

        thread = threading.Thread(target=_run_spawn_loop, daemon=True)
        thread.start()
        ready.wait()
        try:
            worker_manager._spawn_loop = spawn_loop
            proc = _managed_proc_mock()

            async def _never_exits() -> None:
                await asyncio.Event().wait()

            proc.wait = _never_exits  # keep the hop pending so we can cancel it
            worker_manager._managed_worker_processes["Lib A"] = proc

            task = asyncio.ensure_future(worker_manager._terminate_via_spawn_loop("Lib A", proc))
            # Let the hop reach the await before cancelling.
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            proc.kill.assert_called_once()
        finally:
            spawn_loop.call_soon_threadsafe(spawn_loop.stop)
            thread.join(timeout=5)
            spawn_loop.close()


class TestSetSessionReady:
    def test_sets_session_ready_event(self, worker_manager: WorkerManager) -> None:
        assert not worker_manager._session_ready_event.is_set()

        worker_manager.set_session_ready()

        assert worker_manager._session_ready_event.is_set()

    def test_calling_twice_does_not_raise(self, worker_manager: WorkerManager) -> None:
        worker_manager.set_session_ready()
        worker_manager.set_session_ready()


class TestHandleStartWorkerRequest:
    @pytest.mark.asyncio
    async def test_returns_success_immediately(self, worker_manager: WorkerManager) -> None:
        request = worker_events.StartWorkerRequest(library_name="My Library")

        with patch.object(worker_manager, "_spawn_when_session_ready", new=AsyncMock()):
            result = await worker_manager.handle_start_worker_request(request)

        assert isinstance(result, worker_events.StartWorkerResultSuccess)


class TestSpawnWhenSessionReady:
    @pytest.mark.asyncio
    async def test_skips_wait_when_session_already_active(self, worker_manager: WorkerManager) -> None:
        """If a session is already active, spawn proceeds without waiting for the event."""
        worker_manager._griptape_nodes.get_session_id.return_value = _SESSION  # type: ignore[union-attr]

        with patch.object(worker_manager, "spawn_worker", new=AsyncMock()) as mock_spawn:
            await worker_manager._spawn_when_session_ready("My Library")

        mock_spawn.assert_called_once()
        assert not worker_manager._session_ready_event.is_set()

    @pytest.mark.asyncio
    async def test_waits_then_spawns_after_session_ready(self, worker_manager: WorkerManager) -> None:
        """If no session yet, waits for the event and spawns once the session is available."""
        # First call (pre-check) returns None; second call (post-wait) returns session ID.
        worker_manager._griptape_nodes.get_session_id.side_effect = [None, _SESSION]  # type: ignore[union-attr]

        with patch.object(worker_manager, "spawn_worker", new=AsyncMock()) as mock_spawn:
            task = asyncio.create_task(worker_manager._spawn_when_session_ready("My Library"))
            await asyncio.sleep(0)  # let the task start and reach the event wait
            worker_manager._session_ready_event.set()
            await task

        mock_spawn.assert_called_once()

    @pytest.mark.asyncio
    async def test_logs_error_when_no_session_after_event(self, worker_manager: WorkerManager) -> None:
        """If the session event fires but get_session_id still returns None, spawn is not attempted."""
        worker_manager._griptape_nodes.get_session_id.side_effect = [None, None]  # type: ignore[union-attr]
        worker_manager._session_ready_event.set()

        with patch.object(worker_manager, "spawn_worker", new=AsyncMock()) as mock_spawn:
            await worker_manager._spawn_when_session_ready("My Library")

        mock_spawn.assert_not_called()


class TestWorkerEvictedCallbacks:
    @pytest.mark.asyncio
    async def test_callback_called_with_worker_id_and_library_name(self, worker_manager: WorkerManager) -> None:
        callback = MagicMock()
        worker_manager.register_worker_evicted_callback(callback)
        worker_manager._workers[_ENGINE] = WorkerRegistration(
            request_topic=_WORKER_REQUEST_TOPIC, worker_key="My Library"
        )

        await worker_manager.evict_worker(_ENGINE)

        callback.assert_called_once_with(_ENGINE, "My Library")

    @pytest.mark.asyncio
    async def test_exception_in_callback_does_not_prevent_others(self, worker_manager: WorkerManager) -> None:
        first = MagicMock(side_effect=RuntimeError("boom"))
        second = MagicMock()
        worker_manager.register_worker_evicted_callback(first)
        worker_manager.register_worker_evicted_callback(second)
        worker_manager._workers[_ENGINE] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key=None)

        await worker_manager.evict_worker(_ENGINE)

        second.assert_called_once()


class TestRouteToWorker:
    @pytest.mark.asyncio
    async def test_sends_request_to_worker_and_returns_raw_payload(self, worker_manager: WorkerManager) -> None:
        """route_to_worker dispatches to the worker and resolves when RequestClient resolves the future."""
        assert isinstance(worker_manager._tx.request_client, _FakeRequestClient)
        fake_rc = worker_manager._tx.request_client
        event_request = EventRequest(request=ExecuteNodeRequest(node_name="MyNode", parameter_values={"x": 1}))
        expected_payload = {
            "event_type": "EventResultSuccess",
            "result_type": ExecuteNodeResultSuccess.__name__,
            "result": {"parameter_output_values": {"out": 99}, "result_details": "ok"},
            "request_id": "",  # overwritten below
        }

        async def resolve_via_future() -> None:
            # Yield once so route_to_worker can register the future before we resolve it.
            await asyncio.sleep(0)
            request_id = next(iter(fake_rc._pending_requests))
            entry = fake_rc._pending_requests[request_id]
            payload = {**expected_payload, "request_id": request_id}
            entry.future.set_result(payload)

        asyncio.create_task(resolve_via_future())  # noqa: RUF006
        result = await worker_manager.route_to_worker(event_request, _ENGINE, _WORKER_REQUEST_TOPIC)

        assert result["result_type"] == ExecuteNodeResultSuccess.__name__
        assert result["result"]["parameter_output_values"] == {"out": 99}
        worker_manager._tx.send_message.assert_called_once()  # type: ignore[union-attr]


class TestGetTopicsToSubscribe:
    def test_orchestrator_includes_base_request_topic(self, worker_manager: WorkerManager) -> None:
        assert "request" in worker_manager.get_topics_to_subscribe(is_worker=False)

    def test_worker_excludes_base_request_topic(self, worker_manager: WorkerManager) -> None:
        # Workers must NOT subscribe to the broadcast "request" topic — doing so causes
        # them to receive and attempt to handle every MCP/GUI request, racing the orchestrator.
        assert "request" not in worker_manager.get_topics_to_subscribe(is_worker=True)

    def test_always_includes_engine_specific_topic(self, worker_manager: WorkerManager) -> None:
        topics_worker = worker_manager.get_topics_to_subscribe(is_worker=True)
        topics_orch = worker_manager.get_topics_to_subscribe(is_worker=False)

        assert f"engines/{_ENGINE}/request" in topics_worker
        assert f"engines/{_ENGINE}/request" in topics_orch

    def test_worker_mode_includes_per_worker_topic(self, worker_manager: WorkerManager) -> None:
        topics = worker_manager.get_topics_to_subscribe(is_worker=True)

        assert f"sessions/{_SESSION}/workers/{_ENGINE}/request" in topics

    def test_worker_mode_excludes_session_request_topic(self, worker_manager: WorkerManager) -> None:
        topics = worker_manager.get_topics_to_subscribe(is_worker=True)

        assert f"sessions/{_SESSION}/request" not in topics

    def test_orchestrator_mode_includes_session_request_topic(self, worker_manager: WorkerManager) -> None:
        topics = worker_manager.get_topics_to_subscribe(is_worker=False)

        assert f"sessions/{_SESSION}/request" in topics

    def test_orchestrator_mode_excludes_per_worker_topic(self, worker_manager: WorkerManager) -> None:
        topics = worker_manager.get_topics_to_subscribe(is_worker=False)

        assert f"sessions/{_SESSION}/workers/{_ENGINE}/request" not in topics

    def test_orchestrator_mode_no_session_excludes_session_topic(self, worker_manager: WorkerManager) -> None:
        worker_manager._griptape_nodes.get_session_id.return_value = None  # type: ignore[union-attr]

        topics = worker_manager.get_topics_to_subscribe(is_worker=False)

        assert not any("sessions/" in t for t in topics)


class TestForwardEventToWorker:
    @pytest.mark.asyncio
    async def test_sends_message_to_worker_request_topic(self, worker_manager: WorkerManager) -> None:
        from griptape_nodes.retained_mode.events.base_events import EventRequest
        from griptape_nodes.retained_mode.events.execution_events import ExecuteNodeRequest

        event = EventRequest(request=ExecuteNodeRequest(node_name="TestNode", parameter_values={}))

        await worker_manager.forward_event_to_worker(
            event, worker_engine_id=_ENGINE, worker_request_topic=_WORKER_REQUEST_TOPIC
        )

        worker_manager._tx.send_message.assert_called_once()  # type: ignore[union-attr]
        assert worker_manager._tx.send_message.call_args[0][2] == _WORKER_REQUEST_TOPIC  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_sets_response_topic_to_worker_response_topic(self, worker_manager: WorkerManager) -> None:
        from griptape_nodes.retained_mode.events.base_events import EventRequest
        from griptape_nodes.retained_mode.events.execution_events import ExecuteNodeRequest

        event = EventRequest(request=ExecuteNodeRequest(node_name="TestNode", parameter_values={}))

        await worker_manager.forward_event_to_worker(
            event, worker_engine_id=_ENGINE, worker_request_topic=_WORKER_REQUEST_TOPIC
        )

        sent_body = worker_manager._tx.send_message.call_args[0][1]  # type: ignore[union-attr]
        sent_payload = json.loads(sent_body)
        assert sent_payload.get("response_topic") == _WORKER_RESPONSE_TOPIC


class TestDetermineResponseTopic:
    def test_returns_session_response_topic_when_session_active(self, worker_manager: WorkerManager) -> None:
        topic = worker_manager._determine_response_topic()

        assert topic == f"sessions/{_SESSION}/response"

    def test_returns_engine_response_topic_when_no_session(self, worker_manager: WorkerManager) -> None:
        worker_manager._griptape_nodes.get_session_id.return_value = None  # type: ignore[union-attr]

        topic = worker_manager._determine_response_topic()

        assert topic == f"engines/{_ENGINE}/response"

    def test_returns_default_when_no_session_or_engine(self, worker_manager: WorkerManager) -> None:
        worker_manager._griptape_nodes.get_session_id.return_value = None  # type: ignore[union-attr]
        worker_manager._griptape_nodes.get_engine_id.return_value = None  # type: ignore[union-attr]

        topic = worker_manager._determine_response_topic()

        assert topic == "response"


class TestOrchestratorHeartbeatLoop:
    @pytest.mark.asyncio
    async def test_evicts_stale_worker(self, worker_manager: WorkerManager, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(worker_manager, "heartbeat_interval_s", 0.01)
        monkeypatch.setattr(worker_manager, "heartbeat_timeout_s", 0.0)
        worker_manager._workers[_ENGINE] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key=None)
        worker_manager._worker_last_seen[_ENGINE] = 0.0

        task = asyncio.create_task(worker_manager.orchestrator_heartbeat_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert _ENGINE not in worker_manager._workers

    @pytest.mark.asyncio
    async def test_sends_heartbeat_challenge_to_live_worker(
        self, worker_manager: WorkerManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(worker_manager, "heartbeat_interval_s", 0.01)
        monkeypatch.setattr(worker_manager, "heartbeat_timeout_s", 60.0)
        worker_manager._workers[_ENGINE] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key=None)
        worker_manager._worker_last_seen[_ENGINE] = time.monotonic()

        task = asyncio.create_task(worker_manager.orchestrator_heartbeat_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert not worker_manager._tx.ws_outgoing_queue.empty()

    @pytest.mark.asyncio
    async def test_does_not_evict_fresh_worker(
        self, worker_manager: WorkerManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(worker_manager, "heartbeat_interval_s", 0.01)
        monkeypatch.setattr(worker_manager, "heartbeat_timeout_s", 60.0)
        worker_manager._workers[_ENGINE] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key=None)
        worker_manager._worker_last_seen[_ENGINE] = time.monotonic()

        task = asyncio.create_task(worker_manager.orchestrator_heartbeat_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert _ENGINE in worker_manager._workers


class TestBroadcastToWorkers:
    @pytest.mark.asyncio
    async def test_no_workers_is_noop(self, worker_manager: WorkerManager) -> None:
        from griptape_nodes.app.worker_routing import ReloadConfigRequest

        event = EventRequest(request=ReloadConfigRequest())

        await worker_manager.broadcast_to_workers(event)

        worker_manager._tx.send_message.assert_not_called()  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_sends_one_message_per_registered_worker(self, worker_manager: WorkerManager) -> None:
        from griptape_nodes.app.worker_routing import ReloadConfigRequest

        expected_broadcast_count = 2
        worker_a, worker_b = "eng-a", "eng-b"
        worker_manager._workers[worker_a] = WorkerRegistration(
            request_topic=f"sessions/{_SESSION}/workers/{worker_a}/request", worker_key=None
        )
        worker_manager._workers[worker_b] = WorkerRegistration(
            request_topic=f"sessions/{_SESSION}/workers/{worker_b}/request", worker_key=None
        )
        event = EventRequest(request=ReloadConfigRequest())

        await worker_manager.broadcast_to_workers(event)

        assert worker_manager._tx.send_message.call_count == expected_broadcast_count  # type: ignore[union-attr]
        topics = {call.args[2] for call in worker_manager._tx.send_message.call_args_list}  # type: ignore[union-attr]
        assert topics == {
            f"sessions/{_SESSION}/workers/{worker_a}/request",
            f"sessions/{_SESSION}/workers/{worker_b}/request",
        }


class TestScheduleBroadcast:
    @pytest.mark.asyncio
    async def test_fans_out_request_to_each_registered_worker(self, worker_manager: WorkerManager) -> None:
        from griptape_nodes.app.worker_routing import ReloadConfigRequest

        worker_manager._workers[_ENGINE] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key=None)

        worker_manager.schedule_broadcast(ReloadConfigRequest)
        # create_task on the running loop -- let it run.
        await asyncio.sleep(0)

        worker_manager._tx.send_message.assert_called_once()  # type: ignore[union-attr]
        sent_payload = json.loads(worker_manager._tx.send_message.call_args[0][1])  # type: ignore[union-attr]
        assert sent_payload["request_type"] == "ReloadConfigRequest"

    @pytest.mark.asyncio
    async def test_refresh_secrets_payload_round_trips(self, worker_manager: WorkerManager) -> None:
        from griptape_nodes.app.worker_routing import RefreshSecretsRequest

        worker_manager._workers[_ENGINE] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key=None)

        worker_manager.schedule_broadcast(RefreshSecretsRequest)
        await asyncio.sleep(0)

        worker_manager._tx.send_message.assert_called_once()  # type: ignore[union-attr]
        sent_payload = json.loads(worker_manager._tx.send_message.call_args[0][1])  # type: ignore[union-attr]
        assert sent_payload["request_type"] == "RefreshSecretsRequest"

    @pytest.mark.asyncio
    async def test_no_workers_is_noop(self, worker_manager: WorkerManager) -> None:
        from griptape_nodes.app.worker_routing import ReloadConfigRequest

        worker_manager.schedule_broadcast(ReloadConfigRequest)
        await asyncio.sleep(0)

        worker_manager._tx.send_message.assert_not_called()  # type: ignore[union-attr]


class TestWorkerManagerDomainEventListeners:
    """WorkerManager owns the bridge from domain events to worker fan-out.

    ConfigManager and SecretsManager emit ConfigChanged / SecretChanged on
    successful state mutations; WorkerManager translates those into
    ReloadConfigRequest / RefreshSecretsRequest broadcasts. The managers
    themselves know nothing about workers.
    """

    @pytest.fixture
    def worker_manager_with_real_events(self) -> WorkerManager:
        from griptape_nodes.retained_mode.managers.event_manager import EventManager

        gtn = MagicMock()
        gtn.get_session_id.return_value = _SESSION
        gtn.get_engine_id.return_value = _ENGINE
        gtn._config_manager.get_config_value.side_effect = lambda _key, default, cast_type=float: cast_type(default)
        wm = WorkerManager(griptape_nodes=gtn, event_manager=EventManager())
        wm.attach_transport(
            ws_outgoing_queue=asyncio.Queue(),
            send_message=AsyncMock(),
            subscribe_to_topic=AsyncMock(),
            unsubscribe_from_topic=AsyncMock(),
            request_client=_FakeRequestClient(),  # type: ignore[arg-type]
        )
        return wm

    @pytest.mark.asyncio
    async def test_config_changed_event_triggers_reload_config_broadcast(
        self, worker_manager_with_real_events: WorkerManager
    ) -> None:
        from griptape_nodes.retained_mode.events.app_events import ConfigChanged

        wm = worker_manager_with_real_events
        wm._workers[_ENGINE] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key=None)

        wm._event_manager.broadcast_app_event(ConfigChanged(key="x.y", old_value=None, new_value="v"))
        await asyncio.sleep(0)

        wm._tx.send_message.assert_called_once()  # type: ignore[union-attr]
        sent_payload = json.loads(wm._tx.send_message.call_args[0][1])  # type: ignore[union-attr]
        assert sent_payload["request_type"] == "ReloadConfigRequest"

    @pytest.mark.asyncio
    async def test_secret_changed_event_triggers_refresh_secrets_broadcast(
        self, worker_manager_with_real_events: WorkerManager
    ) -> None:
        from griptape_nodes.retained_mode.events.app_events import SecretChanged

        wm = worker_manager_with_real_events
        wm._workers[_ENGINE] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key=None)

        wm._event_manager.broadcast_app_event(SecretChanged(key="MY_KEY"))
        await asyncio.sleep(0)

        wm._tx.send_message.assert_called_once()  # type: ignore[union-attr]
        sent_payload = json.loads(wm._tx.send_message.call_args[0][1])  # type: ignore[union-attr]
        assert sent_payload["request_type"] == "RefreshSecretsRequest"

    @pytest.mark.asyncio
    async def test_no_broadcast_when_there_are_no_registered_workers(
        self, worker_manager_with_real_events: WorkerManager
    ) -> None:
        """On a worker process there are zero registered workers; the listener fires but is a no-op."""
        from griptape_nodes.retained_mode.events.app_events import ConfigChanged

        wm = worker_manager_with_real_events

        wm._event_manager.broadcast_app_event(ConfigChanged(key="x", old_value=None, new_value="v"))
        await asyncio.sleep(0)

        wm._tx.send_message.assert_not_called()  # type: ignore[union-attr]

    def test_broadcast_completes_when_listener_is_dispatched_via_threadrunner(
        self, worker_manager_with_real_events: WorkerManager
    ) -> None:
        """Production path: sync request handler -> sync broadcast_app_event -> ThreadRunner side loop.

        ``EventManager.broadcast_app_event`` detects a running loop and
        runs the listener fan-out on a transient ``ThreadRunner`` side
        loop. If the listener schedules its fan-out via
        ``asyncio.create_task`` and returns, the side loop is torn down
        before the orphan task ever runs and no broadcast actually goes
        out. The listener now awaits the broadcast inline; this test
        confirms the broadcast lands by the time
        ``broadcast_app_event`` returns control to the caller, even
        when the underlying transport ``await`` does not resolve
        synchronously (the production shape -- a real WebSocket send
        yields back to the loop).
        """
        from griptape_nodes.retained_mode.events.app_events import ConfigChanged

        wm = worker_manager_with_real_events
        wm._workers[_ENGINE] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key=None)

        # Force send_message to yield repeatedly before recording the call.
        # An orphan ``asyncio.create_task`` scheduled inside the listener
        # would lose its race with ``ThreadRunner.__exit__`` -> ``loop.stop()``
        # under enough yield points, so the broadcast would silently drop.
        # ``AsyncMock`` returns synchronously which would mask the bug;
        # the production transport awaits real I/O and yields many times.
        send_calls: list[tuple] = []

        async def slow_send(*args: object) -> None:
            for _ in range(50):
                await asyncio.sleep(0)
            send_calls.append(args)

        wm._tx.send_message = slow_send  # type: ignore[union-attr,assignment]

        async def driver() -> None:
            # Inside this coroutine there is a running loop on the main
            # thread. ``broadcast_app_event`` is sync; calling it from
            # here triggers the ThreadRunner side-loop branch -- the same
            # branch that runs in production when a sync request handler
            # (e.g. ``on_handle_set_config_value_request``) calls
            # ``set_config_value`` which calls ``broadcast_app_event``.
            wm._event_manager.broadcast_app_event(ConfigChanged(key="x.y", old_value=None, new_value="v"))

        asyncio.run(driver())

        # By the time broadcast_app_event returns, the listener (and the
        # awaited broadcast inside it) must have completed.
        assert len(send_calls) == 1
        sent_payload = json.loads(send_calls[0][1])
        assert sent_payload["request_type"] == "ReloadConfigRequest"
