"""ParameterJson component for JSON inputs with enhanced UI options."""

from collections.abc import Callable
from typing import Any

from json_repair import repair_json

from griptape_nodes.exe_types.core_types import BadgeData, Parameter, ParameterMode, Trait


class ParameterJson(Parameter):
    """A specialized Parameter class for JSON inputs with enhanced UI options.

    This class provides a convenient way to create JSON parameters with common
    UI customizations. It exposes these UI options as direct properties for easy runtime modification.

    Example:
        param = ParameterJson(
            name="data",
            tooltip="Enter JSON data",
            default_value={},
            button=True,
            button_label="Load JSON",
            button_icon="refresh"
        )
        param.accept_any = True  # Change conversion behavior at runtime
        param.button_label = "Reload"  # Change button label at runtime
        param.button_icon = "reload"  # Change button icon at runtime
    """

    def __init__(  # noqa: PLR0913
        self,
        name: str,
        tooltip: str | None = None,
        *,
        type: str = "json",  # noqa: A002, ARG002
        input_types: list[str] | None = None,  # noqa: ARG002
        output_type: str = "json",  # noqa: ARG002
        default_value: Any = None,
        tooltip_as_input: str | None = None,
        tooltip_as_property: str | None = None,
        tooltip_as_output: str | None = None,
        allowed_modes: set[ParameterMode] | None = None,
        traits: set[type[Trait] | Trait] | None = None,
        converters: list[Callable[[Any], Any]] | None = None,
        validators: list[Callable[[Parameter, Any], None]] | None = None,
        ui_options: dict | None = None,
        placeholder_text: str | None = '{"key": "value", "nested": {"key": "value"}}',
        accept_any: bool = True,
        button: bool = False,
        button_label: str | None = "Edit",
        button_icon: str | None = None,
        hide: bool | None = None,
        hide_label: bool = False,
        hide_property: bool = False,
        display_name: str | None = None,
        allow_input: bool = True,
        allow_property: bool = True,
        allow_output: bool = True,
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
        """Initialize a JSON parameter with enhanced UI options.

        Args:
            name: Parameter name
            tooltip: Parameter tooltip
            type: Parameter type (ignored, always "json" for ParameterJson)
            input_types: Allowed input types (ignored, set based on accept_any)
            output_type: Output type (ignored, always "json" for ParameterJson)
            default_value: Default parameter value
            tooltip_as_input: Tooltip for input mode
            tooltip_as_property: Tooltip for property mode
            tooltip_as_output: Tooltip for output mode
            allowed_modes: Allowed parameter modes
            traits: Parameter traits
            converters: Parameter converters
            validators: Parameter validators
            ui_options: Dictionary of UI options
            placeholder_text: Placeholder text shown in the input field when empty
            accept_any: Whether to accept any input type and convert to JSON (default: True)
            button: Whether to show a button in the UI (default: False)
            button_label: Label text for the button (default: "Edit")
            button_icon: Icon identifier/name for the button (optional)
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

        # Add JSON-specific UI options if they have values
        if placeholder_text is not None:
            ui_options["placeholder_text"] = placeholder_text
        if button:
            ui_options["button"] = button
        if button_label is not None:
            ui_options["button_label"] = button_label
        if button_icon is not None:
            ui_options["button_icon"] = button_icon

        # Set up JSON conversion based on accept_any setting
        if converters is None:
            existing_converters = []
        else:
            existing_converters = converters

        if accept_any:
            final_input_types = ["any"]
            final_converters = [self._convert_to_json, *existing_converters]
        else:
            final_input_types = ["json", "str", "dict"]
            final_converters = existing_converters

        # Call parent with explicit parameters, following ControlParameter pattern
        super().__init__(
            name=name,
            tooltip=tooltip,
            type="json",  # Always a json type for ParameterJson
            input_types=final_input_types,
            output_type="json",  # Always output as json
            default_value=default_value,
            tooltip_as_input=tooltip_as_input,
            tooltip_as_property=tooltip_as_property,
            tooltip_as_output=tooltip_as_output,
            allowed_modes=allowed_modes,
            traits=traits,
            converters=final_converters,
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

    def _convert_to_json(self, value: Any) -> dict | list | str | int | float | bool | None:
        """Convert any input value to JSON-compatible format.

        Uses json_repair for robust handling of malformed JSON strings.
        Handles various input types including strings, dicts, and other objects.

        Args:
            value: The value to convert to JSON

        Returns:
            JSON-compatible representation of the value (dict, list, or primitive)

        Raises:
            ValueError: If the value cannot be converted to JSON
        """
        # Handle None
        if value is None:
            return None

        # If it's already a dict, use it as is
        if isinstance(value, dict):
            return value

        # If it's already a list, use it as is
        if isinstance(value, list):
            return value

        # If it's a string, try to repair and parse it
        if isinstance(value, str):
            try:
                return repair_json(value)
            except Exception as e:
                msg = f"ParameterJson: Failed to repair and parse JSON string: {e}. Input: {value[:200]!r}"
                raise ValueError(msg) from e

        # For other types, convert to string and try to repair
        try:
            return repair_json(str(value))
        except Exception as e:
            msg = f"ParameterJson: Failed to convert input to JSON: {e}. Input type: {type(value)}, value: {value!r}"
            raise ValueError(msg) from e

    @property
    def placeholder_text(self) -> str | None:
        return self.ui_options.get("placeholder_text")

    @placeholder_text.setter
    def placeholder_text(self, value: str | None) -> None:
        if value is None:
            ui_options = self.ui_options.copy()
            ui_options.pop("placeholder_text", None)
            self.ui_options = ui_options
        else:
            self.update_ui_options_key("placeholder_text", value)

    @property
    def button(self) -> bool:
        """Get whether a button is shown in the UI.

        Returns:
            True if button is enabled, False otherwise
        """
        return self.ui_options.get("button", False)

    @button.setter
    def button(self, value: bool) -> None:
        """Set whether a button is shown in the UI.

        Args:
            value: Whether to show a button
        """
        if value:
            self.update_ui_options_key("button", value)
        else:
            ui_options = self.ui_options.copy()
            ui_options.pop("button", None)
            self.ui_options = ui_options

    @property
    def button_label(self) -> str | None:
        """Get the label text for the button.

        Returns:
            The button label if set, None otherwise
        """
        return self.ui_options.get("button_label")

    @button_label.setter
    def button_label(self, value: str | None) -> None:
        """Set the label text for the button.

        Args:
            value: The button label to use, or None to remove it
        """
        if value is None:
            ui_options = self.ui_options.copy()
            ui_options.pop("button_label", None)
            self.ui_options = ui_options
        else:
            self.update_ui_options_key("button_label", value)

    @property
    def button_icon(self) -> str | None:
        """Get the icon identifier/name for the button.

        Returns:
            The button icon if set, None otherwise
        """
        return self.ui_options.get("button_icon")

    @button_icon.setter
    def button_icon(self, value: str | None) -> None:
        """Set the icon identifier/name for the button.

        Args:
            value: The button icon to use, or None to remove it
        """
        if value is None:
            ui_options = self.ui_options.copy()
            ui_options.pop("button_icon", None)
            self.ui_options = ui_options
        else:
            self.update_ui_options_key("button_icon", value)
