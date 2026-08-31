"""ParameterNumber base class for numeric parameters with step validation support."""

import math
from collections.abc import Callable
from typing import Any

from griptape_nodes.exe_types.core_types import BadgeData, Parameter, ParameterMode, Trait
from griptape_nodes.traits.clamp import Clamp
from griptape_nodes.traits.minmax import MinMax
from griptape_nodes.traits.slider import Slider


class ParameterNumber(Parameter):
    """Base class for numeric parameters with step validation support.

    This class provides common functionality for numeric parameters including
    step validation, UI options, and type conversion. Subclasses should set
    the appropriate type and conversion methods.
    """

    def __init__(  # noqa: PLR0913
        self,
        name: str,
        tooltip: str | None = None,
        *,
        type: str,  # noqa: A002
        input_types: list[str] | None = None,  # noqa: ARG002
        output_type: str,
        default_value: Any = None,
        tooltip_as_input: str | None = None,
        tooltip_as_property: str | None = None,
        tooltip_as_output: str | None = None,
        allowed_modes: set[ParameterMode] | None = None,
        traits: set[type[Trait] | Trait] | None = None,
        converters: list[Callable[[Any], Any]] | None = None,
        validators: list[Callable[[Parameter, Any], None]] | None = None,
        ui_options: dict | None = None,
        step: float | None = None,
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
        """Initialize a numeric parameter with step validation.

        Args:
            name: Parameter name
            tooltip: Parameter tooltip
            type: Parameter type (should be "int" or "float")
            input_types: Allowed input types
            output_type: Output type (should be "int" or "float")
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
            accept_any: Whether to accept any input type and convert to number (default: True)
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

        # Add numeric-specific UI options if they have values
        if step is not None:
            ui_options["step"] = step

        # Set up numeric conversion based on accept_any setting
        if converters is None:
            existing_converters = []
        else:
            existing_converters = converters

        if accept_any:
            final_input_types = ["any"]
            final_converters = [self._convert_to_number, *existing_converters]
        else:
            final_input_types = [type]
            final_converters = existing_converters

        # Set up validators
        if validators is None:
            existing_validators = []
        else:
            existing_validators = validators

        # Add step validator if step is specified
        final_validators = existing_validators.copy()
        if step is not None:
            final_validators.append(self._create_step_validator(step))

        # Set up constraint traits based on parameters
        self._setup_constraint_traits(
            name=name, traits=traits, slider=slider, min_val=min_val, max_val=max_val, validate_min_max=validate_min_max
        )

        # Call parent with explicit parameters, following ControlParameter pattern
        super().__init__(
            name=name,
            tooltip=tooltip,
            type=type,
            input_types=final_input_types,
            output_type=output_type,
            default_value=default_value,
            tooltip_as_input=tooltip_as_input,
            tooltip_as_property=tooltip_as_property,
            tooltip_as_output=tooltip_as_output,
            allowed_modes=allowed_modes,
            traits=self._constraint_traits,
            converters=final_converters,
            validators=final_validators,
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

    def _create_step_validator(self, step_value: float) -> Callable[[Parameter, Any], None]:  # noqa: ARG002
        """Create a validator function that enforces step constraints for numbers.

        Args:
            step_value: The step size to enforce

        Returns:
            A validator function that raises ValueError if the value is not a multiple of step
        """

        def validate_step(param: Parameter, value: Any) -> None:
            if value is None:
                return

            if not isinstance(value, (int, float)):
                return  # Let other validators handle type issues

            # Get the current step value from the parameter's UI options
            current_step = param.ui_options.get("step")
            if current_step is None:
                return  # No step constraint

            # For numbers, we need to check if the value is approximately a multiple of step
            # due to floating point precision issues
            remainder = value % current_step
            # Use math.isclose() for proper floating-point comparison
            if not (
                math.isclose(remainder, 0.0, abs_tol=1e-10) or math.isclose(remainder, current_step, abs_tol=1e-10)
            ):
                msg = f"Value {value} is not a multiple of step {current_step}"
                raise ValueError(msg)

        return validate_step

    def _convert_to_number(self, value: Any) -> int | float:
        """Convert any input value to a number.

        This is an abstract method that subclasses must implement to provide
        the appropriate type conversion (int or float).

        Args:
            value: The value to convert

        Returns:
            The converted number

        Raises:
            NotImplementedError: If not implemented by subclass
        """
        msg = f"{self.name}: Subclasses must implement _convert_to_number"
        raise NotImplementedError(msg)

    def _setup_constraint_traits(  # noqa: PLR0913
        self,
        name: str,
        traits: set[type[Trait] | Trait] | None,
        *,
        slider: bool,
        min_val: float | None,
        max_val: float | None,
        validate_min_max: bool,
    ) -> None:
        """Set up constraint traits based on parameters.

        Args:
            name: Parameter name for error messages
            traits: Existing traits set
            slider: Whether to use slider trait
            min_val: Minimum value
            max_val: Maximum value
            validate_min_max: Whether to validate min/max with error
        """
        # Validation rules
        if slider and (min_val is None or max_val is None):
            msg = f"{name}: If slider is True, both min_val and max_val must be provided"
            raise ValueError(msg)
        if validate_min_max and (min_val is None or max_val is None):
            msg = f"{name}: If validate_min_max is True, both min_val and max_val must be provided"
            raise ValueError(msg)

        # Set up traits based on parameters
        if traits is None:
            traits = set()
        else:
            traits = set(traits)

        # Add constraint trait based on priority: Slider > MinMax > Clamp
        if slider and min_val is not None and max_val is not None:
            traits.add(Slider(min_val=min_val, max_val=max_val))
        elif validate_min_max and min_val is not None and max_val is not None:
            traits.add(MinMax(min_val=min_val, max_val=max_val))
        elif min_val is not None or max_val is not None:
            traits.add(Clamp(min_val=min_val, max_val=max_val))

        # Store traits for later use
        self._constraint_traits = traits

        # Store min/max values for runtime property access
        self._min_val = min_val
        self._max_val = max_val

    @property
    def slider(self) -> bool:
        """Whether slider trait is active."""
        return any(isinstance(trait, Slider) for trait in self.find_elements_by_type(Trait))

    @slider.setter
    def slider(self, value: bool) -> None:
        """Set slider trait."""
        if value:
            # Get min/max from stored values
            min_val = getattr(self, "_min_val", None)
            max_val = getattr(self, "_max_val", None)
            if min_val is None or max_val is None:
                msg = f"{self.name}: Cannot enable slider without min_val and max_val"
                raise ValueError(msg)
            # Find existing constraint traits and replace with slider
            self._remove_constraint_traits()
            self.add_trait(Slider(min_val=min_val, max_val=max_val))
        else:
            # Remove slider trait
            slider_traits = self.find_elements_by_type(Slider)
            for trait in slider_traits:
                self.remove_trait(trait)

    @property
    def min_val(self) -> float | None:
        """Get minimum value from constraint traits."""
        for trait in self.find_elements_by_type(Trait):
            if hasattr(trait, "min"):
                return getattr(trait, "min", None)
        return None

    @min_val.setter
    def min_val(self, value: float | None) -> None:
        """Set minimum value and update constraint traits."""
        self._min_val = value
        self._update_constraint_traits()

    @property
    def max_val(self) -> float | None:
        """Get maximum value from constraint traits."""
        for trait in self.find_elements_by_type(Trait):
            if hasattr(trait, "max"):
                return getattr(trait, "max", None)
        return None

    @max_val.setter
    def max_val(self, value: float | None) -> None:
        """Set maximum value and update constraint traits."""
        self._max_val = value
        self._update_constraint_traits()

    @property
    def validate_min_max(self) -> bool:
        """Whether MinMax trait is active."""
        return any(isinstance(trait, MinMax) for trait in self.find_elements_by_type(Trait))

    @validate_min_max.setter
    def validate_min_max(self, value: bool) -> None:
        """Set MinMax validation."""
        if value:
            # Check if we have stored min/max values
            if self.min_val is None or self.max_val is None:
                msg = f"{self.name}: Cannot enable validate_min_max without min_val and max_val"
                raise ValueError(msg)
            # Replace existing constraint traits with MinMax
            self._remove_constraint_traits()
            self.add_trait(MinMax(min_val=self.min_val, max_val=self.max_val))
        else:
            # Remove MinMax trait and replace with Clamp if we have min/max
            min_max_traits = self.find_elements_by_type(MinMax)
            for trait in min_max_traits:
                self.remove_trait(trait)
            min_val = getattr(self, "_min_val", None)
            max_val = getattr(self, "_max_val", None)
            if min_val is not None and max_val is not None:
                self.add_trait(Clamp(min_val=min_val, max_val=max_val))

    def _remove_constraint_traits(self) -> None:
        """Remove all constraint traits."""
        for trait_type in [Slider, MinMax, Clamp]:
            traits = self.find_elements_by_type(trait_type)
            for trait in traits:
                self.remove_trait(trait)

    def _update_constraint_traits(self) -> None:
        """Update constraint traits based on current min/max values."""
        min_val = getattr(self, "_min_val", None)
        max_val = getattr(self, "_max_val", None)

        if min_val is None and max_val is None:
            self._remove_constraint_traits()
            return

        if self.slider and (min_val is None or max_val is None):
            msg = f"{self.name}: Cannot enable slider without min_val and max_val"
            raise ValueError(msg)

        if self.validate_min_max and (min_val is None or max_val is None):
            msg = f"{self.name}: Cannot enable validate_min_max without min_val and max_val"
            raise ValueError(msg)

        # Determine which trait to use based on current state
        if self.slider:
            # Checked above: both min_val and max_val are available when slider is enabled.
            # Type narrowing: assign to variables after None check so type checker understands
            min_val_float: float = min_val  # type: ignore[assignment]
            max_val_float: float = max_val  # type: ignore[assignment]
            # Python will naturally coerce int to float if needed (no precision loss for reasonable values)
            self._remove_constraint_traits()
            self.add_trait(Slider(min_val=min_val_float, max_val=max_val_float))
        elif self.validate_min_max:
            # Checked above: both min_val and max_val are available when validate_min_max is enabled.
            # Type narrowing: assign to variables after None check so type checker understands
            min_val_float: float = min_val  # type: ignore[assignment]
            max_val_float: float = max_val  # type: ignore[assignment]
            # Python will naturally coerce int to float if needed (no precision loss for reasonable values)
            self._remove_constraint_traits()
            self.add_trait(MinMax(min_val=min_val_float, max_val=max_val_float))
        else:
            self._remove_constraint_traits()
            self.add_trait(Clamp(min_val=min_val, max_val=max_val))
