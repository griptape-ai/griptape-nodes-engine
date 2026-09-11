import logging
from typing import TYPE_CHECKING, ClassVar, Literal, get_args

from griptape_nodes.exe_types.callback_binding import is_derived_from_state, mark_derived_from_state
from griptape_nodes.exe_types.core_types import NodeMessagePayload, NodeMessageResult, Trait

if TYPE_CHECKING:
    from collections.abc import Callable

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


class Button(Trait):
    # The two callbacks are behavior, not state, so they are never saved as state. They are
    # carried by method name instead, which is why they still need an attribute mapping.
    STATE_EXCLUDE: ClassVar[frozenset[str]] = frozenset({"on_click", "get_button_state"})
    STATE_ALIASES: ClassVar[dict[str, str]] = {
        "on_click": "on_click_callback",
        "get_button_state": "get_button_state_callback",
    }

    # Specific callback types for better type safety and clarity
    type OnClickCallback = Callable[[Button, ButtonDetailsMessagePayload], NodeMessageResult | None]
    type GetButtonStateCallback = Callable[[Button, ButtonDetailsMessagePayload], NodeMessageResult | None]

    # Static message type constants
    ON_CLICK_MESSAGE_TYPE = "on_click"
    GET_BUTTON_STATUS_MESSAGE_TYPE = "get_button_status"
    SET_BUTTON_STATUS_MESSAGE_TYPE = "set_button_status"

    def __init__(  # noqa: PLR0913
        self,
        *,
        label: str = "",  # Allows a button with no text.
        variant: ButtonVariant = "secondary",
        size: ButtonSize = "default",
        state: ButtonState = "normal",
        icon: str | None = None,
        icon_class: str | None = None,
        icon_position: IconPosition | None = None,
        full_width: bool = False,
        loading_label: str | None = None,
        loading_icon: str | None = None,
        loading_icon_class: str | None = None,
        tooltip: str | None = None,
        button_link: str | None = None,
        on_click: OnClickCallback | None = None,
        get_button_state: GetButtonStateCallback | None = None,
    ) -> None:
        super().__init__(element_id="Button")
        # Annotated here because setattr in on_message_received tells the type checker
        # nothing about what these hold.
        self.label: str = label
        self.variant: ButtonVariant = variant
        self.size: ButtonSize = size
        self.state: ButtonState = state
        self.icon: str | None = icon
        self.icon_class: str | None = icon_class
        self.icon_position: IconPosition | None = icon_position
        self.full_width: bool = full_width
        self.loading_label: str | None = loading_label
        self.loading_icon: str | None = loading_icon
        self.loading_icon_class: str | None = loading_icon_class
        self.tooltip: str | None = tooltip
        self.button_link: str | None = button_link

        # Validate that both button_link and on_click are not provided simultaneously
        if button_link is not None and on_click is not None:
            error_msg = (
                "Cannot specify both 'button_link' and 'on_click' for Button. "
                "Use 'button_link' for simple URL navigation or 'on_click' for custom behavior."
            )
            raise ValueError(error_msg)

        # If button_link is provided and no custom on_click handler, create a default handler
        if button_link is not None:
            self.on_click_callback = self._create_button_link_handler(button_link)
        else:
            self.on_click_callback = on_click
        self.get_button_state_callback = get_button_state

    def _create_button_link_handler(self, url: str) -> OnClickCallback:
        """Create a default handler for button_link URLs."""

        def handler(
            button: Button,  # noqa: ARG001
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

        # Rebuilt from button_link, which is saved as state, so this handler needs no name.
        return mark_derived_from_state(handler)

    def _recompute_derived_state(self) -> None:
        """Rebuild ``on_click_callback`` from a changed ``button_link``.

        Only touches the callback when it is itself derived from state, or absent, so a
        node-supplied ``on_click`` set by the constructor survives ``apply_state`` untouched.
        A plain ``None`` cannot represent that intent (there is nothing to preserve), so it is
        treated the same as derived: otherwise a button built with no ``button_link`` and no
        ``on_click`` would gain a saved link with no handler to fire it.
        """
        if self.on_click_callback is not None and not is_derived_from_state(self.on_click_callback):
            # The node's own handler won, so a saved link is dead: it belongs to a version of
            # this node that wired the button differently. Dropping it keeps the one-or-the-
            # other invariant the constructor enforces, and stops the next save recording a
            # link that a later build-from-scratch would revive as the handler instead.
            self.button_link = None
            return
        if self.button_link is None:
            self.on_click_callback = None
            return
        self.on_click_callback = self._create_button_link_handler(self.button_link)

    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return ["button", "addbutton"]

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
