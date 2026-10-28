"""Which node, if any, the current chain is creating or applying saved state to.

A workflow load, a paste and a worker rebuilding its copy of a node all create a node and then apply its saved
structure and values. The graph around that node is incomplete until they finish -- a load makes connections
before values, a paste after them, and a worker's copy has none -- so nothing decides anything by asking the
graph about the node meanwhile.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

_loading_node_name: ContextVar[str | None] = ContextVar("_node_loading_node_name", default=None)


@contextmanager
def node_loading(node_name: str) -> Iterator[None]:
    """Mark the enclosed block as creating `node_name`, or applying its saved state."""
    token = _loading_node_name.set(node_name)
    try:
        yield
    finally:
        _loading_node_name.reset(token)


def loading_node_name() -> str | None:
    """The node being created or having its saved state applied in this chain, or None."""
    return _loading_node_name.get()
