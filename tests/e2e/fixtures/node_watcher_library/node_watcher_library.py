"""An advanced library that watches every node run, the way a library author would.

Registered alongside the iterative group fixture nodes so a test can run a loop and read back what
the library's listeners heard.
"""

from __future__ import annotations

from typing import Any

from griptape_nodes.node_library.advanced_node_library import AdvancedNodeLibrary
from griptape_nodes.retained_mode.events.execution_events import NodeFinishProcessEvent, NodeStartProcessEvent


class NodeWatcherLibrary(AdvancedNodeLibrary):
    """Records every node start and finish it hears."""

    def __init__(self) -> None:
        self.heard: list[NodeStartProcessEvent | NodeFinishProcessEvent] = []

    def get_execution_event_listeners(self) -> list[tuple[type, Any]]:
        return [
            (NodeStartProcessEvent, self.heard.append),
            (NodeFinishProcessEvent, self.heard.append),
        ]
