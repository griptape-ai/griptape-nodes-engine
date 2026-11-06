"""A private, authenticated byte channel between a parent process and one worker.

The parent creates a `ChannelListener`, hands the worker its address and authkey through
environment variables (`child_environment`), and `accept()`s the worker's single connection.
The worker calls `connect_from_environment` to get its end. Both ends then share the same
`Channel` API: `request`/`request_async` for a call that waits for a reply, `notify` for a
one-way message, and a single gated dispatch point for incoming requests.

Frame bodies are opaque dicts; this module only moves them, in order, and never pickles them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import shutil
import sys
import tempfile
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client, Connection, Listener
from pathlib import Path
from queue import Queue
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

logger = logging.getLogger("griptape_nodes")

CHANNEL_ADDRESS_ENV = "GTN_WORKER_CHANNEL_ADDRESS"
CHANNEL_AUTHKEY_ENV = "GTN_WORKER_CHANNEL_AUTHKEY"

_WINDOWS = sys.platform.startswith("win")
_FAMILY = "AF_PIPE" if _WINDOWS else "AF_UNIX"
_FRAME_TYPES = ("request", "result", "notification")
# How long to give Listener.close() to unblock a thread parked in accept() before falling back
# to connecting a throwaway client to force it to return.
_UNBLOCK_GRACE_S = 0.5
_CLOSED_MSG = "The worker channel is closed."


class ChannelClosedError(Exception):
    """Raised for a request in flight, or a new request or notification, once the channel ends."""


class GateDecision(NamedTuple):
    """What `Channel`'s dispatch thread should do with one incoming request frame."""

    dispatch: dict | None
    reply: dict | None


class ChannelListener:
    """Parent side of a channel: binds one address and accepts exactly one worker connection."""

    def __init__(self) -> None:
        self._authkey = secrets.token_bytes(32)
        self._directory: str | None = None
        if _WINDOWS:
            self._address = rf"\\.\pipe\gtn-worker-{uuid.uuid4().hex}"
        else:
            # /tmp, not tempfile.gettempdir(): macOS's per-user temp dir is long enough that
            # appending a socket filename blows the ~104 byte AF_UNIX path limit.
            self._directory = tempfile.mkdtemp(dir="/tmp", prefix="gtn-")
            Path(self._directory).chmod(0o700)
            self._address = str(Path(self._directory) / "s")
        self._listener = Listener(self._address, family=_FAMILY, authkey=self._authkey)

    @property
    def address(self) -> str:
        """The address a worker connects to (a socket path, or a named pipe on Windows)."""
        return self._address

    def child_environment(self) -> dict[str, str]:
        """Environment variables that let `connect_from_environment` reach this listener."""
        return {
            CHANNEL_ADDRESS_ENV: self._address,
            CHANNEL_AUTHKEY_ENV: self._authkey.hex(),
        }

    def accept(self, timeout_s: float) -> Channel:
        """Block for one worker connection, then stop listening regardless of the outcome."""
        connection_holder: list[Connection] = []
        done = threading.Event()

        def _accept() -> None:
            while True:
                try:
                    connection = self._listener.accept()
                except AuthenticationError:
                    logger.error("Worker channel at %s rejected a connection: wrong authkey.", self._address)
                    continue
                except OSError:
                    done.set()
                    return
                connection_holder.append(connection)
                done.set()
                return

        thread = threading.Thread(target=_accept, name="gtn-channel-accept", daemon=True)
        thread.start()
        accepted_in_time = done.wait(timeout_s)
        if not accepted_in_time:
            self._force_unblock_accept(done)
        thread.join()
        self._stop_listening()

        if accepted_in_time and connection_holder:
            return Channel(connection_holder[0])
        if connection_holder:
            # A connection landed only after we gave up waiting for it: it is our own
            # unblocking client below, or a worker that arrived too late. Either way, reject it.
            connection_holder[0].close()
        msg = f"No worker connected to channel {self._address} within {timeout_s}s."
        raise TimeoutError(msg)

    def close(self) -> None:
        """Stop listening and, on POSIX, remove the private directory. Safe to call more than once."""
        self._stop_listening()
        if self._directory is not None:
            shutil.rmtree(self._directory, ignore_errors=True)

    def _force_unblock_accept(self, done: threading.Event) -> None:
        self._stop_listening()
        if done.wait(_UNBLOCK_GRACE_S):
            return
        # Closing the listener should already have raised inside accept(). Where it does not
        # (observed as a risk on Windows named pipes), connect to unblock it ourselves.
        with contextlib.suppress(OSError, AuthenticationError):
            throwaway = Client(self._address, family=_FAMILY, authkey=self._authkey)
            throwaway.close()
        done.wait()

    def _stop_listening(self) -> None:
        with contextlib.suppress(OSError):
            self._listener.close()


def connect_from_environment(environ: Mapping[str, str]) -> Channel:
    """Worker side: connect to the `ChannelListener` named by `environ`."""
    address = environ[CHANNEL_ADDRESS_ENV]
    authkey = bytes.fromhex(environ[CHANNEL_AUTHKEY_ENV])
    connection = Client(address, family=_FAMILY, authkey=authkey)
    return Channel(connection)


class _PendingSend(NamedTuple):
    """A request frame that has been sent and is waiting on its result."""

    request_id: str
    future: Future[dict]


class Channel:
    """Either end of a connected channel.

    `send_bytes`/`recv_bytes` only: this class never calls `Connection.send`/`.recv`, which
    pickle their argument, so unpickling an attacker-controlled frame would run arbitrary code.
    """

    def __init__(self, connection: Connection) -> None:
        self._connection = connection
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending_requests: dict[str, Future[dict]] = {}
        self._frame_queue: Queue[dict | None] = Queue()
        self._closed = threading.Event()
        self._started = False
        self._gate: Callable[[dict], GateDecision] | None = None
        # Real callbacks arrive through start(), which the reader and dispatch threads cannot
        # outrun: both are spawned by start() itself, after these are assigned for real.
        self._on_request: Callable[[dict], dict] = lambda _body: {}
        self._on_notification: Callable[[dict], None] = lambda _body: None
        self._on_closed: Callable[[], None] = lambda: None
        self._executor = ThreadPoolExecutor(thread_name_prefix="gtn-channel-request")
        self._reader_thread: threading.Thread | None = None
        self._dispatch_thread: threading.Thread | None = None

    def start(
        self,
        *,
        on_request: Callable[[dict], dict],
        on_notification: Callable[[dict], None],
        on_closed: Callable[[], None],
    ) -> None:
        """Start the reader and ordered-dispatch threads. Call at most once."""
        if self._started:
            msg = "Channel.start() was already called."
            raise RuntimeError(msg)
        self._started = True
        self._on_request = on_request
        self._on_notification = on_notification
        self._on_closed = on_closed
        self._reader_thread = threading.Thread(target=self._read_loop, name="gtn-channel-reader", daemon=True)
        self._dispatch_thread = threading.Thread(target=self._dispatch_loop, name="gtn-channel-dispatch", daemon=True)
        self._reader_thread.start()
        self._dispatch_thread.start()

    def set_request_gate(self, gate: Callable[[dict], GateDecision]) -> None:
        """Install the gate `_dispatch_request` consults before ever calling `on_request`. Call before `start()`."""
        if self._started:
            msg = "Channel.set_request_gate() must be called before start()."
            raise RuntimeError(msg)
        self._gate = gate

    def request(self, body: dict, timeout_s: float | None) -> dict:
        """Send a request and block the calling thread for its result.

        Raises `RuntimeError` if called from this channel's own reader or dispatch thread,
        since those threads are what deliver the result; calling from there would deadlock.
        """
        self._assert_not_on_channel_thread()
        pending = self._send_request_frame(body)
        try:
            return pending.future.result(timeout=timeout_s)
        except TimeoutError:
            self._forget_pending(pending.request_id)
            raise

    async def request_async(self, body: dict, timeout_s: float | None) -> dict:
        """Send a request and await its result without blocking the event loop."""
        self._assert_not_on_channel_thread()
        pending = self._send_request_frame(body)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(pending.future), timeout=timeout_s)
        except TimeoutError:
            self._forget_pending(pending.request_id)
            raise

    def notify(self, body: dict) -> None:
        """Send a one-way notification. Raises `ChannelClosedError` once the channel has ended."""
        self._send_frame({"frame": "notification", "body": body})

    def close(self) -> None:
        """End this side of the channel, which the peer observes as EOF. Idempotent."""
        self._closed.set()
        with contextlib.suppress(OSError):
            self._connection.close()

    def _assert_not_on_channel_thread(self) -> None:
        current = threading.current_thread()
        if current is self._reader_thread or current is self._dispatch_thread:
            msg = "Channel.request()/request_async() cannot be called from the channel's reader or dispatch thread."
            raise RuntimeError(msg)

    def _send_request_frame(self, body: dict) -> _PendingSend:
        future: Future[dict] = Future()
        request_id = uuid.uuid4().hex
        with self._pending_lock:
            if self._closed.is_set():
                raise ChannelClosedError(_CLOSED_MSG)
            self._pending_requests[request_id] = future
        try:
            self._send_frame({"frame": "request", "id": request_id, "body": body})
        except ChannelClosedError:
            self._forget_pending(request_id)
            raise
        return _PendingSend(request_id, future)

    def _forget_pending(self, request_id: str) -> Future[dict] | None:
        with self._pending_lock:
            return self._pending_requests.pop(request_id, None)

    def _send_frame(self, frame: dict) -> None:
        raw = json.dumps(frame).encode("utf-8")
        with self._write_lock:
            try:
                self._connection.send_bytes(raw)
            except OSError as exc:
                raise ChannelClosedError(_CLOSED_MSG) from exc

    def _send_result(self, request_id: str, body: dict) -> None:
        self._send_frame({"frame": "result", "id": request_id, "body": body})

    def _read_loop(self) -> None:
        """Only reads frames onto the queue. Never runs a callback or waits on anything else."""
        while True:
            try:
                raw = self._connection.recv_bytes()
            except (EOFError, OSError):
                self._frame_queue.put(None)
                return
            frame = self._parse_frame(raw)
            if frame is None:
                with contextlib.suppress(OSError):
                    self._connection.close()
                self._frame_queue.put(None)
                return
            self._frame_queue.put(frame)

    def _parse_frame(self, raw: bytes) -> dict | None:
        try:
            frame = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.error("Worker channel received a frame that was not valid UTF-8 JSON; closing the channel.")
            return None
        if not isinstance(frame, dict) or frame.get("frame") not in _FRAME_TYPES:
            logger.error("Worker channel received a frame with an unrecognized shape; closing the channel.")
            return None
        if not isinstance(frame.get("body"), dict):
            logger.error("Worker channel received a frame with no body; closing the channel.")
            return None
        if frame["frame"] != "notification" and not isinstance(frame.get("id"), str):
            logger.error("Worker channel received a request or result frame with no id; closing the channel.")
            return None
        return frame

    def _dispatch_loop(self) -> None:
        """The single thread that applies frames in arrival order: notify inline, resolve inline, gate requests."""
        while True:
            frame = self._frame_queue.get()
            if frame is None:
                self._finish_closing()
                return
            try:
                self._dispatch_frame(frame)
            # gate/on_notification are caller-supplied; a failure there must close the channel, not kill this thread.
            except Exception as exc:
                logger.error("Worker channel dispatch raised %s: %s; closing the channel.", type(exc).__name__, exc)
                with contextlib.suppress(OSError):
                    self._connection.close()
                self._finish_closing()
                return

    def _dispatch_frame(self, frame: dict) -> None:
        frame_type = frame["frame"]
        if frame_type == "notification":
            self._on_notification(frame["body"])
            return
        if frame_type == "result":
            self._resolve_pending(frame["id"], frame["body"])
            return
        self._dispatch_request(frame)

    def _dispatch_request(self, frame: dict) -> None:
        """The single point that may call `on_request`: a gate, if set, runs before it on every frame."""
        request_id = frame["id"]
        body = frame["body"]
        if self._gate is None:
            self._executor.submit(self._run_request, request_id, body)
            return
        decision = self._gate(body)
        if decision.dispatch is None:
            if decision.reply is None:
                msg = "GateDecision denied a request without a reply to send back."
                raise RuntimeError(msg)
            with contextlib.suppress(ChannelClosedError):
                self._send_result(request_id, decision.reply)
            return
        self._executor.submit(self._run_request, request_id, decision.dispatch)

    def _run_request(self, request_id: str, body: dict) -> None:
        try:
            result_body = self._on_request(body)
        except Exception as exc:  # on_request is caller-supplied; its failures still need a result frame sent back
            logger.error("Worker channel request handler raised %s: %s", type(exc).__name__, exc)
            result_body = {"error": str(exc)}
        with contextlib.suppress(ChannelClosedError):
            self._send_result(request_id, result_body)

    def _resolve_pending(self, request_id: str, body: dict) -> None:
        future = self._forget_pending(request_id)
        if future is not None and not future.done():
            future.set_result(body)

    def _finish_closing(self) -> None:
        with contextlib.suppress(OSError):
            self._connection.close()
        with self._pending_lock:
            self._closed.set()
            pending_futures = list(self._pending_requests.values())
            self._pending_requests.clear()
        for future in pending_futures:
            if not future.done():
                future.set_exception(ChannelClosedError(_CLOSED_MSG))
        self._executor.shutdown(wait=False)
        self._on_closed()
