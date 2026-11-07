"""The engine's end of the channel to one library worker process it started for itself.

Everything the worker asks for passes one gate first (`gate`): a request is screened by the engine's
worker request gates, the license wire policy the app installs, exactly as a client's request is. A
checkpoint is decided by the engine's own authorization hooks. Nothing else is answered.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
from typing import TYPE_CHECKING, Any, NamedTuple

from griptape_nodes.retained_mode.events import converters
from griptape_nodes.retained_mode.events.base_events import AppEvent, ExecutionEvent, ExecutionGriptapeNodeEvent
from griptape_nodes.retained_mode.managers.authorization_checkpoint import (
    AuthorizationCheckpoint,
    CheckpointDenial,
    CheckpointFailure,
)
from griptape_nodes.retained_mode.runtime.binding import NO_WORKFLOW, bound_workflow
from griptape_nodes.worker.channel import GateDecision
from griptape_nodes.worker.wire import (
    APP_EVENT,
    AUTHORIZATION_CHECKPOINT,
    EVENT_REQUEST,
    EVENT_RESULT,
    EXECUTION_EVENT,
    QUEUED_APP_PAYLOADS,
    RELAYED_EXECUTION_PAYLOADS,
    WORKER_HELLO,
    WireFormatError,
    WorkerHello,
    answer_event_request,
    read_event_request,
    read_payload,
    request_failure,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from concurrent.futures import Future

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import EventRequest
    from griptape_nodes.worker.channel import Channel

logger = logging.getLogger("griptape_nodes")

_QUEUED_APP_PAYLOAD_NAMES = frozenset(payload.__name__ for payload in QUEUED_APP_PAYLOADS)

# The bodies a worker may send that are not requests, so no wire policy reads them.
_ANSWERED_WITHOUT_WIRE_POLICY = frozenset({WORKER_HELLO, AUTHORIZATION_CHECKPOINT})


class ParentEnd:
    """Answers one local worker on behalf of `engine`.

    `on_hello` decides the worker's hello and returns the reply body. `on_closed` is called once, from
    the channel's own thread, when the channel ends. `relay_to_editor` sends on, untouched, something the
    worker would have sent the editor that this engine must not act on, given its wire `event_type`
    (`EVENT_RESULT` or `APP_EVENT`) and payload. The two coroutines run on the loop given to `attach`.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        on_hello: Callable[[WorkerHello], Coroutine[Any, Any, dict[str, Any]]],
        on_closed: Callable[[], None],
        relay_to_editor: Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]],
    ) -> None:
        self._engine = engine
        self._on_hello = on_hello
        self._on_closed = on_closed
        self._relay_to_editor = relay_to_editor
        self._loop: asyncio.AbstractEventLoop | None = None

    def attach(self, channel: Channel, loop: asyncio.AbstractEventLoop) -> None:
        """Start answering `channel` on `loop`, the engine loop that owns this engine's requests."""
        self._loop = loop
        channel.set_request_gate(self.gate)
        channel.start(on_request=self._on_request, on_notification=self._on_notification, on_closed=self._on_closed)

    def gate(self, body: dict[str, Any]) -> GateDecision:
        """Decide whether a worker's request reaches `_on_request`, and with what body."""
        body_type = body.get("type")
        if body_type in _ANSWERED_WITHOUT_WIRE_POLICY:
            return GateDecision(dispatch=body, reply=None)
        if body_type != EVENT_REQUEST:
            details = f"Attempted to answer a '{body_type}' from a library's worker process. Failed because this engine does not answer that."
            return GateDecision(dispatch=None, reply={"error": details})
        # Checked before any gate runs: the license wire policy only screens what declares itself a
        # request, so anything else would pass it and then be read as one.
        payload = body.get("payload")
        if not isinstance(payload, dict) or payload.get("event_type") != EVENT_REQUEST:
            details = "Attempted to handle a request from a library's worker process. Failed because it was not a single request."
            return GateDecision(dispatch=None, reply=request_failure(body, details))
        decision = self._engine.event_manager.evaluate_worker_request_gate(body)
        if decision.forward is None:
            request_type = payload.get("request_type")
            logger.warning(
                "The license policy refused a %s from a library's worker process: %s", request_type, decision.denials
            )
            details = (
                f"Attempted to run '{request_type}' from a library's worker process. "
                "Failed because the license policy refused it."
            )
            return GateDecision(dispatch=None, reply=request_failure(body, details))
        return GateDecision(dispatch=decision.forward, reply=None)

    async def answer_worker_request(self, event_request: EventRequest) -> dict[str, Any]:
        """Handle a request the worker forwarded, in the workflow its envelope names, and return its result."""
        return await answer_event_request(self._engine, event_request)

    def answer_checkpoint(self, body: dict[str, Any]) -> dict[str, Any]:
        """Decide a worker's checkpoint with this engine's authorization hooks. Unreadable denies."""
        return decide_worker_checkpoint(self._engine, body)

    def _on_request(self, body: dict[str, Any]) -> dict[str, Any]:
        """Answer one gated request. Runs on a channel pool thread, so it may wait on the engine loop."""
        body_type = body.get("type")
        if body_type == AUTHORIZATION_CHECKPOINT:
            return self.answer_checkpoint(body)
        if body_type == WORKER_HELLO:
            try:
                hello = read_payload(body, WorkerHello)
            except WireFormatError as e:
                return {
                    "accepted": False,
                    "details": f"Attempted to accept a library's worker process. Failed because {e}.",
                }
            return self._run_on_loop(self._on_hello(hello)).result()
        try:
            event_request = read_event_request(body)
        except WireFormatError as e:
            return request_failure(
                body, f"Attempted to handle a request from a library's worker process. Failed because {e}."
            )
        return self._run_on_loop(self.answer_worker_request(event_request), bound_to_no_workflow=False).result()

    def _on_notification(self, body: dict[str, Any]) -> None:
        """Queue what the worker would have sent the editor, in order, as if a node here emitted it.

        Runs on the channel's dispatch thread. Each event is handed to the engine loop rather than put
        here, so execution-event listeners run where they would for a node here, and so an event lands
        on the queue ahead of the result that the same thread resolves after it.
        """
        try:
            self._accept_notification(body)
        except WireFormatError as e:
            logger.error("Dropped an event from a library's worker process: %s.", e)

    def _accept_notification(self, body: dict[str, Any]) -> None:
        loop = self._require_loop()
        notification = read_worker_notification(body)
        if isinstance(notification, QueuedWorkerEvent):
            loop.call_soon_threadsafe(self._engine.event_manager.put_event, notification.event)
            return
        relay = self._run_on_loop(self._relay_to_editor(notification.event_type, notification.payload))
        relay.add_done_callback(_log_relay_failure)

    def _run_on_loop(self, coro: Coroutine[Any, Any, Any], *, bound_to_no_workflow: bool = True) -> Future[Any]:
        """Run `coro` on the engine loop from a channel thread.

        Bound to no workflow unless told otherwise: what runs here outlives the channel frame that
        started it, and a forwarded request binds the workflow its envelope names when dispatched.
        """
        loop = self._require_loop()
        context = contextvars.copy_context()
        if bound_to_no_workflow:
            context.run(bound_workflow.set, NO_WORKFLOW)
        return context.run(asyncio.run_coroutine_threadsafe, coro, loop)

    def _require_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            msg = "ParentEnd.attach() must be called before the channel delivers anything."
            raise RuntimeError(msg)
        return self._loop


def decide_worker_checkpoint(engine: Engine, body: dict[str, Any]) -> dict[str, Any]:
    """Decide a worker's checkpoint body with `engine`'s authorization hooks, as the reply the worker reads. Unreadable denies."""
    try:
        checkpoint = read_payload(body, AuthorizationCheckpoint)
    except WireFormatError as e:
        logger.error("Denied a checkpoint from a library's worker process because %s.", e)
        detail = f"Attempted to check a permission for a library's worker process. Failed because {e}."
        denial = CheckpointDenial(failures=(CheckpointFailure(detail=detail),))
        return {"denial": json.loads(converters.engine.dumps(denial))}
    denial = engine.event_manager.evaluate_authorization_checkpoint(checkpoint)
    if denial is None:
        return {"denial": None}
    return {"denial": json.loads(converters.engine.dumps(denial))}


class QueuedWorkerEvent(NamedTuple):
    """A worker's event the engine queues as if a node of its own emitted it."""

    event: Any


class EditorBoundWorkerEvent(NamedTuple):
    """A worker's event the engine sends on to the editor without acting on it, by wire `event_type` and payload."""

    event_type: str
    payload: dict[str, Any]


def read_worker_notification(body: dict[str, Any]) -> QueuedWorkerEvent | EditorBoundWorkerEvent:
    """What the engine a worker works for does with one of the worker's one-way bodies.

    Raises:
        WireFormatError: The body is not something a worker sends, or does not have its type's shape.
    """
    body_type = body.get("type")
    if body_type == EXECUTION_EVENT:
        execution_event = read_payload(body, ExecutionEvent)
        if not isinstance(execution_event.payload, RELAYED_EXECUTION_PAYLOADS):
            msg = f"a worker does not send '{type(execution_event.payload).__name__}' events"
            raise WireFormatError(msg)
        return QueuedWorkerEvent(event=ExecutionGriptapeNodeEvent(wrapped_event=execution_event))
    if body_type in (APP_EVENT, EVENT_RESULT):
        payload = body.get("payload")
        if not isinstance(payload, dict):
            msg = f"a '{body_type}' carried no payload"
            raise WireFormatError(msg)
        if body_type == APP_EVENT and payload.get("payload_type") in _QUEUED_APP_PAYLOAD_NAMES:
            return QueuedWorkerEvent(event=read_payload(body, AppEvent))
        return EditorBoundWorkerEvent(event_type=body_type, payload=payload)
    msg = f"a '{body_type}' is not something a worker sends"
    raise WireFormatError(msg)


async def apply_worker_notifications(
    engine: Engine,
    bodies: list[dict[str, Any]],
    relay_to_editor: Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]],
) -> None:
    """On the engine loop, do with each of a worker's one-way bodies, in order, what `ParentEnd` does with them."""
    for body in bodies:
        try:
            notification = read_worker_notification(body)
        except WireFormatError as e:
            logger.error("Dropped an event from a library's worker process: %s.", e)
            continue
        if isinstance(notification, QueuedWorkerEvent):
            engine.event_manager.put_event(notification.event)
            continue
        try:
            await relay_to_editor(notification.event_type, notification.payload)
        except Exception as e:
            logger.error("Could not send what a library's worker process sent on to the editor: %s", e)


def _log_relay_failure(relay: Future[Any]) -> None:
    if relay.cancelled():
        return
    error = relay.exception()
    if error is not None:
        logger.error("Could not send what a library's worker process sent on to the editor: %s", error)
