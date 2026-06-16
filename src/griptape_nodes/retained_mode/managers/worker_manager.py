from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from griptape_nodes.bootstrap.utils.subprocess_websocket_base import WebSocketMessage
from griptape_nodes.retained_mode.events import worker_events
from griptape_nodes.retained_mode.events.app_events import ConfigChanged, SecretChanged
from griptape_nodes.retained_mode.events.base_events import EventRequest
from griptape_nodes.retained_mode.managers.settings import (
    WORKER_HEARTBEAT_INTERVAL_KEY,
    WORKER_HEARTBEAT_STARTUP_GRACE_KEY,
    WORKER_HEARTBEAT_TIMEOUT_KEY,
)
from griptape_nodes.utils.version_utils import engine_version

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from griptape_nodes.api_client.request_client import RequestClient
    from griptape_nodes.retained_mode.events.base_events import RequestPayload
    from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
    from griptape_nodes.retained_mode.managers.event_manager import EventManager

logger = logging.getLogger("griptape_nodes_app")


@dataclass
class WorkerRegistration:
    """Tracks a registered worker's routing topic and optional library key.

    worker_key is the library_name the worker was spawned for, or None for
    general-purpose workers.
    """

    request_topic: str
    worker_key: str | None


@dataclass
class _WorkerTransport:
    """Transport-layer dependencies for WorkerManager.

    Held separately from WorkerManager so the manager can be constructed up
    front (e.g. by the GriptapeNodes singleton) and wired to a concrete
    transport later, once the WebSocket client and request client exist.
    """

    ws_outgoing_queue: asyncio.Queue
    send_message: Callable[[str, str, str | None], Awaitable[None]]
    subscribe_to_topic: Callable[[str], Awaitable[None]]
    unsubscribe_from_topic: Callable[[str], Awaitable[None]]
    request_client: RequestClient


class WorkerManager:
    """Manages worker registration, heartbeating, eviction, and event routing.

    Encapsulates all state and logic related to worker engines on both the
    orchestrator side (registry, heartbeat challenges, eviction, result relay)
    and the worker side (heartbeat response, self-termination monitor).

    Transport operations (subscribe, unsubscribe, send message) are injected
    as callables so this class has no direct dependency on WebSocket plumbing.
    """

    DEFAULT_HEARTBEAT_INTERVAL_S: float = 5.0
    DEFAULT_HEARTBEAT_TIMEOUT_S: float = 15.0
    # How long after spawn to wait before enforcing heartbeat timeout.
    # Workers install venv deps and import modules before receiving heartbeats;
    # this matches the _await_pending_workers() ceiling so a worker never kills
    # itself before the orchestrator gives up waiting for it.
    DEFAULT_HEARTBEAT_STARTUP_GRACE_S: float = 600.0
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
        griptape_nodes: GriptapeNodes,
        event_manager: EventManager,
    ) -> None:
        self._griptape_nodes = griptape_nodes
        self._event_manager = event_manager
        self._transport: _WorkerTransport | None = None

        # Orchestrator-side registry: worker_engine_id → WorkerRegistration
        self._workers: dict[str, WorkerRegistration] = {}

        # Subprocesses spawned by this orchestrator (library_name → process)
        self._managed_worker_processes: dict[str, asyncio.subprocess.Process] = {}

        # The event loop that spawned the worker subprocesses. asyncio.subprocess.Process
        # binds its exit Future to its creating loop, so proc.wait() is only legal on this
        # loop. Eviction can run on a different loop (the websocket-tasks loop), which is
        # why termination is hopped back here. Captured in spawn_worker.
        self._spawn_loop: asyncio.AbstractEventLoop | None = None

        # Orchestrator-side: worker_engine_id → monotonic timestamp of last heartbeat response
        self._worker_last_seen: dict[str, float] = {}

        # Worker-side: monotonic timestamp of last heartbeat received from the orchestrator
        self._worker_heartbeat_last_received_at: float = 0.0

        # Callbacks invoked when a worker is evicted: (worker_engine_id, library_name | None)
        self._worker_evicted_callbacks: list[Callable[[str, str | None], None]] = []

        # Fire-and-forget broadcast tasks scheduled from sync callers; held here so
        # the event loop's weak-ref to tasks does not GC them before completion.
        self._inflight_broadcast_tasks: set[asyncio.Task] = set()

        # Set when an active session becomes available; gates worker spawning.
        self._session_ready_event: asyncio.Event = asyncio.Event()

        config = griptape_nodes._config_manager
        self.heartbeat_interval_s: float = config.get_config_value(
            WORKER_HEARTBEAT_INTERVAL_KEY, default=WorkerManager.DEFAULT_HEARTBEAT_INTERVAL_S, cast_type=float
        )
        self.heartbeat_timeout_s: float = config.get_config_value(
            WORKER_HEARTBEAT_TIMEOUT_KEY, default=WorkerManager.DEFAULT_HEARTBEAT_TIMEOUT_S, cast_type=float
        )
        self.heartbeat_startup_grace_s: float = config.get_config_value(
            WORKER_HEARTBEAT_STARTUP_GRACE_KEY,
            default=WorkerManager.DEFAULT_HEARTBEAT_STARTUP_GRACE_S,
            cast_type=float,
        )

        event_manager.assign_manager_to_request_type(
            worker_events.RegisterWorkerRequest, self.handle_register_worker_request
        )
        event_manager.assign_manager_to_request_type(
            worker_events.WorkerHeartbeatRequest, self.handle_worker_heartbeat_request
        )
        event_manager.assign_manager_to_request_type(
            worker_events.UnregisterWorkerRequest, self.handle_unregister_worker_request
        )
        event_manager.assign_manager_to_request_type(worker_events.StartWorkerRequest, self.handle_start_worker_request)

        # Subscribe to domain events from ConfigManager / SecretsManager so
        # those managers don't have to know workers exist. The listeners are
        # the single place that translates a "something changed" signal into
        # a worker fan-out.
        event_manager.add_listener_to_app_event(ConfigChanged, self._on_config_changed)
        event_manager.add_listener_to_app_event(SecretChanged, self._on_secret_changed)

    @property
    def _tx(self) -> _WorkerTransport:
        if self._transport is None:
            msg = "WorkerManager transport has not been attached; call attach_transport() before use."
            raise RuntimeError(msg)
        return self._transport

    def attach_transport(
        self,
        *,
        ws_outgoing_queue: asyncio.Queue,
        send_message: Callable[[str, str, str | None], Awaitable[None]],
        subscribe_to_topic: Callable[[str], Awaitable[None]],
        unsubscribe_from_topic: Callable[[str], Awaitable[None]],
        request_client: RequestClient,
    ) -> None:
        """Bind the transport-layer callables used for WebSocket I/O.

        Called once the WebSocket client and RequestClient exist. Until this is
        called, methods that depend on the transport will raise RuntimeError.
        """
        self._transport = _WorkerTransport(
            ws_outgoing_queue=ws_outgoing_queue,
            send_message=send_message,
            subscribe_to_topic=subscribe_to_topic,
            unsubscribe_from_topic=unsubscribe_from_topic,
            request_client=request_client,
        )

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
            logger.error(details)
            return worker_events.RegisterWorkerResultFailure(result_details=details)

        session_id = self._griptape_nodes.get_session_id()
        request_topic = f"sessions/{session_id}/workers/{wid}/request"
        self._workers[wid] = WorkerRegistration(request_topic=request_topic, worker_key=request.library_name)
        self._worker_last_seen[wid] = time.monotonic()

        if request.library_name:
            logger.info("Worker registered: %s → library '%s'", wid, request.library_name)
        else:
            logger.info("Worker registered: %s (general-purpose)", wid)

        response_topic = f"sessions/{session_id}/workers/{wid}/response"
        await self._tx.subscribe_to_topic(response_topic)
        return worker_events.RegisterWorkerResultSuccess(
            worker_engine_id=wid, result_details="Worker registered successfully."
        )

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

    async def handle_unregister_worker_request(
        self,
        request: worker_events.UnregisterWorkerRequest,
    ) -> worker_events.UnregisterWorkerResultSuccess | worker_events.UnregisterWorkerResultFailure:
        """Handle a worker unregister request from a worker engine."""
        wid = request.worker_engine_id
        session_id = self._griptape_nodes.get_session_id()
        registration = self._workers.pop(wid, None)
        self._worker_last_seen.pop(wid, None)
        worker_key = registration.worker_key if registration else None
        response_topic = f"sessions/{session_id}/workers/{wid}/response"
        await self._tx.unsubscribe_from_topic(response_topic)
        # Remove the managed process entry so a new worker can be spawned for this key.
        if worker_key:
            removed = self._managed_worker_processes.pop(worker_key, None)
            if removed is not None:
                logger.debug(
                    "Worker unregistered: removed managed process for key '%s' (pid %s)", worker_key, removed.pid
                )
        logger.info("Worker unregistered: %s", wid)
        return worker_events.UnregisterWorkerResultSuccess(worker_engine_id=wid, result_details="Worker unregistered.")

    async def orchestrator_heartbeat_loop(self) -> None:
        """Challenge each registered worker on an interval; evict those that go silent."""
        while True:
            await asyncio.sleep(self.heartbeat_interval_s)
            if not self._workers:
                continue

            now = time.monotonic()
            stale = [
                wid
                for wid in list(self._workers)
                if now - self._worker_last_seen.get(wid, 0) > self.heartbeat_timeout_s
            ]
            for wid in stale:
                await self.evict_worker(wid)

            session_id = self._griptape_nodes.get_session_id()
            for wid, registration in list(self._workers.items()):
                hb = EventRequest(
                    request=worker_events.WorkerHeartbeatRequest(heartbeat_id=str(uuid.uuid4())),
                    response_topic=f"sessions/{session_id}/workers/{wid}/response",
                )
                await self._tx.ws_outgoing_queue.put(
                    WebSocketMessage("EventRequest", hb.json(), registration.request_topic)
                )

    async def worker_heartbeat_monitor(self) -> None:
        """Shut down the worker if orchestrator heartbeats stop arriving.

        Waits out a startup grace period before enforcing the timeout, so
        library loading (venv creation, pip install, module import) cannot kill
        the worker before the orchestrator has a chance to start sending
        challenges. Does not mutate `_worker_heartbeat_last_received_at`; that
        attribute is owned by `handle_worker_heartbeat_request`.
        """
        await asyncio.sleep(self.heartbeat_startup_grace_s)
        while True:
            await asyncio.sleep(self.heartbeat_interval_s)
            elapsed = time.monotonic() - self._worker_heartbeat_last_received_at
            if elapsed > self.heartbeat_timeout_s:
                msg = f"Orchestrator heartbeat lost ({elapsed:.1f}s since last heartbeat); worker is shutting down."
                logger.warning(msg)
                raise RuntimeError(msg)

    def get_worker_for_key(self, key: str) -> tuple[str, str] | None:
        """Return (worker_engine_id, worker_request_topic) for a worker registered under key, or None.

        Today returns the first registered worker for the key. Future versions can
        load-balance across multiple workers for the same key.
        """
        for wid, registration in self._workers.items():
            if registration.worker_key == key:
                return wid, registration.request_topic
        return None

    async def spawn_worker(self, args: list[str], worker_key: str) -> None:
        """Spawn a worker subprocess using the given command args.

        worker_key is an opaque identifier used to track the process and prevent
        duplicate spawns. Callers are responsible for constructing the args list.
        """
        if worker_key in self._managed_worker_processes:
            logger.error("Worker for key '%s' already spawned; refusing duplicate spawn.", worker_key)
            return
        proc = await asyncio.create_subprocess_exec(*args, env={**os.environ, "GTN_ENGINE_ID": str(uuid.uuid4())})
        # Record the loop that owns this subprocess so termination can hop back to it.
        # All spawns run on the engine event-queue loop, so this is idempotent.
        self._spawn_loop = asyncio.get_running_loop()
        self._managed_worker_processes[worker_key] = proc
        logger.info("Spawned worker for key '%s' (pid %s)", worker_key, proc.pid)

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
        await asyncio.gather(
            *(
                self._terminate_via_spawn_loop(library_name, proc)
                for library_name, proc in list(self._managed_worker_processes.items())
            )
        )
        session_id = self._griptape_nodes.get_session_id()
        if session_id and self._transport is not None:
            for wid in list(self._workers):
                response_topic = f"sessions/{session_id}/workers/{wid}/response"
                try:
                    await self._tx.unsubscribe_from_topic(response_topic)
                except Exception as e:
                    logger.debug("Failed to unsubscribe from '%s' during reset: %s", response_topic, e)
        self._managed_worker_processes.clear()
        self._workers.clear()
        self._worker_last_seen.clear()

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

        await self.forward_event_to_worker(
            event_request.model_copy(update={"request_id": request_id}),
            worker_engine_id=worker_engine_id,
            worker_request_topic=worker_request_topic,
        )
        # No wall-clock timeout here: long-running AI workloads (diffusion,
        # multi-pass refinement) routinely exceed any sensible default. Worker
        # liveness is enforced by the heartbeat loop, which evicts silent
        # workers and cancels their in-flight requests via
        # RequestClient.cancel_requests_by_tag, so a dead worker still surfaces
        # to the caller without a per-request ceiling.
        return await future

    async def evict_worker(self, worker_engine_id: str) -> None:
        """Remove a worker from the registry and unsubscribe from its response topic."""
        session_id = self._griptape_nodes.get_session_id()
        registration = self._workers.pop(worker_engine_id, None)
        self._worker_last_seen.pop(worker_engine_id, None)
        lib_name = registration.worker_key if registration else None
        topic = f"sessions/{session_id}/workers/{worker_engine_id}/response"
        await self._tx.unsubscribe_from_topic(topic)
        logger.warning("Worker evicted: %s", worker_engine_id)
        # Terminate the managed subprocess for this worker, if any.
        if lib_name:
            proc = self._managed_worker_processes.pop(lib_name, None)
            if proc is not None:
                await self._terminate_via_spawn_loop(lib_name, proc)
        # Cancel any requests that were awaiting a result from this worker.
        await self._tx.request_client.cancel_requests_by_tag(worker_engine_id)

        # Notify registered callbacks that this worker has been evicted.
        for cb in self._worker_evicted_callbacks:
            try:
                cb(worker_engine_id, lib_name)
            except Exception:
                logger.warning("Worker-evicted callback raised an exception for worker '%s'", worker_engine_id)

    async def _terminate_via_spawn_loop(self, library_name: str, proc: asyncio.subprocess.Process) -> None:
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
            # gets a kill signal that needs no await on the spawning loop.
            logger.warning(
                "Termination of worker for key '%s' was cancelled; sending a "
                "synchronous signal without awaiting exit confirmation",
                library_name,
            )
            self._terminate_without_wait(library_name, proc)
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

    async def _terminate_managed_process(self, library_name: str, proc: asyncio.subprocess.Process) -> None:
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
        logger.info("Terminated worker for key '%s' (pid %s)", library_name, proc.pid)
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

    def _terminate_without_wait(self, library_name: str, proc: asyncio.subprocess.Process) -> None:
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

    def register_worker_evicted_callback(self, callback: Callable[[str, str | None], None]) -> None:
        """Register a callback invoked when a worker is evicted.

        Callbacks are called synchronously in registration order. Exceptions are logged
        but do not prevent other callbacks from running.

        Callback signature: (worker_engine_id: str, library_name: str | None) -> None
        """
        self._worker_evicted_callbacks.append(callback)

    def set_session_ready(self) -> None:
        """Signal that a session is available, unblocking any pending worker spawns."""
        self._session_ready_event.set()

    def clear_session_ready(self) -> None:
        """Clear the session-ready gate so future worker spawns wait for a new session."""
        self._session_ready_event.clear()

    async def handle_start_worker_request(
        self, request: worker_events.StartWorkerRequest
    ) -> worker_events.StartWorkerResultSuccess | worker_events.StartWorkerResultFailure:
        """Schedule a worker subprocess spawn for the given library.

        Returns immediately; the actual spawn runs once a session becomes available.
        """
        task = asyncio.get_running_loop().create_task(self._spawn_when_session_ready(request.library_name))
        task.add_done_callback(functools.partial(self._log_spawn_error, library_name=request.library_name))
        return worker_events.StartWorkerResultSuccess(result_details="Worker spawn scheduled.")

    async def _spawn_when_session_ready(self, library_name: str) -> None:
        """Wait for an active session then spawn a worker subprocess for the given library."""
        # If a session is already active, skip the wait entirely.
        if not self._griptape_nodes.get_session_id():
            logger.info(
                "Worker for library '%s' is waiting for a session to start before spawning. "
                "Start a session (via the Griptape Nodes GUI or AppStartSessionRequest) to proceed.",
                library_name,
            )
            await self._session_ready_event.wait()
            logger.info("Session started; spawning worker for library '%s'.", library_name)
        session_id = self._griptape_nodes.get_session_id()
        if not session_id:
            logger.error("Session event set but no session ID available for library '%s'.", library_name)
            return
        args = [
            sys.executable,
            "-m",
            "griptape_nodes_app",
            "engine",
            "--session-id",
            session_id,
            "--library-name",
            library_name,
        ]
        await self.spawn_worker(args, library_name)

    @staticmethod
    def _log_spawn_error(task: asyncio.Task, library_name: str) -> None:
        exc = task.exception()
        if exc is not None:
            logger.error("Failed to spawn worker for library '%s': %s", library_name, exc)

    def get_topics_to_subscribe(self, *, is_worker: bool) -> list[str]:
        """Build the list of topics to subscribe to at connection start.

        In worker mode the engine subscribes only to its dedicated per-worker request topic
        and its direct-target engine topic. Workers must NOT subscribe to the generic "request"
        topic, which is where the MCP server broadcasts; doing so causes workers to handle
        requests intended for the orchestrator.

        In orchestrator mode it subscribes to the generic "request" topic (MCP/API entry point)
        and the session request topic.
        """
        engine_id = self._griptape_nodes.get_engine_id()
        session_id = self._griptape_nodes.get_session_id()

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
        session_id = self._griptape_nodes.get_session_id()
        worker_response_topic = f"sessions/{session_id}/workers/{worker_engine_id}/response"
        forwarded = event.model_copy(update={"response_topic": worker_response_topic})
        logger.debug("Forwarding %s to worker %s", type(event.request).__name__, worker_engine_id)
        await self._tx.send_message("EventRequest", forwarded.json(), worker_request_topic)

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

        Safe to call with zero registered workers -- it is a no-op.
        """
        if not self._workers:
            return
        for wid, registration in list(self._workers.items()):
            await self.forward_event_to_worker(
                event,
                worker_engine_id=wid,
                worker_request_topic=registration.request_topic,
            )

    async def relay_worker_result(self, payload: dict) -> None:
        """Relay an unmatched worker result to the GUI session response topic.

        Called for worker result messages not claimed by RequestClient
        (heartbeats and any results without a pending request).
        The orchestrator always mediates between workers and the GUI; workers never
        publish directly to the session response topic.
        """
        # Heartbeat responses update the last-seen timestamp but are not forwarded to the GUI.
        # BaseEvent.dict() adds result_type at the outer level (not inside the result dict).
        result_event_type = payload.get("result_type", "")
        if result_event_type == worker_events.WorkerHeartbeatResultSuccess.__name__:
            if m := self._WORKER_RESPONSE_TOPIC_RE.match(payload.get("response_topic", "")):
                worker_engine_id = m.group("worker_engine_id")
                self._worker_last_seen[worker_engine_id] = time.monotonic()
                logger.debug("Heartbeat received from worker %s", worker_engine_id)
            return  # Internal health check — do not forward to GUI

        # 1 engine = 1 session — the orchestrator's session response topic is always the right target.
        session_response_topic = self._determine_response_topic()
        dest_socket = "success_result" if payload.get("event_type") == "EventResultSuccess" else "failure_result"
        payload["response_topic"] = session_response_topic
        logger.debug("Relaying %s to %s", payload.get("event_type"), session_response_topic)
        await self._tx.send_message(dest_socket, json.dumps(payload), session_response_topic)

    def _determine_response_topic(self) -> str:
        """Determine the response topic based on current session and engine IDs."""
        session_id = self._griptape_nodes.get_session_id()
        if session_id:
            return f"sessions/{session_id}/response"
        engine_id = self._griptape_nodes.get_engine_id()
        if engine_id:
            return f"engines/{engine_id}/response"
        return "response"
