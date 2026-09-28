"""WebSocket sender mixin for subprocess communication.

This module provides a reusable mixin for sending WebSocket events
from subprocess executions back to the parent process using native asyncio.
"""

from __future__ import annotations

import asyncio
import json
import logging

from griptape_nodes.bootstrap.utils.subprocess_websocket_base import SubprocessWebSocketBaseMixin, WebSocketMessage
from griptape_nodes.retained_mode.events.base_events import (
    BaseEvent,
    EventResultFailure,
    EventResultSuccess,
    EventSerializationError,
)
from griptape_nodes.retained_mode.events.generic_events import GenericResultFailure

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

    def _send_event(self, event_type: str, event: BaseEvent) -> EventSerializationError | None:
        """Send an event, logging and skipping it if it holds a value with no JSON form.

        Returns:
            The error when the event was skipped, for callers that cannot let it go.
        """
        try:
            payload = event.json()
        except EventSerializationError as error:
            logger.error("Could not send %s: %s", event_type, error)
            return error
        self.send_event(event_type, payload)
        return None

    def _send_result(self, event_type: str, event: EventResultSuccess | EventResultFailure) -> None:
        """Send a result, or a GenericResultFailure naming why it could not be sent, so the requester hears back."""
        try:
            payload = event.json()
        except EventSerializationError as error:
            logger.error("Could not send %s for %s: %s", event_type, type(event.request).__name__, error)
            failure_event = EventResultFailure(
                request=event.request,
                result=GenericResultFailure(result_details=str(error)),
                request_id=event.request_id,
                response_topic=event.response_topic,
            )
            self.send_event("failure_result", failure_event.json())
            return
        self.send_event(event_type, payload)

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
