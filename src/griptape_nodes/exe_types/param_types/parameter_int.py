"""ParameterInt component for integer inputs with enhanced UI options."""

from collections.abc import Callable
from typing import Any

from griptape_nodes.exe_types.core_types import BadgeData, Parameter, ParameterMode, Trait
from griptape_nodes.exe_types.param_types.parameter_number import ParameterNumber


class ParameterInt(ParameterNumber):
    """A specialized Parameter class for integer inputs with enhanced UI options.

    This class provides a convenient way to create integer parameters with common
    UI customizations like step size for numeric input controls. It exposes these
    UI options as direct properties for easy runtime modification.

    Example:
        param = ParameterInt(
            name="count",
            tooltip="Enter item count",
            step=5,
            min=0,
            max=100,
            default_value=10
        )
        param.step = 1  # Change step size at runtime
        param.min = 10  # Change minimum value at runtime
    """

    def __init__(  # noqa: PLR0913
        self,
        name: str,
        tooltip: str | None = None,
        *,
        default_value: Any = None,
        tooltip_as_input: str | None = None,
        tooltip_as_property: str | None = None,
        tooltip_as_output: str | None = None,
        allowed_modes: set[ParameterMode] | None = None,
        traits: set[type[Trait] | Trait] | None = None,
        converters: list[Callable[[Any], Any]] | None = None,
        validators: list[Callable[[Parameter, Any], None]] | None = None,
        ui_options: dict | None = None,
        step: int | None = None,
        slider: bool = False,
        min_val: float | None = None,
        max_val: float | None = None,
        validate_min_max: bool = False,
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
        """Initialize an integer parameter with step validation.

        Args:
            name: Parameter name
            tooltip: Parameter tooltip
            default_value: Default parameter value
            tooltip_as_input: Tooltip for input mode
            tooltip_as_property: Tooltip for property mode
            tooltip_as_output: Tooltip for output mode
            allowed_modes: Allowed parameter modes
            traits: Parameter traits
            converters: Parameter converters
            validators: Parameter validators
            ui_options: Dictionary of UI options
            step: Step size for numeric input controls
            slider: Whether to use slider trait
            min_val: Minimum value for constraints (None to disable constraints)
            max_val: Maximum value for constraints (None to disable constraints)
            validate_min_max: Whether to validate min/max with error
            accept_any: Whether to accept any input type and convert to integer (default: True)
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
        # Call parent with integer-specific settings
        super().__init__(
            name=name,
            tooltip=tooltip,
            type="int",
            input_types=None,  # Will be set by parent based on accept_any
            output_type="int",
            default_value=default_value,
            tooltip_as_input=tooltip_as_input,
            tooltip_as_property=tooltip_as_property,
            tooltip_as_output=tooltip_as_output,
            allowed_modes=allowed_modes,
            traits=traits,
            converters=converters,
            validators=validators,
            ui_options=ui_options,
            step=step,
            slider=slider,
            min_val=min_val,
            max_val=max_val,
            validate_min_max=validate_min_max,
            accept_any=accept_any,
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

    def _convert_to_number(self, value: Any) -> int:  # noqa: C901, PLR0911
        """Safely convert any input value to an integer.

        Handles various input types including strings, floats, and other objects.
        Uses Python's built-in int() conversion with proper error handling.

        Args:
            value: The value to convert to integer

        Returns:
            Integer representation of the value

        Raises:
            ValueError: If the value cannot be converted to an integer
        """
        # Handle None and empty string cases first
        if value is None:
            return 0

        if isinstance(value, str):
            value = value.strip()
            if not value:
                return 0

        # Handle boolean inputs
        if isinstance(value, bool):
            if value:
                return 1
            return 0

        # Handle numeric inputs
        if isinstance(value, (int, float)):
            return int(value)

        # Handle string inputs
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                # Try converting float first, then to int
                try:
                    return int(float(value))
                except (ValueError, TypeError) as e:
                    msg = f"Cannot convert '{value}' to integer"
                    raise ValueError(msg) from e

        # For all other types, try direct conversion
        try:
            return int(value)
        except (ValueError, TypeError) as e:
            msg = f"Cannot convert {type(value).__name__} to integer"
            raise ValueError(msg) from e
