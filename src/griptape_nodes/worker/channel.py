"""A private, authenticated byte channel between a parent process and one worker.

Transport is loopback TCP (127.0.0.1) over asyncio, not a Unix socket or named pipe: a worker is
often started through another program (`worker.command_prefix` wraps it in a shell, `cmd /c`, or
similar), which only carries the environment forward, not file descriptors or handles. Loopback
TCP needs nothing but an address and a secret, and `asyncio.start_server`/`open_connection` are
public, cross-platform asyncio, unlike named pipes on Windows.

The parent calls `listen_for_worker()`, hands the worker its address and token through
environment variables (`PendingWorker.child_environment`), and `accept()`s the worker's single
connection. The worker calls `connect_to_parent` to get its end. Both ends then share the same
`AsyncChannel` API: `request`/`request_async` for a call that waits for a reply, `notify` for a
one-way message, and a single gated dispatch point for incoming requests. `WorkerLink` runs an
`AsyncChannel` on its own I/O loop thread so a worker's synchronous code can call it without
deadlocking.

Loopback is reachable by any process on the same machine, so the worker's first frame carries a
per-connection token, compared with `hmac.compare_digest`; anything else closes that connection
without touching the real one. Frame bodies are opaque dicts; this module only moves them, in
order, as JSON, never pickle.
"""

from __future__ import annotations

import asyncio
import functools
import hmac
import json
import logging
import secrets
import struct
import threading
import uuid
from typing import TYPE_CHECKING, Any, NamedTuple

from griptape_nodes.utils.async_utils import call_function

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Mapping

logger = logging.getLogger("griptape_nodes")

CHANNEL_ADDRESS_ENV = "GTN_WORKER_CHANNEL_ADDRESS"
CHANNEL_TOKEN_ENV = "GTN_WORKER_CHANNEL_TOKEN"  # noqa: S105 - a secret's env var name, not a secret

_HANDSHAKE_TIMEOUT_S = 5.0
# The hello frame is ~80 bytes (one token). An unauthenticated peer must not be able to make the
# parent buffer an attacker-chosen amount of memory before the token is even checked.
_HELLO_MAX_LENGTH = 4096
_HEADER = struct.Struct(">I")
_FRAME_TYPES = ("request", "result", "notification")
_CLOSED_MSG = "The worker channel is closed."


class ChannelClosedError(Exception):
    """Raised for a request in flight, or a new request or notification, once the channel ends."""


class ChannelRequestError(Exception):
    """Raised by `request`/`request_async` when the peer's `on_request` handler itself failed.

    Carries the failure as data (`error_type`, `message`), not a re-raised peer exception: the
    peer's exception class may not exist, or may mean something else, on this side of the wire.
    """

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(f"{error_type}: {message}")
        self.error_type = error_type
        self.message = message


class GateDecision(NamedTuple):
    """What `AsyncChannel`'s read loop should do with one incoming request frame."""

    dispatch: dict | None
    reply: dict | None


def _encode_frame(frame: dict) -> bytes:
    raw = json.dumps(frame).encode("utf-8")
    return _HEADER.pack(len(raw)) + raw


async def _read_json_frame(reader: asyncio.StreamReader, *, max_length: int | None = None) -> dict:
    """Read one length-prefixed UTF-8 JSON object. Raises on EOF, a bad length, or malformed JSON.

    `readexactly` buffers the declared length before anything can be checked; `max_length` rejects
    an oversized declared length before reading the body at all, which the pre-auth handshake uses
    since the peer offering it has not been authenticated yet.
    """
    header = await reader.readexactly(_HEADER.size)
    (length,) = _HEADER.unpack(header)
    if max_length is not None and length > max_length:
        msg = "frame exceeds the maximum allowed size"
        raise ValueError(msg)
    raw = await reader.readexactly(length)
    frame = json.loads(raw.decode("utf-8"))
    if not isinstance(frame, dict):
        msg = "frame is not a JSON object"
        raise ValueError(msg)  # noqa: TRY004 - ValueError so every malformed-frame condition is caught uniformly
    return frame


def _is_valid_error_shape(error: object) -> bool:
    return isinstance(error, dict) and isinstance(error.get("type"), str) and isinstance(error.get("message"), str)


def _validate_result_frame(frame: dict) -> None:
    has_body = isinstance(frame.get("body"), dict)
    has_error = _is_valid_error_shape(frame.get("error"))
    if has_body == has_error:  # neither, or both: exactly one is required
        msg = "result frame must have exactly one of body or error"
        raise ValueError(msg)


def _validate_channel_frame(frame: dict) -> None:
    """Raise ValueError unless `frame` has the shape of a request/result/notification frame."""
    frame_type = frame.get("frame")
    if frame_type not in _FRAME_TYPES:
        msg = "frame has an unrecognized type"
        raise ValueError(msg)
    if frame_type != "notification" and not isinstance(frame.get("id"), str):
        msg = "request or result frame has no id"
        raise ValueError(msg)
    if frame_type == "result":
        _validate_result_frame(frame)
        return
    if not isinstance(frame.get("body"), dict):
        msg = "frame has no body"
        raise ValueError(msg)  # noqa: TRY004 - ValueError so every malformed-frame condition is caught uniformly


async def _default_on_request(_body: dict) -> dict:
    msg = "AsyncChannel.start() was never called."
    raise RuntimeError(msg)


def _default_on_notification(_body: dict) -> None:
    msg = "AsyncChannel.start() was never called."
    raise RuntimeError(msg)


def _default_on_closed() -> None:
    msg = "AsyncChannel.start() was never called."
    raise RuntimeError(msg)


class AsyncChannel:
    """Either end of a connected channel. Every method runs on the loop that owns its streams."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._pending: dict[str, asyncio.Future[dict]] = {}
        self._closed = False
        self._gate: Callable[[dict], GateDecision] | None = None
        # Real callbacks arrive through start(), which the read loop cannot outrun: start() sets
        # these before spawning it.
        self._on_request: Callable[[dict], Awaitable[dict]] = _default_on_request
        self._on_notification: Callable[[dict], None] = _default_on_notification
        self._on_closed: Callable[[], None] = _default_on_closed
        self._tasks: set[asyncio.Task] = set()

    def start(
        self,
        *,
        on_request: Callable[[dict], Awaitable[dict]],
        on_notification: Callable[[dict], None],
        on_closed: Callable[[], None],
        gate: Callable[[dict], GateDecision] | None = None,
    ) -> None:
        """Start the read loop. Call on the loop that owns this channel's streams."""
        self._on_request = on_request
        self._on_notification = on_notification
        self._on_closed = on_closed
        self._gate = gate
        self._spawn(self._read_loop())

    async def request(self, body: dict, timeout_s: float | None) -> dict:
        """Send a request and await its result.

        Raises `ChannelRequestError` if the peer's `on_request` handler failed, or
        `ChannelClosedError` once the channel has ended.
        """
        if self._closed:
            raise ChannelClosedError(_CLOSED_MSG)
        request_id = uuid.uuid4().hex
        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            self._send_frame({"frame": "request", "id": request_id, "body": body})
            await self._writer.drain()
            return await asyncio.wait_for(future, timeout_s)
        finally:
            self._pending.pop(request_id, None)

    def notify(self, body: dict) -> None:
        """Send a one-way notification. Raises `ChannelClosedError` once the channel has ended."""
        self._send_frame({"frame": "notification", "body": body})

    def close(self) -> None:
        """End this side of the channel, which the peer observes as EOF. Idempotent."""
        self._writer.close()

    def _send_frame(self, frame: dict) -> None:
        self._write_encoded(self._prepare_frame(frame))

    def _prepare_frame(self, frame: dict) -> bytes:
        """Encode a frame, raising `ChannelClosedError` if already closed. Safe from any thread.

        `WorkerLink.notify` calls this from the caller's own thread, ahead of scheduling the
        write, so `ChannelClosedError` and an unencodable body's `TypeError` reach that caller
        directly instead of only reaching asyncio's default exception logging.
        """
        if self._closed or self._writer.is_closing():
            raise ChannelClosedError(_CLOSED_MSG)
        return _encode_frame(frame)

    def _write_encoded(self, encoded: bytes) -> None:
        """Write bytes already produced by `_prepare_frame`. Must run on the owning loop."""
        self._writer.write(encoded)

    def _answer(self, request_id: str, *, body: dict | None = None, error: dict | None = None) -> None:
        """Best-effort send of one result frame.

        A channel that is already closed means the peer gave up on this request on its own
        side already; there is nothing left to report, so this does not raise.
        """
        if self._closed:
            return
        frame: dict = {"frame": "result", "id": request_id}
        if error is not None:
            frame["error"] = error
        else:
            frame["body"] = body
        try:
            self._send_frame(frame)
        except ChannelClosedError:
            # Closing raced the check above; the peer already gave up on this request too.
            logger.debug("Worker channel could not answer request %s: the channel closed first.", request_id)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _read_loop(self) -> None:
        """Applies frames in arrival order. The only path that can end this side on its own."""
        try:
            while True:
                frame = await _read_json_frame(self._reader)
                _validate_channel_frame(frame)
                self._dispatch_frame(frame)
        except asyncio.IncompleteReadError as exc:
            if exc.partial:
                # EOF mid-frame: the peer dropped, rather than closing cleanly between frames.
                logger.error("Worker channel closed: %s: %s", type(exc).__name__, exc)
            else:
                logger.debug("Worker channel closed.")
        except (ConnectionError, ValueError, UnicodeDecodeError) as exc:
            logger.error("Worker channel closed: %s: %s", type(exc).__name__, exc)
        except (
            Exception
        ) as exc:  # the gate and on_notification are caller-supplied; a failure must close, not crash this loop
            logger.error("Worker channel dispatch raised %s: %s; closing the channel.", type(exc).__name__, exc)
        finally:
            self._finish_closing()

    def _dispatch_frame(self, frame: dict) -> None:
        frame_type = frame["frame"]
        if frame_type == "notification":
            self._on_notification(frame["body"])
            return
        if frame_type == "result":
            self._resolve_pending(frame)
            return
        self._dispatch_request(frame["id"], frame["body"])

    def _dispatch_request(self, request_id: str, body: dict) -> None:
        """The single point that may call `on_request`: the gate, if set, always runs first."""
        if self._gate is None:
            self._spawn(self._run_request(request_id, body))
            return
        decision = self._gate(body)
        if decision.dispatch is None:
            if decision.reply is None:
                msg = "GateDecision denied a request without a reply to send back."
                raise RuntimeError(msg)
            if not isinstance(decision.reply, dict):
                # A bad gate reply fails only this one request, same as a bad on_request result:
                # the gate itself is not at fault for the shape of the body it was handed to send.
                logger.error("Worker channel gate reply was %s, not a dict.", type(decision.reply).__name__)
                self._answer(
                    request_id,
                    error={
                        "type": "TypeError",
                        "message": f"gate reply is {type(decision.reply).__name__}, not a dict",
                    },
                )
                return
            self._answer(request_id, body=decision.reply)
            return
        self._spawn(self._run_request(request_id, decision.dispatch))

    async def _run_request(self, request_id: str, body: dict) -> None:
        try:
            result_body = await call_function(self._on_request, body)
            if not isinstance(result_body, dict):
                msg = f"on_request returned {type(result_body).__name__}, not a dict"
                raise TypeError(msg)  # noqa: TRY301 - caught immediately below, same as on_request raising directly
            self._answer(request_id, body=result_body)
        except (
            Exception
        ) as exc:  # on_request is caller-supplied, and its result may not be a dict or encode either; answer regardless
            logger.error("Worker channel could not answer a request: %s: %s", type(exc).__name__, exc)
            self._answer(request_id, error={"type": type(exc).__name__, "message": str(exc)})

    def _resolve_pending(self, frame: dict) -> None:
        future = self._pending.pop(frame["id"], None)
        if future is None or future.done():
            return
        error = frame.get("error")
        if error is not None:
            future.set_exception(ChannelRequestError(error["type"], error["message"]))
            return
        future.set_result(frame["body"])

    def _finish_closing(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._writer.close()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ChannelClosedError(_CLOSED_MSG))
        self._pending.clear()
        self._on_closed()


class PendingWorker:
    """Parent side: a one-shot loopback server waiting for one worker with the right token."""

    def __init__(self, server: asyncio.Server, token: str, connected: asyncio.Future[AsyncChannel]) -> None:
        self._server = server
        self._token = token
        self._connected = connected

    @property
    def child_environment(self) -> dict[str, str]:
        """Environment variables that let `connect_to_parent` reach this server."""
        host, port = self._server.sockets[0].getsockname()[:2]
        return {CHANNEL_ADDRESS_ENV: f"{host}:{port}", CHANNEL_TOKEN_ENV: self._token}

    async def accept(self, timeout_s: float) -> AsyncChannel:
        """Block for the one worker connection, then stop listening regardless of the outcome."""
        try:
            return await asyncio.wait_for(asyncio.shield(self._connected), timeout_s)
        finally:
            self.close()

    def close(self) -> None:
        """Stop listening. Idempotent; safe even if `accept()` was never awaited."""
        self._server.close()
        if not self._connected.done():
            self._connected.cancel()


async def listen_for_worker() -> PendingWorker:
    """Parent side: bind a loopback port and wait for one worker that offers the right token."""
    token = secrets.token_hex(32)
    connected: asyncio.Future[AsyncChannel] = asyncio.get_running_loop().create_future()

    async def _on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            hello = await asyncio.wait_for(_read_json_frame(reader, max_length=_HELLO_MAX_LENGTH), _HANDSHAKE_TIMEOUT_S)
        except (TimeoutError, asyncio.IncompleteReadError, ConnectionError, ValueError, UnicodeDecodeError) as exc:
            logger.error("Worker channel handshake rejected a connection: %s: %s", type(exc).__name__, exc)
            writer.close()
            return
        offered = hello.get("token")
        if connected.done():
            logger.error("Worker channel handshake rejected a connection: a worker already connected.")
            writer.close()
            return
        if not isinstance(offered, str) or not hmac.compare_digest(offered, token):
            logger.error("Worker channel handshake rejected a connection: wrong token.")
            writer.close()
            return
        connected.set_result(AsyncChannel(reader, writer))

    server = await asyncio.start_server(_on_connect, host="127.0.0.1", port=0)
    return PendingWorker(server, token, connected)


async def connect_to_parent(environ: Mapping[str, str]) -> AsyncChannel:
    """Worker side: connect to the parent named by `environ` and send the handshake token."""
    host, port = environ[CHANNEL_ADDRESS_ENV].rsplit(":", 1)
    reader, writer = await asyncio.open_connection(host, int(port))
    writer.write(_encode_frame({"token": environ[CHANNEL_TOKEN_ENV]}))
    return AsyncChannel(reader, writer)


class WorkerLink:
    """Worker side: the channel on its own I/O loop thread, with a blocking API for sync callers."""

    def __init__(self, environ: Mapping[str, str]) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="gtn-worker-io", daemon=True)
        self._thread.start()
        self.channel: AsyncChannel = asyncio.run_coroutine_threadsafe(connect_to_parent(environ), self._loop).result()

    def start(
        self,
        *,
        on_request: Callable[[dict], Awaitable[dict]],
        on_notification: Callable[[dict], None],
        on_closed: Callable[[], None],
        gate: Callable[[dict], GateDecision] | None = None,
    ) -> None:
        """Start the channel's read loop on the I/O thread. Callbacks run on that thread."""
        bound_start = functools.partial(
            self.channel.start, on_request=on_request, on_notification=on_notification, on_closed=on_closed, gate=gate
        )
        self._loop.call_soon_threadsafe(bound_start)

    def request(self, body: dict, timeout_s: float | None) -> dict:
        """Block the calling thread for a reply.

        Raises `RuntimeError` if called from the channel's own I/O thread, since that thread is
        what would deliver the result; calling from there would deadlock.
        """
        if threading.current_thread() is self._thread:
            msg = "WorkerLink.request() cannot be called from the channel's own I/O thread: it would deadlock."
            raise RuntimeError(msg)
        return asyncio.run_coroutine_threadsafe(self.channel.request(body, timeout_s), self._loop).result()

    async def request_async(self, body: dict, timeout_s: float | None) -> dict:
        """Await a reply from another event loop without blocking it."""
        future = asyncio.run_coroutine_threadsafe(self.channel.request(body, timeout_s), self._loop)
        return await asyncio.wrap_future(future)

    def notify(self, body: dict) -> None:
        """Send a one-way notification. Thread-safe; never blocks, even from the I/O thread itself.

        Raises `ChannelClosedError` once the channel has ended, or `TypeError` if `body` cannot
        be encoded as JSON: both checked on the calling thread, before the write is scheduled,
        so the caller sees them directly rather than only asyncio's default exception logging.
        """
        encoded = self.channel._prepare_frame({"frame": "notification", "body": body})
        self._loop.call_soon_threadsafe(self.channel._write_encoded, encoded)

    def close(self) -> None:
        """End this side of the channel, which the peer observes as EOF. Idempotent."""
        self._loop.call_soon_threadsafe(self.channel.close)
