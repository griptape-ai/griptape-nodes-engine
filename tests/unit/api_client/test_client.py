"""Tests for WebSocket client large payload warning."""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock

import pytest

from griptape_nodes.api_client.client import LARGE_PAYLOAD_WARNING_THRESHOLD, Client


class TestClientLargePayloadWarning:
    @pytest.fixture
    def client(self) -> Client:
        """Client with a mocked WebSocket so _send_message can run without a real connection."""
        c = Client(api_key="test_key", url="ws://localhost")
        c._websocket = AsyncMock()
        return c

    @pytest.mark.asyncio
    async def test_no_warning_for_small_payload(self, client: Client, caplog: pytest.LogCaptureFixture) -> None:
        """No warning is logged when the serialized message is under the threshold."""
        message = {"type": "test_event", "payload": {"data": "small"}, "topic": "test/topic"}

        with caplog.at_level(logging.WARNING, logger="griptape_nodes_client"):
            await client._send_message(message)

        assert not any("large" in record.message.lower() for record in caplog.records)

    @pytest.mark.asyncio
    async def test_warns_for_large_payload(self, client: Client, caplog: pytest.LogCaptureFixture) -> None:
        """A warning including the event type is logged when the message exceeds the threshold."""
        large_data = "x" * (LARGE_PAYLOAD_WARNING_THRESHOLD + 1)
        message = {"type": "test_event", "payload": {"data": large_data}, "topic": "test/topic"}

        with caplog.at_level(logging.WARNING, logger="griptape_nodes_client"):
            await client._send_message(message)

        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning_records) == 1
        assert "test_event" in warning_records[0].message

    @pytest.mark.asyncio
    async def test_message_still_sent_when_large(self, client: Client) -> None:
        """The message is still delivered over the WebSocket even when the payload is large."""
        large_data = "x" * (LARGE_PAYLOAD_WARNING_THRESHOLD + 1)
        message = {"type": "test_event", "payload": {"data": large_data}, "topic": "test/topic"}

        await client._send_message(message)

        client._websocket.send.assert_called_once_with(json.dumps(message))


MIN_BOUNDED_ITERATIONS = 3


class TestBoundedReconnect:
    """The bounded reconnect path (max_reconnect_delay_s / pre_reconnect_check).

    The library iterator's exponential backoff caps near 90s, which suits a
    long-lived cloud relay but strands a consumer whose peers churn: a fresh
    server on a recycled address can sit unreached for the whole ceiling.
    These tests pin the bounded loop's contract without a real socket.
    """

    @pytest.mark.asyncio
    async def test_pre_reconnect_check_false_skips_the_dial(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failing check must wait one bounded delay and never dial (or log)."""
        checks = 0

        async def never_ready() -> bool:
            nonlocal checks
            checks += 1
            return False

        client = Client(
            api_key="k", url="ws://localhost:1", max_reconnect_delay_s=0.01, pre_reconnect_check=never_ready
        )
        dialed = AsyncMock()
        monkeypatch.setattr("griptape_nodes.api_client.client.connect", dialed)

        import asyncio

        task = asyncio.create_task(client._manage_connection_bounded())
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert checks >= MIN_BOUNDED_ITERATIONS  # kept looking on the bounded cadence
        dialed.assert_not_called()

    @pytest.mark.asyncio
    async def test_dial_failure_retries_on_the_bounded_cadence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """OSError from a dial is a transient: retry, never die, never back off past the cap."""
        client = Client(api_key="k", url="ws://localhost:1", max_reconnect_delay_s=0.01)
        attempts = 0

        async def refused(*_args: object, **_kwargs: object) -> object:
            nonlocal attempts
            attempts += 1
            msg = "connection refused"
            raise OSError(msg)

        monkeypatch.setattr("griptape_nodes.api_client.client.connect", refused)

        import asyncio

        task = asyncio.create_task(client._manage_connection_bounded())
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert attempts >= MIN_BOUNDED_ITERATIONS

    @pytest.mark.asyncio
    async def test_connects_and_hands_off_when_ready(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A passing check dials once and hands the socket to the session handler."""

        async def ready() -> bool:
            return True

        client = Client(api_key="k", url="ws://localhost:1", max_reconnect_delay_s=0.01, pre_reconnect_check=ready)
        fake_socket = object()

        async def dial(*_args: object, **_kwargs: object) -> object:
            return fake_socket

        sessions: list[object] = []

        async def session(websocket: object) -> bool:
            sessions.append(websocket)
            return False  # do not reconnect: the loop must exit

        monkeypatch.setattr("griptape_nodes.api_client.client.connect", dial)
        monkeypatch.setattr(client, "_handle_websocket_session", session)

        await client._manage_connection_bounded()

        assert sessions == [fake_socket]
