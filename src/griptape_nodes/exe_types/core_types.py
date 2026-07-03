from __future__ import annotations

import logging
import uuid
import warnings
from abc import ABC, abstractmethod
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum, StrEnum, auto
from typing import TYPE_CHECKING, Any, ClassVar, Literal, NamedTuple, Self, TypeVar, get_args

from pydantic import BaseModel

logger = logging.getLogger("griptape_nodes")


class NodeMessagePayload(BaseModel):
    """Structured payload for node messages.

    This replaces the use of Any in message payloads, providing
    better type safety and validation for node message handling.
    """

    data: Any = None


class NodeMessageResult(BaseModel):
    """Result from a node message callback.

    Attributes:
        success: True if the message was handled successfully, False otherwise
        details: Human-readable description of what happened
        response: Optional response data to return to the sender
        altered_workflow_state: True if the message handling altered workflow state.
            Clients can use this to determine if the workflow needs to be re-saved.
    """

    success: bool
    details: str
    response: NodeMessagePayload | None = None
    altered_workflow_state: bool = True


if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

    from griptape_nodes.exe_types.node_types import BaseNode

# Type alias for element message callback functions
type ElementMessageCallback = Callable[[str, "NodeMessagePayload | None"], "NodeMessageResult"]

T = TypeVar("T", bound="Parameter")
N = TypeVar("N", bound="BaseNodeElement")

# Badge variant type for element badge (aligned with ParameterMessage.VariantType, excluding "none")
BadgeVariantType = Literal["info", "warning", "error", "success", "tip", "link", "docs", "help", "note", "cloud-upload"]
VALID_BADGE_VARIANTS: frozenset[str] = frozenset(get_args(BadgeVariantType))

# Parameter render location for control parameters - controls where they appear in the node UI
# "top": render at the top (default), "bottom": render at the bottom, "in-order": render in-line with other parameters
ParameterRenderLocation = Literal["top", "bottom", "in-order"]
VALID_PARAMETER_RENDER_LOCATIONS: frozenset[str] = frozenset(get_args(ParameterRenderLocation))


@dataclass
class BadgeData:
    """Serializable badge data for BaseNodeElement.

    Used to display badge indicators (info, warning, error, success, etc.) on
    parameters and groups. All subclasses of BaseNodeElement inherit badge
    and can use get_badge(), set_badge(), clear_badge(), and dismiss_badge().

    Attributes:
        variant: Badge style (e.g. info, warning, error, success).
        title: Optional short title for the badge.
        message: Badge message body.
        icon: Optional Lucide icon name (e.g. "upload-cloud"); when set, overrides variant's default icon.
        color: Optional badge color; can be hex (e.g. "#3b82f6"), rgb (e.g. "rgb(59, 130, 246)"), etc. Overrides variant's default.
        hide: When True, the badge indicator is hidden in the UI.
        hide_clear_button: When True, the clear/dismiss button is hidden; when False,
            the button is shown so the user can dismiss the badge (e.g. via clear_badge_display).
    """

    variant: BadgeVariantType = "info"
    title: str | None = None
    message: str = ""
    icon: str | None = None
    color: str | None = None
    hide: bool = False
    hide_clear_button: bool = True

    _parent_element: Any = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable dictionary suitable for events and element serialization."""
        result: dict[str, Any] = {
            "variant": self.variant,
            "title": self.title,
            "message": self.message,
            "hide": self.hide,
            "hide_clear_button": self.hide_clear_button,
        }
        if self.icon is not None:
            result["icon"] = self.icon
        if self.color is not None:
            result["color"] = self.color
        return result


# Types of Modes provided for Parameters
class ParameterMode(Enum):
    OUTPUT = auto()
    INPUT = auto()
    PROPERTY = auto()


class ParameterTypeBuiltin(StrEnum):
    STR = "str"
    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    ANY = "any"
    NONE = "none"
    CONTROL_TYPE = "parametercontroltype"
    ALL = "all"


class ParameterType:
    class KeyValueTypePair(NamedTuple):
        """A named tuple for storing a pair of types for key-value parameters.

        Fields:
            key_type: The type of the key
            value_type: The type of the value
        """

        key_type: str
        value_type: str

    _builtin_aliases: ClassVar[dict] = {
        "str": ParameterTypeBuiltin.STR,
        "string": ParameterTypeBuiltin.STR,
        "bool": ParameterTypeBuiltin.BOOL,
        "boolean": ParameterTypeBuiltin.BOOL,
        "int": ParameterTypeBuiltin.INT,
        "float": ParameterTypeBuiltin.FLOAT,
        "any": ParameterTypeBuiltin.ANY,
        "none": ParameterTypeBuiltin.NONE,
        "parametercontroltype": ParameterTypeBuiltin.CONTROL_TYPE,
        "all": ParameterTypeBuiltin.ALL,
    }

    @staticmethod
    def attempt_get_builtin(type_name: str) -> ParameterTypeBuiltin | None:
        ret_val = ParameterType._builtin_aliases.get(type_name.lower())
        return ret_val

    @staticmethod
    def _extract_base_type(type_str: str) -> str:
        """Extract the base type from a potentially generic type string.

        Examples:
            'list[any]' -> 'list'
            'dict[str, int]' -> 'dict'
            'str' -> 'str'
        """
        bracket_index = type_str.find("[")
        if bracket_index == -1:
            return type_str
        return type_str[:bracket_index]

    @staticmethod
    def are_types_compatible(source_type: str | None, target_type: str | None) -> bool:  # noqa: PLR0911
        if source_type is None or target_type is None:
            return False

        source_type_lower = source_type.lower()
        target_type_lower = target_type.lower()

        # If either are None, bail.
        if ParameterTypeBuiltin.NONE.value in (source_type_lower, target_type_lower):
            return False
        if target_type_lower == ParameterTypeBuiltin.ANY.value:
            # If the TARGET accepts Any, we're good. Not always true the other way 'round.
            return True

        # First try exact match
        if source_type_lower == target_type_lower:
            return True

        source_base = ParameterType._extract_base_type(source_type_lower)
        target_base = ParameterType._extract_base_type(target_type_lower)

        # If base types match
        if source_base == target_base:
            # Allow any generic to flow to base type (list[any] -> list, list[str] -> list)
            if target_type_lower == target_base:
                return True

            # Allow specific types to flow to [any] generic (list[str] -> list[any])
            if target_type_lower == f"{target_base}[{ParameterTypeBuiltin.ANY.value}]":
                return True

        return False

    @staticmethod
    def parse_kv_type_pair(type_str: str) -> KeyValueTypePair | None:  # noqa: C901
        """Parse a string that potentially defines a Key-Value Type Pair.

        Args:
            type_str: A string like "[str, int]" or "[dict[str, bool], list[float]]"

        Returns:
            A KeyValueTypePair object if valid KV pair format, or None if not a KV pair

        Raises:
            ValueError: If the string appears to be a KV pair but is malformed
        """
        # Remove any whitespace
        type_str = type_str.strip()

        # Check if it starts with '[' and ends with ']'
        if not (type_str.startswith("[") and type_str.endswith("]")):
            return None  # Not a KV pair, just a regular type

        # Remove the outer brackets
        inner_content = type_str[1:-1].strip()

        # Now we need to find the comma that separates key type from value type
        # This is tricky because we might have nested structures with commas

        # Keep track of nesting level with different brackets
        bracket_stack = []
        comma_positions = []

        for i, char in enumerate(inner_content):
            if char in "[{(":
                bracket_stack.append(char)
            elif char in "]})":
                if bracket_stack:  # Ensure stack isn't empty
                    bracket_stack.pop()
                else:
                    # Unmatched closing bracket
                    err_str = f"Unmatched closing bracket at position {i} in '{type_str}'."
                    raise ValueError(err_str)
            elif char == "," and not bracket_stack:
                # This is a top-level comma
                comma_positions.append(i)

        # Check for unclosed brackets
        if bracket_stack:
            err_str = f"Unclosed brackets in '{type_str}'."
            raise ValueError(err_str)

        # We should have exactly one top-level comma
        if len(comma_positions) != 1:
            err_str = (
                f"Missing comma separator in '{type_str}'."
                if len(comma_positions) == 0
                else f"Too many comma separators in '{type_str}'."
            )
            raise ValueError(err_str)

        # Split at the comma
        key_type = inner_content[: comma_positions[0]].strip()
        value_type = inner_content[comma_positions[0] + 1 :].strip()

        # Validate that both parts are not empty
        if not key_type:
            err_str = f"Empty key type in '{type_str}'."
            raise ValueError(err_str)
        if not value_type:
            err_str = f"Empty value type in '{type_str}'."
            raise ValueError(err_str)

        return ParameterType.KeyValueTypePair(key_type=key_type, value_type=value_type)


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
        self._changes["badge"] = self._badge.to_dict()
        # Batch UI updates: add to node's tracked list so emit_parameter_changes() sends our _changes later.
        # Only when attached to a node and not already in the list (avoids duplicate events).
        if self._node_context is not None and self not in self._node_context._tracked_parameters:
            self._node_context._tracked_parameters.append(self)

    def clear_badge(self) -> None:
        """Set badge to None (cleared)."""
        self._badge = None
        self._changes["badge"] = None
        # Batch UI updates: add to node's tracked list so emit_parameter_changes() sends our _changes later.
        # Only when attached to a node and not already in the list (avoids duplicate events).
        if self._node_context is not None and self not in self._node_context._tracked_parameters:
            self._node_context._tracked_parameters.append(self)

    def dismiss_badge(self) -> None:
        """Hide the badge indicator (hide=True). Frontend can send clear_badge_display to trigger this."""
        if self._badge is None:
            return
        self._badge.hide = True
        self._changes["badge"] = self._badge.to_dict()
        # Batch UI updates: add to node's tracked list so emit_parameter_changes() sends our _changes later.
        # Only when attached to a node and not already in the list (avoids duplicate events).
        if self._node_context is not None and self not in self._node_context._tracked_parameters:
            self._node_context._tracked_parameters.append(self)

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
                    self._changes[func.__name__] = new_value
                    # Batch UI updates: add to node's tracked list so emit_parameter_changes() sends our _changes later.
                    # Only when attached to a node and not already in the list (avoids duplicate events).
                    if self._node_context is not None and self not in self._node_context._tracked_parameters:
                        self._node_context._tracked_parameters.append(self)
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

    def _apply_badge_from_message_data(self, data: dict) -> None:  # noqa: C901
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
        self._changes["badge"] = self._badge.to_dict()
        # Batch UI updates: add to node's tracked list so emit_parameter_changes() sends our _changes later.
        # Only when attached to a node and not already in the list (avoids duplicate events).
        if self._node_context is not None and self not in self._node_context._tracked_parameters:
            self._node_context._tracked_parameters.append(self)

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


class UIOptionsMixin:
    """Mixin providing UI options update functionality for classes with ui_options."""

    def _validate_ui_option_conflict(
        self,
        ui_options_dict: dict,
        param_name: str,
        param_value: Any,
    ) -> None:
        """Validate that explicit parameter doesn't conflict with ui_options dict.

        Logs a warning if there's a conflict and the ui_options value will be used.

        Args:
            ui_options_dict: The ui_options dictionary to check
            param_name: Name of the parameter (e.g., "hide", "markdown")
            param_value: Value of the explicit parameter
        """
        if param_name not in ui_options_dict:
            return

        dict_value = ui_options_dict[param_name]

        if param_value != dict_value:
            # Get element name for better error messages
            element_name = getattr(self, "name", None)
            class_name = self.__class__.__name__

            # Build element part
            if element_name:
                element_part = f"{class_name} '{element_name}'"
            else:
                element_part = class_name

            msg = (
                f"{element_part}: Conflicting values for '{param_name}'. "
                f'Explicit parameter {param_name}={param_value!r} conflicts with ui_options["{param_name}"]={dict_value!r}. '
                f"The value from ui_options will be used. Please contact the library author to fix this issue."
            )
            logger.warning(msg)

    def update_ui_options_key(self, key: str, value: Any) -> None:
        """Update a single UI option key."""
        ui_options = self.ui_options
        ui_options[key] = value
        self.ui_options = ui_options

    def update_ui_options(self, updates: dict[str, Any]) -> None:
        """Update multiple UI options at once."""
        ui_options = self.ui_options
        ui_options.update(updates)
        self.ui_options = ui_options


class ParameterMessage(BaseNodeElement, UIOptionsMixin):
    """Represents a UI message element, such as a warning or informational text."""

    # Define default titles as a class-level constant
    DEFAULT_TITLES: ClassVar[dict[str, str]] = {
        "info": "Info",
        "warning": "Warning",
        "error": "Error",
        "success": "Success",
        "tip": "Tip",
        "link": "Link",
        "docs": "Documentation",
        "help": "Help",
        "note": "Note",
        "cloud-upload": "Upload",
        "none": "",
    }

    # Define default icons as a class-level constant (based on Lucide icons)
    DEFAULT_ICONS: ClassVar[dict[str, str]] = {
        "info": "info",
        "warning": "alert-triangle",
        "error": "x-circle",
        "success": "check-circle",
        "tip": "lightbulb",
        "link": "external-link",
        "docs": "book-open",
        "help": "help-circle",
        "note": "sticky-note",
        "cloud-upload": "cloud-upload",
        "none": "",
    }

    # Create a type alias using the keys from DEFAULT_TITLES
    type VariantType = Literal[
        "info", "warning", "error", "success", "tip", "link", "docs", "help", "note", "cloud-upload", "none"
    ]
    type ButtonAlignType = Literal["full-width", "left", "center", "right"]
    type ButtonVariantType = Literal["default", "destructive", "outline", "secondary", "ghost", "link"]

    element_type: str = field(default_factory=lambda: ParameterMessage.__name__)
    _variant: VariantType = field(init=False)
    _title: str | None = field(default=None, init=False)
    _value: str = field(init=False)
    _message_icon: str | None = field(default="__DEFAULT__", init=False)
    _button_link: str | None = field(default=None, init=False)
    _button_text: str | None = field(default=None, init=False)
    _button_icon: str | None = field(default=None, init=False)
    _button_variant: ButtonVariantType = field(default="outline", init=False)
    _button_align: ButtonAlignType = field(default="full-width", init=False)
    _full_width: bool = field(default=False, init=False)
    _ui_options: dict = field(default_factory=dict, init=False)

    def __init__(  # noqa: PLR0913
        self,
        variant: VariantType,
        value: str,
        *,
        title: str | None = None,
        message_icon: str | None = "__DEFAULT__",
        button_link: str | None = None,
        button_text: str | None = None,
        button_icon: str | None = None,
        button_variant: ButtonVariantType = "outline",
        button_align: ButtonAlignType = "full-width",
        full_width: bool = False,
        markdown: bool | None = None,
        hide: bool | None = None,
        ui_options: dict | None = None,
        traits: set[Trait.__class__ | Trait] | None = None,
        **kwargs,
    ):
        # Remove markdown and hide from kwargs to prevent passing them to parent class
        kwargs.pop("markdown", None)
        kwargs.pop("hide", None)
        super().__init__(element_type=ParameterMessage.__name__, **kwargs)
        self._variant = variant
        self._title = title
        self._value = value
        self._message_icon = message_icon
        self._button_link = button_link
        self._button_text = button_text
        self._button_icon = button_icon
        self._button_variant = button_variant
        self._button_align = button_align
        self._full_width = full_width
        self._ui_options = ui_options or {}

        # Validate that explicit parameters don't conflict with ui_options (only if not None)
        if markdown is not None:
            self._validate_ui_option_conflict(
                ui_options_dict=self._ui_options, param_name="markdown", param_value=markdown
            )
        if hide is not None:
            self._validate_ui_option_conflict(ui_options_dict=self._ui_options, param_name="hide", param_value=hide)

        # Add common UI options if explicitly provided (not None) and NOT already in ui_options
        # (ui_options always wins in case of conflict)
        if markdown is not None and "markdown" not in self._ui_options:
            self._ui_options["markdown"] = markdown
        if hide is not None and "hide" not in self._ui_options:
            self._ui_options["hide"] = hide

        # Handle traits if provided
        if traits:
            for trait in traits:
                if isinstance(trait, type):
                    # It's a trait class, instantiate it
                    trait_instance = trait()
                else:
                    # It's already a trait instance
                    trait_instance = trait
                self.add_child(trait_instance)

    @property
    def variant(self) -> VariantType:
        return self._variant

    @variant.setter
    @BaseNodeElement.emits_update_on_write
    def variant(self, value: VariantType) -> None:
        self._variant = value

    @property
    def title(self) -> str | None:
        return self._title

    @title.setter
    @BaseNodeElement.emits_update_on_write
    def title(self, value: str | None) -> None:
        self._title = value

    @property
    def value(self) -> str:
        return self._value

    @value.setter
    @BaseNodeElement.emits_update_on_write
    def value(self, value: str) -> None:
        self._value = value

    @property
    def button_link(self) -> str | None:
        return self._button_link

    @button_link.setter
    @BaseNodeElement.emits_update_on_write
    def button_link(self, value: str | None) -> None:
        self._button_link = value

    @property
    def button_text(self) -> str | None:
        return self._button_text

    @button_text.setter
    @BaseNodeElement.emits_update_on_write
    def button_text(self, value: str | None) -> None:
        self._button_text = value

    @property
    def full_width(self) -> bool:
        return self._full_width

    @full_width.setter
    @BaseNodeElement.emits_update_on_write
    def full_width(self, value: bool) -> None:
        self._full_width = value

    @property
    def message_icon(self) -> str | None:
        return self._message_icon

    @message_icon.setter
    @BaseNodeElement.emits_update_on_write
    def message_icon(self, value: str | None) -> None:
        self._message_icon = value

    @property
    def button_icon(self) -> str | None:
        return self._button_icon

    @button_icon.setter
    @BaseNodeElement.emits_update_on_write
    def button_icon(self, value: str | None) -> None:
        self._button_icon = value

    @property
    def button_variant(self) -> ButtonVariantType:
        return self._button_variant

    @button_variant.setter
    @BaseNodeElement.emits_update_on_write
    def button_variant(self, value: ButtonVariantType) -> None:
        self._button_variant = value

    @property
    def button_align(self) -> ButtonAlignType:
        return self._button_align

    @button_align.setter
    @BaseNodeElement.emits_update_on_write
    def button_align(self, value: ButtonAlignType) -> None:
        self._button_align = value

    @property
    def markdown(self) -> bool:
        """Get whether markdown rendering is enabled.

        Returns:
            True if markdown rendering is enabled, False otherwise
        """
        return self.ui_options.get("markdown", False)

    @markdown.setter
    @BaseNodeElement.emits_update_on_write
    def markdown(self, value: bool) -> None:
        """Set whether to enable markdown rendering.

        Args:
            value: True to enable markdown rendering, False to disable it
        """
        self.update_ui_options_key("markdown", value)

    @property
    def hide(self) -> bool:
        """Get whether the message is hidden in the UI.

        Returns:
            True if the message should be hidden, False otherwise
        """
        return self.ui_options.get("hide", False)

    @hide.setter
    @BaseNodeElement.emits_update_on_write
    def hide(self, value: bool) -> None:
        """Set whether to hide the message in the UI.

        Args:
            value: True to hide the message, False to show it
        """
        self.update_ui_options_key("hide", value)

    @property
    def ui_options(self) -> dict:
        return self._ui_options

    @ui_options.setter
    @BaseNodeElement.emits_update_on_write
    def ui_options(self, value: dict) -> None:
        self._ui_options = value

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()

        # Use class-level default titles and icons
        title = self.title or self.DEFAULT_TITLES.get(str(self.variant), "")

        # Handle message_icon logic:
        # - "__DEFAULT__" means use the default icon for the variant
        # - None means explicitly no icon (empty string)
        # - Any other string means use that icon
        if self.message_icon == "__DEFAULT__":
            message_icon = self.DEFAULT_ICONS.get(str(self.variant), "")
        elif self.message_icon is None:
            message_icon = ""
        else:
            message_icon = self.message_icon

        # Handle button_icon logic:
        # - None means no icon
        # - Empty string means no icon
        # - Any other string means use that icon
        if self.button_icon is None or self.button_icon == "":
            button_icon = ""
        else:
            button_icon = self.button_icon

        # Check if there are any Button traits with on_click callbacks
        has_button_callback = False
        for child in self.children:
            # Import here to avoid circular imports
            from griptape_nodes.traits.button import Button

            if isinstance(child, Button) and child.on_click_callback is not None:
                has_button_callback = True
                break

        # Merge the UI options with the message-specific options
        # Always include these fields, even if they're None or empty
        message_ui_options = {
            "title": title,
            "variant": self.variant,
            "message_icon": message_icon,
            "button_link": self.button_link,
            "button_text": self.button_text,
            "button_icon": button_icon,
            "button_variant": self.button_variant,
            "button_align": self.button_align,
            "button_on_click": has_button_callback,
            "full_width": self.full_width,
        }

        merged_ui_options = {
            **self.ui_options,
            **message_ui_options,
        }

        data["name"] = self.name
        data["value"] = self.value
        data["default_value"] = self.value  # for compatibility
        data["ui_options"] = merged_ui_options

        return data

    def to_event(self, node: BaseNode) -> dict:
        event_data = super().to_event(node)
        dict_data = self.to_dict()
        # Combine them both to get what we need for the UI.
        event_data.update(dict_data)
        return event_data


class DeprecationMessage(ParameterMessage):
    """A specialized ParameterMessage for deprecation warnings with default warning styling."""

    # Keep the same element_type as ParameterMessage so UI recognizes it
    element_type: str = "ParameterMessage"

    def __init__(
        self,
        value: str,
        button_text: str,
        migrate_function: Callable[[Any, Any], Any],
        **kwargs,
    ):
        """Initialize a deprecation message with default warning styling.

        Args:
            value: The deprecation message text
            button_text: Text for the migration button
            migrate_function: Function to call when migration button is clicked
            **kwargs: Additional arguments passed to ParameterMessage
        """
        # Set defaults for deprecation messages
        kwargs.setdefault("variant", "warning")
        kwargs.setdefault("full_width", True)

        # Add the button trait
        from griptape_nodes.traits.button import Button

        kwargs.setdefault("traits", {})
        kwargs["traits"][Button(label=button_text, icon="plus", variant="secondary", on_click=migrate_function)] = None

        super().__init__(value=value, button_text=button_text, **kwargs)

    def to_dict(self) -> dict:
        """Override to_dict to use element_type instead of class name.

        The base to_dict() method uses self.__class__.__name__ which would return
        "DeprecationMessage", but the UI expects element_type to be "ParameterMessage"
        to recognize it as a valid ParameterMessage element.
        """
        data = super().to_dict()
        data["element_type"] = self.element_type  # Use "ParameterMessage" not "DeprecationMessage"
        return data


class ParameterGroup(BaseNodeElement, UIOptionsMixin):
    """UI element for a group of parameters."""

    def __init__(
        self,
        name: str,
        ui_options: dict | None = None,
        *,
        collapsed: bool = False,
        user_defined: bool = False,
        badge: BadgeData
        | None = None,  # Optional BadgeData for initial badge (title, message, variant, and whether to show a clear button).
        **kwargs,
    ):
        super().__init__(name=name, **kwargs)
        if ui_options is None:
            ui_options = {}
        else:
            ui_options = ui_options.copy()

        # Add collapsed to ui_options if it's True
        if collapsed:
            ui_options["collapsed"] = collapsed

        self._ui_options = ui_options
        self.user_defined = user_defined

        if badge is not None:
            self.set_badge(
                variant=badge.variant,
                title=badge.title,
                message=badge.message,
                icon=badge.icon,
                color=badge.color,
                hide=badge.hide,
                hide_clear_button=badge.hide_clear_button,
            )

    @property
    def ui_options(self) -> dict:
        return self._ui_options

    @ui_options.setter
    @BaseNodeElement.emits_update_on_write
    def ui_options(self, value: dict) -> None:
        self._ui_options = value

    @property
    def collapsed(self) -> bool:
        """Get whether the parameter group is collapsed.

        Returns:
            True if the group is collapsed, False otherwise
        """
        return self._ui_options.get("collapsed", False)

    @collapsed.setter
    @BaseNodeElement.emits_update_on_write
    def collapsed(self, value: bool) -> None:
        """Set whether the parameter group is collapsed.

        Args:
            value: Whether to collapse the group
        """
        if value:
            self.update_ui_options_key("collapsed", value)
        else:
            ui_options = self._ui_options.copy()
            ui_options.pop("collapsed", None)
            self._ui_options = ui_options

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
        # Get the parent's version first.
        our_dict = super().to_dict()
        # Add in our deltas.
        our_dict["name"] = self.name
        our_dict["ui_options"] = self.ui_options
        return our_dict

    def to_event(self, node: BaseNode) -> dict:
        event_data = super().to_event(node)
        event_data["ui_options"] = self.ui_options
        return event_data

    def equals(self, other: ParameterGroup) -> dict:
        self_dict = {"name": self.name, "ui_options": self.ui_options}
        other_dict = {"name": other.name, "ui_options": other.ui_options}
        if self_dict == other_dict:
            return {}
        differences = {}
        for key, self_value in self_dict.items():
            other_value = other_dict.get(key)
            if self_value != other_value:
                differences[key] = other_value
        return differences

    def add_child(self, child: BaseNodeElement) -> None:
        child.parent_group_name = self.name
        # Keep parent_element_name in sync with parent_group_name for Parameters.
        # These two fields track the same relationship but have different origins:
        # - parent_group_name: set here by add_child(), always correct
        # - parent_element_name: set by Parameter constructors and handlers, used by
        #   BaseNode.add_parameter() to look up the group, and by cattrs serialization
        # Without this sync, Parameters created via the context manager path (which calls
        # add_child() directly) would have parent_element_name=None, causing them to
        # serialize without their parent group reference and reload as flat/root-level.
        if isinstance(child, Parameter):
            child.parent_element_name = self.name
        return super().add_child(child)

    def remove_child(self, child: BaseNodeElement | str) -> None:
        """Clear parent tracking fields (inverse of add_child), then remove from the tree."""
        if isinstance(child, str):
            child_from_str = self.find_element_by_name(child)
            if child_from_str is not None and isinstance(child_from_str, BaseNodeElement):
                child_from_str.parent_group_name = None
                if isinstance(child_from_str, Parameter):
                    child_from_str.parent_element_name = None
                return super().remove_child(child_from_str)
        else:
            child.parent_group_name = None
            if isinstance(child, Parameter):
                child.parent_element_name = None
        return super().remove_child(child)


class ParameterButtonGroup(BaseNodeElement, UIOptionsMixin):
    """UI element for grouping buttons together in a row (similar to shadcn ButtonGroup).

    This class creates a button group container that displays buttons horizontally
    with proper spacing and styling, similar to shadcn/ui's ButtonGroup component.

    Example:
        with ParameterButtonGroup(name="actions", orientation="horizontal") as button_group:
            ParameterButton(
                name="save",
                label="Save",
                variant="default",
            )
            ParameterButton(
                name="cancel",
                label="Cancel",
                variant="secondary",
            )
    """

    def __init__(
        self,
        name: str,
        ui_options: dict | None = None,
        *,
        orientation: Literal["horizontal", "vertical"] = "horizontal",
        hide_label: bool | None = None,
        display_name: str | None = None,
        **kwargs,
    ):
        super().__init__(name=name, element_type="ParameterButtonGroup", **kwargs)

        if ui_options is None:
            ui_options = {}
        else:
            ui_options = ui_options.copy()

        if hide_label is not None:
            self._validate_ui_option_conflict(
                ui_options_dict=ui_options, param_name="hide_label", param_value=hide_label
            )
        if display_name is not None:
            self._validate_ui_option_conflict(
                ui_options_dict=ui_options, param_name="display_name", param_value=display_name
            )

        if "hide_label" not in ui_options:
            if hide_label is not None:
                ui_options["hide_label"] = hide_label
            else:
                ui_options["hide_label"] = True

        if display_name is not None and "display_name" not in ui_options:
            ui_options["display_name"] = display_name

        ui_options["button_group"] = True
        ui_options["orientation"] = orientation

        self._ui_options = ui_options
        self._orientation: Literal["horizontal", "vertical"] = orientation

    @property
    def ui_options(self) -> dict:
        return self._ui_options

    @ui_options.setter
    @BaseNodeElement.emits_update_on_write
    def ui_options(self, value: dict) -> None:
        self._ui_options = value

    @property
    def orientation(self) -> Literal["horizontal", "vertical"]:
        """Get the button group orientation.

        Returns:
            "horizontal" for buttons in a row, "vertical" for buttons in a column
        """
        return self._orientation

    @orientation.setter
    @BaseNodeElement.emits_update_on_write
    def orientation(self, value: Literal["horizontal", "vertical"]) -> None:
        """Set the button group orientation.

        Args:
            value: "horizontal" for buttons in a row, "vertical" for buttons in a column
        """
        self._orientation = value
        self.update_ui_options_key("orientation", value)

    @property
    def hide_label(self) -> bool:
        """Whether the button group label is hidden in the UI (defaults to True)."""
        return self.ui_options.get("hide_label", True)

    @hide_label.setter
    @BaseNodeElement.emits_update_on_write
    def hide_label(self, value: bool) -> None:
        self.update_ui_options_key("hide_label", value)

    @property
    def display_name(self) -> str | None:
        """Human-readable label for the button group (overrides the default from `name`)."""
        return self.ui_options.get("display_name")

    @display_name.setter
    @BaseNodeElement.emits_update_on_write
    def display_name(self, value: str | None) -> None:
        if value is None:
            ui_options = self.ui_options.copy()
            ui_options.pop("display_name", None)
            self.ui_options = ui_options
        else:
            self.update_ui_options_key("display_name", value)

    def to_dict(self) -> dict[str, Any]:
        """Returns a nested dictionary representation of this button group and its children."""
        our_dict = super().to_dict()
        our_dict["name"] = self.name
        our_dict["ui_options"] = self.ui_options
        return our_dict

    def to_event(self, node: BaseNode) -> dict:
        event_data = super().to_event(node)
        event_data["ui_options"] = self.ui_options
        return event_data

    def add_child(self, child: BaseNodeElement) -> None:
        child.parent_group_name = self.name
        if isinstance(child, Parameter):
            child.parent_element_name = self.name
        return super().add_child(child)

    def remove_child(self, child: BaseNodeElement | str) -> None:
        """Clear parent tracking fields (inverse of add_child), then remove from the tree."""
        if isinstance(child, str):
            child_from_str = self.find_element_by_name(child)
            if child_from_str is not None and isinstance(child_from_str, BaseNodeElement):
                child_from_str.parent_group_name = None
                if isinstance(child_from_str, Parameter):
                    child_from_str.parent_element_name = None
                return super().remove_child(child_from_str)
        else:
            child.parent_group_name = None
            if isinstance(child, Parameter):
                child.parent_element_name = None
        return super().remove_child(child)


# TODO: https://github.com/griptape-ai/griptape-nodes/issues/856
class ParameterBase(BaseNodeElement, ABC):
    @property
    @abstractmethod
    def tooltip(self) -> str | list[dict]:
        """Get the default tooltip for this Parameter-like object.

        Returns:
            str | list[dict]: Either the explicit tooltip string or a list of dicts for special UI handling.
        """

    @tooltip.setter
    @abstractmethod
    def tooltip(self, value: str | list[dict]) -> None:
        pass

    @abstractmethod
    def get_default_value(self) -> Any:
        """Get the default value that should be assigned to this Parameter-like object.

        Returns:
            Any: The default value to assign when initialized or reset.
        """

    @abstractmethod
    def get_input_types(self) -> list[str] | None:
        """Get the list of input types this Parameter-like object accepts, or None if it doesn't accept any.

        Returns:
            list[str] | None: List of user-defined types supported.
        """

    @abstractmethod
    def get_output_type(self) -> str | None:
        """Get the output type this Parameter-like object emits, or None if it doesn't output.

        Returns:
            str | None: User-defined type output.
        """

    @abstractmethod
    def get_type(self) -> str | None:
        pass

    @abstractmethod
    def get_tooltip_as_input(self) -> str | list[dict] | None:
        pass


class Parameter(BaseNodeElement, UIOptionsMixin):
    # Maximum number of input types to show in tooltip before truncating
    _MAX_TOOLTIP_INPUT_TYPES = 3

    # This is the list of types that the Parameter can accept, either externally or when internally treated as a property.
    # Today, we can accept multiple types for input, but only a single output type.
    tooltip: str | list[dict]  # Default tooltip, can be string or list of dicts
    default_value: Any = None
    _input_types: list[str] | None
    _output_type: str | None
    _type: str | None
    tooltip_as_input: str | list[dict] | None = None
    tooltip_as_property: str | list[dict] | None = None
    tooltip_as_output: str | list[dict] | None = None

    # "settable" here means whether it can be assigned to during regular business operation.
    # During save/load, this value IS still serialized to save its proper state.
    _settable: bool = True

    # "serializable" controls whether parameter values should be serialized during save/load operations.
    # Set to False for parameters containing non-serializable types (ImageDrivers, PromptDrivers, file handles, etc.)
    serializable: bool = True

    user_defined: bool = False
    private: bool = False
    _allowed_modes: set = field(
        default_factory=lambda: {
            ParameterMode.OUTPUT,
            ParameterMode.INPUT,
            ParameterMode.PROPERTY,
        }
    )
    _converters: list[Callable[[Any], Any]]
    _validators: list[Callable[[Parameter, Any], None]]
    _on_incoming_connection_removed: list[Callable[[Parameter, str, str], None]]
    _on_outgoing_connection_removed: list[Callable[[Parameter, str, str], None]]
    _ui_options: dict
    next: Parameter | None = None
    prev: Parameter | None = None
    parent_container_name: str | None = None
    parent_element_name: str | None = None

    def __init__(  # noqa: C901, PLR0912, PLR0913, PLR0915
        self,
        name: str,
        tooltip: str | list[dict] | None = None,
        type: str | None = None,  # noqa: A002
        input_types: list[str] | None = None,
        output_type: str | None = None,
        default_value: Any = None,
        tooltip_as_input: str | list[dict] | None = None,
        tooltip_as_property: str | list[dict] | None = None,
        tooltip_as_output: str | list[dict] | None = None,
        allowed_modes: set[ParameterMode] | None = None,
        converters: list[Callable[[Any], Any]] | None = None,
        validators: list[Callable[[Parameter, Any], None]] | None = None,
        traits: set[Trait.__class__ | Trait] | None = None,  # We are going to make these children.
        ui_options: dict | None = None,
        *,
        hide: bool | None = None,
        hide_label: bool | None = None,
        hide_property: bool | None = None,
        display_name: str | None = None,
        allow_input: bool = True,
        allow_property: bool = True,
        allow_output: bool = True,
        settable: bool = True,
        serializable: bool = True,
        user_defined: bool = False,
        private: bool = False,
        element_id: str | None = None,
        element_type: str | None = None,
        parent_container_name: str | None = None,
        parent_element_name: str | None = None,
        badge: BadgeData
        | None = None,  # Optional BadgeData for initial badge (title, message, variant, and whether to show a clear button).
    ):
        if not element_id:
            element_id = str(uuid.uuid4().hex)
        if not element_type:
            element_type = self.__class__.__name__

        # Set parent references BEFORE super().__init__(), which triggers __post_init__().
        # If this Parameter is being created inside a ParameterGroup context manager,
        # __post_init__() will call add_child() which overwrites these with the correct
        # group name. Setting them first ensures they exist as attributes, and the context
        # manager path gets the final say.
        self.parent_container_name = parent_container_name
        self.parent_element_name = parent_element_name

        super().__init__(element_id=element_id, element_type=element_type)
        self.name = name

        # Generate default tooltip if none provided
        if not tooltip:
            tooltip = self._generate_default_tooltip(name, type, input_types, output_type)

        self.tooltip = tooltip
        self.default_value = default_value
        self.tooltip_as_input = tooltip_as_input
        self.tooltip_as_property = tooltip_as_property
        self.tooltip_as_output = tooltip_as_output
        self._settable = settable
        self.serializable = serializable
        self.user_defined = user_defined
        self.private = private

        # Process allowed_modes - use convenience parameters if allowed_modes not explicitly set
        if allowed_modes is None:
            self._allowed_modes = set()
            if allow_input:
                self._allowed_modes.add(ParameterMode.INPUT)
            if allow_property:
                self._allowed_modes.add(ParameterMode.PROPERTY)
            if allow_output:
                self._allowed_modes.add(ParameterMode.OUTPUT)
        else:
            self._allowed_modes = allowed_modes

            # Warn if both allowed_modes and convenience parameters are set
            convenience_params_used = []
            if not allow_input:
                convenience_params_used.append("allow_input=False")
            if not allow_property:
                convenience_params_used.append("allow_property=False")
            if not allow_output:
                convenience_params_used.append("allow_output=False")

            if convenience_params_used:
                warnings.warn(
                    f"Parameter '{name}': Both 'allowed_modes' and convenience parameters "
                    f"({', '.join(convenience_params_used)}) are set. Using 'allowed_modes' "
                    f"and ignoring convenience parameters.",
                    UserWarning,
                    stacklevel=2,
                )

        if converters is None:
            self._converters = []
        else:
            self._converters = converters

        if validators is None:
            self._validators = []
        else:
            self._validators = validators

        self._on_incoming_connection_removed = []
        self._on_outgoing_connection_removed = []

        # Process common UI options from constructor parameters
        if ui_options is None:
            self._ui_options = {}
        else:
            self._ui_options = ui_options.copy()

        # Validate that explicit parameters don't conflict with ui_options (only if not None)
        if hide is not None:
            self._validate_ui_option_conflict(ui_options_dict=self._ui_options, param_name="hide", param_value=hide)
        if hide_label is not None:
            self._validate_ui_option_conflict(
                ui_options_dict=self._ui_options, param_name="hide_label", param_value=hide_label
            )
        if hide_property is not None:
            self._validate_ui_option_conflict(
                ui_options_dict=self._ui_options, param_name="hide_property", param_value=hide_property
            )
        if display_name is not None:
            self._validate_ui_option_conflict(
                ui_options_dict=self._ui_options, param_name="display_name", param_value=display_name
            )

        # Add common UI options if explicitly provided (not None) and NOT already in ui_options
        # (ui_options always wins in case of conflict)
        if hide is not None and "hide" not in self._ui_options:
            self._ui_options["hide"] = hide
        if hide_label is not None and "hide_label" not in self._ui_options:
            self._ui_options["hide_label"] = hide_label
        if hide_property is not None and "hide_property" not in self._ui_options:
            self._ui_options["hide_property"] = hide_property
        if display_name is not None and "display_name" not in self._ui_options:
            self._ui_options["display_name"] = display_name
        if traits:
            for trait in traits:
                if not isinstance(trait, Trait):
                    created = trait()
                else:
                    created = trait
                # Add a trait as a child
                # UI options are now traits! sorry!
                self.add_child(created)
        if badge is not None:
            self.set_badge(
                variant=badge.variant,
                title=badge.title,
                message=badge.message,
                icon=badge.icon,
                color=badge.color,
                hide=badge.hide,
                hide_clear_button=badge.hide_clear_button,
            )
        self.type = type
        self.input_types = input_types
        self.output_type = output_type

    def _generate_default_tooltip(
        self,
        name: str,
        type: str | None,  # noqa: A002
        input_types: list[str] | None,
        output_type: str | None,
    ) -> str:
        """Generate a default tooltip describing the parameter type and usage.

        Args:
            name: The parameter name
            type: The parameter type
            input_types: List of accepted input types
            output_type: The output type

        Returns:
            A descriptive tooltip string
        """
        # Determine the primary type to describe
        primary_type = type
        if not primary_type and input_types:
            primary_type = input_types[0]
        if not primary_type and output_type:
            primary_type = output_type
        if not primary_type:
            primary_type = "any"

        # Create a human-readable description
        type_descriptions = {
            "str": "text/string",
            "bool": "boolean (true/false)",
            "int": "integer number",
            "float": "decimal number",
            "any": "any type of data",
            "list": "list/array",
            "dict": "dictionary/object",
            "parametercontroltype": "control flow",
        }

        type_desc = type_descriptions.get(primary_type.lower(), primary_type)

        # Build the tooltip
        tooltip_parts = [f"Enter {type_desc} for {name}"]

        # Add input type info if different from primary type
        if input_types and len(input_types) > 1:
            input_desc = ", ".join(
                type_descriptions.get(t.lower(), t) for t in input_types[: self._MAX_TOOLTIP_INPUT_TYPES]
            )
            if len(input_types) > self._MAX_TOOLTIP_INPUT_TYPES:
                input_desc += f" or {len(input_types) - self._MAX_TOOLTIP_INPUT_TYPES} other types"
            tooltip_parts.append(f"Accepts: {input_desc}")

        return ". ".join(tooltip_parts) + "."

    def to_dict(self) -> dict[str, Any]:
        """Returns a nested dictionary representation of this node and its children."""
        # Get the parent's version first.
        our_dict = super().to_dict()
        # Add in our deltas.
        our_dict["name"] = self.name
        our_dict["type"] = self.type
        our_dict["input_types"] = self.input_types
        our_dict["output_type"] = self.output_type
        our_dict["default_value"] = self.default_value
        our_dict["tooltip"] = self.tooltip
        our_dict["tooltip_as_input"] = self.tooltip_as_input
        our_dict["tooltip_as_output"] = self.tooltip_as_output
        our_dict["tooltip_as_property"] = self.tooltip_as_property

        our_dict["is_user_defined"] = self.user_defined
        our_dict["settable"] = self.settable
        our_dict["serializable"] = self.serializable
        our_dict["private"] = self.private
        our_dict["ui_options"] = self.ui_options

        # Let's bundle up the mode details.
        allows_input = ParameterMode.INPUT in self.allowed_modes
        allows_property = ParameterMode.PROPERTY in self.allowed_modes
        allows_output = ParameterMode.OUTPUT in self.allowed_modes
        our_dict["mode_allowed_input"] = allows_input
        our_dict["mode_allowed_property"] = allows_property
        our_dict["mode_allowed_output"] = allows_output
        our_dict["parent_container_name"] = self.parent_container_name
        our_dict["parent_element_name"] = self.parent_element_name
        our_dict["parent_group_name"] = self.parent_group_name

        return our_dict

    def to_event(self, node: BaseNode) -> dict:
        event_dict = self.to_dict()
        event_data = super().to_event(node)
        event_dict.update(event_data)
        # Update for our name with the right values
        name = event_dict.pop("name")
        event_dict["parameter_name"] = name
        # Update with value
        if node is not None:
            event_dict["value"] = node.get_parameter_value(self.name)
        return event_dict

    @property
    def type(self) -> str:
        return self._custom_getter_for_property_type()

    def _custom_getter_for_property_type(self) -> str:
        """Derived classes may override this. Overriding property getter/setters is fraught with peril."""
        if self._type:
            return self._type
        if self._input_types:
            return self._input_types[0]
        if self._output_type:
            return self._output_type
        return ParameterTypeBuiltin.STR.value

    @type.setter  # noqa: A003
    @BaseNodeElement.emits_update_on_write
    def type(self, value: str | None) -> None:
        self._custom_setter_for_property_type(value)

    def _custom_setter_for_property_type(self, value: str | None) -> None:
        """Derived classes may override this. Overriding property getter/setters is fraught with peril."""
        if value is not None:
            # See if it's an alias to a builtin first.
            builtin = ParameterType.attempt_get_builtin(value)
            if builtin is not None:
                self._type = builtin.value
            else:
                self._type = value
            return
        self._type = None

    @property
    def converters(self) -> list[Callable[[Any], Any]]:
        converters = []
        traits = self.find_elements_by_type(Trait)
        for trait in traits:
            converters += trait.converters_for_trait()
        converters += self._converters
        return converters

    @property
    def validators(self) -> list[Callable[[Parameter, Any], None]]:
        validators = []
        traits = self.find_elements_by_type(Trait)  # TODO: https://github.com/griptape-ai/griptape-nodes/issues/857
        for trait in traits:
            validators += trait.validators_for_trait()
        validators += self._validators
        return validators

    @property
    def has_directly_attached_converters(self) -> bool:
        """The directly-attached converter list is non-empty.

        Trait-derived converters are merged in by the ``converters``
        getter; this checks only what was passed to ``__init__`` or
        appended directly.
        """
        return bool(self._converters)

    @property
    def has_directly_attached_validators(self) -> bool:
        """The directly-attached validator list is non-empty.

        Trait-derived validators are merged in by the ``validators``
        getter; this checks only what was passed to ``__init__`` or
        appended directly.
        """
        return bool(self._validators)

    @property
    def has_traits(self) -> bool:
        """Any Trait child is attached.

        Walks ``find_elements_by_type(Trait)`` because traits are stored
        as child node-elements, not in a flat list. Cheap at probe
        scale (called once per parameter at library load).
        """
        return bool(self.find_elements_by_type(Trait))

    @property
    def on_incoming_connection_removed(self) -> list[Callable[[Parameter, str, str], None]]:
        return self._on_incoming_connection_removed

    @property
    def on_outgoing_connection_removed(self) -> list[Callable[[Parameter, str, str], None]]:
        return self._on_outgoing_connection_removed

    @property
    def allowed_modes(self) -> set[ParameterMode]:
        return self._allowed_modes

    @allowed_modes.setter
    @BaseNodeElement.emits_update_on_write
    def allowed_modes(self, value: Any) -> None:
        self._allowed_modes = value
        # Handle mode flag decomposition
        if isinstance(value, set):
            self._changes["mode_allowed_input"] = ParameterMode.INPUT in value
            self._changes["mode_allowed_output"] = ParameterMode.OUTPUT in value
            self._changes["mode_allowed_property"] = ParameterMode.PROPERTY in value

    @property
    def settable(self) -> bool:
        return self._settable

    @settable.setter
    @BaseNodeElement.emits_update_on_write
    def settable(self, value: bool) -> None:
        self._settable = value

    @property
    def ui_options(self) -> dict:
        ui_options = {}
        traits = self.find_elements_by_type(Trait)
        for trait in traits:
            ui_options = ui_options | trait.ui_options_for_trait()
        ui_options = ui_options | self._ui_options
        return ui_options

    @ui_options.setter
    @BaseNodeElement.emits_update_on_write
    def ui_options(self, value: dict) -> None:
        self._ui_options = value

    @property
    def hide(self) -> bool:
        """Get whether the entire parameter is hidden in the UI.

        Returns:
            True if the parameter should be hidden, False otherwise
        """
        return self.ui_options.get("hide", False)

    @hide.setter
    @BaseNodeElement.emits_update_on_write
    def hide(self, value: bool) -> None:
        """Set whether to hide the entire parameter in the UI.

        Args:
            value: True to hide the parameter, False to show it
        """
        self.update_ui_options_key("hide", value)

    @property
    def hide_label(self) -> bool:
        """Get whether the parameter label is hidden in the UI.

        Returns:
            True if the label should be hidden, False otherwise
        """
        return self.ui_options.get("hide_label", False)

    @hide_label.setter
    @BaseNodeElement.emits_update_on_write
    def hide_label(self, value: bool) -> None:
        """Set whether to hide the parameter label in the UI.

        Args:
            value: True to hide the label, False to show it
        """
        self.update_ui_options_key("hide_label", value)

    @property
    def hide_property(self) -> bool:
        """Get whether the parameter is hidden in property mode.

        Returns:
            True if the parameter should be hidden in property mode, False otherwise
        """
        return self.ui_options.get("hide_property", False)

    @hide_property.setter
    @BaseNodeElement.emits_update_on_write
    def hide_property(self, value: bool) -> None:
        """Set whether to hide the parameter in property mode.

        Args:
            value: True to hide in property mode, False to show it
        """
        self.update_ui_options_key("hide_property", value)

    @property
    def display_name(self) -> str | None:
        """Get the display name override for the parameter.

        Returns:
            The display name if set, None to use the default (parameter name)
        """
        return self.ui_options.get("display_name")

    @display_name.setter
    @BaseNodeElement.emits_update_on_write
    def display_name(self, value: str | None) -> None:
        """Set the display name override for the parameter.

        Args:
            value: Display name string, or None to use the default (parameter name)
        """
        if value is None:
            ui_options = self.ui_options.copy()
            ui_options.pop("display_name", None)
            self.ui_options = ui_options
        else:
            self.update_ui_options_key("display_name", value)

    @property
    def allow_input(self) -> bool:
        """Get whether the parameter allows INPUT mode.

        Returns:
            True if INPUT mode is allowed, False otherwise
        """
        return ParameterMode.INPUT in self.allowed_modes

    @allow_input.setter
    def allow_input(self, value: bool) -> None:
        """Set whether to allow INPUT mode.

        Args:
            value: True to allow INPUT mode, False to disallow it
        """
        current_modes = self.allowed_modes.copy()
        if value:
            current_modes.add(ParameterMode.INPUT)
        else:
            current_modes.discard(ParameterMode.INPUT)
        self.allowed_modes = current_modes

    @property
    def allow_property(self) -> bool:
        """Get whether the parameter allows PROPERTY mode.

        Returns:
            True if PROPERTY mode is allowed, False otherwise
        """
        return ParameterMode.PROPERTY in self.allowed_modes

    @allow_property.setter
    def allow_property(self, value: bool) -> None:
        """Set whether to allow PROPERTY mode.

        Args:
            value: True to allow PROPERTY mode, False to disallow it
        """
        current_modes = self.allowed_modes.copy()
        if value:
            current_modes.add(ParameterMode.PROPERTY)
        else:
            current_modes.discard(ParameterMode.PROPERTY)
        self.allowed_modes = current_modes

    @property
    def allow_output(self) -> bool:
        """Get whether the parameter allows OUTPUT mode.

        Returns:
            True if OUTPUT mode is allowed, False otherwise
        """
        return ParameterMode.OUTPUT in self.allowed_modes

    @allow_output.setter
    def allow_output(self, value: bool) -> None:
        """Set whether to allow OUTPUT mode.

        Args:
            value: True to allow OUTPUT mode, False to disallow it
        """
        current_modes = self.allowed_modes.copy()
        if value:
            current_modes.add(ParameterMode.OUTPUT)
        else:
            current_modes.discard(ParameterMode.OUTPUT)
        self.allowed_modes = current_modes

    @property
    def input_types(self) -> list[str]:
        return self._custom_getter_for_property_input_types()

    def _custom_getter_for_property_input_types(self) -> list[str]:
        """Derived classes may override this. Overriding property getter/setters is fraught with peril."""
        if self._input_types:
            return self._input_types
        if self._type:
            return [self._type]
        if self._output_type:
            return [self._output_type]
        return [ParameterTypeBuiltin.STR.value]

    @input_types.setter
    @BaseNodeElement.emits_update_on_write
    def input_types(self, value: list[str] | None) -> None:
        self._custom_setter_for_property_input_types(value)

    def _custom_setter_for_property_input_types(self, value: list[str] | None) -> None:
        """Derived classes may override this. Overriding property getter/setters is fraught with peril."""
        if value is None:
            self._input_types = None
        else:
            self._input_types = []
            for new_type in value:
                # See if it's an alias to a builtin first.
                builtin = ParameterType.attempt_get_builtin(new_type)
                if builtin is not None:
                    self._input_types.append(builtin.value)
                else:
                    self._input_types.append(new_type)

    @property
    def output_type(self) -> str:
        return self._custom_getter_for_property_output_type()

    def _custom_getter_for_property_output_type(self) -> str:
        """Derived classes may override this. Overriding property getter/setters is fraught with peril."""
        if self._output_type:
            # If an output type was specified, use that.
            return self._output_type
        if self._type:
            # Otherwise, see if we have a list of input_types. If so, use the first one.
            return self._type

        # Otherwise, see if we have a list of input_types. If so, use the first one.
        if self._input_types:
            return self._input_types[0]
        # Otherwise, return a string.
        return ParameterTypeBuiltin.STR.value

    @output_type.setter
    @BaseNodeElement.emits_update_on_write
    def output_type(self, value: str | None) -> None:
        self._custom_setter_for_property_output_type(value)

    def _custom_setter_for_property_output_type(self, value: str | None) -> None:
        """Derived classes may override this. Overriding property getter/setters is fraught with peril."""
        if value is not None:
            # See if it's an alias to a builtin first.
            builtin = ParameterType.attempt_get_builtin(value)
            if builtin is not None:
                self._output_type = builtin.value
            else:
                self._output_type = value
            return
        self._output_type = None

    def add_trait(self, trait: type[Trait] | Trait) -> None:
        if not isinstance(trait, Trait):
            created = trait()
        else:
            created = trait
        self.add_child(created)

    def remove_trait(self, trait_type: BaseNodeElement) -> None:
        # You are NOT ALLOWED TO ADD DUPLICATE TRAITS (kate)
        self.remove_child(trait_type)

    def is_incoming_type_allowed(self, incoming_type: str | None) -> bool:
        if incoming_type is None:
            return False

        if incoming_type.lower() == ParameterTypeBuiltin.ALL.value:
            return True

        ret_val = False

        if self.input_types:
            for test_type in self.input_types:
                if ParameterType.are_types_compatible(source_type=incoming_type, target_type=test_type):
                    ret_val = True
                    break
        else:
            # Customer feedback was to treat as a string by default.
            ret_val = ParameterType.are_types_compatible(
                source_type=incoming_type, target_type=ParameterTypeBuiltin.STR.value
            )

        return ret_val

    def is_outgoing_type_allowed(self, target_type: str | None) -> bool:
        return ParameterType.are_types_compatible(source_type=self.output_type, target_type=target_type)

    @BaseNodeElement.emits_update_on_write
    def set_default_value(self, value: Any) -> None:
        self.default_value = value

    def get_mode(self) -> set:
        return self.allowed_modes

    def add_mode(self, mode: ParameterMode) -> None:
        self.allowed_modes.add(mode)

    def remove_mode(self, mode: ParameterMode) -> None:
        self.allowed_modes.remove(mode)

    def copy(self) -> Parameter:
        param = deepcopy(self)
        param.next = None
        param.prev = None
        return param

    def check_list(self, self_value: Any, other_value: Any, differences: dict, key: Any) -> None:
        # Convert both to lists for index-based iteration
        self_list = list(self_value)
        other_list = list(other_value)
        # Check if they have different lengths
        if len(self_list) != len(other_list):
            differences[key] = other_value
            return
        # Compare each element
        list_differences = False
        for i, item in enumerate(self_list):
            if i >= len(other_list):
                list_differences = True
                break
            # If the element is a Parameter, use its equals method
            if isinstance(item, Parameter) and isinstance(other_list[i], Parameter):
                if item.equals(other_list[i]):  # If there are differences
                    list_differences = True
                    break
            elif isinstance(item, BaseNodeElement) and isinstance(other_list[i], BaseNodeElement):
                if item != other_list[i]:
                    list_differences = True
                    break
            # Otherwise use direct comparison
            elif item != other_list[i]:
                list_differences = True
                break
        if list_differences:
            differences[key] = other_value

    # intentionally not overwriting __eq__ because I want to return a dict not true or false
    def equals(self, other: Parameter) -> dict:
        self_dict = self.to_dict().copy()
        other_dict = other.to_dict().copy()
        self_dict.pop("next", None)
        self_dict.pop("prev", None)
        self_dict.pop("element_id", None)
        other_dict.pop("next", None)
        other_dict.pop("element_id", None)
        other_dict.pop("prev", None)
        if self_dict == other_dict:
            return {}
        differences = {}
        for key, self_value in self_dict.items():
            other_value = other_dict.get(key, None)
            # handle children here
            if isinstance(self_value, BaseNodeElement) and isinstance(other_value, BaseNodeElement):
                if self_value != other_value:
                    differences[key] = other_value
            elif isinstance(self_value, (list, set)) and isinstance(other_value, (list, set)):
                self.check_list(self_value, other_value, differences, key)
            elif self_value != other_value:
                differences[key] = other_value
        return differences


# Convenience classes to reduce boilerplate in node definitions
class ControlParameter(Parameter, ABC):
    def __init__(  # noqa: PLR0913
        self,
        name: str,
        tooltip: str | list[dict],
        input_types: list[str] | None = None,
        output_type: str | None = None,
        tooltip_as_input: str | list[dict] | None = None,
        tooltip_as_property: str | list[dict] | None = None,
        tooltip_as_output: str | list[dict] | None = None,
        allowed_modes: set[ParameterMode] | None = None,
        traits: set[Trait.__class__ | Trait] | None = None,
        converters: list[Callable[[Any], Any]] | None = None,
        validators: list[Callable[[Parameter, Any], None]] | None = None,
        ui_options: dict | None = None,
        *,
        parameter_render_location: ParameterRenderLocation | None = None,
        display_name: str | None = None,
        user_defined: bool = False,
        private: bool = False,
    ):
        # Process ui_options before passing to parent
        if ui_options is None:
            processed_ui_options = {}
        else:
            processed_ui_options = ui_options.copy()

        # Validate for conflicts if explicitly provided
        if parameter_render_location is not None:
            self._validate_ui_option_conflict(
                ui_options_dict=processed_ui_options,
                param_name="parameter_render_location",
                param_value=parameter_render_location,
            )
            # Validate it's a valid value
            if parameter_render_location not in VALID_PARAMETER_RENDER_LOCATIONS:
                msg = f"Invalid parameter_render_location '{parameter_render_location}' for parameter '{name}'. Valid values: {sorted(VALID_PARAMETER_RENDER_LOCATIONS)}. Using 'top'."
                logger.warning(msg)
                parameter_render_location = "top"

        # By default, the editor renders all control parameters at the top of the node.
        # Set parameter_render_location to control where they render:
        # - "top": Render at the top of the node (default for ControlParameter)
        # - "bottom": Render at the bottom of the node
        # - "in-order": Render in-order in the order they are defined
        # This is useful for nodes with multiple control outputs that need to appear in specific positions.
        if "parameter_render_location" not in processed_ui_options:
            if parameter_render_location is not None:
                processed_ui_options["parameter_render_location"] = parameter_render_location
            else:
                processed_ui_options["parameter_render_location"] = "top"

        # Call parent with a few explicit tweaks.
        super().__init__(
            type=ParameterTypeBuiltin.CONTROL_TYPE.value,
            default_value=None,
            settable=True,
            name=name,
            tooltip=tooltip,
            input_types=input_types,
            output_type=output_type,
            tooltip_as_input=tooltip_as_input,
            tooltip_as_property=tooltip_as_property,
            tooltip_as_output=tooltip_as_output,
            allowed_modes=allowed_modes,
            traits=traits,
            converters=converters,
            validators=validators,
            ui_options=processed_ui_options,
            display_name=display_name,
            user_defined=user_defined,
            private=private,
            element_type=self.__class__.__name__,
        )


class ControlParameterInput(ControlParameter):
    def __init__(  # noqa: PLR0913
        self,
        tooltip: str | list[dict] = "Connection from previous node in the execution chain",
        name: str = "exec_in",
        display_name: str | None = "Flow In",
        tooltip_as_input: str | list[dict] | None = None,
        tooltip_as_property: str | list[dict] | None = None,
        tooltip_as_output: str | list[dict] | None = None,
        traits: set[Trait.__class__ | Trait] | None = None,
        converters: list[Callable[[Any], Any]] | None = None,
        validators: list[Callable[[Parameter, Any], None]] | None = None,
        *,
        parameter_render_location: ParameterRenderLocation | None = None,
        user_defined: bool = False,
        private: bool = False,
    ):
        allowed_modes = {ParameterMode.INPUT}
        input_types = [ParameterTypeBuiltin.CONTROL_TYPE.value]

        # Call parent with a few explicit tweaks.
        super().__init__(
            name=name,
            tooltip=tooltip,
            input_types=input_types,
            output_type=None,
            tooltip_as_input=tooltip_as_input,
            tooltip_as_property=tooltip_as_property,
            tooltip_as_output=tooltip_as_output,
            allowed_modes=allowed_modes,
            traits=traits,
            converters=converters,
            validators=validators,
            parameter_render_location=parameter_render_location,
            display_name=display_name,
            user_defined=user_defined,
            private=private,
        )


class ControlParameterOutput(ControlParameter):
    def __init__(  # noqa: PLR0913
        self,
        tooltip: str | list[dict] = "Connection to the next node in the execution chain",
        name: str = "exec_out",
        display_name: str | None = "Flow Out",
        tooltip_as_input: str | list[dict] | None = None,
        tooltip_as_property: str | list[dict] | None = None,
        tooltip_as_output: str | list[dict] | None = None,
        traits: set[Trait.__class__ | Trait] | None = None,
        converters: list[Callable[[Any], Any]] | None = None,
        validators: list[Callable[[Parameter, Any], None]] | None = None,
        *,
        parameter_render_location: ParameterRenderLocation | None = None,
        user_defined: bool = False,
        private: bool = False,
    ):
        allowed_modes = {ParameterMode.OUTPUT}
        output_type = ParameterTypeBuiltin.CONTROL_TYPE.value

        # Call parent with a few explicit tweaks.
        super().__init__(
            name=name,
            tooltip=tooltip,
            input_types=None,
            output_type=output_type,
            tooltip_as_input=tooltip_as_input,
            tooltip_as_property=tooltip_as_property,
            tooltip_as_output=tooltip_as_output,
            allowed_modes=allowed_modes,
            traits=traits,
            converters=converters,
            validators=validators,
            parameter_render_location=parameter_render_location,
            display_name=display_name,
            user_defined=user_defined,
            private=private,
        )


class ParameterContainer(Parameter, ABC):
    """Class managing a container (list/dict/tuple/etc.) of Parameters.

    It is, itself, a Parameter (so it can be the target of compatible Container connections, etc.)
    But it also has the ability to own and manage children and make them accessible by keys, etc.
    """

    def __init__(  # noqa: PLR0913
        self,
        name: str,
        tooltip: str | list[dict],
        type: str | None = None,  # noqa: A002
        input_types: list[str] | None = None,
        output_type: str | None = None,
        default_value: Any = None,
        tooltip_as_input: str | list[dict] | None = None,
        tooltip_as_property: str | list[dict] | None = None,
        tooltip_as_output: str | list[dict] | None = None,
        allowed_modes: set[ParameterMode] | None = None,
        ui_options: dict | None = None,
        traits: set[Trait.__class__ | Trait] | None = None,
        converters: list[Callable[[Any], Any]] | None = None,
        validators: list[Callable[[Parameter, Any], None]] | None = None,
        *,
        hide: bool | None = None,
        display_name: str | None = None,
        settable: bool = True,
        user_defined: bool = False,
        private: bool = False,
        element_id: str | None = None,
        element_type: str | None = None,
    ):
        super().__init__(
            name=name,
            tooltip=tooltip,
            type=type,
            input_types=input_types,
            output_type=output_type,
            default_value=default_value,
            tooltip_as_input=tooltip_as_input,
            tooltip_as_property=tooltip_as_property,
            tooltip_as_output=tooltip_as_output,
            allowed_modes=allowed_modes,
            ui_options=ui_options,
            traits=traits,
            converters=converters,
            validators=validators,
            hide=hide,
            display_name=display_name,
            settable=settable,
            user_defined=user_defined,
            private=private,
            element_id=element_id,
            element_type=element_type,
        )

    def __bool__(self) -> bool:
        """Parameter containers are always truthy, even when empty.

        This overrides Python's default truthiness behavior for containers with __len__().
        By default, Python makes objects with __len__() falsy when len() == 0, which
        caused bugs where empty ParameterList/ParameterDictionary objects would fail
        'if param' checks and fall back to stale cached values instead of computing
        fresh empty results.

        Unlike standard Python containers, ParameterContainer objects represent
        parameter structure/definitions rather than just data, so they remain
        meaningful even when empty.

        See: https://github.com/griptape-ai/griptape-nodes/issues/1799
        """
        return True

    @abstractmethod
    def add_child_parameter(self) -> Parameter:
        pass


class ParameterList(ParameterContainer):
    _original_traits: set[Trait.__class__ | Trait]

    def __init__(  # noqa: PLR0913
        self,
        name: str,
        tooltip: str | list[dict],
        type: str | None = None,  # noqa: A002
        input_types: list[str] | None = None,
        output_type: str | None = None,
        default_value: Any = None,
        tooltip_as_input: str | list[dict] | None = None,
        tooltip_as_property: str | list[dict] | None = None,
        tooltip_as_output: str | list[dict] | None = None,
        allowed_modes: set[ParameterMode] | None = None,
        ui_options: dict | None = None,
        traits: set[Trait.__class__ | Trait] | None = None,
        converters: list[Callable[[Any], Any]] | None = None,
        validators: list[Callable[[Parameter, Any], None]] | None = None,
        *,
        hide: bool | None = None,
        display_name: str | None = None,
        settable: bool = True,
        user_defined: bool = False,
        private: bool = False,
        element_id: str | None = None,
        element_type: str | None = None,
        max_items: int | None = None,
        # UI convenience parameters
        collapsed: bool | None = None,
        child_prefix: str | None = None,
        grid: bool | None = None,
        grid_columns: int | None = None,
    ):
        if traits:
            self._original_traits = traits
        else:
            self._original_traits = set()

        self._max_items = max_items
        # Store the UI convenience parameters
        self._collapsed = collapsed
        self._child_prefix = child_prefix
        self._grid = grid
        self._grid_columns = grid_columns

        # Remember: we're a Parameter, too, just like everybody else.
        super().__init__(
            name=name,
            tooltip=tooltip,
            type=type,
            input_types=input_types,
            output_type=output_type,
            default_value=default_value,
            tooltip_as_input=tooltip_as_input,
            tooltip_as_property=tooltip_as_property,
            tooltip_as_output=tooltip_as_output,
            allowed_modes=allowed_modes,
            ui_options=ui_options,
            traits=traits,
            converters=converters,
            validators=validators,
            hide=hide,
            display_name=display_name,
            settable=settable,
            user_defined=user_defined,
            private=private,
            element_id=element_id,
            element_type=element_type,
        )

    @property
    def collapsed(self) -> bool | None:
        return self._collapsed

    @collapsed.setter
    @BaseNodeElement.emits_update_on_write
    def collapsed(self, value: bool | None) -> None:
        self._collapsed = value

    @property
    def child_prefix(self) -> str | None:
        return self._child_prefix

    @child_prefix.setter
    @BaseNodeElement.emits_update_on_write
    def child_prefix(self, value: str | None) -> None:
        self._child_prefix = value

    @property
    def grid(self) -> bool | None:
        return self._grid

    @grid.setter
    @BaseNodeElement.emits_update_on_write
    def grid(self, value: bool | None) -> None:
        self._grid = value

    @property
    def grid_columns(self) -> int | None:
        return self._grid_columns

    @grid_columns.setter
    @BaseNodeElement.emits_update_on_write
    def grid_columns(self, value: int | None) -> None:
        self._grid_columns = value

    @property
    def ui_options(self) -> dict:
        """Override ui_options to merge convenience parameters in real-time."""
        # Get base ui_options from parent
        base_ui_options = super().ui_options

        # Build convenience options from instance parameters
        convenience_options = {}

        if self._collapsed is not None:
            convenience_options["collapsed"] = self._collapsed

        if self._child_prefix is not None:
            convenience_options["child_prefix"] = self._child_prefix

        if self._grid is not None and self._grid:
            convenience_options["display"] = "grid"

        if self._grid_columns is not None and self._grid:
            convenience_options["columns"] = self._grid_columns

        # Merge convenience options with base ui_options
        return {
            **base_ui_options,
            **convenience_options,
        }

    @ui_options.setter
    @BaseNodeElement.emits_update_on_write
    def ui_options(self, value: dict) -> None:
        """Set ui_options, preserving convenience parameters."""
        # Extract convenience parameters from the incoming value
        if "display" in value and value["display"] == "grid":
            self._grid = True
            if "columns" in value:
                self._grid_columns = value["columns"]
        else:
            self._grid = False

        if "collapsed" in value:
            self._collapsed = value["collapsed"]

        if "child_prefix" in value:
            self._child_prefix = value["child_prefix"]

        # Set the base ui_options (excluding convenience parameters)
        base_ui_options = {
            k: v for k, v in value.items() if k not in ["display", "columns", "collapsed", "child_prefix"]
        }
        self._ui_options = base_ui_options

    def to_dict(self) -> dict[str, Any]:
        """Override to_dict to use the merged ui_options."""
        data = super().to_dict()
        data["ui_options"] = self.ui_options
        return data

    def _custom_getter_for_property_type(self) -> str:
        base_type = super()._custom_getter_for_property_type()
        result = f"list[{base_type}]"
        return result

    def _custom_setter_for_property_type(self, value: str | None) -> None:
        # If we are setting a type, we need to propagate this to our children as well.
        for child in self._children:
            if isinstance(child, Parameter):
                child.type = value
        super()._custom_setter_for_property_type(value)

    def _custom_setter_for_property_input_types(self, value: list[str] | None) -> None:
        # If we are setting a type, we need to propagate this to our children as well.
        for child in self._children:
            if isinstance(child, Parameter):
                child.input_types = value
        return super()._custom_setter_for_property_input_types(value)

    def _custom_setter_for_property_output_type(self, value: str | None) -> None:
        # If we are setting a type, we need to propagate this to our children as well.
        for child in self._children:
            if isinstance(child, Parameter):
                child.output_type = value
        return super()._custom_setter_for_property_output_type(value)

    def _custom_getter_for_property_input_types(self) -> list[str]:
        # For every valid input type, also accept a list variant of that for the CONTAINER Parameter only.
        # Children still use the input types given to them.
        base_input_types = super()._custom_getter_for_property_input_types()
        result = []
        for base_input_type in base_input_types:
            container_variant = f"list[{base_input_type}]"
            result.append(container_variant)

        return result

    def _custom_getter_for_property_output_type(self) -> str:
        base_type = super()._custom_getter_for_property_output_type()
        result = f"list[{base_type}]"
        return result

    def __len__(self) -> int:
        # Returns the number of child Parameters. Just do the top level.
        param_children = self.find_elements_by_type(element_type=Parameter, find_recursively=False)
        return len(param_children)

    def __getitem__(self, key: int) -> Parameter:
        count = 0
        for child in self._children:
            if isinstance(child, Parameter):
                if count == key:
                    # Found it.
                    return child
                count += 1

        # If we fell out of the for loop, we had a bad value.
        err_str = f"Attempted to get a Parameter List index {key}, which was out of range."
        raise KeyError(err_str)

    def add_child_parameter(self) -> Parameter:
        # Generate a name. This needs to be UNIQUE because children need
        # to be tracked as individuals and not as indices in the list.
        # Ex: a Connection is made to Parameter List[1]. List[0] gets deleted.
        # The OLD List[1] is now List[0], but we need to maintain the Connection
        # to the original entry.
        #
        # (No, we're not renaming it List[0] everywhere for you)
        name = f"{self.name}_ParameterListUniqueParamID_{uuid.uuid4().hex!s}"

        param = Parameter(
            name=name,
            tooltip=self.tooltip,
            type=self._type,
            input_types=self._input_types,
            output_type=self._output_type,
            default_value=self.default_value,
            tooltip_as_input=self.tooltip_as_input,
            tooltip_as_output=self.tooltip_as_output,
            tooltip_as_property=self.tooltip_as_property,
            allowed_modes=self.allowed_modes,
            ui_options=self.ui_options,
            traits=self._original_traits,
            converters=self.converters,
            validators=self.validators,
            settable=self.settable,
            user_defined=True,
            parent_container_name=self.name,
        )

        # Add at the end.
        self.add_child(param)

        return param

    def clear_list(self) -> None:
        """Remove all children that have been added to the list."""
        children = self.find_elements_by_type(element_type=Parameter)
        for child in children:
            if isinstance(child, Parameter):
                self.remove_child(child)
                del child

    # --- Convenience methods for stable list management ---
    def get_child_parameters(self) -> list[Parameter]:
        """Return direct child parameters only, in order of appearance."""
        return self.find_elements_by_type(element_type=Parameter, find_recursively=False)

    def append_child_parameter(self, display_name: str | None = None) -> Parameter:
        """Append one child parameter and optionally set a display name.

        This preserves existing children and adds a new one at the end.
        """
        child = self.add_child_parameter()
        if display_name is not None:
            child.display_name = display_name
        return child

    def remove_last_child_parameter(self) -> None:
        """Remove the last child parameter if one exists.

        This removes from the end to preserve earlier children and their connections.
        """
        children = self.get_child_parameters()
        if children:
            last = children[-1]
            self.remove_child(last)
            del last

    def ensure_length(self, desired_count: int, display_name_prefix: str | None = None) -> None:
        """Grow or shrink the list to the desired length while preserving existing items.

        - If increasing, appends new children to the end.
        - If decreasing, removes children from the end.
        - Optionally sets display names like "{prefix} 1", "{prefix} 2", ...
        """
        if desired_count is None:
            return
        try:
            desired_count = int(desired_count)
        except Exception:
            desired_count = 0
        desired_count = max(desired_count, 0)

        current_children = self.get_child_parameters()
        current_len = len(current_children)

        # Grow
        if current_len < desired_count:
            for index in range(current_len, desired_count):
                name = f"{display_name_prefix} {index + 1}" if display_name_prefix else None
                self.append_child_parameter(display_name=name)

        # Shrink
        elif current_len > desired_count:
            for _ in range(current_len - desired_count):
                self.remove_last_child_parameter()

        # Optionally re-apply display names to existing children to keep indices tidy
        if display_name_prefix:
            for index, child in enumerate(self.get_child_parameters()):
                child.display_name = f"{display_name_prefix} {index + 1}"

    def add_child(self, child: BaseNodeElement) -> None:
        """Override to mark parent node as unresolved when children are added.

        When a ParameterList gains a child parameter, the parent node needs to be
        marked as unresolved to trigger re-evaluation of the node's state and outputs.
        """
        # Validate max_items before adding child
        if self._max_items is not None:
            current_count = len(self._children)
            if current_count >= self._max_items:
                msg = f"Cannot add more items to {self.name}. Maximum {self._max_items} items allowed."
                raise ValueError(msg)

        super().add_child(child)

        # Mark the parent node as unresolved since the parameter structure changed
        if self._node_context is not None:
            # Import at runtime to avoid circular import
            from griptape_nodes.exe_types.node_types import NodeResolutionState

            self._node_context.make_node_unresolved(
                current_states_to_trigger_change_event={NodeResolutionState.RESOLVED, NodeResolutionState.RESOLVING}
            )

    def remove_child(self, child: BaseNodeElement | str) -> None:
        """Override to mark parent node as unresolved when children are removed.

        When a ParameterList loses a child parameter, the parent node needs to be
        marked as unresolved to trigger re-evaluation of the node's state and outputs.
        """
        super().remove_child(child)

        # Mark the parent node as unresolved since the parameter structure changed
        if self._node_context is not None:
            # Import at runtime to avoid circular import
            from griptape_nodes.exe_types.node_types import NodeResolutionState

            self._node_context.make_node_unresolved(
                current_states_to_trigger_change_event={NodeResolutionState.RESOLVED, NodeResolutionState.RESOLVING}
            )


class ParameterKeyValuePair(Parameter):
    def __init__(  # noqa: PLR0913
        self,
        name: str,
        tooltip: str | list[dict],
        # Main parameter options
        type: str | None = None,  # noqa: A002
        default_value: Any = None,
        tooltip_as_input: str | list[dict] | None = None,
        tooltip_as_property: str | list[dict] | None = None,
        tooltip_as_output: str | list[dict] | None = None,
        allowed_modes: set[ParameterMode] | None = None,
        ui_options: dict | None = None,
        traits: set[Trait.__class__ | Trait] | None = None,
        converters: list[Callable[[Any], Any]] | None = None,
        validators: list[Callable[[Parameter, Any], None]] | None = None,
        # Key and Value specific options
        key_default_value: Any = None,
        key_tooltip: str | list[dict] | None = None,
        key_ui_options: dict | None = None,
        key_traits: set[Trait.__class__ | Trait] | None = None,
        key_converters: list[Callable[[Any], Any]] | None = None,
        key_validators: list[Callable[[Parameter, Any], None]] | None = None,
        value_default_value: Any = None,
        value_tooltip: str | list[dict] | None = None,
        value_ui_options: dict | None = None,
        value_traits: set[Trait.__class__ | Trait] | None = None,
        value_converters: list[Callable[[Any], Any]] | None = None,
        value_validators: list[Callable[[Parameter, Any], None]] | None = None,
        *,
        display_name: str | None = None,
        settable: bool = True,
        user_defined: bool = False,
        private: bool = False,
        element_id: str | None = None,
        element_type: str | None = None,
    ):
        # Remember: we're a Parameter, too, just like everybody else.
        super().__init__(
            name=name,
            tooltip=tooltip,
            type=type,
            default_value=default_value,
            tooltip_as_input=tooltip_as_input,
            tooltip_as_property=tooltip_as_property,
            tooltip_as_output=tooltip_as_output,
            allowed_modes=allowed_modes,
            ui_options=ui_options,
            traits=traits,
            converters=converters,
            validators=validators,
            display_name=display_name,
            settable=settable,
            user_defined=user_defined,
            private=private,
            element_id=element_id,
            element_type=element_type,
        )

        kvp_type = ParameterType.parse_kv_type_pair(self.type)
        if kvp_type is None:
            err_str = f"PropertyKeyValuePair type '{type}' was not a valid Key-Value Type Pair. Format should be: ['<key type>', '<value type>']"
            raise ValueError(err_str)

        # Create key parameter as a child
        key_param = Parameter(
            name=f"{name}.key",
            tooltip=key_tooltip or "Key for the key-value pair",
            type=kvp_type.key_type,
            default_value=key_default_value,
            ui_options=key_ui_options,
            traits=key_traits,
            converters=key_converters,
            validators=key_validators,
        )
        self.add_child(key_param)

        # Create value parameter as a child
        value_param = Parameter(
            name=f"{name}.value",
            tooltip=value_tooltip or "Value for the key-value pair",
            type=kvp_type.value_type,
            default_value=value_default_value,
            ui_options=value_ui_options,
            traits=value_traits,
            converters=value_converters,
            validators=value_validators,
        )
        self.add_child(value_param)

    def _custom_setter_for_property_type(self, value: Any) -> None:
        # Set it as normal.
        super()._custom_setter_for_property_type(value)

        # Ensure this is a valid Key-Value Pair
        base_type = super()._custom_getter_for_property_type()
        kvp_type = ParameterType.parse_kv_type_pair(base_type)
        if kvp_type is None:
            err_str = f"PropertyKeyValuePair type '{base_type}' was not a valid Key-Value Type Pair. Format should be: ['<key type>', '<value type>']"
            raise ValueError(err_str)

        # Update the key and value parameter types
        key_param = self.find_element_by_id(f"{self.name}.key")
        value_param = self.find_element_by_id(f"{self.name}.value")
        if isinstance(key_param, Parameter) and isinstance(value_param, Parameter):
            key_param.type = kvp_type.key_type
            value_param.type = kvp_type.value_type

    def get_key(self) -> Any:
        """Get the current value of the key parameter."""
        key_param = self.find_element_by_id(f"{self.name}.key")
        if isinstance(key_param, Parameter):
            return key_param.default_value
        return None

    def set_key(self, value: Any) -> None:
        """Set the value of the key parameter."""
        key_param = self.find_element_by_id(f"{self.name}.key")
        if isinstance(key_param, Parameter):
            key_param.default_value = value

    def get_value(self) -> Any:
        """Get the current value of the value parameter."""
        value_param = self.find_element_by_id(f"{self.name}.value")
        if isinstance(value_param, Parameter):
            return value_param.default_value
        return None

    def set_value(self, value: Any) -> None:
        """Set the value of the value parameter."""
        value_param = self.find_element_by_id(f"{self.name}.value")
        if isinstance(value_param, Parameter):
            value_param.default_value = value


class ParameterDictionary(ParameterContainer):
    _kvp_type: ParameterType.KeyValueTypePair
    _original_traits: set[Trait.__class__ | Trait]

    def __init__(  # noqa: PLR0913
        self,
        name: str,
        tooltip: str | list[dict],
        type: str | None = None,  # noqa: A002
        default_value: Any = None,
        tooltip_as_input: str | list[dict] | None = None,
        tooltip_as_property: str | list[dict] | None = None,
        tooltip_as_output: str | list[dict] | None = None,
        allowed_modes: set[ParameterMode] | None = None,
        ui_options: dict | None = None,
        traits: set[Trait.__class__ | Trait] | None = None,
        converters: list[Callable[[Any], Any]] | None = None,
        validators: list[Callable[[Parameter, Any], None]] | None = None,
        *,
        display_name: str | None = None,
        settable: bool = True,
        user_defined: bool = False,
        private: bool = False,
        element_id: str | None = None,
        element_type: str | None = None,
    ):
        # Remember: we're a Parameter, too, just like everybody else.
        super().__init__(
            name=name,
            tooltip=tooltip,
            type=type,
            default_value=default_value,
            tooltip_as_input=tooltip_as_input,
            tooltip_as_property=tooltip_as_property,
            tooltip_as_output=tooltip_as_output,
            allowed_modes=allowed_modes,
            ui_options=ui_options,
            traits=traits,
            converters=converters,
            validators=validators,
            display_name=display_name,
            settable=settable,
            user_defined=user_defined,
            private=private,
            element_id=element_id,
            element_type=element_type,
        )

        if traits:
            self._original_traits = traits
        else:
            self._original_traits = set()

    def _custom_getter_for_property_type(self) -> str:
        base_type = super()._custom_getter_for_property_type()
        # NOT A TYPO. Internally, we are representing the Dict as a List to preserve the order.
        result = f"list[{base_type}]"
        return result

    def _custom_setter_for_property_type(self, value: Any) -> None:
        # Set it as normal.
        super()._custom_setter_for_property_type(value)

        # We set the type value, now get it back.
        base_type = super()._custom_getter_for_property_type()

        # Ensure this is a valid Key-Value Pair
        base_type = super()._custom_getter_for_property_type()
        kvp_type = ParameterType.parse_kv_type_pair(base_type)
        if kvp_type is None:
            err_str = f"PropertyDictionary type '{base_type}' was not a valid Key-Value Type Pair. Format should be: ['<key type>', '<value type>']"
            raise ValueError(err_str)
        self._kvp_type = kvp_type

    def _custom_getter_for_property_input_types(self) -> list[str]:
        # For every valid input type, also accept a list variant of that for the CONTAINER Parameter only.
        # Children still use the input types given to them.
        base_input_types = super()._custom_getter_for_property_input_types()
        result = []
        for base_input_type in base_input_types:
            container_variant = f"dict[{base_input_type}]"
            result.append(container_variant)

        return result

    def _custom_getter_for_property_output_type(self) -> str:
        base_type = super()._custom_getter_for_property_output_type()
        result = f"dict[{base_type}]"
        return result

    def __len__(self) -> int:
        # Returns the number of child Parameters. Just do the top level.
        param_children = self.find_elements_by_type(element_type=ParameterKeyValuePair, find_recursively=False)
        return len(param_children)

    def __getitem__(self, key: int) -> ParameterKeyValuePair:
        count = 0
        for child in self._children:
            if isinstance(child, ParameterKeyValuePair):
                if count == key:
                    # Found it.
                    return child
                count += 1

        # If we fell out of the for loop, we had a bad value.
        err_str = f"Attempted to get a Parameter Dictionary index {key}, which was out of range."
        raise KeyError(err_str)

    def add_key_value_pair(self) -> ParameterKeyValuePair:
        # Generate a name. This needs to be UNIQUE because children need
        # to be tracked as individuals and not as indices/keys in the dict.
        name = f"{self.name}_ParameterDictUniqueParamID_{uuid.uuid4().hex!s}"

        param = ParameterKeyValuePair(
            name=name,
            tooltip=self.tooltip,
            type=self._type,
            default_value=self.default_value,
            tooltip_as_input=self.tooltip_as_input,
            tooltip_as_output=self.tooltip_as_output,
            tooltip_as_property=self.tooltip_as_property,
            allowed_modes=self.allowed_modes,
            ui_options=self.ui_options,
            traits=self._original_traits,
            converters=self.converters,
            validators=self.validators,
            settable=self.settable,
            user_defined=self.user_defined,
        )

        # Add at the end.
        self.add_child(param)

        return param


# TODO: https://github.com/griptape-ai/griptape-nodes/issues/858


@dataclass(eq=False)
class Trait(ABC, BaseNodeElement):
    def __hash__(self) -> int:
        # Use a unique, immutable attribute for hashing
        return hash(self.element_id)

    def __eq__(self, other: object) -> bool:
        if not (isinstance(other, Trait)):
            return False
        return self.to_dict() == other.to_dict()

    def to_dict(self) -> dict[str, Any]:
        updated = super().to_dict()
        updated["trait_ui_options"] = self.ui_options_for_trait()
        updated["trait_name"] = self.__class__.__name__
        updated["trait_display_options"] = self.display_options_for_trait()
        return updated

    @classmethod
    @abstractmethod
    def get_trait_keys(cls) -> list[str]:
        """This will return keys that trigger this trait."""

    def ui_options_for_trait(self) -> dict:
        """Returns a list of UI options for the parameter as a list of strings or dictionaries."""
        return {}

    def display_options_for_trait(self) -> dict:
        """Returns a list of display options for the parameter as a dictionary."""
        return {}

    def converters_for_trait(self) -> list[Callable[[Any], Any]]:
        """Returns a list of methods to be applied as a convertor."""
        return []

    def validators_for_trait(self) -> list[Callable[[Parameter, Any]]]:
        """Returns a list of methods to be applied as a validator."""
        return []
