"""The context stack of one open workflow: the flows, nodes, and elements entered in it.

`ContextManager` is the front that resolves which stack the current work means, and re-exports
these errors as `ContextManager.WorkflowContextError` and `ContextManager.EmptyStackError`.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from griptape_nodes.exe_types.core_types import BaseNodeElement
    from griptape_nodes.exe_types.flow import ControlFlow
    from griptape_nodes.exe_types.node_types import BaseNode


class WorkflowContextError(Exception):
    """Base exception for workflow context errors."""


class EmptyStackError(WorkflowContextError):
    """Empty stack error."""


class WorkflowContextStack:
    """Internal class that represents a Workflow's state which owns a stack of flow names.

    Holds no workflow identity: the runtime it belongs to is the workflow.
    """

    _flow_stack: list[FlowContextState]
    _task_local_flow_stack: contextvars.ContextVar[tuple[FlowContextState, ...] | None]

    def __init__(self) -> None:
        self._flow_stack = []
        self._task_local_flow_stack = contextvars.ContextVar("task_local_flow_stack", default=None)

    @contextmanager
    def task_local_flow_stack(self) -> Iterator[None]:
        """Give the current task its own copy of the flow stack for the `with` block.

        The copy starts as a snapshot of the current stack, so flows already entered
        stay visible. Tasks spawned inside the block share the copied node and element
        states, so their node and element pushes are visible to this task; flow pushes
        and pops stay per task.
        """
        snapshot = tuple(state.copy() for state in self._flow_states())
        token = self._task_local_flow_stack.set(snapshot)
        try:
            yield
        finally:
            self._task_local_flow_stack.reset(token)

    def push_flow(self, flow: ControlFlow) -> ControlFlow:
        """Push a flow onto this workflow's flow stack."""
        flow_context = FlowContextState(flow)
        overlay = self._task_local_flow_stack.get()
        if overlay is None:
            self._flow_stack.append(flow_context)
        else:
            self._task_local_flow_stack.set((*overlay, flow_context))
        return flow

    def pop_flow(self) -> ControlFlow:
        """Pop the top flow from this workflow's flow stack."""
        overlay = self._task_local_flow_stack.get()
        if overlay is None:
            if not self._flow_stack:
                msg = "Cannot pop Flow: no active Flows in Workflow"
                raise EmptyStackError(msg)
            return self._flow_stack.pop()._flow

        if not overlay:
            msg = "Cannot pop Flow: no active Flows in Workflow"
            raise EmptyStackError(msg)
        self._task_local_flow_stack.set(overlay[:-1])
        return overlay[-1]._flow

    def enter_flow(self, flow: ControlFlow) -> Callable[[], None]:
        """Push a flow for a `with` block and return the action that exits it.

        On a task-local stack, exiting restores the stack from before the push, dropping
        any bare pushes made since. On the shared stack it pops, since restoring would
        discard flows other tasks pushed meanwhile.
        """
        overlay = self._task_local_flow_stack.get()
        self.push_flow(flow)
        if overlay is None:
            return self._exit_shared_flow

        def exit_flow() -> None:
            self._task_local_flow_stack.set(overlay)

        return exit_flow

    @contextmanager
    def flow(self, flow: ControlFlow) -> Iterator[ControlFlow]:
        """Enter `flow` for the length of a `with` block."""
        exit_flow = self.enter_flow(flow)
        try:
            yield flow
        finally:
            exit_flow()

    def push_node(self, node: BaseNode) -> BaseNode:
        """Enter `node` in the current flow.

        Raises:
            EmptyStackError: No flow is entered.
        """
        states = self._flow_states()
        if not states:
            msg = f"Cannot enter Node '{node.name}': no active Flow in Workflow"
            raise EmptyStackError(msg)
        return states[-1].push_node(node)

    def pop_node(self) -> BaseNode:
        """Leave the current flow's current node.

        Raises:
            EmptyStackError: No flow is entered, or the current flow has no node entered.
        """
        states = self._flow_states()
        if not states:
            msg = "Cannot pop Node: no active Flow in Workflow"
            raise EmptyStackError(msg)
        return states[-1].pop_node()

    @contextmanager
    def node(self, node: BaseNode) -> Iterator[BaseNode]:
        """Enter `node` in the current flow for the length of a `with` block.

        Raises:
            EmptyStackError: No flow is entered.
        """
        self.push_node(node)
        # Leave the node in the flow it entered, even if the block changed which flow is current.
        flow_context = self.current_flow_state()
        try:
            yield node
        finally:
            flow_context.pop_node()

    def has_current_flow(self) -> bool:
        """Check if this workflow has an active flow."""
        return len(self._flow_states()) > 0

    def get_current_flow(self) -> ControlFlow:
        """Get the current flow in this workflow."""
        return self.current_flow_state()._flow

    def current_flow_state(self) -> FlowContextState:
        """Get the state of the current flow in this workflow.

        Raises:
            EmptyStackError: No flow is entered.
        """
        states = self._flow_states()
        if not states:
            msg = "No active Flow in Workflow"
            raise EmptyStackError(msg)

        return states[-1]

    def has_current_node(self) -> bool:
        """Check if the current flow in this workflow has an active node."""
        states = self._flow_states()
        if not states:
            return False
        return states[-1].has_current_node()

    def get_current_node(self) -> BaseNode:
        """Get the current node in the current flow of this workflow."""
        return self.current_flow_state().get_current_node()

    def _exit_shared_flow(self) -> None:
        if not self._flow_stack:
            msg = "Cannot pop Flow: no active Flows in Workflow"
            raise EmptyStackError(msg)
        self._flow_stack.pop()

    def _flow_states(self) -> Sequence[FlowContextState]:
        overlay = self._task_local_flow_stack.get()
        if overlay is None:
            return self._flow_stack
        return overlay


class FlowContextState:
    """Internal class that represents a Flow's state which owns a stack of node names."""

    _flow: ControlFlow
    _node_stack: list[NodeContextState]

    def __init__(self, flow: ControlFlow):
        self._flow = flow
        self._node_stack = []

    def copy(self) -> FlowContextState:
        """Copy this flow state with its own node stack of copied node states."""
        flow_copy = FlowContextState(self._flow)
        flow_copy._node_stack = [node_state.copy() for node_state in self._node_stack]
        return flow_copy

    def push_node(self, node: BaseNode) -> BaseNode:
        """Push a node name onto this flow's node stack."""
        node_context = NodeContextState(node)
        self._node_stack.append(node_context)
        return node

    def pop_node(self) -> BaseNode:
        """Pop the top node from this flow's node stack."""
        if not self._node_stack:
            msg = f"Cannot pop Node: no active Nodes in Flow '{self._flow.name}'"
            raise EmptyStackError(msg)

        node_context = self._node_stack.pop()
        return node_context.node

    def get_current_node(self) -> BaseNode:
        """Get the name of the current node in this flow."""
        if not self._node_stack:
            msg = f"No active Node in Flow '{self._flow.name}'"
            raise EmptyStackError(msg)

        node_context = self._node_stack[-1]
        return node_context.node

    def has_current_node(self) -> bool:
        """Check if this flow has an active node."""
        return len(self._node_stack) > 0


class NodeContextState:
    """Internal class that represents a Node's state which owns a stack of node elements."""

    node: BaseNode
    _element_stack: list[BaseNodeElement]

    def __init__(self, node: BaseNode):
        self.node = node
        self._element_stack = []

    def copy(self) -> NodeContextState:
        """Copy this node state with its own element stack."""
        node_copy = NodeContextState(self.node)
        node_copy._element_stack = list(self._element_stack)
        return node_copy

    def push_element(self, element: BaseNodeElement) -> BaseNodeElement:
        """Push an element name onto this node's element stack."""
        self._element_stack.append(element)
        return element

    def pop_element(self) -> BaseNodeElement:
        """Pop the top element from this node's element stack."""
        if not self._element_stack:
            msg = f"Cannot pop Element: no active Elements in Node '{self.node.name}'"
            raise EmptyStackError(msg)

        element = self._element_stack.pop()
        return element

    def get_current_element(self) -> BaseNodeElement:
        """Get the name of the current element in this node."""
        if not self._element_stack:
            msg = f"No active Element in Node '{self.node.name}'"
            raise EmptyStackError(msg)

        return self._element_stack[-1]

    def has_current_element(self) -> bool:
        """Check if this node has an active element."""
        return len(self._element_stack) > 0
