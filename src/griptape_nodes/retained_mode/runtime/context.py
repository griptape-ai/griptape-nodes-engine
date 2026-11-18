"""The flow and node a caller entered in one open workflow with `with` blocks.

Saved workflow files, and the engine replaying serialized commands, send requests that name no flow
or node inside `with ContextManager().flow(...)` and `.node(...)` blocks. The blocks belong to the
caller: they live in a `ContextVar`, so they reach the tasks the caller starts inside them and
nothing else. Outside every block the current flow is the workflow's top-level flow, and no node is
current.

`ContextManager` re-exports these errors as `ContextManager.WorkflowContextError` and
`ContextManager.EmptyStackError`.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from griptape_nodes.exe_types.flow import ControlFlow
    from griptape_nodes.exe_types.node_types import BaseNode


class WorkflowContextError(Exception):
    """Base exception for workflow context errors."""


class EmptyStackError(WorkflowContextError):
    """No flow or node is current."""


@dataclass(frozen=True)
class _Frame:
    flow: ControlFlow
    node: BaseNode | None


class WorkflowContextStack:
    """The flow and node blocks the current caller has entered in one workflow."""

    def __init__(self, top_level_flow: Callable[[], ControlFlow | None]) -> None:
        """Make an empty context.

        Args:
            top_level_flow: Finds the workflow's top-level flow, the current flow outside every block.
        """
        self._top_level_flow = top_level_flow
        self._frames: contextvars.ContextVar[tuple[_Frame, ...]] = contextvars.ContextVar(
            "workflow_context_frames", default=()
        )

    @contextmanager
    def flow(self, flow: ControlFlow) -> Iterator[ControlFlow]:
        """Make `flow` current, with no node, for the length of a `with` block."""
        token = self._frames.set((*self._frames.get(), _Frame(flow=flow, node=None)))
        try:
            yield flow
        finally:
            self._frames.reset(token)

    @contextmanager
    def node(self, node: BaseNode) -> Iterator[BaseNode]:
        """Make `node` current, in the current flow, for the length of a `with` block.

        Raises:
            EmptyStackError: No flow is current.
        """
        frame = _Frame(flow=self.get_current_flow(), node=node)
        token = self._frames.set((*self._frames.get(), frame))
        try:
            yield node
        finally:
            self._frames.reset(token)

    def has_current_flow(self) -> bool:
        return self._current_flow_or_none() is not None

    def get_current_flow(self) -> ControlFlow:
        """The flow of the innermost block, else the workflow's top-level flow.

        Raises:
            EmptyStackError: No block is entered and the workflow has no top-level flow.
        """
        flow = self._current_flow_or_none()
        if flow is None:
            msg = "No flow is current: the workflow has no top-level flow."
            raise EmptyStackError(msg)
        return flow

    def has_current_node(self) -> bool:
        frames = self._frames.get()
        return bool(frames) and frames[-1].node is not None

    def get_current_node(self) -> BaseNode:
        """The node of the innermost block.

        Raises:
            EmptyStackError: The innermost block is a flow block, or no block is entered.
        """
        frames = self._frames.get()
        if not frames or frames[-1].node is None:
            msg = "No node is current: name the node, or send the request inside a node block."
            raise EmptyStackError(msg)
        return frames[-1].node

    def _current_flow_or_none(self) -> ControlFlow | None:
        frames = self._frames.get()
        if frames:
            return frames[-1].flow
        return self._top_level_flow()
