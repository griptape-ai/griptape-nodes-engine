"""Tests for KeyedMutex — per-key exclusion across threads and event loops."""

from __future__ import annotations

import asyncio
import threading
import time

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
