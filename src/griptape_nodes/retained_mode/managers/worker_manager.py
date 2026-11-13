from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

import anyio

from griptape_nodes.drivers.cloud_credentials import API_KEY_SECRET_NAME, LICENSE_SECRET_NAME
from griptape_nodes.drivers.storage.local_storage_driver import LocalStorageDriver
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.engine_transports import (
    DISTRIBUTED_EXECUTION_SCHEMA_VERSION,
    EngineNotFoundError,
)
from griptape_nodes.retained_mode.events import worker_events
from griptape_nodes.retained_mode.events.app_events import (
    ConfigChanged,
    CurrentProjectChanged,
)
from griptape_nodes.retained_mode.events.base_events import EventRequest
from griptape_nodes.retained_mode.events.converters import EventSerializationError
from griptape_nodes.retained_mode.events.execution_events import ExecuteNodeRequest
from griptape_nodes.retained_mode.events.generic_events import GenericResultFailure
from griptape_nodes.retained_mode.events.secrets_events import (
    GetSecretValueRequest,
)
from griptape_nodes.retained_mode.managers.event_manager import RemoteWorkerRequest, require_request_sender
from griptape_nodes.retained_mode.managers.external_environment import (
    WorkerCommand,
    WorkerCommandRefusal,
    provisioned_by_environment,
    read_worker_command_prefix,
    resolve_worker_command,
    worker_requests_from_environment,
)
from griptape_nodes.retained_mode.managers.library.common import LibraryLifecycleState
from griptape_nodes.retained_mode.managers.run_queue import RunSpec
from griptape_nodes.retained_mode.managers.secrets_manager import SecretsManager
from griptape_nodes.retained_mode.managers.settings import (
    DISTRIBUTED_EXECUTION_LIBRARY_ENGINES_KEY,
    DISTRIBUTED_EXECUTION_MAX_WORKERS_KEY,
    DISTRIBUTED_EXECUTION_RELEASE_HOW_KEY,
    DISTRIBUTED_EXECUTION_RELEASE_WHICH_KEY,
    WORKER_LIBRARY_LOAD_TIMEOUT_KEY,
    ReleaseSettings,
)
from griptape_nodes.retained_mode.request_handlers import RequiredSender, handles
from griptape_nodes.retained_mode.runtime.binding import create_task_bound_to_no_workflow
from griptape_nodes.retained_mode.runtime.flows import WorkflowFlows
from griptape_nodes.retained_mode.topics import (
    REQUEST_TOPIC,
    default_response_topic,
    engine_request_topic,
    session_request_topic,
)
from griptape_nodes.serialization.values import untag
from griptape_nodes.servers.static import ORCHESTRATOR_STATIC_SERVER_BASE_URL_ENV
from griptape_nodes.utils.version_utils import engine_version
from griptape_nodes.worker.channel import ChannelClosedError, listen_for_worker
from griptape_nodes.worker.parent import ParentEnd, apply_worker_notifications, decide_worker_checkpoint
from griptape_nodes.worker.remote import RemoteWorkerEnd
from griptape_nodes.worker.wire import (
    AUTHORIZATION_CHECKPOINT,
    EVENT_RESULT,
    WORKER_ENGINE_ID_ENV,
    WireFormatError,
    event_request_envelope,
    read_event_request,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import RequestPayload, RequestSender, ResultPayload
    from griptape_nodes.retained_mode.events.worker_events import LibraryLoadReport
    from griptape_nodes.retained_mode.managers.event_manager import EventManager, ResultContext
    from griptape_nodes.retained_mode.managers.run_queue import Run
    from griptape_nodes.worker.channel import AsyncChannel, PendingWorker
    from griptape_nodes.worker.wire import WorkerHello

logger = logging.getLogger("griptape_nodes_app")

# How long a spawn waits for initialization to decide the static server URL. Resolution is a socket
# bind, so seconds -- not the startup grace, whose 10 minutes is sized for dependency installs. On a
# restart into an existing session it resolves in the same event fan-out that triggers the spawn; on
# a fresh boot the spawn comes later, off AppSessionStartedEvent, by which point it is long settled.
_STATIC_URL_SETTLE_TIMEOUT_S = 30.0

# How long an engine waits for another engine to answer a request. Starting a worker
# answers once its process exists, so this bounds an engine that is down or unreachable.
ENGINE_REQUEST_TIMEOUT_S = 60.0

# How many times an engine tries to release a lease on another engine, and how long it pauses between
# tries. That engine holds itself for the lease until it hears, or until it loses its connection to this one.
_REPORT_ATTEMPTS = 5
_REPORT_RETRY_PAUSE_S = 10.0

# Never in a worker's environment: the worker is given an identity of its own.
_LOCAL_WORKER_WITHHELD_ENV = ("GTN_ORCHESTRATOR_ENGINE_ID",)
# Present but empty in a worker's environment, as in an engine with no credential: a worker gets its
# credential from `resolve_cloud_credential`, and griptape's Cloud drivers raise KeyError when these are unset.
_WORKER_EMPTIED_ENV = (LICENSE_SECRET_NAME, API_KEY_SECRET_NAME)

# Everything but a node that an engine sends a worker another engine runs for it, by request type name,
# since some are defined in a module that imports this one. The engine that runs the worker passes on
# these alone, since only a node carries the lease that admits it. A worker reads its configuration on
# the machine it runs on, and asks for each secret as it needs it, so neither reload is sent.
_SENT_TO_REMOTE_WORKERS = frozenset(
    {
        "ActivateProjectRequest",
        "DropLocalObjectsRequest",
        "DropAllLocalObjectsRequest",
        "DropLibraryNamedLocalObjectsRequest",
        "CancelExecuteNodeRequest",
    }
)

# What an engine sends a worker another engine runs for it under a lease: the node to run, alone.
_RUN_ON_REMOTE_WORKERS = frozenset({ExecuteNodeRequest.__name__})

# Why a node's local worker went away, when nothing that stopped it said otherwise.
_LOCAL_WORKER_STOPPED = "the worker process running it stopped."


@dataclass
class _SessionRegistration:
    """What an engine knows about an artist's session on another engine it runs workers for."""

    engine_id: str
    static_server_base_url: str | None
    # The version of each library the session declared, checked against this engine's again at every
    # start, since this engine's libraries can change after the session registered.
    library_versions: dict[str, str]


@dataclass(frozen=True)
class _WorkerUnavailable:
    """Why no worker will run a library."""

    reason: str
    # The library's next node asks for its worker again, as after another engine refused it for capacity.
    retryable: bool = False


@dataclass
class _StartAskedOfHost:
    """A worker the artist's engine asked another engine to start, until it registers, fails, or is withdrawn."""

    host_engine_id: str
    library_name: str


@dataclass
class _ServedFor:
    """What makes a worker this engine started one it runs for an artist's session on another engine."""

    owning_engine_id: str
    session_id: str
    library_name: str
    # Set once the start has spawned the process or given up, which a stop sent meanwhile waits for.
    started: asyncio.Event = field(default_factory=asyncio.Event)
    # Reports the process's exit to the artist's engine, done once that engine has answered.
    exit_watcher: asyncio.Task | None = None
    # The end that carries the worker's channel to the artist's engine, once it connects.
    end: RemoteWorkerEnd | None = None
    # Whether it ended on its own, as when its channel closed or it never connected, rather than being stopped.
    ended_on_its_own: bool = False
    # Why this engine refused its hello, which its exit report carries to the artist's engine.
    refusal: str | None = None


@dataclass
class _EngineRun:
    """Another engine's lease on this engine, waiting in the line of leases or holding this engine."""

    engine_id: str
    lease_id: str
    # The lease's run on that engine, or None for a node run on its own.
    run_id: str | None
    # Resolved with None once the lease holds this engine, or with why it never will.
    granted: asyncio.Future[str | None]
    # Set when the lease is released.
    released: asyncio.Event = field(default_factory=asyncio.Event)
    # The run that stands for it in the line of leases, set once it is submitted.
    line_run: Run | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.engine_id, self.lease_id)

    @property
    def holds_engine(self) -> bool:
        """Whether the lease holds this engine now: granted, and not yet released."""
        return self.granted.done() and self.granted.result() is None and not self.released.is_set()


@dataclass
class _EngineRunSpec(RunSpec):
    """A lease of another engine's, which holds this engine from when it starts until that engine releases it."""

    manager: WorkerManager
    engine_run: _EngineRun
    flow_name: str

    async def execute(self, engine: Engine) -> ResultPayload:  # noqa: ARG002
        return await self.manager.hold_for_engine_run(self.engine_run)

    def failure(self, details: str) -> ResultPayload:
        return worker_events.AcquireLeaseResultFailure(result_details=details)

    def on_place_in_line(self, run: Run) -> None:
        self.manager.report_place_in_line(self.engine_run, run.position)

    def on_turned_away(self, details: str) -> None:
        self.manager.refuse_engine_run(self.engine_run, details)


@dataclass
class EngineLease:
    """A lease this engine holds, or waits for, on the engine that runs a library's worker, until it is released."""

    lease_id: str
    host_engine_id: str
    # The run on this engine the lease is for, or None for a node run on its own.
    run_id: str | None
    # The request acquiring it, done once that engine has answered.
    request_task: asyncio.Task[ResultPayload] | None = None


@dataclass
class WorkerRegistration:
    """Tracks a registered worker and its optional library key.

    worker_key is the library_name the worker was spawned for, or None for
    general-purpose workers. A worker this engine runs for another engine's
    session is keyed by that session as well, as "<session_id>/<library_name>".
    """

    worker_key: str | None
    # Engine ID of the engine that runs this worker, or None for a local worker.
    host_engine_id: str | None = None
    # The private channel to a worker this engine started, and its process. None for a worker another
    # engine runs for this one. A served worker's process is None until it exists: stopping compares it
    # to the managed process for the key, which a newer start for the same library may already have replaced.
    channel: AsyncChannel | None = None
    process: asyncio.subprocess.Process | None = None
    # Set for a worker this engine runs for another engine's session, and None for one that works for
    # this engine. Never set together with `host_engine_id`.
    serves: _ServedFor | None = None
    # Why this engine is done with the worker, once it has decided. What was waiting on it fails with this.
    gone_reason: str | None = None
    # Set with `gone_reason`. A request to a worker another engine runs waits on that engine's answer,
    # which nothing here ends, so it watches this instead.
    gone: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _WorkerTransport:
    """Transport-layer dependencies for WorkerManager.

    Held separately from WorkerManager so the manager can be constructed up
    front (e.g. by the Engine) and wired to a concrete transport later, once
    the WebSocket client exists.
    """

    # Sends this engine's clients, such as the editor, what a worker would have sent them.
    send_message: Callable[[str, str, str | None], Awaitable[None]]


class WorkerManager(EngineScoped):
    """Manages the worker processes an engine runs, for itself and for other engines, and routing to them.

    Each worker is this engine's child process on a private channel. A worker another engine runs for
    this one is reached through that engine, and one this engine runs for another engine has its
    traffic carried to that engine.

    Sending to this engine's clients is injected as a callable, so this class
    has no direct dependency on WebSocket plumbing.
    """

    # How long a worker may take to load its library (venv creation, installs, imports): the ceiling
    # on `wait_until_executable` and on each worker's reply to a project-switch fan-out.
    DEFAULT_LIBRARY_LOAD_TIMEOUT_S: float = 600.0
    # How long to wait for a worker to exit after SIGTERM before escalating to
    # SIGKILL. A worker handles no SIGTERM, so it normally ends at once; a process the
    # signal does not end is killed.
    DEFAULT_TERMINATE_GRACE_S: float = 10.0

    # Ceiling on awaiting a cross-loop termination hopped onto the spawning loop.
    # The hopped coroutine itself can take up to DEFAULT_TERMINATE_GRACE_S, so this
    # must exceed it; the extra margin covers the SIGKILL escalation and reap. It
    # bounds the case where the spawning loop closes after the hop is scheduled but
    # before it completes, so the evicting loop never blocks forever on a future
    # that will never resolve.
    DEFAULT_TERMINATE_HOP_TIMEOUT_S: float = 15.0

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
        # from sending the start until that engine reports the worker ready, the start fails, or it
        # is withdrawn.
        self._starts_asked_of_hosts: dict[str, _StartAskedOfHost] = {}

        # This engine's registration with each engine it sends work to, by (host_engine_id, session).
        self._engine_registrations: dict[tuple[str, str], asyncio.Task[ResultPayload]] = {}

        # Artists' sessions on other engines registered here, by session id.
        self._sessions: dict[str, _SessionRegistration] = {}

        # Requests telling an engine to stop a worker whose start was withdrawn, held so they are
        # not garbage-collected while they wait for the answer.
        self._stop_requests: set[asyncio.Task] = set()

        # Other engines' leases on this engine, waiting in its line or holding it, by (engine, lease).
        self._engine_runs: dict[tuple[str, str], _EngineRun] = {}
        # The line other engines' runs wait in here. It belongs to no workflow: an engine that runs nodes
        # for other engines need not have one open, and its machine is shared whichever is.
        self._lease_line = WorkflowFlows()

        # The engine whose lease last held this engine, so the next lease from another engine frees its workers.
        self._last_engine_run_engine_id: str | None = None

        # Leases this engine holds or waits for on other engines, by lease ID, until each is released.
        self._leases: dict[str, EngineLease] = {}

        # The lease each of this engine's runs has on each engine, by (run, engine), shared by the run's nodes.
        self._run_leases: dict[tuple[str, str], EngineLease] = {}

        # Reports and lease releases sent to other engines from synchronous callbacks, held until answered.
        self._engine_notices: set[asyncio.Task] = set()

        # Every worker by worker engine ID: this engine's own, the ones other engines run for it, and
        # the ones it runs for artists' sessions on other engines. `_own_workers` and
        # `_workers_served_for` tell them apart.
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
        self._worker_unavailable: dict[str | None, _WorkerUnavailable] = {}

        config = engine.config_manager
        self.library_load_timeout_s: float = config.get_config_value(
            WORKER_LIBRARY_LOAD_TIMEOUT_KEY,
            default=WorkerManager.DEFAULT_LIBRARY_LOAD_TIMEOUT_S,
            cast_type=float,
        )

        event_manager.register_request_handlers(self)
        event_manager.recognize_sessions(self.has_registered_session)
        event_manager.admit_remote_worker_requests(self.admit_remote_worker_request)

        # Subscribe to domain events from ConfigManager / ProjectManager so
        # those managers don't have to know workers exist. The listeners are
        # the single place that translates a "something changed" signal into
        # a worker fan-out.
        event_manager.add_listener_to_app_event(ConfigChanged, self._on_config_changed)
        event_manager.add_listener_to_app_event(CurrentProjectChanged, self._on_current_project_changed)

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
        ws_outgoing_queue: asyncio.Queue | None = None,  # noqa: ARG002
    ) -> None:
        """Bind the transport-layer callable used to reach this engine's clients.

        Called once the WebSocket client exists. Until this is called, methods
        that depend on the transport will raise RuntimeError.

        `ws_outgoing_queue` is ignored. Accepted so app releases that still pass it keep working.
        TODO(https://github.com/griptape-ai/griptape-nodes-app/issues/254): remove.
        """
        self._transport = _WorkerTransport(send_message=send_message)

    async def _activate_project_on_worker(self, worker_engine_id: str) -> None:
        """Have one worker adopt the project this engine is on, which it asks for.

        Sent without awaiting a reply: registration must not depend on a round trip back into the
        worker, which would make answering it hostage to a worker that is merely slow. The worker
        blocks on having applied an activation before it loads a library instead, which is a wait it
        can bound locally.
        """
        from griptape_nodes.app.worker_routing import ActivateProjectRequest

        await self.forward_event_to_worker(
            EventRequest(request=ActivateProjectRequest()), worker_engine_id=worker_engine_id
        )

    @handles(worker_events.GetCommittedProjectRequest)
    def handle_get_committed_project_request(
        self,
        request: worker_events.GetCommittedProjectRequest,  # noqa: ARG002
    ) -> worker_events.GetCommittedProjectResultSuccess:
        """Answer a worker asking which project to be on."""
        project_id = self.engine.project_manager.committed_project_id
        return worker_events.GetCommittedProjectResultSuccess(
            project_id=project_id, result_details=f"This engine is on project '{project_id}'."
        )

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
        if any(start.library_name == worker_key for start in self._starts_asked_of_hosts.values()):
            return True
        return self.get_worker_for_key(worker_key) is not None

    def get_worker_for_key(self, key: str | None) -> str | None:
        """Return the engine ID of a worker registered under key, or None.

        A key of None finds a general-purpose worker, one not tied to any library.

        Today returns the first registered worker for the key. Future versions can
        load-balance across multiple workers for the same key.
        """
        for wid, registration in self._own_workers().items():
            if registration.worker_key == key:
                return wid
        return None

    def _own_workers(self) -> dict[str, WorkerRegistration]:
        """The workers that work for this engine, by worker engine ID: all but those it runs for other engines."""
        return {wid: registration for wid, registration in self._workers.items() if registration.serves is None}

    def _own_worker(self, worker_engine_id: str) -> WorkerRegistration | None:
        """The worker that works for this engine under `worker_engine_id`, or None if there is none."""
        registration = self._workers.get(worker_engine_id)
        if registration is None or registration.serves is not None:
            return None
        return registration

    def _workers_served_for(self, owning_engine_id: str | None = None) -> dict[str, WorkerRegistration]:
        """The workers this engine runs for `owning_engine_id`, or for every other engine when None, by worker engine ID."""
        return {
            wid: registration
            for wid, registration in self._workers.items()
            if registration.serves is not None
            and (owning_engine_id is None or registration.serves.owning_engine_id == owning_engine_id)
        }

    def _served_worker(self, worker_engine_id: str) -> WorkerRegistration | None:
        """The worker this engine runs for another engine under `worker_engine_id`, or None if there is none."""
        registration = self._workers.get(worker_engine_id)
        if registration is None or registration.serves is None:
            return None
        return registration

    async def _spawn_local_worker(self, args: list[str], worker_key: str | None) -> None:
        """Start a worker this engine runs for itself, as its child on a private channel, and take it on.

        The worker is told how to reach this engine and the engine ID minted for this spawn, which
        names it and is not an address. It holds no credentials in its environment: it asks this
        engine for every secret, including the license, and for every authorization checkpoint.
        """
        worker_id = str(uuid.uuid4())
        pending = await listen_for_worker()

        async def environ() -> dict[str, str]:
            worker_environ = await self._base_worker_environ(worker_key, worker_key)
            for withheld in _LOCAL_WORKER_WITHHELD_ENV:
                worker_environ.pop(withheld, None)
            for emptied in _WORKER_EMPTIED_ENV:
                worker_environ[emptied] = ""
            worker_environ[WORKER_ENGINE_ID_ENV] = worker_id
            worker_environ.update(pending.child_environment)
            return worker_environ

        try:
            proc = await self._start_worker_process(args, worker_key, environ)
        except BaseException:
            pending.close()
            raise
        if proc is None:
            pending.close()
            return
        channel = await self._connect_local_worker(worker_key, proc, pending)
        if channel is not None:
            await self._take_on_local_worker(worker_id, worker_key, proc, channel)

    async def _connect_local_worker(
        self, worker_key: str | None, proc: asyncio.subprocess.Process, pending: PendingWorker
    ) -> AsyncChannel | None:
        """Wait for a just-started local worker to connect, or settle why it never will. None when it did not."""
        try:
            channel = await self._await_worker_connection(proc, pending)
        except TimeoutError:
            logger.error(
                "Worker for key '%s' (pid %s) did not connect within %.0fs; stopping it.",
                worker_key,
                proc.pid,
                self.library_load_timeout_s,
            )
            if self._managed_worker_processes.get(worker_key) is proc:
                del self._managed_worker_processes[worker_key]
                await self._terminate_managed_process(worker_key, proc)
                self.note_worker_unavailable(
                    worker_key,
                    f"its worker process did not connect to this engine within {self.library_load_timeout_s:.0f} seconds.",
                )
            return None
        if channel is None:
            self._note_early_exit(worker_key, proc, proc.returncode)
            return None
        return channel

    async def _await_worker_connection(
        self, proc: asyncio.subprocess.Process, pending: PendingWorker
    ) -> AsyncChannel | None:
        """Wait for a just-started worker to connect. None when its process exited first.

        Every way out stops listening: `accept()` does so itself, including when it is cancelled.

        Raises:
            TimeoutError: It did not connect within the library load timeout.
        """
        accepting = asyncio.ensure_future(pending.accept(self.library_load_timeout_s))
        exiting = asyncio.ensure_future(proc.wait())
        try:
            done, _ = await asyncio.wait({accepting, exiting}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            exiting.cancel()
            if not accepting.done():
                accepting.cancel()
        if accepting not in done:
            return None
        return accepting.result()

    async def _take_on_local_worker(
        self, worker_id: str, worker_key: str | None, proc: asyncio.subprocess.Process, channel: AsyncChannel
    ) -> None:
        """Register a connected local worker under the engine ID it was started with, and start answering it."""
        # Stopped while it was connecting: whatever stopped it already recorded why.
        if self._managed_worker_processes.get(worker_key) is not proc:
            channel.close()
            return
        self._workers[worker_id] = WorkerRegistration(worker_key=worker_key, channel=channel, process=proc)
        parent_end = ParentEnd(
            self.engine,
            on_hello=functools.partial(self._accept_hello, worker_id),
            on_closed=functools.partial(self._start_forget_local_worker, worker_id),
            relay_to_editor=self._relay_worker_event,
        )
        parent_end.attach(channel)
        logger.debug("Worker for key '%s' (pid %s) connected as %s", worker_key, proc.pid, worker_id)
        await self._activate_project_on_worker(worker_id)

    async def _accept_hello(self, worker_id: str, hello: WorkerHello) -> dict[str, Any]:
        """Take a local worker into service once it has loaded what it serves, or refuse it with why.

        Each library it reports loading is noted as a per-library load report would be, including the
        libraries the one it serves depends on: a node of one of those may be waiting on its gate.
        """
        registration = self._own_worker(worker_id)
        if registration is None:
            return {"accepted": False, "details": "This engine has already stopped this worker."}
        worker_key = registration.worker_key
        refusal = self._hello_refusal(worker_key, hello)
        if refusal is not None:
            logger.error("Refused the worker for key '%s': %s", worker_key, refusal)
            registration.gone_reason = refusal
            self.note_worker_unavailable(worker_key, refusal)
            return {
                "accepted": False,
                "details": f"Attempted to start {_worker_described(worker_key)}. Failed because {refusal}",
            }
        await self._note_libraries_loaded(worker_key, hello.libraries)
        if worker_key is None:
            self.note_library_loaded(None)
        return {"accepted": True, "details": f"Started {_worker_described(worker_key)}."}

    async def _note_libraries_loaded(self, worker_key: str | None, libraries: list[LibraryLoadReport]) -> None:
        """Note each library a worker reports loading, as a per-library load report would be."""
        for library in libraries:
            report = await self.engine.library_manager.workers.on_report_library_loaded_request(library)
            if report.failed():
                logger.warning(
                    "Worker for key '%s' reported loading library '%s': %s",
                    worker_key,
                    library.library_name,
                    report.result_details,
                )

    @staticmethod
    def _hello_refusal(worker_key: str | None, hello: WorkerHello) -> str | None:
        """Why a local worker's hello is refused, phrased to follow "Failed because", or None to accept it."""
        if hello.engine_version != engine_version:
            return (
                f"its worker process runs griptape-nodes-engine {hello.engine_version}, and this engine runs "
                f"{engine_version}. Install griptape-nodes-engine {engine_version} in the environment the worker "
                "starts in."
            )
        expected = [worker_key] if worker_key is not None else []
        if hello.library_names != expected:
            served = ", ".join(f"'{name}'" for name in hello.library_names) or "every library"
            return f"its worker process reported serving {served} instead."
        if not hello.loaded:
            return f"its worker process could not load it: {hello.failure_reason}"
        return None

    def _start_forget_local_worker(self, worker_id: str) -> None:
        """As a local worker's channel ends: forget the worker."""
        task = create_task_bound_to_no_workflow(asyncio.get_running_loop(), self._forget_local_worker(worker_id))
        self._exit_watchers.add(task)
        task.add_done_callback(self._exit_watchers.discard)

    async def _forget_local_worker(self, worker_id: str) -> None:
        """A local worker's channel ended: it is gone, so stop its process and release what waits on it."""
        registration = self._own_worker(worker_id)
        # Stopped by this engine, which recorded why.
        if registration is None:
            return
        del self._workers[worker_id]
        worker_key = registration.worker_key
        proc = registration.process
        returncode = None
        if proc is not None and self._managed_worker_processes.get(worker_key) is proc:
            del self._managed_worker_processes[worker_key]
            await self._terminate_managed_process(worker_key, proc)
            returncode = proc.returncode
        reason = registration.gone_reason
        if reason is None:
            reason = f"the worker process running it stopped (exit code {returncode}). The engine log has the details."
            logger.error(
                "Worker for key '%s' stopped (exit code %s). Its own log lines above say why.", worker_key, returncode
            )
        self.note_worker_unavailable(worker_key, reason)

    async def _relay_worker_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Send on, unacted on, what a worker would have sent the editor, when there is an editor to send it to."""
        if self._transport is None:
            return
        if event_type == EVENT_RESULT:
            await self.relay_worker_result(payload)
            return
        # Untagged like a relayed result: the editor reads values without type tags.
        await self._tx.send_message(
            "app_event",
            json.dumps(untag(payload)),
            default_response_topic(self.engine.get_engine_id(), self.engine.get_session_id()),
        )

    async def _start_worker_process(
        self,
        args: list[str],
        worker_key: str | None,
        environ: Callable[[], Awaitable[dict[str, str]]],
    ) -> asyncio.subprocess.Process | None:
        """Start a worker's process under a claim on `worker_key`, or None when one is already starting or running."""
        if worker_key in self._managed_worker_processes or worker_key in self._spawns_in_flight:
            logger.error("Worker for key '%s' already spawned; refusing duplicate spawn.", worker_key)
            return None
        # Claimed in the same step as the check above, so no await separates them. The registry
        # entry cannot serve as this guard: it is written only once the subprocess exists, and the
        # work in between suspends. A second fork for one library leaves one of the two processes
        # untracked, holding that library's dependencies until this engine stops.
        claim = asyncio.Event()
        self._spawns_in_flight[worker_key] = claim
        try:
            worker_environ = await environ()
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
        finally:
            # Released even when the fork raises, or the claim would silently refuse every later
            # attempt for this library -- but only while this attempt still holds it. A reset drops
            # the claims so a reload can spawn again, so a spawn suspended across one resumes to
            # find the key belonging to the reload's spawn, and freeing that admits a third fork.
            if self._spawns_in_flight.get(worker_key) is claim:
                del self._spawns_in_flight[worker_key]
            claim.set()
        logger.debug("Spawned worker for key '%s' (pid %s)", worker_key, proc.pid)
        return proc

    async def _base_worker_environ(self, worker_key: str | None, library_name: str | None) -> dict[str, str]:
        """The environment every worker starts with, before what its kind adds or withholds."""
        # The PRE-project environ, not the live one: baking in the orchestrator's current-project
        # vars would leave the worker unable to unset them on a later project switch. Nothing is
        # forwarded by pattern; a configured worker.command_prefix owns what to do with anything
        # already present, and the engine adds only the variables named below.
        worker_environ = dict(self.engine.project_manager.get_pre_project_environ())
        # Unbuffered output keeps worker log lines from stalling in Python's block buffer and
        # being lost on a crash when stdout is a pipe (e.g. under the desktop app).
        worker_environ["PYTHONUNBUFFERED"] = "1"

        # PYTHONPATH precedes site-packages, not a later sys.path splice: sys.modules never
        # reconsiders a module this process already imported. A general-purpose worker serves no
        # single library, so it imports from the engine's own environment, as the orchestrator does.
        execution_site_packages = None
        environment_library = library_name or worker_key
        if environment_library is not None:
            execution_site_packages = self.engine.library_manager.environment.execution_site_packages(
                environment_library
            )
        if execution_site_packages is not None:
            # Prepended, not assigned: a launcher-set PYTHONPATH (embedding hosts, source checkouts)
            # is part of the environment the engine booted with, and must not be lost just for workers
            # with a library's execution environment.
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
        # handed one could never follow its orchestrator onto a project's workspace again.

        # The orchestrator's long-lived static server must be the one to serve the shared workspace:
        # a worker serving its own wins an arbitrary port, and a saved workflow's asset URLs on it
        # are dead once the workflow is reopened.
        static_base_url = await self._orchestrator_static_server_base_url()
        if static_base_url is not None:
            worker_environ[ORCHESTRATOR_STATIC_SERVER_BASE_URL_ENV] = static_base_url
        return worker_environ

    def _note_early_exit(
        self, worker_key: str | None, proc: asyncio.subprocess.Process, returncode: int | None
    ) -> None:
        """Record that `proc` exited before it finished loading, unless this manager stopped it or it had loaded."""
        # A process this manager stopped itself was dropped from the records first, and whoever
        # stopped it records the reason.
        if self._managed_worker_processes.get(worker_key) is not proc:
            return
        # Loaded, then exited: its channel ending forgets it and says so.
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
        """Terminate all managed worker processes and clear their state.

        Used both on orchestrator shutdown and before a library reload: freshly
        spawned workers must start with a clean slate and no stale entries in the
        routing tables. Best-effort: already-exited processes are logged and skipped.
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

        Anything awaiting one of them is failed with the reason.
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
        self._release_workers(library_names=library_names, action=action)
        await asyncio.gather(*(self._terminate_via_spawn_loop(library_name, proc) for library_name, proc in processes))
        self._forget_starts_asked_of_hosts(library_names=library_names)
        if library_names is None:
            self._managed_worker_processes.clear()
            self._withdraw_session_offers()

    def _release_workers(self, *, library_names: set[str | None] | None, action: str) -> None:
        """Forget the named libraries' workers (every one when None), closing the channels to this engine's own.

        Before their processes are stopped, so what was waiting on one fails at once with why, and the
        channel ending reads as this manager's doing rather than as a worker that died. A worker another
        engine runs for this one is left running there, which replaces it at this engine's next start.
        """
        for wid, registration in self._own_workers().items():
            if library_names is not None and registration.worker_key not in library_names:
                continue
            if registration.worker_key is None:
                reason = f"the worker process running it was shut down to {action} libraries."
            else:
                reason = f"the worker process running it was shut down to {action} library '{registration.worker_key}'."
            self._release_worker(wid, reason)

    def _release_worker(self, worker_engine_id: str, reason: str) -> WorkerRegistration | None:
        """Forget one worker, failing what waits on it with `reason`. Returns its registration, or None if it was unknown."""
        registration = self._own_worker(worker_engine_id)
        if registration is None:
            return None
        del self._workers[worker_engine_id]
        registration.gone_reason = reason
        registration.gone.set()
        if registration.channel is not None:
            registration.channel.close()
        return registration

    def _stops_with(self, worker_key: str | None, library_names: set[str | None] | None) -> bool:
        """Whether stopping `library_names` (every library when None) stops the worker process under `worker_key`.

        A worker this engine runs for another engine's session is keyed by that session as well, and
        stops with its library, since resetting a library replaces the environment it runs in.
        """
        if library_names is None or worker_key in library_names:
            return True
        remote_library = self._served_library_of(worker_key)
        return remote_library is not None and remote_library in library_names

    def _served_library_of(self, worker_key: str | None) -> str | None:
        """The library of a worker this engine runs for another engine, or None for any other worker."""
        for registration in self._workers_served_for().values():
            if registration.worker_key == worker_key:
                return cast("_ServedFor", registration.serves).library_name
        return None

    def _forget_starts_asked_of_hosts(self, *, library_names: set[str | None] | None) -> None:
        """Forget the named libraries' starts on other engines (every library's when None)."""
        for wid, start in list(self._starts_asked_of_hosts.items()):
            if library_names is None or start.library_name in library_names:
                del self._starts_asked_of_hosts[wid]

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
        self, event_request: EventRequest, worker_engine_id: str, *, lease_id: str | None = None
    ) -> dict:
        """Send `event_request` to the named worker and await its raw result payload.

        The caller deserializes the returned dict into the appropriate result type. A node for a worker
        another engine runs is sent under `lease_id`, that engine's lease for it.

        No timeout: long-running AI workloads exceed any sensible default. The worker going away is what
        fails a request to it, at once, with `WorkerGoneError`.
        """
        registration = self._own_worker(worker_engine_id)
        if registration is None:
            msg = f"the worker process '{worker_engine_id}' is not running."
            raise worker_events.WorkerGoneError(msg)
        if registration.channel is not None:
            return await self._route_over_channel(event_request, registration, registration.channel)
        return await self._route_through_host(event_request, worker_engine_id, registration, lease_id)

    @staticmethod
    async def _route_over_channel(
        event_request: EventRequest, registration: WorkerRegistration, channel: AsyncChannel
    ) -> dict:
        """Send `event_request` to a local worker and await its raw result."""
        try:
            return await channel.request(event_request_envelope(event_request), timeout_s=None)
        except ChannelClosedError:
            raise worker_events.WorkerGoneError(registration.gone_reason or _LOCAL_WORKER_STOPPED) from None

    async def _route_through_host(
        self,
        event_request: EventRequest,
        worker_engine_id: str,
        registration: WorkerRegistration,
        lease_id: str | None,
    ) -> dict:
        """Send `event_request` to a worker another engine runs for this one, through that engine, and await its raw result."""
        host_engine_id = cast("str", registration.host_engine_id)
        envelope = event_request_envelope(event_request)
        request: RequestPayload
        if isinstance(event_request.request, ExecuteNodeRequest):
            if lease_id is None:
                msg = f"Attempted to run a node on engine {host_engine_id}. Failed because it was sent without a lease there."
                raise RuntimeError(msg)
            request = worker_events.ExecuteNodeOnRemoteWorkerRequest(
                worker_engine_id=worker_engine_id, lease_id=lease_id, execute_node=envelope
            )
        else:
            request = worker_events.SendToRemoteWorkerRequest(
                worker_engine_id=worker_engine_id, request=envelope, await_reply=True
            )
        sending = asyncio.ensure_future(self.engine.request(host_engine_id, request, timeout_s=None))
        gone = asyncio.ensure_future(registration.gone.wait())
        try:
            done, _ = await asyncio.wait({sending, gone}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            gone.cancel()
            if not sending.done():
                sending.cancel()
        if sending not in done:
            raise worker_events.WorkerGoneError(registration.gone_reason or _LOCAL_WORKER_STOPPED)
        try:
            result = sending.result()
        except EngineNotFoundError as e:
            raise worker_events.WorkerGoneError(str(e)) from e
        if isinstance(result, worker_events.ExecuteNodeOnRemoteWorkerResultSuccess):
            return result.result
        if isinstance(result, worker_events.SendToRemoteWorkerResultSuccess) and result.result is not None:
            return result.result
        gone = (
            isinstance(
                result,
                (worker_events.ExecuteNodeOnRemoteWorkerResultFailure, worker_events.SendToRemoteWorkerResultFailure),
            )
            and result.worker_gone
        )
        if gone:
            msg = f"{host_engine_id}, the computer running this library's nodes, stopped running them."
            raise worker_events.WorkerGoneError(msg)
        msg = f"{host_engine_id}, the computer running this library's nodes, did not run it: {result.result_details}"
        raise worker_events.WorkerGoneError(msg)

    async def acquire_lease(self, library_name: str, node_name: str) -> EngineLease | None:
        """Wait until the engine that runs `library_name`'s worker holds a lease for the node, or None if it runs here.

        A run asks each engine once: its later nodes there wait on the same lease, and its end releases
        it. A node outside any run has a lease of its own, which `release_after_node` releases.
        Unbounded, like the node itself: another run can hold that engine for as long as it runs. That
        engine may stop this engine's worker there to free itself for the lease, so pick the worker only
        after this returns, through `wait_until_executable`, which starts it again.

        Raises:
            RuntimeError: That engine will not run the node, with why.
        """
        host_engine_id = self._engine_for_library(library_name)
        if host_engine_id is None:
            return None
        lease = self._lease_for(host_engine_id)
        request_task = cast("asyncio.Task[ResultPayload]", lease.request_task)
        try:
            # Shielded, since the run's other nodes may wait on the same lease.
            result = await asyncio.shield(request_task)
        except EngineNotFoundError as e:
            self._end_lease(lease)
            msg = f"Attempted to run node '{node_name}' on engine {host_engine_id}. Failed because {e}"
            raise RuntimeError(msg) from e
        except asyncio.CancelledError:
            self.release_after_node(lease)
            raise
        if not result.succeeded():
            # Ended, so the run's next node there asks again.
            self._end_lease(lease)
            msg = f"Attempted to run node '{node_name}' on engine {host_engine_id}. Failed because {result.result_details}"
            raise RuntimeError(msg)
        return lease

    def release_after_node(self, lease: EngineLease | None) -> None:
        """Release a node's lease once the node ends, however it ends, if it is the node's own.

        A run's lease is released when the run ends instead, since its later nodes use it too.
        """
        if lease is None or lease.run_id is not None:
            return
        self._end_lease(lease)

    def _lease_for(self, host_engine_id: str) -> EngineLease:
        """The lease the code executing now uses on `host_engine_id`, acquired there if it has none."""
        run = self.engine.flow_manager.current_run()
        if run is not None:
            lease = self._run_leases.get((run.run_id, host_engine_id))
            if lease is not None:
                return lease
        run_id = None
        if run is not None:
            run_id = run.run_id
        lease = EngineLease(lease_id=str(uuid.uuid4()), host_engine_id=host_engine_id, run_id=run_id)
        self._leases[lease.lease_id] = lease
        request = worker_events.AcquireLeaseRequest(lease_id=lease.lease_id, run_id=run_id)
        lease.request_task = asyncio.ensure_future(self.engine.request(host_engine_id, request, timeout_s=None))
        # However the acquire is answered, the run no longer waits, even if that engine's last report was lost.
        lease.request_task.add_done_callback(lambda _task: self._stop_reporting_wait(lease))
        if run is not None:
            self._run_leases[(run.run_id, host_engine_id)] = lease
            run.ended.add_done_callback(lambda _ended: self._end_lease(lease))
        return lease

    def _stop_reporting_wait(self, lease: EngineLease) -> None:
        if lease.run_id is not None:
            self.engine.flow_manager.report_run_waiting_for_engine(lease.run_id, 0)

    def _end_lease(self, lease: EngineLease) -> None:
        """Release a lease on its engine, once, however it ended, so that engine serves the next run.

        Sent without waiting and retried: while the two engines are connected, nothing else releases it.
        """
        if self._leases.pop(lease.lease_id, None) is None:
            return
        if lease.run_id is not None and self._run_leases.get((lease.run_id, lease.host_engine_id)) is lease:
            del self._run_leases[(lease.run_id, lease.host_engine_id)]
        if lease.request_task is not None and not lease.request_task.done():
            lease.request_task.cancel()
        request = worker_events.ReleaseLeaseRequest(lease_id=lease.lease_id)
        self._send_notice(
            self._report_until_answered(lease.host_engine_id, request, f"lease {lease.lease_id} was released")
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
            # re-raise so cooperative cancellation propagates. Hop callers run under
            # teardown (the reset_workers gather); swallowing the cancel would let cleanup resume
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

        A worker handles no SIGTERM: it ends at once, and in normal operation it has
        already ended because its channel closed. After DEFAULT_TERMINATE_GRACE_S we
        send SIGKILL, which the kernel delivers regardless, so a hung worker can never
        leak.

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

        Returns immediately. This engine starts its own workers at once, as its child processes; a
        library the distribution settings assign to another engine is started there once a session
        is available.
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
            start = self._spawn_general_purpose_worker()
        else:
            host_engine_id = self._engine_for_library(library_name)
            if host_engine_id is None:
                start = self._spawn_library_worker(library_name)
            else:
                start = self._start_worker_on_engine(library_name, host_engine_id)
        # It outlives this request, so it acts on no workflow rather than the one the request names.
        task = create_task_bound_to_no_workflow(asyncio.get_running_loop(), start)
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
        return await self._start_worker_for_session(request, session_id, sender.engine_id)

    def _release_spawn_request(self, task: asyncio.Task, library_name: str | None) -> None:
        """Forget a finished spawn request, unless stopping the library's workers already withdrew it."""
        if self._requested_spawns.get(library_name) is task:
            del self._requested_spawns[library_name]

    async def _spawn_library_worker(self, library_name: str) -> None:
        """Spawn the worker subprocess that runs the given library's nodes for this engine."""
        # The worker is handed its library's execution environment as PYTHONPATH, so that directory
        # has to exist before the process starts. It does: the orchestrator builds it while
        # registering the library, and LibraryManager asks for no worker until that registration has
        # finished, nor for one whose build failed.
        resolved = self._prefixed_worker_command(
            library_name, [sys.executable, "-m", "griptape_nodes.worker", "--library-name", library_name]
        )
        if isinstance(resolved, WorkerCommandRefusal):
            logger.error("Not starting a worker for library '%s': %s", library_name, resolved.reason)
            self.note_worker_unavailable(library_name, resolved.reason)
            return
        await self._spawn_local_worker(resolved.args, library_name)

    async def _start_worker_on_engine(self, library_name: str, host_engine_id: str) -> None:
        """Wait for an active session, then ask the engine `host_engine_id` to start the library's worker.

        That engine runs the worker as its own child and reports it ready here once it has loaded.
        Any refusal on the way is recorded as the reason the library cannot run, so a run fails with
        it at once.
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
            # That engine's record of the session does not name the library, so register again, once.
            result = await self._ask_engine_to_start_worker(library_name, host_engine_id, session_id)
        if result is None or isinstance(result, worker_events.StartWorkerForEngineResultSuccess):
            return
        reason = f"{host_engine_id} did not start its worker: {result.result_details}"
        logger.error("Not starting a worker for library '%s': %s", library_name, reason)
        # The node waiting now fails with the reason; after a capacity refusal, the library's next node asks again.
        at_capacity = isinstance(result, worker_events.StartWorkerForEngineResultFailure) and result.at_capacity
        self.note_worker_unavailable(library_name, reason, retryable=at_capacity)

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
        self._starts_asked_of_hosts[worker_engine_id] = _StartAskedOfHost(
            host_engine_id=host_engine_id, library_name=library_name
        )
        logger.info("Asking %s to start worker %s for library '%s'.", host_engine_id, worker_engine_id, library_name)
        request = worker_events.StartWorkerForEngineRequest(
            library_name=library_name,
            worker_engine_id=worker_engine_id,
            project_id=self.engine.project_manager.committed_project_id,
        )
        try:
            result = await self.engine.request(host_engine_id, request, timeout_s=ENGINE_REQUEST_TIMEOUT_S)
        except EngineNotFoundError as e:
            # That engine may still get the start, such as once a connection that was down comes back.
            self._schedule_ask_host_to_stop_remote_worker(worker_engine_id, host_engine_id)
            logger.error("Not starting a worker for library '%s': %s", library_name, e)
            self.note_worker_unavailable(library_name, str(e))
            return None
        except BaseException:
            # That engine may have started the worker before the start was withdrawn or failed here.
            self._schedule_ask_host_to_stop_remote_worker(worker_engine_id, host_engine_id)
            raise
        if not isinstance(result, worker_events.StartWorkerForEngineResultSuccess):
            self._starts_asked_of_hosts.pop(worker_engine_id, None)
        return result

    def _schedule_ask_host_to_stop_remote_worker(self, worker_engine_id: str, host_engine_id: str) -> None:
        """Forget a start withdrawn while `host_engine_id` was answering it, and tell that engine to stop that worker."""
        self._starts_asked_of_hosts.pop(worker_engine_id, None)
        task = create_task_bound_to_no_workflow(
            asyncio.get_running_loop(), self._ask_host_to_stop_remote_worker(worker_engine_id, host_engine_id)
        )
        self._stop_requests.add(task)
        task.add_done_callback(self._stop_requests.discard)

    async def _ask_host_to_stop_remote_worker(self, worker_engine_id: str, host_engine_id: str) -> None:
        request = worker_events.StopWorkerForEngineRequest(worker_engine_id=worker_engine_id)
        try:
            result = await self.engine.request(host_engine_id, request, timeout_s=ENGINE_REQUEST_TIMEOUT_S)
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
            self._forget_leases_on(host_engine_id)
            task = create_task_bound_to_no_workflow(
                asyncio.get_running_loop(), self._withdraw_session_offer(host_engine_id)
            )
            self._stop_requests.add(task)
            task.add_done_callback(self._stop_requests.discard)

    async def _withdraw_session_offer(self, host_engine_id: str) -> None:
        try:
            result = await self.engine.request(
                host_engine_id, worker_events.WithdrawSessionOfferRequest(), timeout_s=ENGINE_REQUEST_TIMEOUT_S
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
            registration = create_task_bound_to_no_workflow(
                asyncio.get_running_loop(), self._send_registration(host_engine_id)
            )
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
        result = await self.engine.request(host_engine_id, request, timeout_s=ENGINE_REQUEST_TIMEOUT_S)
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

        Handled like a local worker's exit: a start whose worker never loaded settles the library with
        the reason, and a worker in service is removed at once. "exited" leaves the library unavailable
        with the reason; "stopped" leaves it free to be started again.
        """
        wid = request.worker_engine_id
        start = self._starts_asked_of_hosts.get(wid)
        registration = self._own_worker(wid)
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
        self._starts_asked_of_hosts.pop(wid, None)
        registered = start is None
        if request.details is not None:
            reason = f"the worker process that runs it was refused by {host_engine_id}: {request.details}"
        elif request.reason == "stopped":
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
            self._release_worker(wid, reason)
        # The local rules: a worker that ended on its own leaves its library unavailable, and one that
        # was stopped does so only if it never loaded, so whatever waits on it hears why.
        if request.reason == "exited" or not self.has_settled(library_name):
            self.note_worker_unavailable(library_name, reason)
        return worker_events.ReportWorkerExitedResultSuccess(result_details=f"Recorded that worker {wid} exited.")

    @handles(
        worker_events.ReportRemoteWorkerReadyRequest,
        sender=RequiredSender.ENGINE,
        owner=lambda manager, request, _sender: manager.worker_host(request.worker_engine_id),
    )
    async def handle_report_remote_worker_ready_request(
        self, request: worker_events.ReportRemoteWorkerReadyRequest
    ) -> worker_events.ReportRemoteWorkerReadyResultSuccess | worker_events.ReportRemoteWorkerReadyResultFailure:
        """Take into service a worker another engine started for this one, now that it has loaded its library."""
        wid = request.worker_engine_id
        start = self._starts_asked_of_hosts.get(wid)
        if start is None:
            return worker_events.ReportRemoteWorkerReadyResultFailure(
                result_details=f"This engine is not waiting on worker {wid}, so it does not need it."
            )
        del self._starts_asked_of_hosts[wid]
        self._workers[wid] = WorkerRegistration(worker_key=start.library_name, host_engine_id=start.host_engine_id)
        logger.debug("Worker %s for library '%s' runs on engine %s", wid, start.library_name, start.host_engine_id)
        await self._note_libraries_loaded(start.library_name, request.libraries)
        # It started on the project the start named; a switch since then reaches it only this way.
        self._send_notice(self._activate_project_on_worker(wid))
        return worker_events.ReportRemoteWorkerReadyResultSuccess(result_details=f"Took worker {wid} into service.")

    @handles(
        worker_events.DeliverWorkerEventsRequest,
        sender=RequiredSender.ENGINE,
        owner=lambda manager, request, _sender: manager.worker_host(request.worker_engine_id),
    )
    async def handle_deliver_worker_events_request(
        self, request: worker_events.DeliverWorkerEventsRequest
    ) -> worker_events.DeliverWorkerEventsResultSuccess | worker_events.DeliverWorkerEventsResultFailure:
        """Act on a batch of what a worker another engine runs for this one sent the editor, as for a worker of this engine's."""
        if self.worker_host(request.worker_engine_id) is None:
            return worker_events.DeliverWorkerEventsResultFailure(
                result_details=f"This engine runs no worker {request.worker_engine_id} on another engine."
            )
        await apply_worker_notifications(self.engine, request.events, self._relay_worker_event)
        return worker_events.DeliverWorkerEventsResultSuccess(result_details=f"Took {len(request.events)} event(s).")

    @handles(
        worker_events.DecideWorkerCheckpointRequest,
        sender=RequiredSender.ENGINE,
        owner=lambda manager, request, _sender: manager.worker_host(request.worker_engine_id),
    )
    async def handle_decide_worker_checkpoint_request(
        self, request: worker_events.DecideWorkerCheckpointRequest
    ) -> worker_events.DecideWorkerCheckpointResultSuccess | worker_events.DecideWorkerCheckpointResultFailure:
        """Decide a checkpoint a worker another engine runs for this one reached, with this engine's authorization hooks."""
        if self.worker_host(request.worker_engine_id) is None:
            return worker_events.DecideWorkerCheckpointResultFailure(
                result_details=f"This engine runs no worker {request.worker_engine_id} on another engine."
            )
        body = {"type": AUTHORIZATION_CHECKPOINT, "payload": request.checkpoint}
        # On the loop, as a local worker's is: the hooks evaluate policy in process and do not block.
        reply = decide_worker_checkpoint(self.engine, body)
        return worker_events.DecideWorkerCheckpointResultSuccess(
            denial=reply["denial"], result_details=f"Decided a checkpoint for worker {request.worker_engine_id}."
        )

    async def admit_remote_worker_request(
        self, request: RequestPayload, result_context: ResultContext
    ) -> RemoteWorkerRequest | ResultPayload:
        """Admit a request another engine relays from a worker it runs for this one, as this engine's own worker's.

        It passes the same worker request gates a local worker's request does, so the license wire
        policy sees the request's own type. A secret it reads is answered by the remote worker secret
        policy when one decides it, and otherwise from this engine's secrets, as a local worker's is.
        """
        worker_engine_id = cast("str", result_context.get("origin_engine_id"))
        sender = result_context.get("sender")
        request_type = type(request).__name__
        host_engine_id = self.worker_host(worker_engine_id)
        if sender is None or host_engine_id is None or host_engine_id != sender.engine_id:
            details = (
                f"Attempted to handle a {request_type} from worker {worker_engine_id}. Failed because this "
                "engine did not ask the engine that sent it to run that worker."
            )
            return GenericResultFailure(result_details=details)
        event_request = EventRequest(
            request=request,
            request_id=result_context.get("request_id"),
            open_workflow_id=result_context.get("open_workflow_id"),
        )
        decision = self._event_manager.evaluate_worker_request_gate(event_request_envelope(event_request))
        if decision.forward is None:
            logger.warning(
                "The license policy refused a %s from a library's worker process on %s: %s",
                request_type,
                host_engine_id,
                decision.denials,
            )
            details = (
                f"Attempted to run '{request_type}' from a library's worker process on {host_engine_id}. "
                "Failed because the license policy refused it."
            )
            return GenericResultFailure(result_details=details)
        try:
            admitted = read_event_request(decision.forward)
        except WireFormatError as e:
            details = f"Attempted to handle a request from a library's worker process on {host_engine_id}. Failed because {e}."
            return GenericResultFailure(result_details=details)
        secret_answer = self._remote_worker_secret_answer(admitted.request)
        if secret_answer is not None:
            return secret_answer
        context: ResultContext = {
            "request_id": result_context.get("request_id"),
            "response_topic": result_context.get("response_topic"),
            "sender": None,
            "open_workflow_id": admitted.open_workflow_id,
        }
        return RemoteWorkerRequest(request=admitted.request, result_context=context)

    def _remote_worker_secret_answer(self, request: RequestPayload) -> ResultPayload | None:
        """The policy's answer to a remote worker's secret read, or None for any other request or one it leaves alone."""
        if not isinstance(request, GetSecretValueRequest):
            return None
        secret_name = SecretsManager._apply_secret_name_compliance(request.key)
        answer = self._event_manager.remote_worker_secret_answer(secret_name)
        if answer is None:
            return None
        return worker_events.RemoteWorkerSecretResultSuccess(
            value=answer.value,
            from_serving_engine=answer.from_serving_engine,
            result_details=f"Answered secret '{secret_name}' for a library's worker another engine runs for this one.",
        )

    @handles(
        worker_events.OfferSessionForWorkersRequest,
        sender=RequiredSender.ANY_SESSION,
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
        self, request: worker_events.StartWorkerForEngineRequest, session_id: str, engine_id: str
    ) -> worker_events.StartWorkerForEngineResultSuccess | worker_events.StartWorkerForEngineResultFailure:
        """Start a worker, as this engine's child process, for an artist's session on another engine, under the ID it chose.

        Answers once the process exists. The worker connects, loads its library on the artist's project,
        and is reported ready to the artist's engine after.
        """
        library_name = request.library_name
        worker_engine_id = request.worker_engine_id
        worker_key = f"{session_id}/{library_name}"
        refusal = self._start_refusal(library_name, session_id, worker_key, worker_engine_id)
        if refusal is not None:
            return refusal
        resolved = self._prefixed_worker_command(
            library_name, [sys.executable, "-m", "griptape_nodes.worker", "--library-name", library_name]
        )
        if isinstance(resolved, WorkerCommandRefusal):
            return worker_events.StartWorkerForEngineResultFailure(result_details=resolved.reason)
        # Read before anything awaits, since a lost connection can forget the session meanwhile.
        session = self._sessions[session_id]
        # Recorded before anything awaits, so a stop or another start sent meanwhile sees this one.
        served = _ServedFor(owning_engine_id=engine_id, session_id=session_id, library_name=library_name)
        registration = WorkerRegistration(worker_key=worker_key, serves=served)
        self._workers[worker_engine_id] = registration

        def abandon() -> None:
            """Forget this start, and release whatever waits for it to have spawned."""
            if self._served_worker(worker_engine_id) is not None:
                del self._workers[worker_engine_id]
            served.started.set()

        try:
            pending = await listen_for_worker()
        except OSError as e:
            abandon()
            return worker_events.StartWorkerForEngineResultFailure(
                result_details=(
                    f"Attempted to start a worker for library '{library_name}'. Failed because this engine could "
                    f"not open a connection for it: {e}"
                )
            )
        except BaseException:
            abandon()
            raise

        environ = functools.partial(
            self._served_worker_environ, registration, worker_engine_id, session, pending.child_environment
        )

        try:
            # The artist's engine asks only once it has no worker for the library, so a running one is a
            # worker it already stopped using, such as after a library reset.
            stale = self._managed_worker_processes.pop(worker_key, None)
            if stale is not None:
                logger.info("Replacing the worker for key '%s' that its session stopped using.", worker_key)
                await self._terminate_via_spawn_loop(worker_key, stale)
            proc = await self._start_worker_process(resolved.args, worker_key, environ)
        except BaseException:
            pending.close()
            abandon()
            raise
        if proc is None:
            pending.close()
            abandon()
            return worker_events.StartWorkerForEngineResultFailure(
                result_details=f"A worker for library '{library_name}' is already starting for that session."
            )
        registration.process = proc
        served.started.set()
        self._watch_and_connect_served_worker(worker_engine_id, registration, proc, pending, request)
        return worker_events.StartWorkerForEngineResultSuccess(
            result_details=f"Started worker {worker_engine_id} for library '{library_name}' for another engine."
        )

    async def _served_worker_environ(
        self,
        registration: WorkerRegistration,
        worker_engine_id: str,
        session: _SessionRegistration,
        child_environment: dict[str, str],
    ) -> dict[str, str]:
        """The environment a worker this engine runs for another engine's session starts with."""
        served = cast("_ServedFor", registration.serves)
        worker_environ = await self._base_worker_environ(registration.worker_key, served.library_name)
        # It runs on the artist's license session, which it asks the artist's engine for, never on this engine's.
        for withheld in _LOCAL_WORKER_WITHHELD_ENV:
            worker_environ.pop(withheld, None)
        for emptied in _WORKER_EMPTIED_ENV:
            worker_environ[emptied] = ""
        # Its asset URLs are opened by the artist's editor, which reaches the artist's engine's server.
        if session.static_server_base_url is not None:
            worker_environ[ORCHESTRATOR_STATIC_SERVER_BASE_URL_ENV] = session.static_server_base_url
        worker_environ[WORKER_ENGINE_ID_ENV] = worker_engine_id
        worker_environ.update(child_environment)
        return worker_environ

    def _watch_and_connect_served_worker(
        self,
        worker_engine_id: str,
        registration: WorkerRegistration,
        proc: asyncio.subprocess.Process,
        pending: PendingWorker,
        request: worker_events.StartWorkerForEngineRequest,
    ) -> None:
        """Watch a just-started worker this engine runs for another engine, and connect it, each in its own task."""
        loop = asyncio.get_running_loop()
        # Watching the process is what reports its exit to the artist's engine and lets another start for the session.
        watcher = create_task_bound_to_no_workflow(loop, self._report_when_exited(worker_engine_id, registration, proc))
        cast("_ServedFor", registration.serves).exit_watcher = watcher
        self._exit_watchers.add(watcher)
        watcher.add_done_callback(self._exit_watchers.discard)
        connecting = create_task_bound_to_no_workflow(
            loop,
            self._connect_served_worker(
                worker_engine_id,
                registration,
                proc,
                pending,
                project_id=request.project_id,
            ),
        )
        self._exit_watchers.add(connecting)
        connecting.add_done_callback(self._exit_watchers.discard)

    async def _connect_served_worker(
        self,
        worker_engine_id: str,
        registration: WorkerRegistration,
        proc: asyncio.subprocess.Process,
        pending: PendingWorker,
        *,
        project_id: str,
    ) -> None:
        """Wait for a worker this engine runs for another engine to connect, then carry it, on that engine's project."""
        # Lazy import: worker_routing imports this module.
        from griptape_nodes.app.worker_routing import ActivateProjectRequest

        served = cast("_ServedFor", registration.serves)
        try:
            channel = await self._await_worker_connection(proc, pending)
        except TimeoutError:
            logger.error(
                "Worker %s for key '%s' did not connect within %.0fs; stopping it.",
                worker_engine_id,
                registration.worker_key,
                self.library_load_timeout_s,
            )
            await self._end_served_worker(registration)
            return
        # Exited before connecting, which its exit watcher reports.
        if channel is None:
            return
        # Stopped while it was connecting.
        if (
            self._workers.get(worker_engine_id) is not registration
            or self._managed_worker_processes.get(registration.worker_key) is not proc
        ):
            channel.close()
            return
        end = RemoteWorkerEnd(
            self.engine,
            worker_engine_id=worker_engine_id,
            artist_engine_id=served.owning_engine_id,
            request_timeout_s=ENGINE_REQUEST_TIMEOUT_S,
            on_hello=functools.partial(self._accept_served_hello, worker_engine_id),
            on_closed=functools.partial(self._start_end_served_worker, registration),
        )
        registration.channel = channel
        served.end = end
        end.attach(channel)
        logger.debug("Worker %s for key '%s' (pid %s) connected", worker_engine_id, registration.worker_key, proc.pid)
        # The worker cannot ask the artist's engine yet: that engine takes it into service only once it is ready.
        activation = EventRequest(request=ActivateProjectRequest(project_id=project_id))
        with contextlib.suppress(ChannelClosedError):
            channel.notify(event_request_envelope(activation))

    async def _accept_served_hello(self, worker_engine_id: str, hello: WorkerHello) -> dict[str, Any]:
        """Report a worker this engine runs for another engine ready to that engine, or refuse it with why."""
        registration = self._served_worker(worker_engine_id)
        if registration is None:
            return {"accepted": False, "details": "This engine has already stopped this worker."}
        served = cast("_ServedFor", registration.serves)
        refusal = self._hello_refusal(served.library_name, hello)
        if refusal is not None:
            logger.error("Refused worker %s for key '%s': %s", worker_engine_id, registration.worker_key, refusal)
            served.refusal = refusal
            return {
                "accepted": False,
                "details": f"Attempted to start worker {worker_engine_id}. Failed because {refusal}",
            }
        ready = worker_events.ReportRemoteWorkerReadyRequest(
            worker_engine_id=worker_engine_id, libraries=hello.libraries
        )
        try:
            result = await self.engine.request(served.owning_engine_id, ready, timeout_s=ENGINE_REQUEST_TIMEOUT_S)
        except EngineNotFoundError as e:
            return {
                "accepted": False,
                "details": f"Attempted to report worker {worker_engine_id} ready. Failed because {e}",
            }
        if not isinstance(result, worker_events.ReportRemoteWorkerReadyResultSuccess):
            return {
                "accepted": False,
                "details": (
                    f"Engine {served.owning_engine_id} did not take worker {worker_engine_id}: {result.result_details}"
                ),
            }
        return {"accepted": True, "details": f"Started worker {worker_engine_id} for engine {served.owning_engine_id}."}

    def _start_end_served_worker(self, registration: WorkerRegistration) -> None:
        """As the channel to a worker this engine runs for another engine ends: end the worker."""
        task = create_task_bound_to_no_workflow(asyncio.get_running_loop(), self._end_served_worker(registration))
        self._exit_watchers.add(task)
        task.add_done_callback(self._exit_watchers.discard)

    async def _end_served_worker(self, registration: WorkerRegistration) -> None:
        """Stop a worker this engine runs for another engine that ended on its own. Its exit watcher reports it exited."""
        proc = registration.process
        # Stopped by this engine, which reports it stopped.
        if proc is None or self._managed_worker_processes.get(registration.worker_key) is not proc:
            return
        cast("_ServedFor", registration.serves).ended_on_its_own = True
        del self._managed_worker_processes[registration.worker_key]
        await self._terminate_via_spawn_loop(registration.worker_key, proc)

    def _start_refusal(
        self, library_name: str, session_id: str, worker_key: str, worker_engine_id: str
    ) -> worker_events.StartWorkerForEngineResultFailure | None:
        """The failure a start of `worker_key`'s worker for that session returns, or None if this engine will start it."""
        # The ID shares the table with this engine's own workers. An earlier served worker's is replaced.
        if self._own_worker(worker_engine_id) is not None:
            return worker_events.StartWorkerForEngineResultFailure(
                result_details=(
                    f"Attempted to start worker {worker_engine_id} for library '{library_name}'. Failed because "
                    "that worker ID already names one of this engine's workers."
                )
            )
        starting = any(
            registration.worker_key == worker_key and not cast("_ServedFor", registration.serves).started.is_set()
            for registration in self._workers_served_for().values()
        )
        if starting or worker_key in self._spawns_in_flight:
            return worker_events.StartWorkerForEngineResultFailure(
                result_details=f"A worker for library '{library_name}' is already starting for that session."
            )
        if self.engine.get_engine_id() is None:
            return worker_events.StartWorkerForEngineResultFailure(
                result_details="this engine has no engine ID, which its workers need to ask it before each node."
            )
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
            {
                registration.worker_key
                for registration in self._workers_served_for().values()
                if registration.worker_key != worker_key
            }
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
        session = self._sessions[session_id]
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
        return library_name not in self._sessions[session_id].library_versions

    async def _report_when_exited(
        self, worker_engine_id: str, registration: WorkerRegistration, proc: asyncio.subprocess.Process
    ) -> None:
        """Tell the artist's engine that a worker this engine ran for it exited, and why."""
        served = cast("_ServedFor", registration.serves)
        exit_code = await proc.wait()
        reason: Literal["stopped", "exited"] = "stopped"
        # A process this manager stopped itself was dropped from the records first.
        if self._managed_worker_processes.get(registration.worker_key) is proc:
            del self._managed_worker_processes[registration.worker_key]
            reason = "exited"
        if served.ended_on_its_own:
            reason = "exited"
        logger.debug(
            "Worker %s for key '%s' %s (exit code %s)", worker_engine_id, registration.worker_key, reason, exit_code
        )
        if self._workers.get(worker_engine_id) is registration:
            del self._workers[worker_engine_id]
        # Fails what this engine was waiting on it for, such as a node or freeing its models at a handoff.
        if registration.channel is not None:
            registration.channel.close()
        request = worker_events.ReportWorkerExitedRequest(
            worker_engine_id=worker_engine_id, exit_code=exit_code, reason=reason, details=served.refusal
        )
        owning_engine_id = served.owning_engine_id
        try:
            result = await self.engine.request(owning_engine_id, request, timeout_s=ENGINE_REQUEST_TIMEOUT_S)
        except EngineNotFoundError as e:
            logger.warning("Could not tell engine %s that worker %s exited: %s", owning_engine_id, worker_engine_id, e)
            return
        if not isinstance(result, worker_events.ReportWorkerExitedResultSuccess):
            logger.warning(
                "Engine %s did not record that worker %s exited: %s",
                owning_engine_id,
                worker_engine_id,
                result.result_details,
            )

    @handles(
        worker_events.StopWorkerForEngineRequest,
        sender=RequiredSender.ENGINE,
        owner=lambda manager, request, _sender: manager.owning_engine_of(request.worker_engine_id),
    )
    async def handle_stop_worker_for_engine_request(
        self, request: worker_events.StopWorkerForEngineRequest
    ) -> worker_events.StopWorkerForEngineResultSuccess | worker_events.StopWorkerForEngineResultFailure:
        """Stop a worker this engine runs for an artist's session, at that artist's engine's request."""
        wid = request.worker_engine_id
        registration = self._served_worker(wid)
        if registration is None:
            return worker_events.StopWorkerForEngineResultSuccess(
                result_details=f"This engine is not running worker {wid}."
            )
        await self._stop_served_worker(registration)
        return worker_events.StopWorkerForEngineResultSuccess(result_details=f"Stopped worker {wid}.")

    @handles(
        worker_events.ExecuteNodeOnRemoteWorkerRequest,
        sender=RequiredSender.ENGINE,
        owner=lambda manager, request, _sender: manager.owning_engine_of(request.worker_engine_id),
    )
    async def handle_execute_node_on_remote_worker_request(
        self, request: worker_events.ExecuteNodeOnRemoteWorkerRequest
    ) -> worker_events.ExecuteNodeOnRemoteWorkerResultSuccess | worker_events.ExecuteNodeOnRemoteWorkerResultFailure:
        """Run a node on a worker this engine runs for the engine that asked, while that engine's lease holds this one.

        Answered once every event the worker sent while running it has reached that engine, so its
        progress lands there ahead of its result.
        """
        engine_id = require_request_sender().engine_id
        wid = request.worker_engine_id
        registration = self._served_worker(wid)
        end = None
        if registration is not None:
            end = cast("_ServedFor", registration.serves).end
        if registration is None or registration.channel is None or end is None:
            return worker_events.ExecuteNodeOnRemoteWorkerResultFailure(
                result_details=f"this engine is not running worker {wid} for engine {engine_id}.", worker_gone=True
            )
        refusal = _served_worker_envelope_refusal(request.execute_node, _RUN_ON_REMOTE_WORKERS, wid)
        if refusal is not None:
            return worker_events.ExecuteNodeOnRemoteWorkerResultFailure(result_details=refusal)
        engine_run = self._engine_runs.get((engine_id, request.lease_id))
        if engine_run is None or not engine_run.holds_engine:
            return worker_events.ExecuteNodeOnRemoteWorkerResultFailure(
                result_details=(
                    f"that engine is not holding itself for engine {engine_id}'s run now. The run may have "
                    "ended, or another run has that engine."
                )
            )
        try:
            raw = await registration.channel.request(request.execute_node, timeout_s=None)
        except ChannelClosedError:
            return worker_events.ExecuteNodeOnRemoteWorkerResultFailure(
                result_details=f"the worker process running it on {self.engine.get_engine_id()} stopped.",
                worker_gone=True,
            )
        await end.flush_events()
        return worker_events.ExecuteNodeOnRemoteWorkerResultSuccess(
            result=raw, result_details=f"Worker {wid} answered the node."
        )

    @handles(
        worker_events.SendToRemoteWorkerRequest,
        sender=RequiredSender.ENGINE,
        owner=lambda manager, request, _sender: manager.owning_engine_of(request.worker_engine_id),
    )
    async def handle_send_to_remote_worker_request(
        self, request: worker_events.SendToRemoteWorkerRequest
    ) -> worker_events.SendToRemoteWorkerResultSuccess | worker_events.SendToRemoteWorkerResultFailure:
        """Pass a request from the engine that asked to a worker this engine runs for it.

        Only what `_SENT_TO_REMOTE_WORKERS` names: anything else, such as a node to run, needs the lease
        that `ExecuteNodeOnRemoteWorkerRequest` carries.
        """
        engine_id = require_request_sender().engine_id
        wid = request.worker_engine_id
        refusal = _served_worker_envelope_refusal(request.request, _SENT_TO_REMOTE_WORKERS, wid)
        if refusal is not None:
            return worker_events.SendToRemoteWorkerResultFailure(result_details=refusal)
        registration = self._served_worker(wid)
        if registration is None or registration.channel is None:
            return worker_events.SendToRemoteWorkerResultFailure(
                result_details=f"this engine is not running worker {wid} for engine {engine_id}.", worker_gone=True
            )
        gone = worker_events.SendToRemoteWorkerResultFailure(
            result_details=f"the worker process {wid} on {self.engine.get_engine_id()} stopped.", worker_gone=True
        )
        if not request.await_reply:
            try:
                registration.channel.notify(request.request)
            except ChannelClosedError:
                return gone
            return worker_events.SendToRemoteWorkerResultSuccess(result_details=f"Sent it to worker {wid}.")
        try:
            raw = await registration.channel.request(request.request, timeout_s=None)
        except ChannelClosedError:
            return gone
        return worker_events.SendToRemoteWorkerResultSuccess(result=raw, result_details=f"Worker {wid} answered it.")

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
        worker_events.AcquireLeaseRequest,
        sender=RequiredSender.SESSION,
        owner=lambda manager, _request, sender: manager._engine_that_offered(sender),
    )
    async def handle_acquire_lease_request(
        self, request: worker_events.AcquireLeaseRequest
    ) -> worker_events.AcquireLeaseResultSuccess | worker_events.AcquireLeaseResultFailure:
        """Answer once another engine's lease holds this engine, which waits while another run has this engine.

        The lease waits in the line of other engines' leases, which this engine's own runs don't join, and
        holds this engine from when it reaches the front until it is released.
        """
        sender = require_request_sender()
        engine_run = self._engine_runs.get((sender.engine_id, request.lease_id))
        if engine_run is None:
            engine_run = self._submit_engine_run(sender.engine_id, request)
        refusal = await asyncio.shield(engine_run.granted)
        if refusal is not None:
            return worker_events.AcquireLeaseResultFailure(result_details=refusal)
        return worker_events.AcquireLeaseResultSuccess(result_details=f"Lease {request.lease_id} holds this engine.")

    @handles(
        worker_events.ReleaseLeaseRequest,
        sender=RequiredSender.SESSION,
        owner=lambda manager, _request, sender: manager._engine_that_offered(sender),
    )
    async def handle_release_lease_request(
        self, request: worker_events.ReleaseLeaseRequest
    ) -> worker_events.ReleaseLeaseResultSuccess:
        """Release another engine's lease, so the next run in line gets this engine, or take it out of line."""
        engine_id = require_request_sender().engine_id
        engine_run = self._engine_runs.get((engine_id, request.lease_id))
        if engine_run is None:
            return worker_events.ReleaseLeaseResultSuccess(
                result_details=f"This engine held no lease {request.lease_id} for engine {engine_id}."
            )
        await self._end_engine_run(engine_run, "its lease was released.")
        return worker_events.ReleaseLeaseResultSuccess(
            result_details=f"Released lease {request.lease_id} of engine {engine_id}."
        )

    @handles(
        worker_events.ReportPlaceInLineRequest,
        sender=RequiredSender.ENGINE,
        owner=lambda manager, request, _sender: manager._engine_leasing(request.lease_id),
    )
    def handle_report_place_in_line_request(
        self, request: worker_events.ReportPlaceInLineRequest
    ) -> worker_events.ReportPlaceInLineResultSuccess:
        """Report the place in line of a run here whose lease waits on another engine, as if it waited here."""
        lease = self._leases.get(request.lease_id)
        # A report that arrives after the acquire was answered is out of date.
        waiting = lease is not None and lease.request_task is not None and not lease.request_task.done()
        if lease is None or lease.run_id is None or not waiting:
            return worker_events.ReportPlaceInLineResultSuccess(
                result_details="No run here waits on that lease, so there was nothing to report."
            )
        self.engine.flow_manager.report_run_waiting_for_engine(lease.run_id, request.position)
        return worker_events.ReportPlaceInLineResultSuccess(result_details=f"Reported place {request.position}.")

    async def hold_for_engine_run(self, engine_run: _EngineRun) -> ResultPayload:
        """Hold this engine for another engine's lease, from when it reaches the front of the line until it is released.

        Passing from one engine's lease to another's first frees workers, as `distributed_execution.release`
        says, and only then grants the lease, so a worker it stops has been reported stopped before that
        engine sends a node. Freeing is best effort: a failure there still hands this engine over.
        """
        if self._last_engine_run_engine_id not in (None, engine_run.engine_id):
            try:
                await self._free_for_handoff(engine_run.engine_id)
            except Exception:
                logger.exception("Could not free this engine's workers for engine %s's run.", engine_run.engine_id)
        self._last_engine_run_engine_id = engine_run.engine_id
        try:
            if not engine_run.granted.done():
                engine_run.granted.set_result(None)
            await engine_run.released.wait()
        finally:
            self._forget_engine_run(engine_run)
        return worker_events.ReleaseLeaseResultSuccess(
            result_details=f"Engine {engine_run.engine_id}'s lease let this engine go."
        )

    def report_place_in_line(self, engine_run: _EngineRun, position: int) -> None:
        """Tell the engine whose lease waits here its place in line."""
        if engine_run.granted.done():
            return
        self._send_notice(self._send_place_in_line(engine_run.engine_id, engine_run.lease_id, position))

    def refuse_engine_run(self, engine_run: _EngineRun, details: str) -> None:
        """Tell another engine's lease waiting here that it will not get this engine, and why.

        Also releases it, so a lease refused while `hold_for_engine_run` frees workers for it lets this engine go.
        """
        if not engine_run.granted.done():
            engine_run.granted.set_result(details)
        engine_run.released.set()
        self._forget_engine_run(engine_run)

    def has_registered_session(self, sender: RequestSender) -> bool:
        """Whether the sender's session registered with this engine, offering it to run that engine's work."""
        return sender.session_id in self._sessions

    def _engine_that_offered(self, sender: RequestSender) -> str | None:
        """The engine that offered the sender's session to this one, or None if none has."""
        session = self._sessions.get(cast("str", sender.session_id))
        if session is None:
            return None
        return session.engine_id

    async def _recycle_served_worker(self, registration: WorkerRegistration) -> None:
        """Stop a worker for a handoff, returning once its engine has heard it stopped.

        Its engine starts it again before that library's next node, so the next lease must not be granted
        while that engine still counts the stopped worker as running.
        """
        await self._stop_served_worker(registration)
        exit_watcher = cast("_ServedFor", registration.serves).exit_watcher
        if exit_watcher is not None:
            await asyncio.shield(exit_watcher)

    async def _stop_served_worker(self, registration: WorkerRegistration) -> None:
        """Stop a worker this engine runs for another engine. Its exit watcher reports it stopped.

        A spawn still under way is waited out first, so a stop sent while the start is being answered
        stops the process that spawn makes.
        """
        await cast("_ServedFor", registration.serves).started.wait()
        proc = registration.process
        if proc is None or self._managed_worker_processes.get(registration.worker_key) is not proc:
            return
        del self._managed_worker_processes[registration.worker_key]
        if registration.channel is not None:
            registration.channel.close()
        await self._terminate_via_spawn_loop(registration.worker_key, proc)

    async def on_engine_lost(self, engine_id: str) -> None:
        """Settle the workers on an engine whose connection was lost, or forget an artist's engine whose connection was.

        On the artist's side, the workers that engine runs for this one are gone, whether started or still
        starting, and a node waiting on one fails with why. On the other engine's side, that artist's
        sessions are forgotten and the workers run for them stopped.
        """
        lost = (
            f"the connection to {engine_id}, which runs its worker, was lost. "
            "Reload your libraries, or restart Griptape Nodes, to start it again."
        )
        for wid, start in list(self._starts_asked_of_hosts.items()):
            if start.host_engine_id != engine_id:
                continue
            del self._starts_asked_of_hosts[wid]
            self.note_worker_unavailable(start.library_name, lost)
        for wid, registration in self._own_workers().items():
            if registration.host_engine_id != engine_id:
                continue
            self._release_worker(wid, lost)
            self.note_worker_unavailable(registration.worker_key, lost)
        # That engine forgets this engine's session once it loses the connection, so the offer is gone too.
        for key in [key for key in self._engine_registrations if key[0] == engine_id]:
            del self._engine_registrations[key]
        self._forget_leases_on(engine_id)
        await self._forget_engine(engine_id, why="lost its connection")

    def _forget_leases_on(self, engine_id: str) -> None:
        """Forget this engine's leases on `engine_id`, which forgets them with the session, so a run's next node asks again.

        Neither released nor cancelled: that engine has no session to release them in, and an acquire still
        waiting fails on its own, with the reason.
        """
        for lease in [lease for lease in self._leases.values() if lease.host_engine_id == engine_id]:
            del self._leases[lease.lease_id]
            if lease.run_id is not None and self._run_leases.get((lease.run_id, lease.host_engine_id)) is lease:
                del self._run_leases[(lease.run_id, lease.host_engine_id)]

    async def _forget_engine(self, engine_id: str, *, why: str) -> None:
        """Forget the sessions `engine_id` offered this engine, let its runs go, and stop their workers."""
        await self._end_engine_runs_from(engine_id, f"engine {engine_id} {why}.")
        offered = [session_id for session_id, session in self._sessions.items() if session.engine_id == engine_id]
        for session_id in offered:
            del self._sessions[session_id]
        served = list(self._workers_served_for(engine_id).values())
        if not offered and not served:
            return
        logger.warning(
            "Engine %s %s; forgetting its %d session(s) and stopping the %d worker(s) run for it.",
            engine_id,
            why,
            len(offered),
            len(served),
        )
        await asyncio.gather(*(self._stop_served_worker(registration) for registration in served))

    def _submit_engine_run(self, engine_id: str, request: worker_events.AcquireLeaseRequest) -> _EngineRun:
        """Submit another engine's lease to this engine's line, as a run that stands for it."""
        engine_run = _EngineRun(
            engine_id=engine_id,
            lease_id=request.lease_id,
            run_id=request.run_id,
            granted=asyncio.get_running_loop().create_future(),
        )
        self._engine_runs[engine_run.key] = engine_run
        spec = _EngineRunSpec(manager=self, engine_run=engine_run, flow_name=f"a run from engine {engine_id}")
        engine_run.line_run = self.engine.flow_manager.submit_run(spec, flows=self._lease_line)
        return engine_run

    async def _end_engine_runs_from(self, engine_id: str, reason: str) -> None:
        """Let go of every lease from `engine_id`, holding this engine or waiting for it."""
        for engine_run in [run for run in self._engine_runs.values() if run.engine_id == engine_id]:
            await self._end_engine_run(engine_run, reason)

    async def _end_engine_run(self, engine_run: _EngineRun, reason: str) -> None:
        """Let another engine's lease go: one holding this engine releases it, and one still waiting leaves the line."""
        if engine_run.granted.done():
            engine_run.released.set()
            return
        # Refused first, so its acquire hears `reason` rather than the line's own cancellation.
        self.refuse_engine_run(engine_run, reason)
        if engine_run.line_run is not None:
            await self.engine.flow_manager.cancel_run(engine_run.line_run.run_id, flows=self._lease_line)

    def _forget_engine_run(self, engine_run: _EngineRun) -> None:
        if self._engine_runs.get(engine_run.key) is engine_run:
            del self._engine_runs[engine_run.key]

    def _engine_leasing(self, lease_id: str) -> str | None:
        """The engine this engine asked for a lease, or None if it asked for no such lease."""
        lease = self._leases.get(lease_id)
        if lease is None:
            return None
        return lease.host_engine_id

    async def _send_place_in_line(self, engine_id: str, lease_id: str, position: int) -> None:
        request = worker_events.ReportPlaceInLineRequest(lease_id=lease_id, position=position)
        try:
            result = await self.engine.request(engine_id, request, timeout_s=ENGINE_REQUEST_TIMEOUT_S)
        except EngineNotFoundError as e:
            logger.warning("Could not tell engine %s its run's place in line: %s", engine_id, e)
            return
        if not isinstance(result, worker_events.ReportPlaceInLineResultSuccess):
            logger.warning("Engine %s did not take its run's place in line: %s", engine_id, result.result_details)

    async def _report_until_answered(self, engine_id: str, request: RequestPayload, what: str) -> None:
        """Tell `engine_id` `what`, trying again until it answers or this engine has no session with it.

        Used for the reports that let that engine go, which nothing else does while it is connected, so a
        lost one would hold it from every other run.
        """
        for attempt in range(1, _REPORT_ATTEMPTS + 1):
            if attempt > 1:
                await asyncio.sleep(_REPORT_RETRY_PAUSE_S)
            if not self._has_session_with(engine_id):
                logger.info("Not telling engine %s that %s: this engine no longer uses it.", engine_id, what)
                return
            try:
                result = await self.engine.request(engine_id, request, timeout_s=ENGINE_REQUEST_TIMEOUT_S)
            except EngineNotFoundError as e:
                logger.warning(
                    "Could not tell engine %s that %s (attempt %d of %d): %s",
                    engine_id,
                    what,
                    attempt,
                    _REPORT_ATTEMPTS,
                    e,
                )
                continue
            if not result.succeeded():
                logger.warning("Engine %s did not take the report that %s: %s", engine_id, what, result.result_details)
            return
        logger.error(
            "Gave up telling engine %s that %s. It runs no one else's nodes until it loses its connection to "
            "this engine or this engine stops its workers.",
            engine_id,
            what,
        )

    def _has_session_with(self, engine_id: str) -> bool:
        """Whether this engine offered its session to `engine_id` and has not withdrawn it or lost that engine."""
        return any(host_engine_id == engine_id for host_engine_id, _session_id in self._engine_registrations)

    def _send_notice(self, notice: Coroutine[Any, Any, None]) -> None:
        """Send a report to another engine without waiting, holding the task until it is answered."""
        task = create_task_bound_to_no_workflow(asyncio.get_running_loop(), notice)
        self._engine_notices.add(task)
        task.add_done_callback(self._engine_notices.discard)

    async def _free_for_handoff(self, incoming_engine_id: str) -> None:
        """Free workers, as `distributed_execution.release` says, before this engine passes to `incoming_engine_id`'s run."""
        config = self.engine.config_manager
        defaults = ReleaseSettings()
        which = config.get_config_value(DISTRIBUTED_EXECUTION_RELEASE_WHICH_KEY, default=defaults.which)
        how = config.get_config_value(DISTRIBUTED_EXECUTION_RELEASE_HOW_KEY, default=defaults.how)
        if which == "none":
            return
        frees = []
        for worker_engine_id, registration in self._workers_served_for().items():
            if which == "others" and cast("_ServedFor", registration.serves).owning_engine_id == incoming_engine_id:
                continue
            if how == "recycle":
                # Its exit is reported stopped, so its engine starts it again before that library's next node.
                logger.info("Stopping worker %s to free this engine for another engine's run.", worker_engine_id)
                frees.append(self._recycle_served_worker(registration))
            else:
                frees.append(self._drop_library_named_objects(worker_engine_id, registration))
        # All at once, so a worker that never answers delays the handoff by one timeout, not one each. Each is
        # waited out even if another fails, so none is still stopping once the next lease is granted.
        outcomes = await asyncio.gather(*frees, return_exceptions=True)
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                logger.error("Could not free a worker for engine %s's run: %s", incoming_engine_id, outcome)

    async def _drop_library_named_objects(self, worker_engine_id: str, registration: WorkerRegistration) -> None:
        """Have a worker this engine runs drop what its libraries keep between runs, waiting until it has."""
        if registration.channel is None:
            logger.info("Worker %s has not connected, so it holds nothing to free.", worker_engine_id)
            return
        event = EventRequest(request=worker_events.DropLibraryNamedLocalObjectsRequest())
        try:
            result = await registration.channel.request(
                event_request_envelope(event), timeout_s=ENGINE_REQUEST_TIMEOUT_S
            )
        except TimeoutError:
            logger.warning(
                "Worker %s did not free its models within %.0f seconds.", worker_engine_id, ENGINE_REQUEST_TIMEOUT_S
            )
            return
        except ChannelClosedError:
            # Gone, so nothing of it is left to free.
            logger.info("Worker %s exited before it freed its models.", worker_engine_id)
            return
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

    def _prefixed_worker_command(self, library_name: str, command: list[str]) -> WorkerCommand | WorkerCommandRefusal:
        """`command`, which starts `library_name`'s worker, behind the configured `worker.command_prefix`."""
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

    async def _spawn_general_purpose_worker(self) -> None:
        """Spawn the general-purpose worker.

        It loads every library but builds none of their environments: the orchestrator built them
        while loading the same libraries, before it asked for any worker.

        Started with the engine's own interpreter and environment, and without
        `worker.command_prefix`: the prefix prepares an environment for one library, and this
        worker serves none. It runs where the engine runs, as Isolated Execution's separate process
        always has.
        """
        await self._spawn_local_worker([sys.executable, "-m", "griptape_nodes.worker"], None)

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

    def note_worker_unavailable(self, library_name: str | None, reason: str, *, retryable: bool = False) -> None:
        """Record why no worker will run `library_name`, and release anything waiting on one.

        Recording without releasing leaves the next run waiting out the whole startup grace before
        blaming a library load that never began; releasing without recording leaves it blaming a
        worker that "may still be starting up" for the rest of the session. A `retryable` reason has
        the library's next node ask for its worker again.
        """
        self._worker_unavailable[library_name] = _WorkerUnavailable(reason=reason, retryable=retryable)
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
        unavailable = self._worker_unavailable.get(library_name)
        if unavailable is None:
            return None
        return unavailable.reason

    def owning_engine_of(self, worker_engine_id: str) -> str | None:
        """The engine a worker this engine runs for another engine works for, or None if it runs no such worker."""
        registration = self._served_worker(worker_engine_id)
        if registration is None:
            return None
        return cast("_ServedFor", registration.serves).owning_engine_id

    def worker_host(self, worker_engine_id: str) -> str | None:
        """The engine that runs a worker this engine asked another engine for, or None for any other worker.

        Known from the moment the start is recorded, before it is sent, so traffic for a worker that registers
        before the start's answer arrives can already be carried to its engine.
        """
        start = self._starts_asked_of_hosts.get(worker_engine_id)
        if start is not None:
            return start.host_engine_id
        registration = self._own_worker(worker_engine_id)
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

    async def wait_until_executable(self, library_name: str | None, *, start_stopped: bool = True) -> None:
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
        worker started there again first, unless `start_stopped` is False: then such a library returns at
        once, as before a lease, which must not load the library there while another run holds that engine.
        """
        if library_name is not None:
            if start_stopped:
                await self._ask_host_to_restart_stopped_worker(library_name)
            elif self._host_stopped_remote_worker(library_name):
                return
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

    async def _ask_host_to_restart_stopped_worker(self, library_name: str) -> None:
        """Start a library's worker on its engine again, if that engine stopped it rather than it ending on its own."""
        if not self._host_stopped_remote_worker(library_name):
            return
        await self.handle_start_worker_request(worker_events.StartWorkerRequest(library_name=library_name))

    def _host_stopped_remote_worker(self, library_name: str) -> bool:
        """Whether `library_name`'s worker on another engine is stopped and may be started again there."""
        if self._engine_for_library(library_name) is None:
            return False
        if self.is_worker_running_or_starting(library_name):
            return False
        # A worker that ended on its own left a reason, which stays until the library is reset.
        unavailable = self._worker_unavailable.get(library_name)
        return unavailable is None or unavailable.retryable

    def get_topics_to_subscribe(self) -> list[str]:
        """Build the list of topics to subscribe to at connection start.

        The generic "request" topic (MCP/API entry point), this engine's own topic, and the session
        request topic.
        """
        engine_id = self.engine.get_engine_id()
        session_id = self.engine.get_session_id()

        topics: list[str] = []
        if engine_id:
            topics.append(engine_request_topic(engine_id))
        topics.append(REQUEST_TOPIC)
        if session_id:
            topics.append(session_request_topic(session_id))
        return topics

    async def forward_event_to_worker(self, event: EventRequest, *, worker_engine_id: str) -> None:
        """Send `event` to one worker without waiting for it to be handled."""
        registration = self._own_worker(worker_engine_id)
        if registration is None:
            logger.debug(
                "Not sending %s to worker %s: it is not running.", type(event.request).__name__, worker_engine_id
            )
            return
        logger.debug("Sending %s to worker %s", type(event.request).__name__, worker_engine_id)
        if registration.channel is not None:
            try:
                registration.channel.notify(event_request_envelope(event))
            except ChannelClosedError:
                logger.debug("Not sending %s to worker %s: it is gone.", type(event.request).__name__, worker_engine_id)
            return
        host_engine_id = cast("str", registration.host_engine_id)
        request = worker_events.SendToRemoteWorkerRequest(
            worker_engine_id=worker_engine_id, request=event_request_envelope(event), await_reply=False
        )
        try:
            result = await self.engine.request(host_engine_id, request, timeout_s=ENGINE_REQUEST_TIMEOUT_S)
        except EngineNotFoundError as e:
            logger.warning("Could not send %s to worker %s: %s", type(event.request).__name__, worker_engine_id, e)
            return
        if not result.succeeded():
            logger.warning(
                "%s did not pass %s on to worker %s: %s",
                host_engine_id,
                type(event.request).__name__,
                worker_engine_id,
                result.result_details,
            )

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

        if not self._own_workers():
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
        if not self._own_workers():
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
        if not keys or not self._own_workers():
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

    async def broadcast_local_object_teardown(self, closed_workflow_id: str | None = None) -> None:
        """Tell every worker to release named pending keys, then everything its libraries hold for the closed workflow.

        One method because both halves are required: the named-key broadcast is what drains the
        orchestrator's pending-release queue, and the drop-all is what covers whatever a worker still holds.
        The engine's close operations call this (`Engine.aclose_workflow`, `Engine.aclose_all_workflows`,
        and through `schedule_local_object_teardown`, `Engine.close_workflow`); none may take half of it.

        Naming `closed_workflow_id`, a worker releases only that workflow's objects and runtime. With
        no workflow named, every workflow was cleared: it releases everything.
        """
        await self.broadcast_pending_local_object_releases()
        await self.broadcast_drop_all_local_objects(workflow_id=closed_workflow_id)

    def schedule_local_object_teardown(self, closed_workflow_id: str | None) -> None:
        """Run `broadcast_local_object_teardown` for a sync teardown, on the engine loop.

        A sync close cannot await the fan-out. It goes to the engine loop rather than the caller's,
        because a sync request can run on a transient side loop, and a task left there dies with it.
        With no engine loop there is nothing to send it on, and the workers keep the objects until a
        later teardown.
        """
        if not self._own_workers():
            # Nothing to tell. Draining keeps the queue from growing for the life of the process.
            self.engine.resource_manager.drain_pending_worker_releases()
            return
        engine_loop = self.engine.event_manager.event_loop
        if engine_loop is None or engine_loop.is_closed():
            logger.warning(
                "Could not tell the workers to release the objects held for a closed workflow: the engine has "
                "no event loop to send on. A worker keeps them until a later teardown."
            )
            return
        teardown = self.broadcast_local_object_teardown(closed_workflow_id)
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is not engine_loop:
            asyncio.run_coroutine_threadsafe(teardown, engine_loop)
            return
        task = engine_loop.create_task(teardown)
        self._inflight_broadcast_tasks.add(task)
        task.add_done_callback(self._inflight_broadcast_tasks.discard)

    async def broadcast_drop_all_local_objects(self, *, workflow_id: str | None = None) -> None:
        """Tell every worker to release the objects its libraries parked in it for `workflow_id`, or all of them.

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

        if not self._own_workers():
            return
        try:
            await self.broadcast_to_workers(EventRequest(request=DropAllLocalObjectsRequest(workflow_id=workflow_id)))
        except Exception as e:
            logger.warning(
                "Could not tell the workers to release the objects held for their libraries: %s. "
                "A worker that did not get the message keeps them until a later teardown.",
                e,
            )

    async def _on_current_project_changed(self, _event: CurrentProjectChanged) -> None:
        """Fan out an ActivateProjectRequest after the orchestrator switched projects.

        It names no project: each worker asks which one when it adopts, so two switches in quick
        succession cannot leave a worker on the older. Awaited inline for the same side-loop reason
        documented on ``_on_config_changed``; lazy import for the same circular-dependency reason.
        """
        from griptape_nodes.app.worker_routing import ActivateProjectRequest

        if not self._own_workers():
            return
        project_id = self.engine.project_manager.committed_project_id
        failures = await self.broadcast_to_workers_awaiting_replies(EventRequest(request=ActivateProjectRequest()))
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
        `route_to_worker` has no ceiling of its own: a worker going away fails it, and a busy one
        does not, so a user-facing switch could wait forever. The bound is the startup grace, the
        same budget a worker's own slow startup gets.

        Each worker gets its own request id so replies cannot be confused. One worker's failure does
        not affect the others; the caller decides what a failure means.
        """
        recipients = self._broadcast_recipients(event)
        if not recipients:
            return []

        async def ask(worker_engine_id: str) -> str | None:
            per_worker = EventRequest(request=event.request)
            per_worker.request_id = str(uuid.uuid4())
            try:
                raw = await asyncio.wait_for(
                    self.route_to_worker(per_worker, worker_engine_id),
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

        outcomes = await asyncio.gather(*(ask(wid) for wid in recipients))
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
        if not self._own_workers():
            return
        event = EventRequest(request=request_type())
        task = asyncio.create_task(self.broadcast_to_workers(event))
        self._inflight_broadcast_tasks.add(task)
        task.add_done_callback(self._inflight_broadcast_tasks.discard)

    async def broadcast_to_workers(self, event: EventRequest) -> None:
        """Fire-and-forget fan out of an EventRequest to every registered worker.

        Used for orchestrator-originated notifications that every worker must
        act on locally (e.g. reload config). No response is awaited.

        Safe to call with zero registered workers -- it is a no-op. An event that cannot be
        serialized is logged and sent to no worker.
        """
        if not self._own_workers():
            return
        try:
            for wid in self._broadcast_recipients(event):
                await self.forward_event_to_worker(event, worker_engine_id=wid)
        except EventSerializationError:
            logger.exception("Could not broadcast %s to workers", type(event.request).__name__)

    def _broadcast_recipients(self, event: EventRequest) -> list[str]:
        """The workers a broadcast of `event` goes to: every one of this engine's, and the ones other engines run for it if they need it."""
        sent_to_remote = type(event.request).__name__ in _SENT_TO_REMOTE_WORKERS
        return [
            wid
            for wid, registration in self._own_workers().items()
            if registration.host_engine_id is None or sent_to_remote
        ]

    async def relay_worker_result(self, payload: dict) -> None:
        """Relay an unmatched worker result to the session topic.

        Called for worker results without a pending request.
        The orchestrator always mediates between workers and the GUI; workers never
        publish their results to clients directly. Clients tell a result's workflow by its `open_workflow_id`.
        """
        # 1 engine = 1 session, so the orchestrator's session topic reaches every client.
        relay_topic = default_response_topic(self.engine.get_engine_id(), self.engine.get_session_id())
        dest_socket = "success_result" if payload.get("event_type") == "EventResultSuccess" else "failure_result"
        # Names the topic it goes out on: a client drops a result whose `response_topic` is another topic
        # it takes, as a copy of a reply it already has.
        payload["response_topic"] = relay_topic
        logger.debug("Relaying %s to %s", payload.get("event_type"), relay_topic)
        # Untags the JSON rather than reading the result back, since its payload class may live in a
        # library only the worker has loaded. Only values carry tags, but a plain field holding its own
        # "$type" key would lose it here.
        await self._tx.send_message(dest_socket, json.dumps(untag(payload)), relay_topic)


def _worker_described(worker_key: str | None) -> str:
    """A worker this engine runs for itself, as an artist would name it."""
    if worker_key is None:
        return "the background process that runs Isolated Execution"
    return f"the worker process for library '{worker_key}'"


def _served_worker_envelope_refusal(
    envelope: dict[str, Any], allowed: frozenset[str], worker_engine_id: str
) -> str | None:
    """Why `envelope` may not be passed to the remote worker `worker_engine_id`, or None if it may.

    It may when it carries a single request of a type `allowed` names. Each way another engine reaches a
    remote worker checks here, against what that way may carry, so none passes on more than it should.
    """
    payload = envelope.get("payload")
    request_type = None
    if (
        envelope.get("type") == "EventRequest"
        and isinstance(payload, dict)
        and payload.get("event_type") == "EventRequest"
    ):
        request_type = payload.get("request_type")
    if request_type in allowed:
        return None
    return (
        f"Attempted to send worker {worker_engine_id} a '{request_type}'. Failed because this engine passes it "
        f"only {', '.join(sorted(allowed))} this way."
    )
