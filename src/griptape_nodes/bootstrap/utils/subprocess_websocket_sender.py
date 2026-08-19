"""WebSocket sender mixin for subprocess communication.

This module provides a reusable mixin for sending WebSocket events
from subprocess executions back to the parent process using native asyncio.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from griptape_nodes.bootstrap.utils.subprocess_websocket_base import SubprocessWebSocketBaseMixin, WebSocketMessage
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.events.base_events import BaseEvent

logger = logging.getLogger(__name__)


class SubprocessWebSocketSenderMixin(SubprocessWebSocketBaseMixin):
    """Mixin providing WebSocket sender functionality for subprocess communication.

    This mixin handles:
    - Starting/stopping a WebSocket connection as a background task
    - Queuing and sending events to the parent process
    - Non-blocking event emission from the main event loop
    """

    _ws_send_queue: asyncio.Queue[WebSocketMessage]
    _ws_shutdown_event: asyncio.Event

    def _init_websocket_sender(self, session_id: str) -> None:
        """Initialize WebSocket sender state.

        Args:
            session_id: Unique session ID for WebSocket topic.
        """
        self._init_websocket_base(session_id)
        self._ws_send_queue = asyncio.Queue()
        self._ws_shutdown_event = asyncio.Event()

    async def _start_websocket_connection(self) -> None:
        """Start WebSocket client and sender background task."""
        logger.info("Starting WebSocket sender for session %s", self._session_id)

        self._ws_shutdown_event.clear()
        await self._start_websocket_client()
        self._create_websocket_task(self._ws_send_loop())

        logger.info("WebSocket sender started for session %s", self._session_id)

    async def _ws_send_loop(self) -> None:
        """Background task to send queued messages."""
        logger.debug("Starting WebSocket send loop for session %s", self._session_id)

        while not self._ws_shutdown_event.is_set():
            try:
                message = await asyncio.wait_for(self._ws_send_queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            try:
                if self._ws_client is None:
                    logger.warning("WebSocket client not available, message dropped")
                    continue

                topic = message.topic or f"sessions/{self._session_id}/response"
                payload_dict = json.loads(message.payload)
                await self._ws_client.publish(message.event_type, payload_dict, topic)
                logger.debug("DELIVERED: %s event", message.event_type)
            except Exception as e:
                logger.error("Error sending WebSocket message: %s", e)
            finally:
                self._ws_send_queue.task_done()

        logger.debug("WebSocket send loop ended for session %s", self._session_id)

    def send_engine_event(self, event_type: str, event: BaseEvent) -> None:
        """Stamp an event with this subprocess's engine identity, then queue it for sending.

        Events built here never pass through the `EventManager` queue that normally stamps
        them, so they would otherwise reach the parent process with no engine or session id.
        Prefer this over serializing an event and calling `send_event` directly.

        Args:
            event_type: Type of event (e.g., "execution_event", "success_result")
            event: The event to stamp and send
        """
        GriptapeNodes.EventManager().stamp_event_identity(event)
        self.send_event(event_type, event.json())

    def send_event(self, event_type: str, payload: str) -> None:
        """Queue an event for sending via WebSocket (non-blocking).

        Args:
            event_type: Type of event (e.g., "execution_event", "success_result")
            payload: JSON string payload to send
        """
        if self._ws_task is None or self._ws_task.done():
            logger.debug("WebSocket sender not active, event not sent: %s", event_type)
            return

        topic = f"sessions/{self._session_id}/response"
        message = WebSocketMessage(event_type, payload, topic)

        try:
            self._ws_send_queue.put_nowait(message)
            logger.debug("QUEUED: %s event via websocket", event_type)
        except asyncio.QueueFull:
            logger.error("WebSocket queue full, event dropped: %s", event_type)

    async def _stop_websocket_connection(self) -> None:
        """Stop the sender task and close client."""
        logger.info("Stopping WebSocket sender for session %s", self._session_id)

        self._ws_shutdown_event.set()
        await self._stop_websocket_task()
        await self._stop_websocket_client()

        logger.info("WebSocket sender stopped for session %s", self._session_id)

    async def _wait_for_websocket_queue_flush(self, timeout_seconds: float = 5.0) -> None:
        """Wait for all queued messages to be sent.

        Args:
            timeout_seconds: Maximum time to wait for queue to flush.
        """
        if self._ws_task is None or self._ws_task.done():
            return

        try:
            await asyncio.wait_for(self._ws_send_queue.join(), timeout=timeout_seconds)
        except TimeoutError:
            logger.warning("Timeout waiting for websocket queue to flush")
