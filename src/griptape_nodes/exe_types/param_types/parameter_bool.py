"""ParameterBool component for boolean inputs with enhanced UI options."""

from collections.abc import Callable
from typing import Any

from griptape_nodes.exe_types.core_types import BadgeData, Parameter, ParameterMode, Trait


class ParameterBool(Parameter):
    """A specialized Parameter class for boolean inputs with enhanced UI options.

    This class provides a convenient way to create boolean parameters with common
    UI customizations like custom on/off labels. It exposes these UI options as
    direct properties for easy runtime modification.

    Example:
        param = ParameterBool(
            name="enabled",
            tooltip="Enable this feature",
            on_label="Yes",
            off_label="No",
            default_value=True
        )
        param.on_label = "Enable"  # Change labels at runtime
    """

    def __init__(  # noqa: PLR0913
        self,
        name: str,
        tooltip: str | None = None,
        *,
        type: str = "bool",  # noqa: A002, ARG002
        default_value: Any = None,
        tooltip_as_input: str | None = None,
        tooltip_as_property: str | None = None,
        tooltip_as_output: str | None = None,
        allowed_modes: set[ParameterMode] | None = None,
        traits: set[type[Trait] | Trait] | None = None,
        converters: list[Callable[[Any], Any]] | None = None,
        validators: list[Callable[[Parameter, Any], None]] | None = None,
        ui_options: dict | None = None,
        on_label: str | None = None,
        off_label: str | None = None,
        accept_any: bool = True,
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
        """Initialize a boolean parameter with enhanced UI options.

        Args:
            name: Parameter name
            tooltip: Parameter tooltip
            type: Parameter type (ignored, always "bool" for ParameterBool)
            default_value: Default parameter value
            tooltip_as_input: Tooltip for input mode
            tooltip_as_property: Tooltip for property mode
            tooltip_as_output: Tooltip for output mode
            allowed_modes: Allowed parameter modes
            traits: Parameter traits
            converters: Parameter converters
            validators: Parameter validators
            ui_options: Dictionary of UI options
            on_label: Label for the "on" state
            off_label: Label for the "off" state
            accept_any: Whether to accept any input type and convert to boolean (default: True)
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

        # Add boolean-specific UI options if they have values
        if on_label is not None:
            ui_options["on_label"] = on_label
        if off_label is not None:
            ui_options["off_label"] = off_label

        # Set up boolean conversion based on accept_any setting
        if converters is None:
            existing_converters = []
        else:
            existing_converters = converters

        if accept_any:
            final_input_types = ["any"]
            final_converters = [self._convert_to_bool, *existing_converters]
        else:
            final_input_types = ["bool"]
            final_converters = existing_converters

        # Call parent with explicit parameters, following ControlParameter pattern
        super().__init__(
            name=name,
            tooltip=tooltip,
            type="bool",  # Always a boolean type for ParameterBool
            input_types=final_input_types,
            output_type="bool",  # Always output as boolean
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

    def _convert_to_bool(self, value: Any) -> bool:
        """Safely convert any input value to a boolean.

        Handles various input types including strings, numbers, and other objects.
        Uses Python's built-in bool() conversion with proper handling of common
        string representations.

        Args:
            value: The value to convert to boolean

        Returns:
            Boolean representation of the value
        """
        if value is None:
            return False

        # Handle boolean inputs
        if isinstance(value, bool):
            return value

        # Handle string inputs with common boolean representations
        if isinstance(value, str):
            value_lower = value.lower().strip()
            if value_lower in ("true", "yes", "on", "1", "enable", "enabled"):
                return True
            if value_lower in ("false", "no", "off", "0", "disable", "disabled"):
                return False
            # For other strings, use truthiness
            return bool(value)

        # For all other types (including numeric), use Python's built-in bool conversion
        return bool(value)

    @property
    def on_label(self) -> str | None:
        """Get the label for the "on" state.

        Returns:
            The on label if set, None otherwise
        """
        return self.ui_options.get("on_label")

    @on_label.setter
    def on_label(self, value: str | None) -> None:
        """Set the label for the "on" state.

        Args:
            value: The on label to use, or None to remove it
        """
        if value is None:
            ui_options = self.ui_options.copy()
            ui_options.pop("on_label", None)
            self.ui_options = ui_options
        else:
            self.update_ui_options_key("on_label", value)

    @property
    def off_label(self) -> str | None:
        """Get the label for the "off" state.

        Returns:
            The off label if set, None otherwise
        """
        return self.ui_options.get("off_label")

    @off_label.setter
    def off_label(self, value: str | None) -> None:
        """Set the label for the "off" state.

        Args:
            value: The off label to use, or None to remove it
        """
        if value is None:
            ui_options = self.ui_options.copy()
            ui_options.pop("off_label", None)
            self.ui_options = ui_options
        else:
            self.update_ui_options_key("off_label", value)
