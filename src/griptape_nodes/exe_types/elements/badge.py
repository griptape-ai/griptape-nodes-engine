"""Badge state an element renders next to itself, and every write that reaches it."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any, Literal, get_args

from griptape_nodes.exe_types.elements.node_messages import NodeMessagePayload, NodeMessageResult

if TYPE_CHECKING:
    from griptape_nodes.exe_types.elements.base import BaseNodeElement

logger = logging.getLogger("griptape_nodes")

# Badge variant type for element badge (aligned with ParameterMessage.VariantType, excluding "none")
BadgeVariantType = Literal["info", "warning", "error", "success", "tip", "link", "docs", "help", "note", "cloud-upload"]
VALID_BADGE_VARIANTS: frozenset[str] = frozenset(get_args(BadgeVariantType))


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


# The fields a badge write may name, in declaration order. Underscored fields are element wiring.
BADGE_FIELD_NAMES: tuple[str, ...] = tuple(
    declared.name for declared in fields(BadgeData) if not declared.name.startswith("_")
)


def write_badge_fields(element: BaseNodeElement, values: dict[str, Any]) -> None:
    """Assign the named badge fields, creating the badge if the element has none, and report the change."""
    badge = element._badge
    if badge is None:
        badge = BadgeData()
        element._badge = badge
    badge._parent_element = element
    for name, value in values.items():
        setattr(badge, name, value)
    element.track_change("badge", badge.to_dict())


def write_badge_message_data(element: BaseNodeElement, data: dict) -> None:
    """Assign the badge fields a message named, falling back to the 'info' variant if it named another."""
    values = {name: data[name] for name in BADGE_FIELD_NAMES if name in data}
    if "variant" in values and values["variant"] not in VALID_BADGE_VARIANTS:
        msg = f"{element.__class__.__name__} received invalid badge variant {values['variant']}; using 'info'. Valid: {sorted(VALID_BADGE_VARIANTS)}"
        logger.error(msg)
        values["variant"] = "info"
    write_badge_fields(element, values)


def set_initial_badge(element: BaseNodeElement, badge: BadgeData) -> None:
    """Report a badge an element was constructed with, field by field."""
    element.set_badge(
        variant=badge.variant,
        title=badge.title,
        message=badge.message,
        icon=badge.icon,
        color=badge.color,
        hide=badge.hide,
        hide_clear_button=badge.hide_clear_button,
    )


def handle_badge_message(
    element: BaseNodeElement, message_type: str, message: NodeMessagePayload | None
) -> NodeMessageResult | None:
    """Handle badge-related messages; return result if handled, None otherwise."""
    msg_lower = message_type.lower()
    match msg_lower:
        case "clear_badge":
            element.clear_badge()
            return NodeMessageResult(
                success=True,
                details="Badge cleared",
                response=None,
                altered_workflow_state=False,
            )
        case "get_badge":
            badge = element.get_badge()
            badge_dict = badge.to_dict() if badge is not None else None
            return NodeMessageResult(
                success=True,
                details="Badge retrieved",
                response=NodeMessagePayload(data=badge_dict),
                altered_workflow_state=False,
            )
        case "set_badge":
            if message is not None and hasattr(message, "data") and isinstance(message.data, dict):
                write_badge_message_data(element, message.data)
            badge = element.get_badge()
            badge_dict = badge.to_dict() if badge is not None else None
            return NodeMessageResult(
                success=True,
                details="Badge updated",
                response=NodeMessagePayload(data=badge_dict),
                altered_workflow_state=False,
            )
        case "clear_badge_display":
            element.dismiss_badge()
            return NodeMessageResult(
                success=True,
                details="Badge dismissed",
                response=None,
                altered_workflow_state=False,
            )
        case _:
            # Not a badge message; return None so caller can delegate to other handlers (e.g. on_click).
            return None
