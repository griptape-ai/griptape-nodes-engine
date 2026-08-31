"""ParameterRange component for list inputs with optional range slider UI options."""

from collections.abc import Callable
from typing import Any

from griptape_nodes.exe_types.core_types import BadgeData, Parameter, ParameterMode, Trait


class ParameterRange(Parameter):
    """A specialized Parameter class for list inputs with optional range slider UI.

    This class provides a convenient way to create list parameters. When the list
    contains exactly 2 numeric values (int or float), you can enable the range_slider
    UI option to display a range slider with customizations like min/max values,
    step size, and label options. It exposes these UI options as direct properties
    for easy runtime modification.

    Example - General list:
        param = ParameterRange(
            name="items",
            tooltip="List of items",
            default_value=["a", "b", "c"]
        )

    Example - Range slider (for 2 numeric values):
        param = ParameterRange(
            name="range",
            tooltip="Select a range",
            range_slider=True,
            min_val=0,
            max_val=100,
            step=1,
            min_label="Minimum",
            max_label="Maximum"
        )
        param.max_val = 200  # Change UI options at runtime
    """

    def __init__(  # noqa: PLR0913
        self,
        name: str,
        tooltip: str | None = None,
        *,
        type: str = "list",  # noqa: A002, ARG002
        input_types: list[str] | None = None,  # noqa: ARG002
        output_type: str = "list",  # noqa: ARG002
        default_value: Any = None,
        tooltip_as_input: str | None = None,
        tooltip_as_property: str | None = None,
        tooltip_as_output: str | None = None,
        allowed_modes: set[ParameterMode] | None = None,
        traits: set[type[Trait] | Trait] | None = None,
        converters: list[Callable[[Any], Any]] | None = None,
        validators: list[Callable[[Parameter, Any], None]] | None = None,
        ui_options: dict | None = None,
        range_slider: bool = True,
        min_val: float = 0,
        max_val: float = 100,
        step: float = 1,
        min_label: str = "min",
        max_label: str = "max",
        hide_range_labels: bool = False,
        hide_range_parameters: bool = False,
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
        """Initialize a list parameter with optional range slider UI options.

        This is a general-purpose list parameter. The range_slider option is only
        applicable when the list contains exactly 2 numeric values (int or float).
        When range_slider is enabled, the UI will display a range slider instead
        of a standard list input.

        Args:
            name: Parameter name
            tooltip: Parameter tooltip
            type: Parameter type (ignored, always "list" for ParameterRange)
            input_types: Allowed input types (ignored, set based on accept_any)
            output_type: Output type (ignored, always "list" for ParameterRange)
            default_value: Default parameter value
            tooltip_as_input: Tooltip for input mode
            tooltip_as_property: Tooltip for property mode
            tooltip_as_output: Tooltip for output mode
            allowed_modes: Allowed parameter modes
            traits: Parameter traits
            converters: Parameter converters
            validators: Parameter validators
            ui_options: Dictionary of UI options
            range_slider: Whether to enable range slider UI (only for 2 numeric values)
            min_val: Minimum value for range slider (default: 0)
            max_val: Maximum value for range slider (default: 100)
            step: Step size for range slider (default: 1)
            min_label: Label for minimum value (default: "min")
            max_label: Label for maximum value (default: "max")
            hide_range_labels: Whether to hide range labels (default: False)
            hide_range_parameters: Whether to hide the range parameters (default: False)
            accept_any: Whether to accept any input type and convert to list (default: True)
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

        # Add list-specific UI options with nested range_slider structure
        if range_slider:
            ui_options["range_slider"] = {
                "min_value": min_val,
                "max_value": max_val,
                "step": step,
                "min_label": min_label,
                "max_label": max_label,
                "hide_range_labels": hide_range_labels,
                "hide_range_parameters": hide_range_parameters,
            }

        # Set up list conversion based on accept_any setting
        if converters is None:
            existing_converters = []
        else:
            existing_converters = converters

        if accept_any:
            final_input_types = ["any"]
            final_converters = [self._accept_any, *existing_converters]
        else:
            final_input_types = ["list"]
            final_converters = existing_converters

        # Call parent with explicit parameters, following ParameterString pattern
        super().__init__(
            name=name,
            tooltip=tooltip,
            type="list",  # Always a list type for ParameterRange
            input_types=final_input_types,
            output_type="list",  # Always output as list
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

    def _accept_any(self, value: Any) -> list:
        """Convert any input value to a list.

        Args:
            value: The value to convert to list

        Returns:
            List representation of the value
        """
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return [value]

    @property
    def range_slider(self) -> bool:
        """Get whether range slider UI is enabled.

        Returns:
            True if range slider is enabled, False otherwise
        """
        return "range_slider" in self.ui_options and isinstance(self.ui_options["range_slider"], dict)

    @range_slider.setter
    def range_slider(self, value: bool) -> None:
        """Set whether range slider UI is enabled.

        Args:
            value: Whether to enable range slider
        """
        if value:
            # Initialize range_slider object if it doesn't exist
            if "range_slider" not in self.ui_options or not isinstance(self.ui_options["range_slider"], dict):
                self.ui_options["range_slider"] = {
                    "min_value": 0,
                    "max_value": 100,
                    "step": 1,
                    "min_label": "min",
                    "max_label": "max",
                    "hide_range_labels": False,
                    "hide_range_parameters": False,
                }
        else:
            ui_options = self.ui_options.copy()
            ui_options.pop("range_slider", None)
            self.ui_options = ui_options

    @property
    def min_val(self) -> float:
        """Get the minimum value for the range slider.

        Returns:
            The minimum value
        """
        range_slider = self.ui_options.get("range_slider")
        if isinstance(range_slider, dict):
            return range_slider.get("min_value", 0)
        return 0

    @min_val.setter
    def min_val(self, value: float) -> None:
        """Set the minimum value for the range slider.

        Args:
            value: The minimum value to use
        """
        if "range_slider" not in self.ui_options or not isinstance(self.ui_options["range_slider"], dict):
            self.range_slider = True  # Initialize range_slider object
        self.ui_options["range_slider"]["min_value"] = value

    @property
    def max_val(self) -> float:
        """Get the maximum value for the range slider.

        Returns:
            The maximum value
        """
        range_slider = self.ui_options.get("range_slider")
        if isinstance(range_slider, dict):
            return range_slider.get("max_value", 100)
        return 100

    @max_val.setter
    def max_val(self, value: float) -> None:
        """Set the maximum value for the range slider.

        Args:
            value: The maximum value to use
        """
        if "range_slider" not in self.ui_options or not isinstance(self.ui_options["range_slider"], dict):
            self.range_slider = True  # Initialize range_slider object
        self.ui_options["range_slider"]["max_value"] = value

    @property
    def step(self) -> float:
        """Get the step size for the range slider.

        Returns:
            The step size
        """
        range_slider = self.ui_options.get("range_slider")
        if isinstance(range_slider, dict):
            return range_slider.get("step", 1)
        return 1

    @step.setter
    def step(self, value: float) -> None:
        """Set the step size for the range slider.

        Args:
            value: The step size to use
        """
        if "range_slider" not in self.ui_options or not isinstance(self.ui_options["range_slider"], dict):
            self.range_slider = True  # Initialize range_slider object
        self.ui_options["range_slider"]["step"] = value

    @property
    def min_label(self) -> str:
        """Get the label for the minimum value.

        Returns:
            The minimum label
        """
        range_slider = self.ui_options.get("range_slider")
        if isinstance(range_slider, dict):
            return range_slider.get("min_label", "min")
        return "min"

    @min_label.setter
    def min_label(self, value: str) -> None:
        """Set the label for the minimum value.

        Args:
            value: The minimum label to use
        """
        if "range_slider" not in self.ui_options or not isinstance(self.ui_options["range_slider"], dict):
            self.range_slider = True  # Initialize range_slider object
        self.ui_options["range_slider"]["min_label"] = value

    @property
    def max_label(self) -> str:
        """Get the label for the maximum value.

        Returns:
            The maximum label
        """
        range_slider = self.ui_options.get("range_slider")
        if isinstance(range_slider, dict):
            return range_slider.get("max_label", "max")
        return "max"

    @max_label.setter
    def max_label(self, value: str) -> None:
        """Set the label for the maximum value.

        Args:
            value: The maximum label to use
        """
        if "range_slider" not in self.ui_options or not isinstance(self.ui_options["range_slider"], dict):
            self.range_slider = True  # Initialize range_slider object
        self.ui_options["range_slider"]["max_label"] = value

    @property
    def hide_range_labels(self) -> bool:
        """Get whether range labels are hidden.

        Returns:
            True if range labels are hidden, False otherwise
        """
        range_slider = self.ui_options.get("range_slider")
        if isinstance(range_slider, dict):
            return range_slider.get("hide_range_labels", True)
        return True

    @hide_range_labels.setter
    def hide_range_labels(self, value: bool) -> None:
        """Set whether to hide range labels.

        Args:
            value: Whether to hide range labels
        """
        if "range_slider" not in self.ui_options or not isinstance(self.ui_options["range_slider"], dict):
            self.range_slider = True  # Initialize range_slider object
        self.ui_options["range_slider"]["hide_range_labels"] = value

    @property
    def hide_range_parameters(self) -> bool:
        """Get whether the range parameters are hidden.

        Returns:
            True if range parameters are hidden, False otherwise
        """
        range_slider = self.ui_options.get("range_slider")
        if isinstance(range_slider, dict):
            return range_slider.get("hide_range_parameters", False)
        return False

    @hide_range_parameters.setter
    def hide_range_parameters(self, value: bool) -> None:
        """Set whether to hide the range parameters.

        Args:
            value: Whether to hide the range parameters
        """
        if "range_slider" not in self.ui_options or not isinstance(self.ui_options["range_slider"], dict):
            self.range_slider = True  # Initialize range_slider object
        self.ui_options["range_slider"]["hide_range_parameters"] = value
