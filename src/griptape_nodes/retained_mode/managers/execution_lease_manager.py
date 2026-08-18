"""Engine-side client of the execution lease protocol.

On a shared GPU machine, an external admission authority (the Load Balancer)
decides when each engine's execution may start. This manager is that
authority's *client* inside the engine: it acquires an execution lease at the
gate (the moment `check_for_existing_running_flow()` would transition
False -> True), owns the release watchdog, and keeps the lease renewed while a
run is in flight. It decides nothing itself and never executes anything -- it
asks, waits, and releases.

Managed execution is off by default (`execution_lease.enabled`); an unmanaged
engine behaves exactly as before, every gate call a no-op. When enabled, the
engine **fails closed**: no reachable admission authority means no execution.

Release is never tied to request-handler control flow. In debug mode the
handler returns while the run is still live and holding VRAM, so a per-acquire
watchdog task owns release, keyed on the same running-flow signal
`_await_flow_completion` polls. Callers report start failures explicitly
(`on_execution_start_failed`) so an acquire whose run never launched is
returned immediately instead of waiting out the watchdog's startup grace.

Transport is injected (`attach_transport`), mirroring WorkerManager's
`_WorkerTransport`: this module has no dependency on WebSocket plumbing, and
tests drive it with an in-memory stub balancer.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events import execution_lease_events
from griptape_nodes.retained_mode.events.event_converter import converter
from griptape_nodes.retained_mode.managers.settings import (
    EXECUTION_LEASE_BALANCER_GRACE_KEY,
    EXECUTION_LEASE_BALANCER_TIMEOUT_KEY,
    EXECUTION_LEASE_ENABLED_KEY,
    EXECUTION_LEASE_RENEW_INTERVAL_KEY,
    EXECUTION_LEASE_TEARDOWN_TIMEOUT_KEY,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from griptape_nodes.retained_mode.engine import Engine

logger = logging.getLogger("griptape_nodes_app")


@dataclass
class _LeaseTransport:
    """Transport-layer dependencies for ExecutionLeaseManager.

    Held separately from the manager so it can be constructed up front (by the
    Engine) and wired to a concrete transport later, once the balancer link
    exists. `send_request` submits a lease-protocol request payload dict and
    returns the raw result payload dict; the acquire call deliberately has no
    wall-clock timeout (queue waits are legitimately unbounded), so the
    callable must not impose one.
    """

    send_request: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
    send_no_response: Callable[[str, dict[str, Any]], Awaitable[None]]


class ExecutionLeaseManager(EngineScoped):
    """Acquires, renews, and releases this engine's execution lease.

    Boundary: `FlowManager` runs flows; the Load Balancer (separate process,
    separate repo) decides admission; this manager only speaks the lease
    protocol between them. See `execution_lease_events` for the wire contract.
    """

    # Poll cadence for the running-flow signal, matching _await_flow_completion.
    _POLL_INTERVAL_S: float = 0.05

    # How long the watchdog waits to observe the run actually starting before
    # concluding it never will and returning the lease. A backstop only:
    # callers report start failures explicitly and release immediately; this
    # covers a caller that dies without reporting.
    _STARTUP_GRACE_S: float = 60.0

    DEFAULT_RENEW_INTERVAL_S: float = 30.0

    DEFAULT_TEARDOWN_TIMEOUT_S: float = 120.0

    DEFAULT_BALANCER_TIMEOUT_S: float = 30.0

    # Generous: covers engine boot (library load) plus the balancer linking and
    # sending its first beacon, mirroring the worker heartbeat startup grace.
    DEFAULT_BALANCER_GRACE_S: float = 600.0

    # After asking the process to shut down gracefully, how long to wait before
    # forcing the exit -- a wedged shutdown must not leave a zombie engine
    # claiming GPU memory on a box the balancer has already given up on.
    _TERMINATE_ESCALATION_S: float = 15.0

    def __init__(self, *, engine: Engine) -> None:
        super().__init__(engine)
        self._transport: _LeaseTransport | None = None

        # The held lease id, or None. Guarded by _state_lock together with
        # _acquire_pending so concurrent gate calls serialize their decisions.
        self._lease_id: str | None = None
        self._lease_scope: str = "workflow"
        self._acquire_pending: bool = False
        self._lease_lost: bool = False
        self._state_lock = asyncio.Lock()

        # Per-acquire watchdog task; cancelled and replaced when a held lease
        # is reused for a follow-on run (queued starts draining back-to-back).
        self._watchdog_task: asyncio.Task | None = None

        # Monotonic timestamp of the last balancer beacon; 0.0 = never seen.
        # Written only by _on_balancer_heartbeat.
        self._balancer_last_seen: float = 0.0

        config = engine.config_manager
        self.enabled: bool = config.get_config_value(EXECUTION_LEASE_ENABLED_KEY, default=False, cast_type=bool)
        self.balancer_timeout_s: float = config.get_config_value(
            EXECUTION_LEASE_BALANCER_TIMEOUT_KEY,
            default=ExecutionLeaseManager.DEFAULT_BALANCER_TIMEOUT_S,
            cast_type=float,
        )
        self.balancer_grace_s: float = config.get_config_value(
            EXECUTION_LEASE_BALANCER_GRACE_KEY,
            default=ExecutionLeaseManager.DEFAULT_BALANCER_GRACE_S,
            cast_type=float,
        )
        if self.enabled:
            engine.event_manager.add_listener_to_app_event(
                execution_lease_events.ExecutionBalancerHeartbeatEvent, self._on_balancer_heartbeat
            )
        self.renew_interval_s: float = config.get_config_value(
            EXECUTION_LEASE_RENEW_INTERVAL_KEY,
            default=ExecutionLeaseManager.DEFAULT_RENEW_INTERVAL_S,
            cast_type=float,
        )
        self.teardown_timeout_s: float = config.get_config_value(
            EXECUTION_LEASE_TEARDOWN_TIMEOUT_KEY,
            default=ExecutionLeaseManager.DEFAULT_TEARDOWN_TIMEOUT_S,
            cast_type=float,
        )

    @property
    def lease_id(self) -> str | None:
        """The currently held lease id, or None when no lease is held."""
        return self._lease_id

    def attach_transport(
        self,
        *,
        send_request: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
        send_no_response: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Bind the transport callables used to reach the admission authority.

        Called by the app layer once the balancer link exists. Until then, a
        gate call on an enabled engine fails closed.
        """
        self._transport = _LeaseTransport(send_request=send_request, send_no_response=send_no_response)

    async def gate_execution_start(self, *, scope: str = "workflow") -> None:
        """Acquire the execution lease; called exactly where a run is about to start.

        No-op when managed execution is disabled. When enabled:

        - Raises immediately if this engine already has an acquire in flight
          (two near-simultaneous run requests both passed the running-flow
          guard; the second must not queue a stale run).
        - Reuses the held lease when one exists (queued starts draining
          back-to-back before the watchdog observed idle), restarting the
          watchdog for the new run.
        - Otherwise sends the acquire and waits -- unboundedly -- for the
          grant. Cancellation of the waiting caller abandons the acquire via
          CancelExecutionLeaseRequest.
        - After the grant, re-checks the running-flow signal: if another run
          started while this one waited, the lease is returned and the start
          is refused.

        Raises:
            RuntimeError: If execution is refused -- duplicate start, admission
                authority unreachable or refusing, or a run raced this one.
        """
        if not self.enabled:
            return

        if self._transport is None:
            msg = (
                "Attempted to start execution on a managed engine. Failed because the "
                "execution manager (load balancer) link is not connected; this engine "
                "refuses to run unmanaged. Contact your administrator."
            )
            raise RuntimeError(msg)

        # The balancer dials in, so "is it connected" is only observable via
        # its beacons. Without this check an acquire would wait forever on a
        # topic nobody is subscribed to -- fail closed with a message instead.
        if self._balancer_last_seen == 0.0:
            msg = (
                "Attempted to start execution on a managed engine. Failed because no "
                "load balancer has connected to this engine yet; this engine refuses "
                "to run unmanaged. Contact your administrator."
            )
            raise RuntimeError(msg)

        async with self._state_lock:
            if self._acquire_pending:
                msg = (
                    "Attempted to start execution. Failed because another run request "
                    "is already waiting for its turn. Cancel it or wait for it to start."
                )
                raise RuntimeError(msg)
            if self._lease_id is not None:
                self._restart_watchdog()
                return
            self._acquire_pending = True
            lease_id = uuid.uuid4().hex

        request = execution_lease_events.AcquireExecutionLeaseRequest(
            engine_id=self._engine_id(),
            lease_id=lease_id,
            session_id=self.engine.session_manager.active_session_id,
            scope=scope,
        )
        try:
            result = await self._transport.send_request(type(request).__name__, self._payload_of(request))
        except asyncio.CancelledError:
            # The waiting caller gave up its place in line; tell the balancer.
            await self._send_cancel(lease_id)
            raise
        except Exception as e:
            msg = (
                "Attempted to start execution. Failed because the execution manager "
                f"(load balancer) could not be reached: {e}. This engine refuses to run unmanaged."
            )
            raise RuntimeError(msg) from e
        finally:
            async with self._state_lock:
                self._acquire_pending = False

        if result.get("result_type") != execution_lease_events.AcquireExecutionLeaseResultSuccess.__name__:
            msg = f"Attempted to start execution. The execution manager refused: {self._details_of(result)}"
            raise RuntimeError(msg)

        async with self._state_lock:
            self._lease_id = lease_id
            self._lease_scope = scope
            self._lease_lost = False
            # A run that started while this acquire waited wins; return the
            # lease rather than piling a second run onto the engine.
            if self.engine.flow_manager.check_for_existing_running_flow():
                await self._release_locked("returned: another run started while waiting")
                msg = (
                    "Attempted to start execution. Failed because another run started "
                    "while this one was waiting for its turn."
                )
                raise RuntimeError(msg)
            self._restart_watchdog()

    async def on_execution_start_failed(self) -> None:
        """Return the lease immediately after a run failed to launch.

        Called from the gate sites' exception paths so a failed start does not
        hold admission until the watchdog's startup grace expires.
        """
        if not self.enabled:
            return
        self._cancel_watchdog()
        async with self._state_lock:
            if self._lease_id is not None:
                await self._release_locked("returned: execution failed to start")

    async def _run_pre_release_teardown(self) -> None:
        """Release execution-scoped memory before the lease is returned.

        Ordering is the point: teardown happens BEFORE the release is sent, so
        the next admitted engine starts against a reclaimed machine. Broadcasts
        ExecutionLeaseReleasing and awaits every listener (libraries clearing
        their pipeline caches). A failing listener is logged and release
        proceeds; a hung one is abandoned at the teardown timeout -- either
        way, a broken teardown must not hold the admission queue forever.
        """
        if self._lease_id is None:
            return
        event = execution_lease_events.ExecutionLeaseReleasing(
            lease_id=self._lease_id,
            scope=self._lease_scope,
        )
        try:
            await asyncio.wait_for(
                self.engine.event_manager.abroadcast_app_event(event),
                timeout=self.teardown_timeout_s,
            )
        except TimeoutError:
            logger.error(
                "Execution memory teardown did not finish within %.0fs; releasing the lease anyway. "
                "The machine may still hold this run's memory.",
                self.teardown_timeout_s,
            )
        except ExceptionGroup:
            logger.exception(
                "Execution memory teardown listener(s) failed; releasing the lease anyway. "
                "The machine may still hold this run's memory."
            )

    def _restart_watchdog(self) -> None:
        """(Re)start the release watchdog for a new run under the current lease."""
        self._cancel_watchdog()
        self._watchdog_task = asyncio.create_task(self._watchdog())

    def _cancel_watchdog(self) -> None:
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            self._watchdog_task = None

    async def _watchdog(self) -> None:
        """Own the lease release: wait for the run to start, then to finish.

        Never keyed on handler control flow -- debug mode returns from the
        handler mid-run. Renewal rides the same loop while the run is live.
        """
        flow_manager = self.engine.flow_manager

        # Phase 1: observe the run starting. A fast run may start and finish
        # between polls; the startup grace bounds how long an unobserved lease
        # is held before being returned.
        started = False
        deadline = time.monotonic() + ExecutionLeaseManager._STARTUP_GRACE_S
        while time.monotonic() < deadline:
            if flow_manager.check_for_existing_running_flow():
                started = True
                break
            await asyncio.sleep(ExecutionLeaseManager._POLL_INTERVAL_S)

        # Phase 2: while the run is live, keep the lease renewed; release when
        # the running-flow signal goes idle (completion, error, or cancel all
        # land there).
        if started:
            last_renew = time.monotonic()
            while flow_manager.check_for_existing_running_flow():
                if time.monotonic() - last_renew >= self.renew_interval_s:
                    last_renew = time.monotonic()
                    await self._renew()
                await asyncio.sleep(ExecutionLeaseManager._POLL_INTERVAL_S)

        await self._run_pre_release_teardown()
        async with self._state_lock:
            if self._lease_id is not None:
                await self._release_locked("run complete")
        self._watchdog_task = None

    async def _renew(self) -> None:
        """Extend the lease TTL; a refused renewal means the lease is lost."""
        if self._transport is None or self._lease_id is None or self._lease_lost:
            return
        request = execution_lease_events.RenewExecutionLeaseRequest(lease_id=self._lease_id)
        try:
            result = await self._transport.send_request(type(request).__name__, self._payload_of(request))
        except Exception as e:
            logger.warning("Execution lease renewal could not reach the load balancer: %s", e)
            return
        if result.get("result_type") != execution_lease_events.RenewExecutionLeaseResultSuccess.__name__:
            # Lease reclaimed out from under a live run. The run itself is not
            # killed here; the engine stops claiming admission and must
            # re-acquire before any further execution.
            self._lease_lost = True
            logger.error(
                "Execution lease was not renewed (%s); this engine no longer holds admission.",
                self._details_of(result),
            )

    async def _release_locked(self, reason: str) -> None:
        """Send the release for the held lease. Caller must hold _state_lock."""
        lease_id = self._lease_id
        self._lease_id = None
        if self._transport is None or lease_id is None:
            return
        if self._lease_lost:
            # The balancer already reclaimed it; nothing to return.
            self._lease_lost = False
            return
        request = execution_lease_events.ReleaseExecutionLeaseRequest(lease_id=lease_id)
        try:
            result = await self._transport.send_request(type(request).__name__, self._payload_of(request))
        except Exception as e:
            logger.warning("Execution lease release could not reach the load balancer: %s", e)
            return
        if result.get("result_type") != execution_lease_events.ReleaseExecutionLeaseResultSuccess.__name__:
            # Already reclaimed (crash eviction won a race) -- treat as released.
            logger.info("Execution lease release answered: %s", self._details_of(result))
        else:
            logger.debug("Execution lease released (%s)", reason)

    async def _send_cancel(self, lease_id: str) -> None:
        """Abandon a waiting acquire; fire-and-forget by design."""
        if self._transport is None:
            return
        request = execution_lease_events.CancelExecutionLeaseRequest(lease_id=lease_id)
        with contextlib.suppress(Exception):
            await self._transport.send_no_response(type(request).__name__, self._payload_of(request))

    def _engine_id(self) -> str:
        engine_id = self.engine.engine_identity_manager.active_engine_id
        return engine_id if engine_id is not None else "unknown-engine"

    async def run_balancer_liveness_monitor(self) -> None:
        """Self-terminate when balancer beacons stop: engines die with their balancer.

        The balancer dials in and connection state is invisible to Python, so
        beacon receipt is the only liveness signal. After the startup grace
        (covering boot + first link), a beacon gap past the timeout means the
        admission authority is gone -- and a brokered engine without one must
        not linger: it cannot run (fail closed) and would only hold memory.
        Termination goes through the process's own signal handlers so shutdown
        is graceful, with a hard exit escalation if that wedges.

        Mirrors worker_heartbeat_monitor's shape; a no-op on unmanaged engines.
        """
        if not self.enabled:
            return
        await asyncio.sleep(self.balancer_grace_s)
        poll_s = max(0.05, min(self.balancer_timeout_s / 3.0, 10.0))
        while True:
            await asyncio.sleep(poll_s)
            last_seen = self._balancer_last_seen
            elapsed = time.monotonic() - last_seen if last_seen else float("inf")
            if elapsed > self.balancer_timeout_s:
                logger.critical(
                    "Load balancer heartbeat lost (%s); this engine is shutting down "
                    "(managed engines do not outlive their admission authority).",
                    f"{elapsed:.0f}s since last beacon" if last_seen else "never connected",
                )
                await self._terminate_process()
                return

    def _on_balancer_heartbeat(self, _event: Any) -> None:
        self._balancer_last_seen = time.monotonic()

    async def _terminate_process(self) -> None:
        """Ask this process to shut down; force the exit if that wedges."""
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.sleep(ExecutionLeaseManager._TERMINATE_ESCALATION_S)
        logger.critical("Graceful shutdown did not complete; forcing exit.")
        os._exit(1)

    @staticmethod
    def _payload_of(request: Any) -> dict[str, Any]:
        return converter.unstructure(request)

    @staticmethod
    def _details_of(result: dict[str, Any]) -> str:
        """Flatten a result envelope's human-readable details.

        On the wire, a result payload's ``result_details`` is the unstructured
        ``ResultDetails`` object -- ``{"result_details": [{"level", "message"}]}``
        nested under the envelope's ``result`` key -- not a plain string.
        """
        details = result.get("result", {}).get("result_details", "")
        if isinstance(details, str):
            return details or "no reason given"
        if isinstance(details, dict):
            details = details.get("result_details", [])
        if isinstance(details, list):
            messages = [entry.get("message", "") for entry in details if isinstance(entry, dict)]
            return " ".join(m for m in messages if m) or "no reason given"
        return "no reason given"
