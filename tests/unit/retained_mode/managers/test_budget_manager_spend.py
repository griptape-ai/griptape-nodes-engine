"""Tests for BudgetManager's direct-provider handlers: the budget check and the usage report.

HTTP is stubbed at `httpx2.request`, below `request_with_retry`, so the real retry loop runs and
the tests can see every attempt it makes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import TYPE_CHECKING, Any, Self
from unittest.mock import MagicMock, patch

import httpx2
import pytest
from tenacity import wait_none

from griptape_nodes.retained_mode.events.base_events import ResultDetails
from griptape_nodes.retained_mode.events.budget_events import (
    BudgetAccessRequest,
    BudgetAccessResultFailure,
    BudgetAccessResultSuccess,
    ReportUsageRequest,
    ReportUsageResultFailure,
    ReportUsageResultSuccess,
)
from griptape_nodes.retained_mode.managers import budget_manager as budget_manager_module
from griptape_nodes.retained_mode.managers.budget_manager import BudgetManager
from griptape_nodes.retained_mode.managers.project_manager import ProjectChainEntry
from griptape_nodes.utils.budget_refusal import BUDGET_HALT_PREFIX, BudgetExceededError
from griptape_nodes.utils.http_utils import request_with_retry

if TYPE_CHECKING:
    from collections.abc import Iterator

BASE_URL = "https://cloud.example.test"


class _InlineQueue:
    """Posts a queued report on the spot, so a test sees the report's HTTP calls before it asserts."""

    def __init__(self, manager: BudgetManager) -> None:
        self._manager = manager

    def put(self, body: dict[str, Any]) -> None:
        self._manager._send_report(body)


def _mock_engine(*, credential: str | None = "gt-key") -> MagicMock:
    engine = MagicMock()
    engine.project_manager.get_project_chain.return_value = [
        ProjectChainEntry(id="shot-6", name="Shot 6"),
        ProjectChainEntry(id="star-wars-x", name="Star Wars X"),
    ]
    engine.context_manager.has_current_workflow.return_value = True
    engine.context_manager.get_current_workflow_name.return_value = "poster_flow"
    engine.get_engine_id.return_value = "engine-1"
    engine.get_session_id.return_value = "session-1"
    secrets = {"GT_CLOUD_BASE_URL": BASE_URL, "GT_CLOUD_API_KEY": credential}
    engine.secrets_manager.get_secret.side_effect = lambda name, **_: secrets.get(name)
    return engine


def _manager(engine: MagicMock | None = None) -> BudgetManager:
    manager = BudgetManager(MagicMock(), engine=engine or _mock_engine())
    manager._reports = _InlineQueue(manager)  # type: ignore[assignment]
    return manager


def _check(
    manager: BudgetManager, request: BudgetAccessRequest
) -> BudgetAccessResultSuccess | BudgetAccessResultFailure:
    return asyncio.run(manager.on_budget_access_request(request))


def _response(status: int, body: object = None) -> httpx2.Response:
    request = httpx2.Request("POST", BASE_URL)
    if body is None:
        return httpx2.Response(status, request=request)
    return httpx2.Response(status, json=body, request=request)


class _AsyncClientStub:
    """Stands in for `httpx2.AsyncClient`, sending the check's POST through the same `http` stub."""

    def __init__(self, request: MagicMock, **_kwargs: Any) -> None:
        self._request = request

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def post(self, url: str, **kwargs: Any) -> httpx2.Response:
        return self._request("POST", url, **kwargs)


@pytest.fixture
def http() -> Iterator[MagicMock]:
    """Stub every Cloud POST: the report's retrying `httpx2.request`, and the check's `AsyncClient`.

    Both land on one mock, so a test reads either request's body the same way.
    """

    def no_wait_retry(*args: Any, **kwargs: Any) -> httpx2.Response:
        return request_with_retry(*args, wait=wait_none(), **kwargs)

    with (
        patch("griptape_nodes.utils.http_utils.httpx2.request") as request,
        patch.object(budget_manager_module, "request_with_retry", side_effect=no_wait_retry),
        patch.object(
            budget_manager_module.httpx2, "AsyncClient", side_effect=lambda **kw: _AsyncClientStub(request, **kw)
        ),
    ):
        yield request


def _sent(http: MagicMock, attempt: int = 0) -> dict[str, Any]:
    """The JSON body of one attempt."""
    return http.call_args_list[attempt].kwargs["json"]


def a_rejection(name: str = "tight", **overrides: Any) -> dict[str, Any]:
    """One `blocked_by` entry, shaped as Cloud's `Rejection.as_body()` builds it."""
    entry = {
        "budget_id": f"id-{name}",
        "budget_name": name,
        "scope_type": "ORG",
        "reset_period": "MONTHLY",
        "enforcement": "HARD",
        "limit_credits": 100,
        "spent_credits": 90,
        "spent_by_cost_basis": {"billed": 0, "estimated": 0, "declared": 90},
        "includes_byok": False,
        "includes_reported": True,
        "remaining_credits": 10,
        "requested_credits": 50,
        "frozen": False,
    }
    entry.update(overrides)
    return entry


class TestBudgetCheckAllowed:
    def test_the_answer_cloud_gives_until_reported_spend_can_be_enforced(self, http: MagicMock) -> None:
        http.return_value = _response(
            200,
            {
                "allowed": True,
                "blocked_by": [],
                "effective_remaining_credits": None,
                "checked_budgets": [],
                "correlation_id": None,
                "spend_id": None,
            },
        )

        result = _check(_manager(), BudgetAccessRequest(model="m"))

        assert isinstance(result, BudgetAccessResultSuccess)
        assert result.checked is True
        assert result.effective_remaining_credits is None

    def test_cleared_with_headroom(self, http: MagicMock) -> None:
        http.return_value = _response(200, {"allowed": True, "blocked_by": [], "effective_remaining_credits": 13})

        result = _check(_manager(), BudgetAccessRequest(model="gpt-5-nano"))

        assert isinstance(result, BudgetAccessResultSuccess)
        assert result.checked is True
        assert result.effective_remaining_credits == 13  # noqa: PLR2004

    def test_posts_the_check_with_attribution_in_tags(self, http: MagicMock) -> None:
        http.return_value = _response(200, {"allowed": True})

        result = _check(
            _manager(),
            BudgetAccessRequest(
                model="gpt-5-nano",
                estimated_cost_micro_usd=40000,
                node_type="AnthropicPrompt",
                node_id="node-7",
            ),
        )

        assert isinstance(result, BudgetAccessResultSuccess)
        method, url = http.call_args.args
        assert (method, url) == ("POST", f"{BASE_URL}/api/budget-checks")
        assert http.call_args.kwargs["headers"] == {"Authorization": "Bearer gt-key"}
        assert _sent(http) == {
            "tags": {
                "project": ["shot-6", "star-wars-x"],
                "workflow": "poster_flow",
                "node_type": "AnthropicPrompt",
                "node_id": "node-7",
                "engine_id": "engine-1",
                "session_id": "session-1",
            },
            "model": "gpt-5-nano",
            "estimated_cost_micro_usd": 40000,
            "correlation_id": result.correlation_id,
        }

    def test_an_estimate_of_zero_is_sent(self, http: MagicMock) -> None:
        """Cloud reads 0 as an estimate that fits only zero headroom, and absence as no estimate."""
        http.return_value = _response(200, {"allowed": True})

        _check(_manager(), BudgetAccessRequest(model="m", estimated_cost_micro_usd=0))

        assert _sent(http)["estimated_cost_micro_usd"] == 0

    def test_leaves_out_what_is_unknown(self, http: MagicMock) -> None:
        http.return_value = _response(200, {"allowed": True})
        engine = _mock_engine()
        engine.project_manager.get_project_chain.return_value = []
        engine.context_manager.has_current_workflow.return_value = False
        engine.get_engine_id.return_value = None
        engine.get_session_id.return_value = None

        _check(_manager(engine), BudgetAccessRequest(model="m"))

        assert _sent(http) == {"tags": {}, "model": "m", "correlation_id": _sent(http)["correlation_id"]}

    def test_each_check_mints_its_own_correlation_id(self, http: MagicMock) -> None:
        http.return_value = _response(200, {"allowed": True})
        manager = _manager()

        first = _check(manager, BudgetAccessRequest(model="m"))
        second = _check(manager, BudgetAccessRequest(model="m"))

        assert isinstance(first, BudgetAccessResultSuccess)
        assert isinstance(second, BudgetAccessResultSuccess)
        assert first.correlation_id != second.correlation_id


class TestBudgetCheckDenied:
    def test_names_every_budget_and_halts_the_run(self, http: MagicMock) -> None:
        blocked_by = [a_rejection("Marketing Q3"), a_rejection("Studio", frozen=True)]
        http.return_value = _response(
            200,
            {
                "allowed": False,
                "blocked_by": blocked_by,
                "effective_remaining_credits": 0,
                "checked_budgets": [
                    {
                        "budget_id": "id-Marketing Q3",
                        "budget_name": "Marketing Q3",
                        "scope_type": "ORG",
                        "enforcement": "HARD",
                        "gated": True,
                        "would_block": True,
                    }
                ],
                "correlation_id": "echoed",
                "spend_id": "s-1",
            },
        )

        result = _check(_manager(), BudgetAccessRequest(model="m"))

        assert isinstance(result, BudgetAccessResultFailure)
        assert result.blocked_by == blocked_by
        message = str(result.result_details)
        assert message.startswith(BUDGET_HALT_PREFIX)
        assert '"Marketing Q3"' in message
        assert '"Studio" (frozen)' in message
        assert isinstance(result.exception, BudgetExceededError)
        assert result.exception.refusal.spend_id == "s-1"

    def test_a_deny_without_a_blocked_by_list_still_refuses(self, http: MagicMock) -> None:
        http.return_value = _response(200, {"allowed": False, "blocked_by": "not a list"})

        result = _check(_manager(), BudgetAccessRequest(model="m"))

        assert isinstance(result, BudgetAccessResultFailure)
        assert result.blocked_by == []

    def test_a_deny_naming_no_budget_still_refuses(self, http: MagicMock) -> None:
        http.return_value = _response(200, {"allowed": False, "blocked_by": [{"unexpected": True}]})

        result = _check(_manager(), BudgetAccessRequest(model="m"))

        assert isinstance(result, BudgetAccessResultFailure)
        assert str(result.result_details).startswith(f"{BUDGET_HALT_PREFIX} The next call was blocked by a budget.")


UNREACHABLE = "Griptape Cloud could not be reached."
UNREADABLE = "Griptape Cloud's answer could not be read."


class TestBudgetCheckFailsOpen:
    @pytest.mark.parametrize(
        ("outcome", "reason", "level"),
        [
            (httpx2.ConnectError("refused"), UNREACHABLE, logging.WARNING),
            (httpx2.ReadTimeout("slow"), UNREACHABLE, logging.WARNING),
            (_response(500), "Griptape Cloud refused the check (HTTP 500).", logging.WARNING),
            (
                _response(400, {"model": ["Ensure this field has no more than 255 characters."]}),
                "Griptape Cloud refused the check (HTTP 400).",
                logging.WARNING,
            ),
            (
                _response(403, {"detail": "not permitted"}),
                "Griptape Cloud refused the check (HTTP 403).",
                logging.WARNING,
            ),
            (_response(404), "this Griptape Cloud deployment does not offer budget checks.", logging.DEBUG),
            (_response(200, ["not", "an", "object"]), UNREADABLE, logging.WARNING),
            (_response(200, {"blocked_by": []}), UNREADABLE, logging.WARNING),
            (_response(200, {"allowed": "no"}), UNREADABLE, logging.WARNING),
        ],
        ids=["connect", "timeout", "500", "400", "403", "404", "not-object", "allowed-missing", "allowed-not-bool"],
    )
    def test_cleared_unchecked(self, http: MagicMock, outcome: object, reason: str, level: int) -> None:
        if isinstance(outcome, Exception):
            http.side_effect = outcome
        else:
            http.return_value = outcome

        result = _check(_manager(), BudgetAccessRequest(model="m"))

        assert isinstance(result, BudgetAccessResultSuccess)
        assert result.checked is False
        assert isinstance(result.result_details, ResultDetails)
        detail = result.result_details.result_details[0]
        assert detail.level == level
        assert f"Failed because {reason} The call will proceed" in detail.message

    def test_a_mistyped_base_url_fails_open(self) -> None:
        """A malformed port raises InvalidURL, which is not an HTTPError."""
        engine = _mock_engine()
        secrets = {"GT_CLOUD_BASE_URL": "https://cloud.griptape.ai:443x", "GT_CLOUD_API_KEY": "gt-key"}
        engine.secrets_manager.get_secret.side_effect = lambda name, **_: secrets.get(name)

        result = _check(_manager(engine), BudgetAccessRequest(model="m"))

        assert isinstance(result, BudgetAccessResultSuccess)
        assert result.checked is False
        assert UNREACHABLE in str(result.result_details)

    def test_the_message_carries_no_raw_error_text(self, http: MagicMock) -> None:
        """The raw HTTP error text is multi-line and links to MDN; an artist sees only the status."""
        http.return_value = _response(503)

        result = _check(_manager(), BudgetAccessRequest(model="m"))

        assert "\n" not in str(result.result_details)
        assert "developer.mozilla.org" not in str(result.result_details)

    def test_the_message_names_the_model(self, http: MagicMock) -> None:
        http.side_effect = httpx2.ConnectError("refused")

        result = _check(_manager(), BudgetAccessRequest(model="gpt-5-nano"))

        assert "'gpt-5-nano'" in str(result.result_details)

    def test_unreadable_body(self, http: MagicMock) -> None:
        http.return_value = httpx2.Response(200, content=b"<html>", request=httpx2.Request("POST", BASE_URL))

        result = _check(_manager(), BudgetAccessRequest(model="m"))

        assert isinstance(result, BudgetAccessResultSuccess)
        assert result.checked is False

    def test_the_check_does_not_block_the_event_loop(self) -> None:
        """Other work on the loop runs while the check waits on Cloud, not only after it returns."""
        events: list[str] = []

        class _SlowClient:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *_exc: object) -> None:
                return None

            async def post(self, *_args: Any, **_kwargs: Any) -> httpx2.Response:
                events.append("check waiting")
                await asyncio.sleep(0.05)
                events.append("check answered")
                return _response(200, {"allowed": True})

        async def other_work() -> None:
            await asyncio.sleep(0.01)
            events.append("other work ran")

        async def scenario() -> object:
            check = asyncio.create_task(_manager().on_budget_access_request(BudgetAccessRequest(model="m")))
            await other_work()
            return await check

        with patch.object(budget_manager_module.httpx2, "AsyncClient", side_effect=lambda **_: _SlowClient()):
            result = asyncio.run(scenario())

        assert events == ["check waiting", "other work ran", "check answered"]
        assert isinstance(result, BudgetAccessResultSuccess)

    def test_check_is_not_retried(self, http: MagicMock) -> None:
        http.return_value = _response(503)

        _check(_manager(), BudgetAccessRequest(model="m"))

        assert http.call_count == 1

    def test_no_credential(self, http: MagicMock) -> None:
        result = _check(_manager(_mock_engine(credential=None)), BudgetAccessRequest(model="m"))

        assert isinstance(result, BudgetAccessResultSuccess)
        assert result.checked is False
        http.assert_not_called()


class TestReportUsage:
    def test_posts_the_report(self, http: MagicMock) -> None:
        http.return_value = _response(201, {"spend_id": "s-1"})

        result = _manager().on_report_usage_request(
            ReportUsageRequest(
                declared_cost_micro_usd=12345,
                provider="anthropic",
                model="claude-sonnet-5",
                activity_type="chat_completion",
                node_type="AnthropicPrompt",
                node_id="node-7",
                correlation_id="corr-1",
            )
        )

        assert isinstance(result, ReportUsageResultSuccess)
        method, url = http.call_args.args
        assert (method, url) == ("POST", f"{BASE_URL}/api/spend")
        body = _sent(http)
        occurred_at = body.pop("occurred_at")
        assert occurred_at
        assert body == {
            "idempotency_key": result.idempotency_key,
            "declared_cost_micro_usd": 12345,
            "tags": {
                "project": ["shot-6", "star-wars-x"],
                "workflow": "poster_flow",
                "node_type": "AnthropicPrompt",
                "node_id": "node-7",
                "engine_id": "engine-1",
                "session_id": "session-1",
            },
            "provider": "anthropic",
            "model": "claude-sonnet-5",
            "activity_type": "chat_completion",
            "correlation_id": "corr-1",
        }

    def test_returns_without_waiting_on_cloud(self) -> None:
        manager = BudgetManager(MagicMock(), engine=_mock_engine())
        manager._reports = MagicMock()

        result = manager.on_report_usage_request(ReportUsageRequest(declared_cost_micro_usd=1))

        assert isinstance(result, ReportUsageResultSuccess)
        manager._reports.put.assert_called_once()

    def test_an_over_long_descriptor_is_left_out(self, http: MagicMock) -> None:
        http.return_value = _response(201)

        _manager().on_report_usage_request(ReportUsageRequest(declared_cost_micro_usd=1, provider="p" * 65, model="m"))

        assert "provider" not in _sent(http)
        assert _sent(http)["model"] == "m"

    @pytest.mark.parametrize("throttle", [_response(500), _response(429)], ids=["500", "429"])
    def test_retries_reuse_the_idempotency_key(self, http: MagicMock, throttle: httpx2.Response) -> None:
        http.side_effect = [throttle, _response(201)]

        result = _manager().on_report_usage_request(ReportUsageRequest(declared_cost_micro_usd=1))

        assert isinstance(result, ReportUsageResultSuccess)
        assert http.call_count == 2  # noqa: PLR2004
        assert _sent(http, 0)["idempotency_key"] == result.idempotency_key
        assert _sent(http, 1)["idempotency_key"] == result.idempotency_key

    def test_a_lasting_transport_error_drops_the_report(
        self, http: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        http.side_effect = httpx2.ConnectError("refused")

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            result = _manager().on_report_usage_request(ReportUsageRequest(declared_cost_micro_usd=1))

        assert isinstance(result, ReportUsageResultSuccess)
        assert http.call_count == budget_manager_module.REPORT_MAX_ATTEMPTS
        assert f"Dropped usage report {result.idempotency_key}" in caplog.text

    def test_a_mistyped_base_url_drops_the_report(self, caplog: pytest.LogCaptureFixture) -> None:
        engine = _mock_engine()
        secrets = {"GT_CLOUD_BASE_URL": "https://cloud.griptape.ai:443x", "GT_CLOUD_API_KEY": "gt-key"}
        engine.secrets_manager.get_secret.side_effect = lambda name, **_: secrets.get(name)

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            result = _manager(engine).on_report_usage_request(ReportUsageRequest(declared_cost_micro_usd=1))

        assert isinstance(result, ReportUsageResultSuccess)
        assert f"Dropped usage report {result.idempotency_key}" in caplog.text

    def test_retries_are_bounded(self, http: MagicMock, caplog: pytest.LogCaptureFixture) -> None:
        http.return_value = _response(503)

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            _manager().on_report_usage_request(ReportUsageRequest(declared_cost_micro_usd=1))

        assert http.call_count == budget_manager_module.REPORT_MAX_ATTEMPTS
        assert "Dropped usage report" in caplog.text

    @pytest.mark.parametrize(
        ("status", "level"),
        [(409, logging.ERROR), (400, logging.WARNING), (422, logging.WARNING), (404, logging.WARNING)],
    )
    def test_rejections_log_and_are_not_retried(
        self, http: MagicMock, caplog: pytest.LogCaptureFixture, status: int, level: int
    ) -> None:
        http.return_value = _response(status, {"error": "x"})

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            result = _manager().on_report_usage_request(ReportUsageRequest(declared_cost_micro_usd=1))

        assert isinstance(result, ReportUsageResultSuccess)
        assert http.call_count == 1
        assert [record.levelno for record in caplog.records] == [level]

    def test_a_credential_removed_before_sending_drops_the_report(
        self, http: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The report is queued with a credential, but the background send finds none."""
        engine = _mock_engine()
        manager = BudgetManager(MagicMock(), engine=engine)
        manager._reports = MagicMock()
        manager.on_report_usage_request(ReportUsageRequest(declared_cost_micro_usd=1))
        (body,), _ = manager._reports.put.call_args
        engine.secrets_manager.get_secret.side_effect = lambda *_, **__: None

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            manager._send_report(body)

        http.assert_not_called()
        assert "Dropped a usage report" in caplog.text

    def test_no_credential_is_not_sent(self, http: MagicMock) -> None:
        result = _manager(_mock_engine(credential=None)).on_report_usage_request(
            ReportUsageRequest(declared_cost_micro_usd=1)
        )

        assert isinstance(result, ReportUsageResultFailure)
        http.assert_not_called()

    def test_the_node_label_never_travels(self, http: MagicMock) -> None:
        http.return_value = _response(201)
        engine = _mock_engine()
        engine.context_manager.get_current_node.return_value.name = "Secret Poster Label"

        _manager(engine).on_report_usage_request(ReportUsageRequest(declared_cost_micro_usd=1, node_type="T"))

        assert "Secret Poster Label" not in json.dumps(_sent(http))


class TestTokenReports:
    """Direct calls report the provider's token counts and Griptape Cloud prices them."""

    def test_posts_the_token_counts_as_usage(self, http: MagicMock) -> None:
        http.return_value = _response(201)

        result = _manager().on_report_usage_request(
            ReportUsageRequest(
                input_tokens=1000,
                output_tokens=500,
                cache_read_tokens=900,
                cache_write_tokens=40,
                provider="anthropic",
                model="claude-haiku-4-5",
                activity_type="chat_completion",
                node_type="Agent",
            )
        )

        assert isinstance(result, ReportUsageResultSuccess)
        body = _sent(http)
        assert body["usage"] == {
            "input_tokens": 1000,
            "output_tokens": 500,
            "cache_read_tokens": 900,
            "cache_write_tokens": 40,
        }
        assert "declared_cost_micro_usd" not in body
        assert body["model"] == "claude-haiku-4-5"
        assert body["idempotency_key"] == result.idempotency_key

    def test_cache_counts_are_left_out_when_not_given(self, http: MagicMock) -> None:
        http.return_value = _response(201)

        _manager().on_report_usage_request(ReportUsageRequest(input_tokens=1000, output_tokens=500, model="gpt-5-nano"))

        assert _sent(http)["usage"] == {"input_tokens": 1000, "output_tokens": 500}

    def test_a_cost_report_still_sends_the_declared_cost(self, http: MagicMock) -> None:
        http.return_value = _response(201)

        cost = 250
        _manager().on_report_usage_request(ReportUsageRequest(declared_cost_micro_usd=cost, model="gpt-5-nano"))

        body = _sent(http)
        assert body["declared_cost_micro_usd"] == cost
        assert "usage" not in body

    def test_an_over_long_model_is_sent_on_a_token_report(self, http: MagicMock) -> None:
        """Cloud prices a token report by its model, so its length rule should answer, not a silent drop."""
        http.return_value = _response(201)
        model = "m" * 256

        _manager().on_report_usage_request(ReportUsageRequest(input_tokens=1000, output_tokens=500, model=model))

        assert _sent(http)["model"] == model

    def test_a_400_names_cloud_s_reason_at_warning(self, http: MagicMock, caplog: pytest.LogCaptureFixture) -> None:
        """Cloud owns the count rules, so a 400 is usually the caller's input, not an engine bug."""
        http.return_value = _response(
            400,
            {
                "type": "validation_error",
                "errors": [
                    {"code": "invalid", "detail": "cache_read_tokens cannot exceed input_tokens.", "attr": None}
                ],
            },
        )

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            _manager().on_report_usage_request(
                ReportUsageRequest(input_tokens=100, output_tokens=50, cache_read_tokens=900, model="m")
            )

        rejected = [r for r in caplog.records if "rejected usage report" in r.getMessage()]
        assert [r.levelno for r in rejected] == [logging.WARNING]
        assert "cache_read_tokens cannot exceed input_tokens." in rejected[0].getMessage()
        assert "calling node or the engine" in rejected[0].getMessage()

    def test_retries_resend_the_same_counts_under_the_same_key(self, http: MagicMock) -> None:
        """Cloud replays a same-key, same-counts report and rejects different counts, so retries must not vary."""
        http.side_effect = [_response(503), _response(201)]

        result = _manager().on_report_usage_request(
            ReportUsageRequest(input_tokens=1000, output_tokens=500, model="gpt-5-nano")
        )

        assert isinstance(result, ReportUsageResultSuccess)
        first, second = _sent(http, 0), _sent(http, 1)
        assert first["idempotency_key"] == second["idempotency_key"] == result.idempotency_key
        assert first["usage"] == second["usage"]

    @pytest.mark.parametrize(
        ("fields", "reason"),
        [
            ({"input_tokens": 1000, "output_tokens": 500, "declared_cost_micro_usd": 250, "model": "m"}, "not both"),
            ({"model": "m"}, "neither"),
            ({"input_tokens": 1000, "model": "m"}, "both input and output"),
            ({"output_tokens": 500, "model": "m"}, "both input and output"),
        ],
        ids=["both", "neither", "input-only", "output-only"],
    )
    def test_a_report_that_cannot_be_built_is_not_sent(self, http: MagicMock, fields: dict, reason: str) -> None:
        """Only the body's shape is checked here; Cloud enforces every other rule on the counts."""
        result = _manager().on_report_usage_request(ReportUsageRequest(**fields))

        assert isinstance(result, ReportUsageResultFailure)
        assert reason in str(result.result_details)
        assert isinstance(result.result_details, ResultDetails)
        assert result.result_details.result_details[0].level == logging.WARNING
        http.assert_not_called()

    def test_count_rules_are_left_to_cloud(self, http: MagicMock) -> None:
        """A report that breaks one of Cloud's count rules is sent; Cloud's 400 is logged and the node unaffected."""
        http.return_value = _response(400, {"type": "validation_error", "errors": [{"code": "invalid"}]})

        result = _manager().on_report_usage_request(
            ReportUsageRequest(input_tokens=100, output_tokens=50, cache_read_tokens=900, model="m")
        )

        assert isinstance(result, ReportUsageResultSuccess)
        assert http.call_count == 1


class TestReportQueue:
    """Reports post in order on one daemon thread, started with the manager."""

    def test_the_report_thread_is_a_daemon(self) -> None:
        """Engine exit must not wait on reports still queued."""
        BudgetManager(MagicMock(), engine=_mock_engine())

        threads = [t for t in threading.enumerate() if t.name == "budget-report" and t.is_alive()]

        assert threads
        assert all(t.daemon for t in threads)

    def test_a_failing_report_does_not_stop_later_ones(self) -> None:
        manager = BudgetManager(MagicMock(), engine=_mock_engine())
        posted = threading.Event()
        calls: list[dict] = []

        def send(body: dict[str, Any]) -> None:
            calls.append(body)
            if body["n"] == 1:
                raise ValueError
            posted.set()

        manager._send_report = send  # type: ignore[method-assign]
        manager._reports.put({"n": 1})
        manager._reports.put({"n": 2})

        assert posted.wait(timeout=5)
        assert [body["n"] for body in calls] == [1, 2]


class TestWiring:
    def test_the_engine_routes_both_requests_to_the_budget_manager(self, engine: Any) -> None:
        manager = engine.budget_manager
        assert (
            engine._event_manager.get_manager_for_request_type(BudgetAccessRequest) == manager.on_budget_access_request
        )
        assert engine._event_manager.get_manager_for_request_type(ReportUsageRequest) == manager.on_report_usage_request
