"""This engine's end of the channel to a library worker it runs for another engine, the artist's.

The worker works for the artist's engine, so this end decides nothing it asks for: each request goes
to the artist's engine as the request it is, each checkpoint is decided there, and its events reach
the artist's engine in ordered batches, passed on as the worker sent them. Only the worker's hello is
answered here, since the process is this engine's.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import TYPE_CHECKING, Any

from griptape_nodes.retained_mode.engine_transports import EngineNotFoundError
from griptape_nodes.retained_mode.events.base_events import EventResultFailure, EventResultSuccess
from griptape_nodes.retained_mode.events.worker_events import (
    DecideWorkerCheckpointRequest,
    DecideWorkerCheckpointResultSuccess,
    DeliverWorkerEventsRequest,
)
from griptape_nodes.retained_mode.runtime.binding import NO_WORKFLOW, bound_workflow
from griptape_nodes.worker.channel import GateDecision
from griptape_nodes.worker.wire import (
    AUTHORIZATION_CHECKPOINT,
    EVENT_REQUEST,
    WORKER_HELLO,
    WireFormatError,
    WorkerHello,
    read_event_request,
    read_payload,
    request_failure,
    result_body,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from concurrent.futures import Future

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import EventRequest
    from griptape_nodes.worker.channel import Channel

logger = logging.getLogger("griptape_nodes")

_ANSWERED_BODY_TYPES = frozenset({WORKER_HELLO, AUTHORIZATION_CHECKPOINT, EVENT_REQUEST})


class HostedWorkerEnd:
    """Carries one worker this engine runs for the engine `artist_engine_id` to that engine.

    `on_hello` decides the worker's hello and returns the reply body. `on_closed` is called once, from
    the channel's own thread, when the channel ends. Requests to the artist's engine wait at most
    `request_timeout_s`. The coroutines run on the loop given to `attach`.
    """

    def __init__(  # noqa: PLR0913 (each is a separate fact about the worker)
        self,
        engine: Engine,
        *,
        worker_engine_id: str,
        artist_engine_id: str,
        request_timeout_s: float,
        on_hello: Callable[[WorkerHello], Coroutine[Any, Any, dict[str, Any]]],
        on_closed: Callable[[], None],
    ) -> None:
        self._engine = engine
        self._worker_engine_id = worker_engine_id
        self._artist_engine_id = artist_engine_id
        self._request_timeout_s = request_timeout_s
        self._on_hello = on_hello
        self._on_closed = on_closed
        self._loop: asyncio.AbstractEventLoop | None = None
        self._events: _EventBatches | None = None

    def attach(self, channel: Channel, loop: asyncio.AbstractEventLoop) -> None:
        """Start carrying `channel` on `loop`, the engine loop that sends this engine's requests."""
        self._loop = loop
        self._events = _EventBatches(self._deliver_events, loop)
        channel.set_request_gate(self.gate)
        channel.start(on_request=self._on_request, on_notification=self._on_notification, on_closed=self._on_closed)

    @staticmethod
    def gate(body: dict[str, Any]) -> GateDecision:
        """Pass on what a worker sends, refusing only what is not a single request, hello, or checkpoint.

        No policy of this engine's applies: the artist's engine applies its own to everything here.
        """
        body_type = body.get("type")
        if body_type not in _ANSWERED_BODY_TYPES:
            details = f"Attempted to answer a '{body_type}' from a library's worker process. Failed because this engine does not answer that."
            return GateDecision(dispatch=None, reply={"error": details})
        return GateDecision(dispatch=body, reply=None)

    async def flush_events(self) -> None:
        """Wait until every event the worker sent before now has reached the artist's engine, or failed to."""
        await self._require_events().flush()

    async def forward_request(self, event_request: EventRequest) -> dict[str, Any]:
        """Send a request the worker forwarded to the artist's engine as itself, and return its result as the wire carries it."""
        request = event_request.request
        try:
            result = await self._engine.request(
                self._artist_engine_id,
                request,
                timeout_s=self._request_timeout_s,
                open_workflow_id=event_request.open_workflow_id,
                worker_engine_id=self._worker_engine_id,
            )
        except EngineNotFoundError as e:
            details = (
                f"Attempted to send {type(request).__name__} to the engine this worker works for. Failed because {e}"
            )
            return request_failure(_envelope_of(event_request), details)
        result_type = EventResultSuccess if result.succeeded() else EventResultFailure
        result_event = result_type(
            request=request,
            result=result,
            request_id=event_request.request_id,
            response_topic=event_request.response_topic,
            open_workflow_id=event_request.open_workflow_id,
        )
        return result_body(result_event)

    async def decide_checkpoint(self, body: dict[str, Any]) -> dict[str, Any]:
        """Have the artist's engine decide a checkpoint the worker reached. Anything short of its answer denies."""
        payload = body.get("payload")
        if not isinstance(payload, dict):
            return {
                "error": "Attempted to check a permission for a library's worker process. Failed because it was unreadable."
            }
        request = DecideWorkerCheckpointRequest(worker_engine_id=self._worker_engine_id, checkpoint=payload)
        try:
            result = await self._engine.request(self._artist_engine_id, request, timeout_s=self._request_timeout_s)
        except EngineNotFoundError as e:
            return {
                "error": f"Attempted to check a permission with the engine this worker works for. Failed because {e}"
            }
        if not isinstance(result, DecideWorkerCheckpointResultSuccess):
            return {"error": f"The engine this worker works for did not decide the checkpoint: {result.result_details}"}
        return {"denial": result.denial}

    async def _deliver_events(self, events: list[dict[str, Any]]) -> None:
        request = DeliverWorkerEventsRequest(worker_engine_id=self._worker_engine_id, events=events)
        try:
            result = await self._engine.request(self._artist_engine_id, request, timeout_s=self._request_timeout_s)
        except EngineNotFoundError as e:
            logger.warning("Dropped %d event(s) from worker %s: %s", len(events), self._worker_engine_id, e)
            return
        if not result.succeeded():
            logger.warning(
                "Engine %s did not take %d event(s) from worker %s: %s",
                self._artist_engine_id,
                len(events),
                self._worker_engine_id,
                result.result_details,
            )

    def _on_request(self, body: dict[str, Any]) -> dict[str, Any]:
        """Answer one request from the worker. Runs on a channel pool thread, so it may wait on the engine loop."""
        body_type = body.get("type")
        if body_type == WORKER_HELLO:
            try:
                hello = read_payload(body, WorkerHello)
            except WireFormatError as e:
                return {
                    "accepted": False,
                    "details": f"Attempted to accept a library's worker process. Failed because {e}.",
                }
            return self._run_on_loop(self._on_hello(hello)).result()
        if body_type == AUTHORIZATION_CHECKPOINT:
            return self._run_on_loop(self.decide_checkpoint(body)).result()
        try:
            event_request = read_event_request(body)
        except WireFormatError as e:
            return request_failure(
                body, f"Attempted to handle a request from a library's worker process. Failed because {e}."
            )
        return self._run_on_loop(self.forward_request(event_request)).result()

    def _on_notification(self, body: dict[str, Any]) -> None:
        """Queue one of the worker's events for the artist's engine, in the order the channel delivers them."""
        self._require_events().add_threadsafe(body)

    def _run_on_loop(self, coro: Coroutine[Any, Any, Any]) -> Future[Any]:
        """Run `coro` on the engine loop from a channel thread, bound to no workflow of this engine's."""
        loop = self._require_loop()
        context = contextvars.copy_context()
        context.run(bound_workflow.set, NO_WORKFLOW)
        return context.run(asyncio.run_coroutine_threadsafe, coro, loop)

    def _require_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            msg = "HostedWorkerEnd.attach() must be called before the channel delivers anything."
            raise RuntimeError(msg)
        return self._loop

    def _require_events(self) -> _EventBatches:
        if self._events is None:
            msg = "HostedWorkerEnd.attach() must be called before the channel delivers anything."
            raise RuntimeError(msg)
        return self._events


class _EventBatches:
    """Sends events in order, one batch at a time: what arrives while a batch is out goes in the next.

    Every method but `add_threadsafe` runs on `loop`.
    """

    def __init__(
        self,
        send: Callable[[list[dict[str, Any]]], Coroutine[Any, Any, None]],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._send = send
        self._loop = loop
        self._pending: list[dict[str, Any]] = []
        self._added = 0
        self._sent = 0
        self._sending: asyncio.Task | None = None
        self._flushes: list[tuple[int, asyncio.Future[None]]] = []

    def add_threadsafe(self, body: dict[str, Any]) -> None:
        """Queue `body` from any thread. Bodies queued from one thread keep their order."""
        self._loop.call_soon_threadsafe(self._add, body)

    async def flush(self) -> None:
        """Wait until every body queued before now has been sent, or failed to send."""
        if self._sent >= self._added:
            return
        done: asyncio.Future[None] = self._loop.create_future()
        self._flushes.append((self._added, done))
        await done

    def _add(self, body: dict[str, Any]) -> None:
        self._pending.append(body)
        self._added += 1
        if self._sending is None:
            context = contextvars.copy_context()
            context.run(bound_workflow.set, NO_WORKFLOW)
            self._sending = self._loop.create_task(self._send_until_empty(), context=context)

    async def _send_until_empty(self) -> None:
        try:
            while self._pending:
                batch = self._pending
                self._pending = []
                try:
                    await self._send(batch)
                finally:
                    self._sent += len(batch)
                    self._release_flushes()
        finally:
            self._sending = None

    def _release_flushes(self) -> None:
        waiting = []
        for target, done in self._flushes:
            if target <= self._sent:
                if not done.done():
                    done.set_result(None)
            else:
                waiting.append((target, done))
        self._flushes = waiting


def _envelope_of(event_request: EventRequest) -> dict[str, Any]:
    """Enough of `event_request`'s wire envelope for `request_failure` to answer it."""
    return {
        "type": EVENT_REQUEST,
        "payload": {
            "request_type": type(event_request.request).__name__,
            "request_id": event_request.request_id,
            "response_topic": event_request.response_topic,
            "open_workflow_id": event_request.open_workflow_id,
        },
    }
