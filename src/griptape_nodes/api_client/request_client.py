"""Request/response tracking with futures and timeouts."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

    from griptape_nodes.api_client.client import Client
    from griptape_nodes.retained_mode.events.base_events import EventRequest

logger = logging.getLogger(__name__)


@dataclass
class _PendingRequest:
    future: asyncio.Future
    tag: str
    # When True, EventResultFailure responses resolve the future with
    # the full payload dict instead of rejecting it with
    # ``Exception(error_msg)``. The default (False) preserves the
    # original "failure -> raised exception" contract that
    # ``request``/``request_to_orchestrator``/``request_batch`` already
    # rely on. The worker fan-out path opts in so the orchestrator can
    # cattrs-structure the failure dict back into a real
    # ``ResultPayloadFailure`` (preserving exception fidelity over the
    # wire).
    resolve_failures_as_payload: bool = False


class RequestClient:
    """Request/response client built on top of Client.

    Wraps a Client to provide request/response semantics on top of
    pub/sub messaging. Tracks pending requests by request_id and resolves/rejects
    futures when responses arrive. Supports timeouts for requests that don't
    receive responses.

    Registers _try_match as a message filter on the Client so that response
    messages are claimed before reaching the client's message queue. Messages
    not matched to a pending request are left for the queue's normal consumers.
    """

    def __init__(
        self,
        client: Client,
        request_topic_fn: Callable[[], str] | None = None,
        response_topic_fn: Callable[[], str] | None = None,
    ) -> None:
        """Initialize request/response client.

        Args:
            client: Client instance to use for communication
            request_topic_fn: Function to determine request topic (defaults to "request")
            response_topic_fn: Function to determine response topic (defaults to "response")
        """
        self.client = client
        self.request_topic_fn = request_topic_fn or (lambda: "request")
        self.response_topic_fn = response_topic_fn or (lambda: "response")

        # Map of request_id -> pending request where tag identifies the originating worker/caller
        self._pending_requests: dict[str, _PendingRequest] = {}
        self._lock = asyncio.Lock()

        # Track subscribed response topics
        self._subscribed_response_topics: set[str] = set()

    async def __aenter__(self) -> Self:
        """Async context manager entry: register response filter on Client."""
        self.client.add_message_filter(self._try_match)
        logger.debug("RequestClient started")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Async context manager exit: deregister response filter from Client."""
        self.client.remove_message_filter(self._try_match)
        logger.debug("RequestClient stopped")

    async def request(
        self,
        request_type: str,
        payload: dict[str, Any],
        topic: str | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        """Send a request and wait for its response.

        This method automatically:
        - Generates a request_id
        - Determines request and response topics
        - Subscribes to response topic if needed
        - Sends the request
        - Waits for and returns the response

        Args:
            request_type: Type of request to send
            payload: Request payload data
            topic: Optional per-call request topic override (defaults to request_topic_fn())
            timeout_ms: Optional timeout in milliseconds

        Returns:
            Response data from the server

        Raises:
            TimeoutError: If request times out
            Exception: If request fails
        """
        # Generate request ID and track it
        request_id = str(uuid.uuid4())
        payload["request_id"] = request_id

        response_future = await self._track_request(request_id)

        # Determine topics
        request_topic = topic or self.request_topic_fn()
        response_topic = self.response_topic_fn()

        # Subscribe to response topic if not already subscribed
        if response_topic not in self._subscribed_response_topics:
            await self.client.subscribe(response_topic)
            self._subscribed_response_topics.add(response_topic)

        # Send the request as an EventRequest
        event_payload = {
            "event_type": "EventRequest",
            "request_type": request_type,
            "request_id": request_id,
            "request": payload,
            "response_topic": response_topic,
        }

        logger.debug("Sending request %s: %s", request_id, request_type)

        try:
            await self.client.publish("EventRequest", event_payload, request_topic)

            # Wait for response with optional timeout
            if timeout_ms:
                timeout_sec = timeout_ms / 1000
                result = await asyncio.wait_for(response_future, timeout=timeout_sec)
            else:
                result = await response_future

        except TimeoutError:
            logger.error("Request %s timed out", request_id)
            await self._cancel_request(request_id)
            raise

        except Exception as e:
            logger.error("Request %s failed: %s", request_id, e)
            await self._cancel_request(request_id)
            raise
        else:
            logger.debug("Request %s completed successfully", request_id)
            return result

    async def request_to_orchestrator(
        self,
        event_request: EventRequest,
        orchestrator_request_topic: str,
        worker_response_topic: str,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        """Publish an EventRequest to the orchestrator and await its matching EventResult.

        Sets event_request.response_topic to worker_response_topic so the orchestrator
        replies on a topic this RequestClient is subscribed to. Tracks the future by
        event_request.request_id (generated if missing), publishes the serialized
        EventRequest to orchestrator_request_topic, and awaits the matching response.

        Args:
            event_request: EventRequest wrapping the RequestPayload to forward.
            orchestrator_request_topic: Topic the orchestrator listens on.
            worker_response_topic: Topic this worker listens on for the reply.
            timeout_ms: Optional timeout in milliseconds.

        Returns:
            The response payload dict (EventResultSuccess or EventResultFailure shape).

        Raises:
            TimeoutError: If no response arrives before timeout_ms.
        """
        if not event_request.request_id:
            event_request.request_id = str(uuid.uuid4())
        request_id = event_request.request_id
        event_request.response_topic = worker_response_topic

        response_future = await self._track_request(request_id)

        if worker_response_topic not in self._subscribed_response_topics:
            await self.client.subscribe(worker_response_topic)
            self._subscribed_response_topics.add(worker_response_topic)

        payload_dict = json.loads(event_request.json())

        logger.debug("Forwarding request %s to orchestrator on %s", request_id, orchestrator_request_topic)

        try:
            await self.client.publish("EventRequest", payload_dict, orchestrator_request_topic)

            if timeout_ms:
                timeout_sec = timeout_ms / 1000
                result = await asyncio.wait_for(response_future, timeout=timeout_sec)
            else:
                result = await response_future

        except TimeoutError:
            logger.error("Forwarded request %s timed out", request_id)
            await self._cancel_request(request_id)
            raise

        except Exception as e:
            logger.error("Forwarded request %s failed: %s", request_id, e)
            await self._cancel_request(request_id)
            raise
        else:
            logger.debug("Forwarded request %s completed", request_id)
            return result

    async def request_batch(
        self,
        requests: list[tuple[str, dict[str, Any]]],
        topic: str | None = None,
        timeout_ms: int | None = None,
        *,
        return_exceptions: bool = False,
    ) -> list[Any]:
        """Send a batch of requests in one frame and gather their responses.

        Each inner request is published as its own EventRequest under one
        EventRequestBatch envelope. Inner requests carry their own request_ids,
        so responses arrive as individual EventResultSuccess/Failure messages and
        are correlated back to per-call futures.

        Args:
            requests: List of (request_type, payload) pairs. Each becomes an
                inner EventRequest with its own request_id.
            topic: Optional per-call request topic override.
            timeout_ms: Optional timeout applied to the whole batch.
            return_exceptions: If True, failures and timeouts are returned in
                their slot instead of raising. Matches asyncio.gather semantics.

        Returns:
            Responses in submission order. When return_exceptions is False, a
            single failure raises immediately and pending sub-requests are
            cancelled. When True, the returned list mirrors submission order
            with exceptions in failed slots.

        Raises:
            TimeoutError: If the batch exceeds timeout_ms (only when
                return_exceptions is False).
            Exception: First failing inner request's error (only when
                return_exceptions is False).
        """
        if not requests:
            return []

        # Determine topics and ensure response subscription, just like request().
        request_topic = topic or self.request_topic_fn()
        response_topic = self.response_topic_fn()
        if response_topic not in self._subscribed_response_topics:
            await self.client.subscribe(response_topic)
            self._subscribed_response_topics.add(response_topic)

        # Pre-register futures for every inner request so _try_match can resolve
        # them as responses arrive in arbitrary order.
        inner_events: list[dict[str, Any]] = []
        futures: list[asyncio.Future] = []
        request_ids: list[str] = []
        for request_type, raw_payload in requests:
            request_id = str(uuid.uuid4())
            payload = {**raw_payload, "request_id": request_id}
            futures.append(await self._track_request(request_id))
            request_ids.append(request_id)
            inner_events.append(
                {
                    "event_type": "EventRequest",
                    "request_type": request_type,
                    "request_id": request_id,
                    "request": payload,
                    "response_topic": response_topic,
                }
            )

        batch_payload = {"event_type": "EventRequestBatch", "requests": inner_events}
        logger.debug("Sending batch of %d requests on %s", len(inner_events), request_topic)

        try:
            await self.client.publish("EventRequestBatch", batch_payload, request_topic)
            gather = asyncio.gather(*futures, return_exceptions=return_exceptions)
            if timeout_ms:
                results = await asyncio.wait_for(gather, timeout=timeout_ms / 1000)
            else:
                results = await gather
        except (TimeoutError, Exception) as e:
            logger.error("Batch request failed: %s", e)
            for request_id in request_ids:
                await self._cancel_request(request_id)
            raise
        else:
            logger.debug("Batch of %d requests completed", len(inner_events))
            return results

    async def track_request(
        self,
        request_id: str,
        tag: str = "",
        *,
        resolve_failures_as_payload: bool = False,
    ) -> asyncio.Future:
        """Register a future for an outgoing request and return it.

        Use this when the send path is handled externally (e.g. WorkerManager
        sends via forward_event_to_worker) and only the future tracking is needed.
        The future is resolved when _try_match claims the response message.

        Args:
            request_id: Unique identifier for this request
            tag: Optional tag for grouping related requests (e.g. worker_engine_id)
            resolve_failures_as_payload: When True, EventResultFailure
                responses resolve the future with the full payload dict
                instead of rejecting it with ``Exception(error_msg)``.
                The worker fan-out path uses this so the orchestrator
                can structure the failure dict back into a real
                ``ResultPayloadFailure`` (preserving exception fidelity).

        Returns:
            Future that will be resolved when the matching response arrives
        """
        return await self._track_request(request_id, tag=tag, resolve_failures_as_payload=resolve_failures_as_payload)

    async def cancel_requests_by_tag(self, tag: str) -> None:
        """Cancel all pending futures that were registered with the given tag.

        Args:
            tag: The tag value used when track_request was called (e.g. worker_engine_id)
        """
        async with self._lock:
            to_cancel = [rid for rid, entry in self._pending_requests.items() if entry.tag == tag]
            for rid in to_cancel:
                entry = self._pending_requests.pop(rid)
                if not entry.future.done():
                    entry.future.cancel()
                    logger.debug("Cancelled request %s (tag=%s)", rid, tag)

    async def _track_request(
        self,
        request_id: str,
        tag: str = "",
        *,
        resolve_failures_as_payload: bool = False,
    ) -> asyncio.Future:
        """Start tracking a request and return a future that will be resolved on response.

        Args:
            request_id: Unique identifier for this request
            tag: Optional tag for grouping (e.g. worker_engine_id)
            resolve_failures_as_payload: See ``track_request``.

        Returns:
            Future that will be resolved when response arrives

        Raises:
            ValueError: If request_id is already being tracked
        """
        async with self._lock:
            if request_id in self._pending_requests:
                msg = f"Request ID already exists: {request_id}"
                raise ValueError(msg)

            future: asyncio.Future = asyncio.Future()
            self._pending_requests[request_id] = _PendingRequest(
                future, tag, resolve_failures_as_payload=resolve_failures_as_payload
            )
            logger.debug("Tracking request: %s (tag=%s)", request_id, tag)
            return future

    def _resolve_request_unlocked(self, request_id: str, result: Any) -> None:
        """Resolve a request's future. Caller must hold self._lock.

        Args:
            request_id: Request identifier
            result: Result data to return to the requester
        """
        entry = self._pending_requests.pop(request_id, None)

        if entry is None:
            logger.warning("Received response for unknown request: %s", request_id)
            return

        if not entry.future.done():
            entry.future.set_result(result)
            logger.debug("Resolved request: %s", request_id)

    def _reject_request_unlocked(self, request_id: str, error: Exception) -> None:
        """Reject a request's future. Caller must hold self._lock.

        Args:
            request_id: Request identifier
            error: Exception to raise for the requester
        """
        entry = self._pending_requests.pop(request_id, None)

        if entry is None:
            logger.warning("Received error for unknown request: %s", request_id)
            return

        if not entry.future.done():
            entry.future.set_exception(error)
            logger.debug("Rejected request: %s with error: %s", request_id, error)

    async def _resolve_request(self, request_id: str, result: Any) -> None:
        """Mark a request as successful and resolve its future with a result.

        Args:
            request_id: Request identifier
            result: Result data to return to the requester
        """
        async with self._lock:
            self._resolve_request_unlocked(request_id, result)

    async def _reject_request(self, request_id: str, error: Exception) -> None:
        """Mark a request as failed and reject its future with an exception.

        Args:
            request_id: Request identifier
            error: Exception to raise for the requester
        """
        async with self._lock:
            self._reject_request_unlocked(request_id, error)

    async def _cancel_request(self, request_id: str) -> None:
        """Cancel a pending request and clean up its tracking.

        Args:
            request_id: Request identifier
        """
        async with self._lock:
            entry = self._pending_requests.pop(request_id, None)

            if entry is None:
                logger.debug("Request already completed or unknown: %s", request_id)
                return

            if not entry.future.done():
                entry.future.cancel()
                logger.debug("Cancelled request: %s", request_id)

    @property
    def pending_count(self) -> int:
        """Get number of currently pending requests.

        Returns:
            Count of pending requests
        """
        return len(self._pending_requests)

    @property
    def pending_request_ids(self) -> list[str]:
        """Get list of all pending request IDs.

        Returns:
            List of request_id strings
        """
        return list(self._pending_requests.keys())

    async def _try_match(self, message: dict[str, Any]) -> bool:
        """Attempt to match an incoming message to a pending request.

        Registered as a Client message filter so response messages are claimed
        before reaching the client's message queue. Messages not matched to a
        pending request return False and are left for queue consumers.

        Expects worker/event-bus response format:
          payload["event_type"] in ("EventResultSuccess", "EventResultFailure")
          payload["request_id"] = UUID (outer event request_id)
          Resolves with the full payload dict.

        Args:
            message: WebSocket message to inspect

        Returns:
            True if the message was matched and resolved/rejected, False otherwise.
        """
        payload = message.get("payload", {})

        request_id = payload.get("request_id") or ""
        async with self._lock:
            if not request_id or request_id not in self._pending_requests:
                return False

            entry = self._pending_requests[request_id]
            event_type = payload.get("event_type", "")
            if event_type == "EventResultSuccess":
                self._resolve_request_unlocked(request_id, payload)
                return True
            if event_type == "EventResultFailure":
                # Worker-tracked requests opt in to receiving the full
                # payload dict so the orchestrator can cattrs-structure
                # the failure back into a real ResultPayloadFailure
                # (preserving the exception field). All other callers
                # keep the legacy "raise Exception(error_msg)" contract.
                if entry.resolve_failures_as_payload:
                    self._resolve_request_unlocked(request_id, payload)
                    return True
                result = payload.get("result", {})
                error_msg = str(result.get("result_details") or result.get("exception") or "Unknown error")
                self._reject_request_unlocked(request_id, Exception(error_msg))
                return True

        return False
