"""Tests for WorkerManager.

Covers registration, heartbeat, eviction, unregistration, the relay
filter that keeps internal health-check results off the GUI topic, and
the route_to_worker / pending-future mechanism.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from unittest.mock import ANY, AsyncMock, MagicMock, call, patch

import pytest

from griptape_nodes.api_client.request_client import _PendingRequest
from griptape_nodes.drivers.cloud_credentials import API_KEY_SECRET_NAME, LICENSE_SECRET_NAME
from griptape_nodes.drivers.storage.local_storage_driver import LocalStorageDriver
from griptape_nodes.retained_mode.engine_transports import (
    DISTRIBUTED_EXECUTION_SCHEMA_VERSION,
    EngineNotFoundError,
)
from griptape_nodes.retained_mode.events import converters, worker_events
from griptape_nodes.retained_mode.events.app_events import CurrentProjectChanged
from griptape_nodes.retained_mode.events.base_events import (
    EventRequest,
    EventResultFailure,
    EventResultSuccess,
    RequestSender,
)
from griptape_nodes.retained_mode.events.execution_events import (
    ExecuteNodeRequest,
    ExecuteNodeResultSuccess,
)
from griptape_nodes.retained_mode.events.parameter_events import (
    GetParameterValueRequest,
    GetParameterValueResultSuccess,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry
from griptape_nodes.retained_mode.managers.event_manager import EventManager, _active_request_sender
from griptape_nodes.retained_mode.managers.library.common import LibraryLifecycleState
from griptape_nodes.retained_mode.managers.settings import DISTRIBUTED_EXECUTION_LIBRARY_ENGINES_KEY
from griptape_nodes.retained_mode.managers.worker_manager import (
    _REPORT_ATTEMPTS,
    _STATIC_URL_SETTLE_TIMEOUT_S,
    EngineLease,
    WorkerManager,
    WorkerRegistration,
    _ServedFor,
)
from griptape_nodes.servers.static import ORCHESTRATOR_STATIC_SERVER_BASE_URL_ENV
from griptape_nodes.utils.version_utils import engine_version
from griptape_nodes.worker.wire import WORKER_ENGINE_ID_ENV

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload

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


class _FakeChannel:
    """Minimal stand-in for a worker's `AsyncChannel`: records what is sent through it."""

    def __init__(self) -> None:
        self.notified: list[dict[str, Any]] = []
        self.requested: list[dict[str, Any]] = []

    def notify(self, body: dict[str, Any]) -> None:
        self.notified.append(body)

    async def request(self, body: dict[str, Any], timeout_s: float | None = None) -> dict[str, Any]:  # noqa: ARG002
        self.requested.append(body)
        return {}

    def close(self) -> None:
        pass


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
    # _base_worker_environ builds the child env from the orchestrator's pre-project environ;
    # hand back a real dict so {**base_environ, ...} doesn't choke on a MagicMock.
    gtn.project_manager.get_pre_project_environ.return_value = {}
    # Registration answers from the committed pair; a bare MagicMock cannot be unpacked.
    gtn.project_manager.committed_project.return_value = ("<system-defaults>", 0)
    # A MagicMock reads as a truthy failure reason and would refuse every spawn.
    gtn.library_manager.environment.execution_env_failure_reason.return_value = None
    # Spawn awaits the library's execution environment before starting the process; a bare
    # MagicMock is not awaitable.
    gtn.library_manager.wait_for_execution_env = AsyncMock()
    # No transport to other engines until a test attaches one.
    gtn.can_reach_other_engines = False
    # Outside any run until a test puts the code in one.
    gtn.flow_manager.current_run.return_value = None
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

        async def fake_route(_event: object, worker_engine_id: str) -> dict:
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

        async def fake_route(_event: object, worker_engine_id: str) -> dict:
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

        async def fake_route(_event: object, _worker_engine_id: str) -> dict:
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

        async def never_answers(_event: object, _worker_engine_id: str) -> dict:
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

        async def odd_reply(_event: object, _worker_engine_id: str) -> dict:
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

        async def carrying_route(event: EventRequest, _worker_engine_id: str) -> dict:
            sent.append((event.request.project_id, event.request.generation))  # type: ignore[attr-defined]
            return {"result_type": "ActivateProjectResultSuccess", "result": {}}

        worker_manager.route_to_worker = carrying_route  # type: ignore[method-assign]

        # The event's id is deliberately stale here: the fan-out must ignore it.
        await worker_manager._on_current_project_changed(CurrentProjectChanged(project_id="proj-stale"))

        assert sent == [("proj-9", 4)]


class TestAWorkerChannelEndingReleasesWaiters:
    """A local worker's channel ending is its own liveness signal: gone, it stops its process and releases waiters."""

    @pytest.mark.asyncio
    async def test_removes_it_from_the_registry_and_its_managed_process(self, worker_manager: WorkerManager) -> None:
        proc = _managed_proc_mock()
        worker_manager._workers[_ENGINE] = WorkerRegistration(
            request_topic=_WORKER_REQUEST_TOPIC, worker_key="My Library", process=proc
        )
        worker_manager._managed_worker_processes["My Library"] = proc

        await worker_manager._forget_local_worker(_ENGINE)

        assert _ENGINE not in worker_manager._workers
        assert "My Library" not in worker_manager._managed_worker_processes
        proc.terminate.assert_called_once()

    @pytest.mark.asyncio
    async def test_tolerates_an_unknown_worker(self, worker_manager: WorkerManager) -> None:
        """Forgetting a worker not in the registry must not raise."""
        await worker_manager._forget_local_worker("ghost-engine")

    @pytest.mark.asyncio
    async def test_releases_a_library_whose_worker_left_before_loading_it(self, worker_manager: WorkerManager) -> None:
        """A worker that shuts itself down is not coming back, so waiters must not sit out the load timeout."""
        worker_manager.expect_worker("My Library")
        worker_manager._workers[_ENGINE] = WorkerRegistration(
            request_topic=_WORKER_REQUEST_TOPIC, worker_key="My Library"
        )

        await worker_manager._forget_local_worker(_ENGINE)

        assert worker_manager.has_settled("My Library"), "a waiter would otherwise block until the load timeout"
        assert worker_manager.worker_unavailable_reason("My Library") is not None

    @pytest.mark.asyncio
    async def test_forget_library_drops_the_recorded_reason(self, worker_manager: WorkerManager) -> None:
        """Keyed by a bare name, so it has to be dropped with the library rather than outlive it."""
        worker_manager.note_worker_unavailable("Lib", "the worker stopped responding.")

        worker_manager.forget_library("Lib")

        assert worker_manager.worker_unavailable_reason("Lib") is None


class TestRelayWorkerResult:
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

    def test_none_finds_the_general_purpose_worker_not_a_library_worker(self, worker_manager: WorkerManager) -> None:
        worker_manager._workers["library-engine"] = WorkerRegistration(
            request_topic="library-topic", worker_key="My Library"
        )
        worker_manager._workers[_ENGINE] = WorkerRegistration(request_topic=_WORKER_REQUEST_TOPIC, worker_key=None)

        result = worker_manager.get_worker_for_key(None)

        assert result == (_ENGINE, _WORKER_REQUEST_TOPIC)

    def test_none_finds_nothing_when_only_library_workers_are_registered(self, worker_manager: WorkerManager) -> None:
        worker_manager._workers[_ENGINE] = WorkerRegistration(
            request_topic=_WORKER_REQUEST_TOPIC, worker_key="My Library"
        )

        result = worker_manager.get_worker_for_key(None)

        assert result is None


class TestStartWorkerProcess:
    """`_start_worker_process` is what spawns every worker process now: general-purpose, library, and remote alike."""

    @staticmethod
    def _environ(worker_manager: WorkerManager, worker_key: str | None) -> Callable[[], Awaitable[dict[str, str]]]:
        return lambda: worker_manager._base_worker_environ(worker_key, worker_key)

    @pytest.mark.asyncio
    async def test_duplicate_spawn_is_noop(self, worker_manager: WorkerManager) -> None:
        worker_manager._managed_worker_processes["my-key"] = MagicMock()

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            proc = await worker_manager._start_worker_process(
                ["/usr/bin/gtn", "engine"], "my-key", self._environ(worker_manager, "my-key")
            )

        assert proc is None
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
                worker_manager._start_worker_process(
                    ["/usr/bin/gtn", "engine"], "My Library", self._environ(worker_manager, "My Library")
                ),
                worker_manager._start_worker_process(
                    ["/usr/bin/gtn", "engine"], "My Library", self._environ(worker_manager, "My Library")
                ),
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
            await worker_manager._start_worker_process(
                ["/usr/bin/gtn", "engine"], "My Library", self._environ(worker_manager, "My Library")
            )

        assert "My Library" not in worker_manager._spawns_in_flight

        with patch("asyncio.create_subprocess_exec", return_value=_managed_proc_mock()) as mock_exec:
            await worker_manager._start_worker_process(
                ["/usr/bin/gtn", "engine"], "My Library", self._environ(worker_manager, "My Library")
            )

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
            stale = asyncio.create_task(
                worker_manager._start_worker_process(
                    ["/usr/bin/gtn", "engine"], "My Library", self._environ(worker_manager, "My Library")
                )
            )
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
            await worker_manager._start_worker_process(
                ["/usr/bin/gtn", "engine"], "My Library", self._environ(worker_manager, "My Library")
            )

        mock_exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_spawns_subprocess_with_provided_args(self, worker_manager: WorkerManager) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        args = ["/usr/local/bin/gtn", "engine", "--session-id", "sess-abc", "--library-name", "My Library"]

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await worker_manager._start_worker_process(args, "My Library", self._environ(worker_manager, "My Library"))

        mock_exec.assert_called_once_with(*args, env=ANY, stdout=sys.stdout, stderr=sys.stderr)
        assert worker_manager._managed_worker_processes["My Library"] is mock_proc

    @pytest.mark.asyncio
    async def test_spawn_hands_worker_the_orchestrator_stdio(self, worker_manager: WorkerManager) -> None:
        # Worker logs must land in the same stream as orchestrator logs. Implicit stdio
        # inheritance is POSIX-only: on Windows a redirected stdout (e.g. the desktop app's
        # pipe) never reaches the child unless passed explicitly, so the worker would log
        # to an invisible console instead.
        with patch("asyncio.create_subprocess_exec", return_value=MagicMock()) as mock_exec:
            await worker_manager._start_worker_process(
                ["/usr/bin/gtn", "engine"], "my-key", self._environ(worker_manager, "my-key")
            )

        assert mock_exec.call_args.kwargs["stdout"] is sys.stdout
        assert mock_exec.call_args.kwargs["stderr"] is sys.stderr
        # Worker stdout is a pipe under a GUI-hosted orchestrator; unbuffered output keeps
        # log lines from stalling in Python's block buffer.
        assert mock_exec.call_args.kwargs["env"]["PYTHONUNBUFFERED"] == "1"


class TestBaseWorkerEnviron:
    @pytest.mark.asyncio
    async def test_does_not_pin_the_workspace_into_the_env(self, worker_manager: WorkerManager) -> None:
        """The spawn env must not carry GTN_CONFIG_WORKSPACE_DIRECTORY.

        Workers spawn before any project resolves, so the value at spawn time is the CWD-relative
        placeholder -- and GTN_CONFIG_ outranks the runtime project override, so a worker handed it
        could never follow its orchestrator onto a project's workspace again. The workspace arrives
        with the project the engine that runs it activates on it instead.
        """
        worker_manager.engine.config_manager.workspace_path = Path("/somewhere/else/GriptapeNodes")  # type: ignore[union-attr]

        env = await worker_manager._base_worker_environ("my-key", "my-key")

        assert "GTN_CONFIG_WORKSPACE_DIRECTORY" not in env


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
            await worker_manager._start_worker_process(
                ["/usr/bin/gtn", "engine"],
                "My Library",
                lambda: worker_manager._base_worker_environ("My Library", "My Library"),
            )

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
            spawn = asyncio.create_task(
                worker_manager._start_worker_process(
                    ["/usr/bin/gtn", "engine"], "Lib A", lambda: worker_manager._base_worker_environ("Lib A", "Lib A")
                )
            )
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

        with patch.object(worker_manager, "_spawn_library_worker", new=AsyncMock()):
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

        with patch.object(worker_manager, "_spawn_library_worker", new=AsyncMock()) as spawn:
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

        with patch.object(worker_manager, "_spawn_library_worker", new=AsyncMock()) as spawn:
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

        with patch.object(worker_manager, "_spawn_library_worker", new=AsyncMock()) as spawn:
            await worker_manager.handle_start_worker_request(worker_events.StartWorkerRequest(library_name="Lib"))
            await asyncio.sleep(0)

        spawn.assert_not_awaited()
        assert worker_manager.has_settled("Lib")

    @pytest.mark.asyncio
    async def test_a_request_for_a_registered_worker_starts_nothing(self, worker_manager: WorkerManager) -> None:
        worker_manager._workers["eng-1"] = WorkerRegistration(
            request_topic=f"sessions/{_SESSION}/workers/eng-1/request", worker_key="Lib"
        )

        with patch.object(worker_manager, "_spawn_library_worker", new=AsyncMock()) as spawn:
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

        with patch.object(worker_manager, "_spawn_library_worker", side_effect=wait_for_a_session):
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

        with patch.object(worker_manager, "_spawn_library_worker", side_effect=claim_then_park):
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


class TestSpawnLibraryWorker:
    @pytest.mark.asyncio
    async def test_spawns_without_waiting_for_a_session(self, worker_manager: WorkerManager) -> None:
        """A worker this engine runs for itself joins no session, so none has to exist first."""
        worker_manager.engine.get_session_id.return_value = None  # type: ignore[union-attr]

        with patch.object(worker_manager, "_spawn_local_worker", new=AsyncMock()) as spawn:
            await worker_manager._spawn_library_worker("My Library")

        spawn.assert_awaited_once()
        assert spawn.await_args_list[0].args[0][1:] == ["-m", "griptape_nodes.worker", "--library-name", "My Library"]
        assert not worker_manager._session_ready_event.is_set()


class TestGetTopicsToSubscribe:
    def test_includes_base_request_topic(self, worker_manager: WorkerManager) -> None:
        assert "request" in worker_manager.get_topics_to_subscribe()

    def test_includes_engine_specific_topic(self, worker_manager: WorkerManager) -> None:
        assert f"engines/{_ENGINE}/request" in worker_manager.get_topics_to_subscribe()

    def test_includes_session_request_topic(self, worker_manager: WorkerManager) -> None:
        topics = worker_manager.get_topics_to_subscribe()

        assert f"sessions/{_SESSION}/request" in topics

    def test_excludes_per_worker_topic(self, worker_manager: WorkerManager) -> None:
        topics = worker_manager.get_topics_to_subscribe()

        assert f"sessions/{_SESSION}/workers/{_ENGINE}/request" not in topics

    def test_no_session_excludes_session_topic(self, worker_manager: WorkerManager) -> None:
        worker_manager.engine.get_session_id.return_value = None  # type: ignore[union-attr]

        topics = worker_manager.get_topics_to_subscribe()

        assert not any("sessions/" in t for t in topics)


class TestForwardEventToWorker:
    @pytest.mark.asyncio
    async def test_notifies_a_local_workers_channel(self, worker_manager: WorkerManager) -> None:
        channel = _FakeChannel()
        worker_manager._workers[_ENGINE] = WorkerRegistration(
            request_topic="",
            worker_key=None,
            channel=channel,  # type: ignore[arg-type]
        )
        event = EventRequest(request=ExecuteNodeRequest(node_name="TestNode", parameter_values={}))

        await worker_manager.forward_event_to_worker(event, worker_engine_id=_ENGINE)

        assert len(channel.notified) == 1
        assert channel.notified[0]["payload"]["request_type"] == "ExecuteNodeRequest"

    @pytest.mark.asyncio
    async def test_is_a_noop_for_an_unknown_worker(self, worker_manager: WorkerManager) -> None:
        event = EventRequest(request=ExecuteNodeRequest(node_name="TestNode", parameter_values={}))

        await worker_manager.forward_event_to_worker(event, worker_engine_id="ghost-engine")


class TestRelayTopic:
    """`relay_worker_result` sends a worker's result to the session topic, whatever workflow it names."""

    @staticmethod
    def _payload(open_workflow_id: str | None) -> dict:
        return {
            "event_type": "EventResultSuccess",
            "result_type": "SomeOtherResultSuccess",
            "result": {},
            "response_topic": _WORKER_RESPONSE_TOPIC,
            "open_workflow_id": open_workflow_id,
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("open_workflow_id", ["wf-1", None])
    async def test_a_result_goes_to_the_session_topic(
        self, worker_manager: WorkerManager, open_workflow_id: str | None
    ) -> None:
        await worker_manager.relay_worker_result(self._payload(open_workflow_id))

        topic = f"sessions/{_SESSION}/response"
        assert worker_manager._tx.send_message.call_args.args[2] == topic  # type: ignore[union-attr]
        relayed = json.loads(worker_manager._tx.send_message.call_args.args[1])  # type: ignore[union-attr]
        assert relayed["response_topic"] == topic
        assert relayed["open_workflow_id"] == open_workflow_id

    @pytest.mark.asyncio
    async def test_with_no_session_the_topic_is_the_engines(self, worker_manager: WorkerManager) -> None:
        worker_manager.engine.get_session_id.return_value = None  # type: ignore[union-attr]

        await worker_manager.relay_worker_result(self._payload("wf-1"))

        assert worker_manager._tx.send_message.call_args.args[2] == f"engines/{_ENGINE}/response"  # type: ignore[union-attr]


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

        worker_a, worker_b = _FakeChannel(), _FakeChannel()
        worker_manager._workers["eng-a"] = WorkerRegistration(
            request_topic="",
            worker_key=None,
            channel=worker_a,  # type: ignore[arg-type]
        )
        worker_manager._workers["eng-b"] = WorkerRegistration(
            request_topic="",
            worker_key=None,
            channel=worker_b,  # type: ignore[arg-type]
        )
        event = EventRequest(request=ReloadConfigRequest())

        await worker_manager.broadcast_to_workers(event)

        assert len(worker_a.notified) == 1
        assert len(worker_b.notified) == 1


class TestScheduleBroadcast:
    @pytest.mark.asyncio
    async def test_fans_out_request_to_each_registered_worker(self, worker_manager: WorkerManager) -> None:
        from griptape_nodes.app.worker_routing import ReloadConfigRequest

        channel = _FakeChannel()
        worker_manager._workers[_ENGINE] = WorkerRegistration(
            request_topic="",
            worker_key=None,
            channel=channel,  # type: ignore[arg-type]
        )

        worker_manager.schedule_broadcast(ReloadConfigRequest)
        # create_task on the running loop -- let it run.
        await asyncio.sleep(0)

        assert len(channel.notified) == 1
        assert channel.notified[0]["payload"]["request_type"] == "ReloadConfigRequest"

    @pytest.mark.asyncio
    async def test_no_workers_is_noop(self, worker_manager: WorkerManager) -> None:
        from griptape_nodes.app.worker_routing import ReloadConfigRequest

        worker_manager.schedule_broadcast(ReloadConfigRequest)
        await asyncio.sleep(0)

        worker_manager._tx.send_message.assert_not_called()  # type: ignore[union-attr]


class TestWorkerManagerDomainEventListeners:
    """WorkerManager owns the bridge from domain events to worker fan-out.

    ConfigManager emits ConfigChanged on successful state mutations;
    WorkerManager translates it into a ReloadConfigRequest broadcast. The managers
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
        channel = _FakeChannel()
        wm._workers[_ENGINE] = WorkerRegistration(request_topic="", worker_key=None, channel=channel)  # type: ignore[arg-type]

        wm._event_manager.broadcast_app_event(ConfigChanged(key="x.y", old_value=None, new_value="v"))
        await asyncio.sleep(0)

        assert len(channel.notified) == 1
        assert channel.notified[0]["payload"]["request_type"] == "ReloadConfigRequest"

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
            await worker_manager._start_worker_process(
                [sys.executable, "-c", ""],
                "My Library",
                lambda: worker_manager._base_worker_environ("My Library", "My Library"),
            )

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
            await worker_manager._start_worker_process(
                [sys.executable, "-c", ""],
                "My Library",
                lambda: worker_manager._base_worker_environ("My Library", "My Library"),
            )

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
            await worker_manager._start_worker_process(
                [sys.executable, "-c", ""],
                "Light Library",
                lambda: worker_manager._base_worker_environ("Light Library", "Light Library"),
            )

        assert "PYTHONPATH" not in spawn.call_args.kwargs["env"]


_HOST_ENGINE = "gpu-01-engine-id"
_LIBRARY = "Heavy Library"
_LICENSE = "artist-license"
_ARTIST_ENGINE = "artist-engine-id"
_ARTIST_SESSION = "artist-sess"


class _FakeEngineTransports:
    """An in-process `EngineTransports` whose other engine answers like an engine that runs workers.

    Each request succeeds unless `refuse` names its type, in which case it fails with that reason.
    `answers` queues results to return, per request type, ahead of the default answer, in the wire
    shape (`result_type` and `result`), read back into payloads as the app transport does.
    """

    def __init__(self, *, refuse: dict[str, str] | None = None) -> None:
        self.sent: list[tuple[str, RequestPayload, float | None]] = []
        self.refuse = refuse or {}
        self.answers: dict[str, list[dict]] = {}
        self.not_found: str | None = None
        self.blocked: dict[str, asyncio.Event] = {}
        self.arrived: dict[str, asyncio.Event] = {}
        self.link_lost: Callable[[str], Awaitable[None]] | None = None

    async def request(
        self,
        engine_id: str,
        request: RequestPayload,
        *,
        timeout_s: float | None,
        open_workflow_id: str | None = None,  # noqa: ARG002
        worker_engine_id: str | None = None,  # noqa: ARG002
    ) -> ResultPayload:
        self.sent.append((engine_id, request, timeout_s))
        if self.not_found is not None:
            raise EngineNotFoundError(self.not_found)
        request_type = type(request).__name__
        self.arrived.setdefault(request_type, asyncio.Event()).set()
        if request_type in self.blocked:
            await self.blocked[request_type].wait()
        queued = self.answers.get(request_type)
        if queued:
            return _read_back(queued.pop(0))
        if request_type in self.refuse:
            return _read_back(
                {
                    "result_type": request_type.replace("Request", "ResultFailure"),
                    "result": {"result_details": self.refuse[request_type]},
                }
            )
        result: dict[str, object] = {"result_details": "ok"}
        if request_type == "OfferSessionForWorkersRequest":
            result["engine_id"] = _HOST_ENGINE
        return _read_back({"result_type": request_type.replace("Request", "ResultSuccess"), "result": result})

    def on_engine_lost(self, callback: Callable[[str], Awaitable[None]]) -> None:
        self.link_lost = callback

    def sent_types(self) -> list[str]:
        return [type(request).__name__ for _engine_id, request, _timeout_s in self.sent]

    def sent_of(self, request_type: type[RequestPayload]) -> list[RequestPayload]:
        return [request for _engine_id, request, _timeout_s in self.sent if isinstance(request, request_type)]


def _read_back(raw: dict) -> ResultPayload:
    """A result in the wire shape, read back into its payload type."""
    result_type = PayloadRegistry.get_type(raw["result_type"])
    assert result_type is not None, raw["result_type"]
    return cast("ResultPayload", converters.engine.structure(raw["result"], result_type))


def _assign_libraries(worker_manager: WorkerManager, libraries: dict[str, str]) -> None:
    """Assign libraries to engines in the distribution settings."""
    read_default = worker_manager.engine.config_manager.get_config_value.side_effect  # type: ignore[union-attr]

    def get_config_value(key: str, default: object = None, cast_type: type | None = None, **kwargs: object) -> object:
        if key == DISTRIBUTED_EXECUTION_LIBRARY_ENGINES_KEY:
            return libraries
        return read_default(key, default, cast_type, **kwargs)

    worker_manager.engine.config_manager.get_config_value.side_effect = get_config_value  # type: ignore[union-attr]


def _host_engine(worker_manager: WorkerManager, *, refuse: dict[str, str] | None = None) -> _FakeEngineTransports:
    """Assign `_LIBRARY` to `_HOST_ENGINE` and attach links whose other engine answers, refusing what `refuse` names."""
    _assign_libraries(worker_manager, {_LIBRARY: _HOST_ENGINE})
    engine = worker_manager.engine
    engine.secrets_manager.get_secret.return_value = _LICENSE  # type: ignore[union-attr]
    engine.library_manager.get_library_info_by_library_name.return_value = MagicMock(  # type: ignore[union-attr]
        library_version="1.0.0", lifecycle_state=LibraryLifecycleState.LOADED
    )
    links = _FakeEngineTransports(refuse=refuse)
    # As Engine.attach_engine_transports does: requests go through the transports, and a lost connection reaches here.
    engine.can_reach_other_engines = True  # type: ignore[misc]
    engine.request = links.request  # type: ignore[method-assign]
    links.on_engine_lost(worker_manager.on_engine_lost)
    return links


async def _start_on_host_engine(worker_manager: WorkerManager, library_name: str = _LIBRARY) -> None:
    with patch.object(worker_manager, "_orchestrator_static_server_base_url", new=AsyncMock(return_value=None)):
        await worker_manager.handle_start_worker_request(worker_events.StartWorkerRequest(library_name=library_name))
        await worker_manager._requested_spawns[library_name]


def _started_worker_id(links: _FakeEngineTransports) -> str:
    """The engine ID the last StartWorkerForEngineRequest named."""
    start = cast(
        "worker_events.StartWorkerForEngineRequest", links.sent_of(worker_events.StartWorkerForEngineRequest)[-1]
    )
    return cast("str", start.worker_engine_id)


def _worker_proc_mock(returncode: int = 0, exits: asyncio.Event | None = None) -> MagicMock:
    """A worker process that exits with `returncode`, once `exits` is set when one is given."""
    proc = MagicMock()

    async def wait() -> int:
        if exits is not None:
            await exits.wait()
        return returncode

    proc.wait = wait
    return proc


class TestStartingAWorkerOnAnotherEngine:
    @pytest.mark.asyncio
    async def test_the_session_registers_then_asks_the_engine_to_start_a_worker_named_here(
        self, worker_manager: WorkerManager
    ) -> None:
        links = _host_engine(worker_manager)

        with patch.object(worker_manager, "_spawn_library_worker", new=AsyncMock()) as spawn:
            await _start_on_host_engine(worker_manager)

        spawn.assert_not_awaited()
        registration, start = links.sent
        assert registration[0] == _HOST_ENGINE
        assert isinstance(registration[1], worker_events.OfferSessionForWorkersRequest)
        assert registration[1].engine_version == engine_version
        assert registration[1].library_versions == {_LIBRARY: "1.0.0"}
        # The fixture's secrets answer every name with _LICENSE, so a credential sent would show up here.
        assert _LICENSE not in vars(registration[1]).values()
        assert start[0] == _HOST_ENGINE
        assert isinstance(start[1], worker_events.StartWorkerForEngineRequest)
        assert _started_worker_id(links) in worker_manager._starts_asked_of_hosts
        assert {timeout_s for _engine_id, _request, timeout_s in links.sent} == {60.0}
        assert worker_manager.worker_unavailable_reason(_LIBRARY) is None

    @pytest.mark.asyncio
    async def test_a_start_the_engine_answered_counts_as_starting_until_the_worker_registers(
        self, worker_manager: WorkerManager
    ) -> None:
        """A second start in that gap would replace the readiness gate a run may already wait on."""
        links = _host_engine(worker_manager)
        await _start_on_host_engine(worker_manager)

        assert worker_manager.is_worker_running_or_starting(_LIBRARY)
        await worker_manager.handle_start_worker_request(worker_events.StartWorkerRequest(library_name=_LIBRARY))

        assert links.sent_types() == ["OfferSessionForWorkersRequest", "StartWorkerForEngineRequest"]

    @pytest.mark.asyncio
    async def test_a_refused_registration_makes_the_library_unavailable_with_the_reason(
        self, worker_manager: WorkerManager
    ) -> None:
        links = _host_engine(worker_manager, refuse={"OfferSessionForWorkersRequest": "engine version 9.9.9 differs"})

        await _start_on_host_engine(worker_manager)

        assert links.sent_types() == ["OfferSessionForWorkersRequest"]
        reason = worker_manager.worker_unavailable_reason(_LIBRARY)
        assert reason is not None
        assert "engine version 9.9.9 differs" in reason
        assert worker_manager.has_settled(_LIBRARY)

    @pytest.mark.asyncio
    async def test_a_refused_registration_is_asked_again_by_the_next_start(self, worker_manager: WorkerManager) -> None:
        links = _host_engine(worker_manager, refuse={"OfferSessionForWorkersRequest": "the engine was busy restarting"})
        await _start_on_host_engine(worker_manager)
        links.refuse.clear()

        await _start_on_host_engine(worker_manager)

        assert links.sent_types() == [
            "OfferSessionForWorkersRequest",
            "OfferSessionForWorkersRequest",
            "StartWorkerForEngineRequest",
        ]

    @pytest.mark.asyncio
    async def test_a_engine_that_forgot_this_session_is_registered_with_again(
        self, worker_manager: WorkerManager
    ) -> None:
        """An engine that restarted no longer knows the session its earlier registration accepted."""
        links = _host_engine(worker_manager)
        links.answers["StartWorkerForEngineRequest"] = [
            {
                "result_type": "StartWorkerForEngineResultFailure",
                "result": {"result_details": "never registered", "session_unknown": True},
            }
        ]

        await _start_on_host_engine(worker_manager)

        assert links.sent_types() == [
            "OfferSessionForWorkersRequest",
            "StartWorkerForEngineRequest",
            "OfferSessionForWorkersRequest",
            "StartWorkerForEngineRequest",
        ]
        assert worker_manager.worker_unavailable_reason(_LIBRARY) is None
        assert list(worker_manager._starts_asked_of_hosts) == [_started_worker_id(links)]

    @pytest.mark.asyncio
    async def test_a_worker_is_known_to_run_on_its_engine_before_its_start_is_answered(
        self, worker_manager: WorkerManager
    ) -> None:
        """The worker can register, and need its traffic carried, before the start's answer arrives."""
        links = _host_engine(worker_manager)
        links.blocked["StartWorkerForEngineRequest"] = asyncio.Event()
        links.arrived["StartWorkerForEngineRequest"] = asyncio.Event()
        with patch.object(worker_manager, "_orchestrator_static_server_base_url", new=AsyncMock(return_value=None)):
            await worker_manager.handle_start_worker_request(worker_events.StartWorkerRequest(library_name=_LIBRARY))
            await links.arrived["StartWorkerForEngineRequest"].wait()

            assert worker_manager.worker_host(_started_worker_id(links)) == _HOST_ENGINE

            links.blocked["StartWorkerForEngineRequest"].set()
            await worker_manager._requested_spawns[_LIBRARY]

    @pytest.mark.asyncio
    async def test_a_refused_start_makes_the_library_unavailable_and_forgets_the_worker(
        self, worker_manager: WorkerManager
    ) -> None:
        links = _host_engine(worker_manager, refuse={"StartWorkerForEngineRequest": "no room"})

        await _start_on_host_engine(worker_manager)

        reason = worker_manager.worker_unavailable_reason(_LIBRARY)
        assert reason is not None
        assert "no room" in reason
        assert worker_manager._starts_asked_of_hosts == {}
        assert worker_manager.worker_host(_started_worker_id(links)) is None
        assert not worker_manager.is_worker_running_or_starting(_LIBRARY)

    @pytest.mark.asyncio
    async def test_a_start_refused_for_capacity_is_asked_again_by_the_library_s_next_node(
        self, worker_manager: WorkerManager
    ) -> None:
        """The node waiting now fails with the reason, and a slot may be free by the next one."""
        _host_engine(worker_manager)
        full = worker_events.StartWorkerForEngineResultFailure(result_details="no room", at_capacity=True)

        with patch.object(worker_manager, "_ask_engine_to_start_worker", new=AsyncMock(return_value=full)):
            await _start_on_host_engine(worker_manager)
        assert "no room" in str(worker_manager.worker_unavailable_reason(_LIBRARY))

        with patch.object(worker_manager, "handle_start_worker_request", new=AsyncMock()) as start:
            await worker_manager._ask_host_to_restart_stopped_worker(_LIBRARY)
        start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_capacity_refusal_followed_by_another_reason_is_not_asked_again(
        self, worker_manager: WorkerManager
    ) -> None:
        _host_engine(worker_manager)
        full = worker_events.StartWorkerForEngineResultFailure(result_details="no room", at_capacity=True)
        with patch.object(worker_manager, "_ask_engine_to_start_worker", new=AsyncMock(return_value=full)):
            await _start_on_host_engine(worker_manager)

        worker_manager.note_worker_unavailable(_LIBRARY, "the connection to gpu-01 was lost.")
        with patch.object(worker_manager, "handle_start_worker_request", new=AsyncMock()) as start:
            await worker_manager._ask_host_to_restart_stopped_worker(_LIBRARY)

        start.assert_not_awaited()
        assert worker_manager.worker_unavailable_reason(_LIBRARY) == "the connection to gpu-01 was lost."

    @pytest.mark.asyncio
    async def test_a_start_refused_for_another_reason_is_not_asked_again(self, worker_manager: WorkerManager) -> None:
        _host_engine(worker_manager, refuse={"StartWorkerForEngineRequest": "not installed"})
        await _start_on_host_engine(worker_manager)

        with patch.object(worker_manager, "handle_start_worker_request", new=AsyncMock()) as start:
            await worker_manager._ask_host_to_restart_stopped_worker(_LIBRARY)
        start.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_engine_that_cannot_be_found_makes_the_library_unavailable_and_is_asked_again(
        self, worker_manager: WorkerManager
    ) -> None:
        links = _host_engine(worker_manager)
        links.not_found = "gpu-01 is not running."

        await _start_on_host_engine(worker_manager)

        assert worker_manager.worker_unavailable_reason(_LIBRARY) == "gpu-01 is not running."
        assert links.sent_types() == ["OfferSessionForWorkersRequest"]
        assert worker_manager._engine_registrations == {}

    @pytest.mark.asyncio
    async def test_a_engine_that_stops_answering_a_start_makes_the_library_unavailable(
        self, worker_manager: WorkerManager
    ) -> None:
        links = _host_engine(worker_manager)
        answer = links.request

        async def leave_the_start_unanswered(
            engine_id: str, request: RequestPayload, *, timeout_s: float | None
        ) -> ResultPayload:
            if type(request).__name__ == "StartWorkerForEngineRequest":
                msg = "gpu-01 did not answer within 60 seconds."
                raise EngineNotFoundError(msg)
            return await answer(engine_id, request, timeout_s=timeout_s)

        worker_manager.engine.request = leave_the_start_unanswered  # type: ignore[method-assign]

        await _start_on_host_engine(worker_manager)

        assert worker_manager.worker_unavailable_reason(_LIBRARY) == "gpu-01 did not answer within 60 seconds."
        assert worker_manager._starts_asked_of_hosts == {}

    @pytest.mark.asyncio
    async def test_a_engine_that_answers_as_another_engine_is_refused_naming_it(
        self, worker_manager: WorkerManager
    ) -> None:
        links = _host_engine(worker_manager)
        links.answers["OfferSessionForWorkersRequest"] = [
            {
                "result_type": "OfferSessionForWorkersResultSuccess",
                "result": {"engine_id": "someone-else", "result_details": "ok"},
            }
        ]

        await _start_on_host_engine(worker_manager)

        reason = worker_manager.worker_unavailable_reason(_LIBRARY)
        assert reason is not None
        assert "someone-else" in reason
        assert links.sent_types() == ["OfferSessionForWorkersRequest"]

    @pytest.mark.asyncio
    async def test_every_start_registers_anew_so_the_engine_checks_the_libraries_as_they_are_now(
        self, worker_manager: WorkerManager
    ) -> None:
        links = _host_engine(worker_manager)
        _assign_libraries(worker_manager, {_LIBRARY: _HOST_ENGINE, "Other": _HOST_ENGINE})

        await _start_on_host_engine(worker_manager, _LIBRARY)
        await _start_on_host_engine(worker_manager, "Other")

        assert links.sent_types() == [
            "OfferSessionForWorkersRequest",
            "StartWorkerForEngineRequest",
            "OfferSessionForWorkersRequest",
            "StartWorkerForEngineRequest",
        ]

    @pytest.mark.asyncio
    async def test_without_engine_transports_the_library_is_unavailable_rather_than_run_here(
        self, worker_manager: WorkerManager
    ) -> None:
        """Running it here would run it in the one place it was assigned away from."""
        _assign_libraries(worker_manager, {_LIBRARY: _HOST_ENGINE})

        with patch.object(worker_manager, "_spawn_library_worker", new=AsyncMock()) as spawn:
            await _start_on_host_engine(worker_manager)

        spawn.assert_not_awaited()
        reason = worker_manager.worker_unavailable_reason(_LIBRARY)
        assert reason is not None
        assert _HOST_ENGINE in reason

    @pytest.mark.asyncio
    async def test_a_library_with_no_assignment_still_spawns_here(self, worker_manager: WorkerManager) -> None:
        links = _host_engine(worker_manager)

        with patch.object(worker_manager, "_spawn_local_worker", new=AsyncMock()) as spawn:
            await _start_on_host_engine(worker_manager, "Local Lib")

        spawn.assert_awaited_once()
        assert links.sent == []

    @pytest.mark.asyncio
    async def test_a_library_assigned_to_this_engine_spawns_here(self, worker_manager: WorkerManager) -> None:
        """As when engines that share a workspace share its config file."""
        links = _host_engine(worker_manager)
        worker_manager.engine.get_engine_id.return_value = _HOST_ENGINE  # type: ignore[union-attr]

        with patch.object(worker_manager, "_spawn_local_worker", new=AsyncMock()) as spawn:
            await _start_on_host_engine(worker_manager)

        spawn.assert_awaited_once()
        assert links.sent == []

    @pytest.mark.asyncio
    async def test_a_start_withdrawn_while_the_engine_answers_tells_it_to_stop_that_worker(
        self, worker_manager: WorkerManager
    ) -> None:
        links = _host_engine(worker_manager)
        links.blocked["StartWorkerForEngineRequest"] = asyncio.Event()
        links.arrived["StartWorkerForEngineRequest"] = asyncio.Event()
        with patch.object(worker_manager, "_orchestrator_static_server_base_url", new=AsyncMock(return_value=None)):
            await worker_manager.handle_start_worker_request(worker_events.StartWorkerRequest(library_name=_LIBRARY))
            start = worker_manager._requested_spawns[_LIBRARY]
            await links.arrived["StartWorkerForEngineRequest"].wait()
            worker_engine_id = _started_worker_id(links)

            await worker_manager.stop_worker_for_library(_LIBRARY)
            await asyncio.gather(start, return_exceptions=True)
            await asyncio.gather(*worker_manager._stop_requests)

        (stop,) = links.sent_of(worker_events.StopWorkerForEngineRequest)
        assert stop == worker_events.StopWorkerForEngineRequest(worker_engine_id=worker_engine_id)
        assert worker_manager._starts_asked_of_hosts == {}

    @pytest.mark.asyncio
    async def test_a_start_that_fails_unexpectedly_is_forgotten_and_that_worker_stopped(
        self, worker_manager: WorkerManager
    ) -> None:
        """Otherwise the library reads as still starting, and is never started again this session."""
        links = _host_engine(worker_manager)
        answer = links.request

        async def fail_the_start(engine_id: str, request: RequestPayload, *, timeout_s: float | None) -> ResultPayload:
            result = await answer(engine_id, request, timeout_s=timeout_s)
            if type(request).__name__ == "StartWorkerForEngineRequest":
                msg = "the link's driver is not running"
                raise RuntimeError(msg)
            return result

        worker_manager.engine.request = fail_the_start  # type: ignore[method-assign]
        with patch.object(worker_manager, "_orchestrator_static_server_base_url", new=AsyncMock(return_value=None)):
            await worker_manager.handle_start_worker_request(worker_events.StartWorkerRequest(library_name=_LIBRARY))
            await asyncio.gather(worker_manager._requested_spawns[_LIBRARY], return_exceptions=True)
            await asyncio.gather(*worker_manager._stop_requests)

        assert worker_manager._starts_asked_of_hosts == {}
        assert links.sent_of(worker_events.StopWorkerForEngineRequest)

    @pytest.mark.asyncio
    async def test_a_lost_link_settles_the_starts_sent_to_that_engine(self, worker_manager: WorkerManager) -> None:
        """Otherwise a start the other engine accepted waits out the library load timeout for a worker that never comes."""
        links = _host_engine(worker_manager)
        await _start_on_host_engine(worker_manager)
        assert links.link_lost is not None

        await links.link_lost(_HOST_ENGINE)

        assert worker_manager._starts_asked_of_hosts == {}
        reason = worker_manager.worker_unavailable_reason(_LIBRARY)
        assert reason is not None
        assert _HOST_ENGINE in reason


class TestARemoteWorkerRegisters:
    @pytest.mark.asyncio
    async def test_a_worker_this_engine_asked_for_is_taken_into_service_as_running_on_its_engine(
        self, worker_manager: WorkerManager
    ) -> None:
        links = _host_engine(worker_manager)
        await _start_on_host_engine(worker_manager)
        worker_engine_id = _started_worker_id(links)

        result = await worker_manager.handle_report_remote_worker_ready_request(
            worker_events.ReportRemoteWorkerReadyRequest(worker_engine_id=worker_engine_id)
        )

        assert isinstance(result, worker_events.ReportRemoteWorkerReadyResultSuccess), result.result_details
        assert worker_manager._workers[worker_engine_id].host_engine_id == _HOST_ENGINE
        assert worker_manager.worker_host(worker_engine_id) == _HOST_ENGINE
        assert worker_manager._starts_asked_of_hosts == {}

    @pytest.mark.asyncio
    async def test_a_worker_this_engine_did_not_ask_for_is_refused(self, worker_manager: WorkerManager) -> None:
        _host_engine(worker_manager)

        result = await worker_manager.handle_report_remote_worker_ready_request(
            worker_events.ReportRemoteWorkerReadyRequest(worker_engine_id="stranger")
        )

        assert isinstance(result, worker_events.ReportRemoteWorkerReadyResultFailure)
        assert worker_manager._workers == {}
        assert worker_manager.worker_host("stranger") is None

    @pytest.mark.asyncio
    async def test_a_reset_forgets_remote_workers_and_starts(self, worker_manager: WorkerManager) -> None:
        _host_engine(worker_manager)
        await _start_on_host_engine(worker_manager)
        worker_manager._workers[_ENGINE] = WorkerRegistration(
            request_topic=_WORKER_REQUEST_TOPIC, worker_key="Other", host_engine_id=_HOST_ENGINE
        )

        await worker_manager.reset_workers()

        assert worker_manager._workers == {}
        assert worker_manager._starts_asked_of_hosts == {}
        assert not worker_manager.is_worker_running_or_starting(_LIBRARY)


def _exited(
    worker_engine_id: str, reason: Literal["stopped", "exited"], exit_code: int = 1
) -> worker_events.ReportWorkerExitedRequest:
    return worker_events.ReportWorkerExitedRequest(
        worker_engine_id=worker_engine_id, exit_code=exit_code, reason=reason
    )


class TestARemoteWorkerExits:
    @pytest.mark.asyncio
    async def test_a_start_that_exits_before_registering_settles_the_library_with_the_reason(
        self, worker_manager: WorkerManager
    ) -> None:
        """Otherwise a run waits out the whole library load timeout for a worker that is gone."""
        links = _host_engine(worker_manager)
        await _start_on_host_engine(worker_manager)
        worker_engine_id = _started_worker_id(links)

        result = await _sent_by(
            _FROM_HOST_ENGINE,
            worker_manager.handle_report_worker_exited_request,
            _exited(worker_engine_id, "exited", exit_code=3),
        )

        assert isinstance(result, worker_events.ReportWorkerExitedResultSuccess), result.result_details
        assert worker_manager.has_settled(_LIBRARY)
        reason = worker_manager.worker_unavailable_reason(_LIBRARY)
        assert reason is not None
        assert "exit code 3" in reason
        assert _HOST_ENGINE in reason
        assert not worker_manager.is_worker_running_or_starting(_LIBRARY)

    @pytest.mark.asyncio
    async def test_a_registered_worker_that_exits_is_removed_at_once(self, worker_manager: WorkerManager) -> None:
        _host_engine(worker_manager)
        registration = WorkerRegistration(
            request_topic=_WORKER_REQUEST_TOPIC, worker_key=_LIBRARY, host_engine_id=_HOST_ENGINE
        )
        worker_manager._workers[_ENGINE] = registration

        await _sent_by(
            _FROM_HOST_ENGINE, worker_manager.handle_report_worker_exited_request, _exited(_ENGINE, "exited")
        )

        assert _ENGINE not in worker_manager._workers
        # Anything awaiting this worker through `route_to_worker` watches `gone`, not a tracked future.
        assert registration.gone.is_set()
        assert worker_manager.worker_unavailable_reason(_LIBRARY) is not None

    @pytest.mark.asyncio
    async def test_a_loaded_worker_the_engine_stopped_leaves_the_library_free_to_start_again(
        self, worker_manager: WorkerManager
    ) -> None:
        _host_engine(worker_manager)
        worker_manager._workers[_ENGINE] = WorkerRegistration(
            request_topic=_WORKER_REQUEST_TOPIC, worker_key=_LIBRARY, host_engine_id=_HOST_ENGINE
        )

        await _sent_by(
            _FROM_HOST_ENGINE,
            worker_manager.handle_report_worker_exited_request,
            _exited(_ENGINE, "stopped", exit_code=0),
        )

        assert _ENGINE not in worker_manager._workers
        assert worker_manager.worker_unavailable_reason(_LIBRARY) is None
        assert not worker_manager.is_worker_running_or_starting(_LIBRARY)

    @pytest.mark.asyncio
    async def test_a_worker_the_engine_stopped_is_started_again_only_when_asked_to(
        self, worker_manager: WorkerManager
    ) -> None:
        """Before a lease, starting it would load the library there while another run holds that engine."""
        _host_engine(worker_manager)
        worker_manager._workers[_ENGINE] = WorkerRegistration(
            request_topic=_WORKER_REQUEST_TOPIC, worker_key=_LIBRARY, host_engine_id=_HOST_ENGINE
        )
        await _sent_by(
            _FROM_HOST_ENGINE,
            worker_manager.handle_report_worker_exited_request,
            _exited(_ENGINE, "stopped", exit_code=0),
        )

        with patch.object(worker_manager, "handle_start_worker_request", new=AsyncMock()) as start:
            await asyncio.wait_for(worker_manager.wait_until_executable(_LIBRARY, start_stopped=False), timeout=1.0)
            start.assert_not_awaited()
            await worker_manager.wait_until_executable(_LIBRARY)
        start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_exit_reported_twice_is_recorded_once(self, worker_manager: WorkerManager) -> None:
        links = _host_engine(worker_manager)
        await _start_on_host_engine(worker_manager)
        worker_engine_id = _started_worker_id(links)
        await _sent_by(
            _FROM_HOST_ENGINE, worker_manager.handle_report_worker_exited_request, _exited(worker_engine_id, "exited")
        )

        reason = worker_manager.worker_unavailable_reason(_LIBRARY)

        result = await _sent_by(
            _FROM_HOST_ENGINE, worker_manager.handle_report_worker_exited_request, _exited(worker_engine_id, "exited")
        )

        assert isinstance(result, worker_events.ReportWorkerExitedResultSuccess)
        assert "nothing to record" in str(result.result_details)
        assert worker_manager.worker_unavailable_reason(_LIBRARY) == reason

    @pytest.mark.asyncio
    async def test_an_exit_reported_by_another_engine_is_refused(self, worker_manager: WorkerManager) -> None:
        links = _host_engine(worker_manager)
        await _start_on_host_engine(worker_manager)
        worker_engine_id = _started_worker_id(links)

        result = await _dispatched_by(
            worker_manager, RequestSender(engine_id="another-engine"), _exited(worker_engine_id, "exited")
        )

        assert not result.succeeded()
        assert worker_engine_id in worker_manager._starts_asked_of_hosts


_ARTIST = RequestSender(engine_id=_ARTIST_ENGINE, session_id=_ARTIST_SESSION)
_FROM_HOST_ENGINE = RequestSender(engine_id=_HOST_ENGINE, session_id=None)


async def _dispatched_by(
    worker_manager: WorkerManager, sender: RequestSender, request: RequestPayload
) -> EventResultSuccess | EventResultFailure:
    """Dispatch `request` to `worker_manager`'s handlers through an EventManager, as though `sender` sent it."""
    event_manager = EventManager()
    event_manager.register_request_handlers(worker_manager)
    event_manager.recognize_sessions(worker_manager.has_registered_session)
    return await event_manager.ahandle_request(request, result_context={"sender": sender})


async def _sent_by[R](sender: RequestSender, handler: Callable[[Any], Awaitable[R]], request: object) -> R:
    """Run `handler` on `request` as though the engine `sender` sent it."""
    token = _active_request_sender.set(sender)
    try:
        return await handler(request)
    finally:
        _active_request_sender.reset(token)


def _register_session(worker_manager: WorkerManager, **overrides: object) -> object:
    fields: dict[str, object] = {
        "schema_version": DISTRIBUTED_EXECUTION_SCHEMA_VERSION,
        "engine_version": engine_version,
        "library_versions": {_LIBRARY: "1.0.0"},
        "static_server_base_url": "http://workstation:8124",
    }
    fields.update(overrides)
    token = _active_request_sender.set(_ARTIST)
    try:
        return worker_manager.handle_offer_session_for_workers_request(
            worker_events.OfferSessionForWorkersRequest(**fields)  # type: ignore[arg-type]
        )
    finally:
        _active_request_sender.reset(token)


def _start_for_artist(
    library_name: str = _LIBRARY, worker_engine_id: str = "w1"
) -> worker_events.StartWorkerForEngineRequest:
    return worker_events.StartWorkerForEngineRequest(
        library_name=library_name, worker_engine_id=worker_engine_id, project_id="proj-1", project_generation=1
    )


async def _skip_connecting(
    _worker_engine_id: str, _remote: object, _proc: object, pending: Any, **_kwargs: object
) -> None:
    """Stand in for `_connect_served_worker`: the connection itself is a separate concern.

    Stops the real listener the start opened, since nothing here will ever connect to it.
    """
    pending.close()


async def _host(worker_manager: WorkerManager, *, returncode: int = 0, exits: asyncio.Event | None = None) -> MagicMock:
    """Start worker "w1" for the artist's session, standing in a process that exits as `_worker_proc_mock` does."""
    proc = _worker_proc_mock(returncode, exits)

    async def record_spawn(_args: list[str], worker_key: str | None, _environ: object) -> MagicMock:
        worker_manager._managed_worker_processes[worker_key] = proc
        return proc

    with (
        patch.object(worker_manager, "_start_worker_process", side_effect=record_spawn),
        patch.object(worker_manager, "_connect_served_worker", side_effect=_skip_connecting),
    ):
        await _sent_by(_ARTIST, worker_manager.handle_start_worker_for_engine_request, _start_for_artist())
    return proc


class TestServingAnotherEnginesSession:
    def test_a_session_on_the_same_versions_is_accepted(self, worker_manager: WorkerManager) -> None:
        _host_engine(worker_manager)

        result = _register_session(worker_manager)

        assert isinstance(result, worker_events.OfferSessionForWorkersResultSuccess)
        assert result.engine_id == _ENGINE
        assert result.refused_libraries == {}

    def test_a_different_engine_version_is_refused_naming_both(self, worker_manager: WorkerManager) -> None:
        _host_engine(worker_manager)

        result = _register_session(worker_manager, engine_version="0.0.1")

        assert isinstance(result, worker_events.OfferSessionForWorkersResultFailure)
        assert "0.0.1" in str(result.result_details)
        assert engine_version in str(result.result_details)

    def test_a_different_library_version_refuses_only_that_library(self, worker_manager: WorkerManager) -> None:
        _host_engine(worker_manager)

        result = _register_session(worker_manager, library_versions={_LIBRARY: "2.0.0"})

        assert isinstance(result, worker_events.OfferSessionForWorkersResultSuccess)
        assert "2.0.0" in result.refused_libraries[_LIBRARY]
        assert "1.0.0" in result.refused_libraries[_LIBRARY]

    def test_a_library_this_engine_lacks_is_refused(self, worker_manager: WorkerManager) -> None:
        _host_engine(worker_manager)
        worker_manager.engine.library_manager.get_library_info_by_library_name.return_value = None  # type: ignore[union-attr]

        result = _register_session(worker_manager)

        assert isinstance(result, worker_events.OfferSessionForWorkersResultSuccess)
        assert "not installed" in result.refused_libraries[_LIBRARY]

    def test_a_worker_run_for_another_engine_names_that_engine_and_any_other_names_none(
        self, worker_manager: WorkerManager
    ) -> None:
        """The transports reach such a worker the way they reach the engine it serves."""
        worker_manager._workers["w1"] = _served(
            worker_key=f"{_ARTIST_SESSION}/{_LIBRARY}",
            owning_engine_id=_ARTIST_ENGINE,
            session_id=_ARTIST_SESSION,
            library_name=_LIBRARY,
        )

        assert worker_manager.owning_engine_of("w1") == _ARTIST_ENGINE
        assert worker_manager.owning_engine_of("stranger") is None

    @pytest.mark.asyncio
    async def test_the_worker_is_started_on_this_engines_license_without_this_engines_identity(
        self, worker_manager: WorkerManager
    ) -> None:
        """It runs with `--library-name`, its own GTN_ENGINE_ID, and this session's static URL -- never this engine's."""
        _host_engine(worker_manager)
        _register_session(worker_manager)
        captured: dict[str, object] = {}

        async def record_spawn(
            args: list[str], worker_key: str | None, environ: Callable[[], Awaitable[dict[str, str]]]
        ) -> MagicMock:
            captured["args"] = args
            captured["worker_key"] = worker_key
            captured["environ"] = await environ()
            return _managed_proc_mock()

        with (
            patch.object(worker_manager, "_start_worker_process", side_effect=record_spawn),
            patch.object(worker_manager, "_connect_served_worker", side_effect=_skip_connecting),
        ):
            await _sent_by(_ARTIST, worker_manager.handle_start_worker_for_engine_request, _start_for_artist())

        assert captured["worker_key"] == f"{_ARTIST_SESSION}/{_LIBRARY}"
        args = cast("list[str]", captured["args"])
        assert args == [sys.executable, "-m", "griptape_nodes.worker", "--library-name", _LIBRARY]
        environ = cast("dict[str, str]", captured["environ"])
        assert environ[WORKER_ENGINE_ID_ENV] == "w1"
        # It runs on the artist's license session, which it asks the artist's engine for, never this engine's.
        assert environ[LICENSE_SECRET_NAME] == ""
        assert environ[API_KEY_SECRET_NAME] == ""
        # Its asset URLs are opened by the artist's editor, which reaches the artist's engine's server.
        assert environ[ORCHESTRATOR_STATIC_SERVER_BASE_URL_ENV] == "http://workstation:8124"

    @pytest.mark.asyncio
    async def test_a_start_that_cannot_open_its_connection_fails_cleanly(self, worker_manager: WorkerManager) -> None:
        """Nothing is left half-started: another start is not refused, and a stop does not wait on this one."""
        _host_engine(worker_manager)
        _register_session(worker_manager)

        with patch(
            "griptape_nodes.retained_mode.managers.worker_manager.listen_for_worker",
            new=AsyncMock(side_effect=OSError("no port left")),
        ):
            failed = await _sent_by(_ARTIST, worker_manager.handle_start_worker_for_engine_request, _start_for_artist())
        await asyncio.wait_for(
            _sent_by(
                _ARTIST,
                worker_manager.handle_stop_worker_for_engine_request,
                worker_events.StopWorkerForEngineRequest(worker_engine_id="w1"),
            ),
            timeout=5,
        )
        with (
            patch.object(worker_manager, "_start_worker_process", new=AsyncMock(return_value=_managed_proc_mock())),
            patch.object(worker_manager, "_connect_served_worker", side_effect=_skip_connecting),
        ):
            again = await _sent_by(_ARTIST, worker_manager.handle_start_worker_for_engine_request, _start_for_artist())

        assert isinstance(failed, worker_events.StartWorkerForEngineResultFailure)
        assert "no port left" in str(failed.result_details)
        assert isinstance(again, worker_events.StartWorkerForEngineResultSuccess)

    @pytest.mark.asyncio
    async def test_an_engine_with_no_id_starts_no_worker(self, worker_manager: WorkerManager) -> None:
        """Its worker could not ask it before each node, so would run whatever it was sent."""
        _host_engine(worker_manager)
        _register_session(worker_manager)
        worker_manager.engine.get_engine_id.return_value = None  # type: ignore[attr-defined]

        with patch.object(worker_manager, "_start_worker_process", new=AsyncMock()) as spawn:
            result = await _sent_by(_ARTIST, worker_manager.handle_start_worker_for_engine_request, _start_for_artist())

        assert isinstance(result, worker_events.StartWorkerForEngineResultFailure)
        assert "no engine ID" in str(result.result_details)
        spawn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_start_for_an_unregistered_session_is_refused(self, worker_manager: WorkerManager) -> None:
        _host_engine(worker_manager)

        with patch.object(worker_manager, "_start_worker_process", new=AsyncMock()) as spawn:
            result = await _dispatched_by(worker_manager, _ARTIST, _start_for_artist())

        assert not result.succeeded()
        assert "has not registered with this engine" in str(result.result.result_details)
        spawn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_start_for_a_registered_session_is_handled(self, worker_manager: WorkerManager) -> None:
        _host_engine(worker_manager)
        offer = worker_events.OfferSessionForWorkersRequest(
            schema_version=DISTRIBUTED_EXECUTION_SCHEMA_VERSION,
            engine_version=engine_version,
            library_versions={_LIBRARY: "1.0.0"},
        )

        offered = await _dispatched_by(worker_manager, _ARTIST, offer)
        # No process comes of the start, which its handler answers as a start already under way.
        with patch.object(worker_manager, "_start_worker_process", new=AsyncMock(return_value=None)):
            started = await _dispatched_by(worker_manager, _ARTIST, _start_for_artist())

        assert isinstance(offered.result, worker_events.OfferSessionForWorkersResultSuccess)
        # Its handler answered it, rather than the sender check refusing it first.
        assert isinstance(started.result, worker_events.StartWorkerForEngineResultFailure)
        assert "already starting" in str(started.result.result_details)

    @pytest.mark.asyncio
    async def test_a_start_from_another_engine_than_registered_the_session_fails(
        self, worker_manager: WorkerManager
    ) -> None:
        _host_engine(worker_manager)
        _register_session(worker_manager)
        impostor = RequestSender(engine_id="impostor", session_id=_ARTIST_SESSION)

        with patch.object(worker_manager, "_start_worker_process", new=AsyncMock()) as spawn:
            result = await _dispatched_by(worker_manager, impostor, _start_for_artist())

        assert not result.succeeded()
        spawn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_start_for_a_running_worker_replaces_it(self, worker_manager: WorkerManager) -> None:
        """The artist's engine asks only once it stopped using that worker, so the old one would never register."""
        _host_engine(worker_manager)
        _register_session(worker_manager)
        stale = _managed_proc_mock()
        worker_manager._managed_worker_processes[f"{_ARTIST_SESSION}/{_LIBRARY}"] = stale

        with (
            patch.object(worker_manager, "_terminate_via_spawn_loop", new=AsyncMock()) as terminate,
            patch.object(
                worker_manager, "_start_worker_process", new=AsyncMock(return_value=_managed_proc_mock())
            ) as spawn,
            patch.object(worker_manager, "_connect_served_worker", side_effect=_skip_connecting),
        ):
            await _sent_by(_ARTIST, worker_manager.handle_start_worker_for_engine_request, _start_for_artist())

        terminate.assert_awaited_once_with(f"{_ARTIST_SESSION}/{_LIBRARY}", stale)
        spawn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_start_for_a_library_the_session_never_named_asks_it_to_register_again(
        self, worker_manager: WorkerManager
    ) -> None:
        """Its version was never checked, so starting it would run whatever this engine has."""
        _host_engine(worker_manager)
        _register_session(worker_manager)

        with patch.object(worker_manager, "_start_worker_process", new=AsyncMock()) as spawn:
            result = await _sent_by(
                _ARTIST,
                worker_manager.handle_start_worker_for_engine_request,
                _start_for_artist(library_name="Unnamed"),
            )

        assert isinstance(result, worker_events.StartWorkerForEngineResultFailure)
        assert result.session_unknown
        spawn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_start_for_a_refused_library_fails_with_the_refusal(self, worker_manager: WorkerManager) -> None:
        _host_engine(worker_manager)
        _register_session(worker_manager, library_versions={_LIBRARY: "2.0.0"})

        with patch.object(worker_manager, "_start_worker_process", new=AsyncMock()) as spawn:
            result = await _sent_by(_ARTIST, worker_manager.handle_start_worker_for_engine_request, _start_for_artist())

        assert isinstance(result, worker_events.StartWorkerForEngineResultFailure)
        assert "2.0.0" in str(result.result_details)
        assert not result.session_unknown
        spawn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stopping_a_library_stops_its_workers_run_for_other_engines(
        self, worker_manager: WorkerManager
    ) -> None:
        """Resetting a library replaces the environment its remote workers run in."""
        _host_engine(worker_manager)
        _register_session(worker_manager)
        proc = await _host(worker_manager, exits=asyncio.Event())

        with patch.object(worker_manager, "_terminate_via_spawn_loop", new=AsyncMock()) as terminate:
            await worker_manager.stop_worker_for_library(_LIBRARY)

        terminate.assert_awaited_once_with(f"{_ARTIST_SESSION}/{_LIBRARY}", proc)

    @pytest.mark.asyncio
    async def test_a_library_this_engine_updated_since_the_session_registered_is_refused(
        self, worker_manager: WorkerManager
    ) -> None:
        _host_engine(worker_manager)
        _register_session(worker_manager)
        worker_manager.engine.library_manager.get_library_info_by_library_name.return_value = MagicMock(  # type: ignore[union-attr]
            library_version="1.1.0", lifecycle_state=LibraryLifecycleState.LOADED
        )

        with patch.object(worker_manager, "_start_worker_process", new=AsyncMock()) as spawn:
            result = await _sent_by(_ARTIST, worker_manager.handle_start_worker_for_engine_request, _start_for_artist())

        assert isinstance(result, worker_events.StartWorkerForEngineResultFailure)
        assert "1.1.0" in str(result.result_details)
        spawn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_library_this_engine_has_not_loaded_is_refused(self, worker_manager: WorkerManager) -> None:
        """A disabled library, one that failed to load, or one still installing has no environment to run in."""
        _host_engine(worker_manager)
        _register_session(worker_manager)
        worker_manager.engine.library_manager.get_library_info_by_library_name.return_value = MagicMock(  # type: ignore[union-attr]
            library_version="1.0.0", lifecycle_state=LibraryLifecycleState.DISABLED
        )

        with patch.object(worker_manager, "_start_worker_process", new=AsyncMock()) as spawn:
            result = await _sent_by(_ARTIST, worker_manager.handle_start_worker_for_engine_request, _start_for_artist())

        assert isinstance(result, worker_events.StartWorkerForEngineResultFailure)
        assert "not loaded" in str(result.result_details)
        spawn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_spawn_that_produced_no_worker_fails_and_forgets_it(self, worker_manager: WorkerManager) -> None:
        _host_engine(worker_manager)
        _register_session(worker_manager)

        with patch.object(worker_manager, "_start_worker_process", new=AsyncMock(return_value=None)):
            result = await _sent_by(_ARTIST, worker_manager.handle_start_worker_for_engine_request, _start_for_artist())

        assert isinstance(result, worker_events.StartWorkerForEngineResultFailure)
        assert worker_manager._workers_served_for() == {}

    @pytest.mark.asyncio
    async def test_a_worker_that_exits_on_its_own_is_reported_exited_with_its_code(
        self, worker_manager: WorkerManager
    ) -> None:
        links = _host_engine(worker_manager)
        _register_session(worker_manager)
        await _host(worker_manager, returncode=7)

        await asyncio.gather(*worker_manager._exit_watchers)

        (report,) = links.sent_of(worker_events.ReportWorkerExitedRequest)
        assert report == worker_events.ReportWorkerExitedRequest(worker_engine_id="w1", exit_code=7, reason="exited")
        assert links.sent[-1][0] == _ARTIST_ENGINE
        assert f"{_ARTIST_SESSION}/{_LIBRARY}" not in worker_manager._managed_worker_processes
        assert worker_manager._workers_served_for() == {}

    @pytest.mark.asyncio
    async def test_a_worker_this_engine_stops_is_reported_stopped(self, worker_manager: WorkerManager) -> None:
        links = _host_engine(worker_manager)
        _register_session(worker_manager)
        exits = asyncio.Event()
        proc = await _host(worker_manager, exits=exits)

        with patch.object(
            worker_manager, "_terminate_via_spawn_loop", new=AsyncMock(side_effect=lambda *_args: exits.set())
        ) as terminate:
            result = await _sent_by(
                _ARTIST,
                worker_manager.handle_stop_worker_for_engine_request,
                worker_events.StopWorkerForEngineRequest(worker_engine_id="w1"),
            )
        await asyncio.gather(*worker_manager._exit_watchers)

        assert isinstance(result, worker_events.StopWorkerForEngineResultSuccess)
        terminate.assert_awaited_once_with(f"{_ARTIST_SESSION}/{_LIBRARY}", proc)
        (report,) = links.sent_of(worker_events.ReportWorkerExitedRequest)
        assert cast("worker_events.ReportWorkerExitedRequest", report).reason == "stopped"

    @pytest.mark.asyncio
    async def test_a_stop_sent_while_the_start_is_still_under_way_stops_the_worker_it_makes(
        self, worker_manager: WorkerManager
    ) -> None:
        """The artist withdrew the start, so the worker it makes must not be left running."""
        _host_engine(worker_manager)
        _register_session(worker_manager)
        worker_key = f"{_ARTIST_SESSION}/{_LIBRARY}"
        worker_manager._managed_worker_processes[worker_key] = _managed_proc_mock()
        replacing = asyncio.Event()
        proc = _worker_proc_mock(0, asyncio.Event())

        async def slow_terminate(*_args: object) -> None:
            await replacing.wait()

        async def record_spawn(_args: list[str], key: str | None, _environ: object) -> MagicMock:
            worker_manager._managed_worker_processes[key] = proc
            return proc

        with (
            patch.object(worker_manager, "_terminate_via_spawn_loop", side_effect=slow_terminate) as terminate,
            patch.object(worker_manager, "_start_worker_process", side_effect=record_spawn),
            patch.object(worker_manager, "_connect_served_worker", side_effect=_skip_connecting),
        ):
            start = asyncio.ensure_future(
                _sent_by(_ARTIST, worker_manager.handle_start_worker_for_engine_request, _start_for_artist())
            )
            await asyncio.sleep(0)
            stop = asyncio.ensure_future(
                _sent_by(
                    _ARTIST,
                    worker_manager.handle_stop_worker_for_engine_request,
                    worker_events.StopWorkerForEngineRequest(worker_engine_id="w1"),
                )
            )
            await asyncio.sleep(0)
            assert not stop.done()
            replacing.set()
            await asyncio.gather(start, stop)

        assert terminate.await_args_list[-1].args == (worker_key, proc)
        watchers = list(worker_manager._exit_watchers)
        for watcher in watchers:
            watcher.cancel()
        await asyncio.gather(*watchers, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_a_second_start_for_a_worker_still_starting_is_refused(self, worker_manager: WorkerManager) -> None:
        _host_engine(worker_manager)
        _register_session(worker_manager)
        worker_manager._managed_worker_processes[f"{_ARTIST_SESSION}/{_LIBRARY}"] = _managed_proc_mock()
        replacing = asyncio.Event()

        async def slow_terminate(*_args: object) -> None:
            await replacing.wait()

        with (
            patch.object(worker_manager, "_terminate_via_spawn_loop", side_effect=slow_terminate),
            patch.object(worker_manager, "_start_worker_process", new=AsyncMock()),
            patch.object(worker_manager, "_connect_served_worker", side_effect=_skip_connecting),
        ):
            first = asyncio.ensure_future(
                _sent_by(_ARTIST, worker_manager.handle_start_worker_for_engine_request, _start_for_artist())
            )
            await asyncio.sleep(0)
            second = await _sent_by(
                _ARTIST, worker_manager.handle_start_worker_for_engine_request, _start_for_artist(worker_engine_id="w2")
            )
            replacing.set()
            await first

        assert isinstance(second, worker_events.StartWorkerForEngineResultFailure)
        assert "already starting" in str(second.result_details)
        watchers = list(worker_manager._exit_watchers)
        for watcher in watchers:
            watcher.cancel()
        await asyncio.gather(*watchers, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_a_stop_from_another_engine_is_refused(self, worker_manager: WorkerManager) -> None:
        _host_engine(worker_manager)
        _register_session(worker_manager)
        proc = await _host(worker_manager, exits=asyncio.Event())
        impostor = RequestSender(engine_id="impostor", session_id=_ARTIST_SESSION)

        result = await _dispatched_by(
            worker_manager, impostor, worker_events.StopWorkerForEngineRequest(worker_engine_id="w1")
        )

        assert not result.succeeded()
        assert worker_manager._managed_worker_processes[f"{_ARTIST_SESSION}/{_LIBRARY}"] is proc
        watchers = list(worker_manager._exit_watchers)
        for watcher in watchers:
            watcher.cancel()
        await asyncio.gather(*watchers, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_a_lost_link_forgets_the_artists_sessions_and_stops_their_workers(
        self, worker_manager: WorkerManager
    ) -> None:
        links = _host_engine(worker_manager)
        _register_session(worker_manager)
        exits = asyncio.Event()
        proc = await _host(worker_manager, exits=exits)
        assert links.link_lost is not None

        with patch.object(
            worker_manager, "_terminate_via_spawn_loop", new=AsyncMock(side_effect=lambda *_args: exits.set())
        ) as terminate:
            await links.link_lost(_ARTIST_ENGINE)
        await asyncio.gather(*worker_manager._exit_watchers)

        assert worker_manager._sessions == {}
        terminate.assert_awaited_once_with(f"{_ARTIST_SESSION}/{_LIBRARY}", proc)
        (report,) = links.sent_of(worker_events.ReportWorkerExitedRequest)
        assert cast("worker_events.ReportWorkerExitedRequest", report).reason == "stopped"

    @pytest.mark.asyncio
    async def test_a_lost_link_from_another_engine_leaves_the_session_alone(
        self, worker_manager: WorkerManager
    ) -> None:
        links = _host_engine(worker_manager)
        _register_session(worker_manager)
        assert links.link_lost is not None

        await links.link_lost("someone-else")

        assert _ARTIST_SESSION in worker_manager._sessions


class _FakeLine:
    """Stands in for this engine's line of runs, recording each run submitted to it."""

    def __init__(self, worker_manager: WorkerManager) -> None:
        self.runs: list[MagicMock] = []
        flow_manager = worker_manager.engine.flow_manager
        flow_manager.submit_run = self.submit  # type: ignore[method-assign]
        flow_manager.cancel_run = AsyncMock(return_value=True)  # type: ignore[method-assign]

    def submit(self, spec: object, *, flows: object = None) -> MagicMock:
        run = MagicMock()
        run.run_id = f"line-{len(self.runs)}"
        run.position = 1
        run.spec = spec
        run.flows = flows
        self.runs.append(run)
        return run


def _served(
    *, worker_key: str, owning_engine_id: str, session_id: str, library_name: str, channel: Any = None
) -> WorkerRegistration:
    """A worker this engine runs for `owning_engine_id`'s session, not yet recorded."""
    return WorkerRegistration(
        request_topic="",
        worker_key=worker_key,
        channel=channel,
        serves=_ServedFor(owning_engine_id=owning_engine_id, session_id=session_id, library_name=library_name),
    )


def _acquire(lease_id: str, run_id: str | None = "run-1") -> worker_events.AcquireLeaseRequest:
    return worker_events.AcquireLeaseRequest(lease_id=lease_id, run_id=run_id)


def _release(lease_id: str) -> worker_events.ReleaseLeaseRequest:
    return worker_events.ReleaseLeaseRequest(lease_id=lease_id)


def _configure(worker_manager: WorkerManager, settings: dict[str, object]) -> None:
    """Answer the given config keys with the given values, and every other key as before."""
    read_before = worker_manager.engine.config_manager.get_config_value.side_effect  # type: ignore[union-attr]

    def get_config_value(key: str, default: object = None, cast_type: type | None = None, **kwargs: object) -> object:
        if key in settings:
            return settings[key]
        return read_before(key, default, cast_type, **kwargs)

    worker_manager.engine.config_manager.get_config_value.side_effect = get_config_value  # type: ignore[union-attr]


async def _settle() -> None:
    """Let the tasks a handler started run as far as they can."""
    for _ in range(10):
        await asyncio.sleep(0)


async def _granted(worker_manager: WorkerManager, line: _FakeLine, lease_id: str = "a1") -> asyncio.Task[ResultPayload]:
    """Acquire `lease_id` as the artist's engine and let it reach the front of the line, returning the hold."""
    acquiring = asyncio.create_task(_sent_by(_ARTIST, worker_manager.handle_acquire_lease_request, _acquire(lease_id)))
    await _settle()
    hold = asyncio.create_task(line.runs[-1].spec.execute(worker_manager.engine))
    assert isinstance(await acquiring, worker_events.AcquireLeaseResultSuccess)
    return hold


def _acquire_answered_when(answered: asyncio.Event) -> asyncio.Task[ResultPayload]:
    """This engine's acquire of a lease on another engine, answered once `answered` is set."""

    async def acquire() -> ResultPayload:
        await answered.wait()
        return worker_events.AcquireLeaseResultSuccess(result_details="ok")

    return asyncio.create_task(acquire())


class TestSharingThisEngineBetweenRuns:
    @pytest.mark.asyncio
    async def test_a_lease_waits_for_its_turn_and_holds_the_engine_until_released(
        self, worker_manager: WorkerManager
    ) -> None:
        _host_engine(worker_manager)
        _register_session(worker_manager)
        line = _FakeLine(worker_manager)

        acquiring = asyncio.create_task(_sent_by(_ARTIST, worker_manager.handle_acquire_lease_request, _acquire("a1")))
        await _settle()
        assert not acquiring.done()
        hold = asyncio.create_task(line.runs[0].spec.execute(worker_manager.engine))
        assert isinstance(await acquiring, worker_events.AcquireLeaseResultSuccess)
        await asyncio.sleep(0.05)
        assert not hold.done(), "the lease holds this engine until it is released"

        await _sent_by(_ARTIST, worker_manager.handle_release_lease_request, _release("a1"))

        assert isinstance(await asyncio.wait_for(hold, timeout=1.0), worker_events.ReleaseLeaseResultSuccess)
        assert worker_manager._engine_runs == {}

    @pytest.mark.asyncio
    async def test_the_same_lease_asked_for_twice_waits_in_line_once(self, worker_manager: WorkerManager) -> None:
        _host_engine(worker_manager)
        _register_session(worker_manager)
        line = _FakeLine(worker_manager)

        first = asyncio.create_task(_sent_by(_ARTIST, worker_manager.handle_acquire_lease_request, _acquire("a1")))
        second = asyncio.create_task(_sent_by(_ARTIST, worker_manager.handle_acquire_lease_request, _acquire("a1")))
        await _settle()
        hold = asyncio.create_task(line.runs[0].spec.execute(worker_manager.engine))

        assert isinstance(await first, worker_events.AcquireLeaseResultSuccess)
        assert isinstance(await second, worker_events.AcquireLeaseResultSuccess)
        assert len(line.runs) == 1
        worker_manager._engine_runs[(_ARTIST_ENGINE, "a1")].released.set()
        await hold

    @pytest.mark.asyncio
    async def test_a_waiting_lease_is_told_its_place_in_line_until_it_is_granted(
        self, worker_manager: WorkerManager
    ) -> None:
        links = _host_engine(worker_manager)
        _register_session(worker_manager)
        line = _FakeLine(worker_manager)

        acquiring = asyncio.create_task(_sent_by(_ARTIST, worker_manager.handle_acquire_lease_request, _acquire("a1")))
        await _settle()
        run = line.runs[0]
        run.spec.on_place_in_line(run)
        run.position = 0
        run.spec.on_place_in_line(run)
        hold = asyncio.create_task(run.spec.execute(worker_manager.engine))
        await acquiring
        run.spec.on_place_in_line(run)
        await asyncio.gather(*worker_manager._engine_notices)

        reports = [
            (engine_id, request.lease_id, request.position)
            for engine_id, request, _timeout_s in links.sent
            if isinstance(request, worker_events.ReportPlaceInLineRequest)
        ]
        assert reports == [(_ARTIST_ENGINE, "a1", 1), (_ARTIST_ENGINE, "a1", 0)]
        worker_manager._engine_runs[(_ARTIST_ENGINE, "a1")].released.set()
        await hold

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("incoming_run_id", "mine_run_id", "mine_granted"),
        [
            (None, "run-here", False),
            ("run-1", None, False),
            ("run-1", "run-here", True),
        ],
        ids=["a-node-outside-a-run-holds-no-line", "this-engine-s-node-outside-a-run", "this-engine-s-lease-granted"],
    )
    async def test_engines_that_are_not_both_stuck_log_nothing(
        self,
        worker_manager: WorkerManager,
        caplog: pytest.LogCaptureFixture,
        incoming_run_id: str | None,
        mine_run_id: str | None,
        *,
        mine_granted: bool,
    ) -> None:
        _host_engine(worker_manager)
        _register_session(worker_manager)
        _FakeLine(worker_manager)
        answered = asyncio.Event()
        if mine_granted:
            answered.set()
        there = _acquire_answered_when(answered)
        await _settle()
        worker_manager._leases["mine"] = EngineLease(
            lease_id="mine", host_engine_id=_ARTIST_ENGINE, run_id=mine_run_id, request_task=there
        )

        with caplog.at_level(logging.ERROR):
            acquiring = asyncio.create_task(
                _sent_by(_ARTIST, worker_manager.handle_acquire_lease_request, _acquire("a1", run_id=incoming_run_id))
            )
            await _settle()

        assert not [record for record in caplog.records if record.levelno == logging.ERROR]
        acquiring.cancel()
        there.cancel()

    @pytest.mark.asyncio
    async def test_a_lease_turned_away_is_refused_saying_why(self, worker_manager: WorkerManager) -> None:
        _host_engine(worker_manager)
        _register_session(worker_manager)
        line = _FakeLine(worker_manager)

        acquiring = asyncio.create_task(_sent_by(_ARTIST, worker_manager.handle_acquire_lease_request, _acquire("a1")))
        await _settle()
        line.runs[0].spec.on_turned_away("The run did not start: it was cancelled.")
        result = await acquiring

        assert isinstance(result, worker_events.AcquireLeaseResultFailure)
        assert "it was cancelled" in str(result.result_details)
        assert worker_manager._engine_runs == {}

    @pytest.mark.asyncio
    async def test_building_a_run_s_failure_leaves_its_lease_waiting(self, worker_manager: WorkerManager) -> None:
        _host_engine(worker_manager)
        _register_session(worker_manager)
        line = _FakeLine(worker_manager)
        acquiring = asyncio.create_task(_sent_by(_ARTIST, worker_manager.handle_acquire_lease_request, _acquire("a1")))
        await _settle()

        failure = line.runs[0].spec.failure("The run did not start: it was cancelled.")
        await _settle()

        assert isinstance(failure, worker_events.AcquireLeaseResultFailure)
        assert not acquiring.done()
        assert (_ARTIST_ENGINE, "a1") in worker_manager._engine_runs
        acquiring.cancel()

    @pytest.mark.asyncio
    async def test_a_lease_released_while_it_waits_leaves_the_line(self, worker_manager: WorkerManager) -> None:
        """Otherwise it would hold this engine for good once it reached the front."""
        _host_engine(worker_manager)
        _register_session(worker_manager)
        line = _FakeLine(worker_manager)

        acquiring = asyncio.create_task(
            _sent_by(_ARTIST, worker_manager.handle_acquire_lease_request, _acquire("a1", run_id=None))
        )
        await _settle()
        await _dispatched_by(worker_manager, _ARTIST, _release("a1"))

        assert isinstance(await asyncio.wait_for(acquiring, timeout=1.0), worker_events.AcquireLeaseResultFailure)
        assert worker_manager._engine_runs == {}
        worker_manager.engine.flow_manager.cancel_run.assert_awaited_once_with(  # type: ignore[attr-defined]
            line.runs[0].run_id, flows=worker_manager._lease_line
        )

    @pytest.mark.asyncio
    async def test_a_lease_released_while_workers_are_freed_for_it_lets_the_engine_go(
        self, worker_manager: WorkerManager
    ) -> None:
        """Otherwise the lease would hold this engine forever, and every run behind it would wait."""
        _host_engine(worker_manager)
        _register_session(worker_manager)
        line = _FakeLine(worker_manager)
        worker_manager._last_engine_run_engine_id = "another-artist-engine"
        freeing = asyncio.Event()

        async def free_slowly(_engine_id: str) -> None:
            await freeing.wait()

        with patch.object(worker_manager, "_free_for_handoff", new=free_slowly):
            acquiring = asyncio.create_task(
                _sent_by(_ARTIST, worker_manager.handle_acquire_lease_request, _acquire("a1"))
            )
            await _settle()
            hold = asyncio.create_task(line.runs[0].spec.execute(worker_manager.engine))
            await _settle()
            await _sent_by(_ARTIST, worker_manager.handle_release_lease_request, _release("a1"))
            freeing.set()

            assert isinstance(await asyncio.wait_for(hold, timeout=1.0), worker_events.ReleaseLeaseResultSuccess)
        assert isinstance(await acquiring, worker_events.AcquireLeaseResultFailure)

    @pytest.mark.asyncio
    async def test_passing_to_another_engine_s_lease_frees_workers_before_granting_it(
        self, worker_manager: WorkerManager
    ) -> None:
        _host_engine(worker_manager)
        _register_session(worker_manager)
        line = _FakeLine(worker_manager)
        worker_manager._last_engine_run_engine_id = "another-artist-engine"
        order: list[str] = []

        async def free(_engine_id: str) -> None:
            order.append("free")

        with patch.object(worker_manager, "_free_for_handoff", new=AsyncMock(side_effect=free)) as freeing:
            acquiring = asyncio.create_task(
                _sent_by(_ARTIST, worker_manager.handle_acquire_lease_request, _acquire("a1"))
            )
            await _settle()
            hold = asyncio.create_task(line.runs[0].spec.execute(worker_manager.engine))
            await acquiring
            order.append("granted")

        freeing.assert_awaited_once_with(_ARTIST_ENGINE)
        assert order == ["free", "granted"]
        worker_manager._engine_runs[(_ARTIST_ENGINE, "a1")].released.set()
        await hold

    @pytest.mark.asyncio
    async def test_the_same_engine_s_next_lease_keeps_its_models(self, worker_manager: WorkerManager) -> None:
        _host_engine(worker_manager)
        _register_session(worker_manager)
        line = _FakeLine(worker_manager)
        worker_manager._last_engine_run_engine_id = _ARTIST_ENGINE

        with patch.object(worker_manager, "_free_for_handoff", new=AsyncMock()) as free:
            hold = await _granted(worker_manager, line)

        free.assert_not_awaited()
        worker_manager._engine_runs[(_ARTIST_ENGINE, "a1")].released.set()
        await hold

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("which", "how", "dropped", "stopped"),
        [
            ("others", "drop_cache", ["w-other"], []),
            ("all", "drop_cache", ["w-incoming", "w-other"], []),
            ("others", "recycle", [], ["w-other"]),
            ("none", "drop_cache", [], []),
        ],
    )
    async def test_the_release_setting_says_whose_workers_are_freed_and_how(
        self,
        worker_manager: WorkerManager,
        which: str,
        how: str,
        dropped: list[str],
        stopped: list[str],
    ) -> None:
        _configure(
            worker_manager,
            {"distributed_execution.release.which": which, "distributed_execution.release.how": how},
        )
        incoming = _served(worker_key="s1/lib", owning_engine_id=_ARTIST_ENGINE, session_id="s1", library_name="lib")
        other = _served(
            worker_key="s2/lib", owning_engine_id="another-artist-engine", session_id="s2", library_name="lib"
        )
        worker_manager._workers.update({"w-incoming": incoming, "w-other": other})
        by_worker = {id(incoming): "w-incoming", id(other): "w-other"}

        with (
            patch.object(worker_manager, "_drop_library_named_objects", new=AsyncMock()) as drop,
            patch.object(worker_manager, "_stop_served_worker", new=AsyncMock()) as stop,
        ):
            await worker_manager._free_for_handoff(_ARTIST_ENGINE)

        assert sorted(call_args.args[0] for call_args in drop.await_args_list) == dropped
        assert sorted(by_worker[id(call_args.args[0])] for call_args in stop.await_args_list) == stopped

    @pytest.mark.asyncio
    async def test_a_recycled_worker_s_engine_hears_it_stopped_before_the_next_run_is_admitted(
        self, worker_manager: WorkerManager
    ) -> None:
        """Otherwise that engine could route the next node to the worker that just stopped."""
        _configure(
            worker_manager,
            {"distributed_execution.release.which": "all", "distributed_execution.release.how": "recycle"},
        )
        answered = asyncio.Event()
        remote = _served(worker_key="s1/lib", owning_engine_id=_ARTIST_ENGINE, session_id="s1", library_name="lib")
        cast("_ServedFor", remote.serves).exit_watcher = asyncio.create_task(answered.wait())
        worker_manager._workers["w1"] = remote

        with patch.object(worker_manager, "_stop_served_worker", new=AsyncMock()):
            freeing = asyncio.create_task(worker_manager._free_for_handoff(_ARTIST_ENGINE))
            await _settle()
            assert not freeing.done()
            answered.set()
            await asyncio.wait_for(freeing, timeout=1.0)

    @pytest.mark.asyncio
    async def test_freeing_a_worker_that_exits_ends_when_it_exits(self, worker_manager: WorkerManager) -> None:
        """Otherwise the next run would wait out the whole timeout for a worker that is gone."""
        _host_engine(worker_manager)
        remote = _served(
            worker_key="s1/lib", owning_engine_id="another-artist-engine", session_id="s1", library_name="lib"
        )
        worker_manager._workers["w1"] = remote
        exited = asyncio.Event()

        async def wait() -> int:
            await exited.wait()
            return 0

        proc = MagicMock()
        proc.wait = wait

        freeing = asyncio.create_task(worker_manager._free_for_handoff(_ARTIST_ENGINE))
        await _settle()
        watcher = asyncio.create_task(worker_manager._report_when_exited("w1", remote, proc))
        exited.set()

        await asyncio.wait_for(freeing, timeout=1.0)
        await watcher

    @pytest.mark.asyncio
    async def test_freeing_a_worker_asks_it_through_its_channel(self, worker_manager: WorkerManager) -> None:
        """The worker is reached through the channel this engine runs it on, not a topic."""
        channel = _FakeChannel()
        remote = _served(
            worker_key="s1/lib",
            owning_engine_id=_ARTIST_ENGINE,
            session_id="s1",
            library_name="lib",
            channel=channel,  # type: ignore[arg-type]
        )

        await worker_manager._drop_library_named_objects("w1", remote)

        assert len(channel.requested) == 1
        assert channel.requested[0]["payload"]["request_type"] == "DropLibraryNamedLocalObjectsRequest"

    @pytest.mark.asyncio
    async def test_freeing_a_worker_that_never_connected_asks_nothing(self, worker_manager: WorkerManager) -> None:
        """It holds nothing yet, so there is nothing to free."""
        remote = _served(worker_key="s1/lib", owning_engine_id=_ARTIST_ENGINE, session_id="s1", library_name="lib")

        await worker_manager._drop_library_named_objects("w1", remote)

        worker_manager._tx.send_message.assert_not_called()  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_an_engine_that_is_lost_lets_its_leases_go(self, worker_manager: WorkerManager) -> None:
        _host_engine(worker_manager)
        _register_session(worker_manager)
        line = _FakeLine(worker_manager)

        hold = await _granted(worker_manager, line)
        waiting = asyncio.create_task(
            _sent_by(_ARTIST, worker_manager.handle_acquire_lease_request, _acquire("a2", run_id="run-2"))
        )
        await _settle()

        await worker_manager.on_engine_lost(_ARTIST_ENGINE)

        await asyncio.wait_for(hold, timeout=1.0)
        refused = await waiting
        assert isinstance(refused, worker_events.AcquireLeaseResultFailure)
        assert "lost its connection" in str(refused.result_details)
        line_cancel = worker_manager.engine.flow_manager.cancel_run
        line_cancel.assert_awaited_once_with(line.runs[1].run_id, flows=worker_manager._lease_line)  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_a_session_that_never_registered_is_refused(self, worker_manager: WorkerManager) -> None:
        _host_engine(worker_manager)
        line = _FakeLine(worker_manager)

        result = await _dispatched_by(worker_manager, _ARTIST, _acquire("a1"))

        assert not result.succeeded()
        assert "has not registered with this engine" in str(result.result.result_details)
        assert line.runs == []

    @pytest.mark.asyncio
    async def test_only_the_engine_whose_lease_it_is_may_release_it(self, worker_manager: WorkerManager) -> None:
        _host_engine(worker_manager)
        _register_session(worker_manager)
        line = _FakeLine(worker_manager)
        hold = await _granted(worker_manager, line)

        intruder = RequestSender(engine_id="another-artist-engine", session_id="another-sess")
        result = await _dispatched_by(worker_manager, intruder, _release("a1"))

        assert isinstance(result, EventResultFailure)
        assert worker_manager._engine_runs[(_ARTIST_ENGINE, "a1")].holds_engine
        worker_manager._engine_runs[(_ARTIST_ENGINE, "a1")].released.set()
        await hold

    @pytest.mark.asyncio
    async def test_a_start_under_the_id_of_one_of_this_engines_own_workers_is_refused(
        self, worker_manager: WorkerManager
    ) -> None:
        """Workers this engine runs for others share a table with its own, which a start must not replace."""
        _host_engine(worker_manager)
        _register_session(worker_manager)
        own = WorkerRegistration(request_topic="", worker_key=_LIBRARY)
        worker_manager._workers["w1"] = own

        with patch.object(worker_manager, "_start_worker_process", new=AsyncMock()) as spawn:
            result = await _sent_by(_ARTIST, worker_manager.handle_start_worker_for_engine_request, _start_for_artist())

        assert isinstance(result, worker_events.StartWorkerForEngineResultFailure)
        assert "already names one of this engine's workers" in str(result.result_details)
        assert worker_manager._workers["w1"] is own
        spawn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_start_beyond_the_worker_limit_is_refused_naming_the_setting(
        self, worker_manager: WorkerManager
    ) -> None:
        _host_engine(worker_manager)
        _configure(worker_manager, {"distributed_execution.max_workers": 1})
        _register_session(worker_manager)
        # Still starting, which counts as much as a running one.
        worker_manager._workers["w-other"] = _served(
            worker_key="another-sess/lib",
            owning_engine_id="another-artist-engine",
            session_id="another-sess",
            library_name="lib",
        )

        with patch.object(worker_manager, "_start_worker_process", new=AsyncMock()) as spawn:
            result = await _sent_by(_ARTIST, worker_manager.handle_start_worker_for_engine_request, _start_for_artist())

        assert isinstance(result, worker_events.StartWorkerForEngineResultFailure)
        assert result.at_capacity
        assert "distributed_execution.max_workers" in str(result.result_details)
        spawn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_this_engine_s_own_workers_do_not_count_against_the_worker_limit(
        self, worker_manager: WorkerManager
    ) -> None:
        _host_engine(worker_manager)
        _configure(worker_manager, {"distributed_execution.max_workers": 1})
        _register_session(worker_manager)
        worker_manager._managed_worker_processes["Local Library"] = _worker_proc_mock()

        assert worker_manager._capacity_refusal(f"{_ARTIST_SESSION}/{_LIBRARY}") is None


def _in_run(worker_manager: WorkerManager, run_id: str = "run-1") -> MagicMock:
    """Put the code under test inside a run of this engine, returning the run."""
    run = MagicMock()
    run.run_id = run_id
    run.ended = asyncio.get_running_loop().create_future()
    worker_manager.engine.flow_manager.current_run.return_value = run  # type: ignore[attr-defined]
    return run


def _offer_session_to(worker_manager: WorkerManager, engine_id: str) -> None:
    """Record that this engine's session registered with `engine_id`."""
    accepted = asyncio.get_running_loop().create_future()
    accepted.set_result(worker_events.OfferSessionForWorkersResultSuccess(result_details="ok", engine_id=engine_id))
    worker_manager._engine_registrations[(engine_id, _SESSION)] = accepted  # type: ignore[assignment]


class TestReleasingALeaseOnAnotherEngine:
    """That engine holds itself for the lease until it hears, so the release is sent again until it is answered."""

    @pytest.mark.asyncio
    async def test_a_node_s_own_lease_is_released_until_it_is_answered(self, worker_manager: WorkerManager) -> None:
        """Nothing else lets that engine go from a node sent without a run."""
        links = _host_engine(worker_manager)
        _offer_session_to(worker_manager, _HOST_ENGINE)
        answer = links.request
        tries = {"count": 0}

        async def lost_once(engine_id: str, request: RequestPayload, *, timeout_s: float | None) -> ResultPayload:
            if isinstance(request, worker_events.ReleaseLeaseRequest):
                tries["count"] += 1
                if tries["count"] == 1:
                    links.sent.append((engine_id, request, timeout_s))
                    msg = "gpu-01 did not answer."
                    raise EngineNotFoundError(msg)
            return await answer(engine_id, request, timeout_s=timeout_s)

        worker_manager.engine.request = lost_once  # type: ignore[method-assign]
        with patch("griptape_nodes.retained_mode.managers.worker_manager._REPORT_RETRY_PAUSE_S", new=0):
            lease = await worker_manager.acquire_lease(_LIBRARY, "Upscale")
            worker_manager.release_after_node(lease)
            await asyncio.gather(*worker_manager._engine_notices)

        lost_then_answered = ["ReleaseLeaseRequest"] * 2
        assert [t for t in links.sent_types() if t == "ReleaseLeaseRequest"] == lost_then_answered

    @pytest.mark.asyncio
    async def test_a_notice_that_does_not_arrive_is_sent_again_until_it_is_answered(
        self, worker_manager: WorkerManager, caplog: pytest.LogCaptureFixture
    ) -> None:
        links = _host_engine(worker_manager)
        _offer_session_to(worker_manager, _HOST_ENGINE)
        answer = links.request
        lost = 2
        tries = {"count": 0}

        async def lost_twice(engine_id: str, request: RequestPayload, *, timeout_s: float | None) -> ResultPayload:
            tries["count"] += 1
            if tries["count"] <= lost:
                links.sent.append((engine_id, request, timeout_s))
                msg = "gpu-01 did not answer."
                raise EngineNotFoundError(msg)
            return await answer(engine_id, request, timeout_s=timeout_s)

        worker_manager.engine.request = lost_twice  # type: ignore[method-assign]
        with (
            patch("griptape_nodes.retained_mode.managers.worker_manager._REPORT_RETRY_PAUSE_S", new=0),
            caplog.at_level(logging.WARNING),
        ):
            await worker_manager._report_until_answered(
                _HOST_ENGINE, worker_events.ReleaseLeaseRequest(lease_id="lease-1"), "lease lease-1 was released"
            )

        assert links.sent_types() == ["ReleaseLeaseRequest"] * (lost + 1)
        assert sum("attempt" in record.getMessage() for record in caplog.records) == lost

    @pytest.mark.asyncio
    async def test_a_notice_that_never_arrives_is_given_up_after_the_last_attempt(
        self, worker_manager: WorkerManager, caplog: pytest.LogCaptureFixture
    ) -> None:
        links = _host_engine(worker_manager)
        _offer_session_to(worker_manager, _HOST_ENGINE)
        links.not_found = "gpu-01 did not answer."

        with (
            patch("griptape_nodes.retained_mode.managers.worker_manager._REPORT_RETRY_PAUSE_S", new=0),
            caplog.at_level(logging.WARNING),
        ):
            await worker_manager._report_until_answered(
                _HOST_ENGINE, worker_events.ReleaseLeaseRequest(lease_id="lease-1"), "lease lease-1 was released"
            )

        assert len(links.sent) == _REPORT_ATTEMPTS
        assert any(record.levelno == logging.ERROR for record in caplog.records)

    @pytest.mark.asyncio
    async def test_no_notice_is_sent_to_an_engine_this_engine_has_no_session_with(
        self, worker_manager: WorkerManager
    ) -> None:
        """Such as one it withdrew its session from, or lost: that engine already let the lease go."""
        links = _host_engine(worker_manager)

        await worker_manager._report_until_answered(
            _HOST_ENGINE, worker_events.ReleaseLeaseRequest(lease_id="lease-1"), "lease lease-1 was released"
        )

        assert links.sent == []

    @pytest.mark.asyncio
    async def test_retrying_stops_once_this_engine_has_no_session_with_that_engine(
        self, worker_manager: WorkerManager
    ) -> None:
        links = _host_engine(worker_manager)
        links.not_found = "gpu-01 did not answer."
        _offer_session_to(worker_manager, _HOST_ENGINE)

        async def withdraw_meanwhile(_pause: float) -> None:
            worker_manager._engine_registrations.clear()

        with patch("griptape_nodes.retained_mode.managers.worker_manager.asyncio.sleep", new=withdraw_meanwhile):
            await worker_manager._report_until_answered(
                _HOST_ENGINE, worker_events.ReleaseLeaseRequest(lease_id="lease-1"), "lease lease-1 was released"
            )

        assert len(links.sent) == 1

    @pytest.mark.asyncio
    async def test_losing_that_engine_forgets_the_session_offered_to_it(self, worker_manager: WorkerManager) -> None:
        links = _host_engine(worker_manager)
        _offer_session_to(worker_manager, _HOST_ENGINE)
        _offer_session_to(worker_manager, "another-engine")

        assert links.link_lost is not None
        await links.link_lost(_HOST_ENGINE)

        assert list(worker_manager._engine_registrations) == [("another-engine", _SESSION)]


class TestAskingAnotherEngineForALease:
    @pytest.mark.asyncio
    async def test_a_run_asks_each_engine_once_and_its_end_releases_the_lease(
        self, worker_manager: WorkerManager
    ) -> None:
        links = _host_engine(worker_manager)
        run = _in_run(worker_manager)
        _offer_session_to(worker_manager, _HOST_ENGINE)

        first = await worker_manager.acquire_lease(_LIBRARY, "Upscale")
        worker_manager.release_after_node(first)
        second = await worker_manager.acquire_lease(_LIBRARY, "Denoise")
        worker_manager.release_after_node(second)
        await _settle()
        assert links.sent_types() == ["AcquireLeaseRequest"], "a run's later nodes ask for nothing more"

        run.ended.set_result(None)
        await _settle()
        await asyncio.gather(*worker_manager._engine_notices)

        assert first is not None
        assert first is second
        (acquire,) = links.sent_of(worker_events.AcquireLeaseRequest)
        assert cast("worker_events.AcquireLeaseRequest", acquire).run_id == "run-1"
        assert links.sent[0][2] is None, "a lease waits as long as the run ahead of it"
        (release,) = links.sent_of(worker_events.ReleaseLeaseRequest)
        assert cast("worker_events.ReleaseLeaseRequest", release).lease_id == first.lease_id
        assert {engine_id for engine_id, _request, _timeout_s in links.sent} == {_HOST_ENGINE}
        assert worker_manager._leases == {}

    @pytest.mark.asyncio
    async def test_a_run_s_next_node_asks_for_a_new_lease_after_that_engine_was_lost(
        self, worker_manager: WorkerManager
    ) -> None:
        """That engine forgot the lease with the connection, so the old one would be refused."""
        links = _host_engine(worker_manager)
        _in_run(worker_manager)
        _offer_session_to(worker_manager, _HOST_ENGINE)
        before = await worker_manager.acquire_lease(_LIBRARY, "Upscale")
        worker_manager.release_after_node(before)

        await worker_manager.on_engine_lost(_HOST_ENGINE)
        _offer_session_to(worker_manager, _HOST_ENGINE)
        after = await worker_manager.acquire_lease(_LIBRARY, "Denoise")

        assert before is not None
        assert after is not None
        assert after.lease_id != before.lease_id
        assert links.sent_types().count("AcquireLeaseRequest") == len([before, after])
        assert "ReleaseLeaseRequest" not in links.sent_types(), "a lost engine is not told to release"

    @pytest.mark.asyncio
    async def test_a_run_s_next_node_asks_for_a_new_lease_after_withdrawing_its_offers(
        self, worker_manager: WorkerManager
    ) -> None:
        """That engine forgets the leases with the offer, so the old one would be refused."""
        links = _host_engine(worker_manager)
        _in_run(worker_manager)
        _offer_session_to(worker_manager, _HOST_ENGINE)
        before = await worker_manager.acquire_lease(_LIBRARY, "Upscale")
        worker_manager.release_after_node(before)

        worker_manager._withdraw_session_offers()
        _offer_session_to(worker_manager, _HOST_ENGINE)
        after = await worker_manager.acquire_lease(_LIBRARY, "Denoise")

        assert before is not None
        assert after is not None
        assert after.lease_id != before.lease_id
        assert links.sent_types().count("AcquireLeaseRequest") == len([before, after])

    @pytest.mark.asyncio
    async def test_a_node_outside_any_run_has_a_lease_of_its_own_released_when_it_ends(
        self, worker_manager: WorkerManager
    ) -> None:
        links = _host_engine(worker_manager)
        _offer_session_to(worker_manager, _HOST_ENGINE)

        first = await worker_manager.acquire_lease(_LIBRARY, "Upscale")
        worker_manager.release_after_node(first)
        second = await worker_manager.acquire_lease(_LIBRARY, "Denoise")
        worker_manager.release_after_node(second)
        await asyncio.gather(*worker_manager._engine_notices)

        assert first is not None
        assert second is not None
        assert first.lease_id != second.lease_id
        assert [
            cast("worker_events.ReleaseLeaseRequest", r).lease_id
            for r in links.sent_of(worker_events.ReleaseLeaseRequest)
        ] == [
            first.lease_id,
            second.lease_id,
        ]

    @pytest.mark.asyncio
    async def test_a_run_stops_waiting_when_its_acquire_is_answered_even_without_a_last_report(
        self, worker_manager: WorkerManager
    ) -> None:
        """Otherwise a lost last report would leave the run listed as waiting while its node runs."""
        links = _host_engine(worker_manager)
        links.blocked["AcquireLeaseRequest"] = asyncio.Event()
        report = worker_manager.engine.flow_manager.report_run_waiting_for_engine
        _in_run(worker_manager)
        acquiring = asyncio.create_task(worker_manager.acquire_lease(_LIBRARY, "Upscale"))
        await _settle()
        (acquire,) = links.sent_of(worker_events.AcquireLeaseRequest)
        lease_id = cast("worker_events.AcquireLeaseRequest", acquire).lease_id
        await _dispatched_by(
            worker_manager, _FROM_HOST_ENGINE, worker_events.ReportPlaceInLineRequest(lease_id=lease_id, position=2)
        )

        links.blocked["AcquireLeaseRequest"].set()
        await acquiring
        await _settle()
        late = await _dispatched_by(
            worker_manager, _FROM_HOST_ENGINE, worker_events.ReportPlaceInLineRequest(lease_id=lease_id, position=1)
        )

        assert isinstance(late, EventResultSuccess)
        assert [c.args for c in report.call_args_list] == [("run-1", 2), ("run-1", 0)]  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_a_library_that_runs_here_needs_no_lease(self, worker_manager: WorkerManager) -> None:
        links = _host_engine(worker_manager)

        assert await worker_manager.acquire_lease("Local Library", "Upscale") is None
        assert links.sent == []

    @pytest.mark.asyncio
    async def test_a_refused_lease_fails_the_node_saying_why_and_the_run_s_next_node_asks_again(
        self, worker_manager: WorkerManager
    ) -> None:
        links = _host_engine(
            worker_manager, refuse={"AcquireLeaseRequest": "the session asking for it never registered"}
        )
        _in_run(worker_manager)

        with pytest.raises(RuntimeError, match="never registered"):
            await worker_manager.acquire_lease(_LIBRARY, "Upscale")
        assert worker_manager._leases == {}
        with pytest.raises(RuntimeError, match="never registered"):
            await worker_manager.acquire_lease(_LIBRARY, "Denoise")

        assert links.sent_types().count("AcquireLeaseRequest") == 2  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_an_engine_that_cannot_be_reached_fails_the_node_and_ends_the_lease(
        self, worker_manager: WorkerManager
    ) -> None:
        links = _host_engine(worker_manager)
        links.not_found = "gpu-01 did not answer."

        with pytest.raises(RuntimeError, match="gpu-01 did not answer"):
            await worker_manager.acquire_lease(_LIBRARY, "Upscale")

        assert worker_manager._leases == {}

    @pytest.mark.asyncio
    async def test_a_node_cancelled_while_its_run_s_lease_waits_leaves_the_lease_to_the_run(
        self, worker_manager: WorkerManager
    ) -> None:
        links = _host_engine(worker_manager)
        links.blocked["AcquireLeaseRequest"] = asyncio.Event()
        run = _in_run(worker_manager)
        _offer_session_to(worker_manager, _HOST_ENGINE)
        cancelled = asyncio.create_task(worker_manager.acquire_lease(_LIBRARY, "Upscale"))
        waiting = asyncio.create_task(worker_manager.acquire_lease(_LIBRARY, "Denoise"))
        await _settle()

        cancelled.cancel()
        await _settle()
        links.blocked["AcquireLeaseRequest"].set()
        lease = await waiting

        assert cancelled.cancelled()
        assert lease is not None
        assert links.sent_of(worker_events.ReleaseLeaseRequest) == []
        run.ended.set_result(None)
        await _settle()
        await asyncio.gather(*worker_manager._engine_notices)
        assert len(links.sent_of(worker_events.ReleaseLeaseRequest)) == 1

    @pytest.mark.asyncio
    async def test_a_node_outside_any_run_cancelled_while_its_lease_waits_releases_it(
        self, worker_manager: WorkerManager
    ) -> None:
        links = _host_engine(worker_manager)
        links.blocked["AcquireLeaseRequest"] = asyncio.Event()
        _offer_session_to(worker_manager, _HOST_ENGINE)
        acquiring = asyncio.create_task(worker_manager.acquire_lease(_LIBRARY, "Upscale"))
        await _settle()

        acquiring.cancel()
        await _settle()
        await asyncio.gather(*worker_manager._engine_notices)

        assert worker_manager._leases == {}
        assert len(links.sent_of(worker_events.ReleaseLeaseRequest)) == 1

    @pytest.mark.asyncio
    async def test_the_run_s_place_in_line_there_is_reported_as_this_run_waiting(
        self, worker_manager: WorkerManager
    ) -> None:
        links = _host_engine(worker_manager)
        links.blocked["AcquireLeaseRequest"] = asyncio.Event()
        _in_run(worker_manager)
        acquiring = asyncio.create_task(worker_manager.acquire_lease(_LIBRARY, "Upscale"))
        await _settle()
        (acquire,) = links.sent_of(worker_events.AcquireLeaseRequest)
        lease_id = cast("worker_events.AcquireLeaseRequest", acquire).lease_id

        result = await _dispatched_by(
            worker_manager, _FROM_HOST_ENGINE, worker_events.ReportPlaceInLineRequest(lease_id=lease_id, position=2)
        )

        assert isinstance(result, EventResultSuccess)
        worker_manager.engine.flow_manager.report_run_waiting_for_engine.assert_called_once_with(  # type: ignore[attr-defined]
            "run-1", 2
        )
        links.blocked["AcquireLeaseRequest"].set()
        await acquiring

    @pytest.mark.asyncio
    async def test_only_the_engine_it_waits_on_may_report_its_place(self, worker_manager: WorkerManager) -> None:
        links = _host_engine(worker_manager)
        links.blocked["AcquireLeaseRequest"] = asyncio.Event()
        acquiring = asyncio.create_task(worker_manager.acquire_lease(_LIBRARY, "Upscale"))
        await _settle()
        (acquire,) = links.sent_of(worker_events.AcquireLeaseRequest)
        lease_id = cast("worker_events.AcquireLeaseRequest", acquire).lease_id

        result = await _dispatched_by(
            worker_manager,
            RequestSender(engine_id="some-other-engine", session_id=None),
            worker_events.ReportPlaceInLineRequest(lease_id=lease_id, position=2),
        )

        assert isinstance(result, EventResultFailure)
        worker_manager.engine.flow_manager.report_run_waiting_for_engine.assert_not_called()  # type: ignore[attr-defined]
        links.blocked["AcquireLeaseRequest"].set()
        await acquiring

    @pytest.mark.asyncio
    async def test_a_worker_its_engine_stopped_is_started_again_before_the_next_node(
        self, worker_manager: WorkerManager
    ) -> None:
        _host_engine(worker_manager)

        with patch.object(worker_manager, "handle_start_worker_request", new=AsyncMock()) as start:
            await worker_manager.wait_until_executable(_LIBRARY)

        start.assert_awaited_once_with(worker_events.StartWorkerRequest(library_name=_LIBRARY))

    @pytest.mark.asyncio
    async def test_a_worker_that_ended_on_its_own_is_not_started_again(self, worker_manager: WorkerManager) -> None:
        _host_engine(worker_manager)
        worker_manager.note_worker_unavailable(_LIBRARY, "the worker process that runs it on gpu-01 stopped.")

        with patch.object(worker_manager, "handle_start_worker_request", new=AsyncMock()) as start:
            await worker_manager.wait_until_executable(_LIBRARY)

        start.assert_not_awaited()
