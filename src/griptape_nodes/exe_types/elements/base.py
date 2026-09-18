"""The element tree: identity, parentage, badges, and batched change reporting."""

from __future__ import annotations

import uuid
from abc import ABCMeta
from contextlib import contextmanager
from dataclasses import field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar, Self, TypeVar, dataclass_transform

import attrs

from griptape_nodes.exe_types.elements.badge import handle_badge_message, write_badge_fields

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from types import TracebackType

    from griptape_nodes.exe_types.elements.badge import BadgeData, BadgeVariantType
    from griptape_nodes.exe_types.elements.node_messages import NodeMessagePayload, NodeMessageResult
    from griptape_nodes.exe_types.node_types import BaseNode


N = TypeVar("N", bound="BaseNodeElement")

# Excludes element wiring from trait state.
WIRING_KEY = "wiring"
WIRING: Mapping[str, bool] = MappingProxyType({WIRING_KEY: True})

# Saves callbacks by owning-node method name rather than as data.
BEHAVIOR_KEY = "behavior"
BEHAVIOR: Mapping[str, bool] = MappingProxyType({BEHAVIOR_KEY: True})


def default_element_id(element_id: str | None) -> str:
    """Generate an ID for ``None``.

    Public because subclasses that fix ``element_id`` must retain this conversion.
    """
    if element_id is None:
        return uuid.uuid4().hex
    return element_id


def default_element_type(element_type: str | None) -> str:
    """Preserve the hand-written constructor's fallback element type."""
    # ``to_dict()`` reports the class name, but alter-element events read this attribute.
    if element_type is None:
        return "BaseNodeElement"
    return element_type


def default_element_name(name: str | None) -> str:
    """Generate an element name for ``None``."""
    if name is None:
        return f"BaseNodeElement_{uuid.uuid4().hex}"
    return name


# Runtime type returned by ``attrs.field()``.
_DECLARED_FIELD = type(attrs.field())


@dataclass_transform(field_specifiers=(field, attrs.field, attrs.Factory), eq_default=False, kw_only_default=True)
class ElementMeta(ABCMeta):
    """Apply attrs to every element class.

    Classes declaring fields get generated keyword-only constructors. Classes with explicit
    constructors keep them, and classes declaring neither inherit their parent's constructor.

    ``auto_attribs=False`` keeps bare annotations from becoming fields. ``slots=False`` lets
    elements hold non-field attributes. ``eq=False`` preserves identity comparison.
    """

    def __new__(cls, name: str, bases: tuple[type, ...], namespace: dict[str, Any], **kwargs: Any) -> ElementMeta:
        created = super().__new__(cls, name, bases, namespace, **kwargs)
        declares_fields = any(isinstance(value, _DECLARED_FIELD) for value in namespace.values())
        generate_init = declares_fields and "__init__" not in namespace
        return attrs.define(eq=False, slots=False, auto_attribs=False, kw_only=True, init=generate_init)(created)


class BaseNodeElement(metaclass=ElementMeta):
    """Base for parameters, groups, messages, and traits.

    Public fields convert ``None`` to their defaults so subclasses may forward optional
    arguments. Subclasses may use generated attrs constructors or define their own.
    """

    _stack: ClassVar[list[BaseNodeElement]] = []

    element_id: str = attrs.field(default=None, converter=default_element_id, metadata=WIRING)
    element_type: str = attrs.field(default=None, converter=default_element_type, metadata=WIRING)
    name: str = attrs.field(default=None, converter=default_element_name, metadata=WIRING)
    parent_group_name: str | None = attrs.field(default=None, metadata=WIRING)
    _changes: dict[str, Any] = attrs.field(factory=dict, init=False)
    _children: list[BaseNodeElement] = attrs.field(factory=list, init=False)
    _parent: BaseNodeElement | None = attrs.field(default=None, init=False)
    _node_context: BaseNode | None = attrs.field(default=None, init=False)
    _badge: BadgeData | None = attrs.field(default=None, init=False)

    def __attrs_post_init__(self) -> None:
        # Adopt only after construction so the parent sees a complete child.
        current = BaseNodeElement.get_current()
        if current is not None:
            current.add_child(self)

    @property
    def children(self) -> list[BaseNodeElement]:
        return self._children

    def __enter__(self) -> Self:
        # Push this element onto the global stack
        BaseNodeElement._stack.append(self)
        return self

    @staticmethod
    @contextmanager
    def detached() -> Iterator[None]:
        """Suppress adoption by an open element context.

        Mutate the stack in place so nested ``__exit__`` calls retain their references.
        """
        open_elements = BaseNodeElement._stack[:]
        BaseNodeElement._stack.clear()
        try:
            yield
        finally:
            BaseNodeElement._stack[:] = open_elements

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
        provided = {
            "variant": variant,
            "title": title,
            "message": message,
            "icon": icon,
            "color": color,
            "hide": hide,
            "hide_clear_button": hide_clear_button,
        }
        write_badge_fields(self, {name: value for name, value in provided.items() if value is not None})

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

    def _emit_alter_element_event_if_possible(self) -> None:
        """Emit an AlterElementEvent if we have node context and the necessary dependencies."""
        if self._node_context is None:
            return

        # Imported here to avoid circular dependencies: the event modules reach the element tree.
        from griptape_nodes.retained_mode.events.base_events import ExecutionEvent, ExecutionGriptapeNodeEvent
        from griptape_nodes.retained_mode.events.parameter_events import AlterElementEvent
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        event_data = {
            "element_id": self.element_id,
            "element_type": self.element_type,
            "name": self.name,
            "node_name": self._node_context.name,
        }
        # ui_options, trait_ui_options, and badge only report in full, so take them from to_dict().
        complete_dict = self.to_dict()
        for key in ("ui_options", "trait_ui_options", "badge"):
            if key in complete_dict:
                self._changes[key] = complete_dict[key]

        event_data.update(self._changes)
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
        badge_result = handle_badge_message(self, message_type, message)
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

    @staticmethod
    def emits_update_on_write(func: Callable) -> Callable:
        """Decorator for property setters that should track changes and emit events.

        Node libraries apply this as ``@BaseNodeElement.emits_update_on_write``.
        """

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
