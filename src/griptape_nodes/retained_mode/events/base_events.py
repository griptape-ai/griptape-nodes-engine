from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field


@dataclass
class ResultDetail:
    """A single detail about an operation result, including logging level and human readable message."""

    level: int
    message: str


@dataclass
class StrictModeViolationDetail(ResultDetail):
    """A ResultDetail that carries structured strict-mode violation metadata.

    Editor renders ``ResultDetail`` today, so this subclass surfaces on
    the result payload for free. The extra fields let future tooling
    filter or group violations without parsing ``message``.
    """

    rule_id: str
    severity: str
    subject: str
    library_name: str | None


@dataclass
class ResultDetails:
    """Container for multiple ResultDetail objects."""

    result_details: list[ResultDetail]

    def __init__(
        self,
        *result_details: ResultDetail,
        message: str | None = None,
        level: int | None = None,
    ):
        """Initialize with ResultDetail objects or create a single one from message/level.

        Args:
            *result_details: Variable number of ResultDetail objects
            message: If provided, creates a single ResultDetail with this message
            level: Logging level for the single ResultDetail (required if message is provided)
        """
        # Handle single message/level convenience
        if message is not None:
            if level is None:
                err_msg = "level is required when message is provided"
                raise ValueError(err_msg)
            if result_details:
                err_msg = "Cannot provide both result_details and message/level"
                raise ValueError(err_msg)
            self.result_details = [ResultDetail(level=level, message=message)]
        else:
            if not result_details:
                err_msg = "ResultDetails requires at least one ResultDetail or message/level"
                raise ValueError(err_msg)
            self.result_details = list(result_details)

    def __str__(self) -> str:
        """String representation of ResultDetails.

        Returns:
            str: Concatenated messages of all ResultDetail objects
        """
        return "\n".join(detail.message for detail in self.result_details)

    def _cattrs_unstructure(self, converter: Any) -> dict[str, Any]:
        return {"result_details": [converter.unstructure(d) for d in self.result_details]}

    @classmethod
    def _cattrs_structure(cls, data: dict[str, Any], converter: Any) -> ResultDetails:
        return cls(*[converter.structure(item, ResultDetail) for item in data["result_details"]])


# The Payload class is a marker interface
class Payload(ABC):  # noqa: B024
    """Base class for all payload types. Customers will derive from this."""


# Request payload base class with optional request ID
@dataclass(kw_only=True)
class RequestPayload(Payload, ABC):
    """Base class for all request payloads.

    Args:
        request_id: Optional request ID for tracking.
        failure_log_level: If set, override the log level for failure results.
                          Use logging.DEBUG (10) or logging.INFO (20) to suppress error toasts.
                          Default: None (use handler's default, typically ERROR).
        broadcast_result: Whether handle_request should queue the result event for broadcast
                          (e.g. to connected WebSocket clients). Defaults to True. Request types
                          whose results are large or only relevant to the direct caller can
                          default this to False on the subclass to avoid unnecessary serialization
                          and transmission. Can also be set per-instance at construction time.
        fields: **Wire-only** dot-path filter applied to the broadcast JSON before it is sent
                over the WebSocket. Has no effect on in-process/retained-mode callers, which
                always receive the full result object. Like broadcast_result and failure_log_level,
                this is a transport-layer concern, not part of the request logic.

                Syntax:
                  - ``None`` (default) — return all fields.
                  - ``[]`` (empty list) — return only framework fields (result_details,
                    altered_workflow_state). Useful to trigger side effects without caring
                    about the result payload.
                  - ``["a", "a.b.c"]`` — dot-paths select nested fields. An empty list
                    entry keeps the whole value; a more-specific sibling narrows it.
                    Prefix-wins: if both ``"workflows"`` and ``"workflows.name"`` are
                    listed, the full ``workflows`` value is kept.
                  - ``"*"`` wildcard — for ``dict[str, SomeObject]`` where keys are
                    arbitrary (e.g. file paths). ``"workflows.*.name"`` plucks ``name``
                    from each value without knowing the keys in advance. Prefer ``"*"``
                    over a concrete key for such maps: naming a specific key warns
                    "not found" whenever that key is legitimately absent.

                Framework fields (result_details, altered_workflow_state) are always
                included regardless of what fields specifies. Filtering is skipped
                entirely on failure results.
    """

    broadcast_result: bool = True
    request_id: str | None = None
    failure_log_level: int | None = None
    fields: list[str] | None = None


# Result payload base class with abstract succeeded/failed methods, and indicator whether the current workflow was altered.
@dataclass(kw_only=True)
class ResultPayload(Payload, ABC):
    """Base class for all result payloads."""

    result_details: ResultDetails | str
    """When set to True, alerts clients that this result made changes to the workflow state.
    Editors can use this to determine if the workflow is dirty and needs to be re-saved, for example."""
    altered_workflow_state: bool = False

    @abstractmethod
    def succeeded(self) -> bool:
        """Returns whether this result represents a success or failure.

        Returns:
            bool: True if success, False if failure
        """

    def failed(self) -> bool:
        return not self.succeeded()


@dataclass
class WorkflowAlteredMixin:
    """Mixin for a ResultPayload that guarantees that a workflow was altered."""

    altered_workflow_state: bool = field(default=True, init=False)


@dataclass
class WorkflowNotAlteredMixin:
    """Mixin for a ResultPayload that guarantees that a workflow was NOT altered."""

    altered_workflow_state: bool = field(default=False, init=False)


class SkipTheLineMixin:
    """Mixin for events that should skip the event queue and be processed immediately.

    Events that implement this mixin will be handled directly without being added
    to the event queue, allowing for priority processing of critical events like
    heartbeats or other time-sensitive operations.
    """


# Success result payload abstract base class
@dataclass(kw_only=True)
class ResultPayloadSuccess(ResultPayload, ABC):
    """Abstract base class for success result payloads."""

    result_details: ResultDetails | str

    def __post_init__(self) -> None:
        """Initialize success result with INFO level default for strings."""
        if isinstance(self.result_details, str):
            self.result_details = ResultDetails(message=self.result_details, level=logging.DEBUG)

    def succeeded(self) -> bool:
        """Returns True as this is a success result.

        Returns:
            bool: Always True
        """
        return True


class ForwardedException(Exception):  # noqa: N818
    """Placeholder for an exception that crossed the worker boundary.

    The converter's Exception hook emits worker-side exceptions as a
    ``{type, message, traceback}`` dict, then rebuilds them into a
    ``ForwardedException`` on the receiving side. The placeholder is
    still an ``Exception`` (so ``raise ... from result.exception``
    chains) and carries the worker-side class name and formatted
    traceback so the orchestrator can show both.

    ``NodeExecutor._format_node_failure_message`` is the consumer:
    it reads ``original_type`` for the ``[builtins.ValueError]``
    prefix on the user-visible ``RuntimeError`` message, and
    ``original_traceback`` for the ``Worker traceback:`` block.
    Without these attributes the chained exception would print only
    ``Type: message`` with no frames, because the placeholder is
    constructed (not raised) and so its ``__traceback__`` is ``None``.
    """

    def __init__(
        self,
        message: str,
        *,
        original_type: str | None = None,
        original_traceback: str | None = None,
    ) -> None:
        super().__init__(message)
        self.original_type = original_type
        self.original_traceback = original_traceback


# Failure result payload abstract base class
@dataclass(kw_only=True)
class ResultPayloadFailure(ResultPayload, ABC):
    """Abstract base class for failure result payloads.

    ``exception`` is the single source of truth. On the local path it
    is the live ``Exception``. Across the worker -> orchestrator wire
    the converter emits it as a structured dict and rebuilds it as a
    ``ForwardedException`` carrying the original type name and
    traceback as attributes, so callers can read both paths uniformly.
    """

    result_details: ResultDetails | str
    exception: Exception | None = None

    def __post_init__(self) -> None:
        """Initialize failure result with ERROR level default for strings."""
        if isinstance(self.result_details, str):
            self.result_details = ResultDetails(message=self.result_details, level=logging.ERROR)

    def succeeded(self) -> bool:
        """Returns False as this is a failure result.

        Returns:
            bool: Always False
        """
        return False


class ExecutionPayload(Payload):
    pass


class AppPayload(Payload):
    pass


# Type variables for our generic payloads
P = TypeVar("P", bound=RequestPayload)
R = TypeVar("R", bound=ResultPayload)
E = TypeVar("E", bound=ExecutionPayload)
A = TypeVar("A", bound=AppPayload)


class BaseEvent(BaseModel, ABC):
    """Abstract base class for all events."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @abstractmethod
    def get_request(self) -> Payload:
        """Get the request payload for this event.

        Returns:
            Payload: The request payload
        """


class EventRequest[P: Payload](BaseEvent):
    """Request event."""

    request: P
    request_id: str | None = None
    response_topic: str | None = None

    def __init__(self, **data) -> None:
        """Initialize an EventRequest, inferring the generic type if needed."""
        # Call the parent class initializer
        super().__init__(**data)

    def get_request(self) -> P:
        """Get the request payload for this event.

        Returns:
            P: The request payload
        """
        return self.request


class EventRequestBatch(BaseEvent):
    """Wire-only envelope that fans out into N individual EventRequests on ingest.

    Each inner EventRequest carries its own request_id and response_topic, so the
    engine does not need a batch-aware handler: results come back as individual
    EventResultSuccess/Failure messages and the caller correlates them by request_id.
    Use this to dispatch many requests in a single WebSocket frame without paying
    per-request envelope overhead.

    The envelope intentionally does not carry its own request_id/response_topic.
    Identity and routing live on the inner requests, which keeps the engine path
    identical to a stream of individual EventRequest frames.
    """

    requests: list[EventRequest] = Field(default_factory=list)

    def get_request(self) -> Payload:
        """EventRequestBatch is a transport envelope; inspect .requests instead."""
        msg = "EventRequestBatch is a transport envelope; inspect .requests instead."
        raise NotImplementedError(msg)


class EventResult[P: RequestPayload, R: ResultPayload](BaseEvent, ABC):
    """Abstract base class for result events."""

    request: P
    result: R
    request_id: str | None = None
    response_topic: str | None = None
    retained_mode: str | None = None

    def __init__(self, **data) -> None:
        """Initialize an EventResult, inferring the generic types if needed."""
        # Call the parent class initializer
        super().__init__(**data)

    def get_request(self) -> P:
        """Get the request payload for this event.

        Returns:
            P: The request payload
        """
        return self.request

    def get_result(self) -> R:
        """Get the result payload for this event.

        Returns:
            R: The result payload
        """
        return self.result

    @abstractmethod
    def succeeded(self) -> bool:
        """Returns whether this result represents a success or failure.

        Returns:
            bool: True if success, False if failure
        """


class EventResultSuccess(EventResult[P, R]):
    """Success result event."""

    def succeeded(self) -> bool:
        """Returns True as this is a success result.

        Returns:
            bool: Always True
        """
        return True


class EventResultFailure(EventResult[P, R]):
    """Failure result event."""

    def succeeded(self) -> bool:
        """Returns False as this is a failure result.

        Returns:
            bool: Always False
        """
        return False


# The `event_type` values that carry an answer to a request. Derived from the classes so a rename
# cannot leave a transport matching on a name nothing sends. Both carry the request they answer, so a
# dispatcher that only understands requests reads one as a malformed request rather than a response.
RESULT_EVENT_TYPES = frozenset({EventResultSuccess.__name__, EventResultFailure.__name__})


# EXECUTION EVENT BASE (this event type is used for the execution of a Griptape Nodes flow)
class ExecutionEvent[E: ExecutionPayload](BaseEvent):
    payload: E

    def __init__(self, **data) -> None:
        """Initialize an ExecutionEvent, inferring the generic type if needed."""
        # Call the parent class initializer
        super().__init__(**data)

    def get_request(self) -> E:
        """Get the payload for this event.

        Returns:
            E: The execution payload
        """
        return self.payload


# Events sent as part of the lifecycle of the Griptape Nodes application.
class AppEvent[A: AppPayload](BaseEvent):
    payload: A

    def __init__(self, **data) -> None:
        """Initialize an AppEvent, inferring the generic type if needed."""
        # Call the parent class initializer
        super().__init__(**data)

    def get_request(self) -> A:
        """Get the payload for this event.

        Returns:
            A: The app event payload
        """
        return self.payload


class GriptapeNodeEvent(BaseEvent):
    wrapped_event: EventResult

    def get_request(self) -> Payload:
        """Get the request from the wrapped event."""
        return self.wrapped_event.get_request()


class ExecutionGriptapeNodeEvent(BaseEvent):
    wrapped_event: ExecutionEvent

    def get_request(self) -> Payload:
        """Get the request from the wrapped event."""
        return self.wrapped_event.get_request()


@dataclass
class ProgressEvent:
    value: Any = field()
    node_name: str = field()
    parameter_name: str = field()
