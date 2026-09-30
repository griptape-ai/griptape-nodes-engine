from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.events.base_events import RequestPayload

F = TypeVar("F", bound="Callable[..., Any]")

_HANDLED_REQUEST_TYPES_ATTR = "__handled_request_types__"


def handles(*request_types: type[RequestPayload]) -> Callable[[F], F]:
    """Mark a method as the handler for `request_types`. `EventManager.register_request_handlers` wires it up."""

    def mark(method: F) -> F:
        setattr(method, _HANDLED_REQUEST_TYPES_ATTR, (*handled_request_types(method), *request_types))
        return method

    return mark


def handled_request_types(attr: object) -> tuple[type[RequestPayload], ...]:
    """Empty for anything not marked with `@handles`."""
    return getattr(attr, _HANDLED_REQUEST_TYPES_ATTR, ())
