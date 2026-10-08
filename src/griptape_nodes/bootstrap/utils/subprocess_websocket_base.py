"""Base WebSocket mixin for subprocess communication.

This module provides a reusable base mixin with shared WebSocket client
and background task lifecycle management used by both listener and sender mixins.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from griptape_nodes.api_client import Client
from griptape_nodes.drivers.cloud_credentials import API_KEY_SECRET_NAME
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from typing import Any

logger = logging.getLogger(__name__)

_ATTEMPTED_SUBPROCESS_RUN = (
    "Attempted to run a node group with Private Execution or in a library execution environment."
)


class SubprocessWebSocketUnavailableError(RuntimeError):
    """Raised when the subprocess WebSocket connection cannot be opened with the current configuration."""


@dataclass
class WebSocketMessage:
    """Message to send via WebSocket."""

    event_type: str
    payload: str
    topic: str | None = None


class SubprocessWebSocketBaseMixin:
    """Base mixin providing shared WebSocket client and task lifecycle management.

    This mixin handles:
    - Session ID management
    - WebSocket client creation and cleanup
    - Background task lifecycle (creation, cancellation, cleanup)

    Subclasses should use the protected methods to build their specific functionality.
    """

    _session_id: str
    _ws_client: Client | None
    _ws_task: asyncio.Task | None

    def _init_websocket_base(self, session_id: str) -> None:
        """Initialize shared WebSocket state.

        Args:
            session_id: Unique session ID for WebSocket topic.
        """
        self._session_id = session_id
        self._ws_client = None
        self._ws_task = None

    def _get_session_id(self) -> str:
        """Get the session ID used for WebSocket communication."""
        return self._session_id

    async def _start_websocket_client(self) -> None:
        """Start the WebSocket client connection.

        Creates and connects the WebSocket client.
        Subclasses should call this, then perform additional setup (subscribe, etc.).

        Raises:
            SubprocessWebSocketUnavailableError: If there is no API key.
        """
        api_key = self._require_websocket_api_key()
        logger.info("Starting WebSocket client for session %s", self._session_id)
        self._ws_client = Client(api_key=api_key)
        await self._ws_client.connect()
        logger.info("WebSocket client connected for session %s", self._session_id)

    def _require_websocket_api_key(self) -> str:
        """Return the API key the subprocess WebSocket connects with, or raise if it cannot connect.

        Running a workflow in a subprocess carries its events over a WebSocket to Griptape Cloud,
        which authenticates with GT_CLOUD_API_KEY. Checking up front turns a connection that would be
        rejected or time out into an error that says what is missing.
        """
        api_key = GriptapeNodes.SecretsManager().get_secret(API_KEY_SECRET_NAME, should_error_on_not_found=False)
        if not api_key:
            msg = (
                f"{_ATTEMPTED_SUBPROCESS_RUN} Failed due to no {API_KEY_SECRET_NAME} being set. "
                f"Run 'gtn init', set {API_KEY_SECRET_NAME}, or run the group without Private Execution."
            )
            raise SubprocessWebSocketUnavailableError(msg)

        return api_key

    def _create_websocket_task(self, coro: Coroutine[Any, Any, None]) -> None:
        """Create a background task for WebSocket operations.

        Args:
            coro: The coroutine to run as a background task.
        """
        self._ws_task = asyncio.create_task(coro)

    async def _stop_websocket_task(self) -> None:
        """Cancel and clean up the background task."""
        if self._ws_task is None:
            return

        self._ws_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._ws_task
        self._ws_task = None

    async def _stop_websocket_client(self) -> None:
        """Close the WebSocket client connection."""
        if self._ws_client is None:
            return

        await self._ws_client.disconnect()
        self._ws_client = None
        logger.info("WebSocket client disconnected for session %s", self._session_id)
