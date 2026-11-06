"""What a library's worker process and the engine it works for send each other over their channel.

Every channel body is a dict with a ``type``. A request travels as the wire envelope a client's
request arrives in (``{"type": "EventRequest", "topic", "payload"}``), so the engine applies the same
license wire policy to a worker's requests that it applies to its clients' (see
``EventManager.add_worker_request_gate``). The other types are what only a worker sends.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from griptape_nodes.retained_mode.engine import engine_scope
from griptape_nodes.retained_mode.events import converters
from griptape_nodes.retained_mode.events.app_events import EngineInitializationProgress
from griptape_nodes.retained_mode.events.base_events import EventRequest, RequestPayload
from griptape_nodes.retained_mode.events.execution_events import GriptapeEvent, ParameterValueUpdateEvent
from griptape_nodes.retained_mode.events.generic_events import GenericResultFailure
from griptape_nodes.retained_mode.events.logger_events import LogHandlerEvent
from griptape_nodes.retained_mode.events.worker_events import (
    LibraryLoadReport,  # noqa: TC001 (WorkerHello is structured at runtime)
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import EventResult
    from griptape_nodes.retained_mode.managers.event_manager import ResultContext

# Requests, in either direction, as the wire envelope.
EVENT_REQUEST = "EventRequest"
# Worker to engine, once, after it has loaded what it serves.
WORKER_HELLO = "WorkerHello"
# Worker to engine: an authorization checkpoint for the engine's policy to decide.
AUTHORIZATION_CHECKPOINT = "AuthorizationCheckpoint"
# Worker to engine, one way: what the worker would have sent the editor.
EXECUTION_EVENT = "ExecutionEvent"
APP_EVENT = "AppEvent"
EVENT_RESULT = "EventResult"

# The engine ID a worker is started with, minted for its spawn by the engine it works for.
WORKER_ENGINE_ID_ENV = "GTN_ENGINE_ID"

# The topic a request's envelope names. Nothing routes on it over a channel; the wire format has one.
CHANNEL_REQUEST_TOPIC = "request"

# The execution events a worker sends on: a running node's new parameter values and progress. Its
# element events name elements of its own copy of the node, which the editor does not have.
RELAYED_EXECUTION_PAYLOADS = (ParameterValueUpdateEvent, GriptapeEvent)

# The app events a worker sends that the engine queues like its own: telemetry for the editor that no
# engine listens for. Any other app event describes the worker's own state, and queued in the engine it
# would reconfigure the engine, so the engine passes it to the editor without acting on it.
QUEUED_APP_PAYLOADS = (LogHandlerEvent, EngineInitializationProgress)


class WireFormatError(ValueError):
    """A channel body that does not have the shape its type promises."""


@dataclass
class WorkerHello:
    """What a worker tells the engine it works for once it has loaded what it serves.

    Attributes:
        engine_version: The griptape-nodes-engine version the worker runs.
        library_name: The library the worker serves, or None for the general-purpose worker.
        loaded: Whether it loaded that library, or for the general-purpose worker, every library.
        libraries: Every library that loaded in the worker, including the ones the library it serves
            depends on. Empty for the general-purpose worker, which is no library's worker.
        failure_reason: Why it did not load, when `loaded` is False.
    """

    engine_version: str
    library_name: str | None
    loaded: bool
    libraries: list[LibraryLoadReport] = field(default_factory=list)
    failure_reason: str | None = None


def event_request_envelope(event_request: EventRequest, topic: str = CHANNEL_REQUEST_TOPIC) -> dict[str, Any]:
    """The wire envelope for `event_request`, giving it a request id if it has none."""
    if not event_request.request_id:
        event_request.request_id = uuid.uuid4().hex
    return {"type": EVENT_REQUEST, "topic": topic, "payload": json.loads(converters.engine.dumps(event_request))}


def read_event_request(envelope: dict[str, Any]) -> EventRequest:
    """The request an envelope carries.

    Raises:
        WireFormatError: The envelope does not carry exactly one readable request.
    """
    payload = envelope.get("payload")
    if not isinstance(payload, dict) or payload.get("event_type") != EVENT_REQUEST:
        msg = "it was not a single request"
        raise WireFormatError(msg)
    try:
        event_request = converters.engine.structure(payload, EventRequest)
    except Exception as e:  # Structuring raises whatever the payload's fields provoke; any of it means unreadable.
        msg = f"'{payload.get('request_type')}' could not be read: {e}"
        raise WireFormatError(msg) from e
    if not isinstance(event_request.request, RequestPayload):
        msg = f"'{payload.get('request_type')}' is not a request"
        raise WireFormatError(msg)
    return event_request


async def answer_event_request(engine: Engine, event_request: EventRequest) -> dict[str, Any]:
    """Have `engine` handle a request that arrived over a channel, and return its result as the wire carries it.

    The envelope's `open_workflow_id` names the workflow the request runs in, as it does for a client.
    """
    result_context: ResultContext = {
        "request_id": event_request.request_id,
        "response_topic": event_request.response_topic,
        "sender": None,
        "open_workflow_id": event_request.open_workflow_id,
    }
    with engine_scope(engine):
        result_event = await engine.event_manager.ahandle_request(event_request.request, result_context=result_context)
    return result_body(result_event)


def result_body(result_event: EventResult) -> dict[str, Any]:
    """`result_event` as the wire carries it, or a failure saying why it could not be."""
    return json.loads(converters.engine.dumps_result(result_event).text)


def request_failure(envelope: dict[str, Any], details: str) -> dict[str, Any]:
    """A failure result answering the request `envelope` carries, without reading the request back."""
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    failure = GenericResultFailure(result_details=details)
    return {
        "event_type": "EventResultFailure",
        "request_type": payload.get("request_type"),
        "request": payload.get("request") if isinstance(payload.get("request"), dict) else {},
        "result_type": GenericResultFailure.__name__,
        "result": json.loads(converters.engine.dumps(failure)),
        "request_id": payload.get("request_id"),
        "response_topic": payload.get("response_topic"),
        "open_workflow_id": payload.get("open_workflow_id"),
    }


def event_body(body_type: str, event: Any) -> dict[str, Any]:
    """A one-way body carrying `event` as the wire carries it."""
    return {"type": body_type, "payload": json.loads(converters.engine.dumps(event))}


def read_payload[T](body: dict[str, Any], cls: type[T]) -> T:
    """The `cls` a body's payload carries.

    Raises:
        WireFormatError: The payload is not a readable `cls`.
    """
    payload = body.get("payload")
    if not isinstance(payload, dict):
        msg = f"a '{body.get('type')}' carried no payload"
        raise WireFormatError(msg)
    try:
        return converters.engine.structure(payload, cls)
    except Exception as e:  # Structuring raises whatever the payload's fields provoke; any of it means unreadable.
        msg = f"a '{body.get('type')}' could not be read: {e}"
        raise WireFormatError(msg) from e
