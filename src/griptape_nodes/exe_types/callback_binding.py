"""Naming and re-binding the callbacks attached to a parameter's elements.

A callback cannot be written to a file. What can be written is the name of a method on
the node that owns the parameter, which a later load resolves against that node.

Only a bound method of the owning node can be named. That covers the ordinary case, where
a node hands one of its own methods to a trait, and it deliberately excludes a lambda or a
closure: those have no name to resolve, and inventing one would restore a callback the node
never declared. Resolution is a ``getattr`` on the node, so a saved file can only ever name
something that node already provides.

A callback a trait derives from its own state is not this module's concern. The trait saves
the state and rebuilds the callback from it, so it never offers one here to be named.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.exe_types.node_types import BaseNode

logger = logging.getLogger("griptape_nodes")


def name_callback(callback: Any, owner: BaseNode | None) -> str | None:
    """Return the method name that would resolve back to ``callback`` on ``owner``.

    Returns None when the callback is not a bound method of ``owner``, which means it
    cannot be saved. Callers report that; this function does not, because it is also used
    to ask whether a callback is nameable at all.
    """
    if callback is None or owner is None:
        return None
    bound_self = getattr(callback, "__self__", None)
    if bound_self is not owner:
        return None
    name = getattr(callback, "__name__", None)
    if name is None:
        return None
    # A method reached under a different attribute name would resolve to the wrong thing.
    if getattr(owner, name, None) != callback:
        return None
    return name


def resolve_callback(name: str, owner: BaseNode | None, *, described_as: str) -> Callable | None:
    """Return ``owner``'s method called ``name``, or None with a warning if there isn't one.

    ``described_as`` names what is being restored so the warning tells the user which
    control on which node stopped working.
    """
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
