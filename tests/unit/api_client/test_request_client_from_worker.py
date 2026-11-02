"""Tests for `RequestClient.request_from_worker`."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from griptape_nodes.api_client.request_client import RequestClient
from griptape_nodes.retained_mode.events import converters
from griptape_nodes.retained_mode.events.base_events import (
    EventRequest,
    EventResultFailure,
    ForwardedException,
    RequestPayload,
)
from griptape_nodes.retained_mode.events.budget_events import BudgetAccessRequest, BudgetAccessResultFailure
from griptape_nodes.retained_mode.events.converters import EventSerializationError
from griptape_nodes.retained_mode.managers.event_manager import _read_forwarded_result
from griptape_nodes.utils.budget_refusal import BudgetExceededError, BudgetRefusal


class _NoJsonForm:
    """A value neither the converter nor JSON knows how to write."""


@dataclasses.dataclass
class _RequestHoldingAnything(RequestPayload):
    anything: Any = None


class _FakeClient:
    def __init__(self) -> None:
        self.publish = AsyncMock()
        self.subscribe = AsyncMock()
        self._filters: list = []

    def add_message_filter(self, fn: Any) -> None:
        self._filters.append(fn)

    def remove_message_filter(self, fn: Any) -> None:
        self._filters.remove(fn)


@pytest_asyncio.fixture
async def request_client() -> Any:
    """RequestClient wired against a fake Client."""
    rc = RequestClient(
        client=_FakeClient(),  # type: ignore[arg-type]
        request_topic_fn=lambda: "request",
        response_topic_fn=lambda: "response",
    )
    async with rc:
        yield rc


class TestRequestToOrchestrator:
    @pytest.mark.asyncio
    async def test_unsendable_request_raises_and_leaves_nothing_pending(self, request_client: RequestClient) -> None:
        event_request = EventRequest(request=_RequestHoldingAnything(anything=_NoJsonForm()))

        with pytest.raises(EventSerializationError):
            await request_client.request_from_worker(
                event_request, request_topic="orchestrator", worker_response_topic="worker"
            )

        assert request_client._pending_requests == {}


class TestAForwardedFailure:
    """A failure the other engine answers with comes back as its result, not as an error raised in its place."""

    @pytest.mark.asyncio
    async def test_a_refused_budget_check_arrives_as_its_failure_result(self, request_client: RequestClient) -> None:
        refusal = BudgetAccessResultFailure(
            blocked_by=[{"budget_name": "Studio monthly"}],
            correlation_id="corr-1",
            result_details='Budget stopped this run. The next call was blocked by the budget "Studio monthly".',
            exception=BudgetExceededError("Budget stopped this run.", BudgetRefusal()),
        )
        event_request = EventRequest(request=BudgetAccessRequest(model="gpt-4o"), request_id="req-1")
        reply = json.loads(converters.engine.dumps(EventResultFailure(request=event_request.request, result=refusal)))
        reply["request_id"] = "req-1"

        sending = asyncio.ensure_future(
            request_client.request_from_worker(
                event_request, request_topic="orchestrator", worker_response_topic="worker"
            )
        )
        await asyncio.sleep(0)
        assert await request_client._try_match({"payload": reply})
        answer = await asyncio.wait_for(sending, timeout=1.0)

        result = _read_forwarded_result(event_request.request, answer)
        assert isinstance(result, BudgetAccessResultFailure)
        assert result.blocked_by == [{"budget_name": "Studio monthly"}]
        assert isinstance(result.exception, ForwardedException)
        # The type, by the name a worker-side node matches to rebuild the refusal.
        assert result.exception.original_type == f"{BudgetExceededError.__module__}.{BudgetExceededError.__qualname__}"
