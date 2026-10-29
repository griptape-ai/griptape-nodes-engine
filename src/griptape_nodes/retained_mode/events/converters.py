from __future__ import annotations

import json
import logging
import weakref
from collections import Counter
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from typing import TYPE_CHECKING, Any

from cattrs.preconf.json import JsonConverter
from cattrs.preconf.json import configure_converter as configure_json_converter
from cattrs.strategies import include_subclasses

from griptape_nodes.retained_mode.events.base_events import (
    AppEvent,
    BaseEvent,
    EventRequest,
    EventRequestBatch,
    EventResult,
    EventResultFailure,
    ExecutionEvent,
    Payload,
    RequestPayload,
    ResultDetail,
    ResultPayload,
)
from griptape_nodes.retained_mode.events.generic_events import GenericResultFailure
from griptape_nodes.retained_mode.events.path_filter import apply_path_tree, build_path_tree
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry
from griptape_nodes.serialization.hooks import configure_converter, with_converter
from griptape_nodes.serialization.type_names import TypeNameError
from griptape_nodes.serialization.values import (
    DisplayValue,
    Value,
    ValueEncodeError,
    dump_json,
    encode_for_display,
    encode_value,
    untag,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from cattrs import Converter

logger = logging.getLogger(__name__)


class EventSerializationError(TypeError):
    """A payload holds a value with no JSON form, so it cannot be sent."""


@dataclass(frozen=True)
class ResultJson:
    """A result as JSON to send, or a failure in its place when the result could not be sent."""

    text: str
    error: EventSerializationError | None = None


class EventConverter(JsonConverter):
    """Build one with ``make_event_converter``.

    After ``copy()``, hooks from ``register_polymorphic_dataclass`` and the
    ``_cattrs_structure``/``_cattrs_unstructure`` class methods still call the original converter.
    """

    def dumps(self, obj: Any, unstructure_as: Any = None, **kwargs: Any) -> str:
        """Raise ``EventSerializationError`` if ``obj`` holds a value with no JSON form."""
        data = _unstructure(obj, self, unstructure_as)
        return _to_json(data, _described_type(obj, data), **kwargs)

    def dumps_result(self, event: EventResult, **kwargs: Any) -> ResultJson:
        """Write ``event``, or a failure naming why it could not be sent, so the requester always hears back."""
        try:
            return ResultJson(text=self.dumps(event, **kwargs))
        except EventSerializationError as error:
            logger.error("Could not send %s for %s: %s", type(event).__name__, type(event.request).__name__, error)
            return ResultJson(text=self._failure_dumps(event, error, **kwargs), error=error)

    def _failure_dumps(self, event: EventResult, error: EventSerializationError, **kwargs: Any) -> str:
        try:
            # Through JSON: unstructuring alone passes some values through for dump_json to reject.
            request = json.loads(_to_json(_unstructure(event.request, self), type(event.request).__name__))
        except EventSerializationError:
            request = {"request_id": event.request.request_id}
        failure: dict[str, Any] = {
            "event_type": EventResultFailure.__name__,
            "request_type": type(event.request).__name__,
            "request": request,
            "result_type": GenericResultFailure.__name__,
            "result": _unstructure(GenericResultFailure(result_details=str(error)), self),
            "request_id": event.request_id,
            "response_topic": event.response_topic,
            "open_workflow_id": event.open_workflow_id,
        }
        if event.retained_mode:
            failure["retained_mode"] = event.retained_mode
        return _to_json(failure, GenericResultFailure.__name__, **kwargs)


def make_event_converter(*, value: Callable[[Any], Any], display: Callable[[Any], Any]) -> EventConverter:
    """``value`` and ``display`` encode fields typed ``Value`` and ``DisplayValue``.

    Both tagged and plain JSON values decode with the shared hooks.
    """
    conv = EventConverter(unstruct_collection_overrides={AbstractSet: list, Counter: dict})
    configure_json_converter(conv)
    conv.register_unstructure_hook(Value, value)
    conv.register_unstructure_hook(DisplayValue, display)
    configure_converter(conv)
    # Register after the shared pydantic hooks so event hooks take precedence.
    _configure_event_hooks(conv)
    for root in _polymorphic_roots:
        include_subclasses(root, conv)
    _event_converters.add(conv)
    return conv


def register_polymorphic_dataclass(cls: type) -> None:
    """Register ``cls`` and its subclasses on every ``EventConverter``, regardless of construction order.

    Without this, a field typed ``list[BaseClass]`` round-trips every entry as the base class and
    silently drops subclass-only fields. Call it once per polymorphic root, after every subclass is
    declared. Subclasses are told apart by their unique field names.
    """
    _polymorphic_roots.append(cls)
    for conv in _event_converters:
        include_subclasses(cls, conv)


def _to_json(data: Any, payload_type: str, **kwargs: Any) -> str:
    try:
        return dump_json(data, **kwargs)
    except (TypeError, ValueError) as error:
        msg = f"Attempted to send a '{payload_type}'. Failed because: {error}"
        raise EventSerializationError(msg) from error


def _unstructure(obj: Any, conv: Converter, unstructure_as: Any = None) -> Any:
    try:
        return conv.unstructure(obj, unstructure_as=unstructure_as)
    except (ValueEncodeError, TypeNameError) as error:
        msg = f"Attempted to send a '{type(obj).__name__}'. Failed because: {error}"
        raise EventSerializationError(msg) from error


def _described_type(obj: Any, data: Any) -> str:
    """Name an event by its payload, so a send error names what the user asked for."""
    if isinstance(obj, Payload) or not isinstance(data, dict):
        return type(obj).__name__
    return data.get("result_type") or data.get("payload_type") or data.get("request_type") or type(obj).__name__


def _untagged_value(value: Any) -> Any:
    return untag(encode_value(value))


def _untagged_display_value(value: Any) -> Any:
    return untag(encode_for_display(value))


def _configure_event_hooks(conv: Converter) -> None:
    def is_event(base: type) -> Callable[[Any], bool]:
        return lambda cls: isinstance(cls, type) and issubclass(cls, base)

    # RequestPayload fields need a type name to select the concrete request when read back.
    # A direct hook would also catch concrete requests; match exactly RequestPayload.
    def is_any_request(cls: Any) -> bool:
        return cls is RequestPayload

    conv.register_unstructure_hook_factory(is_any_request, with_converter(_unstructure_any_request))
    conv.register_structure_hook_factory(is_any_request, with_converter(_structure_any_request))

    conv.register_unstructure_hook_func(is_event(BaseEvent), _envelope)
    conv.register_unstructure_hook_factory(is_event(EventRequest), with_converter(_unstructure_request))
    conv.register_unstructure_hook_factory(is_event(EventRequestBatch), with_converter(_unstructure_batch))
    conv.register_unstructure_hook_factory(is_event(EventResult), with_converter(_unstructure_result))
    conv.register_unstructure_hook_factory(is_event(ExecutionEvent), with_converter(_unstructure_payload_event))
    conv.register_unstructure_hook_factory(is_event(AppEvent), with_converter(_unstructure_payload_event))

    conv.register_structure_hook_factory(is_event(EventRequest), with_converter(_structure_request))
    conv.register_structure_hook_factory(is_event(EventRequestBatch), with_converter(_structure_batch))
    conv.register_structure_hook_factory(is_event(EventResult), with_converter(_structure_result))
    conv.register_structure_hook_factory(is_event(ExecutionEvent), with_converter(_structure_payload_event))
    conv.register_structure_hook_factory(is_event(AppEvent), with_converter(_structure_payload_event))


def _unstructure_any_request(request: RequestPayload, conv: Converter) -> dict[str, Any]:
    request_type = type(request)
    if PayloadRegistry.get_type(request_type.__name__) is not request_type:
        msg = f"A '{request_type.__name__}' request is not registered, so it could not be read back."
        raise ValueEncodeError(msg)
    return {"request_type": request_type.__name__, "request": conv.unstructure(request, request_type)}


def _structure_any_request(data: dict[str, Any], _cls: Any, conv: Converter) -> RequestPayload:
    request_type = PayloadRegistry.get_type(data["request_type"])
    if request_type is None or not issubclass(request_type, RequestPayload):
        msg = f"'{data['request_type']}' is not a registered request."
        raise ValueError(msg)
    return conv.structure(data["request"], request_type)


def _envelope(event: BaseEvent, exclude: set[str] | None = None) -> dict[str, Any]:
    """Exclude payload fields to avoid a pydantic walk that converter hooks would overwrite.

    ``event_type`` and the ``{field}_type`` entries are part of the wire format: reading resolves
    the concrete payload class from ``{field}_type``, and consumers dispatch on ``event_type``.
    """
    result = event.model_dump(exclude=exclude)
    result["event_type"] = type(event).__name__
    for field_name, field_value in event.__dict__.items():
        if isinstance(field_value, Payload):
            result[f"{field_name}_type"] = type(field_value).__name__
    return result


def _unstructure_request(event: EventRequest, conv: Converter) -> dict[str, Any]:
    result = _envelope(event, exclude={"request"})
    result["request"] = _unstructure(event.request, conv)
    return result


def _unstructure_batch(event: EventRequestBatch, conv: Converter) -> dict[str, Any]:
    result = _envelope(event, exclude={"requests"})
    result["requests"] = [conv.unstructure(inner) for inner in event.requests]
    return result


_RESULT_FRAMEWORK_FIELDS = frozenset(field.name for field in dataclass_fields(ResultPayload))


def _unstructure_result(event: EventResult, conv: Converter) -> dict[str, Any]:
    result = _envelope(event, exclude={"request", "result"})
    result["request"] = _unstructure(event.request, conv)
    result_dict = _unstructure(event.result, conv)
    if event.request.fields is not None and event.result.succeeded():
        tree = build_path_tree(event.request.fields)
        filtered = apply_path_tree(result_dict, tree)
        # Callers need result_details and altered_workflow_state to handle the response,
        # even when fields omits them.
        for framework_field in _RESULT_FRAMEWORK_FIELDS:
            if framework_field in result_dict:
                filtered.setdefault(framework_field, result_dict[framework_field])
        result_dict = filtered
    result["result"] = result_dict
    if event.retained_mode:
        result["retained_mode"] = event.retained_mode
    return result


def _unstructure_payload_event(event: ExecutionEvent | AppEvent, conv: Converter) -> dict[str, Any]:
    result = _envelope(event, exclude={"payload"})
    result["payload"] = _unstructure(event.payload, conv)
    return result


def _structure_request(data: dict[str, Any], cls: type[EventRequest], conv: Converter) -> EventRequest:
    event_data = data.copy()
    request_data = event_data.pop("request", {})
    request_type = _resolve_payload_type(event_data, "request_type")
    return cls(request=conv.structure(request_data, request_type), **event_data)


def _structure_batch(data: dict[str, Any], cls: type[EventRequestBatch], conv: Converter) -> EventRequestBatch:
    event_data = data.copy()
    raw_requests = event_data.pop("requests", [])
    requests = [conv.structure(raw, EventRequest) for raw in raw_requests]
    return cls(requests=requests, **event_data)


def _structure_result(data: dict[str, Any], cls: type[EventResult], conv: Converter) -> EventResult:
    event_data = data.copy()
    request_data = event_data.pop("request", {})
    result_data = event_data.pop("result", {})
    request_type = _resolve_payload_type(event_data, "request_type")
    result_type = _resolve_payload_type(event_data, "result_type")
    return cls(request=conv.structure(request_data, request_type), result=conv.structure(result_data, result_type))


def _structure_payload_event[T: ExecutionEvent | AppEvent](data: dict[str, Any], cls: type[T], conv: Converter) -> T:
    event_data = data.copy()
    payload_data = event_data.pop("payload", {})
    payload_type = _resolve_payload_type(event_data, "payload_type")
    return cls(payload=conv.structure(payload_data, payload_type), **event_data)


def _resolve_payload_type(event_data: dict[str, Any], type_key: str) -> type:
    type_name = event_data.pop(type_key, None)
    if type_name is None:
        msg = f"Cannot resolve payload type: '{type_key}' not found in event data."
        raise ValueError(msg)
    resolved = PayloadRegistry.get_type(type_name)
    if resolved is None:
        msg = f"Cannot resolve payload type: '{type_name}' is not registered."
        raise ValueError(msg)
    return resolved


_event_converters: weakref.WeakSet[EventConverter] = weakref.WeakSet()
_polymorphic_roots: list[type] = [ResultDetail]

engine = make_event_converter(value=encode_value, display=encode_for_display)
"""Writes for another engine process: parameter values tagged with their type."""

client = make_event_converter(value=_untagged_value, display=_untagged_display_value)
"""Writes for a client, such as the editor or an MCP agent: parameter values as plain JSON."""
