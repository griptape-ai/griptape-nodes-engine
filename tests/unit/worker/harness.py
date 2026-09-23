"""In-process harness for exercising orchestrator<->worker event routing.

Motivation
----------
`WorkerManager.route_to_worker` and `EventManager.ahandle_request` connect via
a WebSocket pub/sub transport in production. That transport is overkill for
unit tests of the routing contract itself: two EventManagers (one "orchestrator"
side and one "worker" side) can be connected in-memory and exercise the same
code paths up to the transport boundary. Fake that transport with an asyncio
queue + a pending-futures map and the tests observe exactly what the wire
contract is supposed to do.

What this harness covers
------------------------
- Orchestrator initiates an `ExecuteNodeRequest` by calling the harness's
  `route_to_worker`, which simulates the WebSocket round-trip and resolves
  with the worker's real `EventManager.ahandle_request` result.
- Worker-side forwarding of orchestrator-owned requests back to the
  orchestrator is wired by registering a test RemoteHandler (mirrors the
  production `register_remote_handlers` swap) that, when the worker is inside
  a `node_execution_scope`, dispatches to the orchestrator-side
  `EventManager.ahandle_request` instead of running locally.

Intentional limits
------------------
No WebSocket, no subprocess, no MQTT payload shape details. Serialization
faithfulness (converters/validators/traits across the JSON boundary -- see
issue #4472/A and #4475/D) belongs in a separate harness that exercises the
on-the-wire dict shape. Here we go straight Python-object to Python-object,
which is enough for routing/concurrency/forwarding coverage.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from griptape_nodes.retained_mode.managers.event_manager import EventManager

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from griptape_nodes.retained_mode.events.base_events import EventRequest, RequestPayload, ResultPayload


@dataclass
class InProcessWorkerHarness:
    """Two EventManagers connected by an in-memory bus.

    `orchestrator` is the side that initiates node-execution requests and
    holds the live node (the same shape as a live session). `worker` is the
    side that actually runs `aprocess`. Handlers registered on either side
    are addressed via the regular `assign_manager_to_request_type` API.

    Call `start()` to spin up the worker dispatch task, `stop()` to join it.
    Inside a test, prefer the `running()` async context manager.
    """

    orchestrator: EventManager = field(default_factory=EventManager)
    worker: EventManager = field(default_factory=EventManager)
    _pending: dict[str, asyncio.Future[dict[str, Any]]] = field(default_factory=dict, init=False)
    _worker_inbox: asyncio.Queue[EventRequest] | None = field(default=None, init=False)
    _worker_task: asyncio.Task[None] | None = field(default=None, init=False)

    def install_remote_handler(self, request_type: type[RequestPayload]) -> None:
        """Register a RemoteHandler on the worker for ``request_type``.

        Mirrors production ``register_remote_handlers``: while the worker side is
        inside a ``node_execution_scope``, dispatches to the orchestrator-
        side EventManager. Outside the scope, delegates to whatever handler was
        already registered on the worker (if any).
        """

        async def _forward(request: RequestPayload) -> ResultPayload:
            result_event = await self.orchestrator.ahandle_request(request)
            return result_event.result

        original = self.worker.get_manager_for_request_type(request_type)

        async def remote(request: RequestPayload) -> ResultPayload:
            if self.worker.in_node_execution():
                return await _forward(request)
            if original is None:
                msg = (
                    f"Harness RemoteHandler for {request_type.__name__} was invoked "
                    "outside a node_execution_scope and no local worker handler is registered."
                )
                raise RuntimeError(msg)
            return await _invoke_handler(original, request)

        if original is not None:
            self.worker.remove_manager_from_request_type(request_type)
        self.worker.assign_manager_to_request_type(request_type, remote)

    async def start(self) -> None:
        """Start the worker-side dispatch loop. Safe to call multiple times."""
        if self._worker_task is not None:
            return
        self._worker_inbox = asyncio.Queue()
        self._worker_task = asyncio.create_task(self._run_worker_loop(), name="harness-worker-dispatch")

    async def stop(self) -> None:
        """Cancel the worker loop and drain pending futures with a failure."""
        if self._worker_task is not None:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._worker_task
            self._worker_task = None
        for request_id, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(RuntimeError("Harness stopped before worker responded."))
            self._pending.pop(request_id, None)
        self._worker_inbox = None

    async def route_to_worker(self, event_request: EventRequest) -> dict[str, Any]:
        """Orchestrator side: send a request to the worker, await the result dict.

        Mirrors `WorkerManager.route_to_worker`'s return shape so tests can swap
        one for the other via dependency injection or monkeypatch.
        """
        if self._worker_inbox is None:
            msg = "Harness not started. Call `start()` or use `running()`."
            raise RuntimeError(msg)

        request_id = event_request.request_id or str(uuid.uuid4())
        event_request = event_request.model_copy(update={"request_id": request_id})

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future
        await self._worker_inbox.put(event_request)
        try:
            return await future
        finally:
            self._pending.pop(request_id, None)

    async def running(self) -> AsyncIterator[InProcessWorkerHarness]:
        """Usage: `async with harness.running() as h:`. Starts and stops the loop."""
        await self.start()
        try:
            yield self
        finally:
            await self.stop()

    async def _run_worker_loop(self) -> None:
        assert self._worker_inbox is not None
        while True:
            event_request = await self._worker_inbox.get()
            asyncio.create_task(self._handle_one(event_request))  # noqa: RUF006

    async def _handle_one(self, event_request: EventRequest) -> None:
        request_id = event_request.request_id or ""
        future = self._pending.get(request_id)
        if future is None or future.done():
            # Orchestrator side gave up before the worker got here; drop silently.
            return
        try:
            result_event = await self.worker.ahandle_request(event_request.request)
        except Exception as exc:
            future.set_exception(exc)
            return
        # Match the on-the-wire shape: {event_type, result_type, result}.
        result = result_event.result
        future.set_result(
            {
                "event_type": type(result_event).__name__,
                "result_type": type(result).__name__,
                "result": _payload_to_dict(result),
                "request_id": request_id,
            }
        )


async def _invoke_handler(handler: Any, request: RequestPayload) -> ResultPayload:
    """Call a handler that may be sync or async; mirrors ``call_function`` without the import cycle."""
    import inspect

    if inspect.iscoroutinefunction(handler):
        return await handler(request)
    return handler(request)


def _payload_to_dict(payload: Any) -> dict[str, Any]:
    """Minimal payload-to-dict: tests compare the live object, not a serialized form.

    Keeps the harness decoupled from cattrs/converter registration by shipping
    the raw instance under a `_payload_object` key. Production goes through
    cattrs + PayloadRegistry; tests that care about serialization fidelity
    belong next to issue #4475 (serialization contract) rather than here.
    """
    return {"_payload_object": payload}


def build_result_from_route_to_worker_dict(
    payload: dict[str, Any],
    *,
    success_type: type,
    failure_type: type,
) -> Any:
    """Unpack a harness `route_to_worker` dict into the declared success/failure type.

    Production code calls `WorkerManager.route_to_worker` and then constructs
    `ExecuteNodeResultSuccess(**result["result"])`. The harness returns the
    same shape except the `result` is `{"_payload_object": <instance>}` so that
    tests keep the real instance. Tests use this helper to unpack, mirroring
    `NodeManager._execute_node_via_worker`.
    """
    result_type_name = payload.get("result_type", "")
    raw = payload.get("result", {})
    if "_payload_object" in raw:
        return raw["_payload_object"]
    if result_type_name == success_type.__name__:
        return success_type(**raw)
    return failure_type(**raw)


__all__ = [
    "InProcessWorkerHarness",
    "build_result_from_route_to_worker_dict",
]
