"""Tests for `RequestClient.request_to_orchestrator`."""

from __future__ import annotations

import dataclasses
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from griptape_nodes.api_client.request_client import RequestClient
from griptape_nodes.retained_mode.events.base_events import EventRequest, RequestPayload
from griptape_nodes.retained_mode.events.converters import EventSerializationError


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
            await request_client.request_to_orchestrator(
                event_request, orchestrator_request_topic="orchestrator", worker_response_topic="worker"
            )

        assert request_client._pending_requests == {}
