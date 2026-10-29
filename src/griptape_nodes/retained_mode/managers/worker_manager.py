from __future__ import annotations

import asyncio
import functools
import json
import logging
import math
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import anyio

from griptape_nodes.drivers.storage.local_storage_driver import LocalStorageDriver
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.engine_transports import (
    DISTRIBUTED_EXECUTION_SCHEMA_VERSION,
    EngineNotFoundError,
)
from griptape_nodes.retained_mode.events import converters, worker_events
from griptape_nodes.retained_mode.events.app_events import (
    ConfigChanged,
    CurrentProjectChanged,
    ReportGeneralPurposeWorkerReadyRequest,
    ReportGeneralPurposeWorkerReadyResultSuccess,
    SecretChanged,
)
from griptape_nodes.retained_mode.events.base_events import RESULT_EVENT_TYPES, EventRequest
from griptape_nodes.retained_mode.events.converters import EventSerializationError
from griptape_nodes.retained_mode.managers.event_manager import require_request_sender
from griptape_nodes.retained_mode.managers.external_environment import (
    WorkerCommand,
    WorkerCommandRefusal,
    provisioned_by_environment,
    read_worker_command_prefix,
    resolve_worker_command,
    worker_requests_from_environment,
)
from griptape_nodes.retained_mode.managers.flow_manager import current_run_id
from griptape_nodes.retained_mode.managers.library.common import LibraryLifecycleState
from griptape_nodes.retained_mode.managers.settings import (
    DISTRIBUTED_EXECUTION_IDLE_TIMEOUT_KEY,
    DISTRIBUTED_EXECUTION_LIBRARY_ENGINES_KEY,
    DISTRIBUTED_EXECUTION_MAX_WORKERS_KEY,
    DISTRIBUTED_EXECUTION_RELEASE_HOW_KEY,
    DISTRIBUTED_EXECUTION_RELEASE_WHICH_KEY,
    WORKER_HEARTBEAT_INTERVAL_KEY,
    WORKER_HEARTBEAT_TIMEOUT_KEY,
    WORKER_LIBRARY_LOAD_TIMEOUT_KEY,
)
from griptape_nodes.retained_mode.request_handlers import RequiredSender, handles
from griptape_nodes.serialization.values import untag
from griptape_nodes.servers.static import ORCHESTRATOR_STATIC_SERVER_BASE_URL_ENV
from griptape_nodes.utils.version_utils import engine_version

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    from griptape_nodes.api_client.request_client import RequestClient
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import RequestPayload, RequestSender, ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager
    from griptape_nodes.retained_mode.managers.run_queue import Run

logger = logging.getLogger("griptape_nodes_app")

# How long a spawn waits for initialization to decide the static server URL. Resolution is a socket
# bind, so seconds -- not the startup grace, whose 10 minutes is sized for dependency installs. On a
# restart into an existing session it resolves in the same event fan-out that triggers the spawn; on
# a fresh boot the spawn comes later, off AppSessionStartedEvent, by which point it is long settled.
_STATIC_URL_SETTLE_TIMEOUT_S = 30.0

# How long an engine waits for another engine to answer a request. Starting a worker
# answers once its process exists, so this bounds an engine that is down or unreachable.
_ENGINE_REQUEST_TIMEOUT_S = 60.0


@dataclass
class _SessionRegistration:
    """What an engine knows about an artist's session on another engine it runs workers for."""

    engine_id: str
    static_server_base_url: str | None
    # The version of each library the session declared, checked against this engine's again at every
    # start, since this engine's libraries can change after the session registered.
    library_versions: dict[str, str]


@dataclass
class _RemoteStart:
    """A worker the artist's engine asked another engine to start, until it registers, fails, or is withdrawn."""

    host_engine_id: str
    library_name: str


@dataclass
class _HostedWorker:
    """A worker this engine runs for an artist's session on another engine."""

    worker_key: str
    engine_id: str
    session_id: str
    # None until the process exists. Stopping compares it to the managed process for the key, which a
    # newer start for the same library may already have replaced.
    process: asyncio.subprocess.Process | None = None
    # Set once the start has spawned the process or given up, which a stop sent meanwhile waits for.
    started: asyncio.Event = field(default_factory=asyncio.Event)
    # Reports the process's exit to the artist's engine, done once that engine has answered.
    exit_watcher: asyncio.Task | None = None


@dataclass
class _EngineRun:
    """Another engine's run on this engine, waiting in this engine's line of runs or holding this engine."""

    engine_id: str
    # The run's ID on that engine, or None for a node sent without a run, which is a run of its own.
    run_id: str | None
    key: tuple[str, str]
    # Resolved with None once the run has this engine, or with why it never will.
    admitted: asyncio.Future[str | None]
    # Set when the run lets this engine go.
    released: asyncio.Event = field(default_factory=asyncio.Event)
    # This engine's own run that stands for it in the line, set once it is submitted.
    line_run: Run | None = None
    # Admissions of its nodes still waiting, which are told the run's place in line.
    waiting: set[str] = field(default_factory=set)
    # Admissions of its nodes running here.
    running: set[str] = field(default_factory=set)
    # Admissions whose node ended before it was admitted.
    abandoned: set[str] = field(default_factory=set)
    # Lets the run go once none of its nodes has run here for the idle timeout.
    idle_timer: asyncio.TimerHandle | None = None


@dataclass
class _EngineRunSpec:
    """A run of another engine's nodes, which holds this engine from when it starts until that run lets it go."""

    manager: WorkerManager
    engine_run: _EngineRun
    flow_name: str

    async def execute(self, engine: Engine) -> ResultPayload:  # noqa: ARG002
        return await self.manager.hold_for_engine_run(self.engine_run)

    def failure(self, details: str) -> ResultPayload:
        self.manager.refuse_engine_run(self.engine_run, details)
        return worker_events.AdmitNodeResultFailure(result_details=details)


@dataclass
class EngineAdmission:
    """A node another engine was asked to admit, which that engine must be told has ended."""

    admission_id: str
    host_engine_id: str
    # The run on this engine the node belongs to, or None for a node run on its own.
    run_id: str | None
    # The request asking for it, done once that engine has answered, or cancelled to give up on it.
    request_task: asyncio.Task[ResultPayload] | None = None
    # Why it was given up, set before `request_task` is cancelled.
    given_up: str | None = None


@dataclass
class WorkerRegistration:
    """Tracks a registered worker's routing topic and optional library key.

    worker_key is the library_name the worker was spawned for, or None for
    general-purpose workers.
    """

    request_topic: str
    worker_key: str | None
    # Challenges sent since this worker last answered one. Eviction counts these rather than
    # measuring elapsed time, because the sweep shares a loop with library load: a wall-clock
    # timeout charges the orchestrator's own latency to the worker and evicts one that was
    # never asked.
    unanswered_challenges: int = 0
    # Engine ID of the engine that runs this worker, or None for a local worker.
    host_engine_id: str | None = None


@dataclass
class _WorkerTransport:
    """Transport-layer dependencies for WorkerManager.

    Held separately from WorkerManager so the manager can be constructed up
    front (e.g. by the Engine) and wired to a concrete transport later, once
    the WebSocket client and request client exist.
    """

    send_message: Callable[[str, str, str | None], Awaitable[None]]
    subscribe_to_topic: Callable[[str], Awaitable[None]]
    unsubscribe_from_topic: Callable[[str], Awaitable[None]]
    request_client: RequestClient


class WorkerManager(EngineScoped):
    """Manages worker registration, heartbeating, eviction, and event routing.

    Encapsulates all state and logic related to worker engines on both the
    orchestrator side (registry, heartbeat challenges, eviction, result relay)
    and the worker side (heartbeat response, self-termination monitor).

    Transport operations (subscribe, unsubscribe, send message) are injected
    as callables so this class has no direct dependency on WebSocket plumbing.
    """

    DEFAULT_HEARTBEAT_INTERVAL_S: float = 5.0
    DEFAULT_HEARTBEAT_TIMEOUT_S: float = 15.0
    # Floor on the configured interval. Zero or negative is a configuration a human can write and
    # the code cannot run: the interval divides the timeout to size the challenge allowance, and it
    # is the sleep in both heartbeat loops, so a non-positive value is a ZeroDivisionError on one
    # path and a hot loop on the other. Low enough that any interval meant seriously survives it.
    MINIMUM_HEARTBEAT_INTERVAL_S: float = 0.1
    # Floor on the silence a worker tolerates before shutting itself down. Deliberately above the
    # heartbeat timeout; see `orchestrator_silence_allowed_s` for why the two sides differ.
    MINIMUM_ORCHESTRATOR_SILENCE_S: float = 30.0
    # How long a worker may take to load its library (venv creation, installs, imports): the ceiling
    # on `wait_until_executable` and on each worker's reply to a project-switch fan-out. Not a
    # heartbeat bound on either side: a worker keeps answering challenges while it loads.
    DEFAULT_LIBRARY_LOAD_TIMEOUT_S: float = 600.0
    # How long to wait for a worker to exit after SIGTERM before escalating to
    # SIGKILL. Workers convert SIGTERM into a cooperative shutdown on their event
    # loop; a wedged loop never services it, so SIGTERM alone can leak the process.
    DEFAULT_TERMINATE_GRACE_S: float = 10.0

    # Ceiling on awaiting a cross-loop termination hopped onto the spawning loop.
    # The hopped coroutine itself can take up to DEFAULT_TERMINATE_GRACE_S, so this
    # must exceed it; the extra margin covers the SIGKILL escalation and reap. It
    # bounds the case where the spawning loop closes after the hop is scheduled but
    # before it completes, so the evicting loop never blocks forever on a future
    # that will never resolve.
    DEFAULT_TERMINATE_HOP_TIMEOUT_S: float = 15.0

    _WORKER_RESPONSE_TOPIC_RE: re.Pattern = re.compile(r"sessions/[^/]+/workers/(?P<worker_engine_id>[^/]+)/response$")

    def __init__(
        self,
        *,
        engine: Engine,
        event_manager: EventManager,
    ) -> None:
        super().__init__(engine)
        self._event_manager = event_manager
        self._transport: _WorkerTransport | None = None

        # Workers another engine was asked to start, by the engine ID this engine chose for them,
        # from sending the start until the worker registers, the start fails, or it is withdrawn.
        # Their worker registers here like a local one.
        self._remote_starts: dict[str, _RemoteStart] = {}
        # Libraries whose last remote start was refused for capacity, which their next node asks for again.
        self._refused_for_capacity: set[str] = set()

        # This engine's registration with each engine it sends work to, by (host_engine_id, session).
        self._engine_registrations: dict[tuple[str, str], asyncio.Task[ResultPayload]] = {}

        # Heartbeats unanswered by each engine there is a session with, and the one in flight to each.
        self._engine_challenges_unanswered: dict[str, int] = {}
        self._engine_challenges: dict[str, asyncio.Task[None]] = {}

        # Artists' sessions on other engines registered here, by session id.
        self._sessions: dict[str, _SessionRegistration] = {}

        # Workers this engine runs for artists' sessions on other engines, by worker engine ID.
        self._hosted_workers: dict[str, _HostedWorker] = {}

        # Requests telling an engine to stop a worker whose start was withdrawn, held so they are
        # not garbage-collected while they wait for the answer.
        self._stop_requests: set[asyncio.Task] = set()

        # Other engines' runs on this engine, waiting in its line or holding it, by (engine, run).
        self._engine_runs: dict[tuple[str, str], _EngineRun] = {}

        # The engine whose run last held this engine, so the next run from another engine frees its workers.
        self._last_engine_run_engine_id: str | None = None

        # Nodes this engine asked other engines to admit, by admission ID, until each ends.
        self._admissions: dict[str, EngineAdmission] = {}

        # The engines each of this engine's runs sent a node to, so the run's end reaches each of them.
        self._run_engines: dict[str, set[str]] = {}

        # Reports and run ends sent to other engines from synchronous callbacks, held until answered.
        self._engine_notices: set[asyncio.Task] = set()

        # Orchestrator-side registry: worker_engine_id → WorkerRegistration
        self._workers: dict[str, WorkerRegistration] = {}

        # Subprocesses spawned by this orchestrator (library_name → process; None for the general-purpose worker)
        self._managed_worker_processes: dict[str | None, asyncio.subprocess.Process] = {}

        # Worker keys whose spawn has been claimed but has not yet reached the registry above,
        # each mapped to a token identifying the attempt that holds it. Keyed by attempt rather
        # than by name alone so a spawn can only ever release its own claim. The claim is set once
        # the attempt ends, so a caller that must not race the spawn can wait for it.
        self._spawns_in_flight: dict[str | None, asyncio.Event] = {}

        # The spawn task each accepted StartWorkerRequest scheduled, by library, until it finishes. A
        # library's worker is requested when it finishes registering and again when a session starts,
        # and the two can overlap; finding the first here is what makes the second start nothing.
        self._requested_spawns: dict[str | None, asyncio.Task] = {}

        # The event loop that spawned the worker subprocesses. asyncio.subprocess.Process
        # binds its exit Future to its creating loop, so proc.wait() is only legal on this
        # loop. Eviction can run on a different loop (the websocket-tasks loop), which is
        # why termination is hopped back here. Captured in spawn_worker.
        self._spawn_loop: asyncio.AbstractEventLoop | None = None

        # One task per spawned process, waiting for it to exit; held here so the loop's weak-ref to
        # tasks does not GC a watcher before its process ends.
        self._exit_watchers: set[asyncio.Task] = set()

        # Worker-side: monotonic timestamp of last heartbeat received from the orchestrator
        self._worker_heartbeat_last_received_at: float = 0.0

        # Fire-and-forget broadcast tasks scheduled from sync callers; held here so
        # the event loop's weak-ref to tasks does not GC them before completion.
        self._inflight_broadcast_tasks: set[asyncio.Task] = set()

        # Set when an active session becomes available; gates worker spawning.
        self._session_ready_event: asyncio.Event = asyncio.Event()

        # Whether a library's execution is available yet, and why not, keyed by library name, with
        # None for the general-purpose worker. Held here rather than on LibraryInfo because they
        # answer questions about a PROCESS: is one coming, has it loaded the library, did it die.
        # One owner, so one writer.
        self._execution_ready: dict[str | None, asyncio.Event] = {}
        self._worker_unavailable: dict[str | None, str] = {}

        config = engine.config_manager
        configured_interval_s: float = config.get_config_value(
            WORKER_HEARTBEAT_INTERVAL_KEY, default=WorkerManager.DEFAULT_HEARTBEAT_INTERVAL_S, cast_type=float
        )
        self.heartbeat_interval_s: float = max(WorkerManager.MINIMUM_HEARTBEAT_INTERVAL_S, configured_interval_s)
        if self.heartbeat_interval_s != configured_interval_s:
            logger.warning(
                "Worker heartbeat interval of %.3gs cannot be used; running at the %.3gs minimum instead. "
                "Set '%s' to a positive number of seconds.",
                configured_interval_s,
                self.heartbeat_interval_s,
                WORKER_HEARTBEAT_INTERVAL_KEY,
            )
        self.heartbeat_timeout_s: float = config.get_config_value(
            WORKER_HEARTBEAT_TIMEOUT_KEY, default=WorkerManager.DEFAULT_HEARTBEAT_TIMEOUT_S, cast_type=float
        )
        self.library_load_timeout_s: float = config.get_config_value(
            WORKER_LIBRARY_LOAD_TIMEOUT_KEY,
            default=WorkerManager.DEFAULT_LIBRARY_LOAD_TIMEOUT_S,
            cast_type=float,
        )

        event_manager.register_request_handlers(self)
        # A run's end reaches every engine it sent a node to, and another engine's run waiting here
        # hears its place in line.
        engine.flow_manager.add_run_ended_listener(self._on_run_ended)
        engine.flow_manager.add_place_in_line_listener(self._on_place_in_line)

        # Subscribe to domain events from ConfigManager / SecretsManager so
        # those managers don't have to know workers exist. The listeners are
        # the single place that translates a "something changed" signal into
        # a worker fan-out.
        event_manager.add_listener_to_app_event(ConfigChanged, self._on_config_changed)
        event_manager.add_listener_to_app_event(SecretChanged, self._on_secret_changed)
        event_manager.add_listener_to_app_event(CurrentProjectChanged, self._on_current_project_changed)

    @property
    def unanswered_challenges_allowed(self) -> int:
        """How many challenges a worker may leave unanswered before it is evicted.

        Derived from the two configured values rather than stored, so tuning either takes effect
        without a restart. Rounded up, not nearest: with a 12s timeout and a 5s interval, nearest
        gives 2 challenges and evicts after about 10s, tolerating less silence than the timeout
        asks for. At least one, so no configuration evicts a worker the first time it is asked.

        Note that the timeout is read two ways. Here it sizes an allowance in challenges, which is
        why a slow sweep cannot evict a worker that was never asked. On the worker,
        `orchestrator_silence_allowed_s` turns it into elapsed time, because a worker can only
        measure silence from a peer it cannot poll.
        """
        return max(1, math.ceil(self.heartbeat_timeout_s / self.heartbeat_interval_s))

    @property
    def orchestrator_silence_allowed_s(self) -> float:
        """How much orchestrator silence a worker tolerates before shutting itself down.

        Above `heartbeat_timeout_s`, because the two sides do not measure the same thing. The
        orchestrator counts unanswered challenges, so its sweep running late costs a worker nothing.
        The worker has only wall-clock time, and a late sweep is indistinguishable there from an
        orchestrator that is gone. That sweep shares an event loop with library loading and has been
        measured 18 seconds late during boot, so a worker held to the eviction timeout would kill
        itself over the orchestrator's own latency.
        """
        return max(self.heartbeat_timeout_s, WorkerManager.MINIMUM_ORCHESTRATOR_SILENCE_S)

    @property
    def _tx(self) -> _WorkerTransport:
        if self._transport is None:
            msg = "WorkerManager transport has not been attached; call attach_transport() before use."
            raise RuntimeError(msg)
        return self._transport

    def attach_transport(
        self,
        *,
        send_message: Callable[[str, str, str | None], Awaitable[None]],
        subscribe_to_topic: Callable[[str], Awaitable[None]],
        unsubscribe_from_topic: Callable[[str], Awaitable[None]],
        request_client: RequestClient,
        ws_outgoing_queue: asyncio.Queue | None = None,  # noqa: ARG002
    ) -> None:
        """Bind the transport-layer callables used for WebSocket I/O.

        Called once the WebSocket client and RequestClient exist. Until this is
        called, methods that depend on the transport will raise RuntimeError.

        `ws_outgoing_queue` is ignored. Accepted so app releases that still pass it keep working.
        TODO(https://github.com/griptape-ai/griptape-nodes-app/issues/254): remove.
        """
        self._transport = _WorkerTransport(
            send_message=send_message,
            subscribe_to_topic=subscribe_to_topic,
            unsubscribe_from_topic=unsubscribe_from_topic,
            request_client=request_client,
        )

    @handles(worker_events.RegisterWorkerRequest)
    async def handle_register_worker_request(
        self,
        request: worker_events.RegisterWorkerRequest,
    ) -> worker_events.RegisterWorkerResultSuccess | worker_events.RegisterWorkerResultFailure:
        """Handle a worker registration request from a worker engine."""
        wid = request.worker_engine_id
        if request.engine_version != engine_version:
            details = (
                f"Worker {wid} reported engine_version '{request.engine_version}' "
                f"but orchestrator is running engine_version '{engine_version}'. "
                "Workers and orchestrators must share an engine version because the "
                "wire shape of every event is tied to the engine build."
            )
            return worker_events.RegisterWorkerResultFailure(result_details=details)
        remote = self._remote_starts.get(wid)
        assigned_engine = self._engine_for_library(request.library_name) if request.library_name else None
        if remote is not None and remote.library_name != request.library_name:
            details = (
                f"Attempted to register worker {wid} for library '{request.library_name}'. Failed because "
                f"this engine asked {remote.host_engine_id} to start that worker for library '{remote.library_name}'."
            )
            return worker_events.RegisterWorkerResultFailure(result_details=details)
        if remote is None and assigned_engine is not None:
            details = (
                f"Attempted to register worker {wid} for library '{request.library_name}'. Failed because "
                f"this engine did not ask {assigned_engine} to start a worker with that ID."
            )
            return worker_events.RegisterWorkerResultFailure(result_details=details)
        host_engine_id = None
        if remote is not None:
            del self._remote_starts[wid]
            host_engine_id = remote.host_engine_id

        session_id = self.engine.get_session_id()
        request_topic = f"sessions/{session_id}/workers/{wid}/request"
        self._workers[wid] = WorkerRegistration(
            request_topic=request_topic, worker_key=request.library_name, host_engine_id=host_engine_id
        )

        if request.library_name:
            logger.debug("Worker registered: %s → library '%s'", wid, request.library_name)
        else:
            logger.debug("Worker registered: %s (general-purpose)", wid)
        if host_engine_id is not None:
            logger.debug("Worker %s runs on engine %s", wid, host_engine_id)

        response_topic = f"sessions/{session_id}/workers/{wid}/response"
        await self._tx.subscribe_to_topic(response_topic)
        # Put the worker on this orchestrator's project rather than answering with it. One sender
        # and one adoption path, so a switch landing mid-registration is a second message on the
        # same channel instead of a reply racing a fan-out.
        await self._activate_project_on_worker(wid, request_topic)
        return worker_events.RegisterWorkerResultSuccess(
            worker_engine_id=wid,
            result_details="Worker registered successfully.",
        )

    async def _activate_project_on_worker(self, worker_engine_id: str, worker_request_topic: str) -> None:
        """Tell one worker which project to be on, as of the last activation that committed.

        Sent without awaiting a reply: registration must not depend on a round trip back into the
        worker, which would make answering it hostage to a worker that is merely slow. The worker
        blocks on having applied an activation before it loads a library instead, which is a wait it
        can bound locally.

        The COMMITTED pair rather than the live id: `_current_project_id` is assigned before
        activation's fallible steps, so reading it mid-switch could name a project about to be
        rolled back. Both halves come from one read, so the generation always describes the id it
        was committed with. Sent for system defaults too -- a worker has to be told what it is on
        even when that is the rest state, or nothing distinguishes "told" from "not yet told".
        """
        from griptape_nodes.app.worker_routing import ActivateProjectRequest

        project_id, generation = self.engine.project_manager.committed_project()
        await self.forward_event_to_worker(
            EventRequest(request=ActivateProjectRequest(project_id=project_id, generation=generation)),
            worker_engine_id=worker_engine_id,
            worker_request_topic=worker_request_topic,
        )

    @handles(worker_events.WorkerHeartbeatRequest)
    def handle_worker_heartbeat_request(
        self,
        request: worker_events.WorkerHeartbeatRequest,
    ) -> worker_events.WorkerHeartbeatResultSuccess:
        """Respond to an orchestrator heartbeat challenge."""
        self._worker_heartbeat_last_received_at = time.monotonic()
        return worker_events.WorkerHeartbeatResultSuccess(
            heartbeat_id=request.heartbeat_id,
            result_details="Worker alive.",
        )

    @handles(worker_events.UnregisterWorkerRequest)
    async def handle_unregister_worker_request(
        self,
        request: worker_events.UnregisterWorkerRequest,
    ) -> worker_events.UnregisterWorkerResultSuccess | worker_events.UnregisterWorkerResultFailure:
        """Handle a worker unregister request from a worker engine."""
        wid = request.worker_engine_id
        session_id = self.engine.get_session_id()
        registration = self._workers.pop(wid, None)
        worker_key = registration.worker_key if registration else None
        response_topic = f"sessions/{session_id}/workers/{wid}/response"
        await self._tx.unsubscribe_from_topic(response_topic)
        # Remove the managed process entry so a new worker can be spawned for this key.
        if registration is not None:
            removed = self._managed_worker_processes.pop(worker_key, None)
            if removed is not None:
                logger.debug(
                    "Worker unregistered: removed managed process for key '%s' (pid %s)", worker_key, removed.pid
                )
            # A worker leaving before its library settled releases nothing otherwise: the registry pop
            # above also removes the entry eviction would have released it through. A clean shutdown
            # unregisters too, but with its gate already set, so has_settled keeps it out.
            if not self.has_settled(worker_key):
                self.note_worker_unavailable(worker_key, "the worker process that runs it shut down before loading it.")
        logger.debug("Worker unregistered: %s", wid)
        return worker_events.UnregisterWorkerResultSuccess(worker_engine_id=wid, result_details="Worker unregistered.")

    async def orchestrator_heartbeat_loop(self) -> None:
        """Challenge each registered worker, and each engine there is a session with, on an interval.

        Evicts the workers, and loses the engines, that stop answering.
        """
        while True:
            await asyncio.sleep(self.heartbeat_interval_s)
            await self._challenge_engines()
            if not self._workers:
                continue

            for wid in [
                wid
                for wid, registration in self._workers.items()
                if registration.unanswered_challenges >= self.unanswered_challenges_allowed
            ]:
                await self.evict_worker(wid)

            session_id = self.engine.get_session_id()
            for wid, registration in list(self._workers.items()):
                hb = EventRequest(
                    request=worker_events.WorkerHeartbeatRequest(heartbeat_id=str(uuid.uuid4())),
                    response_topic=f"sessions/{session_id}/workers/{wid}/response",
                )
                # Only a challenge that went out counts against the worker, and a send that fails
                # must not end the loop: the transport raises while a connection is re-establishing,
                # and both charging that to the worker and leaving nothing to evict it are the
                # orchestrator's problem becoming the worker's.
                try:
                    await self._tx.send_message("EventRequest", converters.engine.dumps(hb), registration.request_topic)
                except Exception:
                    logger.warning(
                        "Could not challenge worker %s on '%s'; not counting it against the worker.",
                        wid,
                        registration.request_topic,
                        exc_info=True,
                    )
                    continue
                registration.unanswered_challenges += 1
                logger.debug(
                    "Challenged worker %s on '%s'; %d unanswered of %d allowed.",
                    wid,
                    registration.request_topic,
                    registration.unanswered_challenges,
                    self.unanswered_challenges_allowed,
                )

    async def worker_heartbeat_monitor(self) -> None:
        """Shut down the worker if orchestrator heartbeats stop arriving.

        Enforced from the start, as the orchestrator's side is: it challenges a worker from the
        moment the worker registers, with no grace period. A worker loading its library keeps
        answering, because the host answers heartbeats on a different event loop from the one that
        loads libraries. Silence is measured from the later of the last heartbeat and this monitor
        starting, so a first challenge still in flight is not counted as silence.

        Tolerates `orchestrator_silence_allowed_s` rather than the eviction timeout, which is longer
        by the margin the orchestrator's sweep needs to run late without being mistaken for a dead one.

        Does not mutate `_worker_heartbeat_last_received_at`; that attribute is owned by
        `handle_worker_heartbeat_request`.
        """
        started_at = time.monotonic()
        while True:
            await asyncio.sleep(self.heartbeat_interval_s)
            last_heard_at = max(self._worker_heartbeat_last_received_at, started_at)
            elapsed = time.monotonic() - last_heard_at
            if elapsed > self.orchestrator_silence_allowed_s:
                msg = f"Orchestrator heartbeat lost ({elapsed:.1f}s since last heartbeat); worker is shutting down."
                logger.warning(msg)
                raise RuntimeError(msg)

    def is_worker_running_or_starting(self, worker_key: str | None) -> bool:
        """Whether a worker for worker_key has been requested, is a process this orchestrator spawned, or is registered.

        Read from the records that stopping a worker clears, not from its readiness gate: a reload
        kills a worker that is still loading without settling the gate, and that worker is gone.
        """
        if worker_key in self._requested_spawns:
            return True
        if worker_key in self._managed_worker_processes:
            return True
        # Another engine answering that it started the worker ends the request above, and the worker
        # registers only later; a second start in between would replace the readiness gate.
        if any(start.library_name == worker_key for start in self._remote_starts.values()):
            return True
        return self.get_worker_for_key(worker_key) is not None

    def get_worker_for_key(self, key: str | None) -> tuple[str, str] | None:
        """Return (worker_engine_id, worker_request_topic) for a worker registered under key, or None.

        A key of None finds a general-purpose worker, one not tied to any library.

        Today returns the first registered worker for the key. Future versions can
        load-balance across multiple workers for the same key.
        """
        for wid, registration in self._workers.items():
            if registration.worker_key == key:
                return wid, registration.request_topic
        return None

    async def spawn_worker(
        self,
        args: list[str],
        worker_key: str | None,
        *,
        library_name: str | None = None,
        worker_engine_id: str | None = None,
        environ_overrides: dict[str, str] | None = None,
    ) -> None:
        """Spawn a worker subprocess using the given command args.

        worker_key is an opaque identifier used to track the process and prevent
        duplicate spawns, None for the general-purpose worker. Callers are responsible
        for constructing the args list.
        library_name is the library whose execution environment the worker imports from, when the
        key is not that name. worker_engine_id is the id the worker takes, when the caller needs it
        before the worker starts. environ_overrides are set in the worker's environment last.
        """
        if worker_key in self._managed_worker_processes or worker_key in self._spawns_in_flight:
            logger.error("Worker for key '%s' already spawned; refusing duplicate spawn.", worker_key)
            return
        # Claimed in the same step as the check above, so no await separates them. The registry
        # entry cannot serve as this guard: it is written only once the subprocess exists, and the
        # work in between suspends. A second fork for one library leaves one of the two processes
        # untracked, holding that library's dependencies until its own heartbeat lapses.
        claim = asyncio.Event()
        self._spawns_in_flight[worker_key] = claim
        try:
            # Spawn with the orchestrator's PRE-project environ so the worker boots with the
            # same clean env baseline a fresh engine would have. Inheriting the live os.environ
            # would bake the orchestrator's current-project env vars into the worker's restore
            # baseline, leaving the worker unable to unset them on a later project switch.
            #
            # On top of that baseline the engine sets only the variables named below, one by one.
            # Nothing is forwarded by pattern: a variable an environment tool set while preparing
            # the orchestrator reaches the worker as part of the baseline every child process
            # inherits, unchanged, and a configured worker.command_prefix -- which owns preparing
            # the worker's environment -- decides what to do with it.
            base_environ = self.engine.project_manager.get_pre_project_environ()
            worker_environ = {**base_environ, "GTN_ENGINE_ID": worker_engine_id or str(uuid.uuid4())}
            # Stamp the spawning orchestrator's id so the worker can report it in its discovery
            # heartbeat (orchestrator_engine_id), letting clients identify and nest worker engines.
            # The orchestrator always has an id by the time it spawns a worker; guard the None
            # case anyway so a subprocess env value is never None.
            orchestrator_engine_id = self.engine.engine_identity_manager.active_engine_id
            if orchestrator_engine_id is not None:
                worker_environ["GTN_ORCHESTRATOR_ENGINE_ID"] = orchestrator_engine_id
            # Worker stdout is a pipe when the orchestrator is hosted by a GUI app (e.g. the
            # desktop app); unbuffered output keeps worker log lines from stalling in Python's
            # block buffer and from being lost on a crash.
            worker_environ["PYTHONUNBUFFERED"] = "1"

            # PYTHONPATH precedes site-packages, making this library-first with the engine's own
            # environment as the fallback. It must be the environment rather than a later sys.path
            # splice: sys.modules never reconsiders a module this process has already imported.
            # The general-purpose worker serves no single library, so it gets no library's environment
            # and imports from the engine's own, as the orchestrator does.
            execution_site_packages = None
            environment_library = library_name or worker_key
            if environment_library is not None:
                execution_site_packages = self.engine.library_manager.environment.execution_site_packages(
                    environment_library
                )
            if execution_site_packages is not None:
                # Prepended, not assigned: a launcher-set PYTHONPATH (embedding hosts, source checkouts)
                # is part of the environment the engine itself booted with, and dropping it only in
                # exec-deps workers would lose those modules in exactly one process kind.
                inherited_pythonpath = worker_environ.get("PYTHONPATH")
                worker_environ["PYTHONPATH"] = (
                    execution_site_packages + os.pathsep + inherited_pythonpath
                    if inherited_pythonpath
                    else execution_site_packages
                )
                logger.debug(
                    "Worker for library '%s' will resolve imports from %s first",
                    worker_key,
                    execution_site_packages,
                )

            # No workspace variable here: GTN_CONFIG_ outranks the runtime project override, so a worker
            # handed one could never follow its orchestrator onto a project's workspace again. The
            # workspace arrives with the project the orchestrator activates on it.

            # Both processes share the workspace on disk, so the orchestrator's long-lived server
            # is the one that must serve it: a worker serving its own wins an arbitrary port, and
            # every asset URL on it is dead by the time a saved workflow is reopened.
            static_base_url = await self._orchestrator_static_server_base_url()
            if static_base_url is not None:
                worker_environ[ORCHESTRATOR_STATIC_SERVER_BASE_URL_ENV] = static_base_url
            worker_environ.update(environ_overrides or {})
            # Hand the orchestrator's own stdout/stderr to the worker explicitly so worker log
            # lines land in the same stream as orchestrator logs. Implicit inheritance is
            # POSIX-only: on Windows, redirected std handles (e.g. the desktop app's pipes) are
            # not passed to a child unless subprocess sends them via STARTF_USESTDHANDLES, so
            # the worker would log to an invisible console instead.
            proc = await asyncio.create_subprocess_exec(
                *args,
                env=worker_environ,
                stdout=sys.stdout,
                stderr=sys.stderr,
            )
            # Record the loop that owns this subprocess so termination can hop back to it.
            # All spawns run on the engine event-queue loop, so this is idempotent.
            self._spawn_loop = asyncio.get_running_loop()
            self._managed_worker_processes[worker_key] = proc
            # Started on this loop, the only one that may await proc.wait().
            watcher = self._spawn_loop.create_task(self._settle_when_worker_exits_early(worker_key, proc))
            self._exit_watchers.add(watcher)
            watcher.add_done_callback(self._exit_watchers.discard)
        finally:
            # Released even when the fork raises, or the claim would silently refuse every later
            # attempt for this library -- but only while this attempt still holds it. A reset drops
            # the claims so a reload can spawn again, so a spawn suspended across one resumes to
            # find the key belonging to the reload's spawn, and freeing that admits a third fork.
            if self._spawns_in_flight.get(worker_key) is claim:
                del self._spawns_in_flight[worker_key]
            claim.set()
        logger.debug("Spawned worker for key '%s' (pid %s)", worker_key, proc.pid)

    async def _settle_when_worker_exits_early(self, worker_key: str | None, proc: asyncio.subprocess.Process) -> None:
        """Release the runs waiting on `worker_key` if its process exits before it has loaded.

        Every other way a worker ends is noticed through the registry: it unregisters, or it stops
        answering heartbeats and is evicted. A worker that dies while starting up -- refused a
        license session, a crash on import -- never registered, so neither happens, and the runs
        waiting on it would wait out the whole load timeout before blaming a load that never began.
        The process exiting is the fact itself, so it is read from the process.
        """
        returncode = await proc.wait()
        # A process this manager stopped itself was dropped from the records first, and whoever
        # stopped it records the reason.
        if self._managed_worker_processes.get(worker_key) is not proc:
            return
        # Loaded, then exited: the heartbeat loop evicts it and says so.
        if self.has_settled(worker_key):
            return
        del self._managed_worker_processes[worker_key]
        logger.error(
            "Worker for key '%s' (pid %s) exited with code %s before it finished loading. Its own log "
            "lines above say why.",
            worker_key,
            proc.pid,
            returncode,
        )
        self.note_worker_unavailable(
            worker_key,
            f"the worker process that runs it stopped while starting up (exit code {returncode}). "
            "The engine log has the details.",
        )

    async def reset_workers(self) -> None:
        """Terminate all managed worker processes, unsubscribe response topics, clear state.

        Used both on orchestrator shutdown and before a library reload: freshly
        spawned workers must start with a clean slate and no stale entries in the
        routing tables or lingering subscriptions on the broker. Best-effort:
        already-exited processes and unsubscribe failures are logged and skipped.
        """
        logger.debug(
            "reset_workers called: %d managed process(es) tracked (%s)",
            len(self._managed_worker_processes),
            list(self._managed_worker_processes.keys()),
        )
        await self._stop_workers(library_names=None, action="reload")
        # Cleared with the registry, not left behind: a spawn still inside its awaits holds this
        # library's claim, and a claim surviving the reset refuses the reload's own spawn for it.
        self._spawns_in_flight.clear()

    async def stop_worker_for_library(self, library_name: str) -> None:
        """Terminate the worker serving one library and forget it, leaving every other worker running.

        A spawn already under way is waited out first and its process stopped with the rest, so once
        this returns no worker for the library is running or about to start.
        """
        claim = self._spawns_in_flight.get(library_name)
        if claim is not None:
            await claim.wait()
        await self._stop_workers(library_names={library_name}, action="reset")
        self.note_worker_unavailable(library_name, "its worker process was stopped while the library is being reset.")

    async def stop_general_purpose_worker(self, reason: str) -> None:
        """Terminate the general-purpose worker and forget it, leaving every library worker running.

        A spawn already under way is waited out first and its process stopped with the rest, so once
        this returns no general-purpose worker is running or about to start. Anything waiting on it is
        released with the reason.
        """
        claim = self._spawns_in_flight.get(None)
        if claim is not None:
            await claim.wait()
        await self._stop_workers(library_names={None}, action="restart")
        self.note_worker_unavailable(None, reason)

    async def _stop_workers(self, *, library_names: set[str | None] | None, action: str) -> None:
        """Terminate the named libraries' workers (every worker when None) and forget them.

        Anything awaiting one of them is failed with the reason, and its response topic is
        unsubscribed.
        """
        self._withdraw_spawn_requests(library_names=library_names, action=action)
        processes = [
            (library_name, proc)
            for library_name, proc in self._managed_worker_processes.items()
            if self._stops_with(library_name, library_names)
        ]
        # Dropped from the records before they are stopped, so the exit this causes reads as this
        # manager's doing and not as a worker dying on its own.
        for library_name, _proc in processes:
            self._managed_worker_processes.pop(library_name, None)
        await asyncio.gather(*(self._terminate_via_spawn_loop(library_name, proc) for library_name, proc in processes))
        # Read after the termination, not before: a worker can register while its process is being
        # stopped, and an id missed here stays routable to a dead process.
        worker_ids = [
            wid
            for wid, registration in self._workers.items()
            if library_names is None or registration.worker_key in library_names
        ]
        # Settle anything still awaiting one of these workers, BEFORE the registry is cleared.
        # Clearing it is what makes this the last chance: the heartbeat loop can only evict ids it
        # can still see, so after this nothing reaches these requests at all. route_to_worker has no
        # wall-clock ceiling, so a node dispatched into a worker this call terminates would await a
        # future that never settles.
        if self._transport is not None:
            for wid in worker_ids:
                registration = self._workers[wid]
                await self._tx.request_client.fail_requests_by_tag(
                    wid,
                    worker_events.WorkerGoneError(
                        f"worker '{wid}' was shut down to {action} library '{registration.worker_key}'"
                        if registration.worker_key
                        else f"worker '{wid}' was shut down to {action} libraries"
                    ),
                )
        session_id = self.engine.get_session_id()
        if session_id and self._transport is not None:
            for wid in worker_ids:
                response_topic = f"sessions/{session_id}/workers/{wid}/response"
                try:
                    await self._tx.unsubscribe_from_topic(response_topic)
                except Exception as e:
                    logger.debug("Failed to unsubscribe from '%s' while stopping workers: %s", response_topic, e)
        self._forget_remote_starts(library_names=library_names)
        if library_names is None:
            self._managed_worker_processes.clear()
            self._workers.clear()
            self._withdraw_session_offers()
            return
        for wid in worker_ids:
            self._workers.pop(wid, None)

    def _stops_with(self, worker_key: str | None, library_names: set[str | None] | None) -> bool:
        """Whether stopping `library_names` (every library when None) stops the worker process under `worker_key`.

        A worker this engine runs for another engine's session is keyed by that session as well, and
        stops with its library, since resetting a library replaces the environment it runs in.
        """
        if library_names is None or worker_key in library_names:
            return True
        hosted_library = self._hosted_library_of(worker_key)
        return hosted_library is not None and hosted_library in library_names

    def _hosted_library_of(self, worker_key: str | None) -> str | None:
        """The library of a worker this engine runs for another engine, or None for any other worker."""
        for hosted in self._hosted_workers.values():
            if hosted.worker_key == worker_key:
                return hosted.worker_key.removeprefix(f"{hosted.session_id}/")
        return None

    def _forget_remote_starts(self, *, library_names: set[str | None] | None) -> None:
        """Forget the named libraries' starts on other engines (every library's when None)."""
        for wid, start in list(self._remote_starts.items()):
            if library_names is None or start.library_name in library_names:
                del self._remote_starts[wid]

    def _withdraw_spawn_requests(self, *, library_names: set[str | None] | None, action: str) -> None:
        """Forget the named libraries' spawn requests (every library's when None), so a later one is honored.

        A spawn that has not begun is cancelled, or it would start a worker after the stop, and a run
        waiting on it is told why. One already spawning is left to finish: cancelling it mid-way could
        leave its process running untracked.
        """
        for library_name, task in list(self._requested_spawns.items()):
            if library_names is not None and library_name not in library_names:
                continue
            del self._requested_spawns[library_name]
            if library_name in self._spawns_in_flight or task.done():
                continue
            task.cancel()
            self.note_worker_unavailable(library_name, f"its worker was stopped to {action} it before it started.")

    async def route_to_worker(
        self,
        event_request: EventRequest,
        worker_engine_id: str,
        worker_request_topic: str,
    ) -> dict:
        """Forward event_request to the named worker and await the raw result payload.

        Registers a Future via RequestClient keyed by request_id and resolves it when
        the worker response arrives. The caller is responsible for deserializing the
        returned dict into the appropriate result type.
        """
        request_id = event_request.request_id or str(uuid.uuid4())
        # Opt into structured-failure delivery so a worker-side
        # ResultPayloadFailure arrives as the raw payload dict rather
        # than being collapsed to a bare ``Exception(error_msg)`` by
        # ``_try_match``. ``_execute_node_via_worker`` then runs the
        # dict through ``converter.structure(...)``, which rebuilds
        # ``self.exception`` into a ForwardedException carrying the
        # worker-side type name and traceback string.
        future = await self._tx.request_client.track_request(
            request_id, tag=worker_engine_id, resolve_failures_as_payload=True
        )

        # Both awaits belong inside the `try`: either can unwind and leave the entry tracked with
        # nobody to settle it. No wall-clock timeout on the response, because long-running AI
        # workloads exceed any sensible default and the heartbeat loop already fails a silent
        # worker's requests with WorkerGoneError. That is also why a CancelledError here means only
        # that the caller was cancelled. wrap_future adapts a future the transport loop settles.
        try:
            await self.forward_event_to_worker(
                event_request.model_copy(update={"request_id": request_id}),
                worker_engine_id=worker_engine_id,
                worker_request_topic=worker_request_topic,
            )
            return await asyncio.wrap_future(future)
        except BaseException:
            # BaseException, not Exception: cancellation is the common case and it is not an
            # Exception. The other ways out remove the request themselves -- a response pops it in
            # _try_match, eviction in fail_requests_by_tag -- so this is the one exit that has to
            # clean up after itself. Without it the entry outlives the run, one per cancelled node
            # execution, each still carrying its worker's tag for fail_requests_by_tag to walk.
            self._tx.request_client.discard_request(request_id)
            raise

    async def admit_on_engine(self, library_name: str, node_name: str) -> EngineAdmission | None:
        """Wait until the engine that runs `library_name`'s worker admits the node, or None if it runs here.

        Unbounded, like the node itself: another run can hold that engine for as long as it runs. That
        engine may stop this engine's worker there to free itself for that run, so pick the worker only
        after this returns, through `wait_until_executable`, which starts it again. Pass the admission
        to `end_admission` once the node ends, however it ends.

        Raises:
            RuntimeError: That engine will not run the node, with why.
        """
        host_engine_id = self._engine_for_library(library_name)
        if host_engine_id is None:
            return None
        admission = EngineAdmission(
            admission_id=str(uuid.uuid4()), host_engine_id=host_engine_id, run_id=current_run_id()
        )
        self._admissions[admission.admission_id] = admission
        if admission.run_id is not None:
            self._run_engines.setdefault(admission.run_id, set()).add(host_engine_id)
        request = worker_events.AdmitNodeRequest(
            admission_id=admission.admission_id, run_id=admission.run_id, node_name=node_name
        )
        admission.request_task = asyncio.ensure_future(self.engine.request(host_engine_id, request, timeout_s=None))
        # However the admission is answered, the run no longer waits, even if that engine's last report was lost.
        admission.request_task.add_done_callback(lambda _task: self._stop_reporting_wait(admission))
        try:
            result = await admission.request_task
        except EngineNotFoundError as e:
            await self.end_admission(admission)
            msg = f"Attempted to run node '{node_name}' on engine {host_engine_id}. Failed because {e}"
            raise RuntimeError(msg) from e
        except asyncio.CancelledError:
            if admission.given_up is None:
                await self.end_admission(admission)
                raise
            # That engine is lost, so it is not told the node ended.
            self._admissions.pop(admission.admission_id, None)
            msg = f"Attempted to run node '{node_name}' on engine {host_engine_id}. Failed because {admission.given_up}"
            raise RuntimeError(msg) from None
        except BaseException:
            await self.end_admission(admission)
            raise
        if not result.succeeded():
            await self.end_admission(admission)
            msg = f"Attempted to run node '{node_name}' on engine {host_engine_id}. Failed because {result.result_details}"
            raise RuntimeError(msg)
        return admission

    def _stop_reporting_wait(self, admission: EngineAdmission) -> None:
        if admission.run_id is not None:
            self.engine.flow_manager.report_run_waiting_for_engine(admission.run_id, 0)

    async def end_admission(self, admission: EngineAdmission | None) -> None:
        """Tell the engine asked to admit a node that the node ended, so its run can let that engine go."""
        if admission is None or self._admissions.pop(admission.admission_id, None) is None:
            return
        request = worker_events.NodeExecutionEndedRequest(admission_id=admission.admission_id)
        try:
            result = await self.engine.request(admission.host_engine_id, request, timeout_s=_ENGINE_REQUEST_TIMEOUT_S)
        except EngineNotFoundError as e:
            logger.warning("Could not tell engine %s that a node it admitted ended: %s", admission.host_engine_id, e)
            return
        if not isinstance(result, worker_events.NodeExecutionEndedResultSuccess):
            logger.warning(
                "Engine %s did not record that a node it admitted ended: %s",
                admission.host_engine_id,
                result.result_details,
            )

    async def _orchestrator_static_server_base_url(self) -> str | None:
        """The base URL the workspace is served on, awaited until initialization decides it.

        On a restart into an existing session, spawning and URL resolution are both reactions to
        AppInitializationComplete, whose listeners fan out as unordered concurrent tasks -- so this
        waits for the decision rather than sampling mid-fan-out. Returns None under cloud storage,
        in which case the worker's URLs come from the same bucket as the orchestrator's and outlive
        it regardless.
        """
        static_files_manager = self.engine.static_files_manager
        # Normally the decision is already in, so read it without an executor hop. The hop below is
        # the one with a cost: a blocking wait handed to a thread cannot be cancelled, so if nothing
        # ever settles it parks a default-executor thread that shutdown_default_executor() joins at
        # teardown. Unreachable in any ordering found so far -- the resolving listener is synchronous
        # and finishes at the library listener's first await -- so this path is what runs.
        if static_files_manager.static_server_base_url_settled:
            base_url = static_files_manager.wait_for_static_server_base_url(0)
        else:
            base_url = await asyncio.to_thread(
                static_files_manager.wait_for_static_server_base_url, _STATIC_URL_SETTLE_TIMEOUT_S
            )
        if base_url is not None:
            return base_url

        # Only for local storage: there, no URL means the worker serves assets on its own ephemeral
        # port and every URL it produces dies with it -- the dead-links bug this handover exists to
        # prevent. Loud, because it is invisible otherwise. On a cloud backend a worker's URLs come
        # from the same bucket as the orchestrator's and outlive it, so there is nothing to say.
        if isinstance(static_files_manager.storage_driver, LocalStorageDriver):
            # Two different failures, and pointing an operator at the wrong one costs real time:
            # initialization can DECIDE there is no server, which settles in microseconds and has
            # nothing to do with the timeout. That means a resolution that raised, since every branch
            # that returns normally under local storage sets a URL.
            if static_files_manager.static_server_base_url_settled:
                logger.warning(
                    "Initialization resolved no static server, so a spawned worker will serve assets "
                    "on its own port. URLs it produces will stop working when it exits. Check for an "
                    "earlier failure resolving the static server."
                )
            else:
                logger.warning(
                    "Worker startup waited %.0fs for the static server URL and it was never decided, "
                    "so the worker is being spawned without one and will serve assets on its own "
                    "port. URLs it produces will stop working when it exits.",
                    _STATIC_URL_SETTLE_TIMEOUT_S,
                )
        return None

    async def evict_worker(self, worker_engine_id: str) -> None:
        """Remove a worker from the registry and unsubscribe from its response topic."""
        session_id = self.engine.get_session_id()
        registration = self._workers.pop(worker_engine_id, None)
        lib_name = registration.worker_key if registration else None
        topic = f"sessions/{session_id}/workers/{worker_engine_id}/response"
        await self._tx.unsubscribe_from_topic(topic)
        # A hung worker cannot shut itself down, so the engine that runs it is told to stop it.
        if registration is not None and registration.host_engine_id is not None:
            self._schedule_remote_stop(worker_engine_id, registration.host_engine_id)
        logger.warning("Worker evicted: %s", worker_engine_id)
        # Terminate the managed subprocess for this worker, if any.
        if registration is not None:
            proc = self._managed_worker_processes.pop(lib_name, None)
            if proc is not None:
                await self._terminate_via_spawn_loop(lib_name, proc)
        # Fail anything awaiting this worker, with the reason, so the awaiter reports why rather
        # than inferring it from a bare cancellation.
        await self._tx.request_client.fail_requests_by_tag(
            worker_engine_id,
            worker_events.WorkerGoneError(f"worker '{worker_engine_id}' stopped responding and was shut down"),
        )
        # Eviction is terminal -- nothing respawns the worker -- so anything still waiting on this
        # library would wait forever, and the next run would report a worker that "may still be
        # starting up" for the rest of the session.
        if registration is not None:
            self.note_worker_unavailable(
                lib_name, "the worker process that runs it stopped responding and was shut down."
            )

    async def _terminate_via_spawn_loop(self, library_name: str | None, proc: asyncio.subprocess.Process) -> None:
        """Terminate a managed worker on the loop that owns its subprocess.

        asyncio.subprocess.Process binds its exit Future to the loop that created
        it (the engine event-queue loop), so proc.wait() is only legal there.
        Eviction can run on a different loop (the websocket-tasks loop); awaiting
        proc.wait() from there raises "got Future attached to a different loop".
        Hop the termination coroutine back onto the spawning loop via
        run_coroutine_threadsafe so proc.wait() always touches its own loop.

        During shutdown the spawning loop may be cancelling or closed. If the hop
        cannot complete, fall back to a loop-agnostic signal (terminate/kill are
        plain os.kill, safe from any loop) without awaiting the exit Future.
        """
        spawn_loop = self._spawn_loop
        running_loop = asyncio.get_running_loop()
        # No separate spawn loop (tests / single-loop deploys) or already on it:
        # proc.wait() is legal here, so run termination inline.
        if spawn_loop is None or spawn_loop is running_loop:
            await self._terminate_managed_process(library_name, proc)
            return
        coro = self._terminate_managed_process(library_name, proc)
        try:
            future = asyncio.run_coroutine_threadsafe(coro, spawn_loop)
        except RuntimeError as e:
            # The spawning loop is closed (shutdown). The coroutine was never
            # scheduled, so close it to avoid a never-awaited warning, then signal
            # the worker directly without awaiting its exit Future.
            coro.close()
            logger.warning(
                "Spawning loop unavailable to terminate worker for key '%s' (%s); "
                "sending a synchronous signal without awaiting exit confirmation",
                library_name,
                e,
            )
            self._terminate_without_wait(library_name, proc)
            return
        try:
            await asyncio.wait_for(asyncio.wrap_future(future), timeout=WorkerManager.DEFAULT_TERMINATE_HOP_TIMEOUT_S)
        except asyncio.CancelledError:
            # Termination was cancelled during shutdown; ensure the worker still
            # gets a kill signal that needs no await on the spawning loop, then
            # re-raise so cooperative cancellation propagates. Both hop callers run
            # under TaskGroup-driven teardown (orchestrator_heartbeat_loop and the
            # reset_workers gather); swallowing the cancel would let cleanup resume
            # in a context that was supposed to stop and wedge TaskGroup convergence.
            logger.warning(
                "Termination of worker for key '%s' was cancelled; sending a "
                "synchronous signal without awaiting exit confirmation",
                library_name,
            )
            self._terminate_without_wait(library_name, proc)
            raise
        except TimeoutError:
            # The hop was scheduled but never completed: the spawning loop most
            # likely closed mid-shutdown before draining it. Stop waiting on a
            # future that will never resolve and signal the worker directly.
            future.cancel()
            logger.warning(
                "Termination of worker for key '%s' did not complete on the spawning loop "
                "within %.0fs; sending a synchronous signal without awaiting exit confirmation",
                library_name,
                WorkerManager.DEFAULT_TERMINATE_HOP_TIMEOUT_S,
            )
            self._terminate_without_wait(library_name, proc)

    async def _terminate_managed_process(self, library_name: str | None, proc: asyncio.subprocess.Process) -> None:
        """Terminate a managed worker, escalating to SIGKILL if it does not exit.

        SIGTERM is converted by the worker into a cooperative shutdown on its
        event loop; a wedged loop never services it. After DEFAULT_TERMINATE_GRACE_S
        we send SIGKILL, which the kernel delivers regardless of loop state, so a
        hung worker can never leak.

        Must run on the loop that spawned proc, since it awaits proc.wait(). Callers
        on another loop route through _terminate_via_spawn_loop.
        """
        try:
            proc.terminate()
        except ProcessLookupError:
            logger.debug("Worker for key '%s' already exited before termination", library_name)
            return
        logger.debug("Terminated worker for key '%s' (pid %s)", library_name, proc.pid)
        try:
            await asyncio.wait_for(proc.wait(), timeout=WorkerManager.DEFAULT_TERMINATE_GRACE_S)
        except TimeoutError:
            logger.warning(
                "Worker for key '%s' (pid %s) did not exit within %.0fs of SIGTERM; sending SIGKILL",
                library_name,
                proc.pid,
                WorkerManager.DEFAULT_TERMINATE_GRACE_S,
            )
            try:
                proc.kill()
            except ProcessLookupError:
                return
            await proc.wait()

    def _terminate_without_wait(self, library_name: str | None, proc: asyncio.subprocess.Process) -> None:
        """Signal a worker to die without awaiting its exit Future.

        Shutdown-only fallback for when the spawning loop is unavailable to run
        proc.wait(). terminate() then kill() are synchronous os.kill calls, safe
        from any loop; we forgo the graceful grace period and exit confirmation
        because the orchestrator is tearing down and only needs the worker gone.
        """
        try:
            proc.terminate()
        except ProcessLookupError:
            logger.debug("Worker for key '%s' already exited before termination", library_name)
            return
        try:
            proc.kill()
        except ProcessLookupError:
            return

    def set_session_ready(self) -> None:
        """Signal that a session is available, unblocking any pending worker spawns."""
        self._session_ready_event.set()

    def clear_session_ready(self) -> None:
        """Clear the session-ready gate so future worker spawns wait for a new session."""
        self._session_ready_event.clear()

    @handles(worker_events.StartWorkerRequest)
    async def handle_start_worker_request(
        self, request: worker_events.StartWorkerRequest
    ) -> worker_events.StartWorkerResultSuccess | worker_events.StartWorkerResultFailure:
        """Schedule a worker for the given library, unless one is already coming.

        Returns immediately; the worker is started once a session becomes available, by this engine
        or by the one the distribution settings assign the library to.
        """
        library_name = request.library_name
        # Checked, declared, and scheduled with no await between, so two requests cannot both pass.
        # Declaring again would be worse than redundant: expect_worker replaces the readiness gate, and
        # a run already waiting on the old one would wait out the whole load timeout.
        if self.is_worker_running_or_starting(library_name):
            logger.debug("Not starting another worker for library '%s': one is starting or running.", library_name)
            return worker_events.StartWorkerResultSuccess(
                result_details=f"A worker for library '{library_name}' is already starting or running."
            )
        self.expect_worker(library_name)
        if library_name is None:
            start = self._spawn_general_purpose_worker_when_session_ready()
        else:
            host_engine_id = self._engine_for_library(library_name)
            if host_engine_id is None:
                start = self._spawn_when_session_ready(library_name)
            else:
                start = self._start_worker_on_engine(library_name, host_engine_id)
        task = asyncio.get_running_loop().create_task(start)
        self._requested_spawns[library_name] = task
        task.add_done_callback(functools.partial(self._release_spawn_request, library_name=library_name))
        task.add_done_callback(functools.partial(self._log_spawn_error, library_name=library_name))
        return worker_events.StartWorkerResultSuccess(result_details="Worker spawn scheduled.")

    @handles(
        worker_events.StartWorkerForEngineRequest,
        sender=RequiredSender.SESSION,
        owner=lambda manager, _request, sender: manager._engine_that_offered(sender),
    )
    async def handle_start_worker_for_engine_request(
        self, request: worker_events.StartWorkerForEngineRequest
    ) -> worker_events.StartWorkerForEngineResultSuccess | worker_events.StartWorkerForEngineResultFailure:
        """Start a worker that serves the session of the engine that asked, under the ID it chose."""
        sender = require_request_sender()
        # EventManager refuses this request from an engine that names no session.
        session_id = cast("str", sender.session_id)
        return await self._start_worker_for_session(
            request.library_name, session_id, sender.engine_id, request.worker_engine_id
        )

    def _release_spawn_request(self, task: asyncio.Task, library_name: str | None) -> None:
        """Forget a finished spawn request, unless stopping the library's workers already withdrew it."""
        if self._requested_spawns.get(library_name) is task:
            del self._requested_spawns[library_name]

    @handles(ReportGeneralPurposeWorkerReadyRequest)
    def handle_report_general_purpose_worker_ready_request(
        self,
        request: ReportGeneralPurposeWorkerReadyRequest,  # noqa: ARG002
    ) -> ReportGeneralPurposeWorkerReadyResultSuccess:
        """Release anything waiting to route to the general-purpose worker, now that it has loaded."""
        self.note_library_loaded(None)
        return ReportGeneralPurposeWorkerReadyResultSuccess(result_details="General-purpose worker is ready.")

    async def _spawn_when_session_ready(self, library_name: str) -> None:
        """Wait for an active session then spawn a worker subprocess for the given library."""
        session_id = await self._wait_for_session(library_name)
        if session_id is None:
            return
        # The worker is handed its library's execution environment as PYTHONPATH, so that directory
        # has to exist before the process starts. It does: the orchestrator builds it while
        # registering the library, and LibraryManager asks for no worker until that registration has
        # finished, nor for one whose build failed.
        resolved = self._worker_command(library_name, session_id)
        if isinstance(resolved, WorkerCommandRefusal):
            logger.error("Not starting a worker for library '%s': %s", library_name, resolved.reason)
            self.note_worker_unavailable(library_name, resolved.reason)
            return
        await self.spawn_worker(resolved.args, library_name)

    async def _start_worker_on_engine(self, library_name: str, host_engine_id: str) -> None:
        """Wait for an active session, then ask the engine `host_engine_id` to start the library's worker.

        The worker joins this engine's session, so it registers here exactly as a local worker does;
        the app carries its traffic. Any refusal on the way is recorded as the reason the library
        cannot run, so a run fails with it at once.
        """
        session_id = await self._wait_for_session(library_name)
        if session_id is None:
            return
        if not self.engine.can_reach_other_engines:
            reason = (
                f"its nodes are set to run on {host_engine_id}, but this engine has no connections to other engines."
            )
            logger.error("Not starting a worker for library '%s': %s", library_name, reason)
            self.note_worker_unavailable(library_name, reason)
            return
        result = await self._ask_engine_to_start_worker(library_name, host_engine_id, session_id)
        if isinstance(result, worker_events.StartWorkerForEngineResultFailure) and result.session_unknown:
            # That engine restarted since this engine registered with it, so register again, once.
            result = await self._ask_engine_to_start_worker(library_name, host_engine_id, session_id)
        if result is None or isinstance(result, worker_events.StartWorkerForEngineResultSuccess):
            return
        reason = f"{host_engine_id} did not start its worker: {result.result_details}"
        logger.error("Not starting a worker for library '%s': %s", library_name, reason)
        self.note_worker_unavailable(library_name, reason)
        if isinstance(result, worker_events.StartWorkerForEngineResultFailure) and result.at_capacity:
            # The node waiting now fails with the reason; the library's next node asks again.
            self._refused_for_capacity.add(library_name)

    async def _ask_engine_to_start_worker(
        self, library_name: str, host_engine_id: str, session_id: str
    ) -> ResultPayload | None:
        """Register with `host_engine_id` if needed, then ask it to start the library's worker under an ID chosen here.

        Returns that engine's result, or None if it refused this engine or the library, could not be
        reached, or did not answer, which is then recorded as the reason the library cannot run. A
        start that did not succeed is forgotten; one that did is kept until its worker registers.
        """
        refusal = await self._register_with_engine(host_engine_id, session_id, library_name)
        if refusal is not None:
            logger.error("Not starting a worker for library '%s' on %s: %s", library_name, host_engine_id, refusal)
            self.note_worker_unavailable(library_name, refusal)
            return None
        worker_engine_id = str(uuid.uuid4())
        self._remote_starts[worker_engine_id] = _RemoteStart(host_engine_id=host_engine_id, library_name=library_name)
        logger.info("Asking %s to start worker %s for library '%s'.", host_engine_id, worker_engine_id, library_name)
        request = worker_events.StartWorkerForEngineRequest(
            library_name=library_name, worker_engine_id=worker_engine_id
        )
        try:
            result = await self.engine.request(host_engine_id, request, timeout_s=_ENGINE_REQUEST_TIMEOUT_S)
        except EngineNotFoundError as e:
            # That engine may still get the start, such as once a connection that was down comes back.
            self._schedule_remote_stop(worker_engine_id, host_engine_id)
            logger.error("Not starting a worker for library '%s': %s", library_name, e)
            self.note_worker_unavailable(library_name, str(e))
            return None
        except BaseException:
            # That engine may have started the worker before the start was withdrawn or failed here.
            self._schedule_remote_stop(worker_engine_id, host_engine_id)
            raise
        if not isinstance(result, worker_events.StartWorkerForEngineResultSuccess):
            self._remote_starts.pop(worker_engine_id, None)
        return result

    def _schedule_remote_stop(self, worker_engine_id: str, host_engine_id: str) -> None:
        """Forget a start withdrawn while `host_engine_id` was answering it, and tell that engine to stop that worker."""
        self._remote_starts.pop(worker_engine_id, None)
        task = asyncio.get_running_loop().create_task(self._stop_remote_worker(worker_engine_id, host_engine_id))
        self._stop_requests.add(task)
        task.add_done_callback(self._stop_requests.discard)

    async def _stop_remote_worker(self, worker_engine_id: str, host_engine_id: str) -> None:
        request = worker_events.StopWorkerForEngineRequest(worker_engine_id=worker_engine_id)
        try:
            result = await self.engine.request(host_engine_id, request, timeout_s=_ENGINE_REQUEST_TIMEOUT_S)
        except EngineNotFoundError as e:
            logger.warning(
                "Could not tell %s to stop worker %s, whose start was withdrawn: %s",
                host_engine_id,
                worker_engine_id,
                e,
            )
            return
        if not isinstance(result, worker_events.StopWorkerForEngineResultSuccess):
            logger.warning(
                "%s did not stop worker %s, whose start was withdrawn: %s",
                host_engine_id,
                worker_engine_id,
                result.result_details,
            )

    def _withdraw_session_offers(self) -> None:
        """Tell each engine this one offered its session to that it is done with them, and forget the offers.

        Sent without waiting, best effort: an engine that cannot be reached forgets the offer anyway
        once its connection to this one is lost.
        """
        host_engine_ids = {host_engine_id for host_engine_id, _session_id in self._engine_registrations}
        self._engine_registrations.clear()
        for host_engine_id in host_engine_ids:
            task = asyncio.get_running_loop().create_task(self._withdraw_session_offer(host_engine_id))
            self._stop_requests.add(task)
            task.add_done_callback(self._stop_requests.discard)

    async def _withdraw_session_offer(self, host_engine_id: str) -> None:
        try:
            result = await self.engine.request(
                host_engine_id, worker_events.WithdrawSessionOfferRequest(), timeout_s=_ENGINE_REQUEST_TIMEOUT_S
            )
        except EngineNotFoundError as e:
            logger.warning("Could not tell %s that this engine is done with its session offer: %s", host_engine_id, e)
            return
        if not isinstance(result, worker_events.WithdrawSessionOfferResultSuccess):
            logger.warning("%s did not forget this engine's session offer: %s", host_engine_id, result.result_details)

    async def _register_with_engine(self, host_engine_id: str, session_id: str, library_name: str) -> str | None:
        """Register this engine's session with `host_engine_id`, and return why it refuses `library_name`, if it does.

        Every start registers anew, so that engine checks the libraries as they are now, after either
        engine changed one. Starts that overlap share the registration still in progress. An accepted
        one is kept, to tell the engines it was offered to that this engine is done with them.
        """
        key = (host_engine_id, session_id)
        registration = self._engine_registrations.get(key)
        if registration is None or registration.done():
            registration = asyncio.get_running_loop().create_task(self._send_registration(host_engine_id))
            registration.add_done_callback(lambda task: self._forget_unaccepted_registration(key, task))
            self._engine_registrations[key] = registration
        try:
            result = await asyncio.shield(registration)
        except EngineNotFoundError as e:
            return str(e)
        if not isinstance(result, worker_events.OfferSessionForWorkersResultSuccess):
            return f"{host_engine_id} refused to run work for this engine: {result.result_details}"
        if library_name in result.refused_libraries:
            return f"{host_engine_id} cannot run it: {result.refused_libraries[library_name]}"
        return None

    def _forget_unaccepted_registration(self, key: tuple[str, str], task: asyncio.Task[ResultPayload]) -> None:
        accepted = (
            not task.cancelled()
            and task.exception() is None
            and isinstance(task.result(), worker_events.OfferSessionForWorkersResultSuccess)
        )
        if not accepted and self._engine_registrations.get(key) is task:
            del self._engine_registrations[key]

    async def _send_registration(self, host_engine_id: str) -> ResultPayload:
        """Send the registration and return that engine's answer.

        Raises:
            EngineNotFoundError: That engine cannot be reached, or the engine that answered is another one.
        """
        assigned = self.engine.config_manager.get_config_value(DISTRIBUTED_EXECUTION_LIBRARY_ENGINES_KEY, default={})
        library_versions: dict[str, str] = {}
        for library_name, assigned_engine in assigned.items():
            info = self.engine.library_manager.get_library_info_by_library_name(library_name)
            if assigned_engine == host_engine_id and info is not None and info.library_version is not None:
                library_versions[library_name] = info.library_version
        request = worker_events.OfferSessionForWorkersRequest(
            schema_version=DISTRIBUTED_EXECUTION_SCHEMA_VERSION,
            engine_version=engine_version,
            library_versions=library_versions,
            static_server_base_url=await self._orchestrator_static_server_base_url(),
        )
        result = await self.engine.request(host_engine_id, request, timeout_s=_ENGINE_REQUEST_TIMEOUT_S)
        # Catches a registry record or address that leads to the wrong engine. It cannot catch one
        # that claims to be `host_engine_id`, since the answer is that engine's own word.
        if isinstance(result, worker_events.OfferSessionForWorkersResultSuccess) and result.engine_id != host_engine_id:
            details = (
                f"Attempted to reach engine {host_engine_id}. Failed because the engine that answered is "
                f"{result.engine_id or 'one that did not say which engine it is'}."
            )
            raise EngineNotFoundError(details)
        return result

    @handles(
        worker_events.ReportWorkerExitedRequest,
        sender=RequiredSender.ENGINE,
        owner=lambda manager, request, _sender: manager.worker_host(request.worker_engine_id),
    )
    async def handle_report_worker_exited_request(
        self, request: worker_events.ReportWorkerExitedRequest
    ) -> worker_events.ReportWorkerExitedResultSuccess | worker_events.ReportWorkerExitedResultFailure:
        """Settle a worker another engine ran for this engine, now that that engine reports it exited.

        Handled like a local worker's exit: a start that never registered settles the library with the
        reason, and a registered worker is removed at once. "exited" leaves the library unavailable
        with the reason; "stopped" leaves it free to be started again.
        """
        wid = request.worker_engine_id
        start = self._remote_starts.get(wid)
        registration = self._workers.get(wid)
        host_engine_id = None
        library_name = None
        if start is not None:
            host_engine_id = start.host_engine_id
            library_name = start.library_name
        elif registration is not None and registration.host_engine_id is not None:
            host_engine_id = registration.host_engine_id
            library_name = registration.worker_key
        if host_engine_id is None:
            return worker_events.ReportWorkerExitedResultSuccess(
                result_details=f"This engine was not waiting on worker {wid}, so there was nothing to record."
            )
        self._remote_starts.pop(wid, None)
        registered = start is None
        if request.reason == "stopped":
            reason = f"{host_engine_id} stopped the worker process that runs it."
        elif not registered:
            reason = (
                f"the worker process that runs it on {host_engine_id} stopped while starting up (exit code "
                f"{request.exit_code}). That engine's log has the details."
            )
        else:
            reason = (
                f"the worker process that runs it on {host_engine_id} stopped (exit code {request.exit_code}). "
                "That engine's log has the details."
            )
        logger.warning("Worker %s for library '%s' on %s %s.", wid, library_name, host_engine_id, request.reason)
        if registered:
            del self._workers[wid]
            await self._tx.unsubscribe_from_topic(f"sessions/{self.engine.get_session_id()}/workers/{wid}/response")
            await self._tx.request_client.fail_requests_by_tag(
                wid, worker_events.WorkerGoneError(f"worker '{wid}' on {host_engine_id} {request.reason}")
            )
        # The local rules: a worker that ended on its own leaves its library unavailable, and one that
        # was stopped does so only if it never loaded, so whatever waits on it hears why.
        if request.reason == "exited" or not self.has_settled(library_name):
            self.note_worker_unavailable(library_name, reason)
        return worker_events.ReportWorkerExitedResultSuccess(result_details=f"Recorded that worker {wid} exited.")

    @handles(
        worker_events.OfferSessionForWorkersRequest,
        sender=RequiredSender.SESSION,
        owner=lambda manager, _request, sender: manager._engine_that_offered(sender),
    )
    def handle_offer_session_for_workers_request(
        self, request: worker_events.OfferSessionForWorkersRequest
    ) -> worker_events.OfferSessionForWorkersResultSuccess | worker_events.OfferSessionForWorkersResultFailure:
        """Accept the session of the engine that asked, whose libraries this engine will run, or refuse it."""
        sender = require_request_sender()
        # EventManager refuses this request from an engine that names no session.
        session_id = cast("str", sender.session_id)
        if request.schema_version != DISTRIBUTED_EXECUTION_SCHEMA_VERSION:
            return worker_events.OfferSessionForWorkersResultFailure(
                result_details=(
                    f"it speaks distributed execution version {DISTRIBUTED_EXECUTION_SCHEMA_VERSION}, and this "
                    f"engine speaks {request.schema_version}."
                )
            )
        if request.engine_version != engine_version:
            return worker_events.OfferSessionForWorkersResultFailure(
                result_details=(
                    f"it runs engine version {engine_version}, and this engine runs {request.engine_version}. "
                    "Both engines must run the same engine version."
                )
            )
        refused: dict[str, str] = {}
        for library_name, version in request.library_versions.items():
            refusal = self._library_version_refusal(library_name, version)
            if refusal is not None:
                refused[library_name] = refusal
        self._sessions[session_id] = _SessionRegistration(
            engine_id=sender.engine_id,
            static_server_base_url=request.static_server_base_url,
            library_versions=dict(request.library_versions),
        )
        return worker_events.OfferSessionForWorkersResultSuccess(
            engine_id=self.engine.get_engine_id() or "",
            refused_libraries=refused,
            result_details=f"Accepted session {session_id} of engine {sender.engine_id}.",
        )

    async def _start_worker_for_session(
        self, library_name: str, session_id: str, engine_id: str, worker_engine_id: str
    ) -> worker_events.StartWorkerForEngineResultSuccess | worker_events.StartWorkerForEngineResultFailure:
        """Start a worker for an artist's session on another engine, in that session, under the ID it chose."""
        worker_key = f"{session_id}/{library_name}"
        starting = any(
            hosted.worker_key == worker_key and not hosted.started.is_set() for hosted in self._hosted_workers.values()
        )
        if starting or worker_key in self._spawns_in_flight:
            return worker_events.StartWorkerForEngineResultFailure(
                result_details=f"A worker for library '{library_name}' is already starting for that session."
            )
        refusal = self._start_refusal(library_name, session_id, worker_key)
        if refusal is not None:
            return refusal
        resolved = self._worker_command(library_name, session_id)
        if isinstance(resolved, WorkerCommandRefusal):
            return worker_events.StartWorkerForEngineResultFailure(result_details=resolved.reason)
        # Read before anything awaits, since a lost connection can forget the session meanwhile.
        session = self._sessions[session_id]
        # The worker runs on this machine's license or API key, which it inherits like a local worker.
        environ_overrides: dict[str, str] = {}
        # Its asset URLs are opened by the artist's editor, which reaches the artist's engine's server.
        if session.static_server_base_url is not None:
            environ_overrides[ORCHESTRATOR_STATIC_SERVER_BASE_URL_ENV] = session.static_server_base_url
        # It reaches the artist's engine the way that engine reached this engine.
        try:
            environ_overrides.update(self.engine.hosted_worker_environment(engine_id))
        except EngineNotFoundError as e:
            return worker_events.StartWorkerForEngineResultFailure(result_details=str(e))
        # Recorded before anything awaits, so a stop or another start sent meanwhile sees this one.
        hosted = _HostedWorker(worker_key=worker_key, engine_id=engine_id, session_id=session_id)
        self._hosted_workers[worker_engine_id] = hosted
        try:
            # The artist's engine asks only once it has no worker for the library, so a running one is a
            # worker it already stopped using, such as after a library reset, and would never register.
            stale = self._managed_worker_processes.pop(worker_key, None)
            if stale is not None:
                logger.info("Replacing the worker for key '%s' that its session stopped using.", worker_key)
                await self._terminate_via_spawn_loop(worker_key, stale)
            await self.spawn_worker(
                resolved.args,
                worker_key,
                library_name=library_name,
                worker_engine_id=worker_engine_id,
                environ_overrides=environ_overrides,
            )
        except BaseException:
            self._hosted_workers.pop(worker_engine_id, None)
            hosted.started.set()
            raise
        proc = self._managed_worker_processes.get(worker_key)
        if proc is None:
            self._hosted_workers.pop(worker_engine_id, None)
            hosted.started.set()
            return worker_events.StartWorkerForEngineResultFailure(
                result_details=f"A worker for library '{library_name}' is already starting for that session."
            )
        hosted.process = proc
        hosted.started.set()
        # It registers with the artist's engine, not here, so its unregistration never reaches this
        # engine. Watching the process is what reports its exit and lets another start for the session.
        watcher = asyncio.get_running_loop().create_task(self._report_when_exited(worker_engine_id, hosted, proc))
        hosted.exit_watcher = watcher
        self._exit_watchers.add(watcher)
        watcher.add_done_callback(self._exit_watchers.discard)
        return worker_events.StartWorkerForEngineResultSuccess(
            result_details=f"Started worker {worker_engine_id} for library '{library_name}' for another engine."
        )

    def _start_refusal(
        self, library_name: str, session_id: str, worker_key: str
    ) -> worker_events.StartWorkerForEngineResultFailure | None:
        """The failure a start of `worker_key`'s worker for that session returns, or None if this engine will start it."""
        refusal = self._refusal_to_serve(library_name, session_id)
        if refusal is not None:
            return worker_events.StartWorkerForEngineResultFailure(
                result_details=refusal, session_unknown=self._session_must_register(library_name, session_id)
            )
        capacity_refusal = self._capacity_refusal(worker_key)
        if capacity_refusal is not None:
            return worker_events.StartWorkerForEngineResultFailure(result_details=capacity_refusal, at_capacity=True)
        return None

    def _capacity_refusal(self, worker_key: str) -> str | None:
        """Why this engine runs no more workers for other engines, or None if it may start `worker_key`'s."""
        max_workers = self.engine.config_manager.get_config_value(DISTRIBUTED_EXECUTION_MAX_WORKERS_KEY, default=None)
        if max_workers is None:
            return None
        # Starts still under way count, and a worker this start replaces does not.
        running = len(
            {hosted.worker_key for hosted in self._hosted_workers.values() if hosted.worker_key != worker_key}
        )
        if running < max_workers:
            return None
        return (
            f"this engine already runs {running} workers for other engines, the most its "
            f"{DISTRIBUTED_EXECUTION_MAX_WORKERS_KEY} setting allows."
        )

    def _refusal_to_serve(self, library_name: str, session_id: str) -> str | None:
        """Why this engine will not start `library_name`'s worker for that session, or None if it will."""
        if not self.engine.can_reach_other_engines:
            return "this engine has no connections to other engines."
        session = self._sessions.get(session_id)
        if session is None:
            return "the session asking for it never registered with this engine."
        if library_name not in session.library_versions:
            return "the session did not name it when it registered with this engine, so its version was never checked."
        return self._library_version_refusal(library_name, session.library_versions[library_name])

    def _library_version_refusal(self, library_name: str, version: str) -> str | None:
        """Why this engine's copy of `library_name` cannot run a session's `version` of it, or None if it can."""
        info = self.engine.library_manager.get_library_info_by_library_name(library_name)
        if info is None:
            return "the library is not installed on it."
        # A disabled library, one whose load failed, and one still installing have no environment to run in.
        if info.lifecycle_state != LibraryLifecycleState.LOADED:
            return f"the library is not loaded on it ({info.lifecycle_state})."
        if info.library_version != version:
            return (
                f"it has version {info.library_version} of the library, and this engine has {version}. "
                "Both engines must have the same version."
            )
        return None

    def _session_must_register(self, library_name: str, session_id: str) -> bool:
        """Whether the artist's engine should register the session again before this library can start."""
        session = self._sessions.get(session_id)
        return session is None or library_name not in session.library_versions

    async def _report_when_exited(
        self, worker_engine_id: str, hosted: _HostedWorker, proc: asyncio.subprocess.Process
    ) -> None:
        """Tell the artist's engine that a worker this engine ran for it exited, and why."""
        exit_code = await proc.wait()
        reason = "stopped"
        # A process this manager stopped itself was dropped from the records first.
        if self._managed_worker_processes.get(hosted.worker_key) is proc:
            del self._managed_worker_processes[hosted.worker_key]
            reason = "exited"
        logger.debug("Worker %s for key '%s' %s (exit code %s)", worker_engine_id, hosted.worker_key, reason, exit_code)
        self._hosted_workers.pop(worker_engine_id, None)
        # A request this engine sent it, such as freeing its models at a handoff, will never be answered.
        await self._tx.request_client.fail_requests_by_tag(
            worker_engine_id, worker_events.WorkerGoneError(f"worker '{worker_engine_id}' {reason}")
        )
        request = worker_events.ReportWorkerExitedRequest(
            worker_engine_id=worker_engine_id, exit_code=exit_code, reason=reason
        )
        try:
            result = await self.engine.request(hosted.engine_id, request, timeout_s=_ENGINE_REQUEST_TIMEOUT_S)
        except EngineNotFoundError as e:
            logger.warning("Could not tell engine %s that worker %s exited: %s", hosted.engine_id, worker_engine_id, e)
            return
        if not isinstance(result, worker_events.ReportWorkerExitedResultSuccess):
            logger.warning(
                "Engine %s did not record that worker %s exited: %s",
                hosted.engine_id,
                worker_engine_id,
                result.result_details,
            )

    @handles(
        worker_events.StopWorkerForEngineRequest,
        sender=RequiredSender.ENGINE,
        owner=lambda manager, request, _sender: manager._engine_served_by(request.worker_engine_id),
    )
    async def handle_stop_worker_for_engine_request(
        self, request: worker_events.StopWorkerForEngineRequest
    ) -> worker_events.StopWorkerForEngineResultSuccess | worker_events.StopWorkerForEngineResultFailure:
        """Stop a worker this engine runs for an artist's session, at that artist's engine's request."""
        wid = request.worker_engine_id
        hosted = self._hosted_workers.get(wid)
        if hosted is None:
            return worker_events.StopWorkerForEngineResultSuccess(
                result_details=f"This engine is not running worker {wid}."
            )
        await self._stop_hosted_worker(hosted)
        return worker_events.StopWorkerForEngineResultSuccess(result_details=f"Stopped worker {wid}.")

    @handles(worker_events.WithdrawSessionOfferRequest, sender=RequiredSender.ENGINE)
    async def handle_withdraw_session_offer_request(
        self,
        request: worker_events.WithdrawSessionOfferRequest,  # noqa: ARG002
    ) -> worker_events.WithdrawSessionOfferResultSuccess:
        """Forget the sessions the engine that asked offered, and stop the workers run for them."""
        engine_id = require_request_sender().engine_id
        await self._forget_engine(engine_id, why="withdrew its session offer")
        return worker_events.WithdrawSessionOfferResultSuccess(
            result_details=f"Forgot the sessions engine {engine_id} offered."
        )

    @handles(
        worker_events.AdmitNodeRequest,
        sender=RequiredSender.SESSION,
        owner=lambda manager, _request, sender: manager._engine_that_offered(sender),
    )
    async def handle_admit_node_request(
        self, request: worker_events.AdmitNodeRequest
    ) -> worker_events.AdmitNodeResultSuccess | worker_events.AdmitNodeResultFailure:
        """Answer once another engine's node may run here, which waits while another run has this engine.

        Another engine's run waits in this engine's own line of runs, and holds this engine from when it
        starts until it lets it go, so its later nodes are admitted at once.
        """
        sender = require_request_sender()
        if self._engine_that_offered(sender) is None:
            return worker_events.AdmitNodeResultFailure(
                result_details="the session asking for it never registered with this engine."
            )
        engine_run = self._engine_run_for(sender.engine_id, request)
        if not engine_run.admitted.done() and any(
            admission.host_engine_id == sender.engine_id for admission in self._admissions.values()
        ):
            logger.error(
                "A run from engine %s waits for this engine while a run here waits for engine %s. Neither "
                "continues until one is cancelled. Engines that send nodes to each other can stop each other this way.",
                sender.engine_id,
                sender.engine_id,
            )
        engine_run.waiting.add(request.admission_id)
        self._report_place_in_line(engine_run, request.admission_id)
        try:
            refusal = await asyncio.shield(engine_run.admitted)
        finally:
            engine_run.waiting.discard(request.admission_id)
        if refusal is not None:
            return worker_events.AdmitNodeResultFailure(result_details=refusal)
        if request.admission_id in engine_run.abandoned:
            engine_run.abandoned.discard(request.admission_id)
            self._let_go_if_idle(engine_run)
            return worker_events.AdmitNodeResultFailure(result_details="the node ended before it was admitted.")
        engine_run.running.add(request.admission_id)
        self._cancel_idle_timer(engine_run)
        return worker_events.AdmitNodeResultSuccess(result_details=f"Admitted node '{request.node_name}'.")

    @handles(
        worker_events.NodeExecutionEndedRequest,
        sender=RequiredSender.SESSION,
        owner=lambda manager, request, _sender: manager._engine_admitted(request.admission_id),
    )
    async def handle_node_execution_ended_request(
        self, request: worker_events.NodeExecutionEndedRequest
    ) -> worker_events.NodeExecutionEndedResultSuccess:
        """Record that a node another engine ran here ended, ending its run if it was the run's only node."""
        engine_run = self._engine_run_admitting(request.admission_id)
        if engine_run is None:
            return worker_events.NodeExecutionEndedResultSuccess(
                result_details="This engine was not running that node, so there was nothing to record."
            )
        # A node sent without a run is its run's only node, admitted or still in line.
        if engine_run.run_id is None:
            await self._end_engine_run(engine_run, "its node ended before it was admitted.")
            return worker_events.NodeExecutionEndedResultSuccess(result_details="Recorded the node as ended.")
        if request.admission_id in engine_run.waiting:
            engine_run.abandoned.add(request.admission_id)
        engine_run.running.discard(request.admission_id)
        self._let_go_if_idle(engine_run)
        return worker_events.NodeExecutionEndedResultSuccess(result_details="Recorded the node as ended.")

    @handles(
        worker_events.EndRunRequest,
        sender=RequiredSender.SESSION,
        owner=lambda manager, _request, sender: manager._engine_that_offered(sender),
    )
    async def handle_end_run_request(self, request: worker_events.EndRunRequest) -> worker_events.EndRunResultSuccess:
        """Let another engine's run go, now that it ended, so the next run in line gets this engine."""
        engine_id = require_request_sender().engine_id
        engine_run = self._engine_runs.get((engine_id, request.run_id))
        if engine_run is None:
            return worker_events.EndRunResultSuccess(
                result_details=f"This engine was not holding run {request.run_id}."
            )
        await self._end_engine_run(engine_run, "its run ended.")
        return worker_events.EndRunResultSuccess(result_details=f"Let run {request.run_id} of engine {engine_id} go.")

    @handles(
        worker_events.ReportPlaceInLineRequest,
        sender=RequiredSender.ENGINE,
        owner=lambda manager, request, _sender: manager._engine_admitting(request.admission_id),
    )
    def handle_report_place_in_line_request(
        self, request: worker_events.ReportPlaceInLineRequest
    ) -> worker_events.ReportPlaceInLineResultSuccess:
        """Report the place in line of a run here whose node waits on another engine, as if it waited here."""
        admission = self._admissions.get(request.admission_id)
        # A report that arrives after the admission was answered is out of date.
        waiting = admission is not None and admission.request_task is not None and not admission.request_task.done()
        if admission is None or admission.run_id is None or not waiting:
            return worker_events.ReportPlaceInLineResultSuccess(
                result_details="No run here waits on that admission, so there was nothing to report."
            )
        self.engine.flow_manager.report_run_waiting_for_engine(admission.run_id, request.position)
        return worker_events.ReportPlaceInLineResultSuccess(result_details=f"Reported place {request.position}.")

    async def hold_for_engine_run(self, engine_run: _EngineRun) -> ResultPayload:
        """Hold this engine for another engine's run, from when it reaches the front of the line until it lets go.

        Passing from one engine's run to another's first frees workers, as `distributed_execution.release`
        says. Freeing is best effort: a failure there still hands this engine over.
        """
        if self._last_engine_run_engine_id not in (None, engine_run.engine_id):
            try:
                await self._free_for_handoff(engine_run.engine_id)
            except Exception:
                logger.exception("Could not free this engine's workers for engine %s's run.", engine_run.engine_id)
        self._last_engine_run_engine_id = engine_run.engine_id
        try:
            if not engine_run.admitted.done():
                engine_run.admitted.set_result(None)
            self._let_go_if_idle(engine_run)
            await engine_run.released.wait()
        finally:
            self._cancel_idle_timer(engine_run)
            self._forget_engine_run(engine_run)
        return worker_events.EndRunResultSuccess(
            result_details=f"Engine {engine_run.engine_id}'s run let this engine go."
        )

    def refuse_engine_run(self, engine_run: _EngineRun, details: str) -> None:
        """Tell another engine's run waiting here that it will not get this engine, and why.

        Also releases it, so a run refused while `hold_for_engine_run` frees workers for it lets this engine go.
        """
        if not engine_run.admitted.done():
            engine_run.admitted.set_result(details)
        engine_run.released.set()
        self._forget_engine_run(engine_run)

    def _engine_that_offered(self, sender: RequestSender) -> str | None:
        """The engine that offered the sender's session to this one, or None if none has."""
        session = self._sessions.get(cast("str", sender.session_id))
        if session is None:
            return None
        return session.engine_id

    def _engine_served_by(self, worker_engine_id: str) -> str | None:
        """The engine a worker this engine runs serves, or None if it runs no such worker."""
        hosted = self._hosted_workers.get(worker_engine_id)
        if hosted is None:
            return None
        return hosted.engine_id

    async def _recycle_hosted_worker(self, hosted: _HostedWorker) -> None:
        """Stop a worker for a handoff, returning once its engine has heard it stopped.

        Its engine starts it again before that library's next node, so the next run's node must not be
        admitted while that engine still counts the stopped worker as running.
        """
        await self._stop_hosted_worker(hosted)
        if hosted.exit_watcher is not None:
            await asyncio.shield(hosted.exit_watcher)

    async def _stop_hosted_worker(self, hosted: _HostedWorker) -> None:
        """Stop a worker this engine runs for another engine. Its exit watcher reports it stopped.

        A spawn still under way is waited out first, so a stop sent while the start is being answered
        stops the process that spawn makes.
        """
        await hosted.started.wait()
        proc = hosted.process
        if proc is None or self._managed_worker_processes.get(hosted.worker_key) is not proc:
            return
        del self._managed_worker_processes[hosted.worker_key]
        await self._terminate_via_spawn_loop(hosted.worker_key, proc)

    @handles(worker_events.EngineSessionHeartbeatRequest, sender=RequiredSender.ENGINE)
    def handle_engine_session_heartbeat_request(
        self, request: worker_events.EngineSessionHeartbeatRequest
    ) -> worker_events.EngineSessionHeartbeatResultSuccess | worker_events.EngineSessionHeartbeatResultFailure:
        """Answer another engine's heartbeat, saying whether a session between the two still exists."""
        engine_id = require_request_sender().engine_id
        if engine_id not in self._session_peers():
            return worker_events.EngineSessionHeartbeatResultFailure(
                result_details=f"This engine has no session with engine {engine_id}."
            )
        return worker_events.EngineSessionHeartbeatResultSuccess(result_details=f"Heartbeat {request.heartbeat_id}.")

    def _session_peers(self) -> set[str]:
        """The engines there is a session with: those this engine registered with, and those that offered it one."""
        peers = {host_engine_id for host_engine_id, _session_id in self._engine_registrations}
        peers.update(session.engine_id for session in self._sessions.values())
        peers.discard(self.engine.get_engine_id())
        return peers

    async def _challenge_engines(self) -> None:
        """Lose each engine there is a session with that left too many heartbeats unanswered, and challenge the rest.

        Over Griptape Cloud nothing else reports another engine gone, and a node waiting for it to admit
        the node would wait forever.
        """
        peers = self._session_peers()
        for engine_id in [engine_id for engine_id in self._engine_challenges_unanswered if engine_id not in peers]:
            del self._engine_challenges_unanswered[engine_id]
        for engine_id in peers:
            if self._engine_challenges_unanswered.get(engine_id, 0) >= self.unanswered_challenges_allowed:
                await self._lose_engine(engine_id, f"engine {engine_id} stopped answering heartbeats.")
                continue
            if engine_id in self._engine_challenges:
                continue
            task = asyncio.get_running_loop().create_task(self._challenge_engine(engine_id))
            self._engine_challenges[engine_id] = task
            task.add_done_callback(lambda _task, engine_id=engine_id: self._engine_challenges.pop(engine_id, None))

    async def _challenge_engine(self, engine_id: str) -> None:
        self._engine_challenges_unanswered[engine_id] = self._engine_challenges_unanswered.get(engine_id, 0) + 1
        request = worker_events.EngineSessionHeartbeatRequest(heartbeat_id=str(uuid.uuid4()))
        try:
            result = await self.engine.request(engine_id, request, timeout_s=self.heartbeat_interval_s)
        except EngineNotFoundError as e:
            logger.debug("Engine %s did not answer a heartbeat: %s", engine_id, e)
            return
        if isinstance(result, worker_events.EngineSessionHeartbeatResultSuccess):
            self._engine_challenges_unanswered[engine_id] = 0
            return
        # It is there but forgot the session, as after it restarted, so nothing waiting on it will be answered.
        await self._lose_engine(engine_id, f"engine {engine_id} no longer has a session with this engine.")

    async def _lose_engine(self, engine_id: str, reason: str) -> None:
        """Settle what a session with `engine_id` left behind, as when the connection to it is lost."""
        logger.warning("Lost engine %s: %s", engine_id, reason)
        self._engine_challenges_unanswered.pop(engine_id, None)
        for admission in self._admissions.values():
            if admission.host_engine_id != engine_id or admission.request_task is None:
                continue
            if admission.request_task.done():
                continue
            admission.given_up = reason
            admission.request_task.cancel()
        # Registered again by the next start, as an engine that restarted needs.
        for key in [key for key in self._engine_registrations if key[0] == engine_id]:
            del self._engine_registrations[key]
        await self.on_engine_lost(engine_id)

    async def on_engine_lost(self, engine_id: str) -> None:
        """Settle the starts sent to an engine whose connection was lost, or forget an artist's engine whose connection was.

        On the artist's side, a start the other engine accepted but whose worker never registered would
        otherwise wait out the library load timeout. On the other engine's side, that artist's sessions are
        forgotten and the workers run for them stopped.
        """
        for wid, start in list(self._remote_starts.items()):
            if start.host_engine_id != engine_id:
                continue
            del self._remote_starts[wid]
            self.note_worker_unavailable(
                start.library_name, f"the connection to {engine_id}, which runs its worker, was lost."
            )
        await self._forget_engine(engine_id, why="lost its connection")

    async def _forget_engine(self, engine_id: str, *, why: str) -> None:
        """Forget the sessions `engine_id` offered this engine, let its runs go, and stop their workers."""
        await self._end_engine_runs_from(engine_id, f"engine {engine_id} {why}.")
        offered = [session_id for session_id, session in self._sessions.items() if session.engine_id == engine_id]
        for session_id in offered:
            del self._sessions[session_id]
        hosted = [worker for worker in self._hosted_workers.values() if worker.engine_id == engine_id]
        if not offered and not hosted:
            return
        logger.warning(
            "Engine %s %s; forgetting its %d session(s) and stopping the %d worker(s) run for it.",
            engine_id,
            why,
            len(offered),
            len(hosted),
        )
        await asyncio.gather(*(self._stop_hosted_worker(worker) for worker in hosted))

    def _engine_run_for(self, engine_id: str, request: worker_events.AdmitNodeRequest) -> _EngineRun:
        """The run here that stands for another engine's run, submitted to this engine's line if it is new."""
        key = (engine_id, request.run_id or request.admission_id)
        engine_run = self._engine_runs.get(key)
        # A run that let this engine go is not joined: its next node waits in line again.
        if engine_run is not None and not engine_run.released.is_set():
            return engine_run
        engine_run = _EngineRun(
            engine_id=engine_id,
            run_id=request.run_id,
            key=key,
            admitted=asyncio.get_running_loop().create_future(),
        )
        self._engine_runs[key] = engine_run
        spec = _EngineRunSpec(manager=self, engine_run=engine_run, flow_name=f"a run from engine {engine_id}")
        engine_run.line_run = self.engine.flow_manager.submit_run(spec)
        return engine_run

    async def _end_engine_runs_from(self, engine_id: str, reason: str) -> None:
        """Let go of every run from `engine_id`, holding this engine or waiting for it."""
        for engine_run in [run for run in self._engine_runs.values() if run.engine_id == engine_id]:
            await self._end_engine_run(engine_run, reason)

    async def _end_engine_run(self, engine_run: _EngineRun, reason: str) -> None:
        """Let another engine's run go: one holding this engine releases it, and one still waiting leaves the line."""
        if engine_run.admitted.done():
            engine_run.released.set()
            return
        # Refused first, so its waiting nodes hear `reason` rather than the line's own cancellation.
        self.refuse_engine_run(engine_run, reason)
        if engine_run.line_run is not None:
            await self.engine.flow_manager.cancel_run(engine_run.line_run.run_id)

    def _let_go_if_idle(self, engine_run: _EngineRun) -> None:
        """Start the idle timeout of a run holding this engine whose nodes have all ended."""
        if not engine_run.admitted.done() or engine_run.admitted.result() is not None:
            return
        if engine_run.running or engine_run.released.is_set():
            return
        self._cancel_idle_timer(engine_run)
        idle_timeout_s = self.engine.config_manager.get_config_value(
            DISTRIBUTED_EXECUTION_IDLE_TIMEOUT_KEY, default=300.0, cast_type=float
        )
        engine_run.idle_timer = asyncio.get_running_loop().call_later(idle_timeout_s, engine_run.released.set)

    @staticmethod
    def _cancel_idle_timer(engine_run: _EngineRun) -> None:
        if engine_run.idle_timer is not None:
            engine_run.idle_timer.cancel()
            engine_run.idle_timer = None

    def _forget_engine_run(self, engine_run: _EngineRun) -> None:
        if self._engine_runs.get(engine_run.key) is engine_run:
            del self._engine_runs[engine_run.key]

    def _engine_run_admitting(self, admission_id: str) -> _EngineRun | None:
        """The run here whose node holds the admission, waiting or running, or None."""
        for engine_run in self._engine_runs.values():
            if admission_id in engine_run.running or admission_id in engine_run.waiting:
                return engine_run
        return None

    def _engine_admitted(self, admission_id: str) -> str | None:
        """The engine whose node holds an admission on this engine, or None if none does."""
        engine_run = self._engine_run_admitting(admission_id)
        if engine_run is None:
            return None
        return engine_run.engine_id

    def _engine_admitting(self, admission_id: str) -> str | None:
        """The engine this engine asked to admit a node, or None if it asked for no such admission."""
        admission = self._admissions.get(admission_id)
        if admission is None:
            return None
        return admission.host_engine_id

    def _on_place_in_line(self, run: Run) -> None:
        """Tell the engine whose run waits here its place in line, for each of its nodes waiting."""
        if not isinstance(run.spec, _EngineRunSpec):
            return
        engine_run = run.spec.engine_run
        for admission_id in list(engine_run.waiting):
            self._send_notice(self._send_place_in_line(engine_run.engine_id, admission_id, run.position))

    def _report_place_in_line(self, engine_run: _EngineRun, admission_id: str) -> None:
        """Tell a node that just asked to be admitted its run's place in line, if the run waits."""
        if engine_run.line_run is None or not engine_run.line_run.position:
            return
        self._send_notice(self._send_place_in_line(engine_run.engine_id, admission_id, engine_run.line_run.position))

    async def _send_place_in_line(self, engine_id: str, admission_id: str, position: int) -> None:
        request = worker_events.ReportPlaceInLineRequest(admission_id=admission_id, position=position)
        try:
            result = await self.engine.request(engine_id, request, timeout_s=_ENGINE_REQUEST_TIMEOUT_S)
        except EngineNotFoundError as e:
            logger.warning("Could not tell engine %s its run's place in line: %s", engine_id, e)
            return
        if not isinstance(result, worker_events.ReportPlaceInLineResultSuccess):
            logger.warning("Engine %s did not take its run's place in line: %s", engine_id, result.result_details)

    def _on_run_ended(self, run_id: str) -> None:
        """Tell every engine the ended run sent a node to that it ended, so they serve the next run."""
        for engine_id in self._run_engines.pop(run_id, set()):
            self._send_notice(self._send_end_run(engine_id, run_id))

    async def _send_end_run(self, engine_id: str, run_id: str) -> None:
        request = worker_events.EndRunRequest(run_id=run_id)
        try:
            result = await self.engine.request(engine_id, request, timeout_s=_ENGINE_REQUEST_TIMEOUT_S)
        except EngineNotFoundError as e:
            logger.warning("Could not tell engine %s that run %s ended: %s", engine_id, run_id, e)
            return
        if not isinstance(result, worker_events.EndRunResultSuccess):
            logger.warning("Engine %s did not end run %s: %s", engine_id, run_id, result.result_details)

    def _send_notice(self, notice: Coroutine[Any, Any, None]) -> None:
        """Send a report to another engine without waiting, holding the task until it is answered."""
        task = asyncio.get_running_loop().create_task(notice)
        self._engine_notices.add(task)
        task.add_done_callback(self._engine_notices.discard)

    async def _free_for_handoff(self, incoming_engine_id: str) -> None:
        """Free workers, as `distributed_execution.release` says, before this engine passes to `incoming_engine_id`'s run."""
        config = self.engine.config_manager
        which = config.get_config_value(DISTRIBUTED_EXECUTION_RELEASE_WHICH_KEY, default="others")
        how = config.get_config_value(DISTRIBUTED_EXECUTION_RELEASE_HOW_KEY, default="drop_cache")
        if which == "none":
            return
        frees = []
        for worker_engine_id, hosted in list(self._hosted_workers.items()):
            if which == "others" and hosted.engine_id == incoming_engine_id:
                continue
            if how == "recycle":
                # Its exit is reported stopped, so its engine starts it again before that library's next node.
                logger.info("Stopping worker %s to free this engine for another engine's run.", worker_engine_id)
                frees.append(self._recycle_hosted_worker(hosted))
            else:
                frees.append(self._drop_library_named_objects(worker_engine_id, hosted))
        # All at once, so a worker that never answers delays the handoff by one timeout, not one each. Each is
        # waited out even if another fails, so none is still stopping once the next run is admitted.
        outcomes = await asyncio.gather(*frees, return_exceptions=True)
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                logger.error("Could not free a worker for engine %s's run: %s", incoming_engine_id, outcome)

    async def _drop_library_named_objects(self, worker_engine_id: str, hosted: _HostedWorker) -> None:
        """Have a worker this engine runs drop what its libraries keep between runs, waiting until it has."""
        request_id = str(uuid.uuid4())
        future = await self._tx.request_client.track_request(
            request_id, tag=worker_engine_id, resolve_failures_as_payload=True
        )
        event = EventRequest(
            request=worker_events.DropLibraryNamedLocalObjectsRequest(),
            request_id=request_id,
            response_topic=f"engines/{self.engine.get_engine_id()}/response",
        )
        request_topic = f"sessions/{hosted.session_id}/workers/{worker_engine_id}/request"
        try:
            await self._tx.send_message("EventRequest", converters.engine.dumps(event), request_topic)
            result = await asyncio.wait_for(asyncio.wrap_future(future), timeout=_ENGINE_REQUEST_TIMEOUT_S)
        except TimeoutError:
            self._tx.request_client.discard_request(request_id)
            logger.warning(
                "Worker %s did not free its models within %.0f seconds.", worker_engine_id, _ENGINE_REQUEST_TIMEOUT_S
            )
            return
        except worker_events.WorkerGoneError:
            # Gone, so nothing of it is left to free.
            logger.info("Worker %s exited before it freed its models.", worker_engine_id)
            return
        except BaseException:
            self._tx.request_client.discard_request(request_id)
            raise
        logger.info("Worker %s: %s", worker_engine_id, result.get("result", {}).get("result_details"))

    async def _wait_for_session(self, library_name: str) -> str | None:
        """Return the active session's id once there is one, or None, recorded, if none came."""
        # If a session is already active, skip the wait entirely.
        if not self.engine.get_session_id():
            logger.info(
                "Worker for library '%s' is waiting for a session to start before spawning. "
                "Start a session (via the Griptape Nodes GUI or AppStartSessionRequest) to proceed.",
                library_name,
            )
            await self._session_ready_event.wait()
            logger.debug("Session started; spawning worker for library '%s'.", library_name)
        session_id = self.engine.get_session_id()
        if not session_id:
            logger.error("Session event set but no session ID available for library '%s'.", library_name)
            self.note_worker_unavailable(library_name, "no session was available to start its worker process.")
            return None
        return session_id

    def _worker_command(self, library_name: str, session_id: str) -> WorkerCommand | WorkerCommandRefusal:
        """The command that starts `library_name`'s worker in session `session_id`."""
        command = [
            sys.executable,
            "-m",
            "griptape_nodes_app",
            "engine",
            "--session-id",
            session_id,
            "--library-name",
            library_name,
        ]
        # A configured prefix starts the worker inside an environment another tool prepares for
        # this library. Read per spawn, like the request list, so a reload picks up a change.
        # The engine's startup environment, not a project's: a project template must not change
        # which packages a library's worker resolves or how it is started.
        startup_environ = self.engine.project_manager.get_pre_project_environ()
        return resolve_worker_command(
            command=command,
            prefix=read_worker_command_prefix(self.engine.config_manager, startup_environ),
            library_name=library_name,
            worker_requests=worker_requests_from_environment(startup_environ),
            engine_version=engine_version,
            python_version=f"{sys.version_info.major}.{sys.version_info.minor}",
            environment_mode=provisioned_by_environment(self.engine.config_manager),
        )

    def _engine_for_library(self, library_name: str) -> str | None:
        """The ID of the engine the distribution settings send `library_name` to, or None for here.

        Settings that name this engine mean here too, as when engines that share a workspace share its
        config file.
        """
        libraries = self.engine.config_manager.get_config_value(DISTRIBUTED_EXECUTION_LIBRARY_ENGINES_KEY, default={})
        engine_id = libraries.get(library_name)
        if engine_id is None or engine_id == self.engine.get_engine_id():
            return None
        return engine_id

    async def _spawn_general_purpose_worker_when_session_ready(self) -> None:
        """Wait for an active session, then spawn the general-purpose worker.

        It loads every library but builds none of their environments: the orchestrator built them
        while loading the same libraries, before it asked for any worker.

        Started with the engine's own interpreter and environment, and without
        `worker.command_prefix`: the prefix prepares an environment for one library, and this
        worker serves none. It runs where the engine runs, as Isolated Execution's separate process
        always has.
        """
        if not self.engine.get_session_id():
            logger.info("The general-purpose worker is waiting for a session to start before spawning.")
            await self._session_ready_event.wait()
        session_id = self.engine.get_session_id()
        if not session_id:
            logger.error("Session event set but no session ID available for the general-purpose worker.")
            self.note_worker_unavailable(None, "no session was available to start it.")
            return
        command = [
            sys.executable,
            "-m",
            "griptape_nodes_app",
            "engine",
            "--session-id",
            session_id,
        ]
        await self.spawn_worker(command, None)

    def _log_spawn_error(self, task: asyncio.Task, library_name: str | None) -> None:
        """Record a spawn that raised before producing a worker.

        `handle_start_worker_request` schedules the spawn and returns Success immediately, so its
        caller cannot tell that a bad interpreter or an OSError stopped the worker ever existing.
        Refusals that return rather than raise are invisible here and record themselves.
        """
        # Asked before `task.exception()`, which raises on a cancelled task. From a done-callback
        # that surfaces as loop-level "Exception in callback" noise and skips the refusal below.
        # Cancellation reaches here at loop teardown, where no run is waiting on a worker.
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        logger.error("Failed to spawn worker for key '%s': %s", library_name, exc)
        self.note_worker_unavailable(library_name, f"the worker process that runs it could not be started ({exc}).")

    def expect_worker(self, library_name: str | None) -> None:
        """Declare that a worker is coming for `library_name`, so callers can wait for it.

        Called as the spawn is scheduled. Installs a fresh readiness gate and drops any account
        of a previous attempt, which no longer describes the situation. Every attempt gets one: a
        worker registers BEFORE it loads libraries, so execution routing has to wait for the load
        rather than for the registration.
        """
        self._execution_ready[library_name] = asyncio.Event()
        self._worker_unavailable.pop(library_name, None)

    def note_library_loaded(self, library_name: str | None) -> None:
        """Release anything waiting on `library_name`, now that its worker has loaded it."""
        ready = self._execution_ready.get(library_name)
        if ready is not None:
            ready.set()

    def note_worker_unavailable(self, library_name: str | None, reason: str) -> None:
        """Record why no worker will run `library_name`, and release anything waiting on one.

        Recording without releasing leaves the next run waiting out the whole startup grace before
        blaming a library load that never began; releasing without recording leaves it blaming a
        worker that "may still be starting up" for the rest of the session.
        """
        self._worker_unavailable[library_name] = reason
        self._refused_for_capacity.discard(library_name)
        self.note_library_loaded(library_name)

    def forget_library(self, library_name: str) -> None:
        """Drop everything this manager records about `library_name`.

        Called when a library leaves the registry. These are keyed by a bare name, so without this
        they outlive the record they describe: the reason from an evicted worker would still be
        reported after the library came back declaring no execution dependencies at all, for a
        library that now runs in this process.
        """
        self._execution_ready.pop(library_name, None)
        self._worker_unavailable.pop(library_name, None)

    def worker_unavailable_reason(self, library_name: str | None) -> str | None:
        """Why no worker is available to run `library_name`, or None if that is not the problem."""
        return self._worker_unavailable.get(library_name)

    def worker_host(self, worker_engine_id: str) -> str | None:
        """The engine that runs a worker this engine asked another engine for, or None for any other worker.

        Known from the moment the start is recorded, before it is sent, so traffic for a worker that registers
        before the start's answer arrives can already be carried to its engine.
        """
        start = self._remote_starts.get(worker_engine_id)
        if start is not None:
            return start.host_engine_id
        registration = self._workers.get(worker_engine_id)
        if registration is None:
            return None
        return registration.host_engine_id

    def has_settled(self, library_name: str | None) -> bool:
        """Whether `library_name` has settled -- loaded, refused, or died -- rather than pending.

        Settled is not available: a refused spawn settles, and `worker_unavailable_reason` then
        says why. This is a wait predicate, not an answer about whether execution can proceed.
        """
        ready = self._execution_ready.get(library_name)
        return ready is None or ready.is_set()

    async def wait_until_executable(self, library_name: str | None) -> None:
        """Block until `library_name` can be executed, or until it is settled that it cannot.

        A worker registers BEFORE it loads libraries -- registration is what carries the
        orchestrator's project to it, and the project decides how libraries load -- so routing would
        otherwise see somewhere to send execution whose library is not loaded yet, and forwarding
        into that window fails node creation there.

        Cannot hang: every terminal outcome releases the gate (loaded, spawn refused, spawn died,
        worker evicted). Bounded anyway, because "every" is a claim about code that will keep
        changing and the cost of it being wrong once is a node that hangs with no diagnosis. A named
        timeout is a bug report; an unbounded wait is a mystery.

        A library whose worker another engine stopped, to free itself for another engine's run, has that
        worker started there again first.
        """
        if library_name is not None:
            await self._start_remote_worker_if_stopped(library_name)
        if self.has_settled(library_name):
            return
        if library_name is None:
            logger.info("Waiting for the general-purpose worker to finish loading before executing")
        else:
            logger.info("Waiting for library '%s''s worker to finish loading before executing", library_name)
        try:
            with anyio.fail_after(self.library_load_timeout_s):
                await self._execution_ready[library_name].wait()
        except TimeoutError:
            if library_name is None:
                msg = (
                    "Attempted to run a node with Isolated Execution. Failed because the background process "
                    f"that runs it did not finish loading libraries within {self.library_load_timeout_s:.0f} seconds."
                )
            else:
                msg = (
                    f"Attempted to run a node from library '{library_name}'. Failed because its worker "
                    f"process did not finish loading the library within {self.library_load_timeout_s:.0f} seconds."
                )
            raise RuntimeError(msg) from None

    async def _start_remote_worker_if_stopped(self, library_name: str) -> None:
        """Start a library's worker on its engine again, if that engine stopped it rather than it ending on its own."""
        if self._engine_for_library(library_name) is None:
            return
        if self.is_worker_running_or_starting(library_name):
            return
        # A worker that ended on its own left a reason, which stays until the library is reset.
        if self.worker_unavailable_reason(library_name) is not None and library_name not in self._refused_for_capacity:
            return
        self._refused_for_capacity.discard(library_name)
        await self.handle_start_worker_request(worker_events.StartWorkerRequest(library_name=library_name))

    def get_topics_to_subscribe(self, *, is_worker: bool) -> list[str]:
        """Build the list of topics to subscribe to at connection start.

        In worker mode the engine subscribes only to its dedicated per-worker request topic
        and its direct-target engine topic. Workers must NOT subscribe to the generic "request"
        topic, which is where the MCP server broadcasts; doing so causes workers to handle
        requests intended for the orchestrator.

        In orchestrator mode it subscribes to the generic "request" topic (MCP/API entry point)
        and the session request topic.
        """
        engine_id = self.engine.get_engine_id()
        session_id = self.engine.get_session_id()

        topics: list[str] = []
        if engine_id:
            topics.append(f"engines/{engine_id}/request")

        if is_worker:
            # Subscribe ONLY to this worker's dedicated per-worker request topic.
            # The orchestrator explicitly routes events here; worker never sees other workers' events.
            if session_id and engine_id:
                topics.append(f"sessions/{session_id}/workers/{engine_id}/request")
        else:
            # Orchestrator handles all broadcast requests from the MCP server and the GUI.
            topics.append("request")
            if session_id:
                topics.append(f"sessions/{session_id}/request")

        return topics

    def get_message_filters(self, *, is_worker: bool) -> list[Callable[[dict[str, Any]], Awaitable[bool]]]:
        """Build the message filters to install at connection start.

        The companion to `get_topics_to_subscribe`: that decides which messages arrive, this decides
        who claims them. Keyed on the role in one place so a filter cannot be installed for one role
        and forgotten for the other.

        Install these AFTER the RequestClient's own filter. They claim every result, so running one
        ahead of it would swallow the reply a caller is awaiting.
        """
        if is_worker:
            return [self._discard_unaddressed_result]
        return [self._claim_worker_result]

    async def _claim_worker_result(self, message: dict[str, Any]) -> bool:
        """Claim a result no pending request wanted, and relay it to the GUI.

        One addressed to this engine's own response topic answers a request this engine sent and stopped
        waiting for, because it timed out or its engine was lost, so it is dropped rather than shown.
        """
        payload = message.get("payload", {})
        if payload.get("event_type") not in RESULT_EVENT_TYPES:
            return False
        if payload.get("response_topic") == f"engines/{self.engine.get_engine_id()}/response":
            logger.debug(
                "Dropping a late reply to request %s, which this engine stopped waiting for.", payload.get("request_id")
            )
            return True
        try:
            await self.relay_worker_result(payload)
        except Exception:
            logger.exception("Failed to relay worker result")
        return True

    async def _discard_unaddressed_result(self, message: dict[str, Any]) -> bool:
        """Claim and drop a result this worker never asked for.

        A worker has no GUI to relay to, and its own replies are claimed ahead of this by the
        RequestClient. What reaches here is another process's answer arriving over the shared bus,
        or a late reply to a request this worker stopped tracking.
        """
        payload = message.get("payload", {})
        if payload.get("event_type") not in RESULT_EVENT_TYPES:
            return False
        logger.debug(
            "Dropping a %s addressed to %s; this worker did not ask for it.",
            payload.get("result_type") or payload.get("event_type"),
            payload.get("response_topic"),
        )
        return True

    async def forward_event_to_worker(
        self,
        event: EventRequest,
        *,
        worker_engine_id: str,
        worker_request_topic: str,
    ) -> None:
        """Route an event to the appropriate worker's dedicated request topic.

        MVP: routes to the single registered worker.
        Future: consult a WorkerRegistry to select the correct worker based on event type
        or target library.
        """
        session_id = self.engine.get_session_id()
        worker_response_topic = f"sessions/{session_id}/workers/{worker_engine_id}/response"
        forwarded = event.model_copy(update={"response_topic": worker_response_topic})
        logger.debug("Forwarding %s to worker %s", type(event.request).__name__, worker_engine_id)
        await self._tx.send_message("EventRequest", converters.engine.dumps(forwarded), worker_request_topic)

    async def _on_config_changed(self, _event: ConfigChanged) -> None:
        """Fan out a ReloadConfigRequest after the orchestrator's config mutation succeeded.

        ConfigManager only emits ``ConfigChanged`` after the disk write
        succeeded, so receiving the event is sufficient evidence that
        workers should re-read the file.

        Listener is async and awaits the broadcast directly so the work
        is owned by the listener's own task. ``broadcast_app_event``
        invokes listeners on a transient ``ThreadRunner`` side loop when
        called from sync code (the production path); a fire-and-forget
        ``asyncio.create_task`` from inside the listener would land on
        that side loop and be killed when ``ThreadRunner.__exit__``
        stops the loop, so the broadcast must be awaited inline.

        Lazy import breaks a cycle between this module and
        ``griptape_nodes.app.worker_routing``, which itself imports
        ``EventManager`` from the retained_mode managers package.
        """
        from griptape_nodes.app.worker_routing import ReloadConfigRequest

        if self._transport is None or not self._workers:
            return
        await self.broadcast_to_workers(EventRequest(request=ReloadConfigRequest()))

    def schedule_pending_local_object_releases(self) -> None:
        """Send queued handle releases to the workers on the caller's running loop, if there is one.

        Sync because the releases happen on sync paths: a parameter value being written, a node being
        deleted. Fire-and-forget like `schedule_broadcast`, and for the same reason it is acceptable here --
        losing the message leaves a worker holding an object until a later teardown.

        With no running loop the keys stay queued and the next drain sends them, so a release issued from a
        thread or a sync test is not lost.
        """
        if self._transport is None or not self._workers:
            # Nothing to tell. Draining keeps the queue from growing for the life of the process.
            self.engine.resource_manager.drain_pending_worker_releases()
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        keys = self.engine.resource_manager.drain_pending_worker_releases()
        if not keys:
            return
        task = loop.create_task(self._send_local_object_releases(keys))
        self._inflight_broadcast_tasks.add(task)
        task.add_done_callback(self._inflight_broadcast_tasks.discard)

    async def _send_local_object_releases(self, keys: list[str]) -> None:
        """Fan the keys out, putting them back on the queue if the send fails.

        They were drained before this ran, so without the re-queue a task that dies with a transient side
        loop -- the case `broadcast_drop_all_local_objects` documents below -- would take them with it and
        nothing would ever retry.

        `broadcast_pending_local_object_releases` does not re-queue, because the drop-all after teardown
        covers its keys.
        """
        from griptape_nodes.app.worker_routing import DropLocalObjectsRequest

        try:
            await self.broadcast_to_workers(EventRequest(request=DropLocalObjectsRequest(keys=keys)))
        except Exception as e:
            self.engine.resource_manager.requeue_pending_worker_releases(keys)
            logger.warning(
                "Could not tell the workers to release %d held object(s): %s. Queued to go with the next "
                "release or at teardown.",
                len(keys),
                e,
            )

    async def broadcast_pending_local_object_releases(self) -> None:
        """Tell every worker about keys released here that it may also be holding.

        Drains the queue the sync release paths fill -- a handle parameter's value being replaced, a node
        being deleted -- so those paths do not each need a loop of their own. Never raises, for the same
        reason as its sibling below: a send failure must not break whatever triggered the release.

        On failure the keys are discarded, not re-queued: this runs on the teardown path, the drop-all
        that follows covers them, and a re-queue would cycle keys from a deleted workflow forever. The
        scheduled sibling `_send_local_object_releases` re-queues, because nothing follows it.

        Lazy import breaks the same cycle as its siblings: `app.worker_routing` imports `EventManager` from
        this package.
        """
        from griptape_nodes.app.worker_routing import DropLocalObjectsRequest

        keys = self.engine.resource_manager.drain_pending_worker_releases()
        if not keys or self._transport is None or not self._workers:
            return
        try:
            await self.broadcast_to_workers(EventRequest(request=DropLocalObjectsRequest(keys=keys)))
        except Exception as e:
            logger.warning(
                "Could not tell the workers to release %d held object(s): %s. A worker that did not get the "
                "message keeps them until a later teardown.",
                len(keys),
                e,
            )

    async def broadcast_local_object_teardown(self) -> None:
        """Tell every worker to release named pending keys, then everything its libraries hold.

        One method because both halves are required: the named-key broadcast is what drains the
        orchestrator's pending-release queue, and the drop-all is what covers whatever a worker still holds.
        Two teardown sites call this; neither may take half of it.
        """
        await self.broadcast_pending_local_object_releases()
        await self.broadcast_drop_all_local_objects()

    async def broadcast_drop_all_local_objects(self) -> None:
        """Tell every worker to release the objects its libraries parked in it.

        Awaited rather than scheduled, for the reason recorded on `_on_config_changed` above: this one is
        called from a workflow teardown that can be dispatched synchronously, and a task created on a
        transient side loop dies with that loop.

        Never raises. Its callers have already destroyed nodes and flows, one with a registry delete
        still to come, so a send failure must not abort them or displace the failure they were already
        reporting.

        Lazy import breaks the same cycle as its siblings: `app.worker_routing` imports `EventManager`
        from this package.
        """
        from griptape_nodes.app.worker_routing import DropAllLocalObjectsRequest

        if self._transport is None or not self._workers:
            return
        try:
            await self.broadcast_to_workers(EventRequest(request=DropAllLocalObjectsRequest()))
        except Exception as e:
            logger.warning(
                "Could not tell the workers to release the objects held for their libraries: %s. "
                "A worker that did not get the message keeps them until a later teardown.",
                e,
            )

    async def _on_secret_changed(self, _event: SecretChanged) -> None:
        """Fan out a RefreshSecretsRequest after the orchestrator's secret mutation succeeded.

        SecretsManager raises if the .env write fails, so reaching the
        event broadcast means disk is up to date. Workers re-read the
        shared file via ``refresh_from_env_file``. Awaited inline for
        the same side-loop reason documented on ``_on_config_changed``;
        lazy import for the same circular-dependency reason.
        """
        from griptape_nodes.app.worker_routing import RefreshSecretsRequest

        if self._transport is None or not self._workers:
            return
        await self.broadcast_to_workers(EventRequest(request=RefreshSecretsRequest()))

    async def _on_current_project_changed(self, _event: CurrentProjectChanged) -> None:
        """Fan out an ActivateProjectRequest after the orchestrator switched projects.

        Reads the committed pair rather than taking the id off the event, so the id and the
        generation describing it come from one read. Two switches in quick succession therefore
        both fan out the newest committed state, and a worker cannot be told to go backwards.
        Awaited inline for the same side-loop reason documented on ``_on_config_changed``; lazy
        import for the same circular-dependency reason.
        """
        from griptape_nodes.app.worker_routing import ActivateProjectRequest

        if self._transport is None or not self._workers:
            return
        project_id, generation = self.engine.project_manager.committed_project()
        failures = await self.broadcast_to_workers_awaiting_replies(
            EventRequest(request=ActivateProjectRequest(project_id=project_id, generation=generation))
        )
        # A worker left on the old project resolves workspace-relative paths against the old
        # workspace, so it writes where this engine does not read. Loud here beats silent there.
        for failure in failures:
            logger.error(
                "Worker did not adopt project '%s' after the switch; its file paths will not match "
                "this engine's. Details: %s",
                project_id,
                failure,
            )

    async def broadcast_to_workers_awaiting_replies(self, event: EventRequest) -> list[str]:
        """Fan out to every worker and WAIT for each to answer. Returns the failures, named.

        The fire-and-forget variant is wrong for anything that changes where paths resolve. A
        project switch moves the workspace, and `broadcast_to_workers` returns as soon as the
        messages are sent -- so `SetCurrentProjectRequest` reports success while a worker may still
        be on the old workspace. Execution dispatched in that window writes files where nothing
        looks, with no error. Awaiting closes the window by construction instead of hoping the
        fan-out wins the race against the next dispatch.

        Concurrent, and bounded per worker. Serially, a worker that is merely SLOW -- adoption runs a
        full library reload, which can include installs -- kept every worker behind it from even
        receiving the request, so the caller waited out the sum rather than the slowest. And
        `route_to_worker` has no ceiling of its own: it argues liveness from the heartbeat, which
        evicts a SILENT worker and says nothing about a busy one, so a user-facing switch could wait
        forever. The bound is the startup grace, the same budget a worker's own slow startup gets.

        Each worker gets its own request id so replies cannot be confused. One worker's failure does
        not affect the others; the caller decides what a failure means.
        """
        if not self._workers:
            return []

        async def ask(worker_engine_id: str, request_topic: str) -> str | None:
            per_worker = EventRequest(request=event.request)
            per_worker.request_id = str(uuid.uuid4())
            try:
                raw = await asyncio.wait_for(
                    self.route_to_worker(per_worker, worker_engine_id, request_topic),
                    timeout=self.library_load_timeout_s,
                )
            except TimeoutError:
                return f"{worker_engine_id}: no reply within {self.library_load_timeout_s:g} seconds"
            except Exception as e:
                return f"{worker_engine_id}: {type(e).__name__}: {e}"
            # endswith, not a substring test: that would read any type merely CONTAINING "Success".
            if not str(raw.get("result_type", "")).endswith("ResultSuccess"):
                result = raw.get("result")
                details = result.get("result_details", raw) if isinstance(result, dict) else raw
                return f"{worker_engine_id}: {details}"
            return None

        outcomes = await asyncio.gather(
            *(ask(wid, registration.request_topic) for wid, registration in list(self._workers.items()))
        )
        return [failure for failure in outcomes if failure is not None]

    def schedule_broadcast(self, request_type: type[RequestPayload]) -> None:
        """Tell every registered worker to handle ``request_type`` locally.

        Wraps ``request_type`` in an EventRequest and fans it out to every
        registered worker as a fire-and-forget background task on the
        caller's running event loop. On a worker process (no registered
        workers, or no transport configured) this is a cheap no-op.

        Must be called from inside a running event loop. Every production
        caller reaches this through EventManager.handle_request /
        ahandle_request, which itself runs inside the event loop that the
        launching application drives the engine on.
        """
        if self._transport is None or not self._workers:
            return
        event = EventRequest(request=request_type())
        task = asyncio.create_task(self.broadcast_to_workers(event))
        self._inflight_broadcast_tasks.add(task)
        task.add_done_callback(self._inflight_broadcast_tasks.discard)

    async def broadcast_to_workers(self, event: EventRequest) -> None:
        """Fire-and-forget fan out of an EventRequest to every registered worker.

        Used for orchestrator-originated notifications that every worker must
        act on locally (e.g. reload config, refresh secrets). The request is
        sent to each worker's dedicated request topic; no response is awaited.

        Safe to call with zero registered workers -- it is a no-op. An event that cannot be
        serialized is logged and sent to no worker.
        """
        if not self._workers:
            return
        try:
            for wid, registration in list(self._workers.items()):
                await self.forward_event_to_worker(
                    event,
                    worker_engine_id=wid,
                    worker_request_topic=registration.request_topic,
                )
        except EventSerializationError:
            logger.exception("Could not broadcast %s to workers", type(event.request).__name__)

    async def relay_worker_result(self, payload: dict) -> None:
        """Relay an unmatched worker result to the GUI session response topic.

        Called for worker result messages not claimed by RequestClient
        (heartbeats and any results without a pending request).
        The orchestrator always mediates between workers and the GUI; workers never
        publish directly to the session response topic.
        """
        # Heartbeat responses update the last-seen timestamp but are not forwarded to the GUI.
        result_event_type = payload.get("result_type", "")
        if result_event_type == worker_events.WorkerHeartbeatResultSuccess.__name__:
            response_topic = payload.get("response_topic", "")
            m = self._WORKER_RESPONSE_TOPIC_RE.match(response_topic)
            if m is None:
                logger.warning(
                    "Heartbeat reply arrived on '%s', which names no worker, so no worker was marked alive.",
                    response_topic,
                )
                return
            worker_engine_id = m.group("worker_engine_id")
            registration = self._workers.get(worker_engine_id)
            if registration is not None:
                registration.unanswered_challenges = 0
            logger.debug("Heartbeat received from worker %s", worker_engine_id)
            return  # Internal health check — do not forward to GUI

        # 1 engine = 1 session — the orchestrator's session response topic is always the right target.
        session_response_topic = self._determine_response_topic()
        dest_socket = "success_result" if payload.get("event_type") == "EventResultSuccess" else "failure_result"
        payload["response_topic"] = session_response_topic
        logger.debug("Relaying %s to %s", payload.get("event_type"), session_response_topic)
        # Untags the JSON rather than reading the result back, since its payload class may live in a
        # library only the worker has loaded. Only values carry tags, but a plain field holding its own
        # "$type" key would lose it here.
        await self._tx.send_message(dest_socket, json.dumps(untag(payload)), session_response_topic)

    def _determine_response_topic(self) -> str:
        """Determine the response topic based on current session and engine IDs."""
        session_id = self.engine.get_session_id()
        if session_id:
            return f"sessions/{session_id}/response"
        engine_id = self.engine.get_engine_id()
        if engine_id:
            return f"engines/{engine_id}/response"
        return "response"
