"""Tests for the private parent<->worker channel transport in `griptape_nodes.worker.channel`.

Most tests connect two `Channel`s in this process over a real `Listener`/`Client` pair: the
transport is a real socket (or named pipe on Windows), only the two processes are faked away.
One test spawns a real child process to prove the environment-variable handshake end to end.

No sleeps for synchronization anywhere: every wait is on an `Event`, a `Future`, or a frame that
has to have already been processed because of the channel's own ordering guarantee.
"""

from __future__ import annotations

import ast
import json
import multiprocessing
import stat
import sys
import threading
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import pytest

from griptape_nodes.worker import channel as channel_module
from griptape_nodes.worker.channel import (
    Channel,
    ChannelClosedError,
    ChannelListener,
    GateDecision,
    connect_from_environment,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from multiprocessing.queues import Queue as ProcessQueue

# Generous enough that a loaded CI box does not flake; every wait below is also event-driven,
# so these are ceilings, not the expected wait time.
REQUEST_TIMEOUT_S = 5.0
ACCEPT_TIMEOUT_S = 5.0
SHORT_TIMEOUT_S = 0.3
_PRIVATE_DIR_MODE = 0o700


class _ConnectedPair(NamedTuple):
    parent: Channel
    child: Channel


class _Recorder:
    """Captures one `Channel`'s callbacks in call order, for assertions without sleeping."""

    def __init__(self, on_request: Callable[[dict], dict] | None = None) -> None:
        self._on_request_impl = on_request if on_request is not None else dict
        self._lock = threading.Lock()
        self.requests_seen: list[dict] = []
        self.notifications: list[dict] = []
        self.closed = threading.Event()
        self.closed_count = 0

    def on_request(self, body: dict) -> dict:
        with self._lock:
            self.requests_seen.append(body)
        return self._on_request_impl(body)

    def on_notification(self, body: dict) -> None:
        with self._lock:
            self.notifications.append(body)

    def on_closed(self) -> None:
        with self._lock:
            self.closed_count += 1
        self.closed.set()


def _connected_pair() -> _ConnectedPair:
    """Connect two Channels over a real Listener/Client pair. Neither is started yet."""
    listener = ChannelListener()
    accepted: list[Channel] = []

    def _accept() -> None:
        accepted.append(listener.accept(ACCEPT_TIMEOUT_S))

    thread = threading.Thread(target=_accept)
    thread.start()
    child = connect_from_environment(listener.child_environment())
    thread.join(ACCEPT_TIMEOUT_S)
    listener.close()
    return _ConnectedPair(accepted[0], child)


def _child_round_trip(environ: dict[str, str], result_queue: ProcessQueue) -> None:
    """Run in a real child process: connect, send one request, report what came back."""
    worker_channel = connect_from_environment(environ)
    worker_channel.start(on_request=lambda _body: {}, on_notification=lambda _body: None, on_closed=lambda: None)
    try:
        result_queue.put(worker_channel.request({"ping": "from-child"}, timeout_s=ACCEPT_TIMEOUT_S))
    finally:
        worker_channel.close()


def test_round_trip_request_result_both_directions() -> None:
    """Either side can call `request()` on the other and get back its `on_request` result."""
    pair = _connected_pair()
    rec_parent = _Recorder(on_request=lambda body: {"sum": body["x"] + body["y"]})
    rec_child = _Recorder(on_request=lambda body: {"echo": body})
    pair.parent.start(
        on_request=rec_parent.on_request, on_notification=rec_parent.on_notification, on_closed=rec_parent.on_closed
    )
    pair.child.start(
        on_request=rec_child.on_request, on_notification=rec_child.on_notification, on_closed=rec_child.on_closed
    )
    try:
        assert pair.parent.request({"x": 1, "y": 2}, timeout_s=REQUEST_TIMEOUT_S) == {"sum": 3}
        assert pair.child.request({"ping": True}, timeout_s=REQUEST_TIMEOUT_S) == {"echo": {"ping": True}}
    finally:
        pair.parent.close()
        pair.child.close()


def test_notifications_delivered_in_order_before_a_later_result() -> None:
    """A result is never observed before notifications the peer sent ahead of it."""
    pair = _connected_pair()
    rec_parent = _Recorder()
    notify_count = 5

    def parent_on_request(_body: dict) -> dict:
        for seq in range(notify_count):
            pair.parent.notify({"seq": seq})
        return {"done": True}

    pair.parent.start(
        on_request=parent_on_request, on_notification=rec_parent.on_notification, on_closed=rec_parent.on_closed
    )
    rec_child = _Recorder()
    pair.child.start(
        on_request=rec_child.on_request, on_notification=rec_child.on_notification, on_closed=rec_child.on_closed
    )
    try:
        result = pair.child.request({}, timeout_s=REQUEST_TIMEOUT_S)
        assert result == {"done": True}
        # By the time the result above is observed, the dispatch thread must already have
        # processed every notification that was sent ahead of it, in order.
        assert rec_child.notifications == [{"seq": seq} for seq in range(notify_count)]
    finally:
        pair.parent.close()
        pair.child.close()


def test_gate_allows_and_runs_before_on_request() -> None:
    """A gate that allows a request runs first, and on_request still sees the dispatched body."""
    pair = _connected_pair()
    call_order: list[str] = []

    def gate(body: dict) -> GateDecision:
        call_order.append("gate")
        return GateDecision(dispatch=body, reply=None)

    def parent_on_request(body: dict) -> dict:
        call_order.append("on_request")
        return {"got": body}

    pair.parent.set_request_gate(gate)
    pair.parent.start(on_request=parent_on_request, on_notification=lambda _body: None, on_closed=lambda: None)
    rec_child = _Recorder()
    pair.child.start(
        on_request=rec_child.on_request, on_notification=rec_child.on_notification, on_closed=rec_child.on_closed
    )
    try:
        result = pair.child.request({"x": 1}, timeout_s=REQUEST_TIMEOUT_S)
        assert result == {"got": {"x": 1}}
        assert call_order == ["gate", "on_request"]
    finally:
        pair.parent.close()
        pair.child.close()


def test_gate_denies_without_calling_on_request() -> None:
    """A gate that denies a request sends its reply itself; on_request is never called."""
    pair = _connected_pair()
    on_request_calls: list[dict] = []

    def gate(_body: dict) -> GateDecision:
        return GateDecision(dispatch=None, reply={"denied": True})

    def parent_on_request(body: dict) -> dict:
        on_request_calls.append(body)
        return {}

    pair.parent.set_request_gate(gate)
    pair.parent.start(on_request=parent_on_request, on_notification=lambda _body: None, on_closed=lambda: None)
    rec_child = _Recorder()
    pair.child.start(
        on_request=rec_child.on_request, on_notification=rec_child.on_notification, on_closed=rec_child.on_closed
    )
    try:
        result = pair.child.request({"x": 1}, timeout_s=REQUEST_TIMEOUT_S)
        assert result == {"denied": True}
        assert on_request_calls == []
    finally:
        pair.parent.close()
        pair.child.close()


def test_wrong_authkey_is_rejected_without_crashing_the_listener() -> None:
    """A bad authkey attempt is rejected; the listener keeps waiting for a valid connection."""
    listener = ChannelListener()
    accepted: list[Channel] = []

    def _accept() -> None:
        accepted.append(listener.accept(ACCEPT_TIMEOUT_S))

    thread = threading.Thread(target=_accept)
    thread.start()
    channel_from_valid_client: Channel | None = None
    try:
        with pytest.raises(AuthenticationError):
            Client(listener.address, family=channel_module._FAMILY, authkey=b"\x00" * 32)
        channel_from_valid_client = connect_from_environment(listener.child_environment())
        thread.join(ACCEPT_TIMEOUT_S)
        assert len(accepted) == 1
    finally:
        if accepted:
            accepted[0].close()
        if channel_from_valid_client is not None:
            channel_from_valid_client.close()


def test_accept_timeout_raises_and_stops_listening() -> None:
    """accept() raises TimeoutError with no connection, and the listener stops listening."""
    listener = ChannelListener()
    try:
        with pytest.raises(TimeoutError):
            listener.accept(SHORT_TIMEOUT_S)
        # The exact failure (FileNotFoundError, ConnectionRefusedError, ...) is platform-specific;
        # what matters here is that nothing can connect any more.
        with pytest.raises(OSError):  # noqa: PT011
            connect_from_environment(listener.child_environment())
    finally:
        listener.close()


def test_close_from_the_peer_fails_pending_requests_and_calls_on_closed_once() -> None:
    """When the peer closes, a pending request fails and on_closed fires exactly once."""
    pair = _connected_pair()
    rec_parent = _Recorder()
    request_received = threading.Event()
    release_child_handler = threading.Event()

    def child_on_request(_body: dict) -> dict:
        request_received.set()
        release_child_handler.wait(REQUEST_TIMEOUT_S)
        return {}

    pair.parent.start(
        on_request=rec_parent.on_request, on_notification=rec_parent.on_notification, on_closed=rec_parent.on_closed
    )
    pair.child.start(on_request=child_on_request, on_notification=lambda _body: None, on_closed=lambda: None)

    result_holder: dict[str, Any] = {}
    done = threading.Event()

    def do_request() -> None:
        try:
            result_holder["result"] = pair.parent.request({"x": 1}, timeout_s=REQUEST_TIMEOUT_S)
        except ChannelClosedError as exc:
            result_holder["error"] = exc
        finally:
            done.set()

    requester = threading.Thread(target=do_request)
    requester.start()
    assert request_received.wait(REQUEST_TIMEOUT_S)
    # The request has definitely reached the child and is pending on the parent's side now.
    pair.child.close()
    assert done.wait(REQUEST_TIMEOUT_S)
    requester.join(REQUEST_TIMEOUT_S)

    assert isinstance(result_holder.get("error"), ChannelClosedError)
    assert rec_parent.closed.wait(REQUEST_TIMEOUT_S)
    assert rec_parent.closed_count == 1
    pair.parent.close()
    # Let the child's still-running handler thread return instead of waiting out its timeout.
    release_child_handler.set()


def test_close_calls_on_closed_on_the_closing_side_too() -> None:
    """close() ends the channel on both the closing side and the peer, each exactly once."""
    pair = _connected_pair()
    rec_parent = _Recorder()
    rec_child = _Recorder()
    pair.parent.start(
        on_request=rec_parent.on_request, on_notification=rec_parent.on_notification, on_closed=rec_parent.on_closed
    )
    pair.child.start(
        on_request=rec_child.on_request, on_notification=rec_child.on_notification, on_closed=rec_child.on_closed
    )

    pair.parent.close()

    assert rec_parent.closed.wait(REQUEST_TIMEOUT_S)
    assert rec_parent.closed_count == 1
    assert rec_child.closed.wait(REQUEST_TIMEOUT_S)
    assert rec_child.closed_count == 1


def test_request_from_the_dispatch_thread_raises_runtime_error() -> None:
    """Calling request() from inside on_notification -- the dispatch thread -- raises RuntimeError."""
    pair = _connected_pair()
    captured: list[BaseException] = []

    def parent_on_notification(_body: dict) -> None:
        try:
            pair.parent.request({}, timeout_s=SHORT_TIMEOUT_S)
        except RuntimeError as exc:
            captured.append(exc)

    pair.parent.start(on_request=lambda _body: {}, on_notification=parent_on_notification, on_closed=lambda: None)
    pair.child.start(on_request=lambda _body: {}, on_notification=lambda _body: None, on_closed=lambda: None)
    try:
        pair.child.notify({"hello": True})
        # The notification and this request cross the wire in that order, and the parent's one
        # dispatch thread handles frames in arrival order, so this result is not observed until
        # the notification above -- and the nested request() attempt inside it -- has returned.
        pair.child.request({}, timeout_s=REQUEST_TIMEOUT_S)
        assert len(captured) == 1
        assert isinstance(captured[0], RuntimeError)
    finally:
        pair.parent.close()
        pair.child.close()


def test_request_from_the_reader_thread_raises_runtime_error() -> None:
    """Pins the reader-thread half of the guard directly: no callback ever runs on that thread."""
    pair = _connected_pair()
    pair.parent.start(on_request=lambda _body: {}, on_notification=lambda _body: None, on_closed=lambda: None)
    pair.child.start(on_request=lambda _body: {}, on_notification=lambda _body: None, on_closed=lambda: None)
    try:
        # `_assert_not_on_channel_thread` treats the reader and dispatch threads the same way;
        # the dispatch-thread branch is covered live above, so this pins the reader-thread one
        # by pointing the check at the thread actually running this test.
        pair.parent._reader_thread = threading.current_thread()
        with pytest.raises(RuntimeError):
            pair.parent.request({}, timeout_s=SHORT_TIMEOUT_S)
    finally:
        pair.parent.close()
        pair.child.close()


def test_nested_request_from_an_on_request_thread() -> None:
    """An on_request handler may itself call request() back to the peer without deadlocking."""
    pair = _connected_pair()

    def parent_on_request(body: dict) -> dict:
        nested_result = pair.parent.request({"nested": True}, timeout_s=REQUEST_TIMEOUT_S)
        return {"outer": body, "nested_result": nested_result}

    def child_on_request(body: dict) -> dict:
        return {"inner": body}

    pair.parent.start(on_request=parent_on_request, on_notification=lambda _body: None, on_closed=lambda: None)
    pair.child.start(on_request=child_on_request, on_notification=lambda _body: None, on_closed=lambda: None)
    try:
        result = pair.child.request({"x": 1}, timeout_s=REQUEST_TIMEOUT_S)
        assert result == {"outer": {"x": 1}, "nested_result": {"inner": {"nested": True}}}
    finally:
        pair.parent.close()
        pair.child.close()


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        json.dumps([1, 2, 3]).encode("utf-8"),
        json.dumps({"frame": "mystery", "id": "x", "body": {}}).encode("utf-8"),
        json.dumps({"frame": "request", "body": {}}).encode("utf-8"),
        json.dumps({"frame": "notification"}).encode("utf-8"),
    ],
)
def test_malformed_frame_closes_the_channel(raw: bytes) -> None:
    """A frame that is not valid UTF-8 JSON, not an object, or an unknown type closes the channel."""
    pair = _connected_pair()
    rec_parent = _Recorder()
    pair.parent.start(
        on_request=rec_parent.on_request, on_notification=rec_parent.on_notification, on_closed=rec_parent.on_closed
    )
    try:
        pair.child._connection.send_bytes(raw)
        assert rec_parent.closed.wait(REQUEST_TIMEOUT_S)
        with pytest.raises(ChannelClosedError):
            pair.parent.request({}, timeout_s=SHORT_TIMEOUT_S)
    finally:
        pair.parent.close()
        pair.child.close()


def test_channel_module_never_calls_send_or_recv_directly() -> None:
    """`Connection.send`/`.recv` pickle their argument; only `send_bytes`/`recv_bytes` are safe."""
    source = Path(channel_module.__file__).read_text()
    tree = ast.parse(source)
    banned = [node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr in ("send", "recv")]
    assert banned == []


@pytest.mark.skipif(sys.platform == "win32", reason="AF_UNIX private-directory semantics are POSIX-only")
def test_listener_directory_is_private_and_removed_by_close() -> None:
    """The socket's directory is mode 0700 and close() removes it."""
    listener = ChannelListener()
    directory = Path(listener.address).parent
    assert directory.is_dir()
    assert stat.S_IMODE(directory.stat().st_mode) == _PRIVATE_DIR_MODE
    listener.close()
    assert not directory.exists()


def test_cross_process_round_trip() -> None:
    """The environment-variable handshake works end to end against a real child process."""
    listener = ChannelListener()
    result_queue: ProcessQueue = multiprocessing.Queue()
    process = multiprocessing.Process(target=_child_round_trip, args=(listener.child_environment(), result_queue))
    process.start()
    parent_channel: Channel | None = None
    try:
        parent_channel = listener.accept(ACCEPT_TIMEOUT_S)
        rec_parent = _Recorder(on_request=lambda body: {"pong": body["ping"]})
        parent_channel.start(
            on_request=rec_parent.on_request, on_notification=rec_parent.on_notification, on_closed=rec_parent.on_closed
        )
        assert result_queue.get(timeout=ACCEPT_TIMEOUT_S) == {"pong": "from-child"}
    finally:
        process.join(ACCEPT_TIMEOUT_S)
        if process.is_alive():
            process.terminate()
            process.join()
        if parent_channel is not None:
            parent_channel.close()
