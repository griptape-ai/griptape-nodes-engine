"""Tests for the loopback event channel between a parent engine and its subprocess."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from griptape_nodes.bootstrap.utils.subprocess_websocket_base import (
    SUBPROCESS_EVENTS_TOKEN_ENV_VAR,
    SubprocessEventChannelError,
)
from griptape_nodes.bootstrap.utils.subprocess_websocket_listener import (
    EVENT_SERVER_HOST,
    SubprocessWebSocketListenerMixin,
)
from griptape_nodes.bootstrap.utils.subprocess_websocket_sender import SubprocessWebSocketSenderMixin

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class _Listener(SubprocessWebSocketListenerMixin):
    """Parent side: records each event after an optional delay, like a slow handler would."""

    def __init__(self, handling_delay_seconds: float = 0.0) -> None:
        self._init_websocket_listener(on_event=None)
        self.handled: list[dict] = []
        self._handling_delay_seconds = handling_delay_seconds

    async def _handle_subprocess_event(self, event: dict) -> None:
        await asyncio.sleep(self._handling_delay_seconds)
        self.handled.append(event)


class _Sender(SubprocessWebSocketSenderMixin):
    """Child side."""

    def __init__(self, events_url: str | None) -> None:
        self._init_websocket_sender("session-1", events_url)


def _auth_header(listener: _Listener) -> dict[str, str]:
    return {"Authorization": f"Bearer {listener._get_events_env()[SUBPROCESS_EVENTS_TOKEN_ENV_VAR]}"}


@pytest_asyncio.fixture
async def listener() -> AsyncIterator[_Listener]:
    """A started event server, stopped after the test."""
    listener = _Listener()
    await listener._start_websocket_listener()
    yield listener
    await listener._stop_websocket_listener()


class TestEventServer:
    @pytest.mark.asyncio
    async def test_listens_on_loopback_only(self, listener: _Listener) -> None:
        assert listener._get_events_url().startswith(f"ws://{EVENT_SERVER_HOST}:")

    @pytest.mark.asyncio
    async def test_events_url_before_start_raises(self) -> None:
        with pytest.raises(SubprocessEventChannelError):
            _Listener()._get_events_url()

    @pytest.mark.asyncio
    async def test_each_run_gets_its_own_token(self) -> None:
        assert _Listener()._get_events_env() != _Listener()._get_events_env()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong-token"}])
    async def test_rejects_connection_without_the_run_token(self, listener: _Listener, headers: dict) -> None:
        with pytest.raises(InvalidStatus) as exc_info:
            await connect(listener._get_events_url(), additional_headers=headers)

        assert exc_info.value.response.status_code == 401  # noqa: PLR2004
        assert listener.handled == []

    @pytest.mark.asyncio
    async def test_accepts_payload_larger_than_one_mebibyte(self, listener: _Listener) -> None:
        large_value = "x" * (2 * 1024 * 1024)
        message = {"type": "execution_event", "payload": {"value": large_value}, "topic": "t"}

        async with connect(listener._get_events_url(), additional_headers=_auth_header(listener)) as ws:
            await ws.send(json.dumps(message))
        await listener._wait_for_subprocess_events()

        assert listener.handled == [message]

    @pytest.mark.asyncio
    async def test_ignores_malformed_message_and_keeps_processing(self, listener: _Listener) -> None:
        message = {"type": "execution_event", "payload": {}, "topic": "t"}

        async with connect(listener._get_events_url(), additional_headers=_auth_header(listener)) as ws:
            await ws.send("not json")
            await ws.send(json.dumps(message))
        await listener._wait_for_subprocess_events()

        assert listener.handled == [message]


class TestWaitForSubprocessEvents:
    @pytest.mark.asyncio
    async def test_waits_for_events_sent_before_the_subprocess_disconnected(self) -> None:
        listener = _Listener(handling_delay_seconds=0.2)
        await listener._start_websocket_listener()
        message = {"type": "execution_event", "payload": {}, "topic": "t"}
        try:
            async with connect(listener._get_events_url(), additional_headers=_auth_header(listener)) as ws:
                await ws.send(json.dumps(message))
            # The subprocess has closed its connection, but its last event is still being handled.
            assert listener.handled == []

            await listener._wait_for_subprocess_events()

            assert listener.handled == [message]
        finally:
            await listener._stop_websocket_listener()

    @pytest.mark.asyncio
    async def test_returns_immediately_when_the_subprocess_never_connected(self, listener: _Listener) -> None:
        await asyncio.wait_for(listener._wait_for_subprocess_events(), timeout=0.5)


class TestSender:
    @pytest.mark.asyncio
    async def test_delivers_events_to_the_parent_in_the_existing_wire_format(
        self, listener: _Listener, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name, value in listener._get_events_env().items():
            monkeypatch.setenv(name, value)
        sender = _Sender(listener._get_events_url())

        await sender._start_websocket_connection()
        sender.send_event("success_result", json.dumps({"result": "ok"}))
        await sender._wait_for_websocket_queue_flush()
        await sender._stop_websocket_connection()
        await listener._wait_for_subprocess_events()

        assert listener.handled == [
            {"type": "success_result", "payload": {"result": "ok"}, "topic": "sessions/session-1/response"}
        ]

    @pytest.mark.asyncio
    async def test_reaches_the_parent_when_the_environment_names_a_proxy(
        self, listener: _Listener, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A proxy cannot reach the parent's loopback listener, so a dial routed through one fails.
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        for name, value in listener._get_events_env().items():
            monkeypatch.setenv(name, value)
        sender = _Sender(listener._get_events_url())

        await sender._start_websocket_connection()
        sender.send_event("success_result", json.dumps({"result": "ok"}))
        await sender._wait_for_websocket_queue_flush()
        await sender._stop_websocket_connection()
        await listener._wait_for_subprocess_events()

        assert [event["type"] for event in listener.handled] == ["success_result"]

    @pytest.mark.asyncio
    async def test_never_sends_the_griptape_cloud_key_to_the_parent(
        self, listener: _Listener, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name, value in listener._get_events_env().items():
            monkeypatch.setenv(name, value)
        sender = _Sender(listener._get_events_url())

        await sender._start_websocket_connection()
        try:
            assert sender._ws_client is not None
            assert sender._ws_client.headers == _auth_header(listener)
        finally:
            await sender._stop_websocket_connection()

    @pytest.mark.asyncio
    async def test_fails_clearly_without_the_parent_address(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SUBPROCESS_EVENTS_TOKEN_ENV_VAR, "token")

        with pytest.raises(SubprocessEventChannelError, match="connection details"):
            await _Sender(None)._start_websocket_connection()

    @pytest.mark.asyncio
    async def test_fails_clearly_without_the_token(self, listener: _Listener, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(SUBPROCESS_EVENTS_TOKEN_ENV_VAR, raising=False)

        with pytest.raises(SubprocessEventChannelError, match="connection details"):
            await _Sender(listener._get_events_url())._start_websocket_connection()

    @pytest.mark.asyncio
    async def test_reports_the_reason_when_the_parent_rejects_the_token(
        self, listener: _Listener, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(SUBPROCESS_EVENTS_TOKEN_ENV_VAR, "wrong-token")

        with pytest.raises(SubprocessEventChannelError, match="HTTP 401"):
            await _Sender(listener._get_events_url())._start_websocket_connection()
