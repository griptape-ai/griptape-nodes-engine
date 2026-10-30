"""Waits the end-to-end tests share, for state the engine reaches on its own schedule."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from griptape_nodes.exe_types.node_types import NodeResolutionState

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine

_POLL_ATTEMPTS = 500
_POLL_SECONDS = 0.01


async def wait_until_resolving(engine: Engine, node_name: str) -> None:
    """Block until the node has entered execution, so whatever the test does next lands mid-run."""
    for _ in range(_POLL_ATTEMPTS):
        node = engine.node_manager.get_node_by_name(node_name)
        if node.state is NodeResolutionState.RESOLVING:
            return
        await asyncio.sleep(_POLL_SECONDS)
    msg = f"Node '{node_name}' never started executing, so the test could not act on it mid-run."
    raise AssertionError(msg)
