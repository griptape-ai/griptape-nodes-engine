"""Recognition, wording, and re-recognition for a Griptape Cloud budget refusal.

When a HARD budget has no room, Griptape Cloud refuses the invocation with HTTP
403 and a body naming every budget that refused and the figures it refused on.
Without this module that body reaches the artist as ``budget_exceeded`` at best
-- the machine-readable code, mistaken for a message -- and the run either stops
with a generic error or, worse, carries on as though the call had succeeded.

The four jobs live together because they are one contract seen from four sides:
:func:`refusal_from_body` reads Cloud's wire format, :class:`BudgetRefusal`
models it, :func:`describe` words it for the artist, and :func:`is_budget_halt`
recognizes the verdict again on the far side of a worker boundary. Splitting
them lets the wording drift from the shape that produced it.

**The engine words the message; Cloud's prose goes to the log.** Cloud sends a
``message`` too, but it is one line serving every refusing surface, so it cannot
name the node, distinguish a frozen budget from an exhausted one, or say that a
call already in flight will still be billed. It is kept on the model so an
engine/Cloud disagreement is diagnosable from a log rather than a screenshot.

**Credits, never dollars.** The credits-per-dollar divisor is unsettled between
Cloud surfaces, so converting risks printing a figure that disagrees with the
dashboard the artist is about to go check. Printing Cloud's integer cannot.
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
"""The machine-readable code Cloud sets on every budget refusal.

Present on both envelopes: at the top level of the flat body, and as
``error.code`` in the OpenAI-compatible one -- which an OpenAI SDK unwraps to
a bare ``code``. Cloud builds a refusal in exactly
one place, so this token is the whole recognition test -- ``blocked_by`` only
confirms there is something to name.
"""

BUDGET_HALT_PREFIX = "Budget stopped this run."
"""Opening words of every halt message, and the last-resort way to recognize one.

A worker flattens an exception to type, message, and traceback, and one library
call site loses the exception object entirely, so in those cases the prefix is
all that survives. Rewording it silently turns a budget halt back into a generic
error; a round-trip test pins it.
"""

_RESET_PHRASES = {
    "DAILY": "tomorrow",
    "WEEKLY": "at the start of next week",
    "MONTHLY": "at the start of next month",
    "YEARLY": "at the start of next year",
}
"""How to say each reset period to someone who did not configure the budget.

Cloud sends enum tokens (``MONTHLY``), which would reach the artist as "wait for
the MONTHLY budget to reset". A period absent from this map -- including one
Cloud adds later -- degrades to vaguer prose rather than leaking its token.

``LIFETIME`` is deliberately absent: it never resets, and telling someone to wait
for a reset that will not come sends them to watch an unchanging dashboard. It
selects a different closing sentence instead.
"""

_LIFETIME_PERIOD = "LIFETIME"

_MISSING = object()
"""Sentinel for "this exception has no body at all", which None does not say.

A carrier can legitimately report a body of None -- a 403 with an empty
response -- and that is still an HTTP failure worth reading a status off.
"""


@dataclass(frozen=True)
class BlockedBudget:
    """One budget that refused the call, and the figures it refused on.

    Only ``budget_name`` is required. Cloud sends every other field today, but a
    refusal that reaches an older engine should still name the budget rather than
    fail to parse, and the artist can act on a name alone.

    ``spent_by_cost_basis``, ``includes_byok``, and ``includes_reported`` are
    deliberately not modelled. They answer "which spend counted toward this
    limit", which belongs to whoever can retune the budget -- reachable through
    the receipt this refusal's ``spend_id`` points at -- not to the artist whose
    run just stopped.
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

    Carries the parsed refusal for a same-process caller. The attribute does not
    survive a worker boundary -- see :func:`is_budget_halt` -- so the message is
    built before raising rather than derived by whoever catches it.

    ``node_name`` records whether the message already names the node whose call
    was refused. A Griptape Cloud driver recognizes a refusal deep inside a
    request it made on some node's behalf and has no idea which node that is, so
    it raises without a name; the node executor knows, and re-words rather than
    letting the artist read a halt that does not say where to look.
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
        cloud_host: Hostname of the Griptape Cloud deployment in use, from
            ``resolve_cloud_host``. An HTTP error from any other host is not ours
            to interpret. Pass the function itself rather than its result where
            this is asked about failures indiscriminately: resolving the host
            reads a secret, and most failures are answered without ever needing
            one. It is called at most once per exception, and not at all for a
            failure carrying no response.

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

    Accepts both envelopes Cloud sends, and the one an OpenAI SDK leaves behind.
    Six surfaces return the refusal as the whole body; the OpenAI-compatible
    surface nests the same values under ``error`` so an OpenAI SDK can parse it.
    That SDK then unwraps ``error`` before raising, so the body on its exception
    -- and on Pydantic AI's, which passes it along -- is the inner object, with
    the code under ``code``. The values are lifted rather than rebuilt at every
    step, so this returns the same refusal whichever shape arrives.

    Args:
        body: The parsed response body, or anything at all -- a body that is not
            a budget refusal is answered with None rather than an exception.

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
        # The code says a budget refused, but nothing survived that names one. A
        # message reading "no budgets" is worse than the generic error, so leave
        # it to the generic path.
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
        A message naming every budget that refused, what it refused on, and what
        the artist can do about it.
    """
    if node_name:
        opening = f"{BUDGET_HALT_PREFIX} Griptape Cloud refused the next call from '{node_name}'"
    else:
        opening = f"{BUDGET_HALT_PREFIX} Griptape Cloud refused the next call"

    timing = (
        "Budgets stop the next call, not the one already running, so any call "
        "already in flight will finish and be billed."
    )

    if len(refusal.budgets) == 1:
        budget = refusal.budgets[0]
        return f"{opening} because {_reason(budget)}. {timing} {_remedy(budget)}"

    # Listed on their own lines: the editor renders these as markdown, and three
    # budgets run together in a paragraph read as one rambling sentence.
    listed = "\n".join(f"- {_sentence(_reason(budget))}" for budget in refusal.budgets)
    closing = "Every budget above must have room before this call can go through"
    if refusal.effective_remaining_credits is not None:
        closing = f"{closing}; the tightest has {_credits(refusal.effective_remaining_credits)} left"
    return f"{opening} because {len(refusal.budgets)} budgets refused it:\n{listed}\n{timing} {closing}."


def log_line(refusal: BudgetRefusal) -> str:
    """Summarize a refusal for the engine log, including what the artist is not shown.

    The halt message answers "what do I do now"; this answers "what exactly
    happened", which is the question an administrator asks later. ``spend_id`` is
    the durable handle: Cloud writes a BLOCKED receipt row for every refusal, and
    that row outlives the message once it has scrolled away.
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

    A halt is worded once, at the node that was refused, and then re-raised and
    re-wrapped on its way out: the node executor raises
    ``RuntimeError("Node 'X' execution failed: ...") from exc``, and a worker
    flattens the original to a ``ForwardedException`` first. By the time the
    scheduler reaps it, the sentence an artist can act on is two layers of
    framing deep and no longer at the front of the string.

    So the search is down the ``__cause__`` chain rather than at the top, and
    what it returns is the halt as it was written rather than the wrapper's
    retelling of it.

    Three tests, because the verdict arrives in three conditions. In the same
    process the original exception is intact. Forwarded from a worker it is a
    ``ForwardedException`` naming the original type, since crossing that
    boundary keeps type, message, and traceback and nothing else. And one
    library call site discards the exception entirely, leaving the message as
    the only evidence.

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

    Three unrelated shapes reach this code, one per HTTP client that spends
    credits, and they agree on nothing -- not the attribute holding the status,
    not whether the body arrives parsed, not whether the URL survives at all.

    ``httpx.HTTPStatusError`` keeps the status on ``response`` and knows the URL
    it called, so it can be host-scoped: a workflow also talks to remote MCP
    servers and third-party APIs that raise the same error, and attributing
    their 403 to a Griptape budget would send the artist hunting for a budget
    that is not the problem.

    ``requests.exceptions.HTTPError`` is what the Griptape SDK's Cloud drivers
    raise -- the prompt driver behind every agent node, and the image-generation
    driver. It also carries a response and a URL, but under a different shape:
    the status is on the response rather than the exception, and the body is a
    method rather than an attribute. Duck-typed rather than imported because
    ``requests`` is not an engine dependency; it arrives through the SDK.

    The third carries ``status_code`` and ``body`` directly and knows no URL --
    Pydantic AI's ``ModelHTTPError``, from the sidebar's chat model. It is
    duck-typed too, so that ``pydantic_ai`` stays out of the import graph of a
    module that node libraries bind to. Unable to check the host, it relies on
    the ``budget_exceeded`` code, which no third party sends.

    Callers wrap and re-raise -- the image toolset raises ``ModelRetry`` from the
    original -- so follow the cause chain rather than only inspecting the top.
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
    """Read a ``requests``-style response off an exception, host-scoped.

    Structural checks rather than an ``isinstance``, since the type is not
    importable here. Anything failing them is not a response this code can read,
    and is left to the rest of the chain rather than guessed at. The host is
    resolved last, after those checks have established there is a response worth
    scoping.
    """
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
    """Call a response's JSON parser, tolerating a body that is not JSON.

    ``requests`` raises its own decode error, which subclasses ``ValueError``;
    an empty body raises the same way. Either means there is nothing here to
    read a refusal out of, which is an answer rather than a failure.
    """
    try:
        return parse()
    except ValueError:
        return None


def _body_of(response: httpx.Response) -> object | None:
    """Parse a response body, tolerating one that is not JSON or was never read.

    A streamed response raises ``ResponseNotRead`` when ``raise_for_status`` runs
    inside the ``stream`` block, before anything read the body. Its status is
    still an answer; its body is simply not there to read a refusal out of.
    """
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


def _reason(budget: BlockedBudget) -> str:
    """Say what this one budget did, in a clause that follows "because"."""
    if budget.frozen:
        # Frozen refuses at any headroom, so "no room left" would contradict the
        # credits the artist can see on the dashboard.
        if budget.remaining_credits is not None:
            return (
                f'the budget "{budget.budget_name}" is frozen: it refuses every call '
                f"regardless of the {_credits(budget.remaining_credits)} it still has"
            )
        return f'the budget "{budget.budget_name}" is frozen: it refuses every call'

    if budget.limit_credits == 0:
        return f'the budget "{budget.budget_name}" is set to block every call under it'

    return f'the budget "{budget.budget_name}" has no room left{_figures(budget)}'


def _figures(budget: BlockedBudget) -> str:
    """Quote the numbers the refusal turned on, when Cloud sent them."""
    parts = []
    if budget.remaining_credits is not None:
        parts.append(f"{_credits(budget.remaining_credits)} remaining")
    if budget.requested_credits is not None:
        parts.append(f"{budget.requested_credits:,} requested")
    if not parts:
        return ""
    return ": " + ", ".join(parts)


def _remedy(budget: BlockedBudget) -> str:
    """Say what the artist can do about this budget."""
    if budget.frozen:
        return "Ask your Griptape administrator to unfreeze it, then run again."
    # A zero limit blocks every call under it by design, so waiting for it to
    # reset only produces the same refusal on a later day.
    if budget.limit_credits == 0:
        return "Raise the limit, or ask your Griptape administrator to, then run again."
    if budget.reset_period == _LIFETIME_PERIOD:
        return "Raise the limit and run again - this budget does not reset on its own."
    reset_phrase = _RESET_PHRASES.get(budget.reset_period or "")
    if reset_phrase is None:
        return "Raise the limit, or wait for the budget to reset, then run again."
    return f"Raise the limit, or wait for the budget to reset {reset_phrase}, then run again."


def _credits(amount: int) -> str:
    """Write a credit figure, keeping "1 credit" from reading as "1 credits"."""
    if amount == 1:
        return "1 credit"
    return f"{amount:,} credits"


def _sentence(clause: str) -> str:
    """Turn a "because" clause into a standalone sentence.

    Uppercases only the first character rather than using ``str.capitalize``,
    which lowercases the rest and would rewrite a budget named "Star Wars X" as
    "star wars x" -- the artist would search the dashboard for a name that is
    not there.
    """
    return clause[:1].upper() + clause[1:] + "."


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
