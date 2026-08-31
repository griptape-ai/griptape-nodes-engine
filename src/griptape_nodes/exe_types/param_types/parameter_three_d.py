"""Parameter3D component for 3D model inputs with enhanced UI options."""

from collections.abc import Callable
from typing import Any

from griptape_nodes_library.three_d.three_d_artifact import ThreeDUrlArtifact  # pyright: ignore[reportMissingImports]

from griptape_nodes.exe_types.core_types import BadgeData, Parameter, ParameterMode, Trait
from griptape_nodes.utils.artifact_normalization import normalize_artifact_input


class Parameter3D(Parameter):
    """A specialized Parameter class for 3D model inputs with enhanced UI options.

    This class provides a convenient way to create 3D model parameters with common
    UI customizations like file browser and expander functionality.
    It exposes these UI options as direct properties for easy runtime modification.

    Example:
        param = Parameter3D(
            name="input_3d",
            tooltip="Select a 3D model",
            clickable_file_browser=True,
            expander=True
        )
        param.pulse_on_run = True  # Change UI options at runtime
    """

    def __init__(  # noqa: PLR0913
        self,
        name: str,
        tooltip: str | None = None,
        *,
        type: str = "ThreeDUrlArtifact",  # noqa: A002, ARG002
        input_types: list[str] | None = None,  # noqa: ARG002
        output_type: str = "ThreeDUrlArtifact",  # noqa: ARG002
        default_value: Any = None,
        tooltip_as_input: str | None = None,
        tooltip_as_property: str | None = None,
        tooltip_as_output: str | None = None,
        allowed_modes: set[ParameterMode] | None = None,
        traits: set[type[Trait] | Trait] | None = None,
        converters: list[Callable[[Any], Any]] | None = None,
        validators: list[Callable[[Parameter, Any], None]] | None = None,
        ui_options: dict | None = None,
        pulse_on_run: bool = False,
        clickable_file_browser: bool = True,
        expander: bool = False,
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
        """Initialize a 3D model parameter with enhanced UI options.

        Args:
            name: Parameter name
            tooltip: Parameter tooltip
            type: Parameter type (ignored, always "ThreeDUrlArtifact" for Parameter3D)
            input_types: Allowed input types (ignored, set based on accept_any)
            output_type: Output type (ignored, always "ThreeDUrlArtifact" for Parameter3D)
            default_value: Default parameter value
            tooltip_as_input: Tooltip for input mode
            tooltip_as_property: Tooltip for property mode
            tooltip_as_output: Tooltip for output mode
            allowed_modes: Allowed parameter modes
            traits: Parameter traits
            converters: Parameter converters
            validators: Parameter validators
            ui_options: Dictionary of UI options
            pulse_on_run: Whether to pulse the parameter on run
            clickable_file_browser: Whether to show clickable file browser
            expander: Whether to enable expander functionality
            accept_any: Whether to accept any input type and convert to 3D (default: True)
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

        # Add 3D-specific UI options if they have values
        if pulse_on_run:
            ui_options["pulse_on_run"] = pulse_on_run
        if clickable_file_browser:
            ui_options["clickable_file_browser"] = clickable_file_browser
        if expander:
            ui_options["expander"] = expander

        # Auto-disable clickable_file_browser if neither input nor property modes are allowed
        if not allow_input and not allow_property and clickable_file_browser:
            ui_options.pop("clickable_file_browser", None)

        # Set up input types based on accept_any setting
        if accept_any:
            final_input_types = ["any"]
        else:
            final_input_types = ["ThreeDUrlArtifact"]

        # Add automatic converter to normalize string inputs to ThreeDUrlArtifact
        # This allows Parameter3D to automatically handle file paths and localhost URLs
        three_d_converters = list(converters) if converters else []
        if accept_any:
            # Create a converter function that uses normalize_artifact_input with ThreeDUrlArtifact
            def _normalize_three_d(value: Any) -> Any:
                return normalize_artifact_input(value, ThreeDUrlArtifact)

            three_d_converters.insert(0, _normalize_three_d)

        # Call parent with explicit parameters, following ControlParameter pattern
        super().__init__(
            name=name,
            tooltip=tooltip,
            type="ThreeDUrlArtifact",  # Always a ThreeDUrlArtifact type for Parameter3D
            input_types=final_input_types,
            output_type="ThreeDUrlArtifact",  # Always output as ThreeDUrlArtifact
            default_value=default_value,
            tooltip_as_input=tooltip_as_input,
            tooltip_as_property=tooltip_as_property,
            tooltip_as_output=tooltip_as_output,
            allowed_modes=allowed_modes,
            traits=traits,
            converters=three_d_converters,
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

    @property
    def pulse_on_run(self) -> bool:
        """Get whether pulse on run is enabled.

        Returns:
            True if pulse on run is enabled, False otherwise
        """
        return self.ui_options.get("pulse_on_run", False)

    @pulse_on_run.setter
    def pulse_on_run(self, value: bool) -> None:
        """Set whether pulse on run is enabled.

        Args:
            value: Whether to enable pulse on run
        """
        if value:
            self.update_ui_options_key("pulse_on_run", value)
        else:
            ui_options = self.ui_options.copy()
            ui_options.pop("pulse_on_run", None)
            self.ui_options = ui_options

    @property
    def clickable_file_browser(self) -> bool:
        """Get whether clickable file browser is enabled.

        Returns:
            True if clickable file browser is enabled, False otherwise
        """
        return self.ui_options.get("clickable_file_browser", False)

    @clickable_file_browser.setter
    def clickable_file_browser(self, value: bool) -> None:
        """Set whether clickable file browser is enabled.

        Args:
            value: Whether to enable clickable file browser
        """
        if value:
            self.update_ui_options_key("clickable_file_browser", value)
        else:
            ui_options = self.ui_options.copy()
            ui_options.pop("clickable_file_browser", None)
            self.ui_options = ui_options

    @property
    def expander(self) -> bool:
        """Get whether expander is enabled.

        Returns:
            True if expander is enabled, False otherwise
        """
        return self.ui_options.get("expander", False)

    @expander.setter
    def expander(self, value: bool) -> None:
        """Set whether expander is enabled.

        Args:
            value: Whether to enable expander
        """
        if value:
            self.update_ui_options_key("expander", value)
        else:
            ui_options = self.ui_options.copy()
            ui_options.pop("expander", None)
            self.ui_options = ui_options
