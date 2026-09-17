"""How an element reports a write: recording it, then sending what was recorded."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.exe_types.elements.base import BaseNodeElement


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


def emit_alter_element_event(element: BaseNodeElement) -> None:
    """Emit an AlterElementEvent if we have node context and the necessary dependencies."""
    if element._node_context is None:
        return

    # Imported here to avoid circular dependencies: the event modules reach the element tree.
    from griptape_nodes.retained_mode.events.base_events import ExecutionEvent, ExecutionGriptapeNodeEvent
    from griptape_nodes.retained_mode.events.parameter_events import AlterElementEvent
    from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

    # Create base event data using the existing to_event method
    # Create a modified event data that only includes changed fields
    event_data = {
        # Include base fields that should always be present
        "element_id": element.element_id,
        "element_type": element.element_type,
        "name": element.name,
        "node_name": element._node_context.name,
    }
    # If ui_options changed, send the complete ui_options from to_dict()
    complete_dict = element.to_dict()
    if "ui_options" in complete_dict:
        element._changes["ui_options"] = complete_dict["ui_options"]
    # Also handle trait_ui_options for traits
    if "trait_ui_options" in complete_dict:
        element._changes["trait_ui_options"] = complete_dict["trait_ui_options"]
    if "badge" in complete_dict:
        element._changes["badge"] = complete_dict["badge"]

    event_data.update(element._changes)
    # Publish the event
    event = ExecutionGriptapeNodeEvent(
        wrapped_event=ExecutionEvent(payload=AlterElementEvent(element_details=event_data))
    )

    GriptapeNodes.EventManager().put_event(event)
    element._changes.clear()
