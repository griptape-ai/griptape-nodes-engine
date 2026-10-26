"""Advanced library that logs how long every node run takes, and how it ended.

The engine registers the listeners from `get_execution_event_listeners()` when this library
loads and removes them when it unloads.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from griptape_nodes.node_library.advanced_node_library import AdvancedNodeLibrary
from griptape_nodes.retained_mode.events.execution_events import NodeFinishProcessEvent, NodeStartProcessEvent

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.node_library.library_registry import Library, LibrarySchema

logger = logging.getLogger("griptape_nodes")


class NodeTimingLibrary(AdvancedNodeLibrary):
    def __init__(self) -> None:
        # Keyed by execution_id rather than node name: the same node can have several runs
        # going at once, for example inside a loop that runs all its items at once.
        self._started_at: dict[str | None, float] = {}
        # Listeners should return quickly, so the log line is written from a background thread.
        # This is where you would send the timing to your own monitoring instead.
        self._writer = ThreadPoolExecutor(max_workers=1)

    def get_execution_event_listeners(self) -> list[tuple[type, Callable]]:
        return [
            (NodeStartProcessEvent, self._on_start),
            (NodeFinishProcessEvent, self._on_finish),
        ]

    def before_library_unregistered(self, library_data: LibrarySchema, library: Library) -> None:
        self._writer.shutdown(wait=True)

    def _on_start(self, event: NodeStartProcessEvent) -> None:
        self._started_at[event.execution_id] = time.perf_counter()

    def _on_finish(self, event: NodeFinishProcessEvent) -> None:
        started_at = self._started_at.pop(event.execution_id, None)
        if started_at is None:
            return
        seconds = time.perf_counter() - started_at
        self._writer.submit(
            logger.info, "%s (%s) %s in %.2fs", event.node_name, event.node_type, event.outcome, seconds
        )
