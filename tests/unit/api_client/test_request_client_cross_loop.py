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
import logging
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
        result = await asyncio.wait_for(asyncio.wrap_future(future), timeout=WAITER_TIMEOUT_S)
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
                    await asyncio.wait_for(asyncio.wrap_future(future), timeout=WAITER_TIMEOUT_S)
                except asyncio.CancelledError:
                    return time.monotonic() - started
                msg = "expected cancellation"
                raise AssertionError(msg)

            pending = asyncio.run_coroutine_threadsafe(track_and_wait(), waiter_loop)
            time.sleep(0.2)

            # cancel_requests_by_tag is async only for its lock; drive its body from here.
            entry = client._pending_requests.pop("req-3")
            RequestClient._settle(entry.future.cancel)
            elapsed = pending.result(timeout=WAITER_TIMEOUT_S + 5)

        assert elapsed < PROMPT_S, f"waiter learned of the cancel after {elapsed:.1f}s"


class TestLockCrossesLoops:
    """The pending map is mutated from more than one loop, through the real locked methods.

    This does NOT reproduce a loop-bound lock's failure, and it passes with either primitive:
    `asyncio.Lock.acquire` only consults its bound loop on the CONTENDED path, and no section this
    lock guards contains an await, so a section is always released before anything else can reach
    it. What the wrong primitive costs is exclusion rather than an exception -- two threads both
    pass the uncontended fast path and mutate the dict together -- which is not deterministically
    reachable from a test. Kept as a smoke test that the cross-loop path works at all.
    """

    def test_tracking_and_cancelling_from_different_loops(self) -> None:
        with _LoopInThread() as other_loop:
            client = _client()

            asyncio.run(client.track_request("req-a", tag="worker-a"))

            # Same lock, second loop, through the public API.
            asyncio.run_coroutine_threadsafe(client.track_request("req-b", tag="worker-b"), other_loop).result(
                timeout=5
            )
            asyncio.run_coroutine_threadsafe(client.cancel_requests_by_tag("worker-a"), other_loop).result(timeout=5)

            assert "req-a" not in client._pending_requests
            assert "req-b" in client._pending_requests


class TestTheFutureHasNoOwningLoop:
    """A tracked future outlives, and is independent of, the loop that created it."""

    def test_a_future_is_awaited_from_a_loop_that_did_not_create_it(self) -> None:
        client = _client()
        # asyncio.run closes its loop on the way out, so by the time anything waits on this
        # future the loop that created it no longer exists.
        future = asyncio.run(client.track_request("portable-1"))

        async def wait_for_it() -> Any:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout=WAITER_TIMEOUT_S)

        with _LoopInThread() as third_loop:
            pending = asyncio.run_coroutine_threadsafe(wait_for_it(), third_loop)
            time.sleep(0.2)
            client._resolve_request_unlocked("portable-1", {"ok": True})

            assert pending.result(timeout=PROMPT_S) == {"ok": True}

    def test_a_result_survives_its_creating_loop_being_closed(self) -> None:
        """The loop-bound design had to drop this settle; there is no longer anything to drop.

        Its owning loop was gone, so `call_soon_threadsafe` raised and the result was discarded
        with a debug line. Nothing was waiting by then, but the settle had to be defended against.
        """
        client = _client()
        future = asyncio.run(client.track_request("orphan-1"))

        client._resolve_request_unlocked("orphan-1", {"ok": True})

        assert future.result(timeout=0) == {"ok": True}


class TestSettlingAnAlreadyFinishedRequest:
    """A waiter that gives up settles the future itself, without holding the client's lock.

    `wrap_future` propagates the waiter's cancellation into the tracked future, so a response
    landing at that moment meets a future that is already done. The loop-bound design could not
    reach this state: every settle was funnelled through one loop's callback queue, which
    serialized them for free.
    """

    @pytest.mark.asyncio
    async def test_a_response_arriving_after_the_waiter_timed_out_is_dropped(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = _client()
        future = await client.track_request("late-1")

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.wrap_future(future), timeout=0.01)
        assert future.cancelled(), "the waiter's timeout should have cancelled the tracked future"

        # The transport has no idea the waiter left. Delivering to it must not raise.
        with caplog.at_level(logging.DEBUG, logger="griptape_nodes.api_client.request_client"):
            await client._resolve_request("late-1", {"ok": True})

        # Asserted so the guard cannot quietly become unreachable: without it this settle raises.
        assert "already-finished" in caplog.text
        assert client.pending_count == 0

    @pytest.mark.asyncio
    async def test_a_rejection_arriving_after_a_cancel_by_tag_is_dropped(self) -> None:
        client = _client()
        await client.track_request("late-2", tag="worker-a")

        await client.cancel_requests_by_tag("worker-a")
        # cancel_requests_by_tag already popped the entry, so this is the unknown-request path.
        await client._reject_request("late-2", RuntimeError("worker died"))

        assert client.pending_count == 0

    @pytest.mark.asyncio
    async def test_a_duplicate_request_id_still_raises(self) -> None:
        client = _client()
        await client.track_request("dup-1")

        with pytest.raises(ValueError, match="Request ID already exists"):
            await client.track_request("dup-1")
