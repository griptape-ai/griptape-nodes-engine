"""ParameterButton component for button inputs with enhanced UI options."""

from collections.abc import Callable
from typing import Any, Literal

from griptape_nodes.exe_types.core_types import (
    BadgeData,
    NodeMessageResult,
    Parameter,
    ParameterMode,
    Trait,
)
from griptape_nodes.traits.button import Button, ButtonDetailsMessagePayload, OnClickMessageResultPayload

# Type aliases matching Button trait
ButtonVariant = Literal["default", "secondary", "destructive", "outline", "ghost", "link"]
ButtonSize = Literal["default", "sm", "icon"]
ButtonState = Literal["normal", "disabled", "loading", "hidden"]
IconPosition = Literal["left", "right"]


class ParameterButton(Parameter):
    """A specialized Parameter class for button inputs with enhanced UI options.

    This class provides a convenient way to create button parameters with all the
    styling and behavior options from the Button trait. It exposes these UI options
    as direct properties for easy runtime modification.

    Example:
        # Label is the display text, value is separate stored data
        param = ParameterButton(
            name="button_param",
            label="Click me",  # Display text on the button
            default_value="some stored data",  # Stored value (can be any data)
            variant="secondary",
            icon="check",
            icon_class="text-green-500",
            full_width=True
        )

        # With callback function (just like Button trait)
        def handle_click(button, details):
            print(f"Button {details.label} was clicked!")
            return NodeMessageResult(success=True, details="Clicked!")

        param3 = ParameterButton(
            name="callback_button",
            label="Submit",
            default_value="submit_action",
            on_click=handle_click
        )

        # Or use href for simple link opening
        link_button = ParameterButton(
            name="docs_link",
            label="View Docs",
            href="https://docs.example.com"
        )

        # Change properties at runtime (just like Button trait)
        # Label and value are independent
        param.label = "New Label"  # Changes display text only
        param.default_value = "new_data"  # Changes stored value only
        param.variant = "destructive"
    """

    def __init__(  # noqa: PLR0913
        self,
        name: str,
        tooltip: str | None = None,
        *,
        type: str = "button",  # noqa: A002
        default_value: Any = None,
        tooltip_as_input: str | None = None,
        tooltip_as_property: str | None = None,
        tooltip_as_output: str | None = None,
        allowed_modes: set[ParameterMode] | None = None,
        traits: set[type[Trait] | Trait] | None = None,
        converters: list[Callable[[Any], Any]] | None = None,
        validators: list[Callable[[Parameter, Any], None]] | None = None,
        ui_options: dict | None = None,
        # Button-specific parameters (matching Button trait defaults)
        label: str = "",  # Allows a button with no text, matching Button trait
        variant: ButtonVariant = "secondary",
        size: ButtonSize = "default",
        state: ButtonState = "normal",
        icon: str | None = None,
        icon_class: str | None = None,
        icon_position: IconPosition | None = None,
        full_width: bool = True,
        loading_label: str | None = None,
        loading_icon: str | None = None,
        loading_icon_class: str | None = None,
        on_click: Button.OnClickCallback | None = None,
        get_button_state: Button.GetButtonStateCallback | None = None,
        href: str | None = None,
        hide: bool | None = None,
        hide_label: bool = False,
        hide_property: bool = False,
        display_name: str | None = None,
        allow_input: bool = False,
        allow_property: bool = True,
        allow_output: bool = False,
        settable: bool = True,
        serializable: bool = True,
        user_defined: bool = False,
        private: bool = False,
        secret: bool = False,
        element_id: str | None = None,
        element_type: str | None = None,
        parent_container_name: str | None = None,
        badge: BadgeData | None = None,
    ) -> None:
        """Initialize a button parameter with enhanced UI options.

        Args:
            name: Parameter name
            tooltip: Parameter tooltip
            type: Parameter type (ignored, always "any" for ParameterButton)
            default_value: Default parameter value
            tooltip_as_input: Tooltip for input mode
            tooltip_as_property: Tooltip for property mode
            tooltip_as_output: Tooltip for output mode
            allowed_modes: Allowed parameter modes
            traits: Parameter traits
            converters: Parameter converters
            validators: Parameter validators
            ui_options: Dictionary of UI options
            label: Button label text
            variant: Button variant style
            size: Button size
            state: Button state (normal, disabled, loading, hidden)
            icon: Icon identifier/name
            icon_class: CSS class for icon
            icon_position: Position of icon relative to label
            full_width: Whether button should take full width
            loading_label: Label to show when in loading state
            loading_icon: Icon to show when in loading state
            loading_icon_class: CSS class for loading icon
            on_click: Callback function for button clicks
            get_button_state: Callback function to get button state
            href: URL to open when button is clicked (alternative to on_click callback)
            hide: Whether to hide the entire parameter
            hide_label: Whether to hide the parameter label
            hide_property: Whether to hide the parameter in property mode
            display_name: Human-readable label for the parameter (overrides default parameter name)
            allow_input: Whether to allow input mode
            allow_property: Whether to allow property mode
            allow_output: Whether to allow output mode
            settable: Whether the parameter is settable
            serializable: Whether the parameter is serializable
            user_defined: Whether the parameter is user-defined
            private: Whether this parameter is private
            secret: Whether this parameter holds a secret value excluded from plaintext metadata
            element_id: Element ID
            element_type: Element type
            parent_container_name: Name of parent container
            badge: Optional BadgeData for initial badge (title, message, variant, and whether to show a clear button).
        """
        # Build ui_options dictionary from the provided UI-specific parameters
        if ui_options is None:
            ui_options = {}
        else:
            ui_options = ui_options.copy()

        # If href is provided but no on_click callback, create a simple callback that opens the link
        final_on_click = on_click
        if href is not None and on_click is None:
            final_on_click = ParameterButton._create_href_callback(href)

        # Create Button trait with all the button-specific options
        # Label and value are separate - label is display text, value is stored data
        button_trait = Button(
            label=label,
            variant=variant,
            size=size,
            state=state,
            icon=icon,
            icon_class=icon_class,
            icon_position=icon_position,
            full_width=full_width,
            loading_label=loading_label,
            loading_icon=loading_icon,
            loading_icon_class=loading_icon_class,
            tooltip=tooltip,
            on_click=final_on_click,
            get_button_state=get_button_state,
        )

        # Store href for property access
        self._href = href

        # Merge button UI options into parameter UI options
        button_ui_options = button_trait.ui_options_for_trait()
        ui_options.update(button_ui_options)

        # Add button trait to traits set
        # Button is a Trait, so it can be added to the traits set
        if traits is None:
            final_traits: set[type[Trait] | Trait] = {button_trait}  # type: ignore[assignment]
        else:
            final_traits = set(traits) | {button_trait}  # type: ignore[assignment]

        # Call parent with explicit parameters
        super().__init__(
            name=name,
            tooltip=tooltip,
            type="button",  # Button parameter type
            input_types=["str", "any"],
            output_type="str",
            default_value=default_value,
            tooltip_as_input=tooltip_as_input,
            tooltip_as_property=tooltip_as_property,
            tooltip_as_output=tooltip_as_output,
            allowed_modes=allowed_modes,
            traits=final_traits,
            converters=converters,
            validators=validators,
            ui_options=ui_options,
            hide=hide,
            hide_label=hide_label,
            hide_property=hide_property,
            display_name=display_name,
            allow_input=allow_input,
            allow_property=allow_property,
            allow_output=allow_output,
            settable=settable,
            serializable=serializable,
            user_defined=user_defined,
            private=private,
            secret=secret,
            element_id=element_id,
            element_type=element_type,
            parent_container_name=parent_container_name,
            badge=badge,
        )

        # Store button trait reference for property access
        self._button_trait = button_trait

    @staticmethod
    def _create_href_callback(href: str) -> Button.OnClickCallback:
        """Create a simple callback that opens a link when the button is clicked."""

        def href_callback(
            button: Button,  # noqa: ARG001
            button_details: ButtonDetailsMessagePayload,
        ) -> NodeMessageResult:
            return NodeMessageResult(
                success=True,
                details=f"Opening link: {href}",
                response=OnClickMessageResultPayload(
                    button_details=button_details,
                    href=href,
                ),
                altered_workflow_state=False,
            )

        return href_callback

    def _get_button_trait(self) -> Button:
        """Get the Button trait associated with this parameter."""
        # Find the Button trait in the parameter's children (traits are stored as children)
        for child in self.children:
            if isinstance(child, Button):
                return child
        # Fallback: should not happen if initialization is correct
        return self._button_trait

    @property
    def label(self) -> str:
        """Get the button label (primary interface, like Button trait)."""
        # Label is the primary property - get it directly from button trait
        return self._get_button_trait().label

    @label.setter
    def label(self, value: str) -> None:
        """Set the button label (display text only - separate from parameter value)."""
        # Update button trait (primary source of truth for display)
        self._get_button_trait().label = value
        # Update UI options
        self.update_ui_options_key("button_label", value)

    @property
    def variant(self) -> ButtonVariant:
        """Get the button variant."""
        return self._get_button_trait().variant

    @variant.setter
    def variant(self, value: ButtonVariant) -> None:
        """Set the button variant."""
        self._get_button_trait().variant = value
        self.update_ui_options_key("variant", value)

    @property
    def size(self) -> ButtonSize:
        """Get the button size."""
        return self._get_button_trait().size

    @size.setter
    def size(self, value: ButtonSize) -> None:
        """Set the button size."""
        self._get_button_trait().size = value
        self.update_ui_options_key("size", value)

    @property
    def state(self) -> ButtonState:
        """Get the button state."""
        return self._get_button_trait().state

    @state.setter
    def state(self, value: ButtonState) -> None:
        """Set the button state."""
        self._get_button_trait().state = value
        self.update_ui_options_key("state", value)

    @property
    def icon(self) -> str | None:
        """Get the button icon."""
        return self._get_button_trait().icon

    @icon.setter
    def icon(self, value: str | None) -> None:
        """Set the button icon."""
        self._get_button_trait().icon = value
        if value is None:
            ui_options = self.ui_options.copy()
            ui_options.pop("button_icon", None)
            self.ui_options = ui_options
        else:
            self.update_ui_options_key("button_icon", value)

    @property
    def icon_class(self) -> str | None:
        """Get the button icon class."""
        return self._get_button_trait().icon_class

    @icon_class.setter
    def icon_class(self, value: str | None) -> None:
        """Set the button icon class."""
        self._get_button_trait().icon_class = value
        if value is None:
            ui_options = self.ui_options.copy()
            ui_options.pop("icon_class", None)
            self.ui_options = ui_options
        else:
            self.update_ui_options_key("icon_class", value)

    @property
    def icon_position(self) -> IconPosition | None:
        """Get the button icon position."""
        return self._get_button_trait().icon_position

    @icon_position.setter
    def icon_position(self, value: IconPosition | None) -> None:
        """Set the button icon position."""
        self._get_button_trait().icon_position = value
        if value is None:
            ui_options = self.ui_options.copy()
            ui_options.pop("iconPosition", None)
            self.ui_options = ui_options
        else:
            self.update_ui_options_key("iconPosition", value)

    @property
    def full_width(self) -> bool:
        """Get whether the button is full width."""
        return self._get_button_trait().full_width

    @full_width.setter
    def full_width(self, value: bool) -> None:
        """Set whether the button is full width."""
        self._get_button_trait().full_width = value
        self.update_ui_options_key("full_width", value)

    @property
    def loading_label(self) -> str | None:
        """Get the loading label."""
        return self._get_button_trait().loading_label

    @loading_label.setter
    def loading_label(self, value: str | None) -> None:
        """Set the loading label."""
        self._get_button_trait().loading_label = value
        if value is None:
            ui_options = self.ui_options.copy()
            ui_options.pop("loading_label", None)
            self.ui_options = ui_options
        else:
            self.update_ui_options_key("loading_label", value)

    @property
    def loading_icon(self) -> str | None:
        """Get the loading icon."""
        return self._get_button_trait().loading_icon

    @loading_icon.setter
    def loading_icon(self, value: str | None) -> None:
        """Set the loading icon."""
        self._get_button_trait().loading_icon = value
        if value is None:
            ui_options = self.ui_options.copy()
            ui_options.pop("loading_icon", None)
            self.ui_options = ui_options
        else:
            self.update_ui_options_key("loading_icon", value)

    @property
    def loading_icon_class(self) -> str | None:
        """Get the loading icon class."""
        return self._get_button_trait().loading_icon_class

    @loading_icon_class.setter
    def loading_icon_class(self, value: str | None) -> None:
        """Set the loading icon class."""
        self._get_button_trait().loading_icon_class = value
        if value is None:
            ui_options = self.ui_options.copy()
            ui_options.pop("loading_icon_class", None)
            self.ui_options = ui_options
        else:
            self.update_ui_options_key("loading_icon_class", value)

    @property
    def on_click_callback(self) -> Button.OnClickCallback | None:
        """Get the on_click callback."""
        return self._get_button_trait().on_click_callback

    @on_click_callback.setter
    def on_click_callback(self, value: Button.OnClickCallback | None) -> None:
        """Set the on_click callback."""
        self._get_button_trait().on_click_callback = value

    @property
    def get_button_state_callback(self) -> Button.GetButtonStateCallback | None:
        """Get the get_button_state callback."""
        return self._get_button_trait().get_button_state_callback

    @get_button_state_callback.setter
    def get_button_state_callback(self, value: Button.GetButtonStateCallback | None) -> None:
        """Set the get_button_state callback."""
        self._get_button_trait().get_button_state_callback = value

    @property
    def href(self) -> str | None:
        """Get the href URL that will be opened when the button is clicked."""
        return getattr(self, "_href", None)

    @href.setter
    def href(self, value: str | None) -> None:
        """Set the href URL to open when button is clicked.

        This will replace any existing on_click callback with a simple link-opening callback.
        """
        self._href = value
        if value is not None:
            # Replace on_click callback with href callback
            self.on_click_callback = ParameterButton._create_href_callback(value)
        else:
            # Clear the callback if href is removed
            self.on_click_callback = None
