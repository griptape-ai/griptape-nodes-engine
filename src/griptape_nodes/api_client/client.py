"""Unified WebSocket client for Nodes API communication."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import ssl
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Self
from urllib.parse import urljoin

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, InvalidURI

from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from types import TracebackType

logger = logging.getLogger("griptape_nodes_client")

# Payload size (in bytes) above which a warning is logged before sending.
# Messages above this threshold can saturate the WebSocket send buffer and cause
# connected clients (e.g. the editor) to stall or disconnect.
LARGE_PAYLOAD_WARNING_THRESHOLD = 100_000


def get_default_websocket_url() -> str:
    """Get the default WebSocket endpoint URL for connecting to Nodes API.

    Returns:
        WebSocket URL for Nodes API events endpoint
    """
    return urljoin(
        os.getenv("GRIPTAPE_NODES_API_BASE_URL", "https://api.nodes.griptape.ai").replace("http", "ws"),
        "/ws/engines/events?version=v2",
    )


class Client:
    """WebSocket client for Nodes API pub/sub communication.

    Provides connection management, topic-based pub/sub, and message routing.
    Handles WebSocket reconnection and async event streaming.
    """

    def __init__(
        self,
        api_key: str | None = None,
        url: str | None = None,
        *,
        max_reconnect_delay_s: float | None = None,
        pre_reconnect_check: Callable[[], Awaitable[bool]] | None = None,
    ):
        """Initialize Nodes API client.

        Args:
            api_key: API key for authentication (defaults to GT_CLOUD_API_KEY from SecretsManager)
            url: WebSocket URL to connect to (defaults to Nodes API endpoint)
            max_reconnect_delay_s: Cap on the wait between reconnect attempts.
                The default reconnect path (the websockets library's iterator)
                backs off exponentially to a ~90s ceiling, which suits a
                long-lived cloud relay but strands a consumer whose peers
                CHURN: a fresh server on a recycled address can sit unreached
                for up to that ceiling. Setting this switches to a bounded
                retry loop. None keeps the library behavior.
            pre_reconnect_check: Awaited before each bounded reconnect attempt;
                returning False skips the dial (and its log line) for one
                delay. Lets a consumer substitute a cheap, silent liveness
                test (e.g. a TCP probe) for dial-and-fail against an address
                that is usually dead. Implies the bounded retry loop.
        """
        self.url = url if url is not None else get_default_websocket_url()
        self._max_reconnect_delay_s = max_reconnect_delay_s
        self._pre_reconnect_check = pre_reconnect_check

        # Get API key from SecretsManager if not provided
        if api_key is None:
            api_key = GriptapeNodes.SecretsManager().get_secret("GT_CLOUD_API_KEY")

        self.api_key = api_key

        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

        # Event streaming management
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._message_filters: list[Callable[[dict[str, Any]], Awaitable[bool]]] = []
        self._subscribed_topics: set[str] = set()
        self._receiving_task: asyncio.Task | None = None
        self._sending_task: asyncio.Task | None = None
        self._websocket: Any = None
        self._connection_ready = asyncio.Event()
        self._reconnect_delay = 2.0

    async def __aenter__(self) -> Self:
        """Async context manager entry: connect to WebSocket server."""
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Async context manager exit: disconnect from WebSocket server."""
        await self.disconnect()

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        """Return self as async iterator."""
        return self

    async def __anext__(self) -> dict[str, Any]:
        """Get next message from the message queue.

        Returns:
            Next message dictionary from subscribed topics

        Raises:
            StopAsyncIteration: When iteration is cancelled
        """
        try:
            return await self._message_queue.get()
        except asyncio.CancelledError:
            raise StopAsyncIteration from None

    @property
    def messages(self) -> AsyncIterator[dict[str, Any]]:
        """Async iterator for receiving messages from subscribed topics.

        Returns:
            Async iterator yielding message dictionaries

        Example:
            async with Client(...) as client:
                await client.subscribe("topic")
                async for message in client.messages:
                    print(message)
        """
        return self

    async def subscribe(self, topic: str) -> None:
        """Subscribe to a topic by sending subscribe command to server.

        Args:
            topic: Topic name to subscribe to

        Example:
            await client.subscribe("sessions/123/response")
        """
        self._subscribed_topics.add(topic)
        await self._send_subscribe_command(topic)

    async def unsubscribe(self, topic: str) -> None:
        """Unsubscribe from a topic.

        Args:
            topic: Topic name to unsubscribe from
        """
        self._subscribed_topics.discard(topic)
        await self._send_unsubscribe_command(topic)

    def add_message_filter(self, fn: Callable[[dict[str, Any]], Awaitable[bool]]) -> None:
        """Register a filter that can claim incoming messages before they reach the queue.

        Args:
            fn: Async callable that returns True if it handled the message (claiming it),
                or False to leave it for the next filter or the message queue.
        """
        self._message_filters.append(fn)

    def remove_message_filter(self, fn: Callable[[dict[str, Any]], Awaitable[bool]]) -> None:
        """Deregister a previously added message filter.

        Args:
            fn: The exact callable that was passed to add_message_filter.
        """
        self._message_filters.remove(fn)

    async def publish(self, event_type: str, payload: dict[str, Any], topic: str) -> None:
        """Publish an event to the server.

        Args:
            event_type: Type of event to publish
            payload: Event payload data
            topic: Topic to publish to
        """
        message = {"type": event_type, "payload": payload, "topic": topic}
        await self._send_message(message)

    async def connect(self) -> None:
        """Connect to the WebSocket server and start receiving messages.

        This method starts the connection manager task.
        It returns once the initial connection is established.

        Raises:
            ConnectionError: If connection fails
        """
        # Start connection manager task
        self._receiving_task = asyncio.create_task(self._manage_connection())

        # Wait for initial connection to be established
        try:
            await asyncio.wait_for(self._connection_ready.wait(), timeout=10.0)
            logger.debug("WebSocket client connected")
        except TimeoutError as e:
            logger.error("Failed to connect WebSocket client: timeout")
            msg = "Connection timeout - failed to connect to Nodes API."
            raise ConnectionError(msg) from e

    async def disconnect(self) -> None:
        """Disconnect from the WebSocket server and clean up tasks."""
        # Cancel tasks
        if self._receiving_task:
            self._receiving_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._receiving_task

        if self._sending_task:
            self._sending_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sending_task

        # Close websocket connection
        if self._websocket:
            await self._websocket.close()
        logger.info("WebSocket client disconnected")

    async def _manage_connection(self) -> None:
        """Manage WebSocket connection lifecycle with automatic reconnection.

        This method establishes and maintains the WebSocket connection,
        automatically reconnecting on failures.
        """
        try:
            if self._max_reconnect_delay_s is not None or self._pre_reconnect_check is not None:
                await self._manage_connection_bounded()
            else:
                async for websocket in connect(self.url, additional_headers=self.headers):
                    should_reconnect = await self._handle_websocket_session(websocket)
                    if not should_reconnect:
                        break
        except InvalidStatus as e:
            if e.response.status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
                logger.error(
                    "Nodes API rejected connection with HTTP %d. "
                    "This indicates an invalid or missing GT_CLOUD_API_KEY.",
                    e.response.status_code,
                )
            else:
                logger.error(
                    "Nodes API rejected WebSocket connection: HTTP %d.",
                    e.response.status_code,
                )
        except InvalidURI as e:
            logger.error(
                "Invalid WebSocket URL: %s. Check GRIPTAPE_NODES_API_BASE_URL configuration.",
                e,
            )
        except ssl.SSLError as e:
            logger.error(
                "SSL error while connecting to Nodes API: %s. "
                "This may indicate a certificate verification failure. "
                "Check that your system's CA certificates are up to date.",
                e,
            )
        except OSError as e:
            logger.error(
                "Network error while connecting to Nodes API: %s. "
                "Check your network connection and that the API endpoint is reachable.",
                e,
            )
        except asyncio.CancelledError:
            logger.debug("Connection manager task cancelled")

    async def _manage_connection_bounded(self) -> None:
        """Reconnect on a bounded cadence instead of the library's backoff.

        Fatal configuration failures (bad URL, rejected credentials, SSL) still
        bubble to _manage_connection's handlers and end the manager, exactly as
        the iterator path does; only transient dial failures retry here.
        """
        delay = self._max_reconnect_delay_s if self._max_reconnect_delay_s is not None else 5.0
        while True:
            if self._pre_reconnect_check is not None and not await self._pre_reconnect_check():
                await asyncio.sleep(delay)
                continue
            try:
                websocket = await connect(self.url, additional_headers=self.headers)
            except (OSError, TimeoutError):
                # The address was reachable a moment ago (or unchecked) but the
                # dial failed; one bounded wait, then look again.
                await asyncio.sleep(delay)
                continue
            should_reconnect = await self._handle_websocket_session(websocket)
            if not should_reconnect:
                return

    async def _handle_websocket_session(self, websocket: Any) -> bool:
        """Handle a single WebSocket session: log, resubscribe, and receive messages.

        Args:
            websocket: Active WebSocket connection

        Returns:
            True if the connection should be retried, False if it should not
        """
        self._websocket = websocket
        self._connection_ready.set()
        if self._subscribed_topics:
            logger.info("WebSocket reconnected successfully")
            logger.debug("Resubscribing to %d topics after reconnection", len(self._subscribed_topics))
            for topic in self._subscribed_topics:
                await self._send_subscribe_command(topic)
        else:
            logger.debug("WebSocket connection established: %s", self.url)

        try:
            await self._receive_messages(websocket)
        except ConnectionClosed:
            logger.info("WebSocket connection closed, reconnecting...")
            self._connection_ready.clear()
            return True
        return False

    async def _receive_messages(self, websocket: Any) -> None:
        """Receive messages from WebSocket and put them in message queue.

        Args:
            websocket: WebSocket connection to receive messages from

        Raises:
            ConnectionClosed: When the WebSocket connection is closed
        """
        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                    claimed = False
                    for f in self._message_filters:
                        if await f(data):
                            claimed = True
                            break
                    if not claimed:
                        await self._message_queue.put(data)
                except json.JSONDecodeError:
                    logger.error("Failed to parse message: %s", message)
                except Exception as e:
                    logger.error("Error receiving message: %s", e)
        except asyncio.CancelledError:
            logger.debug("Receive messages task cancelled")
            raise

    async def _send_message(self, message: dict[str, Any]) -> None:
        """Send a message through the WebSocket connection.

        Args:
            message: Message dictionary to send

        Raises:
            ConnectionError: If not connected
        """
        if not self._websocket:
            msg = "Not connected to WebSocket"
            raise ConnectionError(msg)

        serialized = json.dumps(message)
        # TODO: Block large payloads https://github.com/griptape-ai/griptape-nodes/issues/4124
        if len(serialized) > LARGE_PAYLOAD_WARNING_THRESHOLD:
            logger.warning(
                "Sending large WebSocket message: type=%s (%s), size=%d bytes. "
                "Large messages can saturate the send buffer and cause connected clients (e.g. the editor) to stall or disconnect.",
                message.get("type"),
                message.get("payload", {}).get("result_type"),
                len(serialized),
            )
        try:
            await self._websocket.send(serialized)
        except Exception as e:
            logger.error("Failed to send message: %s", e)

    async def _send_subscribe_command(self, topic: str) -> None:
        """Send subscribe command to server.

        Args:
            topic: Topic to subscribe to
        """
        message = {"type": "subscribe", "topic": topic, "payload": {}}
        await self._send_message(message)
        logger.debug("Sent subscribe command for topic: %s", topic)

    async def _send_unsubscribe_command(self, topic: str) -> None:
        """Send unsubscribe command to server.

        Args:
            topic: Topic to unsubscribe from
        """
        message = {"type": "unsubscribe", "topic": topic, "payload": {}}
        await self._send_message(message)
        logger.debug("Sent unsubscribe command for topic: %s", topic)
