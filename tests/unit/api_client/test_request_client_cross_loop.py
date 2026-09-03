"""A response that arrives on a different loop than the requester must wake the requester.

Websocket messages are handled on the transport loop; a request may have been issued from the loop
running a flow. An asyncio future belongs to exactly one loop, and settling it from another one
marks it done while scheduling the waiting task through a non-threadsafe `call_soon` -- which does
not wake a loop sitting in its selector. The waiter then resumes only when something unrelated
happens to wake that loop, so a node's result arrived tens of seconds late and a chattier log level
made the symptom disappear.

These tests drive the two-loop shape directly, and assert on ELAPSED TIME rather than on the
future's done flag: the flag was always set promptly, which is exactly why the bug was invisible.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from griptape_nodes.api_client.request_client import RequestClient

# Generous enough that a loaded CI box does not flake, far below the multi-second stall the bug
# produced (which was bounded only by the waiter's own timeout).
PROMPT_S = 2.0
# The waiter's ceiling. With the bug the settle is picked up when this timer fires, so it must be
# comfortably larger than PROMPT_S for the assertion to distinguish the two.
WAITER_TIMEOUT_S = 20.0


class _LoopInThread:
    """A second event loop, running in its own thread, otherwise completely idle.

    Idleness is the point: a busy loop wakes constantly and hides the missing wakeup.
    """

    def __enter__(self) -> asyncio.AbstractEventLoop:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        return self.loop

    def __exit__(self, *_: object) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()


def _client() -> RequestClient:
    return RequestClient(client=MagicMock())


def _await_tracked(client: RequestClient, request_id: str) -> Any:
    """Coroutine body: track `request_id`, then wait on it and report how long that took."""

    async def run() -> tuple[Any, float]:
        future = await client.track_request(request_id)
        started = time.monotonic()
        result = await asyncio.wait_for(future, timeout=WAITER_TIMEOUT_S)
        return result, time.monotonic() - started

    return run()


class TestCrossLoopSettlement:
    def test_result_settled_from_another_thread_wakes_the_waiter(self) -> None:
        with _LoopInThread() as waiter_loop:
            pending = asyncio.run_coroutine_threadsafe(_await_tracked(_c := _client(), "req-1"), waiter_loop)
            # Let the tracking register and the loop settle back into its selector.
            time.sleep(0.2)

            # Settled from THIS thread, which has no running loop -- the shape the transport
            # side has. The _unlocked internals are called directly because only one settle
            # happens here, so there is nothing for the lock to serialize against.
            _c._resolve_request_unlocked("req-1", {"ok": True})
            result, elapsed = pending.result(timeout=WAITER_TIMEOUT_S + 5)

        assert result == {"ok": True}
        assert elapsed < PROMPT_S, f"waiter resumed after {elapsed:.1f}s; its loop was not woken"

    def test_rejection_settled_from_another_thread_wakes_the_waiter(self) -> None:
        with _LoopInThread() as waiter_loop:
            client = _client()
            pending = asyncio.run_coroutine_threadsafe(_await_tracked(client, "req-2"), waiter_loop)
            time.sleep(0.2)

            boom = RuntimeError("upstream said no")
            client._reject_request_unlocked("req-2", boom)

            with pytest.raises(RuntimeError, match="upstream said no"):
                pending.result(timeout=PROMPT_S)

    def test_cancellation_by_tag_from_another_thread_wakes_the_waiter(self) -> None:
        """Worker eviction cancels by tag from the transport side; the waiter must feel it."""
        with _LoopInThread() as waiter_loop:
            client = _client()

            async def track_and_wait() -> float:
                future = await client.track_request("req-3", tag="worker-a")
                started = time.monotonic()
                try:
                    await asyncio.wait_for(future, timeout=WAITER_TIMEOUT_S)
                except asyncio.CancelledError:
                    return time.monotonic() - started
                msg = "expected cancellation"
                raise AssertionError(msg)

            pending = asyncio.run_coroutine_threadsafe(track_and_wait(), waiter_loop)
            time.sleep(0.2)

            # cancel_requests_by_tag is async only for its lock; drive its body from here.
            entry = client._pending_requests.pop("req-3")
            RequestClient._settle(entry, entry.future.cancel)
            elapsed = pending.result(timeout=WAITER_TIMEOUT_S + 5)

        assert elapsed < PROMPT_S, f"waiter learned of the cancel after {elapsed:.1f}s"
