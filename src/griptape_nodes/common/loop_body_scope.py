"""Which canvas node each copy in a running loop body stands for.

A loop runs its body as renamed copies of the canvas nodes, plus a Start and an End node the engine
adds. `NodeExecutor` sets `current_loop_body_scope` around each iteration so the node events it
sends name the canvas node, and so it can leave the engine's own nodes out of them.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Collection, Iterator, Mapping


@dataclass(frozen=True)
class LoopBodyScope:
    """The copies a loop iteration runs, and which of them the artist placed.

    Args:
        canvas_node_names: Maps each copy's name to the name of the canvas node it stands for.
        engine_node_names: Names of the copies the engine added to the body, which stand for no
            node on the canvas.
    """

    canvas_node_names: Mapping[str, str]
    engine_node_names: frozenset[str]

    def canvas_name_for(self, node_name: str) -> str | None:
        """The canvas node a copy stands for, or None when the name isn't a copy in this body."""
        return self.canvas_node_names.get(node_name)

    def is_engine_node(self, node_name: str) -> bool:
        """Whether the node is one the engine added to the body."""
        return node_name in self.engine_node_names


current_loop_body_scope: ContextVar[LoopBodyScope | None] = ContextVar("current_loop_body_scope", default=None)


def loop_body_scope_for(
    node_name_mappings: Mapping[str, str],
    *,
    engine_made_node_names: Collection[str],
    enclosing_scope: LoopBodyScope | None,
) -> LoopBodyScope:
    """Build the scope for one deserialized loop body.

    Args:
        node_name_mappings: Maps each name in the packaged body to the name of its copy.
        engine_made_node_names: Names in the packaged body of the nodes the engine added.
        enclosing_scope: The scope of the loop iteration this loop is nested in, or None for a loop
            on the canvas.

    For a loop on the canvas, the packaged names are the artist's names. For a loop nested in
    another loop's body they are the outer iteration's copies, so they are resolved through the
    enclosing scope. A copy that can't be traced back to the canvas is left out.
    """
    canvas_node_names: dict[str, str] = {}
    engine_node_names: set[str] = set()
    for packaged_name, copy_name in node_name_mappings.items():
        if packaged_name in engine_made_node_names:
            engine_node_names.add(copy_name)
            continue
        if enclosing_scope is None:
            canvas_node_names[copy_name] = packaged_name
            continue
        canvas_name = enclosing_scope.canvas_name_for(packaged_name)
        if canvas_name is None:
            continue
        canvas_node_names[copy_name] = canvas_name
    return LoopBodyScope(canvas_node_names=canvas_node_names, engine_node_names=frozenset(engine_node_names))


@contextmanager
def entered_loop_body_scope(scope: LoopBodyScope) -> Iterator[None]:
    """Name the nodes run inside this block by the canvas nodes they stand for."""
    token = current_loop_body_scope.set(scope)
    try:
        yield
    finally:
        current_loop_body_scope.reset(token)
