"""A SubflowNode backed by a versioned live file with optional local overrides.

Extends SubflowNode with versioning, locking, and live-reference semantics.
The inner canvas behaves identically to SubflowNode until the user publishes,
at which point the node becomes locked (is_locked: true) and backed by a
versioned .py file on disk (live_path, live_version). Users can make local
overrides (is_locally_overridden: true) or create a plain editable copy.

Key metadata flags (read by the frontend to render UI state):
    is_live: True  -- always set; marks this as a Live Subflow (LIVE badge, file strip)
    is_locked: True  -- set after publish; shows Lock icon, blocks inner canvas
    live_version: "1.0"  -- current version string
    live_path: "/path/to/my_subflow_v1.0.py"  -- backing file
    is_locally_overridden: True  -- set after "Make Editable"; shows override banner
"""

from __future__ import annotations

from typing import Any

from griptape_nodes.exe_types.subflow_node import SubflowNode

IS_LOCKED_KEY = "is_locked"
IS_LIVE_KEY = "is_live"
LIVE_VERSION_KEY = "live_version"
LIVE_PATH_KEY = "live_path"
IS_LOCALLY_OVERRIDDEN_KEY = "is_locally_overridden"


class LiveSubflowNode(SubflowNode):
    """A SubflowNode that can be published with a version number and placed from the sidebar.

    Before publish: behaves exactly like SubflowNode (inner canvas editable, no lock).
    After publish: is_locked becomes True; the inner canvas is inaccessible unless
    the user explicitly makes it editable. Execution always runs the inner child flow
    when present, regardless of lock state (locking is an editing restriction, not
    a runtime restriction).
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)
        self.metadata[IS_LIVE_KEY] = True
