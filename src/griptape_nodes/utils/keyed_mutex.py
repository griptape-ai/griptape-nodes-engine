"""Per-key mutual exclusion that works across threads and event loops."""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

# How long a waiter sleeps between acquisition attempts. Contention on a key means
# duplicate requests for one resource whose critical section does real work (file
# I/O, media decoding), so tens of milliseconds of wake-up latency is noise there,
# while the sleep keeps waiting nearly free for the event loop.
_ACQUIRE_POLL_INTERVAL_SECONDS = 0.02


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

    Waiters poll with a non-blocking acquire plus an async sleep rather than
    blocking in a thread. Parking each waiter in the shared ``to_thread`` pool
    would deadlock under exactly the fan-in this class exists to tame: enough
    same-key waiters exhaust the pool's bounded workers, and the HOLDER --
    which needs a worker from that same pool to run its critical section --
    can never finish to release them. Polling costs a waiter at most one
    interval of latency and consumes no thread at all. It also makes
    cancellation trivially safe: the lock is only ever taken by a synchronous
    successful acquire with no await between it and the try/finally that
    releases, so there is no window where cancellation can strand a held lock.

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

        Args:
            key: The identity to serialize on. Callers should canonicalize paths
                (``canonicalize_for_identity``) before using them as keys so two
                spellings of one file collide.
        """
        entry = self._checkout(key)
        try:
            # ASYNC110 wants an asyncio.Event here, but an asyncio primitive is
            # exactly what this class cannot use: waiters live on different event
            # loops (and threads), and an Event binds to one loop. See class docstring.
            while not entry.lock.acquire(blocking=False):  # noqa: ASYNC110
                await asyncio.sleep(_ACQUIRE_POLL_INTERVAL_SECONDS)
        except BaseException:
            # Cancellation can only surface at the sleep, where nothing is held.
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
