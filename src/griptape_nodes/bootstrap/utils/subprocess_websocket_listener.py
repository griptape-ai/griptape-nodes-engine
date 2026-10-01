"""WebSocket listener mixin for subprocess communication.

This module provides a reusable mixin for receiving WebSocket events
from subprocess executions using native asyncio. Used by both
SubprocessWorkflowExecutor and SubprocessWorkflowPublisher.

The listener runs a WebSocket server on the loopback interface. The subprocess
connects to it directly, so events never leave the machine.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
import uuid
from http import HTTPStatus
from typing import TYPE_CHECKING

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosedError

from griptape_nodes.bootstrap.utils.subprocess_websocket_base import (
    SUBPROCESS_EVENTS_TOKEN_ENV_VAR,
    SubprocessEventChannelError,
    SubprocessWebSocketBaseMixin,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from websockets.asyncio.server import Server, ServerConnection
    from websockets.http11 import Request, Response

logger = logging.getLogger(__name__)

# Only processes on this machine can reach the event server.
EVENT_SERVER_HOST = "127.0.0.1"

# How long to wait, after the subprocess exits, for the events it already sent to be processed.
EVENT_DRAIN_TIMEOUT_SECONDS = 5.0


class SubprocessWebSocketListenerMixin(SubprocessWebSocketBaseMixin):
    """Mixin providing WebSocket listener functionality for subprocess communication.

    This mixin handles:
    - Starting/stopping a loopback WebSocket server for the subprocess to connect to
    - Rejecting connections that do not present this run's token
    - Processing incoming events and calling callbacks

    Subclasses should implement _handle_subprocess_event() for custom event handling.
    """

    _on_event: Callable[[dict], None] | None
    _ws_server: Server | None
    _events_url: str | None
    _events_token: str
    _open_subprocess_connections: int
    _subprocess_connections_closed: asyncio.Event

    def _init_websocket_listener(
        self,
        session_id: str | None = None,
        on_event: Callable[[dict], None] | None = None,
    ) -> None:
        """Initialize WebSocket listener state.

        Args:
            session_id: Unique session ID for the subprocess.
                       If None, a random UUID will be generated.
            on_event: Optional callback invoked for each received event.
        """
        self._init_websocket_base(session_id or uuid.uuid4().hex)
        self._on_event = on_event
        self._ws_server = None
        self._events_url = None
        self._events_token = secrets.token_urlsafe(32)
        self._open_subprocess_connections = 0
        self._subprocess_connections_closed = asyncio.Event()
        self._subprocess_connections_closed.set()

    async def _start_websocket_listener(self) -> None:
        """Start the event server the subprocess connects to."""
        logger.info("Starting WebSocket listener for session %s", self._session_id)

        # Port 0 lets the operating system pick a free port. max_size=None because a
        # workflow's final result can carry media well over websockets' 1 MiB default,
        # and only a caller holding this run's token gets far enough to send anything.
        self._ws_server = await serve(
            self._handle_subprocess_connection,
            EVENT_SERVER_HOST,
            0,
            max_size=None,
            process_request=self._authorize_subprocess_connection,
        )
        port = self._ws_server.sockets[0].getsockname()[1]
        self._events_url = f"ws://{EVENT_SERVER_HOST}:{port}/"

        logger.info("WebSocket listener started for session %s on %s", self._session_id, self._events_url)

    def _get_events_url(self) -> str:
        """Get the address the subprocess should connect to.

        Raises:
            SubprocessEventChannelError: If the listener has not been started.
        """
        if self._events_url is None:
            msg = (
                "Attempted to start an isolated run. "
                "Failed because the main engine was not yet listening for the run's results."
            )
            raise SubprocessEventChannelError(msg)
        return self._events_url

    def _get_events_env(self) -> dict[str, str]:
        """Get the environment variables the subprocess needs to connect back."""
        return {SUBPROCESS_EVENTS_TOKEN_ENV_VAR: self._events_token}

    async def _wait_for_subprocess_events(self) -> None:
        """Wait until the events the subprocess sent before exiting have been processed.

        Call this after the subprocess exits and before reading any result it reported.
        """
        try:
            await asyncio.wait_for(self._subprocess_connections_closed.wait(), timeout=EVENT_DRAIN_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.warning(
                "Timed out waiting for events from the subprocess for session %s to finish arriving",
                self._session_id,
            )

    def _authorize_subprocess_connection(self, connection: ServerConnection, request: Request) -> Response | None:
        """Reject any connection that does not present this run's token.

        The events this server receives set the run's outputs, so another local process must
        not be able to inject them.
        """
        expected = f"Bearer {self._events_token}"
        presented = request.headers.get("Authorization", "")
        if not hmac.compare_digest(presented.encode(), expected.encode()):
            logger.warning("Rejected a connection to the event server for session %s", self._session_id)
            return connection.respond(HTTPStatus.UNAUTHORIZED, "Unauthorized\n")

        # Counted here, before the handshake completes, so the subprocess cannot send an event
        # (and exit) before the drain wait knows there is a connection to wait for.
        self._open_subprocess_connections += 1
        self._subprocess_connections_closed.clear()
        return None

    async def _handle_subprocess_connection(self, connection: ServerConnection) -> None:
        """Process every event the subprocess sends over one connection."""
        logger.debug("Subprocess connected for session %s", self._session_id)
        try:
            async for raw_message in connection:
                await self._process_raw_message(raw_message)
        except ConnectionClosedError as e:
            logger.warning("Subprocess connection for session %s closed unexpectedly: %s", self._session_id, e)
        finally:
            self._open_subprocess_connections -= 1
            if self._open_subprocess_connections == 0:
                self._subprocess_connections_closed.set()

        logger.debug("Subprocess disconnected for session %s", self._session_id)

    async def _process_raw_message(self, raw_message: str | bytes) -> None:
        """Decode one message from the subprocess and process it."""
        try:
            message = json.loads(raw_message)
        except json.JSONDecodeError:
            logger.error("Failed to parse message from subprocess for session %s", self._session_id)
            return

        try:
            logger.debug("Received WebSocket message: %s", message.get("type"))
            await self._process_listener_event(message)
        except Exception:
            logger.exception(
                "Error processing WebSocket message of type '%s' for session %s",
                message.get("type", "unknown"),
                self._session_id,
            )

    async def _process_listener_event(self, event: dict) -> None:
        """Process events received from the subprocess via WebSocket.

        This method:
        1. Calls the on_event callback if provided (for GUI updates)
        2. Delegates to _handle_subprocess_event for subclass-specific handling

        Args:
            event: The event dictionary received from the subprocess
        """
        if self._on_event:
            self._on_event(event)

        await self._handle_subprocess_event(event)

    async def _stop_websocket_listener(self) -> None:
        """Stop the event server and close any open subprocess connection."""
        logger.info("Stopping WebSocket listener for session %s", self._session_id)

        if self._ws_server is not None:
            self._ws_server.close()
            await self._ws_server.wait_closed()
            self._ws_server = None

        logger.info("WebSocket listener stopped for session %s", self._session_id)

    async def _handle_subprocess_event(self, event: dict) -> None:
        """Handle subprocess-specific events.

        Override this method in subclasses to implement custom event handling.
        The default implementation does nothing.

        Args:
            event: The event dictionary received from the subprocess
        """
