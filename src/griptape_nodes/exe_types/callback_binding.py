"""Save node callbacks by method name and bind them on load.

A name resolves through ``getattr`` on the owning node, unguarded because every channel
carrying one is the workflow owner's own session: a saved workflow is a Python file the
engine executes, and an editor request arrives on that session's own connection.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.exe_types.node_types import BaseNode

logger = logging.getLogger("griptape_nodes")


def name_callback(callback: Any, owner: BaseNode | None) -> str | None:
    """Return the callback's name only when it resolves to the same method on ``owner``."""
    if callback is None or owner is None:
        return None
    bound_self = getattr(callback, "__self__", None)
    if bound_self is not owner:
        return None
    name = getattr(callback, "__name__", None)
    if name is None:
        return None
    if getattr(owner, name, None) != callback:
        return None
    return name


def resolve_callback(name: str, owner: BaseNode | None, *, described_as: str) -> Callable | None:
    """Resolve a saved method name, warning when its behavior cannot be restored."""
    if owner is None:
        logger.warning(
            "Attempted to restore %s, but the parameter is not attached to a node yet. "
            "The control will load without its behavior.",
            described_as,
        )
        return None
    candidate = getattr(owner, name, None)
    if candidate is None:
        logger.warning(
            "Attempted to restore %s from the method '%s' on node '%s'. "
            "That node has no method by that name, so the control will load without its "
            "behavior. This usually means the node was renamed or its library changed.",
            described_as,
            name,
            owner.name,
        )
        return None
    if not callable(candidate):
        logger.warning(
            "Attempted to restore %s from the method '%s' on node '%s'. "
            "That name holds a value rather than a method, so the control will load without "
            "its behavior.",
            described_as,
            name,
            owner.name,
        )
        return None
    return candidate
