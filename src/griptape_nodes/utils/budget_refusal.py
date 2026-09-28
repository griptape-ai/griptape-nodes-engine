"""Recognize, word, and re-recognize a Griptape Cloud budget refusal.

When a HARD budget has no room, Cloud refuses the call with HTTP 403 and a body
naming every budget that refused. :func:`refusal_from_body` reads that body,
:func:`describe` and :func:`describe_reply` word it for the artist, and
:func:`is_budget_halt` recognizes the halt again after it crosses a worker boundary.

The halt names the node and the budgets and sends the artist to their
administrator, short enough for the editor's Run blocked bar. Cloud's own
``message`` and the credit figures go to :func:`log_line` only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, NamedTuple
from urllib.parse import urlsplit

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

BUDGET_EXCEEDED_CODE = "budget_exceeded"
"""The code Cloud sets on every budget refusal, and the whole recognition test."""

BUDGET_HALT_PREFIX = "Budget stopped this run."
"""Opening words of every halt message.

Where only the message survives (a worker boundary, or a library call site that
drops the exception), these words are how a halt is recognized. A test pins them.
"""

BUDGET_REPLY_HALT_PREFIX = "Budget stopped this reply."
"""Opening words of a halt in the sidebar chat, which the editor uses to skip its error toast."""

_REMEDY = "Contact your Griptape administrator."
"""The closing sentence of every halt: the fix is on Griptape Cloud, whatever the cause."""

_MISSING = object()
"""Sentinel for "this exception has no body attribute", since a body of None is valid."""


@dataclass(frozen=True)
class BlockedBudget:
    """One budget that refused the call, and the figures it refused on.

    Only ``budget_name`` is required, so a refusal with fields this engine does
    not expect still names the budget.
    """

    budget_name: str
    budget_id: str | None = None
    scope_type: str | None = None
    reset_period: str | None = None
    enforcement: str | None = None
    limit_credits: int | None = None
    spent_credits: int | None = None
    remaining_credits: int | None = None
    requested_credits: int | None = None
    frozen: bool = False


@dataclass(frozen=True)
class BudgetRefusal:
    """Everything Cloud said about why it refused the call."""

    budgets: tuple[BlockedBudget, ...] = field(default_factory=tuple)
    cloud_message: str | None = None
    effective_remaining_credits: int | None = None
    spend_id: str | None = None


class BudgetExceededError(Exception):
    """A budget refused this call, so the run stops.

    ``refusal`` does not survive a worker boundary, so the message is worded
    before raising. ``node_name`` is None when the raiser (a Cloud driver, say)
    did not know which node it was calling for; the node manager then re-words
    the halt to name it.
    """

    def __init__(self, message: str, refusal: BudgetRefusal, *, node_name: str | None = None) -> None:
        super().__init__(message)
        self.refusal = refusal
        self.node_name = node_name


class CloudHttpFailure(NamedTuple):
    """An HTTP failure found on an exception chain, and what it carried."""

    status: int
    body: object | None


def refusal_from_exception(exc: BaseException, *, cloud_host: str | Callable[[], str]) -> BudgetRefusal | None:
    """Return the budget refusal an exception is carrying, or None if it is not one.

    Args:
        exc: The exception to inspect, including anything it was raised from.
        cloud_host: Hostname of the Griptape Cloud deployment in use, or a
            function returning it. HTTP errors from other hosts are ignored. Pass
            the function to defer reading the secret behind it until a failure
            actually carries a response; it is called at most once.

    Returns:
        The refusal, or None when this is not a budget refusal from Cloud.
    """
    failure = _cloud_http_failure(exc, _host_resolver(cloud_host))
    if failure is None:
        return None
    if failure.status != HTTPStatus.FORBIDDEN:
        return None
    return refusal_from_body(failure.body)


def refusal_from_body(body: object) -> BudgetRefusal | None:
    """Return the refusal a 403 body describes, or None if it does not describe one.

    Accepts the flat body, the OpenAI-compatible body nested under ``error``, and
    the inner object an OpenAI SDK leaves after unwrapping ``error``.

    Args:
        body: The parsed response body, of any type.

    Returns:
        The refusal, or None when the body is not a budget refusal.
    """
    if not isinstance(body, dict):
        return None

    error = body.get("error")
    if isinstance(error, dict):
        payload: Mapping[str, Any] = error
        code = error.get("code")
    elif error is None:
        payload = body
        code = body.get("code")
    else:
        payload = body
        code = error

    if code != BUDGET_EXCEEDED_CODE:
        return None

    entries = payload.get("blocked_by")
    if not isinstance(entries, list):
        return None

    budgets = tuple(budget for budget in (_budget_from_entry(entry) for entry in entries) if budget is not None)
    if not budgets:
        # Nothing names a budget, so fall back to the generic error.
        return None

    return BudgetRefusal(
        budgets=budgets,
        cloud_message=_optional_str(payload.get("message")),
        effective_remaining_credits=_optional_int(payload.get("effective_remaining_credits")),
        spend_id=_optional_str(payload.get("spend_id")),
    )


def describe(refusal: BudgetRefusal, *, node_name: str | None = None) -> str:
    """Word a refusal for the artist whose run just stopped.

    Args:
        refusal: The parsed refusal.
        node_name: The node whose call was refused, when known.

    Returns:
        A short message naming the node and every budget that refused, and sending
        the artist to their administrator.
    """
    if node_name:
        subject = f"'{node_name}'"
    else:
        subject = "The next call"
    return f"{BUDGET_HALT_PREFIX} {subject} was blocked by {_blocked_by(refusal)}. {_REMEDY}"


def describe_reply(refusal: BudgetRefusal) -> str:
    """Word a refusal for the artist whose sidebar chat reply just stopped.

    Args:
        refusal: The parsed refusal.

    Returns:
        A short message naming every budget that refused, and sending the artist
        to their administrator.
    """
    return f"{BUDGET_REPLY_HALT_PREFIX} It was blocked by {_blocked_by(refusal)}. {_REMEDY}"


def log_line(refusal: BudgetRefusal) -> str:
    """Summarize a refusal for the engine log, including the figures the artist is not shown.

    ``spend_id`` points at the BLOCKED receipt Cloud writes for every refusal.
    """
    budgets = "; ".join(
        f"{budget.budget_name} (id={budget.budget_id}, scope={budget.scope_type}, "
        f"period={budget.reset_period}, enforcement={budget.enforcement}, "
        f"limit={budget.limit_credits}, spent={budget.spent_credits}, "
        f"remaining={budget.remaining_credits}, requested={budget.requested_credits}, "
        f"frozen={budget.frozen})"
        for budget in refusal.budgets
    )
    return (
        f"Griptape Cloud refused an invocation over budget. spend_id={refusal.spend_id} "
        f"effective_remaining_credits={refusal.effective_remaining_credits} "
        f"cloud_message={refusal.cloud_message!r} blocked_by: {budgets}"
    )


def halt_message(exception: BaseException | None = None, message: str | None = None) -> str | None:
    """Return a budget halt's own wording from wherever it has ended up, or None.

    The halt gets wrapped on its way out, so this walks the ``__cause__`` chain
    and returns the halt's own wording. It matches a ``BudgetExceededError``, a
    ``ForwardedException`` whose original type is one (from a worker), or, when
    there is no exception, a message starting with :data:`BUDGET_HALT_PREFIX`.

    Args:
        exception: The exception that ended the node, including anything it was
            raised from, when there is one.
        message: The failure message, when there is one.

    Returns:
        The halt's wording, or None when a budget refusal is not what stopped
        this run.
    """
    seen: set[int] = set()
    current: BaseException | None = exception
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, BudgetExceededError):
            return str(current)
        original_type = getattr(current, "original_type", None)
        if isinstance(original_type, str) and original_type.endswith(f".{BudgetExceededError.__name__}"):
            return str(current)
        current = current.__cause__

    if message is not None and message.startswith(BUDGET_HALT_PREFIX):
        return message
    return None


def is_budget_halt(exception: BaseException | None = None, message: str | None = None) -> bool:
    """Return whether a failure is a budget halt, on either side of a worker boundary.

    Args:
        exception: The exception that ended the node, when there is one.
        message: The failure message, when there is one.

    Returns:
        True when a budget refusal is what stopped this run.
    """
    return halt_message(exception, message) is not None


def _host_resolver(cloud_host: str | Callable[[], str]) -> Callable[[], str]:
    """Return a one-shot reader for the Cloud host, from either a value or a function."""
    if not callable(cloud_host):
        return lambda: cloud_host

    resolved: list[str] = []

    def read_once() -> str:
        if not resolved:
            resolved.append(cloud_host())
        return resolved[0]

    return read_once


def _cloud_http_failure(exc: BaseException, resolve_host: Callable[[], str]) -> CloudHttpFailure | None:
    """Find the Griptape Cloud HTTP failure on an exception chain.

    Three shapes, one per HTTP client that spends credits:

    - ``httpx.HTTPStatusError``: host-scoped, so a 403 from an MCP server or a
      third-party API is not read as a budget refusal.
    - ``requests.exceptions.HTTPError``, from the Griptape SDK's Cloud drivers:
      host-scoped too, and duck-typed because ``requests`` is not an engine
      dependency.
    - Pydantic AI's ``ModelHTTPError``, from the sidebar chat: ``status_code`` and
      ``body`` with no URL, so it relies on the ``budget_exceeded`` code alone.
      Duck-typed to keep ``pydantic_ai`` out of this module's imports.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, httpx.HTTPStatusError):
            if current.request.url.host != resolve_host():
                return None
            return CloudHttpFailure(status=current.response.status_code, body=_body_of(current.response))
        status = getattr(current, "status_code", None)
        body = getattr(current, "body", _MISSING)
        if isinstance(status, int) and body is not _MISSING:
            return CloudHttpFailure(status=status, body=_coerce_body(body))
        response_failure = _response_failure(getattr(current, "response", None), resolve_host)
        if response_failure is not None:
            return response_failure
        current = current.__cause__
    return None


def _response_failure(response: object, resolve_host: Callable[[], str]) -> CloudHttpFailure | None:
    """Read a ``requests``-style response off an exception, host-scoped, or return None."""
    status = getattr(response, "status_code", None)
    if not isinstance(status, int):
        return None
    url = getattr(response, "url", None)
    if not isinstance(url, str):
        return None
    if urlsplit(url).hostname != resolve_host():
        return None
    parse = getattr(response, "json", None)
    if not callable(parse):
        return None

    return CloudHttpFailure(status=status, body=_parsed_body(parse))


def _parsed_body(parse: Callable[[], object]) -> object | None:
    """Call a response's JSON parser, returning None for an empty or non-JSON body."""
    try:
        return parse()
    except ValueError:
        return None


def _body_of(response: httpx.Response) -> object | None:
    """Parse a response body, returning None when it is not JSON or a stream never read it."""
    try:
        return response.json()
    except (ValueError, httpx.ResponseNotRead):
        return None


def _coerce_body(body: object) -> object | None:
    """Return a body as parsed JSON, whether it arrived parsed or as text."""
    if isinstance(body, str):
        try:
            return json.loads(body)
        except ValueError:
            return None
    return body


def _budget_from_entry(entry: object) -> BlockedBudget | None:
    """Read one ``blocked_by`` entry, or None when it names no budget."""
    if not isinstance(entry, dict):
        return None
    name = _optional_str(entry.get("budget_name"))
    if not name:
        return None
    return BlockedBudget(
        budget_name=name,
        budget_id=_optional_str(entry.get("budget_id")),
        scope_type=_optional_str(entry.get("scope_type")),
        reset_period=_optional_str(entry.get("reset_period")),
        enforcement=_optional_str(entry.get("enforcement")),
        limit_credits=_optional_int(entry.get("limit_credits")),
        spent_credits=_optional_int(entry.get("spent_credits")),
        remaining_credits=_optional_int(entry.get("remaining_credits")),
        requested_credits=_optional_int(entry.get("requested_credits")),
        frozen=entry.get("frozen") is True,
    )


def _blocked_by(refusal: BudgetRefusal) -> str:
    """Name every budget that refused, as the object of "was blocked by"."""
    if len(refusal.budgets) == 1:
        return f"the budget {_label(refusal.budgets[0])}"
    return f"the budgets {_joined([_label(budget) for budget in refusal.budgets])}"


def _label(budget: BlockedBudget) -> str:
    """Name a budget the way the budget page does, marking a frozen one, which refuses at any headroom."""
    if budget.frozen:
        return f'"{budget.budget_name}" (frozen)'
    return f'"{budget.budget_name}"'


def _joined(names: list[str]) -> str:
    """Join names as prose: "a and b", "a, b and c"."""
    if len(names) <= 2:  # noqa: PLR2004  # two names join with "and" alone
        return " and ".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _optional_str(value: object) -> str | None:
    """Return a string field, or None when it is missing or the wrong type."""
    if isinstance(value, str):
        return value
    return None


def _optional_int(value: object) -> int | None:
    """Return an integer field, or None when it is missing or the wrong type."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None
