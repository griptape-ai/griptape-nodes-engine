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
from typing import TYPE_CHECKING, Any

from griptape_nodes.retained_mode.events import converters
from griptape_nodes.retained_mode.events.base_events import AppEvent, ExecutionEvent, ExecutionGriptapeNodeEvent
from griptape_nodes.retained_mode.managers.authorization_checkpoint import (
    AuthorizationCheckpoint,
    CheckpointDenial,
    CheckpointFailure,
)
from griptape_nodes.retained_mode.runtime.binding import create_task_bound_to_no_workflow
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

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import EventRequest
    from griptape_nodes.worker.channel import AsyncChannel

logger = logging.getLogger("griptape_nodes")

_QUEUED_APP_PAYLOAD_NAMES = frozenset(payload.__name__ for payload in QUEUED_APP_PAYLOADS)

# The bodies a worker may send that are not requests, so no wire policy reads them.
_ANSWERED_WITHOUT_WIRE_POLICY = frozenset({WORKER_HELLO, AUTHORIZATION_CHECKPOINT})


class ParentEnd:
    """Answers one local worker on behalf of `engine`, on the engine loop that owns the channel.

    `on_hello` decides the worker's hello and returns the reply body. `on_closed` is called once when
    the channel ends. `relay_to_editor` sends on, untouched, something the worker would have sent the
    editor that this engine must not act on, given its wire `event_type` (`EVENT_RESULT` or
    `APP_EVENT`) and payload.
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
        # Relays outlive the frame that started them, so they are held here until they finish.
        self._relays: set[asyncio.Task] = set()
        self._channel: AsyncChannel | None = None

    def attach(self, channel: AsyncChannel) -> None:
        """Start answering `channel`. Call on the engine loop that owns it."""
        self._channel = channel
        # Started from an empty context: every request and notification handler inherits the
        # context the read loop starts in, and a request must bind only the workflow its envelope
        # names, never one that happened to be bound where the worker was spawned.
        contextvars.Context().run(
            channel.start,
            on_request=self._on_request,
            on_notification=self._on_notification,
            on_closed=self._on_closed,
            gate=self.gate,
        )

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
        try:
            checkpoint = read_payload(body, AuthorizationCheckpoint)
        except WireFormatError as e:
            logger.error("Denied a checkpoint from a library's worker process because %s.", e)
            detail = f"Attempted to check a permission for a library's worker process. Failed because {e}."
            denial = CheckpointDenial(failures=(CheckpointFailure(detail=detail),))
            return {"denial": json.loads(converters.engine.dumps(denial))}
        denial = self._engine.event_manager.evaluate_authorization_checkpoint(checkpoint)
        if denial is None:
            return {"denial": None}
        return {"denial": json.loads(converters.engine.dumps(denial))}

    async def _on_request(self, body: dict[str, Any]) -> dict[str, Any]:
        """Answer one gated request."""
        body_type = body.get("type")
        if body_type == AUTHORIZATION_CHECKPOINT:
            return self.answer_checkpoint(body)
        if body_type == WORKER_HELLO:
            reply = await self._answer_hello(body)
            if not reply.get("accepted") and self._channel is not None:
                # A refused worker ends because its channel does, not because it chooses to: library
                # code can keep its process alive. Scheduled so the refusal goes out first.
                asyncio.get_running_loop().call_soon(self._channel.close)
            return reply
        try:
            event_request = read_event_request(body)
        except WireFormatError as e:
            return request_failure(
                body, f"Attempted to handle a request from a library's worker process. Failed because {e}."
            )
        return await self.answer_worker_request(event_request)

    async def _answer_hello(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            hello = read_payload(body, WorkerHello)
        except WireFormatError as e:
            return {
                "accepted": False,
                "details": f"Attempted to accept a library's worker process. Failed because {e}.",
            }
        return await _bound_to_no_workflow(self._on_hello(hello))

    def _on_notification(self, body: dict[str, Any]) -> None:
        """Queue what the worker would have sent the editor, in order, as if a node here emitted it.

        Applied as each frame arrives, so a node's events are on the queue before its result, which
        the channel delivers after them.
        """
        try:
            self._accept_notification(body)
        except WireFormatError as e:
            logger.error("Dropped an event from a library's worker process: %s.", e)

    def _accept_notification(self, body: dict[str, Any]) -> None:
        body_type = body.get("type")
        if body_type == EXECUTION_EVENT:
            execution_event = read_payload(body, ExecutionEvent)
            if not isinstance(execution_event.payload, RELAYED_EXECUTION_PAYLOADS):
                msg = f"a worker does not send '{type(execution_event.payload).__name__}' events"
                raise WireFormatError(msg)
            self._engine.event_manager.put_event(ExecutionGriptapeNodeEvent(wrapped_event=execution_event))
            return
        if body_type in (APP_EVENT, EVENT_RESULT):
            payload = body.get("payload")
            if not isinstance(payload, dict):
                msg = f"a '{body_type}' carried no payload"
                raise WireFormatError(msg)
            if body_type == APP_EVENT and payload.get("payload_type") in _QUEUED_APP_PAYLOAD_NAMES:
                self._engine.event_manager.put_event(read_payload(body, AppEvent))
                return
            relay = create_task_bound_to_no_workflow(
                asyncio.get_running_loop(), self._relay_to_editor(body_type, payload)
            )
            self._relays.add(relay)
            relay.add_done_callback(self._relays.discard)
            relay.add_done_callback(_log_relay_failure)
            return
        msg = f"a '{body_type}' is not something a worker sends"
        raise WireFormatError(msg)


async def _bound_to_no_workflow[T](coro: Coroutine[Any, Any, T]) -> T:
    """Await `coro` as work that acts on no workflow."""
    return await create_task_bound_to_no_workflow(asyncio.get_running_loop(), coro)


def _log_relay_failure(relay: asyncio.Task) -> None:
    if relay.cancelled():
        return
    error = relay.exception()
    if error is not None:
        logger.error("Could not send what a library's worker process sent on to the editor: %s", error)
