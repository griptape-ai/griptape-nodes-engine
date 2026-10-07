"""Base WebSocket mixin for subprocess communication.

This module provides a reusable base mixin with shared session and background
task lifecycle management used by both listener and sender mixins.

The parent process (listener) runs a WebSocket server on the loopback interface and
the child process (sender) connects to it directly. Nothing leaves the machine, so
Private Execution works without Griptape Cloud.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from typing import Any

logger = logging.getLogger(__name__)

# Environment variable carrying the token the child presents to the parent's event server.
# It travels in the environment rather than on the command line because other local users
# can read a process's command line.
SUBPROCESS_EVENTS_TOKEN_ENV_VAR = "GTN_SUBPROCESS_EVENTS_TOKEN"  # noqa: S105


class SubprocessEventChannelError(Exception):
    """Raised when the channel between a parent engine and its subprocess cannot be used."""


@dataclass
class WebSocketMessage:
    """Message to send via WebSocket."""

    event_type: str
    payload: str
    topic: str | None = None


class SubprocessWebSocketBaseMixin:
    """Base mixin providing shared session and task lifecycle management.

    This mixin handles:
    - Session ID management
    - Background task lifecycle (creation, cancellation, cleanup)

    Subclasses should use the protected methods to build their specific functionality.
    """

    _session_id: str
    _ws_task: asyncio.Task | None

    def _init_websocket_base(self, session_id: str) -> None:
        """Initialize shared WebSocket state.

        Args:
            session_id: Unique session ID for WebSocket topic.
        """
        self._session_id = session_id
        self._ws_task = None

    def _get_session_id(self) -> str:
        """Get the session ID used for WebSocket communication."""
        return self._session_id

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
