"""WebSocket listener mixin for subprocess communication.

This module provides a reusable mixin for listening to WebSocket events
from subprocess executions using native asyncio. Used by both
SubprocessWorkflowExecutor and SubprocessWorkflowPublisher.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from griptape_nodes.bootstrap.utils.subprocess_websocket_base import SubprocessWebSocketBaseMixin

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


class SubprocessWebSocketListenerMixin(SubprocessWebSocketBaseMixin):
    """Mixin providing WebSocket listener functionality for subprocess communication.

    This mixin handles:
    - Starting/stopping a WebSocket listener as a background task
    - Subscribing to session-specific topics
    - Processing incoming events and calling callbacks

    Subclasses should implement _handle_subprocess_event() for custom event handling.
    """

    _on_event: Callable[[dict], None] | None

    def _init_websocket_listener(
        self,
        session_id: str | None = None,
        on_event: Callable[[dict], None] | None = None,
    ) -> None:
        """Initialize WebSocket listener state.

        Args:
            session_id: Unique session ID for WebSocket topic subscription.
                       If None, a random UUID will be generated.
            on_event: Optional callback invoked for each received event.
        """
        self._init_websocket_base(session_id or uuid.uuid4().hex)
        self._on_event = on_event

    async def _start_websocket_listener(self) -> None:
        """Start WebSocket client and listener background task."""
        logger.debug("Starting WebSocket listener for session %s", self._session_id)

        await self._start_websocket_client()

        if self._ws_client is None:
            msg = "WebSocket client failed to initialize"
            raise RuntimeError(msg)

        topic = f"sessions/{self._session_id}/response"
        await self._ws_client.subscribe(topic)

        self._create_websocket_task(self._ws_listen_loop())

        logger.debug("WebSocket listener started for session %s", self._session_id)

    async def _ws_listen_loop(self) -> None:
        """Background task to process incoming messages."""
        logger.debug("Starting WebSocket listen loop for session %s", self._session_id)

        if self._ws_client is None:
            logger.error("WebSocket client not available for listening")
            return

        try:
            async for message in self._ws_client.messages:
                try:
                    logger.debug("Received WebSocket message: %s", message.get("type"))
                    await self._process_listener_event(message)
                except Exception:
                    logger.exception(
                        "Error processing WebSocket message of type '%s' for session %s",
                        message.get("type", "unknown"),
                        self._session_id,
                    )
        except asyncio.CancelledError:
            logger.debug("WebSocket listener cancelled for session %s", self._session_id)
        except Exception as e:
            logger.error("Error in WebSocket message listener: %s", e)

        logger.debug("WebSocket listen loop ended for session %s", self._session_id)

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
        """Stop the listener task and close client."""
        logger.debug("Stopping WebSocket listener for session %s", self._session_id)

        await self._stop_websocket_task()
        await self._stop_websocket_client()

        logger.debug("WebSocket listener stopped for session %s", self._session_id)

    async def _handle_subprocess_event(self, event: dict) -> None:
        """Handle subprocess-specific events.

        Override this method in subclasses to implement custom event handling.
        The default implementation does nothing.

        Args:
            event: The event dictionary received from the subprocess
        """
