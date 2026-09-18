import logging
from collections.abc import Callable
from typing import Literal, get_args

import attrs

from griptape_nodes.exe_types.core_types import (
    BEHAVIOR,
    WIRING,
    NodeMessagePayload,
    NodeMessageResult,
    Trait,
    default_element_id,
)

# Don't export callback types - let users import explicitly

logger = logging.getLogger("griptape_nodes")


# Type aliases using Literals
ButtonVariant = Literal[
    "default",
    "secondary",
    "destructive",
    "outline",
    "ghost",
    "link",
]

ButtonSize = Literal[
    "default",
    "sm",
    "icon",
]

ButtonState = Literal[
    "normal",
    "disabled",
    "loading",
    "hidden",
]

IconPosition = Literal[
    "left",
    "right",
]


class ButtonDetailsMessagePayload(NodeMessagePayload):
    """Payload containing complete button details and status information."""

    label: str
    variant: str
    size: str
    state: str
    icon: str | None = None
    icon_class: str | None = None
    icon_position: str | None = None
    full_width: bool = False
    loading_label: str | None = None
    loading_icon: str | None = None
    loading_icon_class: str | None = None
    tooltip: str | None = None


class ModalContentPayload(NodeMessagePayload):
    """Payload containing content to be displayed in a modal dialog."""

    clipboard_copyable_content: str | None = None
    render_url: str | None = None
    title: str | None = None


class OnClickMessageResultPayload(NodeMessagePayload):
    """Payload for button click result messages."""

    button_details: ButtonDetailsMessagePayload
    modal_content: ModalContentPayload | None = None
    href: str | None = None


class SetButtonStatusMessagePayload(NodeMessagePayload):
    """Payload for setting button status with explicit field updates."""

    updates: dict[str, str | bool | None]


def _build_link_handler(url: str) -> Callable:
    """Build a link callback from saved URL state."""

    def handler(
        button: "Button",  # noqa: ARG001
        button_details: ButtonDetailsMessagePayload,
    ) -> NodeMessageResult:
        return NodeMessageResult(
            success=True,
            details="Opening URL",
            response=OnClickMessageResultPayload(
                button_details=button_details,
                href=url,
            ),
            altered_workflow_state=False,
        )

    return handler


class Button(Trait):
    type OnClickCallback = Callable[[Button, ButtonDetailsMessagePayload], NodeMessageResult | None]
    type GetButtonStateCallback = Callable[[Button, ButtonDetailsMessagePayload], NodeMessageResult | None]

    ON_CLICK_MESSAGE_TYPE = "on_click"
    GET_BUTTON_STATUS_MESSAGE_TYPE = "get_button_status"
    SET_BUTTON_STATUS_MESSAGE_TYPE = "set_button_status"

    element_id: str = attrs.field(default="Button", converter=default_element_id, metadata=WIRING)

    label: str = attrs.field(default="")  # Allows a button with no text.
    variant: ButtonVariant = attrs.field(default="secondary")
    size: ButtonSize = attrs.field(default="default")
    state: ButtonState = attrs.field(default="normal")
    icon: str | None = attrs.field(default=None)
    icon_class: str | None = attrs.field(default=None)
    icon_position: IconPosition | None = attrs.field(default=None)
    full_width: bool = attrs.field(default=False)
    loading_label: str | None = attrs.field(default=None)
    loading_icon: str | None = attrs.field(default=None)
    loading_icon_class: str | None = attrs.field(default=None)
    tooltip: str | None = attrs.field(default=None)

    # State a handler may leave stored but unread: reading always prefers the handler, so a
    # link saved alongside one is inert rather than cleared. See ``on_click_callback``.
    button_link: str | None = attrs.field(default=None)

    # A link callback is derived state and must not be saved as node behavior.
    on_click_handler: OnClickCallback | None = attrs.field(
        default=None, alias="on_click", metadata=BEHAVIOR, kw_only=True
    )
    get_button_state_callback: GetButtonStateCallback | None = attrs.field(
        default=None, alias="get_button_state", metadata=BEHAVIOR, kw_only=True
    )

    def __attrs_post_init__(self) -> None:
        # Before the element is adopted, so a rejected button is never half-attached.
        if self.button_link is not None and self.on_click_handler is not None:
            error_msg = (
                "Cannot specify both 'button_link' and 'on_click' for Button. "
                "Use 'button_link' for simple URL navigation or 'on_click' for custom behavior."
            )
            raise ValueError(error_msg)
        super().__attrs_post_init__()

    @property
    def on_click_callback(self) -> OnClickCallback | None:
        """The callback a click fires: a handler set by node code wins over a derived link."""
        if self.on_click_handler is not None:
            return self.on_click_handler
        if self.button_link is None:
            return None
        return _build_link_handler(self.button_link)

    @on_click_callback.setter
    def on_click_callback(self, callback: OnClickCallback | None) -> None:
        self.on_click_handler = callback

    def get_button_details(self, state: ButtonState | None = None) -> ButtonDetailsMessagePayload:
        """Create a ButtonDetailsMessagePayload with current or specified button state."""
        return ButtonDetailsMessagePayload(
            label=self.label,
            variant=self.variant,
            size=self.size,
            state=state or self.state,
            icon=self.icon,
            icon_class=self.icon_class,
            icon_position=self.icon_position,
            full_width=self.full_width,
            loading_label=self.loading_label,
            loading_icon=self.loading_icon,
            loading_icon_class=self.loading_icon_class,
            tooltip=self.tooltip,
        )

    def ui_options_for_trait(self) -> dict:
        """Generate UI options for the button trait with all styling properties."""
        options = {
            "button_label": self.label,
            "variant": self.variant,
            "size": self.size,
            "state": self.state,
            "full_width": self.full_width,
        }

        # Only include icon properties if icon is specified
        if self.icon:
            options["button_icon"] = self.icon
            options["iconPosition"] = self.icon_position or "left"
            if self.icon_class:
                options["icon_class"] = self.icon_class

        # Include loading properties if specified
        if self.loading_label:
            options["loading_label"] = self.loading_label
        if self.loading_icon:
            options["loading_icon"] = self.loading_icon
        if self.loading_icon_class:
            options["loading_icon_class"] = self.loading_icon_class

        # Include tooltip if specified
        if self.tooltip:
            options["tooltip"] = self.tooltip

        return options

    def on_message_received(self, message_type: str, message: NodeMessagePayload | None) -> NodeMessageResult | None:  # noqa: C901, PLR0911, PLR0912
        """Handle messages sent to this button trait.

        Args:
            message_type: String indicating the message type for parsing
            message: Message payload as NodeMessagePayload or None

        Returns:
            NodeMessageResult | None: Result if handled, None if no handler available
        """
        match message_type.lower():
            case self.ON_CLICK_MESSAGE_TYPE:
                if self.on_click_callback is not None:
                    try:
                        # Pre-fill button details with current state and pass to callback
                        button_details = self.get_button_details()
                        # Include original message's data if present (for payloadData support)
                        if message is not None:
                            # Handle both NodeMessagePayload objects and dict messages
                            if isinstance(message, NodeMessagePayload) and message.data is not None:
                                button_details.data = message.data
                            elif isinstance(message, dict) and "data" in message:
                                button_details.data = message["data"]
                        result = self.on_click_callback(self, button_details)

                        # If callback returns None, provide optimistic success result
                        if result is None:
                            result = NodeMessageResult(
                                success=True,
                                details=f"Button '{self.label}' clicked successfully",
                                response=button_details,
                            )
                        return result  # noqa: TRY300
                    except Exception as e:
                        return NodeMessageResult(
                            success=False,
                            details=f"Button '{self.label}' callback failed: {e!s}",
                            response=None,
                        )

                # Log debug message and fall through if no callback specified
                logger.debug("Button '%s' was clicked, but no on_click_callback was specified.", self.label)

            case self.GET_BUTTON_STATUS_MESSAGE_TYPE:
                # Use custom callback if provided, otherwise use default implementation
                if self.get_button_state_callback is not None:
                    try:
                        # Pre-fill button details with current state and pass to callback
                        button_details = self.get_button_details()
                        result = self.get_button_state_callback(self, button_details)

                        # If callback returns None, provide optimistic success result
                        if result is None:
                            result = NodeMessageResult(
                                success=True,
                                details=f"Button '{self.label}' state retrieved successfully",
                                response=button_details,
                                altered_workflow_state=False,
                            )
                        return result  # noqa: TRY300
                    except Exception as e:
                        return NodeMessageResult(
                            success=False,
                            details=f"Button '{self.label}' get_button_state callback failed: {e!s}",
                            response=None,
                        )
                else:
                    return self._default_get_button_status(message_type, message)

            case self.SET_BUTTON_STATUS_MESSAGE_TYPE:
                return self._handle_set_button_status(message)

        # Delegate to parent implementation for unhandled messages or no callback
        return super().on_message_received(message_type, message)

    def _default_get_button_status(
        self,
        message_type: str,  # noqa: ARG002
        message: NodeMessagePayload | None,  # noqa: ARG002
    ) -> NodeMessageResult:
        """Default implementation for get_button_status that returns current button details."""
        button_details = self.get_button_details()

        return NodeMessageResult(
            success=True,
            details=f"Button '{self.label}' details retrieved",
            response=button_details,
            altered_workflow_state=False,
        )

    def _handle_set_button_status(self, message: NodeMessagePayload | None) -> NodeMessageResult:  # noqa: C901
        """Handle set button status messages by updating fields specified in the updates dict."""
        if not message:
            return NodeMessageResult(
                success=False,
                details="No message payload provided for set_button_status",
                response=None,
                altered_workflow_state=False,
            )

        if not isinstance(message, SetButtonStatusMessagePayload):
            return NodeMessageResult(
                success=False,
                details="Invalid message payload type for set_button_status",
                response=None,
                altered_workflow_state=False,
            )

        # Track which fields were updated
        updated_fields = []
        validation_errors = []

        # Valid field names and their expected types
        valid_fields = {
            "label": str,
            "variant": str,  # Will validate against ButtonVariant literals
            "size": str,  # Will validate against ButtonSize literals
            "state": str,  # Will validate against ButtonState literals
            "icon": str,
            "icon_class": str,
            "icon_position": str,  # Will validate against IconPosition literals
            "full_width": bool,
            "loading_label": str,
            "loading_icon": str,
            "loading_icon_class": str,
        }

        # Process each update
        for field_name, value in message.updates.items():
            # Check if field is valid
            if field_name not in valid_fields:
                validation_errors.append(f"Invalid field: {field_name}")
                continue

            # Type check if value is not None
            if value is not None and not isinstance(value, valid_fields[field_name]):
                validation_errors.append(
                    f"Invalid type for {field_name}: expected {valid_fields[field_name].__name__}, got {type(value).__name__}"
                )
                continue

            # Additional validation for Literal types
            if field_name == "variant" and value is not None and value not in get_args(ButtonVariant):
                validation_errors.append(f"Invalid variant: {value}")
                continue
            if field_name == "size" and value is not None and value not in get_args(ButtonSize):
                validation_errors.append(f"Invalid size: {value}")
                continue
            if field_name == "state" and value is not None and value not in get_args(ButtonState):
                validation_errors.append(f"Invalid state: {value}")
                continue
            if field_name == "icon_position" and value is not None and value not in get_args(IconPosition):
                validation_errors.append(f"Invalid icon_position: {value}")
                continue

            # Update the field
            setattr(self, field_name, value)
            updated_fields.append(field_name)

        # Return validation errors if any
        if validation_errors:
            return NodeMessageResult(
                success=False,
                details=f"Validation errors: {'; '.join(validation_errors)}",
                response=None,
                altered_workflow_state=False,
            )

        # Return success with updated button details
        button_details = self.get_button_details()
        fields_str = ", ".join(updated_fields) if updated_fields else "no fields"

        return NodeMessageResult(
            success=True,
            details=f"Button '{self.label}' updated ({fields_str})",
            response=button_details,
            altered_workflow_state=True,
        )
