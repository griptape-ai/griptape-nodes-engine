"""Per-key mutual exclusion that works across threads and event loops."""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from griptape_nodes.utils.async_utils import to_thread

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@dataclass
class _KeyedMutexEntry:
    """One key's lock plus the number of holders and waiters referencing it."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    refcount: int = 0


class KeyedMutex:
    """Mutual exclusion per string key, safe across threads AND event loops.

    Why not ``dict[str, asyncio.Lock]``: request handlers are dispatched from
    arbitrary threads, and the sync ``handle_request`` path drives async handlers
    on a transient event loop per call (``asyncio.run`` or a ``ThreadRunner``
    side loop -- see ``EventManager._invoke_handler_from_sync``). An
    ``asyncio.Lock`` binds to the loop that first awaits it, so a waiter on a
    different loop raises RuntimeError -- or worse, two loops each construct
    their own notion of "held" and there is no exclusion at all -- and a bare
    dict registry races across threads. A ``threading.Lock`` has neither
    problem, and it may be released from a different thread than the one that
    acquired it, which the ThreadRunner dispatch pattern requires.

    Acquisition happens via ``to_thread`` so a waiting coroutine never blocks
    its event loop: two coroutines on ONE loop contending for a raw
    ``threading.Lock`` would otherwise deadlock the loop.

    Entries are refcounted and removed when the last holder or waiter checks
    in, so the registry does not grow with every key ever seen.
    """

    def __init__(self) -> None:
        # Guards the registry itself; each entry's lock guards the key's critical section.
        self._guard = threading.Lock()
        self._entries: dict[str, _KeyedMutexEntry] = {}

    @asynccontextmanager
    async def locked(self, key: str) -> AsyncIterator[None]:
        """Hold the key's lock for the duration of the ``async with`` block.

        Each waiter parks a worker in the shared ``to_thread`` pool for the whole
        time the current holder runs its critical section. That is acceptable here
        because waiters on one key are by definition duplicate requests for the
        same resource — bounded by how many components display one artifact — not
        general fan-out. If a caller ever serializes high-fan-in work on hot keys,
        it needs its own executor rather than this class as-is.

        Args:
            key: The identity to serialize on. Callers should canonicalize paths
                (``canonicalize_for_identity``) before using them as keys so two
                spellings of one file collide.
        """
        entry = self._checkout(key)
        # Set from inside the worker thread, immediately after the acquire
        # returns, so it is ground truth for whether WE hold the lock — the
        # only thing that decides whether the failure path below may release.
        # Releasing on any weaker evidence risks unlocking another holder's
        # critical section, which would silently break mutual exclusion.
        acquired = threading.Event()

        def acquire_and_record() -> None:
            entry.lock.acquire()
            acquired.set()

        try:
            await to_thread(acquire_and_record)
        except BaseException:
            # to_thread waits for its worker thread even when the awaiting
            # coroutine is cancelled, so by the time cancellation surfaces here
            # the acquire has normally completed — but only the flag knows for
            # sure (the task may also have died before the thread ran).
            if acquired.is_set():
                entry.lock.release()
            self._checkin(key)
            raise
        try:
            yield
        finally:
            entry.lock.release()
            self._checkin(key)

    def _checkout(self, key: str) -> _KeyedMutexEntry:
        """Get or create the key's entry and count this caller against it."""
        with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                entry = _KeyedMutexEntry()
                self._entries[key] = entry
            entry.refcount += 1
            return entry

    def _checkin(self, key: str) -> None:
        """Drop this caller's reference, discarding the entry at zero."""
        with self._guard:
            entry = self._entries[key]
            entry.refcount -= 1
            if entry.refcount == 0:
                del self._entries[key]
