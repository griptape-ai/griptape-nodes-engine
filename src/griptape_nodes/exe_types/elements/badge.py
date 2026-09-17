"""Badge state an element renders next to itself."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, get_args

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
