"""Tests for BudgetManager's direct-provider handlers: the budget check and the usage report.

HTTP is stubbed at `httpx2.request`, below `request_with_retry`, so the real retry loop runs and
the tests can see every attempt it makes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
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


def _check(
    manager: BudgetManager, request: BudgetAccessRequest
) -> BudgetAccessResultSuccess | BudgetAccessResultFailure:
    return asyncio.run(manager.on_budget_access_request(request))


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

    def test_the_http_call_runs_off_the_event_loop_thread(self, http: MagicMock) -> None:
        loop_thread = threading.get_ident()
        http.side_effect = lambda *_, **__: _response(200, {"allowed": threading.get_ident() != loop_thread})

        result = _check(_manager(), BudgetAccessRequest(model="m"))

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

    def test_a_credential_removed_before_sending_drops_the_report(
        self, http: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The report is queued with a credential, but the background send finds none."""
        engine = _mock_engine()
        manager = BudgetManager(MagicMock(), engine=engine)
        queued: list[Callable[[], object]] = []
        manager._report_executor = MagicMock()
        manager._report_executor.submit.side_effect = lambda fn, *args: queued.append(lambda: fn(*args))
        manager.on_report_usage_request(ReportUsageRequest(declared_cost_micro_usd=1))
        engine.secrets_manager.get_secret.side_effect = lambda *_, **__: None

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            queued[0]()

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


class TestReportQueue:
    def test_no_thread_until_the_first_report(self) -> None:
        budget_manager_module._DaemonQueue("test-report-lazy")

        assert not [thread for thread in threading.enumerate() if thread.name == "test-report-lazy"]

    def test_exit_does_not_wait_on_pending_reports(self) -> None:
        queue = budget_manager_module._DaemonQueue("test-report")
        done = threading.Event()
        queue.submit(done.set)
        assert done.wait(timeout=5)

        threads = [thread for thread in threading.enumerate() if thread.name == "test-report"]
        assert threads
        assert all(thread.daemon for thread in threads)

    def test_a_failing_report_does_not_stop_later_ones(self) -> None:
        queue = budget_manager_module._DaemonQueue("test-report-survives")
        done = threading.Event()

        def boom() -> None:
            raise ValueError

        queue.submit(boom)
        queue.submit(done.set)

        assert done.wait(timeout=5)


class TestWiring:
    def test_the_engine_routes_both_requests_to_the_budget_manager(self, engine: Any) -> None:
        manager = engine.budget_manager
        assert engine._event_manager._request_type_to_manager[BudgetAccessRequest] == manager.on_budget_access_request
        assert engine._event_manager._request_type_to_manager[ReportUsageRequest] == manager.on_report_usage_request
