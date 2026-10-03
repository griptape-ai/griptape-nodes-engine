"""Tests for BudgetManager's direct-provider handlers: the budget check and the usage report.

HTTP is stubbed at `httpx2.request`, below `request_with_retry`, so the real retry loop runs and
the tests can see every attempt it makes.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import Future
from typing import TYPE_CHECKING, Any
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
    from collections.abc import Callable, Iterator

BASE_URL = "https://cloud.example.test"


class _InlineExecutor:
    """Runs submitted work on the spot, so a test sees the report's HTTP calls before it asserts."""

    def submit(self, fn: Callable[..., Any], *args: Any) -> Future:
        future: Future = Future()
        future.set_result(fn(*args))
        return future


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
    manager._report_executor = _InlineExecutor()  # type: ignore[assignment]
    return manager


def _response(status: int, body: object = None) -> httpx2.Response:
    request = httpx2.Request("POST", BASE_URL)
    if body is None:
        return httpx2.Response(status, request=request)
    return httpx2.Response(status, json=body, request=request)


@pytest.fixture
def http() -> Iterator[MagicMock]:
    """Stub `httpx2.request`, with the retry loop's backoff removed."""

    def no_wait_retry(*args: Any, **kwargs: Any) -> httpx2.Response:
        return request_with_retry(*args, wait=wait_none(), **kwargs)

    with (
        patch("griptape_nodes.utils.http_utils.httpx2.request") as request,
        patch.object(budget_manager_module, "request_with_retry", side_effect=no_wait_retry),
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

        result = _manager().on_budget_access_request(BudgetAccessRequest(model_id="m"))

        assert isinstance(result, BudgetAccessResultSuccess)
        assert result.checked is True
        assert result.effective_remaining_credits is None

    def test_cleared_with_headroom(self, http: MagicMock) -> None:
        http.return_value = _response(200, {"allowed": True, "blocked_by": [], "effective_remaining_credits": 13})

        result = _manager().on_budget_access_request(BudgetAccessRequest(model_id="gtc_claude_opus_4_7"))

        assert isinstance(result, BudgetAccessResultSuccess)
        assert result.checked is True
        assert result.effective_remaining_credits == 13  # noqa: PLR2004

    def test_posts_the_check_with_attribution_in_tags(self, http: MagicMock) -> None:
        http.return_value = _response(200, {"allowed": True})

        result = _manager().on_budget_access_request(
            BudgetAccessRequest(
                model_id="gtc_claude_opus_4_7",
                estimated_cost_micro_usd=40000,
                node_type="AnthropicPrompt",
                node_id="node-7",
            )
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
            "model": "gtc_claude_opus_4_7",
            "estimated_cost_micro_usd": 40000,
            "correlation_id": result.correlation_id,
        }

    def test_leaves_out_what_is_unknown(self, http: MagicMock) -> None:
        http.return_value = _response(200, {"allowed": True})
        engine = _mock_engine()
        engine.project_manager.get_project_chain.return_value = []
        engine.context_manager.has_current_workflow.return_value = False
        engine.get_engine_id.return_value = None
        engine.get_session_id.return_value = None

        _manager(engine).on_budget_access_request(BudgetAccessRequest(model_id="m"))

        assert _sent(http) == {"tags": {}, "model": "m", "correlation_id": _sent(http)["correlation_id"]}

    def test_each_check_mints_its_own_correlation_id(self, http: MagicMock) -> None:
        http.return_value = _response(200, {"allowed": True})
        manager = _manager()

        first = manager.on_budget_access_request(BudgetAccessRequest(model_id="m"))
        second = manager.on_budget_access_request(BudgetAccessRequest(model_id="m"))

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

        result = _manager().on_budget_access_request(BudgetAccessRequest(model_id="m"))

        assert isinstance(result, BudgetAccessResultFailure)
        assert result.blocked_by == blocked_by
        message = str(result.result_details)
        assert message.startswith(BUDGET_HALT_PREFIX)
        assert '"Marketing Q3"' in message
        assert '"Studio" (frozen)' in message
        assert isinstance(result.exception, BudgetExceededError)
        assert result.exception.refusal.spend_id == "s-1"

    def test_a_deny_naming_no_budget_still_refuses(self, http: MagicMock) -> None:
        http.return_value = _response(200, {"allowed": False, "blocked_by": [{"unexpected": True}]})

        result = _manager().on_budget_access_request(BudgetAccessRequest(model_id="m"))

        assert isinstance(result, BudgetAccessResultFailure)
        assert str(result.result_details).startswith(f"{BUDGET_HALT_PREFIX} The next call was blocked by a budget.")


class TestBudgetCheckFailsOpen:
    @pytest.mark.parametrize(
        "outcome",
        [
            httpx2.ConnectError("refused"),
            httpx2.ReadTimeout("slow"),
            _response(500),
            _response(403, {"detail": "not permitted"}),
            _response(404),
            _response(200, ["not", "an", "object"]),
            _response(200, {"blocked_by": []}),
            _response(200, {"allowed": "no"}),
        ],
        ids=["connect", "timeout", "500", "403", "404", "not-object", "allowed-missing", "allowed-not-bool"],
    )
    def test_cleared_unchecked(self, http: MagicMock, outcome: object) -> None:
        if isinstance(outcome, Exception):
            http.side_effect = outcome
        else:
            http.return_value = outcome

        result = _manager().on_budget_access_request(BudgetAccessRequest(model_id="m"))

        assert isinstance(result, BudgetAccessResultSuccess)
        assert result.checked is False
        assert isinstance(result.result_details, ResultDetails)
        assert result.result_details.result_details[0].level == logging.WARNING

    def test_unreadable_body(self, http: MagicMock) -> None:
        http.return_value = httpx2.Response(200, content=b"<html>", request=httpx2.Request("POST", BASE_URL))

        result = _manager().on_budget_access_request(BudgetAccessRequest(model_id="m"))

        assert isinstance(result, BudgetAccessResultSuccess)
        assert result.checked is False

    def test_check_is_not_retried(self, http: MagicMock) -> None:
        http.return_value = _response(503)

        _manager().on_budget_access_request(BudgetAccessRequest(model_id="m"))

        assert http.call_count == 1

    def test_no_credential(self, http: MagicMock) -> None:
        result = _manager(_mock_engine(credential=None)).on_budget_access_request(BudgetAccessRequest(model_id="m"))

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
        assert body.pop("occurred_at")
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
        manager._report_executor = MagicMock()

        result = manager.on_report_usage_request(ReportUsageRequest(declared_cost_micro_usd=1))

        assert isinstance(result, ReportUsageResultSuccess)
        manager._report_executor.submit.assert_called_once()

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

    def test_retries_are_bounded(self, http: MagicMock, caplog: pytest.LogCaptureFixture) -> None:
        http.return_value = _response(503)

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            _manager().on_report_usage_request(ReportUsageRequest(declared_cost_micro_usd=1))

        assert http.call_count == budget_manager_module.REPORT_MAX_ATTEMPTS
        assert "Dropped usage report" in caplog.text

    @pytest.mark.parametrize(
        ("status", "level"),
        [(409, logging.ERROR), (400, logging.ERROR), (422, logging.WARNING), (404, logging.WARNING)],
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

    def test_negative_cost_is_not_sent(self, http: MagicMock) -> None:
        result = _manager().on_report_usage_request(ReportUsageRequest(declared_cost_micro_usd=-1))

        assert isinstance(result, ReportUsageResultFailure)
        assert isinstance(result.result_details, ResultDetails)
        assert result.result_details.result_details[0].level == logging.WARNING
        http.assert_not_called()

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


class TestWiring:
    def test_the_engine_routes_both_requests_to_the_budget_manager(self, engine: Any) -> None:
        manager = engine.budget_manager
        assert engine._event_manager._request_type_to_manager[BudgetAccessRequest] == manager.on_budget_access_request
        assert engine._event_manager._request_type_to_manager[ReportUsageRequest] == manager.on_report_usage_request
