"""Keeps a test from starting real library worker processes unless it is marked as one that does.

A library whose nodes execute in a worker asks for that worker as soon as it finishes registering, so a
test that registers one through a real engine would otherwise start `python -m griptape_nodes.worker`.
Without the `worker_processes` mark the spawn is replaced by one that starts nothing and releases
whatever waits on it, so the library reads as having no worker yet, as it does before a worker connects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from griptape_nodes.retained_mode.managers.worker_manager import WorkerManager

if TYPE_CHECKING:
    from collections.abc import Iterator

WORKER_PROCESSES_MARK = "worker_processes"


@pytest.fixture(autouse=True)
def no_local_worker_processes(request: pytest.FixtureRequest) -> Iterator[list[str | None]]:
    """The worker keys a test asked to start, none of which started."""
    asked: list[str | None] = []
    if request.node.get_closest_marker(WORKER_PROCESSES_MARK) is not None:
        yield asked
        return

    async def start_nothing(manager: WorkerManager, _args: list[str], worker_key: str | None) -> None:
        asked.append(worker_key)
        manager.note_library_loaded(worker_key)

    with patch.object(WorkerManager, "_spawn_local_worker", start_nothing):
        yield asked
