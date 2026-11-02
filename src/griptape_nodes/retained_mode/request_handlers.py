from __future__ import annotations

import inspect
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from griptape_nodes.retained_mode.events.base_events import RequestPayload, RequestSender

F = TypeVar("F", bound="Callable[..., Any]")

_HANDLED_REQUEST_TYPES_ATTR = "__handled_request_types__"
_SENDER_RULES_ATTR = "__sender_rules__"


class RequiredSender(StrEnum):
    """Who must have sent a request for its handler to run. `EventManager` refuses any other."""

    # Another engine, named on the request's envelope.
    ENGINE = "engine"
    # Another engine, from any of its sessions, as for the request that registers the session with this engine.
    ANY_SESSION = "any_session"
    # Another engine, from a session that registered with this engine, as `EventManager.recognize_sessions` tells.
    SESSION = "session"


@dataclass(frozen=True)
class SenderRule:
    """Which other engines may send a request type. Requests no rule admits are refused.

    `owner`, called on the handler's owner with the request and its sender, names the one engine
    allowed to send that request, such as the engine a worker serves, or returns None when the
    request names nothing that engine owns, which the handler then answers.
    """

    required: RequiredSender
    owner: Callable[[Any, Any, RequestSender], str | None] | None = None


def handles(
    *request_types: type[RequestPayload],
    sender: RequiredSender | None = None,
    owner: Callable[[Any, Any, RequestSender], str | None] | None = None,
) -> Callable[[F], F]:
    """Mark a method as the handler for `request_types`. `EventManager.register_request_handlers` wires it up.

    Requests another engine sends are refused unless the handler names `sender`: then only they
    run it, and, with `owner`, only from the engine that owns what the request names.
    """
    # A bare `@handles` passes the method itself here and would otherwise mark nothing.
    if not request_types or not all(inspect.isclass(request_type) for request_type in request_types):
        msg = f"@handles takes one or more request types, e.g. @handles(SomeRequest). Got {request_types!r}."
        raise TypeError(msg)
    if owner is not None and sender is None:
        msg = "@handles takes owner= only with sender=, since only another engine's request has an owner to check."
        raise TypeError(msg)

    def mark(method: F) -> F:
        # Marks set on the wrapper would be skipped, since the registration walk reads `__func__`.
        if isinstance(method, (staticmethod, classmethod)):
            msg = f"@handles must sit directly above `def`, below any @staticmethod or @classmethod. Got {method!r}."
            raise TypeError(msg)
        setattr(method, _HANDLED_REQUEST_TYPES_ATTR, (*handled_request_types(method), *request_types))
        if sender is not None:
            rule = SenderRule(required=sender, owner=owner)
            setattr(method, _SENDER_RULES_ATTR, {**sender_rules(method), **dict.fromkeys(request_types, rule)})
        return method

    return mark


def handled_request_types(attr: object) -> tuple[type[RequestPayload], ...]:
    """Empty for anything not marked with `@handles`. Looks through @staticmethod and @classmethod."""
    return getattr(getattr(attr, "__func__", attr), _HANDLED_REQUEST_TYPES_ATTR, ())


def sender_rules(attr: object) -> dict[type[RequestPayload], SenderRule]:
    """The sender rule for each request type `attr` handles that another engine may send."""
    return getattr(getattr(attr, "__func__", attr), _SENDER_RULES_ATTR, {})
