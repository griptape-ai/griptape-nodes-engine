"""Tests for KeyedMutex — per-key exclusion across threads and event loops."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from griptape_nodes.utils.keyed_mutex import KeyedMutex


class TestKeyedMutex:
    def test_same_loop_exclusion(self) -> None:
        """Concurrent coroutines on one loop take the key's critical section one at a time."""
        mutex = KeyedMutex()
        active = 0
        max_active = 0

        async def worker() -> None:
            nonlocal active, max_active
            async with mutex.locked("key"):
                active += 1
                max_active = max(max_active, active)
                await asyncio.sleep(0.01)
                active -= 1

        async def run_all() -> None:
            await asyncio.gather(*(worker() for _ in range(5)))

        asyncio.run(run_all())

        assert max_active == 1

    def test_distinct_keys_do_not_contend(self) -> None:
        """Different keys hold their locks simultaneously."""
        mutex = KeyedMutex()
        max_active = 0
        active = 0

        async def worker(key: str) -> None:
            nonlocal active, max_active
            async with mutex.locked(key):
                active += 1
                max_active = max(max_active, active)
                await asyncio.sleep(0.01)
                active -= 1

        async def run_all() -> None:
            await asyncio.gather(worker("a"), worker("b"), worker("c"))

        asyncio.run(run_all())

        assert max_active == 3  # noqa: PLR2004

    def test_cross_loop_and_thread_exclusion(self) -> None:
        """Exclusion holds when each acquirer runs on its own event loop and thread.

        This is the dispatch shape that broke the previous dict[str, asyncio.Lock]
        design: the sync handle_request path drives async handlers on a transient
        event loop per call (asyncio.run / ThreadRunner) from arbitrary threads,
        where an asyncio.Lock raises RuntimeError or fails open.
        """
        mutex = KeyedMutex()
        order: list[str] = []
        holder_entered = threading.Event()
        release_holder = threading.Event()

        def holder_thread() -> None:
            async def hold() -> None:
                async with mutex.locked("key"):
                    order.append("holder-in")
                    holder_entered.set()
                    await asyncio.to_thread(release_holder.wait)
                    order.append("holder-out")

            asyncio.run(hold())

        def contender_thread() -> None:
            async def contend() -> None:
                async with mutex.locked("key"):
                    order.append("contender-in")

            asyncio.run(contend())

        holder = threading.Thread(target=holder_thread)
        holder.start()
        assert holder_entered.wait(timeout=5)

        contender = threading.Thread(target=contender_thread)
        contender.start()
        # Give the contender ample time to (incorrectly) enter the critical section
        time.sleep(0.2)
        assert "contender-in" not in order

        release_holder.set()
        holder.join(timeout=5)
        contender.join(timeout=5)
        assert not holder.is_alive()
        assert not contender.is_alive()

        assert order == ["holder-in", "holder-out", "contender-in"]

    def test_registry_evicts_released_keys(self) -> None:
        """The per-key entry disappears once the last holder or waiter is done."""
        mutex = KeyedMutex()

        async def use_it() -> None:
            async with mutex.locked("key"):
                assert "key" in mutex._entries

        asyncio.run(use_it())

        assert mutex._entries == {}

    def test_cancelled_waiter_checks_in_and_holds_nothing(self) -> None:
        """A waiter cancelled while polling drops its registry reference and never takes the lock.

        Cancellation can only land at the sleep between acquisition attempts, where
        nothing is held — so the holder is undisturbed and the entry's refcount
        returns to the holder's alone.
        """
        mutex = KeyedMutex()

        async def scenario() -> None:
            holder_entered = asyncio.Event()
            release_holder = asyncio.Event()

            async def holder() -> None:
                async with mutex.locked("key"):
                    holder_entered.set()
                    await release_holder.wait()

            async def waiter() -> None:
                async with mutex.locked("key"):
                    pytest.fail("cancelled waiter must never enter the critical section")

            holder_task = asyncio.create_task(holder())
            await holder_entered.wait()

            waiter_task = asyncio.create_task(waiter())
            await asyncio.sleep(0.05)  # let the waiter reach its polling sleep
            waiter_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter_task

            # Only the holder's reference remains; the waiter checked in on cancel.
            assert mutex._entries["key"].refcount == 1

            release_holder.set()
            await holder_task

        asyncio.run(scenario())

        assert mutex._entries == {}
