"""Tests for `RequestClient.request_batch`.

`request_batch` wraps N inner EventRequests in one EventRequestBatch frame on
the wire. Each inner request gets its own request_id and a future tracked by
the RequestClient, so responses arriving in arbitrary order can resolve their
matching slot. The tests below validate the wire shape, future fan-out,
ordering of returned results, and cancellation on failure.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from griptape_nodes.api_client.request_client import RequestClient

_BATCH_SIZE_3 = 3


class _FakeClient:
    """Minimal Client stand-in capturing publish/subscribe calls for assertions."""

    def __init__(self) -> None:
        self.publish = AsyncMock()
        self.subscribe = AsyncMock()
        self._filters: list = []

    def add_message_filter(self, fn: Any) -> None:
        self._filters.append(fn)

    def remove_message_filter(self, fn: Any) -> None:
        self._filters.remove(fn)


@pytest.fixture
def fake_client() -> _FakeClient:
    """Capture-only Client surrogate so we can assert on publish/subscribe calls."""
    return _FakeClient()


@pytest_asyncio.fixture
async def request_client(fake_client: _FakeClient) -> Any:
    """RequestClient wired against a fake Client and a stable response topic."""
    rc = RequestClient(
        client=fake_client,  # type: ignore[arg-type]
        request_topic_fn=lambda: "request",
        response_topic_fn=lambda: "response",
    )
    async with rc:
        yield rc


async def _resolve(rc: RequestClient, request_id: str, payload: dict[str, Any]) -> None:
    """Inject a successful response into the tracking map, the way `_try_match` would."""
    await rc._resolve_request(request_id, payload)


class TestRequestBatchWire:
    @pytest.mark.asyncio
    async def test_publishes_single_envelope_with_inner_event_requests(
        self, request_client: RequestClient, fake_client: _FakeClient
    ) -> None:
        """A non-empty batch produces one EventRequestBatch publish, one frame on the wire."""

        # Resolve all three slots before publish is awaited so request_batch can complete.
        async def auto_resolve() -> None:
            # Wait until publish has been called so request_ids are populated, then resolve.
            await asyncio.sleep(0)
            for request_id in list(request_client._pending_requests):
                await _resolve(request_client, request_id, {"ok": True})

        async with asyncio.TaskGroup() as tg:
            tg.create_task(auto_resolve())
            results = await request_client.request_batch(
                [
                    ("CreateConnectionRequest", {"target_node_name": "A"}),
                    ("CreateConnectionRequest", {"target_node_name": "B"}),
                    ("CreateConnectionRequest", {"target_node_name": "C"}),
                ]
            )

        # One publish, with the batch envelope shape.
        assert fake_client.publish.await_count == 1
        assert fake_client.publish.await_args is not None
        event_type, payload, topic = fake_client.publish.await_args.args
        assert event_type == "EventRequestBatch"
        assert topic == "request"
        assert payload["event_type"] == "EventRequestBatch"
        assert len(payload["requests"]) == _BATCH_SIZE_3

        # Each inner event is a fully-formed EventRequest with its own request_id.
        inner_ids: list[str] = []
        for inner, expected_target in zip(payload["requests"], ("A", "B", "C"), strict=True):
            assert inner["event_type"] == "EventRequest"
            assert inner["request_type"] == "CreateConnectionRequest"
            assert inner["response_topic"] == "response"
            assert inner["request"]["target_node_name"] == expected_target
            assert inner["request"]["request_id"] == inner["request_id"]
            inner_ids.append(inner["request_id"])

        # All inner request_ids are unique.
        assert len(set(inner_ids)) == _BATCH_SIZE_3

        # Caller got one result per inner request, in submission order.
        assert results == [{"ok": True}, {"ok": True}, {"ok": True}]

    @pytest.mark.asyncio
    async def test_empty_batch_returns_immediately_without_publishing(
        self, request_client: RequestClient, fake_client: _FakeClient
    ) -> None:
        """No inner requests means no envelope on the wire and no futures registered."""
        results = await request_client.request_batch([])

        assert results == []
        fake_client.publish.assert_not_awaited()
        assert request_client.pending_count == 0

    @pytest.mark.asyncio
    async def test_subscribes_to_response_topic_only_once(
        self, request_client: RequestClient, fake_client: _FakeClient
    ) -> None:
        """Repeat batches reuse the existing response subscription, no duplicate subscribes."""

        async def auto_resolve() -> None:
            await asyncio.sleep(0)
            for request_id in list(request_client._pending_requests):
                await _resolve(request_client, request_id, {})

        for _ in range(3):
            async with asyncio.TaskGroup() as tg:
                tg.create_task(auto_resolve())
                await request_client.request_batch([("X", {})])

        assert fake_client.subscribe.await_count == 1


class TestRequestBatchResolution:
    @pytest.mark.asyncio
    async def test_results_returned_in_submission_order_regardless_of_resolution_order(
        self, request_client: RequestClient
    ) -> None:
        """Resolving the third future first must not reorder the returned list."""

        async def out_of_order_resolve() -> None:
            await asyncio.sleep(0)
            # Snapshot in insertion order, then resolve in reverse with distinct payloads.
            ids = list(request_client._pending_requests)
            await _resolve(request_client, ids[2], {"slot": "C"})
            await _resolve(request_client, ids[0], {"slot": "A"})
            await _resolve(request_client, ids[1], {"slot": "B"})

        async with asyncio.TaskGroup() as tg:
            tg.create_task(out_of_order_resolve())
            results = await request_client.request_batch([("R", {"k": "a"}), ("R", {"k": "b"}), ("R", {"k": "c"})])

        assert [r["slot"] for r in results] == ["A", "B", "C"]

    @pytest.mark.asyncio
    async def test_failure_cancels_pending_sub_requests_and_raises(self, request_client: RequestClient) -> None:
        """A rejected inner future raises by default and pending siblings are cancelled."""

        async def reject_first() -> None:
            await asyncio.sleep(0)
            ids = list(request_client._pending_requests)
            await request_client._reject_request(ids[0], RuntimeError("boom"))

        rejecter = asyncio.create_task(reject_first())
        try:
            with pytest.raises(RuntimeError, match="boom"):
                await request_client.request_batch([("R", {}), ("R", {}), ("R", {})])
        finally:
            await rejecter

        # All inner requests have been cleaned up, regardless of which raised.
        assert request_client.pending_count == 0

    @pytest.mark.asyncio
    async def test_return_exceptions_keeps_failures_in_their_slots(self, request_client: RequestClient) -> None:
        """With return_exceptions=True the result list mirrors gather() semantics."""

        async def mixed_resolve() -> None:
            await asyncio.sleep(0)
            ids = list(request_client._pending_requests)
            await _resolve(request_client, ids[0], {"ok": True})
            await request_client._reject_request(ids[1], ValueError("bad"))
            await _resolve(request_client, ids[2], {"ok": True})

        async with asyncio.TaskGroup() as tg:
            tg.create_task(mixed_resolve())
            results = await request_client.request_batch(
                [("R", {}), ("R", {}), ("R", {})],
                return_exceptions=True,
            )

        assert results[0] == {"ok": True}
        assert isinstance(results[1], ValueError)
        assert results[2] == {"ok": True}

    @pytest.mark.asyncio
    async def test_timeout_cancels_pending_requests(self, request_client: RequestClient) -> None:
        """An overall timeout raises TimeoutError and cleans up every tracking entry."""
        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            await request_client.request_batch([("R", {}), ("R", {})], timeout_ms=10)

        assert request_client.pending_count == 0


class TestDefaultResponseTopic:
    def test_each_client_takes_its_results_on_a_topic_of_its_own(self, fake_client: _FakeClient) -> None:
        """Not a topic every client shares, so no other client gets this one's results."""
        first = RequestClient(client=fake_client)  # type: ignore[arg-type]
        second = RequestClient(client=fake_client)  # type: ignore[arg-type]

        assert first.response_topic_fn() == f"clients/{first.client_id}/response"
        assert first.response_topic_fn() != second.response_topic_fn()

    def test_its_topic_is_under_its_session_once_it_knows_one(self, fake_client: _FakeClient) -> None:
        given = RequestClient(client=fake_client, session_id="session-1")  # type: ignore[arg-type]
        learned = RequestClient(client=fake_client)  # type: ignore[arg-type]

        learned.session_id = "session-2"

        assert given.response_topic_fn() == f"sessions/session-1/clients/{given.client_id}/response"
        assert learned.response_topic_fn() == f"sessions/session-2/clients/{learned.client_id}/response"

    @pytest.mark.asyncio
    async def test_subscribes_to_its_own_topic_and_names_it_on_the_request(self, fake_client: _FakeClient) -> None:
        rc = RequestClient(client=fake_client)  # type: ignore[arg-type]
        async with rc:
            with pytest.raises(TimeoutError):
                await rc.request_batch([("CreateConnectionRequest", {})], timeout_ms=1)

        own_topic = f"clients/{rc.client_id}/response"
        fake_client.subscribe.assert_awaited_once_with(own_topic)
        assert fake_client.publish.await_args is not None
        _, payload, topic = fake_client.publish.await_args.args
        assert topic == "request"
        assert payload["requests"][0]["response_topic"] == own_topic
