"""ParameterString component for string inputs with enhanced UI options."""

import json
from collections.abc import Callable
from typing import Any

from griptape_nodes.exe_types.core_types import BadgeData, Parameter, ParameterMode, Trait


class ParameterString(Parameter):
    """A specialized Parameter class for string inputs with enhanced UI options.

    This class provides a convenient way to create string parameters with common
    UI customizations like markdown support, multiline input, and placeholder text.
    It exposes these UI options as direct properties for easy runtime modification.

    Example:
        param = ParameterString(
            name="description",
            tooltip="Enter a description",
            markdown=True,
            multiline=True,
            placeholder_text="Type your description here..."
        )
        param.multiline = False  # Change UI options at runtime
    """

    def __init__(  # noqa: PLR0913
        self,
        name: str,
        tooltip: str | None = None,
        *,
        type: str = "str",  # noqa: A002, ARG002
        input_types: list[str] | None = None,  # noqa: ARG002
        output_type: str = "str",  # noqa: ARG002
        default_value: Any = None,
        tooltip_as_input: str | None = None,
        tooltip_as_property: str | None = None,
        tooltip_as_output: str | None = None,
        allowed_modes: set[ParameterMode] | None = None,
        traits: set[type[Trait] | Trait] | None = None,
        converters: list[Callable[[Any], Any]] | None = None,
        validators: list[Callable[[Parameter, Any], None]] | None = None,
        ui_options: dict | None = None,
        markdown: bool = False,
        multiline: bool = False,
        placeholder_text: str | None = None,
        is_full_width: bool = False,
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
        exclude_from_metadata: bool = False,
        allow_variable_substitution: bool = True,
        element_id: str | None = None,
        element_type: str | None = None,
        parent_container_name: str | None = None,
        badge: BadgeData | None = None,
    ) -> None:
        """Initialize a string parameter with enhanced UI options.

        Args:
            name: Parameter name
            tooltip: Parameter tooltip
            type: Parameter type (ignored, always "str" for ParameterString)
            input_types: Allowed input types (ignored, set based on accept_any)
            output_type: Output type (ignored, always "str" for ParameterString)
            default_value: Default parameter value
            tooltip_as_input: Tooltip for input mode
            tooltip_as_property: Tooltip for property mode
            tooltip_as_output: Tooltip for output mode
            allowed_modes: Allowed parameter modes
            traits: Parameter traits
            converters: Parameter converters
            validators: Parameter validators
            ui_options: Dictionary of UI options
            markdown: Whether to enable markdown rendering
            multiline: Whether to use multiline input
            placeholder_text: Placeholder text for the input field
            is_full_width: Whether the parameter should take full width in the UI
            accept_any: Whether to accept any input type and convert to string (default: True)
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
            exclude_from_metadata: Whether this parameter is excluded from plaintext sidecar and image metadata
            allow_variable_substitution: Whether {VAR} tokens in this parameter's value are substituted at execution time
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

        # Add string-specific UI options if they have values
        if markdown:
            ui_options["markdown"] = markdown
        if multiline:
            ui_options["multiline"] = multiline
        if placeholder_text is not None:
            ui_options["placeholder_text"] = placeholder_text
        if is_full_width:
            ui_options["is_full_width"] = is_full_width

        # Set up string conversion based on accept_any setting
        if converters is None:
            existing_converters = []
        else:
            existing_converters = converters

        if accept_any:
            final_input_types = ["any"]
            final_converters = [self._accept_any, *existing_converters]
        else:
            final_input_types = ["str"]
            final_converters = existing_converters

        # When allowed_modes is explicitly provided, it takes precedence over the
        # allow_input/allow_property/allow_output convenience parameters. Forward defaults
        # for the convenience parameters so Parameter does not emit a conflict warning.
        if allowed_modes is not None:
            allow_input = True
            allow_property = True
            allow_output = True

        # Call parent with explicit parameters, following ControlParameter pattern
        super().__init__(
            name=name,
            tooltip=tooltip,
            type="str",  # Always a string type for ParameterString
            input_types=final_input_types,
            output_type="str",  # Always output as string
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
            exclude_from_metadata=exclude_from_metadata,
            allow_variable_substitution=allow_variable_substitution,
            element_id=element_id,
            element_type=element_type,
            parent_container_name=parent_container_name,
            badge=badge,
        )

    def _accept_any(self, value: Any) -> str:
        """Convert any input value to a string.

        Dicts and lists are serialized via ``json.dumps`` so JSON-shaped data
        round-trips cleanly. ``str()`` on a dict/list emits Python-repr
        quoting (single quotes, ``True``/``None``), which is not valid JSON.

        Args:
            value: The value to convert to string

        Returns:
            String representation of the value
        """
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, (dict, list)):
            try:
                return json.dumps(value)
            except (TypeError, ValueError):
                return str(value)
        return str(value)

    @property
    def markdown(self) -> bool:
        """Get whether markdown rendering is enabled.

        Returns:
            True if markdown is enabled, False otherwise
        """
        return self.ui_options.get("markdown", False)

    @markdown.setter
    def markdown(self, value: bool) -> None:
        """Set whether markdown rendering is enabled.

        Args:
            value: Whether to enable markdown rendering
        """
        if value:
            self.update_ui_options_key("markdown", value)
        else:
            ui_options = self.ui_options.copy()
            ui_options.pop("markdown", None)
            self.ui_options = ui_options

    @property
    def multiline(self) -> bool:
        """Get whether multiline input is enabled.

        Returns:
            True if multiline is enabled, False otherwise
        """
        return self.ui_options.get("multiline", False)

    @multiline.setter
    def multiline(self, value: bool) -> None:
        """Set whether multiline input is enabled.

        Args:
            value: Whether to enable multiline input
        """
        if value:
            self.update_ui_options_key("multiline", value)
        else:
            ui_options = self.ui_options.copy()
            ui_options.pop("multiline", None)
            self.ui_options = ui_options

    @property
    def placeholder_text(self) -> str | None:
        """Get the placeholder text for the input field.

        Returns:
            The placeholder text if set, None otherwise
        """
        return self.ui_options.get("placeholder_text")

    @placeholder_text.setter
    def placeholder_text(self, value: str | None) -> None:
        """Set the placeholder text for the input field.

        Args:
            value: The placeholder text to use, or None to remove it
        """
        if value is None:
            ui_options = self.ui_options.copy()
            ui_options.pop("placeholder_text", None)
            self.ui_options = ui_options
        else:
            self.update_ui_options_key("placeholder_text", value)

    @property
    def is_full_width(self) -> bool:
        """Get whether the parameter should take full width in the UI.

        Returns:
            True if full width is enabled, False otherwise
        """
        return self.ui_options.get("is_full_width", False)

    @is_full_width.setter
    def is_full_width(self, value: bool) -> None:
        """Set whether the parameter should take full width in the UI.

        Args:
            value: Whether to enable full width
        """
        if value:
            self.update_ui_options_key("is_full_width", value)
        else:
            ui_options = self.ui_options.copy()
            ui_options.pop("is_full_width", None)
            self.ui_options = ui_options
