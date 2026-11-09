"""Tests for the private parent<->worker channel transport in `griptape_nodes.worker.channel`.

Most tests connect two `AsyncChannel`s in this process over a real loopback TCP pair: the
transport is real, only the two processes are faked away. One test spawns a real child process
through a wrapper command (standing in for `worker.command_prefix`) to prove the environment-
variable handshake, the token, and a worker's own synchronous `WorkerLink` usage end to end.

No sleeps for synchronization anywhere: every wait is on an `asyncio.Event`, a request/response
round trip, or `asyncio.gather` over the outstanding tasks -- never a fixed delay.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import os
import shutil
import struct
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import pytest

from griptape_nodes.utils.async_utils import call_function
from griptape_nodes.worker import channel as channel_module
from griptape_nodes.worker.channel import (
    AsyncChannel,
    ChannelClosedError,
    ChannelRequestError,
    GateDecision,
    WorkerLink,
    connect_to_parent,
    listen_for_worker,
)

if TYPE_CHECKING:
    from collections.abc import Callable

CHILD_SCRIPT = str(Path(__file__).parent / "worker_channel_child.py")
WRAPPER = ["cmd", "/c"] if sys.platform == "win32" else [shutil.which("env") or "/usr/bin/env"]

# Generous enough that a loaded CI box does not flake; every wait below is also event-driven,
# so these are ceilings, not the expected wait time.
REQUEST_TIMEOUT_S = 5.0
ACCEPT_TIMEOUT_S = 5.0
SHORT_TIMEOUT_S = 0.3


class _ConnectedPair(NamedTuple):
    parent: AsyncChannel
    child: AsyncChannel


async def _connected_pair() -> _ConnectedPair:
    """Connect two AsyncChannels in this process over a real loopback TCP pair."""
    pending = await listen_for_worker()
    child_task = asyncio.ensure_future(connect_to_parent(pending.child_environment))
    parent_channel = await pending.accept(ACCEPT_TIMEOUT_S)
    child_channel = await child_task
    return _ConnectedPair(parent_channel, child_channel)


async def _echo_body(body: dict) -> dict:
    return dict(body)


async def _empty_response(_body: dict) -> dict:
    return {}


class _Recorder:
    """Captures one `AsyncChannel`'s callbacks in call order, for assertions without sleeping."""

    def __init__(self, on_request: Callable[[dict], Any] | None = None) -> None:
        self._on_request_impl = on_request if on_request is not None else _echo_body
        self.requests_seen: list[dict] = []
        self.notifications: list[dict] = []
        self.closed = asyncio.Event()
        self.closed_count = 0

    async def on_request(self, body: dict) -> dict:
        self.requests_seen.append(body)
        return await call_function(self._on_request_impl, body)

    def on_notification(self, body: dict) -> None:
        self.notifications.append(body)

    def on_closed(self) -> None:
        self.closed_count += 1
        self.closed.set()


async def _drain_other_tasks() -> None:
    """Wait for every other currently-outstanding task to finish. No sleep, no polling."""
    others = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
    await asyncio.gather(*others, return_exceptions=True)


@pytest.mark.asyncio
async def test_round_trip_request_result_both_directions() -> None:
    """Either side can call request() on the other and get back its on_request result."""
    pair = await _connected_pair()
    rec_parent = _Recorder(on_request=lambda body: {"sum": body["x"] + body["y"]})
    rec_child = _Recorder(on_request=lambda body: {"echo": body})
    pair.parent.start(
        on_request=rec_parent.on_request, on_notification=rec_parent.on_notification, on_closed=rec_parent.on_closed
    )
    pair.child.start(
        on_request=rec_child.on_request, on_notification=rec_child.on_notification, on_closed=rec_child.on_closed
    )
    try:
        assert await pair.parent.request({"ping": True}, timeout_s=REQUEST_TIMEOUT_S) == {"echo": {"ping": True}}
        assert await pair.child.request({"x": 1, "y": 2}, timeout_s=REQUEST_TIMEOUT_S) == {"sum": 3}
    finally:
        pair.parent.close()
        pair.child.close()


@pytest.mark.asyncio
async def test_notifications_delivered_in_order_before_a_later_result() -> None:
    """A result is never observed before notifications the peer sent ahead of it."""
    pair = await _connected_pair()
    rec_parent = _Recorder()
    notify_count = 5

    async def parent_on_request(_body: dict) -> dict:
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
        result = await pair.child.request({}, timeout_s=REQUEST_TIMEOUT_S)
        assert result == {"done": True}
        assert rec_child.notifications == [{"seq": seq} for seq in range(notify_count)]
    finally:
        pair.parent.close()
        pair.child.close()


@pytest.mark.asyncio
async def test_gate_allows_and_runs_before_on_request() -> None:
    """A gate that allows a request runs first, and on_request still sees the dispatched body."""
    pair = await _connected_pair()
    call_order: list[str] = []

    def gate(body: dict) -> GateDecision:
        call_order.append("gate")
        return GateDecision(dispatch=body, reply=None)

    async def parent_on_request(body: dict) -> dict:
        call_order.append("on_request")
        return {"got": body}

    pair.parent.start(on_request=parent_on_request, on_notification=lambda _b: None, on_closed=lambda: None, gate=gate)
    rec_child = _Recorder()
    pair.child.start(
        on_request=rec_child.on_request, on_notification=rec_child.on_notification, on_closed=rec_child.on_closed
    )
    try:
        result = await pair.child.request({"x": 1}, timeout_s=REQUEST_TIMEOUT_S)
        assert result == {"got": {"x": 1}}
        assert call_order == ["gate", "on_request"]
    finally:
        pair.parent.close()
        pair.child.close()


@pytest.mark.asyncio
async def test_gate_denies_without_calling_on_request() -> None:
    """A gate that denies a request sends its reply itself, as a normal body; on_request never runs."""
    pair = await _connected_pair()
    on_request_calls: list[dict] = []

    def gate(_body: dict) -> GateDecision:
        return GateDecision(dispatch=None, reply={"denied": True})

    async def parent_on_request(body: dict) -> dict:
        on_request_calls.append(body)
        return {}

    pair.parent.start(on_request=parent_on_request, on_notification=lambda _b: None, on_closed=lambda: None, gate=gate)
    rec_child = _Recorder()
    pair.child.start(
        on_request=rec_child.on_request, on_notification=rec_child.on_notification, on_closed=rec_child.on_closed
    )
    try:
        result = await pair.child.request({"x": 1}, timeout_s=REQUEST_TIMEOUT_S)
        assert result == {"denied": True}
        assert on_request_calls == []
    finally:
        pair.parent.close()
        pair.child.close()


@pytest.mark.asyncio
async def test_gate_that_raises_closes_the_channel() -> None:
    """A gate exception is fail-closed: the channel closes, on_request never runs, on_closed fires once."""
    pair = await _connected_pair()
    rec_parent = _Recorder()
    on_request_calls: list[dict] = []

    def gate(_body: dict) -> GateDecision:
        msg = "gate exploded"
        raise RuntimeError(msg)

    async def parent_on_request(body: dict) -> dict:
        on_request_calls.append(body)
        return {}

    pair.parent.start(
        on_request=parent_on_request, on_notification=lambda _b: None, on_closed=rec_parent.on_closed, gate=gate
    )
    rec_child = _Recorder()
    pair.child.start(
        on_request=rec_child.on_request, on_notification=rec_child.on_notification, on_closed=rec_child.on_closed
    )
    try:
        with pytest.raises(ChannelClosedError):
            await pair.child.request({"x": 1}, timeout_s=REQUEST_TIMEOUT_S)
        assert on_request_calls == []
        await asyncio.wait_for(rec_parent.closed.wait(), REQUEST_TIMEOUT_S)
        assert rec_parent.closed_count == 1
        await asyncio.wait_for(rec_child.closed.wait(), REQUEST_TIMEOUT_S)
        assert rec_child.closed_count == 1
    finally:
        pair.parent.close()
        pair.child.close()


@pytest.mark.asyncio
async def test_on_notification_that_raises_closes_the_channel() -> None:
    """An on_notification exception is fail-closed: the channel closes and on_closed fires once."""
    pair = await _connected_pair()
    rec_parent = _Recorder()

    def parent_on_notification(_body: dict) -> None:
        msg = "on_notification exploded"
        raise RuntimeError(msg)

    pair.parent.start(
        on_request=rec_parent.on_request, on_notification=parent_on_notification, on_closed=rec_parent.on_closed
    )
    rec_child = _Recorder()
    pair.child.start(
        on_request=rec_child.on_request, on_notification=rec_child.on_notification, on_closed=rec_child.on_closed
    )
    try:
        pair.child.notify({"hello": True})
        await asyncio.wait_for(rec_parent.closed.wait(), REQUEST_TIMEOUT_S)
        assert rec_parent.closed_count == 1
        await asyncio.wait_for(rec_child.closed.wait(), REQUEST_TIMEOUT_S)
        assert rec_child.closed_count == 1
        with pytest.raises(ChannelClosedError):
            await pair.child.request({}, timeout_s=SHORT_TIMEOUT_S)
    finally:
        pair.parent.close()
        pair.child.close()


@pytest.mark.asyncio
async def test_gate_denial_with_no_reply_closes_the_channel() -> None:
    """A gate that denies without a reply is a contract violation: fail-closed, not a bad frame."""
    pair = await _connected_pair()
    rec_parent = _Recorder()
    on_request_calls: list[dict] = []

    def gate(_body: dict) -> GateDecision:
        return GateDecision(dispatch=None, reply=None)

    async def parent_on_request(body: dict) -> dict:
        on_request_calls.append(body)
        return {}

    pair.parent.start(
        on_request=parent_on_request, on_notification=lambda _b: None, on_closed=rec_parent.on_closed, gate=gate
    )
    rec_child = _Recorder()
    pair.child.start(
        on_request=rec_child.on_request, on_notification=rec_child.on_notification, on_closed=rec_child.on_closed
    )
    try:
        with pytest.raises(ChannelClosedError):
            await pair.child.request({"x": 1}, timeout_s=REQUEST_TIMEOUT_S)
        assert on_request_calls == []
        await asyncio.wait_for(rec_parent.closed.wait(), REQUEST_TIMEOUT_S)
        assert rec_parent.closed_count == 1
    finally:
        pair.parent.close()
        pair.child.close()


@pytest.mark.asyncio
async def test_gate_reply_that_is_not_a_dict_answers_with_a_channel_request_error() -> None:
    """A non-dict gate reply fails only that one request with an error frame, not the whole channel."""
    pair = await _connected_pair()

    def gate(_body: dict) -> GateDecision:
        return GateDecision(dispatch=None, reply=["not", "a", "dict"])  # pyright: ignore[reportArgumentType]

    pair.parent.start(on_request=_empty_response, on_notification=lambda _b: None, on_closed=lambda: None, gate=gate)
    rec_child = _Recorder()
    pair.child.start(
        on_request=rec_child.on_request, on_notification=rec_child.on_notification, on_closed=rec_child.on_closed
    )
    try:
        with pytest.raises(ChannelRequestError) as exc_info:
            await pair.child.request({"x": 1}, timeout_s=REQUEST_TIMEOUT_S)
        assert exc_info.value.error_type == "TypeError"
        # The channel stays open: only this one request failed, not the whole channel.
        with pytest.raises(ChannelRequestError):
            await pair.child.request({"x": 1}, timeout_s=REQUEST_TIMEOUT_S)
    finally:
        pair.parent.close()
        pair.child.close()


@pytest.mark.asyncio
async def test_on_request_raising_answers_with_a_channel_request_error() -> None:
    """on_request raising answers with an error frame; the requester sees ChannelRequestError, not a body."""
    pair = await _connected_pair()

    async def parent_on_request(_body: dict) -> dict:
        msg = "boom"
        raise ValueError(msg)

    pair.parent.start(on_request=parent_on_request, on_notification=lambda _b: None, on_closed=lambda: None)
    rec_child = _Recorder()
    pair.child.start(
        on_request=rec_child.on_request, on_notification=rec_child.on_notification, on_closed=rec_child.on_closed
    )
    try:
        with pytest.raises(ChannelRequestError) as exc_info:
            await pair.child.request({"x": 1}, timeout_s=REQUEST_TIMEOUT_S)
        assert exc_info.value.error_type == "ValueError"
        assert exc_info.value.message == "boom"
        # The channel itself stays open: only this one request failed, not the whole channel.
        with pytest.raises(ChannelRequestError):
            await pair.child.request({"x": 1}, timeout_s=REQUEST_TIMEOUT_S)
    finally:
        pair.parent.close()
        pair.child.close()


@pytest.mark.asyncio
async def test_unencodable_handler_result_answers_with_a_channel_request_error() -> None:
    """A handler result that will not encode as JSON answers with an error frame, not a hang."""
    pair = await _connected_pair()

    async def parent_on_request(_body: dict) -> dict:
        return {"bad": object()}

    pair.parent.start(on_request=parent_on_request, on_notification=lambda _b: None, on_closed=lambda: None)
    rec_child = _Recorder()
    pair.child.start(
        on_request=rec_child.on_request, on_notification=rec_child.on_notification, on_closed=rec_child.on_closed
    )
    try:
        with pytest.raises(ChannelRequestError) as exc_info:
            await pair.child.request({"x": 1}, timeout_s=REQUEST_TIMEOUT_S)
        assert exc_info.value.error_type == "TypeError"
        # The channel stays open and still answers a normal request afterward.
        assert await pair.parent.request({"y": 1}, timeout_s=REQUEST_TIMEOUT_S) == {"y": 1}
    finally:
        pair.parent.close()
        pair.child.close()


@pytest.mark.asyncio
async def test_on_request_returning_a_non_dict_answers_with_a_channel_request_error() -> None:
    """on_request returning None (or anything but a dict) answers with an error, not a malformed body."""
    pair = await _connected_pair()

    async def parent_on_request(_body: dict) -> dict:
        return None  # type: ignore[return-value]  # the point of the test: on_request forgot to return

    pair.parent.start(on_request=parent_on_request, on_notification=lambda _b: None, on_closed=lambda: None)
    rec_child = _Recorder()
    pair.child.start(
        on_request=rec_child.on_request, on_notification=rec_child.on_notification, on_closed=rec_child.on_closed
    )
    try:
        with pytest.raises(ChannelRequestError) as exc_info:
            await pair.child.request({"x": 1}, timeout_s=REQUEST_TIMEOUT_S)
        assert exc_info.value.error_type == "TypeError"
        # The channel stays open: only this one request failed, not the whole channel.
        assert await pair.parent.request({"y": 1}, timeout_s=REQUEST_TIMEOUT_S) == {"y": 1}
    finally:
        pair.parent.close()
        pair.child.close()


@pytest.mark.asyncio
async def test_unencodable_outgoing_body_raises_to_the_caller_without_sending() -> None:
    """A request/notification body that will not encode raises to the caller; nothing goes out."""
    pair = await _connected_pair()
    rec_parent = _Recorder()
    pair.parent.start(
        on_request=rec_parent.on_request, on_notification=rec_parent.on_notification, on_closed=rec_parent.on_closed
    )
    rec_child = _Recorder()
    pair.child.start(
        on_request=rec_child.on_request, on_notification=rec_child.on_notification, on_closed=rec_child.on_closed
    )
    try:
        with pytest.raises(TypeError):
            await pair.child.request({"bad": object()}, timeout_s=REQUEST_TIMEOUT_S)
        with pytest.raises(TypeError):
            pair.child.notify({"bad": object()})
        # Neither attempt reached the wire: a normal request still round-trips afterward, and the
        # notification ordering guarantee confirms no stray notify from above arrived either.
        pair.child.notify({"ok": True})
        result = await pair.child.request({}, timeout_s=REQUEST_TIMEOUT_S)
        assert result == {}
        assert rec_parent.notifications == [{"ok": True}]
    finally:
        pair.parent.close()
        pair.child.close()


@pytest.mark.asyncio
async def test_answering_a_request_after_close_raises_no_unhandled_task_exception() -> None:
    """on_request's reply racing close() must not surface as 'Task exception was never retrieved'."""
    loop = asyncio.get_running_loop()
    unhandled: list[dict] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    try:
        pair = await _connected_pair()
        reached_handler = asyncio.Event()
        release_handler = asyncio.Event()

        async def parent_on_request(_body: dict) -> dict:
            reached_handler.set()
            await release_handler.wait()
            return {}

        rec_child = _Recorder()
        pair.parent.start(on_request=parent_on_request, on_notification=lambda _b: None, on_closed=lambda: None)
        pair.child.start(
            on_request=rec_child.on_request, on_notification=rec_child.on_notification, on_closed=rec_child.on_closed
        )

        request_task = asyncio.ensure_future(pair.child.request({"x": 1}, timeout_s=REQUEST_TIMEOUT_S))
        await asyncio.wait_for(reached_handler.wait(), REQUEST_TIMEOUT_S)
        pair.parent.close()
        pair.child.close()
        with pytest.raises(ChannelClosedError):
            await request_task
        release_handler.set()
        await _drain_other_tasks()
        assert unhandled == []
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_nested_request_from_an_on_request_task() -> None:
    """An on_request handler may itself await request() back to the peer without deadlocking."""
    pair = await _connected_pair()

    async def parent_on_request(body: dict) -> dict:
        nested_result = await pair.parent.request({"nested": True}, timeout_s=REQUEST_TIMEOUT_S)
        return {"outer": body, "nested_result": nested_result}

    async def child_on_request(body: dict) -> dict:
        return {"inner": body}

    pair.parent.start(on_request=parent_on_request, on_notification=lambda _b: None, on_closed=lambda: None)
    pair.child.start(on_request=child_on_request, on_notification=lambda _b: None, on_closed=lambda: None)
    try:
        result = await pair.child.request({"x": 1}, timeout_s=REQUEST_TIMEOUT_S)
        assert result == {"outer": {"x": 1}, "nested_result": {"inner": {"nested": True}}}
    finally:
        pair.parent.close()
        pair.child.close()


@pytest.mark.asyncio
async def test_request_timeout_removes_its_pending_entry() -> None:
    """A timed-out request raises TimeoutError and does not leave its future behind."""
    pair = await _connected_pair()
    never_answer = asyncio.Event()

    async def parent_on_request(_body: dict) -> dict:
        await never_answer.wait()
        return {}

    pair.parent.start(on_request=parent_on_request, on_notification=lambda _b: None, on_closed=lambda: None)
    rec_child = _Recorder()
    pair.child.start(
        on_request=rec_child.on_request, on_notification=rec_child.on_notification, on_closed=rec_child.on_closed
    )
    try:
        with pytest.raises(TimeoutError):
            await pair.child.request({"x": 1}, timeout_s=SHORT_TIMEOUT_S)
        assert pair.child._pending == {}
    finally:
        never_answer.set()
        pair.parent.close()
        pair.child.close()


@pytest.mark.asyncio
async def test_close_from_the_peer_fails_pending_requests_and_calls_on_closed_once() -> None:
    """When the peer closes, a pending request fails and on_closed fires exactly once."""
    pair = await _connected_pair()
    rec_parent = _Recorder()
    request_received = asyncio.Event()
    release_child_handler = asyncio.Event()

    async def child_on_request(_body: dict) -> dict:
        request_received.set()
        await release_child_handler.wait()
        return {}

    pair.parent.start(
        on_request=rec_parent.on_request, on_notification=rec_parent.on_notification, on_closed=rec_parent.on_closed
    )
    pair.child.start(on_request=child_on_request, on_notification=lambda _b: None, on_closed=lambda: None)

    request_task = asyncio.ensure_future(pair.parent.request({"x": 1}, timeout_s=REQUEST_TIMEOUT_S))
    try:
        await asyncio.wait_for(request_received.wait(), REQUEST_TIMEOUT_S)
        # The request has definitely reached the child and is pending on the parent's side now.
        pair.child.close()
        with pytest.raises(ChannelClosedError):
            await request_task
        await asyncio.wait_for(rec_parent.closed.wait(), REQUEST_TIMEOUT_S)
        assert rec_parent.closed_count == 1
    finally:
        release_child_handler.set()
        pair.parent.close()


@pytest.mark.asyncio
async def test_close_calls_on_closed_on_the_closing_side_too() -> None:
    """close() ends the channel on both the closing side and the peer, each exactly once."""
    pair = await _connected_pair()
    rec_parent = _Recorder()
    rec_child = _Recorder()
    pair.parent.start(
        on_request=rec_parent.on_request, on_notification=rec_parent.on_notification, on_closed=rec_parent.on_closed
    )
    pair.child.start(
        on_request=rec_child.on_request, on_notification=rec_child.on_notification, on_closed=rec_child.on_closed
    )

    pair.parent.close()

    await asyncio.wait_for(rec_parent.closed.wait(), REQUEST_TIMEOUT_S)
    assert rec_parent.closed_count == 1
    await asyncio.wait_for(rec_child.closed.wait(), REQUEST_TIMEOUT_S)
    assert rec_child.closed_count == 1


@pytest.mark.asyncio
async def test_clean_close_does_not_log_at_error(caplog: pytest.LogCaptureFixture) -> None:
    """A peer closing cleanly between frames is a normal close, not an error worth logging."""
    pair = await _connected_pair()
    rec_parent = _Recorder()
    rec_child = _Recorder()
    pair.parent.start(
        on_request=rec_parent.on_request, on_notification=rec_parent.on_notification, on_closed=rec_parent.on_closed
    )
    pair.child.start(
        on_request=rec_child.on_request, on_notification=rec_child.on_notification, on_closed=rec_child.on_closed
    )
    with caplog.at_level(logging.DEBUG, logger="griptape_nodes"):
        pair.child.close()
        await asyncio.wait_for(rec_parent.closed.wait(), REQUEST_TIMEOUT_S)
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff\xff\xff\xff not json",
        struct.pack(">I", 7) + b"not son",
        struct.pack(">I", 7) + b"[1,2,3",
        channel_module._encode_frame({"frame": "mystery", "id": "x", "body": {}}),
        channel_module._encode_frame({"frame": "request", "id": "x"}),
        channel_module._encode_frame({"frame": "notification"}),
        channel_module._encode_frame({"frame": "result", "id": "x"}),
        channel_module._encode_frame(
            {"frame": "result", "id": "x", "body": {}, "error": {"type": "E", "message": "m"}}
        ),
        channel_module._encode_frame({"frame": "result", "id": "x", "error": {"type": "E"}}),
    ],
)
@pytest.mark.asyncio
async def test_malformed_frame_closes_the_channel(raw: bytes) -> None:
    """A frame that is not valid JSON, not an object, or has the wrong shape closes the channel."""
    pair = await _connected_pair()
    rec_parent = _Recorder()
    pair.parent.start(
        on_request=rec_parent.on_request, on_notification=rec_parent.on_notification, on_closed=rec_parent.on_closed
    )
    try:
        pair.child._writer.write(raw)
        await pair.child._writer.drain()
        # Close so a truncated length prefix hits EOF promptly instead of waiting for bytes that
        # are never coming; a complete frame has already been fully read by the time this lands.
        pair.child.close()
        await asyncio.wait_for(rec_parent.closed.wait(), REQUEST_TIMEOUT_S)
        with pytest.raises(ChannelClosedError):
            await pair.parent.request({}, timeout_s=SHORT_TIMEOUT_S)
    finally:
        pair.parent.close()
        pair.child.close()


def test_channel_module_never_uses_pickle() -> None:
    """Frames are JSON only; nothing in this module's code may import or reference pickle."""
    source = Path(channel_module.__file__).read_text()
    tree = ast.parse(source)
    imported_names = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
        if "pickle" in alias.name
    }
    referenced_names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and "pickle" in node.id}
    assert imported_names == set()
    assert referenced_names == set()


@pytest.mark.asyncio
async def test_accept_times_out_cleanly_when_nobody_connects() -> None:
    """accept() raises TimeoutError, promptly, when nothing ever connects."""
    pending = await listen_for_worker()
    with pytest.raises(TimeoutError):
        await pending.accept(SHORT_TIMEOUT_S)


@pytest.mark.asyncio
async def test_oversized_handshake_frame_is_rejected_without_reading_the_body() -> None:
    """A declared hello length over the cap is rejected from the header alone, before any body."""
    pending = await listen_for_worker()
    host, port_str = pending.child_environment[channel_module.CHANNEL_ADDRESS_ENV].rsplit(":", 1)
    port = int(port_str)

    _reader, bad_writer = await asyncio.open_connection(host, port)
    oversized_length = channel_module._HELLO_MAX_LENGTH + 1
    bad_writer.write(struct.pack(">I", oversized_length))
    # Deliberately never sends the (huge, declared) body: rejection must not wait for it.

    start = time.monotonic()
    child = await connect_to_parent(pending.child_environment)
    parent_channel = await pending.accept(ACCEPT_TIMEOUT_S)
    elapsed = time.monotonic() - start
    # Reading the declared-oversized body would have blocked for the full accept() timeout.
    assert elapsed < ACCEPT_TIMEOUT_S / 2
    rec_parent = _Recorder()
    parent_channel.start(
        on_request=rec_parent.on_request, on_notification=rec_parent.on_notification, on_closed=rec_parent.on_closed
    )
    rec_child = _Recorder()
    child.start(
        on_request=rec_child.on_request, on_notification=rec_child.on_notification, on_closed=rec_child.on_closed
    )
    try:
        assert await child.request({"op": "echo"}, timeout_s=REQUEST_TIMEOUT_S) == {"op": "echo"}
    finally:
        parent_channel.close()
        child.close()
        bad_writer.close()


@pytest.mark.asyncio
async def test_bad_and_silent_peers_do_not_block_a_real_worker() -> None:
    """A silent peer, a wrong token, and garbage bytes are each rejected without affecting accept()."""
    pending = await listen_for_worker()
    host, port_str = pending.child_environment[channel_module.CHANNEL_ADDRESS_ENV].rsplit(":", 1)
    port = int(port_str)

    _silent_reader, silent_writer = await asyncio.open_connection(host, port)  # connects, sends nothing
    _wrong_reader, wrong_writer = await asyncio.open_connection(host, port)
    wrong_writer.write(channel_module._encode_frame({"token": "0" * 64}))
    _garbage_reader, garbage_writer = await asyncio.open_connection(host, port)
    garbage_writer.write(b"\xff\xff\xff\xff not json")

    child = await connect_to_parent(pending.child_environment)
    parent_channel = await pending.accept(ACCEPT_TIMEOUT_S)
    rec_parent = _Recorder()
    parent_channel.start(
        on_request=rec_parent.on_request, on_notification=rec_parent.on_notification, on_closed=rec_parent.on_closed
    )
    rec_child = _Recorder()
    child.start(
        on_request=rec_child.on_request, on_notification=rec_child.on_notification, on_closed=rec_child.on_closed
    )
    try:
        assert await child.request({"op": "echo"}, timeout_s=REQUEST_TIMEOUT_S) == {"op": "echo"}
    finally:
        parent_channel.close()
        child.close()
        silent_writer.close()
        wrong_writer.close()
        garbage_writer.close()


@pytest.mark.asyncio
async def test_worker_link_request_from_the_io_thread_raises_runtime_error() -> None:
    """WorkerLink.request() called from an on_request handler (the I/O thread) raises RuntimeError."""
    pending = await listen_for_worker()
    link = WorkerLink(pending.child_environment)
    parent_channel = await pending.accept(ACCEPT_TIMEOUT_S)
    rec_parent = _Recorder()
    parent_channel.start(
        on_request=rec_parent.on_request, on_notification=rec_parent.on_notification, on_closed=rec_parent.on_closed
    )
    captured: list[BaseException] = []

    async def worker_on_request(_body: dict) -> dict:
        try:
            link.request({}, timeout_s=SHORT_TIMEOUT_S)
        except RuntimeError as exc:
            captured.append(exc)
        return {}

    link.start(on_request=worker_on_request, on_notification=lambda _b: None, on_closed=lambda: None)
    try:
        result = await parent_channel.request({}, timeout_s=REQUEST_TIMEOUT_S)
        assert result == {}
        assert len(captured) == 1
        assert isinstance(captured[0], RuntimeError)
    finally:
        link.close()
        parent_channel.close()


@pytest.mark.asyncio
async def test_worker_link_notify_after_close_raises_channel_closed_error() -> None:
    """WorkerLink.notify() after the channel ends raises to the caller, not just asyncio's log."""
    pending = await listen_for_worker()
    link = WorkerLink(pending.child_environment)
    parent_channel = await pending.accept(ACCEPT_TIMEOUT_S)
    # The worker's own on_closed, not the parent's: it only fires after link.channel's _closed
    # flag is actually set, on the I/O thread link.notify() needs to see it from.
    worker_closed = threading.Event()
    link.start(on_request=_empty_response, on_notification=lambda _b: None, on_closed=worker_closed.set)
    parent_channel.start(on_request=_empty_response, on_notification=lambda _b: None, on_closed=lambda: None)

    parent_channel.close()
    await asyncio.to_thread(worker_closed.wait, REQUEST_TIMEOUT_S)

    # Called from this test's own thread, which is not the WorkerLink's I/O thread.
    with pytest.raises(ChannelClosedError):
        link.notify({"x": 1})


@pytest.mark.asyncio
async def test_worker_link_notify_with_unencodable_body_raises_type_error() -> None:
    """WorkerLink.notify() with a body that will not encode raises to the caller, nothing is sent."""
    pending = await listen_for_worker()
    link = WorkerLink(pending.child_environment)
    parent_channel = await pending.accept(ACCEPT_TIMEOUT_S)
    link.start(on_request=_empty_response, on_notification=lambda _b: None, on_closed=lambda: None)
    notified = asyncio.Event()

    def on_notification(note: dict) -> None:
        if note == {"ok": True}:
            notified.set()

    parent_channel.start(on_request=_empty_response, on_notification=on_notification, on_closed=lambda: None)
    try:
        # Called from this test's own thread, which is not the WorkerLink's I/O thread.
        with pytest.raises(TypeError):
            link.notify({"bad": object()})
        link.notify({"ok": True})
        await asyncio.wait_for(notified.wait(), REQUEST_TIMEOUT_S)
    finally:
        link.close()
        parent_channel.close()


@pytest.mark.asyncio
async def test_worker_link_sync_request_from_inside_a_running_loop_on_another_thread() -> None:
    """A blocking link.request() call made from inside another thread's own asyncio.run() must not deadlock."""
    pending = await listen_for_worker()
    link = WorkerLink(pending.child_environment)
    parent_channel = await pending.accept(ACCEPT_TIMEOUT_S)
    rec_parent = _Recorder(on_request=lambda body: {"sum": body["x"] + body["y"]})
    parent_channel.start(
        on_request=rec_parent.on_request, on_notification=rec_parent.on_notification, on_closed=rec_parent.on_closed
    )
    link.start(on_request=_empty_response, on_notification=lambda _b: None, on_closed=lambda: None)

    result_holder: dict[str, Any] = {}

    async def main_loop_work() -> None:
        result_holder["result"] = link.request({"x": 2, "y": 3}, timeout_s=REQUEST_TIMEOUT_S)

    def run_on_another_thread() -> None:
        asyncio.run(main_loop_work())

    other_thread = threading.Thread(target=run_on_another_thread)
    other_thread.start()
    # A plain thread.join() here would block this test's own event loop, which parent_channel's
    # read loop needs to run on in order to answer the request this thread is waiting on.
    await asyncio.to_thread(other_thread.join, REQUEST_TIMEOUT_S)
    try:
        assert not other_thread.is_alive()
        assert result_holder.get("result") == {"sum": 5}
    finally:
        link.close()
        parent_channel.close()


@pytest.mark.asyncio
async def test_cross_process_round_trip_through_a_wrapper() -> None:
    """The environment-variable/token handshake works end to end against a real child process."""
    pending = await listen_for_worker()
    env = {**os.environ, **pending.child_environment}
    process = await asyncio.create_subprocess_exec(*WRAPPER, sys.executable, CHILD_SCRIPT, env=env)
    try:
        channel = await pending.accept(ACCEPT_TIMEOUT_S)
        notes: list[dict] = []
        enough_notes = asyncio.Event()
        expected_note_count = 3  # 2 from notify_then_answer, 1 from the worker's own sync request

        def on_notification(note: dict) -> None:
            notes.append(note)
            if len(notes) >= expected_note_count:
                enough_notes.set()

        async def on_request(body: dict) -> dict:
            return {"sum": body["x"] + body["y"]}

        channel.start(on_request=on_request, on_notification=on_notification, on_closed=lambda: None)
        assert await channel.request({"op": "echo", "v": 1}, timeout_s=REQUEST_TIMEOUT_S) == {
            "echo": {"op": "echo", "v": 1}
        }
        assert await channel.request({"op": "notify_then_answer"}, timeout_s=REQUEST_TIMEOUT_S) == {"answered": True}
        await asyncio.wait_for(enough_notes.wait(), REQUEST_TIMEOUT_S)
        assert [n for n in notes if "event" in n] == [{"event": 1}, {"event": 2}]
        # The worker's own main loop drives this through WorkerLink.request() -- a synchronous
        # call made from inside its running event loop -- which must not have deadlocked.
        assert [n for n in notes if "sum_reply" in n] == [{"sum_reply": {"sum": 5}}]
        channel.close()
        assert await asyncio.wait_for(process.wait(), REQUEST_TIMEOUT_S) == 0
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.asyncio
async def test_a_killed_worker_process_closes_the_channel_and_fails_what_waits() -> None:
    """Killing the worker process (not the wrapper) ends the channel; a pending request fails."""
    pending = await listen_for_worker()
    env = {**os.environ, **pending.child_environment}
    # Unwrapped: killing a wrapper such as cmd.exe would not kill the worker process it started.
    process = await asyncio.create_subprocess_exec(sys.executable, CHILD_SCRIPT, env=env)
    try:
        channel = await pending.accept(ACCEPT_TIMEOUT_S)
        closed = asyncio.Event()
        channel.start(on_request=_echo_body, on_notification=lambda _b: None, on_closed=closed.set)
        waiting = asyncio.ensure_future(channel.request({"op": "hang"}, timeout_s=None))
        process.kill()
        await asyncio.wait_for(closed.wait(), REQUEST_TIMEOUT_S)
        with pytest.raises(ChannelClosedError):
            await waiting
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
