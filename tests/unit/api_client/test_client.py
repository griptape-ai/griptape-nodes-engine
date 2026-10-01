"""Tests for the WebSocket client: large payload warning and connection failures."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import ssl
import time
from http import HTTPStatus
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from websockets.asyncio.server import serve

from griptape_nodes.api_client import client as client_module
from griptape_nodes.api_client.client import CONNECT_TIMEOUT_SECONDS, LARGE_PAYLOAD_WARNING_THRESHOLD, Client

if TYPE_CHECKING:
    from websockets.asyncio.server import ServerConnection
    from websockets.http11 import Request, Response


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


class TestClientConnectFailures:
    """connect() must report why it failed, fail fast when retrying cannot help, and stop redialing."""

    @pytest.mark.asyncio
    async def test_rejected_credentials_fail_fast_with_the_http_status(self) -> None:
        async def _reject(connection: ServerConnection, request: Request) -> Response:  # noqa: ARG001
            return connection.respond(HTTPStatus.UNAUTHORIZED, "Unauthorized\n")

        async with serve(lambda _ws: asyncio.sleep(0), "127.0.0.1", 0, process_request=_reject) as server:
            port = server.sockets[0].getsockname()[1]
            client = Client(api_key="bad-key", url=f"ws://127.0.0.1:{port}/")
            started = time.monotonic()

            with pytest.raises(ConnectionError, match="HTTP 401"):
                await client.connect()

        assert time.monotonic() - started < CONNECT_TIMEOUT_SECONDS / 2
        assert client._receiving_task is not None
        assert client._receiving_task.done()

    @pytest.mark.asyncio
    async def test_ssl_failure_fails_fast_instead_of_retrying(self) -> None:
        async def _not_tls(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(_not_tls, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = Client(api_key="key", url=f"wss://127.0.0.1:{port}/")
        started = time.monotonic()
        try:
            with pytest.raises(ConnectionError) as exc_info:
                await client.connect()
        finally:
            server.close()
            await server.wait_closed()

        assert isinstance(exc_info.value.__cause__, ssl.SSLError)
        assert time.monotonic() - started < CONNECT_TIMEOUT_SECONDS / 2

    @pytest.mark.asyncio
    async def test_unreachable_server_reports_the_network_error_and_stops_redialing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(client_module, "CONNECT_TIMEOUT_SECONDS", 0.5)
        # Bind and close a socket so the port is known to be closed.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        client = Client(api_key="key", url=f"ws://127.0.0.1:{port}/")

        with pytest.raises(ConnectionError) as exc_info:
            await client.connect()

        assert isinstance(exc_info.value.__cause__, OSError)
        assert "timeout" not in str(exc_info.value).lower()
        assert client._receiving_task is not None
        assert client._receiving_task.done()

    @pytest.mark.asyncio
    async def test_no_response_reports_the_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(client_module, "CONNECT_TIMEOUT_SECONDS", 0.5)

        async def _silent(_reader: asyncio.StreamReader, _writer: asyncio.StreamWriter) -> None:
            await asyncio.sleep(10)

        server = await asyncio.start_server(_silent, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = Client(api_key="key", url=f"ws://127.0.0.1:{port}/")
        try:
            with pytest.raises(ConnectionError, match=r"no response within 0\.5 seconds"):
                await client.connect()
        finally:
            server.close()

        assert client._receiving_task is not None
        assert client._receiving_task.done()
