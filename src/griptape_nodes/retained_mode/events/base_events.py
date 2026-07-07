from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from griptape_nodes.retained_mode.events.event_converter import (
    converter,
    register_polymorphic_dataclass,
    safe_unstructure,
)

if TYPE_CHECKING:
    import builtins

logger = logging.getLogger(__name__)


def _resolve_payload_type(event_data: dict[str, Any], type_key: str) -> type:
    """Resolve a payload type from a type-name field in the event data.

    Args:
        event_data: The event dictionary (mutated: the type-name key is popped if used).
        type_key: The key in event_data that holds the payload type name (e.g. "request_type").

    Returns:
        The resolved concrete type.

    Raises:
        ValueError: If the type cannot be resolved.
    """
    # Lazy import to avoid circular dependency: payload_registry imports Payload from this module.
    from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry

    type_name = event_data.pop(type_key, None)
    if type_name is None:
        msg = f"Cannot resolve payload type: '{type_key}' not found in event data."
        raise ValueError(msg)

    resolved = PayloadRegistry.get_type(type_name)
    if resolved is None:
        msg = f"Cannot resolve payload type: '{type_name}' is not registered."
        raise ValueError(msg)

    return resolved


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

    def to_json(self, **kwargs) -> str:
        """Serialize this payload to JSON string.

        Returns:
            JSON string representation of the payload
        """
        return json.dumps(safe_unstructure(self), default=str, **kwargs)


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

    # Instance fields for engine and session identification
    _engine_id: ClassVar[str | None] = None
    _session_id: ClassVar[str | None] = None

    engine_id: str | None = Field(default_factory=lambda: BaseEvent._engine_id)
    session_id: str | None = Field(default_factory=lambda: BaseEvent._session_id)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def dict(self, *args, **kwargs) -> dict[str, Any]:
        """Override dict to handle payload serialization and add event_type."""
        result = super().dict(*args, **kwargs)

        # Add event type based on class name
        result["event_type"] = self.__class__.__name__

        # Include payload type information in serialized output
        for field_name, field_value in self.__dict__.items():
            if isinstance(field_value, Payload):
                result[f"{field_name}_type"] = field_value.__class__.__name__

        return result

    def json(self, **kwargs) -> str:
        """Serialize to JSON string."""
        logger = logging.getLogger(__name__)

        def _default(obj: Any) -> str:
            logger.debug(
                "json.dumps fallback hit: type=%s, value=%r",
                type(obj).__name__,
                obj,
            )
            return str(obj)

        return json.dumps(self.dict(), default=_default, **kwargs)

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

    def dict(self, *args, **kwargs) -> dict[str, Any]:
        """Override dict to handle payload serialization."""
        result = super().dict(*args, **kwargs)
        result["request"] = safe_unstructure(self.request)
        return result

    def get_request(self) -> P:
        """Get the request payload for this event.

        Returns:
            P: The request payload
        """
        return self.request

    @classmethod
    def from_dict(cls, data: builtins.dict[str, Any]) -> EventRequest:  # pyright: ignore[reportIncompatibleMethodOverride]
        """Create an event from a dictionary."""
        event_data = data.copy()
        request_data = event_data.pop("request", {})
        resolved_type = _resolve_payload_type(event_data, "request_type")

        request_payload = converter.structure(request_data, resolved_type)
        return cls(request=request_payload, **event_data)


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

    def dict(self, *args, **kwargs) -> dict[str, Any]:
        """Serialize the envelope, recursing into each inner request's own serializer."""
        result = super().dict(*args, **kwargs)
        result["requests"] = [inner.dict() for inner in self.requests]
        return result

    def get_request(self) -> Payload:
        """EventRequestBatch is a transport envelope; inspect .requests instead."""
        msg = "EventRequestBatch is a transport envelope; inspect .requests instead."
        raise NotImplementedError(msg)

    @classmethod
    def from_dict(cls, data: builtins.dict[str, Any]) -> EventRequestBatch:  # pyright: ignore[reportIncompatibleMethodOverride]
        """Create a batch envelope by deserializing each inner request individually."""
        event_data = data.copy()
        raw_requests = event_data.pop("requests", [])
        requests = [EventRequest.from_dict(raw) for raw in raw_requests]
        return cls(requests=requests, **event_data)


_RESULT_FRAMEWORK_FIELDS = frozenset(f.name for f in dataclass_fields(ResultPayload))


def _build_path_tree(paths: list[str]) -> dict:
    # ["a.b", "a.c.d"] -> {"a": {"b": {}, "c": {"d": {}}}}
    # Empty {} at a leaf = keep the value whole.
    #
    # Prefix-wins rule: if "workflows" (keep-whole) and "workflows.name" (narrow) are both
    # requested, "workflows" wins regardless of order. Implemented two ways:
    #   - Skip a child path if any ancestor is already a {} leaf (broad path was added first).
    #   - Always assign the leaf with = {} rather than setdefault, so a later broad path
    #     overwrites children that a narrower path already built.
    tree: dict = {}
    for path in paths:
        parts = path.split(".")
        node = tree
        skip = False
        for part in parts[:-1]:
            if node.get(part) == {}:
                # An ancestor is already a keep-whole leaf; this child path is dominated.
                skip = True
                break
            # setdefault: create the branch if missing, or return the existing one so shared
            # prefixes like ["a.b", "a.c"] converge on the same "a" node.
            node = node.setdefault(part, {})
        if not skip:
            # Unconditional assignment so a broad path ("workflows") that arrives after a
            # narrow one ("workflows.name") still wins by replacing the subtree with {}.
            node[parts[-1]] = {}
    return tree


def _apply_path_tree(data: Any, tree: dict) -> Any:
    # Called by EventResult.dict() to prune the unstructured result dict before WebSocket broadcast.
    # Frontend sets RequestPayload.fields (e.g. ["workflows.*.name"]) to avoid sending 256KB+
    # payloads when only a few fields are needed. Framework fields are re-added by the caller.
    if not isinstance(data, dict):
        return data

    if "*" in tree:
        # result.workflows is dict[str, WorkflowMetadata] keyed by file path — the keys are
        # not field names, so "workflows.name" would look for a key literally called "name" and
        # return {}. "*" means: apply the subtree to every value, ignoring the key.
        subtree = tree["*"]
        if not subtree:
            return dict(data)
        filtered = {}
        for k, v in data.items():
            if isinstance(v, list):
                filtered[k] = [_apply_path_tree(item, subtree) if isinstance(item, dict) else item for item in v]
            else:
                filtered[k] = _apply_path_tree(v, subtree)
        return filtered

    # Named-key traversal: keep only fields present in the tree, drop everything else.
    result = {}
    for key, subtree in tree.items():
        if key not in data:
            continue
        value = data[key]
        if not subtree:
            result[key] = value  # leaf — keep value whole
        elif isinstance(value, list):
            result[key] = [_apply_path_tree(item, subtree) if isinstance(item, dict) else item for item in value]
        else:
            result[key] = _apply_path_tree(value, subtree)
    return result


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

    def dict(self, *args, **kwargs) -> dict[str, Any]:
        """Override dict to handle payload serialization."""
        result = super().dict(*args, **kwargs)
        result["request"] = safe_unstructure(self.request)
        result_dict = safe_unstructure(self.result)
        if self.request.fields is not None and self.result.succeeded():
            tree = _build_path_tree(self.request.fields)
            for top_key in tree:
                if top_key != "*" and top_key not in result_dict:
                    logger.warning(
                        "fields filter: '%s' not found in %s result — typo or version skew?",
                        top_key,
                        type(self.result).__name__,
                    )
            filtered = _apply_path_tree(result_dict, tree)
            for fw_field in _RESULT_FRAMEWORK_FIELDS:
                if fw_field in result_dict:
                    filtered.setdefault(fw_field, result_dict[fw_field])
            result_dict = filtered
        result["result"] = result_dict
        if self.retained_mode:
            result["retained_mode"] = self.retained_mode
        return result

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

    @classmethod
    def from_dict(cls, data: builtins.dict[str, Any]) -> EventResult:  # pyright: ignore[reportIncompatibleMethodOverride]
        """Create an event from a dictionary."""
        event_data = data.copy()
        request_data = event_data.pop("request", {})
        result_data = event_data.pop("result", {})

        resolved_req_type = _resolve_payload_type(event_data, "request_type")
        resolved_res_type = _resolve_payload_type(event_data, "result_type")

        request_payload = converter.structure(request_data, resolved_req_type)
        result_payload = converter.structure(result_data, resolved_res_type)
        return cls(request=request_payload, result=result_payload)


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


# EXECUTION EVENT BASE (this event type is used for the execution of a Griptape Nodes flow)
class ExecutionEvent[E: ExecutionPayload](BaseEvent):
    payload: E

    def __init__(self, **data) -> None:
        """Initialize an ExecutionEvent, inferring the generic type if needed."""
        # Call the parent class initializer
        super().__init__(**data)

    def dict(self, *args, **kwargs) -> dict[str, Any]:
        """Override dict to handle payload serialization."""
        result = super().dict(*args, **kwargs)
        result["payload"] = safe_unstructure(self.payload)
        return result

    def get_request(self) -> E:
        """Get the payload for this event.

        Returns:
            E: The execution payload
        """
        return self.payload

    @classmethod
    def from_dict(cls, data: builtins.dict[str, Any]) -> ExecutionEvent:  # pyright: ignore[reportIncompatibleMethodOverride]
        """Create an event from a dictionary."""
        event_data = data.copy()
        payload_data = event_data.pop("payload", {})
        resolved_type = _resolve_payload_type(event_data, "payload_type")

        event_payload = converter.structure(payload_data, resolved_type)
        return cls(payload=event_payload, **event_data)


# Events sent as part of the lifecycle of the Griptape Nodes application.
class AppEvent[A: AppPayload](BaseEvent):
    payload: A

    def __init__(self, **data) -> None:
        """Initialize an AppEvent, inferring the generic type if needed."""
        # Call the parent class initializer
        super().__init__(**data)

    def dict(self, *args, **kwargs) -> dict[str, Any]:
        """Override dict to handle payload serialization."""
        result = super().dict(*args, **kwargs)
        result["payload"] = safe_unstructure(self.payload)
        return result

    def get_request(self) -> A:
        """Get the payload for this event.

        Returns:
            A: The app event payload
        """
        return self.payload

    @classmethod
    def from_dict(cls, data: builtins.dict[str, Any]) -> AppEvent:  # pyright: ignore[reportIncompatibleMethodOverride]
        """Create an event from a dictionary."""
        event_data = data.copy()
        payload_data = event_data.pop("payload", {})
        resolved_type = _resolve_payload_type(event_data, "payload_type")

        event_payload = converter.structure(payload_data, resolved_type)
        return cls(payload=event_payload, **event_data)


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


# Register ResultDetail subclasses (e.g. StrictModeViolationDetail) with the
# converter so a ``list[ResultDetail]`` round-trip preserves subclass identity
# and subclass-only fields (rule_id, severity, subject, library_name) instead
# of degrading every entry to a bare ResultDetail. Must run after every
# subclass is defined.
register_polymorphic_dataclass(ResultDetail)
