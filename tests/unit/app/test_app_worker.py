"""Tests for WorkerManager.

Covers registration, heartbeat, eviction, unregistration, the relay
filter that keeps internal health-check results off the GUI topic, and
the route_to_worker / pending-future mechanism.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import cast
from unittest.mock import ANY, AsyncMock, MagicMock, call, patch

import pytest

from griptape_nodes.api_client.request_client import _PendingRequest
from griptape_nodes.drivers.storage.local_storage_driver import LocalStorageDriver
from griptape_nodes.retained_mode.beta_features import DISTRIBUTED_EXECUTION
from griptape_nodes.retained_mode.events import converters, worker_events
from griptape_nodes.retained_mode.events.app_events import CurrentProjectChanged
from griptape_nodes.retained_mode.events.base_events import EventRequest, EventResultSuccess
from griptape_nodes.retained_mode.events.execution_events import (
    ExecuteNodeRequest,
    ExecuteNodeResultSuccess,
)
from griptape_nodes.retained_mode.events.parameter_events import (
    GetParameterValueRequest,
    GetParameterValueResultSuccess,
)
from griptape_nodes.retained_mode.managers.machine_links import (
    DISTRIBUTED_EXECUTION_SCHEMA_VERSION,
    MachineNotFoundError,
)
from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY
from griptape_nodes.retained_mode.managers.settings import (
    DISTRIBUTED_EXECUTION_LIBRARIES_KEY,
    DISTRIBUTED_EXECUTION_MAX_WORKERS_KEY,
    DISTRIBUTED_EXECUTION_RELEASE_HOW_KEY,
    DISTRIBUTED_EXECUTION_RELEASE_WHICH_KEY,
)
from griptape_nodes.retained_mode.managers.worker_manager import (
    _STATIC_URL_SETTLE_TIMEOUT_S,
    WorkerManager,
    WorkerRegistration,
    _HostedWorker,
)
from griptape_nodes.servers.static import ORCHESTRATOR_STATIC_SERVER_BASE_URL_ENV
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
    ) -> concurrent.futures.Future:
        future: concurrent.futures.Future = concurrent.futures.Future()
        self._pending_requests[request_id] = _PendingRequest(
            future, tag, resolve_failures_as_payload=resolve_failures_as_payload
        )
        return future

    async def fail_requests_by_tag(self, tag: str, error: Exception) -> None:
        to_fail = [rid for rid, entry in self._pending_requests.items() if entry.tag == tag]
        for rid in to_fail:
            entry = self._pending_requests.pop(rid)
            if not entry.future.done():
                entry.future.set_exception(error)

    def discard_request(self, request_id: str) -> None:
        entry = self._pending_requests.pop(request_id, None)
        if entry is not None and not entry.future.done():
            entry.future.cancel()


@pytest.fixture
def worker_manager() -> WorkerManager:
    """Construct a WorkerManager with AsyncMock transport callables for isolated testing."""
    gtn = MagicMock()
    gtn.get_session_id.return_value = _SESSION
    gtn.get_engine_id.return_value = _ENGINE
    # WorkerManager reads several float config values at construction; hand back
    # the declared default so asyncio.wait_for / time arithmetic gets a real number. A spawn reads
    # worker.command_prefix and library.provisioned_by too, uncast, and gets their defaults.
    gtn.config_manager.get_config_value.side_effect = lambda _key, default=None, cast_type=None, **_kwargs: (
        default if cast_type is None else cast_type(default)
    )
    # spawn_worker builds the child env from the orchestrator's pre-project environ;
    # hand back a real dict so {**base_environ, ...} doesn't choke on a MagicMock.
    gtn.project_manager.get_pre_project_environ.return_value = {}
    # Registration answers from the committed pair; a bare MagicMock cannot be unpacked.
    gtn.project_manager.committed_project.return_value = ("<system-defaults>", 0)
    # A MagicMock reads as a truthy failure reason and would refuse every spawn.
    gtn.library_manager.environment.execution_env_failure_reason.return_value = None
    # Spawn awaits the library's execution environment before starting the process; a bare
    # MagicMock is not awaitable.
    gtn.library_manager.wait_for_execution_env = AsyncMock()
    wm = WorkerManager(engine=gtn, event_manager=MagicMock())
    wm.attach_transport(
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


async def _sweep_until_evicted(worker_manager: WorkerManager) -> None:
    """Run the orchestrator's sweep until it evicts ``_ENGINE``, giving up after 5s.

    Waits for the eviction rather than a fixed window: the sweep ticks in real time, and a garbage
    collection pause or a loaded test runner can hold off the tick that evicts past a short window.
    """
    evicted = asyncio.Event()
    evict_worker = worker_manager.evict_worker

    async def evict_and_signal(worker_engine_id: str) -> None:
        await evict_worker(worker_engine_id)
        evicted.set()

    with patch.object(worker_manager, "evict_worker", new=evict_and_signal):
        task = asyncio.create_task(worker_manager.orchestrator_heartbeat_loop())
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(evicted.wait(), timeout=5.0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class TestHandleRegisterWorkerRequest:
    @pytest.mark.asyncio
    async def test_adds_worker_to_registered_workers(self, worker_manager: WorkerManager) -> None:
        request = worker_events.RegisterWorkerRequest(worker_engine_id=_ENGINE, engine_version=engine_version)

        await worker_manager.handle_register_worker_request(request)

        assert _ENGINE in worker_manager._workers
        assert worker_manager._workers[_ENGINE].request_topic == _WORKER_REQUEST_TOPIC

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


class TestRegistrationActivatesTheProject:
    @pytest.mark.asyncio
    async def test_registering_sends_the_worker_its_first_activation(self, worker_manager: WorkerManager) -> None:
        """One sender and one adoption path, rather than a reply that has to be ordered against a fan-out.

        The reply carries no project, so a switch landing mid-registration is a second message on
        the same channel instead of a second source the worker has to reconcile.
        """
        committed_generation = 7
        worker_manager.engine.project_manager.committed_project.return_value = ("proj-42", committed_generation)  # type: ignore[union-attr]
        request = worker_events.RegisterWorkerRequest(worker_engine_id=_ENGINE, engine_version=engine_version)

        result = await worker_manager.handle_register_worker_request(request)

        assert isinstance(result, worker_events.RegisterWorkerResultSuccess)
        sent = cast("MagicMock", worker_manager._tx.send_message).await_args_list
        activations = [call for call in sent if "ActivateProjectRequest" in str(call)]
        assert len(activations) == 1, "registration must send exactly one activation"
        assert "proj-42" in str(activations[0])
        assert f'"generation": {committed_generation}' in str(activations[0])

    @pytest.mark.asyncio
    async def test_the_activation_is_sent_for_system_defaults_too(self, worker_manager: WorkerManager) -> None:
        """A worker has to be told what it is on even when that is the rest state.

        Skipping it there would leave nothing distinguishing "told" from "not yet told", and the
        worker's wait before library load would never lift.
        """
        worker_manager.engine.project_manager.committed_project.return_value = (SYSTEM_DEFAULTS_KEY, 0)  # type: ignore[union-attr]
        request = worker_events.RegisterWorkerRequest(worker_engine_id=_ENGINE, engine_version=engine_version)

        await worker_manager.handle_register_worker_request(request)

        sent = cast("MagicMock", worker_manager._tx.send_message).await_args_list
        assert any("ActivateProjectRequest" in str(call) for call in sent)


class TestProjectSwitchWaitsForWorkers:
    @pytest.mark.asyncio
    async def test_switch_awaits_each_worker_and_reports_failures(self, worker_manager: WorkerManager) -> None:
        """A switch must not report success while a worker is still on the old workspace.

        The fire-and-forget fan-out returns once messages are sent, so execution dispatched right
        after a switch could reach a worker that had not adopted yet -- which writes files where
        this engine does not read, silently.
        """
        worker_manager._workers = {
            "worker-a": WorkerRegistration(request_topic="t/a", worker_key=None),
            "worker-b": WorkerRegistration(request_topic="t/b", worker_key=None),
        }
        replies = {
            "worker-a": {"result_type": "ActivateProjectResultSuccess", "result": {}},
            "worker-b": {"result_type": "ActivateProjectResultFailure", "result": {"result_details": "unknown id"}},
        }
        awaited: list[str] = []

        async def fake_route(_event: object, worker_engine_id: str, _topic: str) -> dict:
            awaited.append(worker_engine_id)
            return replies[worker_engine_id]

        worker_manager.route_to_worker = fake_route  # type: ignore[method-assign]

        failures = await worker_manager.broadcast_to_workers_awaiting_replies(
            EventRequest(request=worker_events.UnregisterWorkerRequest(worker_engine_id="x"))
        )

        # Every worker was awaited, and the one that refused is named rather than swallowed.
        assert sorted(awaited) == ["worker-a", "worker-b"]
        assert len(failures) == 1
        assert "worker-b" in failures[0]
        assert "unknown id" in failures[0]

    @pytest.mark.asyncio
    async def test_one_unreachable_worker_does_not_strand_the_others(self, worker_manager: WorkerManager) -> None:
        worker_manager._workers = {
            "dead": WorkerRegistration(request_topic="t/dead", worker_key=None),
            "alive": WorkerRegistration(request_topic="t/alive", worker_key=None),
        }

        async def fake_route(_event: object, worker_engine_id: str, _topic: str) -> dict:
            if worker_engine_id == "dead":
                msg = "worker is gone"
                raise RuntimeError(msg)
            return {"result_type": "ActivateProjectResultSuccess", "result": {}}

        worker_manager.route_to_worker = fake_route  # type: ignore[method-assign]

        failures = await worker_manager.broadcast_to_workers_awaiting_replies(
            EventRequest(request=worker_events.UnregisterWorkerRequest(worker_engine_id="x"))
        )

        assert len(failures) == 1
        assert "dead" in failures[0]

    @pytest.mark.asyncio
    async def test_a_slow_worker_does_not_delay_the_others(self, worker_manager: WorkerManager) -> None:
        """Serially, the caller waited out the SUM of every worker's adoption, not the slowest.

        Adoption runs a full library reload, so slow is the normal case rather than the exception,
        and this is on SetCurrentProjectRequest -- a GUI action someone is sitting in front of.
        """
        worker_count = 4
        worker_manager._workers = {
            f"w{i}": WorkerRegistration(request_topic=f"t/w{i}", worker_key=None) for i in range(worker_count)
        }
        peak = 0
        in_flight = 0

        async def fake_route(_event: object, _worker_engine_id: str, _topic: str) -> dict:
            nonlocal peak, in_flight
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.05)
            in_flight -= 1
            return {"result_type": "ActivateProjectResultSuccess", "result": {}}

        worker_manager.route_to_worker = fake_route  # type: ignore[method-assign]

        failures = await worker_manager.broadcast_to_workers_awaiting_replies(
            EventRequest(request=worker_events.UnregisterWorkerRequest(worker_engine_id="x"))
        )

        assert failures == []
        assert peak == worker_count, f"workers were asked one at a time (peak concurrency {peak})"

    @pytest.mark.asyncio
    async def test_a_worker_that_never_answers_is_reported_rather_than_waited_on(
        self, worker_manager: WorkerManager
    ) -> None:
        """A worker that never answers is named, not waited on.

        route_to_worker has no ceiling: it argues liveness from the heartbeat, which evicts a SILENT
        worker and says nothing about one busy adopting. Unbounded, the switch never returns.
        """
        worker_manager.library_load_timeout_s = 0.05
        worker_manager._workers = {"stuck": WorkerRegistration(request_topic="t/stuck", worker_key=None)}

        async def never_answers(_event: object, _worker_engine_id: str, _topic: str) -> dict:
            await asyncio.sleep(30)
            msg = "unreachable"
            raise AssertionError(msg)

        worker_manager.route_to_worker = never_answers  # type: ignore[method-assign]

        failures = await worker_manager.broadcast_to_workers_awaiting_replies(
            EventRequest(request=worker_events.UnregisterWorkerRequest(worker_engine_id="x"))
        )

        assert len(failures) == 1
        assert "no reply within" in failures[0]

    @pytest.mark.asyncio
    async def test_a_result_type_merely_containing_success_is_not_treated_as_one(
        self, worker_manager: WorkerManager
    ) -> None:
        """The suffix is the discriminator, not the substring.

        A substring test read any type merely containing "Success" as one. Result payloads all end
        in ResultSuccess or ResultFailure.
        """
        worker_manager._workers = {"w": WorkerRegistration(request_topic="t/w", worker_key=None)}

        async def odd_reply(_event: object, _worker_engine_id: str, _topic: str) -> dict:
            return {"result_type": "SuccessorLookupResultFailure", "result": {"result_details": "nope"}}

        worker_manager.route_to_worker = odd_reply  # type: ignore[method-assign]

        failures = await worker_manager.broadcast_to_workers_awaiting_replies(
            EventRequest(request=worker_events.UnregisterWorkerRequest(worker_engine_id="x"))
        )

        assert len(failures) == 1
        assert "nope" in failures[0]

    @pytest.mark.asyncio
    async def test_fan_out_reads_the_committed_pair_rather_than_the_event(self, worker_manager: WorkerManager) -> None:
        """The id and the generation describing it must come from one read.

        A worker adopts only strictly newer generations, so a fan-out stamped with the default 0
        makes every switch after the first look stale -- and the worker answers Success, which
        costs the caller the failure log too. Taking the id from the event and the generation from
        state separately can pair an older id with a newer switch's generation.
        """
        worker_manager._workers = {"worker-a": WorkerRegistration(request_topic="t/a", worker_key=None)}
        worker_manager.engine.project_manager.committed_project.return_value = ("proj-9", 4)  # type: ignore[union-attr]
        sent: list[tuple[str, int]] = []

        async def carrying_route(event: EventRequest, _worker_engine_id: str, _topic: str) -> dict:
            sent.append((event.request.project_id, event.request.generation))  # type: ignore[attr-defined]
            return {"result_type": "ActivateProjectResultSuccess", "result": {}}

        worker_manager.route_to_worker = carrying_route  # type: ignore[method-assign]

        # The event's id is deliberately stale here: the fan-out must ignore it.
        await worker_manager._on_current_project_changed(CurrentProjectChanged(project_id="proj-stale"))

        assert sent == [("proj-9", 4)]


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
        monkeypatch.setattr(WorkerManager, "MINIMUM_ORCHESTRATOR_SILENCE_S", 0.0)
        monkeypatch.setattr(worker_manager, "heartbeat_interval_s", 0.01)
        monkeypatch.setattr(worker_manager, "heartbeat_timeout_s", 0.0)
        worker_manager._worker_heartbeat_last_received_at = 0.0

        with pytest.raises(RuntimeError, match="Orchestrator heartbeat lost"):
            await worker_manager.worker_heartbeat_monitor()

    @pytest.mark.asyncio
    async def test_raises_on_the_heartbeat_timeout_not_the_library_load_timeout(
        self, worker_manager: WorkerManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A worker whose orchestrator is gone shuts down within the timeout, not after the load deadline."""
        monkeypatch.setattr(WorkerManager, "MINIMUM_ORCHESTRATOR_SILENCE_S", 0.0)
        monkeypatch.setattr(worker_manager, "heartbeat_interval_s", 0.01)
        monkeypatch.setattr(worker_manager, "heartbeat_timeout_s", 0.05)
        monkeypatch.setattr(worker_manager, "library_load_timeout_s", 600.0)
        worker_manager._worker_heartbeat_last_received_at = 0.0

        with pytest.raises(RuntimeError, match="Orchestrator heartbeat lost"):
            await asyncio.wait_for(worker_manager.worker_heartbeat_monitor(), timeout=5.0)

    def test_tolerates_more_silence_than_an_eviction_costs(
        self, worker_manager: WorkerManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The orchestrator's sweep can run tens of seconds late, and a worker cannot tell that from death."""
        monkeypatch.setattr(worker_manager, "heartbeat_timeout_s", 15.0)

        assert worker_manager.orchestrator_silence_allowed_s == 30.0  # noqa: PLR2004

    def test_never_gives_up_before_the_orchestrator_does(
        self, worker_manager: WorkerManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A configured timeout above the floor has to win, or workers shut down while still wanted."""
        monkeypatch.setattr(worker_manager, "heartbeat_timeout_s", 120.0)

        assert worker_manager.orchestrator_silence_allowed_s == 120.0  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_does_not_raise_before_the_first_heartbeat_is_due(
        self, worker_manager: WorkerManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Silence is counted from when the monitor started, not from a heartbeat that never came."""
        monkeypatch.setattr(worker_manager, "heartbeat_interval_s", 0.01)
        monkeypatch.setattr(worker_manager, "heartbeat_timeout_s", 60.0)
        worker_manager._worker_heartbeat_last_received_at = 0.0

        task = asyncio.create_task(worker_manager.worker_heartbeat_monitor())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_does_not_raise_while_heartbeats_arrive(
        self, worker_manager: WorkerManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Monitor does not raise when the timestamp is kept current."""
        monkeypatch.setattr(worker_manager, "heartbeat_interval_s", 0.01)
        monkeypatch.setattr(worker_manager, "heartbeat_timeout_s", 60.0)
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

        request = worker_events.UnregisterWorkerRequest(worker_engine_id=_ENGINE)
        await worker_manager.handle_unregister_worker_request(request)

        assert _ENGINE not in worker_manager._workers

    @pytest.mark.asyncio
    async def test_unsubscribes_from_worker_response_topic(self, worker_manager: WorkerManager) -> None:
        worker_manager._workers[_ENGINE] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key=None)

        request = worker_events.UnregisterWorkerRequest(worker_engine_id=_ENGINE)
        await worker_manager.handle_unregister_worker_request(request)

        worker_manager._tx.unsubscribe_from_topic.assert_called_once_with(_WORKER_RESPONSE_TOPIC)  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_returns_success_with_engine_id(self, worker_manager: WorkerManager) -> None:
        worker_manager._workers[_ENGINE] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key=None)

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
        worker_manager._managed_worker_processes["My Library"] = proc

        await worker_manager.handle_unregister_worker_request(
            worker_events.UnregisterWorkerRequest(worker_engine_id=_ENGINE)
        )

        assert "My Library" not in worker_manager._managed_worker_processes

    @pytest.mark.asyncio
    async def test_releases_a_library_whose_worker_left_before_loading_it(self, worker_manager: WorkerManager) -> None:
        """A worker that shuts itself down is not coming back, so waiters must not sit out the load timeout."""
        worker_manager.expect_worker("My Library")
        worker_manager._workers[_ENGINE] = WorkerRegistration(
            request_topic=_WORKER_REQUEST_TOPIC, worker_key="My Library"
        )

        await worker_manager.handle_unregister_worker_request(
            worker_events.UnregisterWorkerRequest(worker_engine_id=_ENGINE)
        )

        assert worker_manager.has_settled("My Library"), "a waiter would otherwise block until the load timeout"
        assert worker_manager.worker_unavailable_reason("My Library") == (
            "the worker process that runs it shut down before loading it."
        )

    @pytest.mark.asyncio
    async def test_leaves_a_library_its_worker_already_loaded_alone(self, worker_manager: WorkerManager) -> None:
        """A clean shutdown unregisters through the same path, and must not mark a loaded library unavailable."""
        worker_manager.expect_worker("My Library")
        worker_manager.note_library_loaded("My Library")
        worker_manager._workers[_ENGINE] = WorkerRegistration(
            request_topic=_WORKER_REQUEST_TOPIC, worker_key="My Library"
        )

        await worker_manager.handle_unregister_worker_request(
            worker_events.UnregisterWorkerRequest(worker_engine_id=_ENGINE)
        )

        assert worker_manager.worker_unavailable_reason("My Library") is None


class TestEvictionCountsUnansweredChallenges:
    """A worker is evicted for not answering, never for time passing.

    The sweep shares a loop with library load, so a wall-clock timeout charges the orchestrator's
    own latency to the worker: an 18s boot evicted a healthy worker that had never been challenged.
    """

    @pytest.mark.asyncio
    async def test_a_worker_it_has_not_challenged_enough_survives(
        self, worker_manager: WorkerManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A worker past its challenge allowance is evicted."""
        monkeypatch.setattr(worker_manager, "heartbeat_interval_s", 0.01)
        monkeypatch.setattr(worker_manager, "heartbeat_timeout_s", 0.05)
        worker_manager._workers[_ENGINE] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key=None)

        task = asyncio.create_task(worker_manager.orchestrator_heartbeat_loop())
        await asyncio.sleep(0.025)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert _ENGINE in worker_manager._workers
        assert worker_manager._workers[_ENGINE].unanswered_challenges > 0, "guard: it must have been challenged"

    @pytest.mark.asyncio
    async def test_a_worker_that_stops_answering_is_evicted(
        self, worker_manager: WorkerManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(worker_manager, "heartbeat_interval_s", 0.01)
        monkeypatch.setattr(worker_manager, "heartbeat_timeout_s", 0.01)
        worker_manager._workers[_ENGINE] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key=None)

        await _sweep_until_evicted(worker_manager)

        assert _ENGINE not in worker_manager._workers

    @pytest.mark.asyncio
    async def test_a_heartbeat_reply_clears_the_count(self, worker_manager: WorkerManager) -> None:
        worker_manager._workers[_ENGINE] = WorkerRegistration(
            request_topic=_WORKER_REQUEST_TOPIC, worker_key=None, unanswered_challenges=2
        )
        payload = {
            "event_type": "EventResultSuccess",
            "result_type": worker_events.WorkerHeartbeatResultSuccess.__name__,
            "result": {"heartbeat_id": "hb-1"},
            "response_topic": _WORKER_RESPONSE_TOPIC,
        }

        await worker_manager.relay_worker_result(payload)

        assert worker_manager._workers[_ENGINE].unanswered_challenges == 0

    def test_the_allowance_tracks_the_configured_values(
        self, worker_manager: WorkerManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Derived on read, so tuning either value takes effect without a restart."""
        challenges_in_fifteen_seconds = 3
        monkeypatch.setattr(worker_manager, "heartbeat_interval_s", 5.0)
        monkeypatch.setattr(worker_manager, "heartbeat_timeout_s", 15.0)
        assert worker_manager.unanswered_challenges_allowed == challenges_in_fifteen_seconds

        monkeypatch.setattr(worker_manager, "heartbeat_timeout_s", 0.0)
        assert worker_manager.unanswered_challenges_allowed == 1, "never evict on the first challenge"


class TestGetMessageFilters:
    """Role-keyed alongside `get_topics_to_subscribe`, so neither role can be left without one."""

    @pytest.mark.asyncio
    async def test_a_worker_claims_and_drops_a_result_it_never_asked_for(self, worker_manager: WorkerManager) -> None:
        """Unclaimed, these reach a dispatcher that only understands requests and raise there."""
        message_filter = worker_manager.get_message_filters(is_worker=True)[0]
        message = {"payload": {"event_type": "EventResultSuccess", "response_topic": "sessions/abc/response"}}

        assert await message_filter(message) is True
        worker_manager._tx.send_message.assert_not_called()  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_an_orchestrator_relays_it(self, worker_manager: WorkerManager) -> None:
        message_filter = worker_manager.get_message_filters(is_worker=False)[0]
        message = {
            "payload": {
                "event_type": "EventResultSuccess",
                "result_type": "SomeOtherResultSuccess",
                "result": {},
                "response_topic": _WORKER_RESPONSE_TOPIC,
            }
        }

        assert await message_filter(message) is True
        worker_manager._tx.send_message.assert_called_once()  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_neither_role_claims_a_request(self, worker_manager: WorkerManager) -> None:
        message = {"payload": {"event_type": "EventRequest", "request": {}}}

        for is_worker in (True, False):
            message_filter = worker_manager.get_message_filters(is_worker=is_worker)[0]
            assert await message_filter(message) is False


class TestRelayWorkerResult:
    @pytest.mark.asyncio
    async def test_heartbeat_success_updates_last_seen(self, worker_manager: WorkerManager) -> None:
        payload = {
            "event_type": "EventResultSuccess",
            "result_type": worker_events.WorkerHeartbeatResultSuccess.__name__,
            "result": {"heartbeat_id": "hb-1"},
            "response_topic": _WORKER_RESPONSE_TOPIC,
        }

        await worker_manager.relay_worker_result(payload)

        worker_manager._tx.send_message.assert_not_called()  # type: ignore[union-attr]

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

    @pytest.mark.asyncio
    async def test_values_reach_the_gui_without_type_tags(self, worker_manager: WorkerManager) -> None:
        result = EventResultSuccess(
            request=GetParameterValueRequest(node_name="n", parameter_name="pair"),
            result=GetParameterValueResultSuccess(
                input_types=["any"], type="any", output_type="any", value=(1, "b"), result_details="ok"
            ),
            request_id="r-1",
            response_topic=_WORKER_RESPONSE_TOPIC,
        )

        await worker_manager.relay_worker_result(json.loads(converters.engine.dumps(result)))

        relayed = json.loads(worker_manager._tx.send_message.call_args.args[1])  # type: ignore[union-attr]
        assert relayed["result"]["value"] == [1, "b"]
        assert relayed["request_id"] == "r-1"
        assert relayed["response_topic"] == f"sessions/{_SESSION}/response"


class TestEvictWorker:
    @pytest.mark.asyncio
    async def test_records_why_its_library_can_no_longer_execute(self, worker_manager: WorkerManager) -> None:
        """A recorded reason is what stops the next run blaming a worker that is already gone.

        Nothing respawns an evicted worker, so without it the next run reports that the worker "may
        still be starting up" for the rest of the session, and anything already waiting on it waits
        out the whole startup grace first.
        """
        worker_manager._workers[_ENGINE] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key="Lib")
        worker_manager.expect_worker("Lib")

        await worker_manager.evict_worker(_ENGINE)

        reason = worker_manager.worker_unavailable_reason("Lib")
        assert reason is not None
        assert "stopped responding" in reason
        assert worker_manager.has_settled("Lib"), "a waiter must not hold on for a worker already gone"

    @pytest.mark.asyncio
    async def test_forget_library_drops_the_recorded_reason(self, worker_manager: WorkerManager) -> None:
        """Keyed by a bare name, so it has to be dropped with the library rather than outlive it."""
        worker_manager.note_worker_unavailable("Lib", "the worker stopped responding.")

        worker_manager.forget_library("Lib")

        assert worker_manager.worker_unavailable_reason("Lib") is None

    @pytest.mark.asyncio
    async def test_removes_worker_from_state(self, worker_manager: WorkerManager) -> None:
        worker_manager._workers[_ENGINE] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key=None)

        await worker_manager.evict_worker(_ENGINE)

        assert _ENGINE not in worker_manager._workers

    @pytest.mark.asyncio
    async def test_calls_unsubscribe_for_response_topic(self, worker_manager: WorkerManager) -> None:
        worker_manager._workers[_ENGINE] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key=None)

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
    async def test_concurrent_spawns_for_one_key_fork_once(self, worker_manager: WorkerManager) -> None:
        """Two spawns racing for one library must produce one subprocess.

        The registry entry is written only once the process exists, and the work in between
        suspends, so checking the registry alone lets the second caller through. The loser's
        process would then be untracked, holding its library's dependencies until its own
        heartbeat lapsed.
        """
        worker_manager.engine.library_manager.environment.execution_site_packages.return_value = None  # type: ignore[union-attr]

        async def _suspend_then_answer() -> None:
            # Yields inside the window between the duplicate check and the registry write.
            await asyncio.sleep(0)

        with (
            patch.object(worker_manager, "_orchestrator_static_server_base_url", _suspend_then_answer),
            patch("asyncio.create_subprocess_exec", return_value=_managed_proc_mock()) as mock_exec,
        ):
            await asyncio.gather(
                worker_manager.spawn_worker(["/usr/bin/gtn", "engine"], "My Library"),
                worker_manager.spawn_worker(["/usr/bin/gtn", "engine"], "My Library"),
            )

        mock_exec.assert_called_once()
        assert list(worker_manager._managed_worker_processes) == ["My Library"]

    @pytest.mark.asyncio
    async def test_a_failed_fork_does_not_keep_the_key_claimed(self, worker_manager: WorkerManager) -> None:
        """A spawn that raises must leave the key spawnable.

        The claim outliving a failed fork would silently refuse every later attempt for that
        library, which reads as a worker that never starts and never says why.
        """
        worker_manager.engine.library_manager.environment.execution_site_packages.return_value = None  # type: ignore[union-attr]

        with (
            patch("asyncio.create_subprocess_exec", side_effect=OSError("no interpreter")),
            pytest.raises(OSError, match="no interpreter"),
        ):
            await worker_manager.spawn_worker(["/usr/bin/gtn", "engine"], "My Library")

        assert "My Library" not in worker_manager._spawns_in_flight

        with patch("asyncio.create_subprocess_exec", return_value=_managed_proc_mock()) as mock_exec:
            await worker_manager.spawn_worker(["/usr/bin/gtn", "engine"], "My Library")

        mock_exec.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_spawn_outlived_by_a_reset_does_not_free_the_next_claim(
        self, worker_manager: WorkerManager
    ) -> None:
        """A spawn only releases a claim it still holds.

        A reset drops the claims so a reload can spawn again, which leaves a spawn suspended across
        it resuming to find the key claimed by the reload's spawn. Releasing by name alone frees
        that one, and the library is admitted for a third fork while a spawn is genuinely in flight.
        """
        worker_manager.engine.library_manager.environment.execution_site_packages.return_value = None  # type: ignore[union-attr]
        released = asyncio.Event()

        async def _park_until_released() -> None:
            await released.wait()

        # The stale spawn, parked mid-flight between its claim and the registry write. Its fork
        # fails, so no registry entry is left behind to shadow a wrongly-freed claim.
        with (
            patch.object(worker_manager, "_orchestrator_static_server_base_url", _park_until_released),
            patch("asyncio.create_subprocess_exec", side_effect=OSError("stale spawn died")),
        ):
            stale = asyncio.create_task(worker_manager.spawn_worker(["/usr/bin/gtn", "engine"], "My Library"))
            await asyncio.sleep(0.01)
            assert "My Library" in worker_manager._spawns_in_flight

            await worker_manager.reset_workers()
            reload_claim = asyncio.Event()
            worker_manager._spawns_in_flight["My Library"] = reload_claim

            released.set()
            with pytest.raises(OSError, match="stale spawn died"):
                await stale

        # The stale spawn has finished and must have left the reload's claim standing.
        assert worker_manager._spawns_in_flight.get("My Library") is reload_claim

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            await worker_manager.spawn_worker(["/usr/bin/gtn", "engine"], "My Library")

        mock_exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_spawns_subprocess_with_provided_args(self, worker_manager: WorkerManager) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        args = ["/usr/local/bin/gtn", "engine", "--session-id", "sess-abc", "--library-name", "My Library"]

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await worker_manager.spawn_worker(args, "My Library")

        mock_exec.assert_called_once_with(*args, env=ANY, stdout=sys.stdout, stderr=sys.stderr)
        assert worker_manager._managed_worker_processes["My Library"] is mock_proc

    @pytest.mark.asyncio
    async def test_spawn_hands_worker_the_orchestrator_stdio(self, worker_manager: WorkerManager) -> None:
        # Worker logs must land in the same stream as orchestrator logs. Implicit stdio
        # inheritance is POSIX-only: on Windows a redirected stdout (e.g. the desktop app's
        # pipe) never reaches the child unless passed explicitly, so the worker would log
        # to an invisible console instead.
        with patch("asyncio.create_subprocess_exec", return_value=MagicMock()) as mock_exec:
            await worker_manager.spawn_worker(["/usr/bin/gtn", "engine"], "my-key")

        assert mock_exec.call_args.kwargs["stdout"] is sys.stdout
        assert mock_exec.call_args.kwargs["stderr"] is sys.stderr
        # Worker stdout is a pipe under a GUI-hosted orchestrator; unbuffered output keeps
        # log lines from stalling in Python's block buffer.
        assert mock_exec.call_args.kwargs["env"]["PYTHONUNBUFFERED"] == "1"

    @pytest.mark.asyncio
    async def test_spawn_does_not_pin_the_workspace_into_the_env(self, worker_manager: WorkerManager) -> None:
        """The spawn env must not carry GTN_CONFIG_WORKSPACE_DIRECTORY.

        Workers spawn before any project resolves, so the value at spawn time is the CWD-relative
        placeholder -- and GTN_CONFIG_ outranks the runtime project override, so a worker handed it
        could never follow its orchestrator onto a project's workspace again. The workspace comes
        from the project adopted out of the registration reply instead.
        """
        worker_manager.engine.config_manager.workspace_path = Path("/somewhere/else/GriptapeNodes")  # type: ignore[union-attr]

        with patch("asyncio.create_subprocess_exec", return_value=MagicMock()) as mock_exec:
            await worker_manager.spawn_worker(["/usr/bin/gtn", "engine"], "my-key")

        assert "GTN_CONFIG_WORKSPACE_DIRECTORY" not in mock_exec.call_args.kwargs["env"]

    @pytest.mark.asyncio
    async def test_spawn_env_stamps_orchestrator_engine_id(self, worker_manager: WorkerManager) -> None:
        # The worker must learn its parent orchestrator's id so it can report it in its
        # discovery heartbeat (orchestrator_engine_id), which the GUI uses to nest workers.
        worker_manager.engine.engine_identity_manager.active_engine_id = _ENGINE  # type: ignore[union-attr]

        with patch("asyncio.create_subprocess_exec", return_value=MagicMock()) as mock_exec:
            await worker_manager.spawn_worker(["/usr/bin/gtn", "engine"], "my-key")

        env = mock_exec.call_args.kwargs["env"]
        assert env["GTN_ORCHESTRATOR_ENGINE_ID"] == _ENGINE
        # The worker still gets its own fresh engine id, distinct from the orchestrator's.
        assert env["GTN_ENGINE_ID"] != _ENGINE

    @pytest.mark.asyncio
    async def test_spawn_env_omits_orchestrator_id_when_unknown(self, worker_manager: WorkerManager) -> None:
        # Defensive: never put a None into the subprocess env dict. If the orchestrator has
        # no id yet, the key is simply absent (worker heartbeats as an orchestrator).
        worker_manager.engine.engine_identity_manager.active_engine_id = None  # type: ignore[union-attr]

        with patch("asyncio.create_subprocess_exec", return_value=MagicMock()) as mock_exec:
            await worker_manager.spawn_worker(["/usr/bin/gtn", "engine"], "my-key")

        env = mock_exec.call_args.kwargs["env"]
        assert "GTN_ORCHESTRATOR_ENGINE_ID" not in env


class TestOrchestratorStaticServerBaseUrl:
    """The spawn side of the static-URL handover.

    Resolution and spawn are both listeners on AppInitializationComplete and fan out as unordered
    concurrent tasks, so the URL is awaited rather than sampled. Which wait runs, and which of the
    two warnings a missing URL earns, are what an operator reads when a worker's asset URLs come
    back dead, so both choices are pinned here.
    """

    @pytest.mark.asyncio
    async def test_a_settled_url_is_read_without_a_thread_hop(self, worker_manager: WorkerManager) -> None:
        """The decision is normally already in, and the hop is the path with a cost.

        A blocking wait handed to a thread cannot be cancelled, so taking it when nothing needs it
        parks a default-executor thread that teardown then joins.
        """
        static_files_manager = cast("MagicMock", worker_manager.engine.static_files_manager)
        static_files_manager.static_server_base_url_settled = True
        static_files_manager.wait_for_static_server_base_url.return_value = "http://orchestrator:4242"

        with patch("asyncio.to_thread", new=AsyncMock()) as mock_to_thread:
            result = await worker_manager._orchestrator_static_server_base_url()

        assert result == "http://orchestrator:4242"
        mock_to_thread.assert_not_called()
        static_files_manager.wait_for_static_server_base_url.assert_called_once_with(0)

    @pytest.mark.asyncio
    async def test_an_undecided_url_is_waited_for_off_the_loop(self, worker_manager: WorkerManager) -> None:
        """The blocking wait must not run on the event loop, which is serving everything else."""
        static_files_manager = cast("MagicMock", worker_manager.engine.static_files_manager)
        static_files_manager.static_server_base_url_settled = False
        static_files_manager.wait_for_static_server_base_url.return_value = "http://orchestrator:4242"
        waiting_thread: list[str] = []

        def _record_thread(timeout_s: float) -> str:  # noqa: ARG001
            waiting_thread.append(threading.current_thread().name)
            return "http://orchestrator:4242"

        static_files_manager.wait_for_static_server_base_url.side_effect = _record_thread

        result = await worker_manager._orchestrator_static_server_base_url()

        assert result == "http://orchestrator:4242"
        # The wait blocks whichever thread runs it, so running it here would stall every other
        # spawn and every request this loop is serving for the whole settle timeout.
        assert len(waiting_thread) == 1
        assert waiting_thread[0] != threading.current_thread().name
        static_files_manager.wait_for_static_server_base_url.assert_called_once_with(_STATIC_URL_SETTLE_TIMEOUT_S)

    @pytest.mark.asyncio
    async def test_a_settled_absence_blames_resolution_rather_than_the_wait(
        self, worker_manager: WorkerManager, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Initialization deciding there is no server settles in microseconds.

        Blaming the settle timeout for it points an operator at slow startup when the real lead is
        an earlier resolution failure, which under local storage is the only way to reach here.
        """
        static_files_manager = cast("MagicMock", worker_manager.engine.static_files_manager)
        static_files_manager.static_server_base_url_settled = True
        static_files_manager.wait_for_static_server_base_url.return_value = None
        static_files_manager.storage_driver = MagicMock(spec=LocalStorageDriver)

        with caplog.at_level(logging.WARNING):
            result = await worker_manager._orchestrator_static_server_base_url()

        assert result is None
        assert "Check for an earlier failure resolving the static server" in caplog.text

    @pytest.mark.asyncio
    async def test_a_url_that_never_arrives_blames_the_wait(
        self, worker_manager: WorkerManager, caplog: pytest.LogCaptureFixture
    ) -> None:
        static_files_manager = cast("MagicMock", worker_manager.engine.static_files_manager)
        static_files_manager.static_server_base_url_settled = False
        static_files_manager.wait_for_static_server_base_url.return_value = None
        static_files_manager.storage_driver = MagicMock(spec=LocalStorageDriver)

        with caplog.at_level(logging.WARNING):
            result = await worker_manager._orchestrator_static_server_base_url()

        assert result is None
        assert "never decided" in caplog.text

    @pytest.mark.asyncio
    async def test_a_cloud_backend_without_a_url_says_nothing(
        self, worker_manager: WorkerManager, caplog: pytest.LogCaptureFixture
    ) -> None:
        """On a cloud backend a worker's URLs come from the same bucket and outlive it.

        There is nothing to warn about, and warning anyway trains people to ignore the case where
        the URLs really do die with the worker.
        """
        static_files_manager = cast("MagicMock", worker_manager.engine.static_files_manager)
        static_files_manager.static_server_base_url_settled = True
        static_files_manager.wait_for_static_server_base_url.return_value = None
        static_files_manager.storage_driver = MagicMock()

        with caplog.at_level(logging.WARNING):
            result = await worker_manager._orchestrator_static_server_base_url()

        assert result is None
        assert caplog.records == []


class TestResetWorkers:
    @pytest.mark.asyncio
    async def test_a_claim_does_not_outlive_the_reset(self, worker_manager: WorkerManager) -> None:
        """A reload resets and then spawns again, so a surviving claim would refuse its own spawn.

        The refusal records nothing -- a worker is normally on its way when a key is claimed -- so
        the next run would wait out the whole startup grace and then blame the library load.
        """
        worker_manager.engine.library_manager.environment.execution_site_packages.return_value = None  # type: ignore[union-attr]
        worker_manager._spawns_in_flight["My Library"] = asyncio.Event()

        await worker_manager.reset_workers()

        with patch("asyncio.create_subprocess_exec", return_value=_managed_proc_mock()) as mock_exec:
            await worker_manager.spawn_worker(["/usr/bin/gtn", "engine"], "My Library")

        mock_exec.assert_called_once()

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

        await worker_manager.reset_workers()

        assert worker_manager._managed_worker_processes == {}
        assert worker_manager._workers == {}

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


class TestStopWorkerForLibrary:
    """Stopping the worker of one library, as a library reset does before deleting its environment."""

    @pytest.mark.asyncio
    async def test_terminates_only_the_process_of_that_library(self, worker_manager: WorkerManager) -> None:
        proc_a, proc_b = _managed_proc_mock(), _managed_proc_mock()
        worker_manager._managed_worker_processes["Lib A"] = proc_a
        worker_manager._managed_worker_processes["Lib B"] = proc_b

        await worker_manager.stop_worker_for_library("Lib A")

        proc_a.terminate.assert_called_once()
        proc_b.terminate.assert_not_called()
        assert list(worker_manager._managed_worker_processes) == ["Lib B"]

    @pytest.mark.asyncio
    async def test_forgets_only_the_registration_of_that_library(self, worker_manager: WorkerManager) -> None:
        worker_manager._workers["eng-a"] = WorkerRegistration(
            request_topic=f"sessions/{_SESSION}/workers/eng-a/request", worker_key="Lib A"
        )
        worker_manager._workers["eng-b"] = WorkerRegistration(
            request_topic=f"sessions/{_SESSION}/workers/eng-b/request", worker_key="Lib B"
        )

        await worker_manager.stop_worker_for_library("Lib A")

        assert list(worker_manager._workers) == ["eng-b"]
        unsubscribed = [call.args[0] for call in worker_manager._tx.unsubscribe_from_topic.call_args_list]  # type: ignore[union-attr]
        assert unsubscribed == [f"sessions/{_SESSION}/workers/eng-a/response"]

    @pytest.mark.asyncio
    async def test_waits_out_a_spawn_in_flight_and_stops_what_it_started(self, worker_manager: WorkerManager) -> None:
        """A spawn parked between its claim and the registry write must not leave a live worker behind.

        Without the wait, the stop would find nothing to terminate, and the spawn would then resume and
        register a worker importing from the environment the caller is about to delete.
        """
        worker_manager.engine.library_manager.environment.execution_site_packages.return_value = None  # type: ignore[union-attr]
        released = asyncio.Event()

        async def park_until_released() -> None:
            await released.wait()

        proc = _managed_proc_mock()
        with (
            patch.object(worker_manager, "_orchestrator_static_server_base_url", park_until_released),
            patch("asyncio.create_subprocess_exec", return_value=proc),
        ):
            spawn = asyncio.create_task(worker_manager.spawn_worker(["/usr/bin/gtn", "engine"], "Lib A"))
            await asyncio.sleep(0.01)
            stop = asyncio.create_task(worker_manager.stop_worker_for_library("Lib A"))
            await asyncio.sleep(0.01)
            assert not stop.done()

            released.set()
            await spawn
            await stop

        proc.terminate.assert_called_once()
        assert "Lib A" not in worker_manager._managed_worker_processes
        assert "Lib A" not in worker_manager._spawns_in_flight

    @pytest.mark.asyncio
    async def test_settles_requests_awaiting_that_worker(self, worker_manager: WorkerManager) -> None:
        """A node running in the worker fails with the reason instead of waiting on a process that is gone."""
        assert isinstance(worker_manager._tx.request_client, _FakeRequestClient)
        worker_manager._workers = {_ENGINE: WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key="Lib")}
        event_request = EventRequest(request=ExecuteNodeRequest(node_name="MyNode", parameter_values={}))

        async def stop_mid_flight() -> None:
            await asyncio.sleep(0)
            await worker_manager.stop_worker_for_library("Lib")

        asyncio.create_task(stop_mid_flight())  # noqa: RUF006

        with pytest.raises(worker_events.WorkerGoneError, match="shut down to reset library 'Lib'"):
            await worker_manager.route_to_worker(event_request, _ENGINE, _WORKER_REQUEST_TOPIC)

    @pytest.mark.asyncio
    async def test_forgets_a_worker_that_registers_while_its_process_is_stopping(
        self, worker_manager: WorkerManager
    ) -> None:
        """A booting worker can register during the termination await; it must not stay routable."""
        worker_manager._managed_worker_processes["Lib A"] = _managed_proc_mock()

        async def register_during_termination(_library_name: str, _proc: object) -> None:
            worker_manager._workers["eng-late"] = WorkerRegistration(
                request_topic=f"sessions/{_SESSION}/workers/eng-late/request", worker_key="Lib A"
            )

        with patch.object(worker_manager, "_terminate_via_spawn_loop", side_effect=register_during_termination):
            await worker_manager.stop_worker_for_library("Lib A")

        assert worker_manager.get_worker_for_key("Lib A") is None

    @pytest.mark.asyncio
    async def test_releases_anything_waiting_for_that_library_to_become_executable(
        self, worker_manager: WorkerManager
    ) -> None:
        """A node waiting on the readiness gate learns why instead of waiting out the load timeout."""
        worker_manager.expect_worker("Lib A")

        await worker_manager.stop_worker_for_library("Lib A")

        assert worker_manager.has_settled("Lib A")
        assert worker_manager.worker_unavailable_reason("Lib A") is not None

    @pytest.mark.asyncio
    async def test_is_a_no_op_for_a_library_without_a_worker(self, worker_manager: WorkerManager) -> None:
        proc = _managed_proc_mock()
        worker_manager._managed_worker_processes["Lib B"] = proc

        await worker_manager.stop_worker_for_library("Lib A")

        proc.terminate.assert_not_called()
        assert list(worker_manager._managed_worker_processes) == ["Lib B"]


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

    @pytest.mark.asyncio
    async def test_a_second_request_while_the_first_is_pending_starts_nothing(
        self, worker_manager: WorkerManager
    ) -> None:
        """A library is requested as it registers and again as a session starts; one worker results.

        The readiness gate is kept too: a run already waiting on it would otherwise never be released.
        """
        request = worker_events.StartWorkerRequest(library_name="Lib")

        with patch.object(worker_manager, "_spawn_when_session_ready", new=AsyncMock()) as spawn:
            await worker_manager.handle_start_worker_request(request)
            gate = worker_manager._execution_ready["Lib"]
            await worker_manager.handle_start_worker_request(request)
            await asyncio.sleep(0)

        spawn.assert_awaited_once_with("Lib")
        assert worker_manager._execution_ready["Lib"] is gate

    @pytest.mark.asyncio
    async def test_a_request_once_the_spawn_finished_and_its_worker_is_gone_starts_another(
        self, worker_manager: WorkerManager
    ) -> None:
        request = worker_events.StartWorkerRequest(library_name="Lib")

        with patch.object(worker_manager, "_spawn_when_session_ready", new=AsyncMock()) as spawn:
            await worker_manager.handle_start_worker_request(request)
            await worker_manager._requested_spawns["Lib"]
            # Done-callbacks, which release the request, run on the next turn of the loop.
            await asyncio.sleep(0)
            await worker_manager.handle_start_worker_request(request)
            await asyncio.sleep(0)

        assert spawn.await_args_list == [call("Lib"), call("Lib")]

    @pytest.mark.asyncio
    async def test_a_request_for_a_running_worker_starts_nothing_and_keeps_its_gate(
        self, worker_manager: WorkerManager
    ) -> None:
        """A loaded worker's gate is already set; replacing it would leave every later run waiting."""
        worker_manager.expect_worker("Lib")
        worker_manager.note_library_loaded("Lib")
        worker_manager._managed_worker_processes["Lib"] = _managed_proc_mock()

        with patch.object(worker_manager, "_spawn_when_session_ready", new=AsyncMock()) as spawn:
            await worker_manager.handle_start_worker_request(worker_events.StartWorkerRequest(library_name="Lib"))
            await asyncio.sleep(0)

        spawn.assert_not_awaited()
        assert worker_manager.has_settled("Lib")

    @pytest.mark.asyncio
    async def test_a_request_for_a_registered_worker_starts_nothing(self, worker_manager: WorkerManager) -> None:
        worker_manager._workers["eng-1"] = WorkerRegistration(
            request_topic=f"sessions/{_SESSION}/workers/eng-1/request", worker_key="Lib"
        )

        with patch.object(worker_manager, "_spawn_when_session_ready", new=AsyncMock()) as spawn:
            await worker_manager.handle_start_worker_request(worker_events.StartWorkerRequest(library_name="Lib"))
            await asyncio.sleep(0)

        spawn.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stop", ["reset_workers", "stop_worker_for_library"])
    async def test_stopping_withdraws_a_pending_request(self, worker_manager: WorkerManager, stop: str) -> None:
        """The pending spawn would start a worker after the stop, and the next request would be refused."""
        never = asyncio.Event()

        async def wait_for_a_session(_library_name: str) -> None:
            await never.wait()

        with patch.object(worker_manager, "_spawn_when_session_ready", side_effect=wait_for_a_session):
            await worker_manager.handle_start_worker_request(worker_events.StartWorkerRequest(library_name="Lib"))
            await asyncio.sleep(0)
            pending = worker_manager._requested_spawns["Lib"]

            if stop == "reset_workers":
                await worker_manager.reset_workers()
            else:
                await worker_manager.stop_worker_for_library("Lib")
            await asyncio.wait({pending}, timeout=1)

            assert pending.cancelled()
            # Whatever waited on the cancelled spawn is told why rather than timing out.
            assert worker_manager.has_settled("Lib")
            assert worker_manager.worker_unavailable_reason("Lib") is not None

            await worker_manager.handle_start_worker_request(worker_events.StartWorkerRequest(library_name="Lib"))
            assert worker_manager._requested_spawns["Lib"] is not pending
            worker_manager._requested_spawns["Lib"].cancel()

    @pytest.mark.asyncio
    async def test_a_reset_leaves_a_spawn_already_under_way_to_finish(self, worker_manager: WorkerManager) -> None:
        """Cancelled between creating its process and recording it, the spawn would leak the process."""
        released = asyncio.Event()

        async def claim_then_park(library_name: str) -> None:
            worker_manager._spawns_in_flight[library_name] = asyncio.Event()
            await released.wait()

        with patch.object(worker_manager, "_spawn_when_session_ready", side_effect=claim_then_park):
            await worker_manager.handle_start_worker_request(worker_events.StartWorkerRequest(library_name="Lib"))
            await asyncio.sleep(0)
            under_way = worker_manager._requested_spawns["Lib"]

            await worker_manager.reset_workers()

            assert not under_way.cancelled()
            assert "Lib" not in worker_manager._requested_spawns
            released.set()
            await under_way


class TestLogSpawnError:
    @pytest.mark.asyncio
    async def test_a_cancelled_spawn_does_not_raise_from_the_callback(self, worker_manager: WorkerManager) -> None:
        """A cancelled spawn task must not make its own done-callback raise.

        `task.exception()` raises on a cancelled task, and a done-callback that raises becomes
        loop-level "Exception in callback" noise with the refusal below it skipped.
        """

        async def _never() -> None:
            await asyncio.sleep(3600)

        task = asyncio.create_task(_never())
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()

        worker_manager._log_spawn_error(task, "My Library")

    @pytest.mark.asyncio
    async def test_a_failed_spawn_still_records_a_refusal(self, worker_manager: WorkerManager) -> None:
        async def _raise() -> None:
            msg = "no interpreter"
            raise OSError(msg)

        task = asyncio.create_task(_raise())
        await asyncio.gather(task, return_exceptions=True)

        with patch.object(worker_manager, "note_worker_unavailable") as mock_refuse:
            worker_manager._log_spawn_error(task, "My Library")

        mock_refuse.assert_called_once()


class TestSpawnWhenSessionReady:
    @pytest.mark.asyncio
    async def test_skips_wait_when_session_already_active(self, worker_manager: WorkerManager) -> None:
        """If a session is already active, spawn proceeds without waiting for the event."""
        worker_manager.engine.get_session_id.return_value = _SESSION  # type: ignore[union-attr]

        with patch.object(worker_manager, "spawn_worker", new=AsyncMock()) as mock_spawn:
            await worker_manager._spawn_when_session_ready("My Library")

        mock_spawn.assert_called_once()
        assert not worker_manager._session_ready_event.is_set()

    @pytest.mark.asyncio
    async def test_waits_then_spawns_after_session_ready(self, worker_manager: WorkerManager) -> None:
        """If no session yet, waits for the event and spawns once the session is available."""
        # First call (pre-check) returns None; second call (post-wait) returns session ID.
        worker_manager.engine.get_session_id.side_effect = [None, _SESSION]  # type: ignore[union-attr]

        with patch.object(worker_manager, "spawn_worker", new=AsyncMock()) as mock_spawn:
            task = asyncio.create_task(worker_manager._spawn_when_session_ready("My Library"))
            await asyncio.sleep(0)  # let the task start and reach the event wait
            worker_manager._session_ready_event.set()
            await task

        mock_spawn.assert_called_once()

    @pytest.mark.asyncio
    async def test_logs_error_when_no_session_after_event(self, worker_manager: WorkerManager) -> None:
        """If the session event fires but get_session_id still returns None, spawn is not attempted."""
        worker_manager.engine.get_session_id.side_effect = [None, None]  # type: ignore[union-attr]
        worker_manager._session_ready_event.set()

        with patch.object(worker_manager, "spawn_worker", new=AsyncMock()) as mock_spawn:
            await worker_manager._spawn_when_session_ready("My Library")

        mock_spawn.assert_not_called()


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

    @pytest.mark.asyncio
    async def test_evicted_worker_raises_with_the_reason(self, worker_manager: WorkerManager) -> None:
        """A worker taken away mid-request surfaces as WorkerGoneError, carrying why.

        Cancelling instead would be indistinguishable from the artist pressing stop, and the
        resolution machine reaps those silently as CANCELED -- no NodeErrorEvent, only an unnamed
        INFO line, so the node comes back UNRESOLVED with nothing anywhere saying why.
        """
        assert isinstance(worker_manager._tx.request_client, _FakeRequestClient)
        fake_rc = worker_manager._tx.request_client
        event_request = EventRequest(request=ExecuteNodeRequest(node_name="MyNode", parameter_values={}))

        async def evict_mid_flight() -> None:
            await asyncio.sleep(0)
            await fake_rc.fail_requests_by_tag(
                _ENGINE, worker_events.WorkerGoneError(f"worker '{_ENGINE}' stopped responding and was shut down")
            )

        asyncio.create_task(evict_mid_flight())  # noqa: RUF006

        with pytest.raises(worker_events.WorkerGoneError, match="stopped responding and was shut down"):
            await worker_manager.route_to_worker(event_request, _ENGINE, _WORKER_REQUEST_TOPIC)

    @pytest.mark.asyncio
    async def test_reset_settles_in_flight_requests_too(self, worker_manager: WorkerManager) -> None:
        """reset_workers is the other path that takes a worker away, and it clears the registry.

        Eviction is not reachable afterwards -- the heartbeat loop can only evict ids it can still
        see -- and route_to_worker has no wall-clock ceiling, so a node dispatched into a worker a
        library reload terminates would await a future nothing ever settles: RESOLVING forever.
        """
        assert isinstance(worker_manager._tx.request_client, _FakeRequestClient)
        worker_manager._workers = {_ENGINE: WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key="Lib")}
        event_request = EventRequest(request=ExecuteNodeRequest(node_name="MyNode", parameter_values={}))

        async def reset_mid_flight() -> None:
            await asyncio.sleep(0)
            await worker_manager.reset_workers()

        asyncio.create_task(reset_mid_flight())  # noqa: RUF006

        with pytest.raises(worker_events.WorkerGoneError, match="shut down to reload library 'Lib'"):
            await worker_manager.route_to_worker(event_request, _ENGINE, _WORKER_REQUEST_TOPIC)

    @pytest.mark.asyncio
    async def test_flow_cancellation_still_cancels(self, worker_manager: WorkerManager) -> None:
        """Cancelling the awaiting task must still raise CancelledError, so stop stays stop."""
        assert isinstance(worker_manager._tx.request_client, _FakeRequestClient)
        event_request = EventRequest(request=ExecuteNodeRequest(node_name="MyNode", parameter_values={}))

        task = asyncio.create_task(worker_manager.route_to_worker(event_request, _ENGINE, _WORKER_REQUEST_TOPIC))
        await asyncio.sleep(0)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_flow_cancellation_stops_tracking_the_request(self, worker_manager: WorkerManager) -> None:
        """A cancelled run must not leave its request behind in the pending map.

        Nothing else pops it on this path, so the entry would outlive the run -- one per cancelled
        node execution for the life of the process -- and each keeps its worker's tag, so a later
        fail_requests_by_tag walks and re-settles long-dead requests.
        """
        assert isinstance(worker_manager._tx.request_client, _FakeRequestClient)
        fake_rc = worker_manager._tx.request_client
        event_request = EventRequest(request=ExecuteNodeRequest(node_name="MyNode", parameter_values={}))

        task = asyncio.create_task(worker_manager.route_to_worker(event_request, _ENGINE, _WORKER_REQUEST_TOPIC))
        # Long enough to be parked on the response, which is the await the cancellation has to
        # unwind from for the entry to be the caller's to remove.
        await asyncio.sleep(0.01)
        assert len(fake_rc._pending_requests) == 1

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert fake_rc._pending_requests == {}

    @pytest.mark.asyncio
    async def test_cancellation_during_the_publish_stops_tracking_the_request(
        self, worker_manager: WorkerManager
    ) -> None:
        """The publish is an await too, so the entry has to be released if it unwinds there.

        Registering before publishing is deliberate -- the response can arrive the instant the
        request lands -- which leaves a window where the entry exists and the publish has not
        returned. A cancellation delivered in that window leaks exactly the entry the response-side
        guard exists to release.
        """
        assert isinstance(worker_manager._tx.request_client, _FakeRequestClient)
        fake_rc = worker_manager._tx.request_client
        event_request = EventRequest(request=ExecuteNodeRequest(node_name="MyNode", parameter_values={}))
        publishing = asyncio.Event()

        async def _park_in_the_publish(*_args: object, **_kwargs: object) -> None:
            publishing.set()
            await asyncio.sleep(3600)

        with patch.object(worker_manager, "forward_event_to_worker", new=_park_in_the_publish):
            task = asyncio.create_task(worker_manager.route_to_worker(event_request, _ENGINE, _WORKER_REQUEST_TOPIC))
            await publishing.wait()
            assert len(fake_rc._pending_requests) == 1

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert fake_rc._pending_requests == {}


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
        worker_manager.engine.get_session_id.return_value = None  # type: ignore[union-attr]

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
        worker_manager.engine.get_session_id.return_value = None  # type: ignore[union-attr]

        topic = worker_manager._determine_response_topic()

        assert topic == f"engines/{_ENGINE}/response"

    def test_returns_default_when_no_session_or_engine(self, worker_manager: WorkerManager) -> None:
        worker_manager.engine.get_session_id.return_value = None  # type: ignore[union-attr]
        worker_manager.engine.get_engine_id.return_value = None  # type: ignore[union-attr]

        topic = worker_manager._determine_response_topic()

        assert topic == "response"


class TestOrchestratorHeartbeatLoop:
    @pytest.mark.asyncio
    async def test_evicts_stale_worker(self, worker_manager: WorkerManager, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(worker_manager, "heartbeat_interval_s", 0.01)
        monkeypatch.setattr(worker_manager, "heartbeat_timeout_s", 0.0)
        worker_manager._workers[_ENGINE] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key=None)

        await _sweep_until_evicted(worker_manager)

        assert _ENGINE not in worker_manager._workers

    @pytest.mark.asyncio
    async def test_sends_heartbeat_challenge_to_live_worker(
        self, worker_manager: WorkerManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(worker_manager, "heartbeat_interval_s", 0.01)
        monkeypatch.setattr(worker_manager, "heartbeat_timeout_s", 60.0)
        worker_manager._workers[_ENGINE] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key=None)

        task = asyncio.create_task(worker_manager.orchestrator_heartbeat_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        # A challenge is counted only once it has actually been sent.
        worker_manager._tx.send_message.assert_called()  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_does_not_evict_fresh_worker(
        self, worker_manager: WorkerManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(worker_manager, "heartbeat_interval_s", 0.01)
        monkeypatch.setattr(worker_manager, "heartbeat_timeout_s", 60.0)
        worker_manager._workers[_ENGINE] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key=None)

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
        gtn.config_manager.get_config_value.side_effect = lambda _key, default, cast_type=float: cast_type(default)
        wm = WorkerManager(engine=gtn, event_manager=EventManager())
        wm.attach_transport(
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

        ``EventManager.broadcast_app_event`` runs the listener fan-out on a transient
        ``ThreadRunner`` side loop, which is torn down as soon as the listener returns -- so a
        listener that schedules its fan-out with ``asyncio.create_task`` leaves an orphan that
        never runs. The listener awaits inline instead, and this confirms the broadcast has landed
        by the time ``broadcast_app_event`` returns, even when the transport ``await`` does not
        resolve synchronously (the production shape -- a real WebSocket send
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


class TestWorkerExecutionPath:
    """A worker gets its library's execution dependencies as PYTHONPATH, not as a later splice.

    Splicing them onto a running interpreter cannot give the library its own versions. A module
    already in sys.modules is never reconsidered, and a package that probed for an optional
    dependency at import time has cached the answer -- which is how a library that ships
    `safetensors` still hit `NameError: name 'safetensors' is not defined` inside huggingface_hub.
    PYTHONPATH is on sys.path before the process imports anything, which is the whole point.
    """

    @pytest.mark.asyncio
    async def test_spawn_hands_the_worker_its_execution_path(self, worker_manager: WorkerManager) -> None:
        environment = worker_manager.engine.library_manager.environment
        environment.execution_site_packages.return_value = "/libs/mine/.venv-exec/sp"  # type: ignore[attr-defined]

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=_managed_proc_mock())) as spawn:
            await worker_manager.spawn_worker([sys.executable, "-c", ""], "My Library")

        env = spawn.call_args.kwargs["env"]
        assert env["PYTHONPATH"] == "/libs/mine/.venv-exec/sp"

    @pytest.mark.asyncio
    async def test_an_inherited_pythonpath_survives_the_handover(self, worker_manager: WorkerManager) -> None:
        """Prepended, not assigned.

        A launcher-set PYTHONPATH -- embedding hosts, source checkouts -- is part of the environment
        the engine itself booted with. Assigning over it would lose those modules in exactly one
        process kind, so an import that resolves in the orchestrator would fail in its worker.
        """
        environment = worker_manager.engine.library_manager.environment
        environment.execution_site_packages.return_value = "/libs/mine/.venv-exec/sp"  # type: ignore[attr-defined]
        worker_manager.engine.project_manager.get_pre_project_environ.return_value = {"PYTHONPATH": "/host/libs"}  # type: ignore[attr-defined]

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=_managed_proc_mock())) as spawn:
            await worker_manager.spawn_worker([sys.executable, "-c", ""], "My Library")

        env = spawn.call_args.kwargs["env"]
        # Library first, so a package the library pins wins; the engine's own environment still
        # resolves anything the library does not carry.
        assert env["PYTHONPATH"] == f"/libs/mine/.venv-exec/sp{os.pathsep}/host/libs"

    @pytest.mark.asyncio
    async def test_no_execution_environment_means_no_pythonpath(self, worker_manager: WorkerManager) -> None:
        """A library with no execution dependencies, or one whose environment is not built yet.

        Pointing PYTHONPATH at a directory that does not exist would be silently ignored by Python,
        so an absent entry and a wrong one look identical from inside the worker. Leave it unset.
        """
        worker_manager.engine.library_manager.environment.execution_site_packages.return_value = None  # type: ignore[attr-defined]

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=_managed_proc_mock())) as spawn:
            await worker_manager.spawn_worker([sys.executable, "-c", ""], "Light Library")

        assert "PYTHONPATH" not in spawn.call_args.kwargs["env"]


class TestHeartbeatIntervalFloor:
    """A configured interval the code cannot run is raised to the minimum, loudly.

    The interval divides the timeout to size the challenge allowance and is the sleep in both
    heartbeat loops, so a non-positive value is a ZeroDivisionError on one path and a hot loop on
    the other.
    """

    @staticmethod
    def _manager_with_interval(interval: float) -> WorkerManager:
        gtn = MagicMock()
        gtn.get_session_id.return_value = _SESSION
        gtn.get_engine_id.return_value = _ENGINE

        def _config(key: str, default: float, cast_type: type = float) -> float:
            from griptape_nodes.retained_mode.managers.settings import WORKER_HEARTBEAT_INTERVAL_KEY

            if key == WORKER_HEARTBEAT_INTERVAL_KEY:
                return cast_type(interval)
            return cast_type(default)

        gtn.config_manager.get_config_value.side_effect = _config
        return WorkerManager(engine=gtn, event_manager=MagicMock())

    def test_a_zero_interval_is_raised_to_the_minimum(self) -> None:
        manager = self._manager_with_interval(0.0)

        assert manager.heartbeat_interval_s == WorkerManager.MINIMUM_HEARTBEAT_INTERVAL_S

    def test_a_negative_interval_is_raised_to_the_minimum(self) -> None:
        manager = self._manager_with_interval(-5.0)

        assert manager.heartbeat_interval_s == WorkerManager.MINIMUM_HEARTBEAT_INTERVAL_S

    def test_a_zero_interval_leaves_the_challenge_allowance_computable(self) -> None:
        """The division is what a zero interval used to break."""
        manager = self._manager_with_interval(0.0)

        assert manager.unanswered_challenges_allowed >= 1

    def test_raising_the_interval_is_reported(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            self._manager_with_interval(0.0)

        assert "cannot be used" in caplog.text

    def test_a_usable_interval_is_left_alone(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            manager = self._manager_with_interval(2.5)

        assert manager.heartbeat_interval_s == 2.5  # noqa: PLR2004
        assert "cannot be used" not in caplog.text


class TestChallengeSendFailure:
    """A send that fails is the orchestrator's problem, not the worker's.

    The counter decides eviction, so a challenge that never left must not be counted, and the
    loop must survive a transport that raises while a connection is re-establishing.
    """

    @pytest.mark.asyncio
    async def test_a_failed_send_is_not_counted_against_the_worker(
        self, worker_manager: WorkerManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(worker_manager, "heartbeat_interval_s", 0.01)
        worker_manager._workers[_ENGINE] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key=None)
        worker_manager._tx.send_message.side_effect = ConnectionError("no socket")  # type: ignore[union-attr]

        task = asyncio.create_task(worker_manager.orchestrator_heartbeat_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert worker_manager._workers[_ENGINE].unanswered_challenges == 0

    @pytest.mark.asyncio
    async def test_a_failed_send_does_not_end_the_loop(
        self, worker_manager: WorkerManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the loop exits, nothing evicts a worker for the rest of the process's life."""
        monkeypatch.setattr(worker_manager, "heartbeat_interval_s", 0.01)
        worker_manager._workers[_ENGINE] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key=None)
        worker_manager._tx.send_message.side_effect = ConnectionError("no socket")  # type: ignore[union-attr]

        task = asyncio.create_task(worker_manager.orchestrator_heartbeat_loop())
        await asyncio.sleep(0.05)
        still_running = not task.done()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert still_running


class TestChallengeAllowanceRounding:
    """The allowance must not tolerate less silence than the configured timeout."""

    def test_a_fractional_ratio_rounds_up(self, worker_manager: WorkerManager, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(worker_manager, "heartbeat_timeout_s", 12.0)
        monkeypatch.setattr(worker_manager, "heartbeat_interval_s", 5.0)

        # Nearest would give 2, evicting after ~10s against a 12s timeout.
        assert worker_manager.unanswered_challenges_allowed == 3  # noqa: PLR2004

    def test_a_half_ratio_rounds_up(self, worker_manager: WorkerManager, monkeypatch: pytest.MonkeyPatch) -> None:
        """Banker's rounding makes `round(2.5)` 2, which is below the configured tolerance."""
        monkeypatch.setattr(worker_manager, "heartbeat_timeout_s", 12.5)
        monkeypatch.setattr(worker_manager, "heartbeat_interval_s", 5.0)

        assert worker_manager.unanswered_challenges_allowed == 3  # noqa: PLR2004

    def test_an_exact_ratio_is_unchanged(self, worker_manager: WorkerManager, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(worker_manager, "heartbeat_timeout_s", 15.0)
        monkeypatch.setattr(worker_manager, "heartbeat_interval_s", 5.0)

        assert worker_manager.unanswered_challenges_allowed == 3  # noqa: PLR2004


_MACHINE = "gpu-01-engine-id"
_LIBRARY = "Heavy Library"
_LICENSE = "artist-license"


def _turn_on_distributed_execution(worker_manager: WorkerManager) -> None:
    """Turn the beta feature on and assign `_LIBRARY` to `_MACHINE` in the distribution settings."""
    read_default = worker_manager.engine.config_manager.get_config_value.side_effect  # type: ignore[union-attr]

    def get_config_value(key: str, default: object = None, cast_type: type | None = None, **kwargs: object) -> object:
        if key == DISTRIBUTED_EXECUTION.config_key:
            return True
        if key == DISTRIBUTED_EXECUTION_LIBRARIES_KEY:
            return {_LIBRARY: _MACHINE}
        return read_default(key, default, cast_type, **kwargs)

    worker_manager.engine.config_manager.get_config_value.side_effect = get_config_value  # type: ignore[union-attr]
    worker_manager.engine.secrets_manager.get_secret.return_value = _LICENSE  # type: ignore[union-attr]
    worker_manager.engine.library_manager.get_library_info_by_library_name.return_value = MagicMock(  # type: ignore[union-attr]
        library_version="1.0.0"
    )


def _machine(worker_manager: WorkerManager, *, refuse: dict[str, str] | None = None) -> MagicMock:
    """Attach machine links whose machine answers like a serving orchestrator, refusing what `refuse` names.

    `refuse` maps a request type to the reason that machine gives for refusing it.
    """
    _turn_on_distributed_execution(worker_manager)
    refuse = refuse or {}
    request_client = cast("_FakeRequestClient", worker_manager._tx.request_client)

    async def answer(_address: str, _event_type: str, payload: str, _topic: str) -> None:
        sent = json.loads(payload)
        request_type = sent["request_type"]
        result_type = request_type.replace("Request", "ResultSuccess")
        result: dict[str, object] = {"result_details": "ok"}
        if request_type == "RegisterOriginatorRequest":
            result["engine_id"] = _MACHINE
        if request_type in refuse:
            result_type = request_type.replace("Request", "ResultFailure")
            result = {"result_details": refuse[request_type]}
        pending = request_client._pending_requests.pop(sent["request_id"], None)
        if pending is not None:
            pending.future.set_result({"result_type": result_type, "result": result})

    links = MagicMock()
    links.connect = AsyncMock()
    links.publish = AsyncMock(side_effect=answer)
    links.subscribe = AsyncMock()
    links.route = AsyncMock()
    links.relay = AsyncMock()
    links.unroute = AsyncMock()
    links.unrelay = AsyncMock()
    worker_manager.attach_machine_links(links)
    return links


def _sent(links: MagicMock) -> list[dict]:
    """The requests published to the machine, in order."""
    return [json.loads(published.args[2]) for published in links.publish.await_args_list]


async def _start_on_machine(worker_manager: WorkerManager, library_name: str = _LIBRARY) -> None:
    with patch.object(worker_manager, "_orchestrator_static_server_base_url", new=AsyncMock(return_value=None)):
        await worker_manager.handle_start_worker_request(worker_events.StartWorkerRequest(library_name=library_name))
        await worker_manager._requested_spawns[library_name]


class TestStartingAWorkerOnAnotherMachine:
    @pytest.mark.asyncio
    async def test_the_originator_registers_then_asks_the_machine_to_start_the_worker(
        self, worker_manager: WorkerManager
    ) -> None:
        links = _machine(worker_manager)

        with patch.object(worker_manager, "spawn_worker", new=AsyncMock()) as spawn:
            await _start_on_machine(worker_manager)

        spawn.assert_not_awaited()
        links.subscribe.assert_has_awaits(
            [call(_MACHINE, f"sessions/{_SESSION}/request"), call(_MACHINE, f"sessions/{_SESSION}/response")]
        )
        registration, start = _sent(links)
        assert registration["request_type"] == "RegisterOriginatorRequest"
        assert registration["request"]["engine_version"] == engine_version
        assert registration["request"]["library_versions"] == {_LIBRARY: "1.0.0"}
        assert registration["request"]["license_token"] == _LICENSE
        assert start["request_type"] == "StartWorkerRequest"
        assert start["request"]["originator_session_id"] == _SESSION
        assert worker_manager.worker_unavailable_reason(_LIBRARY) is None

    @pytest.mark.asyncio
    async def test_a_refused_registration_makes_the_library_unavailable_with_the_reason(
        self, worker_manager: WorkerManager
    ) -> None:
        links = _machine(worker_manager, refuse={"RegisterOriginatorRequest": "engine version 9.9.9 differs"})

        await _start_on_machine(worker_manager)

        assert [sent["request_type"] for sent in _sent(links)] == ["RegisterOriginatorRequest"]
        reason = worker_manager.worker_unavailable_reason(_LIBRARY)
        assert reason is not None
        assert "engine version 9.9.9 differs" in reason
        assert worker_manager.has_settled(_LIBRARY)

    @pytest.mark.asyncio
    async def test_a_refused_registration_is_asked_again_by_the_next_start(self, worker_manager: WorkerManager) -> None:
        refuse = {"RegisterOriginatorRequest": "the machine was busy restarting"}
        links = _machine(worker_manager, refuse=refuse)
        await _start_on_machine(worker_manager)
        refuse.clear()

        await _start_on_machine(worker_manager)

        assert [sent["request_type"] for sent in _sent(links)] == [
            "RegisterOriginatorRequest",
            "RegisterOriginatorRequest",
            "StartWorkerRequest",
        ]

    @pytest.mark.asyncio
    async def test_a_machine_that_forgot_this_orchestrator_is_registered_with_again(
        self, worker_manager: WorkerManager
    ) -> None:
        """A machine that restarted no longer knows the originator its earlier registration accepted."""
        links = _machine(worker_manager)
        answer = links.publish.side_effect
        request_client = cast("_FakeRequestClient", worker_manager._tx.request_client)
        forgotten = {"once": True}

        async def forget_once(address: str, event_type: str, payload: str, topic: str) -> None:
            sent = json.loads(payload)
            if sent["request_type"] == "StartWorkerRequest" and forgotten.pop("once", False):
                request_client._pending_requests.pop(sent["request_id"]).future.set_result(
                    {
                        "result_type": "StartWorkerResultFailure",
                        "result": {"result_details": "never registered", "originator_unknown": True},
                    }
                )
                return
            await answer(address, event_type, payload, topic)

        links.publish.side_effect = forget_once

        await _start_on_machine(worker_manager)

        assert [sent["request_type"] for sent in _sent(links)] == [
            "RegisterOriginatorRequest",
            "StartWorkerRequest",
            "RegisterOriginatorRequest",
            "StartWorkerRequest",
        ]
        assert worker_manager.worker_unavailable_reason(_LIBRARY) is None

    @pytest.mark.asyncio
    async def test_a_refused_start_makes_the_library_unavailable_with_the_reason(
        self, worker_manager: WorkerManager
    ) -> None:
        _machine(worker_manager, refuse={"StartWorkerRequest": "no room"})

        await _start_on_machine(worker_manager)

        reason = worker_manager.worker_unavailable_reason(_LIBRARY)
        assert reason is not None
        assert "no room" in reason
        assert _LIBRARY not in worker_manager._remote_libraries

    @pytest.mark.asyncio
    async def test_a_machine_that_cannot_be_found_makes_the_library_unavailable_and_is_looked_up_again(
        self, worker_manager: WorkerManager
    ) -> None:
        links = _machine(worker_manager)
        links.connect.side_effect = MachineNotFoundError("gpu-01 is not running.")

        await _start_on_machine(worker_manager)

        assert worker_manager.worker_unavailable_reason(_LIBRARY) == "gpu-01 is not running."
        links.publish.assert_not_awaited()
        assert worker_manager._machine_registrations == {}

    @pytest.mark.asyncio
    async def test_a_machine_that_answers_as_another_engine_is_refused_naming_it(
        self, worker_manager: WorkerManager
    ) -> None:
        links = _machine(worker_manager)
        answer = links.publish.side_effect

        async def answer_as_another(address: str, event_type: str, payload: str, topic: str) -> None:
            request_client = cast("_FakeRequestClient", worker_manager._tx.request_client)
            sent = json.loads(payload)
            if sent["request_type"] != "RegisterOriginatorRequest":
                await answer(address, event_type, payload, topic)
                return
            pending = request_client._pending_requests.pop(sent["request_id"])
            pending.future.set_result(
                {"result_type": "RegisterOriginatorResultSuccess", "result": {"engine_id": "someone-else"}}
            )

        links.publish.side_effect = answer_as_another

        await _start_on_machine(worker_manager)

        reason = worker_manager.worker_unavailable_reason(_LIBRARY)
        assert reason is not None
        assert "someone-else" in reason
        assert [sent["request_type"] for sent in _sent(links)] == ["RegisterOriginatorRequest"]

    @pytest.mark.asyncio
    async def test_two_libraries_on_one_machine_register_with_it_once(self, worker_manager: WorkerManager) -> None:
        links = _machine(worker_manager)
        worker_manager.engine.config_manager.get_config_value.side_effect = (  # type: ignore[union-attr]
            lambda key, default=None, **_: (
                True
                if key == DISTRIBUTED_EXECUTION.config_key
                else {_LIBRARY: _MACHINE, "Other": _MACHINE}
                if key == DISTRIBUTED_EXECUTION_LIBRARIES_KEY
                else default
            )
        )

        await _start_on_machine(worker_manager, _LIBRARY)
        await _start_on_machine(worker_manager, "Other")

        assert [sent["request_type"] for sent in _sent(links)] == [
            "RegisterOriginatorRequest",
            "StartWorkerRequest",
            "StartWorkerRequest",
        ]

    @pytest.mark.asyncio
    async def test_without_machine_links_the_library_is_unavailable_rather_than_run_here(
        self, worker_manager: WorkerManager
    ) -> None:
        """Running it here would run it in the one place it was assigned away from."""
        _turn_on_distributed_execution(worker_manager)

        with patch.object(worker_manager, "spawn_worker", new=AsyncMock()) as spawn:
            await _start_on_machine(worker_manager)

        spawn.assert_not_awaited()
        reason = worker_manager.worker_unavailable_reason(_LIBRARY)
        assert reason is not None
        assert _MACHINE in reason

    @pytest.mark.asyncio
    async def test_a_library_with_no_assignment_still_spawns_here(self, worker_manager: WorkerManager) -> None:
        links = _machine(worker_manager)

        with patch.object(worker_manager, "spawn_worker", new=AsyncMock()) as spawn:
            await _start_on_machine(worker_manager, "Local Lib")

        spawn.assert_awaited_once()
        links.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_with_the_beta_feature_off_an_assigned_library_spawns_here(
        self, worker_manager: WorkerManager
    ) -> None:
        links = _machine(worker_manager)
        worker_manager.engine.config_manager.get_config_value.side_effect = (  # type: ignore[union-attr]
            lambda key, default=None, **_: (
                {_LIBRARY: _MACHINE} if key == DISTRIBUTED_EXECUTION_LIBRARIES_KEY else default
            )
        )

        with patch.object(worker_manager, "spawn_worker", new=AsyncMock()) as spawn:
            await _start_on_machine(worker_manager)

        spawn.assert_awaited_once()
        links.publish.assert_not_awaited()


def _register_originator(worker_manager: WorkerManager, **overrides: object) -> object:
    fields: dict[str, object] = {
        "originator_session_id": "origin-sess",
        "schema_version": DISTRIBUTED_EXECUTION_SCHEMA_VERSION,
        "engine_version": engine_version,
        "library_versions": {_LIBRARY: "1.0.0"},
        "license_token": _LICENSE,
        "static_server_base_url": "http://workstation:8124",
    }
    fields.update(overrides)
    return worker_manager.handle_register_originator_request(
        worker_events.RegisterOriginatorRequest(**fields)  # type: ignore[arg-type]
    )


class TestServingAnotherMachinesOrchestrator:
    def test_an_originator_on_the_same_versions_is_accepted(self, worker_manager: WorkerManager) -> None:
        _machine(worker_manager)

        result = _register_originator(worker_manager)

        assert isinstance(result, worker_events.RegisterOriginatorResultSuccess)
        assert result.refused_libraries == {}

    def test_a_different_engine_version_is_refused_naming_both(self, worker_manager: WorkerManager) -> None:
        _machine(worker_manager)

        result = _register_originator(worker_manager, engine_version="0.0.1")

        assert isinstance(result, worker_events.RegisterOriginatorResultFailure)
        assert "0.0.1" in str(result.result_details)
        assert engine_version in str(result.result_details)

    def test_a_different_library_version_refuses_only_that_library(self, worker_manager: WorkerManager) -> None:
        _machine(worker_manager)

        result = _register_originator(worker_manager, library_versions={_LIBRARY: "2.0.0"})

        assert isinstance(result, worker_events.RegisterOriginatorResultSuccess)
        assert "2.0.0" in result.refused_libraries[_LIBRARY]
        assert "1.0.0" in result.refused_libraries[_LIBRARY]

    def test_a_library_this_machine_lacks_is_refused(self, worker_manager: WorkerManager) -> None:
        _machine(worker_manager)
        worker_manager.engine.library_manager.get_library_info_by_library_name.return_value = None  # type: ignore[union-attr]

        result = _register_originator(worker_manager)

        assert isinstance(result, worker_events.RegisterOriginatorResultSuccess)
        assert "not installed" in result.refused_libraries[_LIBRARY]

    def test_with_the_beta_feature_off_every_originator_is_refused(self, worker_manager: WorkerManager) -> None:
        result = _register_originator(worker_manager)

        assert isinstance(result, worker_events.RegisterOriginatorResultFailure)

    @pytest.mark.asyncio
    async def test_the_worker_joins_the_originators_session_under_its_license_and_relays_first(
        self, worker_manager: WorkerManager
    ) -> None:
        links = _machine(worker_manager)
        _register_originator(worker_manager)
        order: list[str] = []
        links.relay.side_effect = lambda prefix: order.append(f"relay {prefix}")
        spawned: dict[str, object] = {}

        async def record_spawn(args: list[str], worker_key: str, **kwargs: object) -> None:
            order.append(f"spawn {worker_key}")
            spawned.update(kwargs, args=args)

        with patch.object(worker_manager, "spawn_worker", side_effect=record_spawn):
            result = await worker_manager.handle_start_worker_request(
                worker_events.StartWorkerRequest(library_name=_LIBRARY, originator_session_id="origin-sess")
            )

        assert isinstance(result, worker_events.StartWorkerResultSuccess), result.result_details
        assert order == [
            "relay sessions/origin-sess/",
            f"relay engines/{spawned['worker_engine_id']}/",
            f"spawn origin-sess/{_LIBRARY}",
        ]
        args = cast("list[str]", spawned["args"])
        assert args[args.index("--session-id") + 1] == "origin-sess"
        assert spawned["library_name"] == _LIBRARY
        assert spawned["environ_overrides"] == {
            "GRIPTAPE_NODES_LICENSE": _LICENSE,
            ORCHESTRATOR_STATIC_SERVER_BASE_URL_ENV: "http://workstation:8124",
        }

    @pytest.mark.asyncio
    async def test_a_start_for_an_unregistered_originator_fails(self, worker_manager: WorkerManager) -> None:
        _machine(worker_manager)

        with patch.object(worker_manager, "spawn_worker", new=AsyncMock()) as spawn:
            result = await worker_manager.handle_start_worker_request(
                worker_events.StartWorkerRequest(library_name=_LIBRARY, originator_session_id="origin-sess")
            )

        assert isinstance(result, worker_events.StartWorkerResultFailure)
        assert result.originator_unknown
        spawn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_start_for_a_running_worker_replaces_it(self, worker_manager: WorkerManager) -> None:
        """The originator asks only once it stopped using that worker, so the old one would never register."""
        _machine(worker_manager)
        _register_originator(worker_manager)
        stale = _managed_proc_mock()
        worker_manager._managed_worker_processes[f"origin-sess/{_LIBRARY}"] = stale

        with (
            patch.object(worker_manager, "_terminate_via_spawn_loop", new=AsyncMock()) as terminate,
            patch.object(worker_manager, "spawn_worker", new=AsyncMock()) as spawn,
        ):
            result = await worker_manager.handle_start_worker_request(
                worker_events.StartWorkerRequest(library_name=_LIBRARY, originator_session_id="origin-sess")
            )

        assert isinstance(result, worker_events.StartWorkerResultSuccess), result.result_details
        terminate.assert_awaited_once_with(f"origin-sess/{_LIBRARY}", stale)
        spawn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_start_for_a_refused_library_fails_with_the_refusal(self, worker_manager: WorkerManager) -> None:
        _machine(worker_manager)
        _register_originator(worker_manager, library_versions={_LIBRARY: "2.0.0"})

        with patch.object(worker_manager, "spawn_worker", new=AsyncMock()) as spawn:
            result = await worker_manager.handle_start_worker_request(
                worker_events.StartWorkerRequest(library_name=_LIBRARY, originator_session_id="origin-sess")
            )

        assert isinstance(result, worker_events.StartWorkerResultFailure)
        assert "2.0.0" in str(result.result_details)
        assert not result.originator_unknown
        spawn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_worker_that_exited_is_forgotten_so_another_can_start(self, worker_manager: WorkerManager) -> None:
        _machine(worker_manager)
        proc = _managed_proc_mock()
        worker_manager._managed_worker_processes["origin-sess/Lib"] = proc

        await worker_manager._forget_when_exited("origin-sess/Lib", proc, "w1")

        assert "origin-sess/Lib" not in worker_manager._managed_worker_processes

    @pytest.mark.asyncio
    async def test_a_workers_topics_stop_being_relayed_and_its_sessions_with_the_last_one(
        self, worker_manager: WorkerManager
    ) -> None:
        """Otherwise a machine serving many sessions relays every topic it ever served, for good."""
        links = _machine(worker_manager)
        _register_originator(worker_manager, library_versions={_LIBRARY: "1.0.0", "Other": "1.0.0"})
        started: dict[str, str] = {}

        async def record_spawn(_args: list[str], worker_key: str, **kwargs: object) -> None:
            started[worker_key] = cast("str", kwargs["worker_engine_id"])
            worker_manager._managed_worker_processes[worker_key] = _managed_proc_mock()

        with patch.object(worker_manager, "spawn_worker", side_effect=record_spawn):
            for library in (_LIBRARY, "Other"):
                await worker_manager.handle_start_worker_request(
                    worker_events.StartWorkerRequest(library_name=library, originator_session_id="origin-sess")
                )
        first, second = (f"origin-sess/{library}" for library in (_LIBRARY, "Other"))

        await worker_manager._forget_when_exited(first, worker_manager._managed_worker_processes[first], started[first])
        assert links.unrelay.await_args_list == [call(f"engines/{started[first]}/")]

        await worker_manager._forget_when_exited(
            second, worker_manager._managed_worker_processes[second], started[second]
        )
        assert links.unrelay.await_args_list[1:] == [call(f"engines/{started[second]}/"), call("sessions/origin-sess/")]

    @pytest.mark.asyncio
    async def test_a_spawn_that_produced_no_worker_stops_relaying_at_once(self, worker_manager: WorkerManager) -> None:
        links = _machine(worker_manager)
        _register_originator(worker_manager)

        with patch.object(worker_manager, "spawn_worker", new=AsyncMock()):
            await worker_manager.handle_start_worker_request(
                worker_events.StartWorkerRequest(library_name=_LIBRARY, originator_session_id="origin-sess")
            )

        assert call("sessions/origin-sess/") in links.unrelay.await_args_list
        assert worker_manager._session_relays == {}


class TestARemoteWorkerIsReachedThroughItsMachine:
    @pytest.mark.asyncio
    async def test_registration_subscribes_and_routes_on_the_machine(self, worker_manager: WorkerManager) -> None:
        links = _machine(worker_manager)
        worker_manager._remote_libraries[_LIBRARY] = _MACHINE

        await worker_manager.handle_register_worker_request(
            worker_events.RegisterWorkerRequest(
                worker_engine_id=_ENGINE, engine_version=engine_version, library_name=_LIBRARY
            )
        )

        assert worker_manager._workers[_ENGINE].machine == _MACHINE
        links.subscribe.assert_awaited_once_with(_MACHINE, _WORKER_RESPONSE_TOPIC)
        links.route.assert_awaited_once_with(_MACHINE, f"engines/{_ENGINE}/")
        worker_manager._tx.subscribe_to_topic.assert_not_called()  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_requests_to_it_go_through_the_machine(self, worker_manager: WorkerManager) -> None:
        links = _machine(worker_manager)
        links.publish.side_effect = None
        worker_manager._workers[_ENGINE] = WorkerRegistration(
            request_topic=_WORKER_REQUEST_TOPIC, worker_key=_LIBRARY, machine=_MACHINE
        )

        await worker_manager.forward_event_to_worker(
            EventRequest(request=GetParameterValueRequest(node_name="n", parameter_name="p")),
            worker_engine_id=_ENGINE,
            worker_request_topic=_WORKER_REQUEST_TOPIC,
        )

        address, event_type, _payload, topic = links.publish.await_args.args
        assert (address, event_type, topic) == (_MACHINE, "EventRequest", _WORKER_REQUEST_TOPIC)
        worker_manager._tx.send_message.assert_not_called()  # type: ignore[union-attr]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("leaves", ["unregisters", "is evicted"])
    async def test_its_topics_stop_going_to_the_machine_once_it_leaves(
        self, worker_manager: WorkerManager, leaves: str
    ) -> None:
        links = _machine(worker_manager)
        worker_manager._workers[_ENGINE] = WorkerRegistration(
            request_topic=_WORKER_REQUEST_TOPIC, worker_key=_LIBRARY, machine=_MACHINE
        )

        if leaves == "unregisters":
            await worker_manager.handle_unregister_worker_request(
                worker_events.UnregisterWorkerRequest(worker_engine_id=_ENGINE)
            )
        else:
            await worker_manager.evict_worker(_ENGINE)

        links.unroute.assert_awaited_once_with(_MACHINE, f"engines/{_ENGINE}/")

    @pytest.mark.asyncio
    async def test_a_local_worker_is_still_reached_directly(self, worker_manager: WorkerManager) -> None:
        links = _machine(worker_manager)
        worker_manager._workers[_ENGINE] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key="Lib")

        await worker_manager.forward_event_to_worker(
            EventRequest(request=GetParameterValueRequest(node_name="n", parameter_name="p")),
            worker_engine_id=_ENGINE,
            worker_request_topic=_WORKER_REQUEST_TOPIC,
        )

        links.publish.assert_not_awaited()
        worker_manager._tx.send_message.assert_awaited_once()  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_heartbeat_challenges_go_through_the_machine(
        self, worker_manager: WorkerManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        links = _machine(worker_manager)
        links.publish.side_effect = None
        monkeypatch.setattr(worker_manager, "heartbeat_interval_s", 0.01)
        monkeypatch.setattr(worker_manager, "heartbeat_timeout_s", 60.0)
        worker_manager._workers[_ENGINE] = WorkerRegistration(
            request_topic=_WORKER_REQUEST_TOPIC, worker_key=_LIBRARY, machine=_MACHINE
        )

        task = asyncio.create_task(worker_manager.orchestrator_heartbeat_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        address, event_type, _payload, topic = links.publish.await_args.args
        assert (address, event_type, topic) == (_MACHINE, "EventRequest", _WORKER_REQUEST_TOPIC)
        worker_manager._tx.send_message.assert_not_called()  # type: ignore[union-attr]


def _remote_worker(worker_manager: WorkerManager) -> MagicMock:
    """Register `_ENGINE` as `_LIBRARY`'s worker on `_MACHINE`, which answers like a serving orchestrator."""
    links = _machine(worker_manager)
    worker_manager._workers[_ENGINE] = WorkerRegistration(
        request_topic=_WORKER_REQUEST_TOPIC, worker_key=_LIBRARY, machine=_MACHINE
    )
    return links


def _execute(node_name: str = "heavy") -> EventRequest:
    return EventRequest(
        request=ExecuteNodeRequest(node_name=node_name, parameter_values={}),
        request_id=f"exec-{node_name}",
    )


class TestANodeForAnotherMachineIsAdmittedFirst:
    @pytest.mark.asyncio
    async def test_it_is_admitted_then_sent_then_reported_ended(self, worker_manager: WorkerManager) -> None:
        links = _remote_worker(worker_manager)

        result = await worker_manager.route_to_worker(_execute(), _ENGINE, _WORKER_REQUEST_TOPIC)

        assert result["result_type"] == "ExecuteNodeResultSuccess"
        sent = _sent(links)
        assert [entry["request_type"] for entry in sent] == [
            "AdmitNodeRequest",
            "ExecuteNodeRequest",
            "NodeExecutionEndedRequest",
        ]
        assert sent[0]["request"]["admission_id"] == sent[2]["request"]["admission_id"]

    @pytest.mark.asyncio
    async def test_a_refused_admission_fails_the_node_without_sending_it(self, worker_manager: WorkerManager) -> None:
        links = _remote_worker(worker_manager)
        refusing = _machine(worker_manager, refuse={"AdmitNodeRequest": "its run ended before the machine was free."})
        links.publish.side_effect = refusing.publish.side_effect

        result = await worker_manager.route_to_worker(_execute(), _ENGINE, _WORKER_REQUEST_TOPIC)

        assert result["result_type"] == "ExecuteNodeResultFailure"
        assert "its run ended" in result["result"]["result_details"]
        assert [entry["request_type"] for entry in _sent(refusing)] == [
            "AdmitNodeRequest",
            "NodeExecutionEndedRequest",
        ]

    @pytest.mark.asyncio
    async def test_a_local_workers_node_is_not_admitted(self, worker_manager: WorkerManager) -> None:
        links = _machine(worker_manager)
        worker_manager._workers[_ENGINE] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key="Lib")
        request_client = cast("_FakeRequestClient", worker_manager._tx.request_client)

        async def answer(_event_type: str, payload: str, _topic: str | None) -> None:
            pending = request_client._pending_requests.pop(json.loads(payload)["request_id"])
            pending.future.set_result({"result_type": "ExecuteNodeResultSuccess", "result": {}})

        worker_manager._tx.send_message.side_effect = answer  # type: ignore[union-attr]

        await worker_manager.route_to_worker(_execute(), _ENGINE, _WORKER_REQUEST_TOPIC)

        links.publish.assert_not_awaited()


class TestARunEndsOnEveryMachineItUsed:
    @pytest.mark.asyncio
    async def test_nodes_in_a_run_carry_it_and_its_end_reaches_their_machine(
        self, worker_manager: WorkerManager
    ) -> None:
        links = _remote_worker(worker_manager)

        async with worker_manager.run_scope():
            await worker_manager.route_to_worker(_execute("first"), _ENGINE, _WORKER_REQUEST_TOPIC)
            await worker_manager.route_to_worker(_execute("second"), _ENGINE, _WORKER_REQUEST_TOPIC)

        sent = _sent(links)
        admissions = [entry for entry in sent if entry["request_type"] == "AdmitNodeRequest"]
        run_id = admissions[0]["request"]["run_id"]
        assert run_id is not None
        assert admissions[1]["request"]["run_id"] == run_id
        assert sent[-1]["request_type"] == "EndRunRequest"
        assert sent[-1]["request"]["run_id"] == run_id

    @pytest.mark.asyncio
    async def test_a_run_started_inside_a_run_joins_it(self, worker_manager: WorkerManager) -> None:
        links = _remote_worker(worker_manager)

        async with worker_manager.run_scope(), worker_manager.run_scope():
            await worker_manager.route_to_worker(_execute(), _ENGINE, _WORKER_REQUEST_TOPIC)

        assert [entry["request_type"] for entry in _sent(links)].count("EndRunRequest") == 1

    @pytest.mark.asyncio
    async def test_a_node_outside_any_run_is_sent_without_one(self, worker_manager: WorkerManager) -> None:
        links = _remote_worker(worker_manager)

        await worker_manager.route_to_worker(_execute(), _ENGINE, _WORKER_REQUEST_TOPIC)

        assert _sent(links)[0]["request"]["run_id"] is None
        assert "EndRunRequest" not in [entry["request_type"] for entry in _sent(links)]

    @pytest.mark.asyncio
    async def test_a_reset_tells_each_machine_to_stop_its_workers_here(self, worker_manager: WorkerManager) -> None:
        links = _machine(worker_manager)
        await _start_on_machine(worker_manager)

        await worker_manager.reset_workers()

        released = [entry for entry in _sent(links) if entry["request_type"] == "ReleaseOriginatorWorkersRequest"]
        assert [entry["request"]["originator_session_id"] for entry in released] == [_SESSION]


def _host(worker_manager: WorkerManager, session: str, library: str) -> MagicMock:
    """Record a worker this machine runs for `session`, returning its process."""
    if worker_manager._machine_links is None:
        links = MagicMock()
        links.relay = AsyncMock()
        links.unrelay = AsyncMock()
        worker_manager.attach_machine_links(links)
    proc = _managed_proc_mock()
    worker_key = f"{session}/{library}"
    worker_manager._managed_worker_processes[worker_key] = proc
    worker_manager._hosted_workers.setdefault(session, {})[worker_key] = _HostedWorker(
        worker_engine_id=f"w-{session}", request_topic=f"sessions/{session}/workers/w-{session}/request"
    )
    return proc


class TestFreeingTheMachineBetweenOrchestrators:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("which", "freed"),
        [("others", ["sess-a"]), ("all", ["sess-a", "sess-b"]), ("none", [])],
    )
    async def test_the_setting_decides_whose_workers_are_freed(
        self, worker_manager: WorkerManager, which: str, freed: list[str]
    ) -> None:
        worker_manager.engine.config_manager.get_config_value.side_effect = (  # type: ignore[union-attr]
            lambda key, default=None, **_: which if key == DISTRIBUTED_EXECUTION_RELEASE_WHICH_KEY else default
        )
        _host(worker_manager, "sess-a", _LIBRARY)
        _host(worker_manager, "sess-b", _LIBRARY)

        with patch.object(worker_manager, "_drop_library_named_objects", new=AsyncMock()) as drop:
            await worker_manager._free_for_handoff("sess-b")

        assert [dropped.args[0].worker_engine_id for dropped in drop.await_args_list] == [f"w-{s}" for s in freed]

    @pytest.mark.asyncio
    async def test_recycling_stops_the_worker_without_counting_its_orchestrator_gone(
        self, worker_manager: WorkerManager
    ) -> None:
        worker_manager.engine.config_manager.get_config_value.side_effect = (  # type: ignore[union-attr]
            lambda key, default=None, **_: "recycle" if key == DISTRIBUTED_EXECUTION_RELEASE_HOW_KEY else default
        )
        proc = _host(worker_manager, "sess-a", _LIBRARY)
        queue = MagicMock()
        worker_manager._machine_queue = queue

        with patch.object(worker_manager, "_terminate_via_spawn_loop", new=AsyncMock()) as terminate:
            await worker_manager._free_for_handoff("sess-b")
        await worker_manager._forget_when_exited(f"sess-a/{_LIBRARY}", proc, "w-sess-a")

        terminate.assert_awaited_once()
        queue.originator_gone.assert_not_called()


class TestAnOriginatorWithNoWorkersLeftIsGone:
    @pytest.mark.asyncio
    async def test_its_run_and_waiting_nodes_are_dropped_when_its_last_worker_exits(
        self, worker_manager: WorkerManager
    ) -> None:
        first = _host(worker_manager, "sess-a", "Lib One")
        second = _host(worker_manager, "sess-a", "Lib Two")
        queue = MagicMock()
        worker_manager._machine_queue = queue

        await worker_manager._forget_when_exited("sess-a/Lib One", first, "w-sess-a")
        queue.originator_gone.assert_not_called()
        await worker_manager._forget_when_exited("sess-a/Lib Two", second, "w-sess-a")

        queue.originator_gone.assert_called_once_with("sess-a")

    @pytest.mark.asyncio
    async def test_a_release_request_stops_its_workers(self, worker_manager: WorkerManager) -> None:
        _host(worker_manager, "sess-a", _LIBRARY)

        with patch.object(worker_manager, "_terminate_via_spawn_loop", new=AsyncMock()) as terminate:
            await worker_manager.handle_release_originator_workers_request(
                worker_events.ReleaseOriginatorWorkersRequest(originator_session_id="sess-a")
            )

        terminate.assert_awaited_once()
        assert "sess-a" not in worker_manager._hosted_workers


class TestTheWorkerLimit:
    @pytest.mark.asyncio
    async def test_a_worker_beyond_the_limit_is_refused_naming_it(self, worker_manager: WorkerManager) -> None:
        _machine(worker_manager)
        _register_originator(worker_manager)
        read = worker_manager.engine.config_manager.get_config_value.side_effect  # type: ignore[union-attr]
        worker_manager.engine.config_manager.get_config_value.side_effect = (  # type: ignore[union-attr]
            lambda key, default=None, cast_type=None, **kwargs: (
                1 if key == DISTRIBUTED_EXECUTION_MAX_WORKERS_KEY else read(key, default, cast_type, **kwargs)
            )
        )
        _host(worker_manager, "sess-other", "Other Lib")

        with patch.object(worker_manager, "spawn_worker", new=AsyncMock()) as spawn:
            result = await worker_manager.handle_start_worker_request(
                worker_events.StartWorkerRequest(library_name=_LIBRARY, originator_session_id="origin-sess")
            )

        assert isinstance(result, worker_events.StartWorkerResultFailure)
        assert "1 of its 1" in str(result.result_details)
        spawn.assert_not_awaited()
