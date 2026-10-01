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

# websockets.asyncio.client is the only module that defines process_exception on every websockets
# version this package supports. 17.x moved it to websockets.client and re-imports it here, which
# pyright reports as a private import.
from websockets.asyncio.client import connect, process_exception  # pyright: ignore[reportPrivateImportUsage]
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

# How long connect() waits for the first connection before giving up.
CONNECT_TIMEOUT_SECONDS = 10.0


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
    ):
        """Initialize Nodes API client.

        Args:
            api_key: API key for authentication (defaults to GT_CLOUD_API_KEY from SecretsManager)
            url: WebSocket URL to connect to (defaults to Nodes API endpoint)
        """
        self.url = url if url is not None else get_default_websocket_url()

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
        # The most recent reason a connection attempt failed, so connect() can report it.
        self._last_connection_error: BaseException | None = None

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

        Returns as soon as the connection is established, or raises as soon as it fails
        with an error that retrying will not fix, instead of always waiting out the timeout.

        Raises:
            ConnectionError: If connection fails, naming the reason
        """
        # Start connection manager task
        self._receiving_task = asyncio.create_task(self._manage_connection())
        ready_task = asyncio.create_task(self._connection_ready.wait())

        await asyncio.wait(
            {ready_task, self._receiving_task},
            timeout=CONNECT_TIMEOUT_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if not ready_task.done():
            ready_task.cancel()
            # Stop the connection manager so it does not keep redialing after connect() gave up.
            await self.disconnect()
            msg = f"Failed to connect to {self.url}: {self._describe_connection_failure()}"
            logger.error(msg)
            raise ConnectionError(msg) from self._last_connection_error

        logger.debug("WebSocket client connected")

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
            async for websocket in connect(
                self.url,
                additional_headers=self.headers,
                process_exception=self._process_connection_exception,
            ):
                should_reconnect = await self._handle_websocket_session(websocket)
                if not should_reconnect:
                    break
        except InvalidStatus as e:
            if e.response.status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
                logger.error(
                    "%s rejected the connection with HTTP %d. The credentials sent were missing or invalid.",
                    self.url,
                    e.response.status_code,
                )
            else:
                logger.error(
                    "%s rejected the WebSocket connection: HTTP %d.",
                    self.url,
                    e.response.status_code,
                )
        except InvalidURI as e:
            logger.error("Invalid WebSocket URL: %s.", e)
        except ssl.SSLError as e:
            logger.error(
                "SSL error while connecting to %s: %s. "
                "This may indicate a certificate verification failure. "
                "Check that your system's CA certificates are up to date.",
                self.url,
                e,
            )
        except OSError as e:
            logger.error(
                "Network error while connecting to %s: %s. Check that the server is reachable.",
                self.url,
                e,
            )
        except asyncio.CancelledError:
            logger.debug("Connection manager task cancelled")

    def _process_connection_exception(self, exc: Exception) -> Exception | None:
        """Record why a connection attempt failed, and decide whether to retry it.

        Keeps websockets' default retry rules, except that an SSL error is fatal: a failed
        certificate check fails the same way on every attempt. websockets would otherwise retry
        it forever, because ``ssl.SSLError`` is an ``OSError``.

        Returns:
            None to retry, or the exception to stop reconnecting and raise it.
        """
        self._last_connection_error = exc
        if isinstance(exc, ssl.SSLError):
            return exc
        return process_exception(exc)

    def _describe_connection_failure(self) -> str:
        """Describe the most recent connection failure for an error message."""
        if self._last_connection_error is None:
            return f"no response within {CONNECT_TIMEOUT_SECONDS:g} seconds"
        return str(self._last_connection_error) or type(self._last_connection_error).__name__

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
