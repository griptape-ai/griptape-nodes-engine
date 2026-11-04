from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from griptape_nodes.retained_mode.events.base_events import RequestPayload, RequestSender, ResultPayload

F = TypeVar("F", bound="Callable[..., Any]")

_HANDLED_REQUEST_TYPES_ATTR = "__handled_request_types__"
_SENDER_RULES_ATTR = "__sender_rules__"


class RequestScope(StrEnum):
    """What a request acts on, read from its handler's `workflow` parameter when the handler registers.

    HOST: no `workflow` parameter. The engine rather than one workflow: dispatch binds no workflow,
        ignores a workflow id on the request envelope and never rejects one.
    WORKFLOW: `workflow: WorkflowRuntime`. One open workflow: the one the request envelope names, else
        the caller's, else the only open one.
    OPTIONAL_WORKFLOW: `workflow: WorkflowRuntime | None = None`. Works without a workflow: the one the
        envelope names, else the caller's, else none. A request type in
        `ONLY_OPEN_UNTIL_CLIENTS_NAME` gets the only open one before none, and fails when several are open.

    A library handler with no `workflow` parameter, and an engine handler listed in
    `AMBIENT_UNTIL_MIGRATED`, have no scope: dispatch calls them with the request alone, bound to the
    caller's workflow or the only open one.
    """

    HOST = "host"
    WORKFLOW = "workflow"
    OPTIONAL_WORKFLOW = "optional_workflow"


class ForwardingHandler(ABC):
    """A handler that sends some requests to another engine instead of answering them.

    Registered with `EventManager.replace_manager_for_request_type` in place of `original`, whose
    scope it keeps. Dispatch asks `forwards_now` once per request, then calls `forward` with the
    request alone, or `handle_locally` with the request and the `workflow` the scope passes.
    """

    original: Callable[..., Any]

    @abstractmethod
    def forwards_now(self) -> bool:
        """Whether a request dispatched now is forwarded."""

    @abstractmethod
    async def forward(self, request: RequestPayload) -> ResultPayload:
        """Send `request` to the other engine and return its answer."""

    @abstractmethod
    async def handle_locally(self, request: RequestPayload, **kwargs: Any) -> ResultPayload:
        """Hand `request` and `kwargs` (the `workflow` its scope passes) to `original`."""


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
    run it, and, with `owner`, only from the engine that owns what the request names. The method's
    `workflow` parameter declares what the request acts on (see `RequestScope`).
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


def takes_workflow_parameter(handler: Callable[..., Any]) -> bool:
    """Whether `handler`'s signature names a `workflow` parameter."""
    return "workflow" in inspect.signature(handler).parameters


def scope_from_signature(handler: Callable[..., Any]) -> RequestScope | None:
    """The scope `handler`'s `workflow` parameter declares, or None when it has no `workflow` parameter.

    Reads `inspect.signature`, which follows `functools.wraps` and a `__signature__` attribute, so a
    wrapper declares the scope of the handler it wraps.

    Raises:
        TypeError: The signature can't say. The handler takes `*args` or `**kwargs` without naming
            `workflow`, as a wrapper that hides its handler's signature does, or its `workflow`
            parameter defaults to something other than None.
    """
    name = getattr(handler, "__qualname__", repr(handler))
    parameters = inspect.signature(handler).parameters
    parameter = parameters.get("workflow")
    if parameter is None:
        variadic = (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        if any(other.kind in variadic for other in parameters.values()):
            msg = (
                f"Attempted to register the request handler '{name}'. Failed because it takes *args or **kwargs, "
                "so whether it takes a workflow can't be read from its signature. Name the parameters it takes, "
                "or wrap it with functools.wraps."
            )
            raise TypeError(msg)
        return None
    if parameter.default is None:
        return RequestScope.OPTIONAL_WORKFLOW
    if parameter.default is not inspect.Parameter.empty:
        msg = (
            f"Attempted to register the request handler '{name}'. Failed because its `workflow` parameter "
            f"defaults to {parameter.default!r}. Give it no default, or default it to None."
        )
        raise TypeError(msg)
    return RequestScope.WORKFLOW
