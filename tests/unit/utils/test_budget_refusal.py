"""Tests for recognizing, wording, and re-recognizing a Griptape Cloud budget refusal.

Cloud sends everything needed to explain a block; the failure mode this module exists to
prevent is the engine reading the wrong key and showing an artist the slug `budget_exceeded`.
So most of these assert on the *message an artist reads* rather than on the parse, and the
fixtures are bodies recorded from Cloud's own tests rather than shapes invented here.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import httpx2
import openai
import pytest

from griptape_nodes.retained_mode.events.event_converter import converter
from griptape_nodes.utils.budget_refusal import (
    BUDGET_EXCEEDED_CODE,
    BUDGET_HALT_PREFIX,
    BudgetExceededError,
    BudgetRefusal,
    describe,
    halt_message,
    is_budget_halt,
    log_line,
    refusal_from_body,
    refusal_from_exception,
)

CLOUD_HOST = "cloud.griptape.ai"

REJECTION_KEYS = {
    "budget_id",
    "budget_name",
    "scope_type",
    "reset_period",
    "enforcement",
    "limit_credits",
    "spent_credits",
    "spent_by_cost_basis",
    "includes_byok",
    "includes_reported",
    "remaining_credits",
    "requested_credits",
    "frozen",
}
"""Every key Cloud's `Rejection.as_body()` sends today.

Pinned so a Cloud field this parser silently ignores is a failure here rather than a number
missing from an artist's halt message. Three of these are deliberately not modelled -- see
`budget_refusal.BlockedBudget` -- and `test_the_unmodelled_keys_are_a_choice` names them, so
"we looked and chose not to" stays distinguishable from "we never noticed".
"""

REFUSAL_KEYS = {"error", "message", "blocked_by", "effective_remaining_credits", "spend_id"}
"""The envelope Cloud's `SpendHold.as_refusal()` builds, pinned for the same reason."""

UNMODELLED_KEYS = {"spent_by_cost_basis", "includes_byok", "includes_reported"}
"""Rejection keys this module reads past on purpose: they answer "which spend counted"."""

REMAINING_CREDITS = 10
"""What ``a_refusal_body()`` leaves in the tightest budget."""

RESET_PERIODS = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY", "LIFETIME")
"""Cloud's `BudgetResetPeriod` members. A token absent here is one this engine has not seen."""


def a_rejection(**overrides: Any) -> dict[str, Any]:
    """One `blocked_by` entry, shaped exactly as Cloud's `Rejection.as_body()` builds it."""
    entry = {
        "budget_id": "3f1c6b4e-0000-4000-8000-000000000001",
        "budget_name": "tight",
        "scope_type": "ORG",
        "reset_period": "MONTHLY",
        "enforcement": "HARD",
        "limit_credits": 100,
        "spent_credits": 90,
        "spent_by_cost_basis": {"billed": 90, "estimated": 0, "declared": 0},
        "includes_byok": False,
        "includes_reported": False,
        "remaining_credits": 10,
        "requested_credits": 50,
        "frozen": False,
    }
    entry.update(overrides)
    return entry


def a_refusal_body(*rejections: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """The flat 403 body, as `SpendHold.as_refusal()` builds it.

    Six Cloud surfaces return this dict as their whole body. Recorded from
    `RefusalBodyTests.test_the_code_is_machine_readable_and_the_message_is_not`.
    """
    entries = list(rejections) or [a_rejection()]
    body = {
        "error": BUDGET_EXCEEDED_CODE,
        "message": "Budget limit reached (tight).",
        "blocked_by": entries,
        "effective_remaining_credits": min(entry["remaining_credits"] for entry in entries),
        "spend_id": "9a2d5e70-0000-4000-8000-00000000000f",
    }
    body.update(overrides)
    return body


def an_openai_refusal_body(refusal: dict[str, Any]) -> dict[str, Any]:
    """The same refusal in the envelope an OpenAI SDK parses.

    Mirrors Cloud's `openai_compat.errors.budget_exceeded`, which lifts these keys from the
    flat body rather than deriving its own.
    """
    return {
        "error": {
            "message": refusal["message"],
            "type": "insufficient_quota",
            "param": None,
            "code": refusal["error"],
            "blocked_by": refusal["blocked_by"],
            "effective_remaining_credits": refusal["effective_remaining_credits"],
            "spend_id": refusal["spend_id"],
        }
    }


def a_cloud_error(
    body: object,
    *,
    status: int = 403,
    host: str = CLOUD_HOST,
) -> httpx.HTTPStatusError:
    """The exception a Cloud 403 actually raises, host and all."""
    request = httpx.Request("POST", f"https://{host}/api/images/generations")
    if isinstance(body, (dict, list)):
        response = httpx.Response(status, json=body, request=request)
    else:
        response = httpx.Response(status, content=str(body).encode(), request=request)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    msg = f"httpx did not raise for status {status}"
    raise AssertionError(msg)


class _ModelHttpError(Exception):
    """Stands in for Pydantic AI's `ModelHTTPError`, which is duck-typed rather than imported."""

    def __init__(self, status_code: int, body: object) -> None:
        super().__init__(f"status_code: {status_code}")
        self.status_code = status_code
        self.body = body


class _RequestsResponse:
    """Stands in for a `requests.Response`, which is duck-typed for the same reason.

    ``requests`` is not an engine dependency -- it arrives through the griptape SDK, which the
    library's Cloud drivers use -- so the recognition matches on shape and the test supplies
    the shape. ``json`` is a method here and an attribute on httpx's response, which is the
    difference the structural check exists to absorb.
    """

    def __init__(self, status_code: int, body: object, *, host: str = CLOUD_HOST) -> None:
        self.status_code = status_code
        self.url = f"https://{host}/api/chat/messages"
        self._body = body

    def json(self) -> object:
        if isinstance(self._body, (dict, list)):
            return self._body
        msg = "Expecting value"
        raise ValueError(msg)


class _RequestsHttpError(Exception):
    """Stands in for `requests.exceptions.HTTPError`: status and body live on `.response`."""

    def __init__(self, response: _RequestsResponse) -> None:
        super().__init__(f"{response.status_code} Client Error for url: {response.url}")
        self.response = response


class TestTheWireFormatWeParse:
    """Tripwires against Cloud changing the shape out from under this parser."""

    def test_a_rejection_carries_exactly_the_keys_cloud_sends(self) -> None:
        assert set(a_rejection()) == REJECTION_KEYS

    def test_a_refusal_carries_exactly_the_envelope_cloud_builds(self) -> None:
        assert set(a_refusal_body()) == REFUSAL_KEYS

    def test_the_unmodelled_keys_are_a_choice(self) -> None:
        """Three keys are read past on purpose; the rest must reach `BlockedBudget`.

        They answer "which spend counted toward this limit", which belongs to whoever can
        retune the budget -- reachable through `spend_id` -- not to the artist whose run
        just stopped. If a fourth key ever joins them, this fails and someone decides.
        """
        refusal = refusal_from_body(a_refusal_body())

        assert refusal is not None
        modelled = set(vars(refusal.budgets[0]))
        assert REJECTION_KEYS - modelled == UNMODELLED_KEYS


class TestRecognizingARefusal:
    """What counts as a budget refusal, and what only looks like one."""

    def test_the_flat_envelope_parses(self) -> None:
        refusal = refusal_from_exception(a_cloud_error(a_refusal_body()), cloud_host=CLOUD_HOST)

        assert refusal is not None
        assert [budget.budget_name for budget in refusal.budgets] == ["tight"]
        assert refusal.effective_remaining_credits == REMAINING_CREDITS
        assert refusal.spend_id == "9a2d5e70-0000-4000-8000-00000000000f"

    def test_both_envelopes_name_the_same_budgets(self) -> None:
        """The engine's counterpart to Cloud's `test_the_openai_envelope_says_the_same_thing`.

        Two envelopes because two protocols. A chat-refused run and a proxy-refused run must
        still read the same sentence.
        """
        flat = a_refusal_body()

        from_flat = refusal_from_exception(a_cloud_error(flat), cloud_host=CLOUD_HOST)
        from_openai = refusal_from_exception(a_cloud_error(an_openai_refusal_body(flat)), cloud_host=CLOUD_HOST)

        assert from_flat == from_openai

    def test_an_entitlement_403_is_not_a_budget_refusal(self) -> None:
        """A license that authenticates but is not entitled also answers 403."""
        body = {"error": {"code": "permission_denied", "message": "Not entitled.", "type": "permission_error"}}

        assert refusal_from_exception(a_cloud_error(body), cloud_host=CLOUD_HOST) is None

    def test_a_streamed_error_whose_body_was_never_read_is_not_a_refusal(self) -> None:
        """`raise_for_status` inside a `stream` block fires before anything reads the body.

        Every node failure is asked this question from inside an `except`, so raising here
        would replace the node's real error with one about unread response content.
        """
        body = json.dumps(a_refusal_body()).encode()
        transport = httpx.MockTransport(lambda _request: httpx.Response(403, stream=httpx.ByteStream(body)))
        with (
            httpx.Client(transport=transport) as client,
            client.stream("POST", f"https://{CLOUD_HOST}/api/images/generations") as response,
            pytest.raises(httpx.HTTPStatusError) as caught,
        ):
            response.raise_for_status()

        assert refusal_from_exception(caught.value, cloud_host=CLOUD_HOST) is None

    def test_a_403_from_another_host_is_not_ours_to_explain(self) -> None:
        """A workflow also calls MCP servers and third-party APIs that raise the same error."""
        error = a_cloud_error(a_refusal_body(), host="api.example.com")

        assert refusal_from_exception(error, cloud_host=CLOUD_HOST) is None

    def test_the_host_is_only_resolved_when_a_response_could_make_it_matter(self) -> None:
        """Resolving the host reads a secret, and most failures never need one.

        The engine asks this about every node failure, the overwhelming majority of which
        carry no HTTP response at all. Resolving eagerly turned one secret read per refusal
        into one per failed node.
        """
        reads = []

        def resolve() -> str:
            reads.append(None)
            return CLOUD_HOST

        assert refusal_from_exception(RuntimeError("process exploded"), cloud_host=resolve) is None
        assert reads == [], "The host was resolved for a failure that carries no response at all."

        assert refusal_from_exception(a_cloud_error(a_refusal_body()), cloud_host=resolve) is not None
        assert len(reads) == 1, f"The host was resolved {len(reads)} times for one exception."

    def test_a_non_403_is_not_a_budget_refusal(self) -> None:
        assert refusal_from_exception(a_cloud_error(a_refusal_body(), status=500), cloud_host=CLOUD_HOST) is None

    def test_a_refusal_naming_no_budget_falls_back_to_the_generic_error(self) -> None:
        """A message reading "no budgets stopped you" is worse than the generic one."""
        error = a_cloud_error(a_refusal_body(blocked_by=[]))

        assert refusal_from_exception(error, cloud_host=CLOUD_HOST) is None

    def test_a_body_that_is_not_json_does_not_raise(self) -> None:
        assert refusal_from_exception(a_cloud_error("<html>403 Forbidden</html>"), cloud_host=CLOUD_HOST) is None

    def test_an_unknown_extra_field_does_not_break_the_parse(self) -> None:
        """Cloud adds fields additively; an older engine must still name the budget."""
        body = a_refusal_body(a_rejection(some_future_field="whatever"), another_new_key=1)

        refusal = refusal_from_body(body)

        assert refusal is not None
        assert refusal.budgets[0].budget_name == "tight"

    def test_a_missing_field_still_names_the_budget(self) -> None:
        """The artist can act on a name alone, so a sparse entry beats no entry."""
        refusal = refusal_from_body({"error": BUDGET_EXCEEDED_CODE, "blocked_by": [{"budget_name": "tight"}]})

        assert refusal is not None
        assert refusal.budgets[0].budget_name == "tight"
        assert refusal.budgets[0].remaining_credits is None


class TestTheExceptionChain:
    """Callers wrap and re-raise, so the refusal is rarely on the exception handed to us."""

    def test_a_wrapped_refusal_is_still_found(self) -> None:
        cause = a_cloud_error(a_refusal_body())
        try:
            msg = "Node failed"
            raise RuntimeError(msg) from cause  # noqa: TRY301
        except RuntimeError as exc:
            wrapped = exc

        assert refusal_from_exception(wrapped, cloud_host=CLOUD_HOST) is not None

    def test_a_model_http_error_carries_its_own_body(self) -> None:
        """Pydantic AI's shape: status and body directly on the exception, no URL.

        The body is the OpenAI SDK's, which has already had its ``error`` wrapper taken off.
        """
        error = _ModelHttpError(403, an_openai_refusal_body(a_refusal_body())["error"])

        refusal = refusal_from_exception(error, cloud_host=CLOUD_HOST)

        assert refusal is not None
        assert refusal.budgets[0].budget_name == "tight"

    def test_the_openai_sdk_error_from_a_chat_refusal_is_recognized(self) -> None:
        """The chat endpoint refuses in the OpenAI envelope, and the SDK unwraps it before raising.

        Driven through the real client so a change in how the SDK hands the body over fails here.
        The SDK ships its own fork of httpx, so its transport comes from there.
        """
        body = an_openai_refusal_body(a_refusal_body())
        transport = httpx2.MockTransport(lambda _request: httpx2.Response(403, json=body))
        client = openai.OpenAI(
            api_key="unused",
            base_url=f"https://{CLOUD_HOST}/api/v1",
            max_retries=0,
            http_client=httpx2.Client(transport=transport),
        )

        with pytest.raises(openai.PermissionDeniedError) as raised:
            client.chat.completions.create(model="gpt-4.1", messages=[{"role": "user", "content": "hi"}])

        refusal = refusal_from_exception(raised.value, cloud_host=CLOUD_HOST)
        assert refusal == refusal_from_body(a_refusal_body())

    def test_a_model_http_error_body_may_arrive_as_text(self) -> None:
        error = _ModelHttpError(403, json.dumps(a_refusal_body()))

        assert refusal_from_exception(error, cloud_host=CLOUD_HOST) is not None

    def test_a_model_http_error_with_an_empty_body_is_not_a_refusal(self) -> None:
        """A body of None is still an HTTP failure; it just does not describe a budget."""
        assert refusal_from_exception(_ModelHttpError(403, None), cloud_host=CLOUD_HOST) is None

    def test_a_requests_style_error_is_recognized(self) -> None:
        """The SDK's Cloud drivers raise this shape, and it agrees with the others on nothing.

        Status and body hang off `.response` rather than the exception, the URL is a plain
        string rather than a parsed object, and `json` is a method rather than an attribute.
        """
        exc = _RequestsHttpError(_RequestsResponse(403, a_refusal_body()))

        refusal = refusal_from_exception(exc, cloud_host=CLOUD_HOST)

        assert refusal is not None
        assert [budget.budget_name for budget in refusal.budgets] == ["tight"]

    def test_a_requests_style_403_from_another_host_is_not_ours_to_explain(self) -> None:
        exc = _RequestsHttpError(_RequestsResponse(403, a_refusal_body(), host="api.example-vendor.com"))

        assert refusal_from_exception(exc, cloud_host=CLOUD_HOST) is None

    def test_a_requests_style_response_with_no_readable_body_is_not_a_refusal(self) -> None:
        """A streamed refusal whose connection closed reads as empty rather than raising out."""
        exc = _RequestsHttpError(_RequestsResponse(403, "not json"))

        assert refusal_from_exception(exc, cloud_host=CLOUD_HOST) is None

    def test_an_ordinary_exception_is_not_a_refusal(self) -> None:
        assert refusal_from_exception(ValueError("something else"), cloud_host=CLOUD_HOST) is None

    def test_a_cyclic_cause_chain_terminates(self) -> None:
        first = ValueError("first")
        second = ValueError("second")
        first.__cause__ = second
        second.__cause__ = first

        assert refusal_from_exception(first, cloud_host=CLOUD_HOST) is None


class TestTheMessage:
    """What the artist reads. The reason this module exists."""

    def test_it_names_the_node_and_the_budget(self) -> None:
        refusal = refusal_from_body(a_refusal_body())
        assert refusal is not None

        message = describe(refusal, node_name="Generate Poster")

        assert message.startswith(BUDGET_HALT_PREFIX)
        assert "Generate Poster" in message
        assert '"tight"' in message

    def test_it_never_shows_the_machine_readable_code(self) -> None:
        """The bug this module was written for: `budget_exceeded` reaching a human."""
        refusal = refusal_from_body(a_refusal_body())
        assert refusal is not None

        assert BUDGET_EXCEEDED_CODE not in describe(refusal, node_name="Generate Poster")

    def test_it_is_short_enough_for_the_run_blocked_bar(self) -> None:
        """The editor shows this in a one-line bar; the figures live on the budget page."""
        refusal = refusal_from_body(a_refusal_body())
        assert refusal is not None

        message = describe(refusal, node_name="Generate Poster")

        assert message == (
            f"{BUDGET_HALT_PREFIX} 'Generate Poster' was blocked by the budget \"tight\". "
            "Contact your Griptape administrator."
        )

    def test_it_never_shows_dollars(self) -> None:
        """Two Cloud surfaces have disagreed about credits-per-dollar, so never convert."""
        refusal = refusal_from_body(a_refusal_body())
        assert refusal is not None

        assert "$" not in describe(refusal)

    def test_it_names_every_budget_that_refused(self) -> None:
        """Raising one limit must not reveal the next by surprise."""
        body = a_refusal_body(
            a_rejection(budget_name="tight", remaining_credits=10),
            a_rejection(budget_name="daily cap", budget_id="b-2", reset_period="DAILY", remaining_credits=4),
            a_rejection(budget_name="Star Wars X", budget_id="b-3", remaining_credits=7),
        )
        refusal = refusal_from_body(body)
        assert refusal is not None

        message = describe(refusal, node_name="Generate Poster")

        assert message == (
            f"{BUDGET_HALT_PREFIX} 'Generate Poster' was blocked by the budgets "
            '"tight", "daily cap" and "Star Wars X". Contact your Griptape administrator.'
        )

    def test_two_budgets_join_without_a_comma(self) -> None:
        body = a_refusal_body(
            a_rejection(budget_name="tight"),
            a_rejection(budget_name="daily cap", budget_id="b-2"),
        )
        refusal = refusal_from_body(body)
        assert refusal is not None

        assert 'the budgets "tight" and "daily cap".' in describe(refusal)

    def test_an_unnamed_node_still_reads_as_a_sentence(self) -> None:
        """A driver raises before the engine knows which node it was serving."""
        refusal = refusal_from_body(a_refusal_body())
        assert refusal is not None

        assert describe(refusal).startswith(f"{BUDGET_HALT_PREFIX} The next call was blocked by the budget")

    def test_a_frozen_budget_is_marked(self) -> None:
        """Frozen refuses at any headroom, so the page's credits left would otherwise puzzle."""
        refusal = refusal_from_body(a_refusal_body(a_rejection(frozen=True, remaining_credits=10_000)))
        assert refusal is not None

        assert '"tight" (frozen)' in describe(refusal)

    @pytest.mark.parametrize(
        "rejection",
        [
            a_rejection(),
            a_rejection(frozen=True, remaining_credits=10_000),
            a_rejection(limit_credits=0, spent_credits=0, remaining_credits=0),
            a_rejection(reset_period="LIFETIME"),
        ],
        ids=["exhausted", "frozen", "zero-limit", "lifetime"],
    )
    def test_every_halt_sends_the_artist_to_their_administrator(self, rejection: dict[str, Any]) -> None:
        """Budgets are changed on Griptape Cloud, so the next step is always the same person."""
        refusal = refusal_from_body(a_refusal_body(rejection))
        assert refusal is not None

        message = describe(refusal)

        assert message.endswith("Contact your Griptape administrator.")
        assert "Raise" not in message
        assert "wait for" not in message

    @pytest.mark.parametrize("period", [*RESET_PERIODS, "FORTNIGHTLY"])
    def test_a_reset_period_token_never_reaches_the_artist(self, period: str) -> None:
        """Cloud sends enum tokens. A period Cloud adds later must degrade, not leak."""
        refusal = refusal_from_body(a_refusal_body(a_rejection(reset_period=period)))
        assert refusal is not None

        message = describe(refusal)

        assert period not in message
        assert "ORG" not in message

    def test_the_engine_words_it_not_cloud(self) -> None:
        """Cloud's one line serves seven surfaces, so it is kept for the log and not shown."""
        body = a_refusal_body(message="SOMETHING CLOUD SAYS THAT IS WRONG HERE")
        refusal = refusal_from_body(body)
        assert refusal is not None

        assert refusal.cloud_message == "SOMETHING CLOUD SAYS THAT IS WRONG HERE"
        assert "SOMETHING CLOUD SAYS" not in describe(refusal)

    def test_the_spend_id_is_logged_not_shown(self) -> None:
        """The receipt stays recoverable without putting a uuid in an artist's face."""
        refusal = refusal_from_body(a_refusal_body())
        assert refusal is not None

        assert refusal.spend_id is not None
        assert refusal.spend_id not in describe(refusal)
        assert refusal.spend_id in log_line(refusal)

    def test_the_log_line_keeps_what_the_message_drops(self) -> None:
        """An administrator asks what exactly happened, long after the artist has moved on."""
        refusal = refusal_from_body(a_refusal_body())
        assert refusal is not None

        line = log_line(refusal)

        assert "tight" in line
        assert "scope=ORG" in line
        assert "enforcement=HARD" in line
        assert "SOMETHING" not in line


class TestTheVerdictSurvivesTheWorkerBoundary:
    """A node runs in a worker; the halt decision is made on the orchestrator."""

    def test_a_forwarded_budget_error_is_still_a_budget_halt(self) -> None:
        """Crossing the boundary keeps type, message and traceback -- and nothing else.

        This is the test that fails the day someone renames `BudgetExceededError` or moves
        the module: `isinstance` cannot work here, so recognition rides on the type name.
        """
        refusal = refusal_from_body(a_refusal_body())
        assert refusal is not None
        original = BudgetExceededError(describe(refusal, node_name="Generate Poster"), refusal)

        forwarded = converter.structure(converter.unstructure(original), Exception)

        assert not isinstance(forwarded, BudgetExceededError)
        assert is_budget_halt(forwarded)

    def test_a_forwarded_ordinary_error_is_not_a_budget_halt(self) -> None:
        forwarded = converter.structure(converter.unstructure(ValueError("something else")), Exception)

        assert not is_budget_halt(forwarded)

    def test_the_message_alone_is_enough(self) -> None:
        """One library call site loses the exception entirely; the prefix is all that is left."""
        assert is_budget_halt(message=f"{BUDGET_HALT_PREFIX} Griptape Cloud refused the next call")

    def test_an_ordinary_message_alone_is_not_enough(self) -> None:
        assert not is_budget_halt(message="Node 'Generate Poster' encountered a problem: boom")

    def test_nothing_at_all_is_not_a_budget_halt(self) -> None:
        assert not is_budget_halt()

    def test_the_error_carries_the_refusal_in_process(self) -> None:
        """A same-process caller can read the figures back off the exception."""
        refusal = BudgetRefusal(cloud_message="x")

        error = BudgetExceededError("Budget stopped this run.", refusal)

        assert error.refusal is refusal
        assert is_budget_halt(error)


class TestFindingTheHaltUnderItsWrappers:
    """A halt is worded once and then re-raised; by the time it is read it is buried.

    The node executor raises ``RuntimeError("Node 'X' execution failed: ...") from exc``, so the
    exception the scheduler reaps is not the halt and its message is not the halt's wording. These
    tests fix the two things that depend on that: that the halt is still recognized, and that the
    sentence handed onward is the one written for the artist rather than the wrapper's retelling.
    """

    def test_a_wrapped_halt_is_still_recognized(self) -> None:
        refusal = refusal_from_body(a_refusal_body())
        assert refusal is not None
        halt = BudgetExceededError(describe(refusal, node_name="Generate Poster"), refusal)

        wrapped = RuntimeError("Node 'Generate Poster' execution failed: something")
        wrapped.__cause__ = halt

        assert is_budget_halt(wrapped, str(wrapped))

    def test_the_wording_recovered_is_the_halt_and_not_the_wrapper(self) -> None:
        """What comes back has to start with the prefix: downstream recognizes it by that."""
        refusal = refusal_from_body(a_refusal_body())
        assert refusal is not None
        halt = BudgetExceededError(describe(refusal, node_name="Generate Poster"), refusal)

        wrapped = RuntimeError("Node 'Generate Poster' execution failed: nested nonsense")
        wrapped.__cause__ = halt

        recovered = halt_message(wrapped, str(wrapped))

        assert recovered is not None
        assert recovered.startswith(BUDGET_HALT_PREFIX)
        assert "execution failed" not in recovered

    def test_a_halt_forwarded_from_a_worker_and_then_wrapped_is_still_found(self) -> None:
        """Both framings at once: the worker flattens the type, then the executor wraps it."""
        refusal = refusal_from_body(a_refusal_body())
        assert refusal is not None
        original = BudgetExceededError(describe(refusal, node_name="Generate Poster"), refusal)
        forwarded = converter.structure(converter.unstructure(original), Exception)

        wrapped = RuntimeError("Node 'Generate Poster' execution failed: something")
        wrapped.__cause__ = forwarded

        recovered = halt_message(wrapped, str(wrapped))

        assert recovered is not None
        assert recovered.startswith(BUDGET_HALT_PREFIX)

    def test_a_wrapped_ordinary_failure_is_left_alone(self) -> None:
        wrapped = RuntimeError("Node 'Generate Poster' execution failed: the file was missing")
        wrapped.__cause__ = FileNotFoundError("no such file")

        assert halt_message(wrapped, str(wrapped)) is None

    def test_a_cycle_in_the_cause_chain_does_not_hang(self) -> None:
        """``__cause__`` is writable, so a cycle is reachable and must terminate the walk."""
        first = RuntimeError("first")
        second = RuntimeError("second")
        first.__cause__ = second
        second.__cause__ = first

        assert halt_message(first, str(first)) is None
