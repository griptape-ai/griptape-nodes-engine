"""The context stack of one open workflow: the flows, nodes, and elements entered in it.

`ContextManager` is the front that resolves which stack the current work means, and re-exports
these errors as `ContextManager.WorkflowContextError` and `ContextManager.EmptyStackError`.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

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

    def __init__(self) -> None:
        self._flow_stack = []

    def push_flow(self, flow: ControlFlow) -> ControlFlow:
        """Push a flow name onto this workflow's flow stack."""
        flow_context = FlowContextState(flow)
        self._flow_stack.append(flow_context)
        return flow

    def pop_flow(self) -> ControlFlow:
        """Pop the top flow from this workflow's flow stack."""
        if not self._flow_stack:
            msg = "Cannot pop Flow: no active Flows in Workflow"
            raise EmptyStackError(msg)

        flow_context = self._flow_stack.pop()
        return flow_context._flow

    @contextmanager
    def flow(self, flow: ControlFlow) -> Iterator[ControlFlow]:
        """Enter `flow` for the length of a `with` block."""
        self.push_flow(flow)
        try:
            yield flow
        finally:
            self.pop_flow()

    @contextmanager
    def node(self, node: BaseNode) -> Iterator[BaseNode]:
        """Enter `node` in the current flow for the length of a `with` block.

        Raises:
            EmptyStackError: No flow is entered.
        """
        if not self._flow_stack:
            msg = f"Cannot enter Node '{node.name}': no active Flow in Workflow"
            raise EmptyStackError(msg)
        flow_context = self._flow_stack[-1]
        flow_context.push_node(node)
        try:
            yield node
        finally:
            flow_context.pop_node()

    def has_current_flow(self) -> bool:
        """Check if this workflow has an active flow."""
        return len(self._flow_stack) > 0

    def get_current_flow(self) -> ControlFlow:
        """Get the current flow in this workflow."""
        if not self._flow_stack:
            msg = "No active Flow in Workflow"
            raise EmptyStackError(msg)

        flow_context = self._flow_stack[-1]
        return flow_context._flow

    def has_current_node(self) -> bool:
        """Check if the current flow in this workflow has an active node."""
        if not self._flow_stack:
            return False
        return self._flow_stack[-1].has_current_node()

    def get_current_node(self) -> BaseNode:
        """Get the current node in the current flow of this workflow."""
        if not self._flow_stack:
            msg = "No active Flow in Workflow"
            raise EmptyStackError(msg)
        return self._flow_stack[-1].get_current_node()


class FlowContextState:
    """Internal class that represents a Flow's state which owns a stack of node names."""

    _flow: ControlFlow
    _node_stack: list[NodeContextState]

    def __init__(self, flow: ControlFlow):
        self._flow = flow
        self._node_stack = []

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
