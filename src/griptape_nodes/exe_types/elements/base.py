"""The element tree: identity, parentage, badges, and batched change reporting."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Self, TypeVar

from griptape_nodes.exe_types.elements.badge import VALID_BADGE_VARIANTS, BadgeData
from griptape_nodes.exe_types.elements.node_messages import NodeMessagePayload, NodeMessageResult

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

    from griptape_nodes.exe_types.elements.badge import BadgeVariantType
    from griptape_nodes.exe_types.node_types import BaseNode

logger = logging.getLogger("griptape_nodes")


N = TypeVar("N", bound="BaseNodeElement")


@dataclass(kw_only=True)
class BaseNodeElement:
    element_id: str = field(default_factory=lambda: str(uuid.uuid4().hex))
    element_type: str = field(default_factory=lambda: BaseNodeElement.__name__)
    name: str = field(default_factory=lambda: str(f"{BaseNodeElement.__name__}_{uuid.uuid4().hex}"))
    parent_group_name: str | None = None
    _changes: dict[str, Any] = field(default_factory=dict)

    _children: list[BaseNodeElement] = field(default_factory=list)
    _stack: ClassVar[list[BaseNodeElement]] = []
    _parent: BaseNodeElement | None = field(default=None)
    _node_context: BaseNode | None = field(default=None)
    _badge: BadgeData | None = field(default=None)

    @property
    def children(self) -> list[BaseNodeElement]:
        return self._children

    def __post_init__(self) -> None:
        # If there's currently an active element, add this new element as a child
        current = BaseNodeElement.get_current()
        if current is not None:
            current.add_child(self)

    def __enter__(self) -> Self:
        # Push this element onto the global stack
        BaseNodeElement._stack.append(self)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        exc_traceback: TracebackType | None,
    ) -> None:
        # Pop this element off the global stack
        popped = BaseNodeElement._stack.pop()
        if popped is not self:
            msg = f"Expected to pop {self}, but got {popped}"
            raise RuntimeError(msg)

    def __repr__(self) -> str:
        return f"BaseNodeElement({self.children=})"

    def get_changes(self) -> dict[str, Any]:
        return self._changes

    def track_change(self, key: str, value: Any) -> None:
        """Record a changed field and queue this element for the next batched UI update.

        A queue rather than a send: ``emit_parameter_changes()`` picks the element up later
        and reports every change recorded since the last flush.
        """
        self._changes[key] = value
        # Only when attached to a node and not already in the list (avoids duplicate events).
        if self._node_context is not None and self not in self._node_context._tracked_parameters:
            self._node_context._tracked_parameters.append(self)

    # --- Badge (discoverable by all subclasses) ---
    # Message types for frontend: clear_badge, get_badge, set_badge, clear_badge_display

    def get_badge(self) -> BadgeData | None:
        """Return current badge, or None if cleared; use .to_dict() when a serializable dict is needed."""
        return self._badge

    def set_badge(  # noqa: PLR0913
        self,
        variant: BadgeVariantType | None = None,
        title: str | None = None,
        message: str | None = None,
        *,
        icon: str | None = None,
        color: str | None = None,
        hide: bool | None = None,
        hide_clear_button: bool | None = None,
    ) -> None:
        """Set badge fields; only provided arguments are updated. No kwargs so badge is discoverable.

        color can be hex (e.g. "#3b82f6"), rgb (e.g. "rgb(59, 130, 246)"), etc.
        """
        if self._badge is None:
            self._badge = BadgeData()
        self._badge._parent_element = self
        if variant is not None:
            self._badge.variant = variant
        if title is not None:
            self._badge.title = title
        if message is not None:
            self._badge.message = message
        if icon is not None:
            self._badge.icon = icon
        if color is not None:
            self._badge.color = color
        if hide is not None:
            self._badge.hide = hide
        if hide_clear_button is not None:
            self._badge.hide_clear_button = hide_clear_button
        self.track_change("badge", self._badge.to_dict())

    def clear_badge(self) -> None:
        """Set badge to None (cleared)."""
        self._badge = None
        self.track_change("badge", None)

    def dismiss_badge(self) -> None:
        """Hide the badge indicator (hide=True). Frontend can send clear_badge_display to trigger this."""
        if self._badge is None:
            return
        self._badge.hide = True
        self.track_change("badge", self._badge.to_dict())

    @staticmethod
    def emits_update_on_write(func: Callable) -> Callable:
        """Decorator for properties that should track changes and emit events."""

        def wrapper(self: BaseNodeElement, *args, **kwargs) -> Callable:
            # For setters, track the change
            if len(args) >= 1:  # setter with value
                old_value = getattr(self, f"{func.__name__}", None) if hasattr(self, f"{func.__name__}") else None
                result = func(self, *args, **kwargs)
                new_value = getattr(self, f"{func.__name__}", None) if hasattr(self, f"{func.__name__}") else None
                # Track change if different
                if old_value != new_value:
                    self.track_change(func.__name__, new_value)
                return result
            return func(self, *args, **kwargs)

        return wrapper

    def _emit_alter_element_event_if_possible(self) -> None:
        """Emit an AlterElementEvent if we have node context and the necessary dependencies."""
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        if self._node_context is None:
            return

        # Import here to avoid circular dependencies

        from griptape_nodes.retained_mode.events.base_events import ExecutionEvent, ExecutionGriptapeNodeEvent
        from griptape_nodes.retained_mode.events.parameter_events import AlterElementEvent

        # Create base event data using the existing to_event method
        # Create a modified event data that only includes changed fields
        event_data = {
            # Include base fields that should always be present
            "element_id": self.element_id,
            "element_type": self.element_type,
            "name": self.name,
            "node_name": self._node_context.name,
        }
        # If ui_options changed, send the complete ui_options from to_dict()
        complete_dict = self.to_dict()
        if "ui_options" in complete_dict:
            self._changes["ui_options"] = complete_dict["ui_options"]
        # Also handle trait_ui_options for traits
        if "trait_ui_options" in complete_dict:
            self._changes["trait_ui_options"] = complete_dict["trait_ui_options"]
        if "badge" in complete_dict:
            self._changes["badge"] = complete_dict["badge"]

        event_data.update(self._changes)
        # Publish the event
        event = ExecutionGriptapeNodeEvent(
            wrapped_event=ExecutionEvent(payload=AlterElementEvent(element_details=event_data))
        )

        GriptapeNodes.EventManager().put_event(event)
        self._changes.clear()

    def to_dict(self) -> dict[str, Any]:
        """Returns a nested dictionary representation of this node and its children.

        Example:
            {
              "element_id": "container-1",
              "element_type": "ParameterGroup",
              "name": "Group 1",
              "children": [
                {
                    "element_id": "A",
                    "element_type": "Parameter",
                    "children": []
                },
                ...
              ]
            }
        """
        badge = self.get_badge()
        return {
            "element_id": self.element_id,
            "element_type": self.__class__.__name__,
            "parent_group_name": self.parent_group_name,
            "badge": badge.to_dict() if badge is not None else None,
            "children": [child.to_dict() for child in self._children],
        }

    def add_child(self, child: BaseNodeElement) -> None:
        if child._parent is not None:
            child._parent.remove_child(child)
        child._parent = self
        # Propagate node context to children
        child._node_context = self._node_context
        self._children.append(child)

        # Also propagate to any existing children of the child
        for grandchild in child.find_elements_by_type(BaseNodeElement, find_recursively=True):
            grandchild._node_context = self._node_context

        # Emit event if we have node context
        if self._node_context is not None:
            self._node_context._emit_parameter_lifecycle_event(child)

    def remove_child(self, child: BaseNodeElement | str) -> None:
        """Remove a child element from the hierarchy.

        This method recursively searches through the element hierarchy to find and remove
        the specified child. When the child is found in a descendant container (e.g., a
        ParameterList), it delegates to that container's remove_child() method to ensure
        proper cleanup and event handling (like marking parent nodes as unresolved).

        Args:
            child: The child element to remove, either as an object or by name string
        """
        ui_elements: list[BaseNodeElement] = [self]
        for ui_element in ui_elements:
            if child in ui_element._children:
                # Delegate to the actual parent container's remove_child method.
                # This ensures specialized containers (like ParameterList) can perform
                # their specific cleanup logic (e.g., marking parent nodes as unresolved).
                if ui_element is not self:
                    ui_element.remove_child(child)
                else:
                    # We are the direct parent, so handle removal directly
                    child._parent = None
                    ui_element._children.remove(child)
                break
            ui_elements.extend(ui_element._children)
        if self._node_context is not None and isinstance(child, BaseNodeElement):
            self._node_context._emit_parameter_lifecycle_event(child, remove=True)

    def find_element_by_id(self, element_id: str) -> BaseNodeElement | None:
        if self.element_id == element_id:
            return self

        for child in self._children:
            found = child.find_element_by_id(element_id)
            if found is not None:
                return found
        return None

    def find_element_by_name(self, element_name: str) -> BaseNodeElement | None:
        # Modified so ParameterGroups also just have name as a field.
        if self.name == element_name:
            return self
        for child in self._children:
            found = child.find_element_by_name(element_name)
            if found is not None:
                return found
        return None

    def find_elements_by_type(self, element_type: type[N], *, find_recursively: bool = True) -> list[N]:
        """Returns a list of child elements that are instances of type specified. Optionally do this recursively."""
        elements: list[N] = []
        for child in self._children:
            if isinstance(child, element_type):
                elements.append(child)
            if find_recursively:
                elements.extend(child.find_elements_by_type(element_type))
        return elements

    @classmethod
    def get_current(cls) -> BaseNodeElement | None:
        """Return the element on top of the stack, or None if no active element."""
        return cls._stack[-1] if cls._stack else None

    def to_event(self, node: BaseNode) -> dict:
        """Serializes the node element and its children into a dictionary representation.

        This method is used to create a data payload for AlterElementEvent to communicate changes or the current state of an element.
        The resulting dictionary includes the element's ID, type, name, the name of the
        provided BaseNode, and a recursively serialized list of its children.

        For new BaseNodeElement types that require different serialization logic and fields, this method should be overridden to provide the necessary data.

        Args:
            node: The BaseNode instance to which this element is associated.
                  Used to include the node's name in the event data.

        Returns:
            A dictionary containing the serialized data of the element and its children.
        """
        event_data = {
            "element_id": self.element_id,
            "element_type": self.element_type,
            "name": self.name,
            "node_name": node.name,
            "children": [child.to_event(node) for child in self.children],
        }
        return event_data

    def _apply_badge_from_message_data(self, data: dict) -> None:
        """Apply badge fields from a message data dict and track change."""
        if self._badge is None:
            self._badge = BadgeData()
        self._badge._parent_element = self
        if "variant" in data:
            val = data["variant"]
            if val in VALID_BADGE_VARIANTS:
                self._badge.variant = val
            else:
                msg = f"{self.__class__.__name__} received invalid badge variant {val}; using 'info'. Valid: {sorted(VALID_BADGE_VARIANTS)}"
                logger.error(msg)
                self._badge.variant = "info"
        if "title" in data:
            self._badge.title = data["title"]
        if "message" in data:
            self._badge.message = data["message"]
        if "icon" in data:
            self._badge.icon = data["icon"]
        if "color" in data:
            self._badge.color = data["color"]
        if "hide" in data:
            self._badge.hide = data["hide"]
        if "hide_clear_button" in data:
            self._badge.hide_clear_button = data["hide_clear_button"]
        self.track_change("badge", self._badge.to_dict())

    def _on_badge_message_received(
        self, message_type: str, message: NodeMessagePayload | None
    ) -> NodeMessageResult | None:
        """Handle badge-related messages; return result if handled, None otherwise."""
        msg_lower = message_type.lower()
        match msg_lower:
            case "clear_badge":
                self.clear_badge()
                return NodeMessageResult(
                    success=True,
                    details="Badge cleared",
                    response=None,
                    altered_workflow_state=False,
                )
            case "get_badge":
                badge = self.get_badge()
                badge_dict = badge.to_dict() if badge is not None else None
                return NodeMessageResult(
                    success=True,
                    details="Badge retrieved",
                    response=NodeMessagePayload(data=badge_dict),
                    altered_workflow_state=False,
                )
            case "set_badge":
                if message is not None and hasattr(message, "data") and isinstance(message.data, dict):
                    self._apply_badge_from_message_data(message.data)
                badge = self.get_badge()
                badge_dict = badge.to_dict() if badge is not None else None
                return NodeMessageResult(
                    success=True,
                    details="Badge updated",
                    response=NodeMessagePayload(data=badge_dict),
                    altered_workflow_state=False,
                )
            case "clear_badge_display":
                self.dismiss_badge()
                return NodeMessageResult(
                    success=True,
                    details="Badge dismissed",
                    response=None,
                    altered_workflow_state=False,
                )
            case _:
                # Not a badge message; return None so caller can delegate to other handlers (e.g. on_click).
                return None

    def on_message_received(self, message_type: str, message: NodeMessagePayload | None) -> NodeMessageResult | None:
        """Virtual method for handling messages sent to this element.

        Handles badge messages (clear_badge, get_badge, set_badge, clear_badge_display)
        on this element. Then attempts to delegate to child elements. If any child handles
        the message (returns non-None), that result is returned immediately.

        Args:
            message_type: String indicating the message type for parsing
            message: Message payload as NodeMessagePayload or None

        Returns:
            NodeMessageResult | None: Result if handled, None if no handler available
        """
        badge_result = self._on_badge_message_received(message_type, message)
        if badge_result is not None:
            return badge_result
        for child in self._children:
            result = child.on_message_received(message_type, message)
            if result is not None:
                return result
        return None

    def get_node(self) -> BaseNode | None:
        """Get the node context associated with this element.

        Returns:
            BaseNode | None: The parent node that owns this element, or None if no node context is set.
        """
        return self._node_context
