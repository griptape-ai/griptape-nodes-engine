"""ParameterYaml component for YAML inputs with enhanced UI options."""

from collections.abc import Callable
from io import StringIO
from typing import Any

from ruamel.yaml import YAML

from griptape_nodes.exe_types.core_types import BadgeData, Parameter, ParameterMode, Trait


class ParameterYaml(Parameter):
    """A specialized Parameter class for YAML inputs with enhanced UI options.

    Values are stored as YAML-formatted strings. Dicts and lists are serialized
    to YAML strings on input. Strings are validated as YAML and passed through.

    Example:
        param = ParameterYaml(
            name="config",
            tooltip="Enter YAML config",
            default_value="",
            button=True,
            button_label="Edit YAML",
        )
        param.button_label = "Reload"
    """

    def __init__(  # noqa: PLR0913
        self,
        name: str,
        tooltip: str | None = None,
        *,
        type: str = "yaml",  # noqa: A002, ARG002
        input_types: list[str] | None = None,  # noqa: ARG002
        output_type: str = "yaml",  # noqa: ARG002
        default_value: Any = None,
        tooltip_as_input: str | None = None,
        tooltip_as_property: str | None = None,
        tooltip_as_output: str | None = None,
        allowed_modes: set[ParameterMode] | None = None,
        traits: set[type[Trait] | Trait] | None = None,
        converters: list[Callable[[Any], Any]] | None = None,
        validators: list[Callable[[Parameter, Any], None]] | None = None,
        ui_options: dict | None = None,
        placeholder_text: str | None = "key: value\nnested:\n  key: value\nlist:\n  - item",
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
        if ui_options is None:
            ui_options = {}
        else:
            ui_options = ui_options.copy()

        if placeholder_text is not None:
            ui_options["placeholder_text"] = placeholder_text
        if button:
            ui_options["button"] = button
        if button_label is not None:
            ui_options["button_label"] = button_label
        if button_icon is not None:
            ui_options["button_icon"] = button_icon

        if converters is None:
            existing_converters = []
        else:
            existing_converters = converters

        if accept_any:
            final_input_types = ["any"]
            final_converters = [self._convert_to_yaml, *existing_converters]
        else:
            final_input_types = ["yaml", "str", "dict"]
            final_converters = existing_converters

        super().__init__(
            name=name,
            tooltip=tooltip,
            type="yaml",
            input_types=final_input_types,
            output_type="yaml",
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

    def _convert_to_yaml(self, value: Any) -> str:
        """Convert any input value to a YAML-formatted string.

        Strings are validated as YAML and returned as-is. Dicts and lists are
        serialized to YAML. Other types are stringified.

        Args:
            value: The value to convert

        Returns:
            YAML-formatted string

        Raises:
            ValueError: If the value cannot be represented as valid YAML
        """
        if value is None:
            return ""

        if isinstance(value, str):
            yaml = YAML()
            yaml.preserve_quotes = True
            try:
                yaml.load(value)
            except Exception as e:
                msg = f"ParameterYaml: Failed to validate YAML string: {e}. Input: {value[:200]!r}"
                raise ValueError(msg) from e
            return value

        if isinstance(value, (dict, list)):
            yaml = YAML()
            yaml.preserve_quotes = True
            stream = StringIO()
            yaml.dump(value, stream)
            return stream.getvalue()

        return str(value)

    @property
    def button(self) -> bool:
        return self.ui_options.get("button", False)

    @button.setter
    def button(self, value: bool) -> None:
        if value:
            self.update_ui_options_key("button", value)
        else:
            ui_options = self.ui_options.copy()
            ui_options.pop("button", None)
            self.ui_options = ui_options

    @property
    def button_label(self) -> str | None:
        return self.ui_options.get("button_label")

    @button_label.setter
    def button_label(self, value: str | None) -> None:
        if value is None:
            ui_options = self.ui_options.copy()
            ui_options.pop("button_label", None)
            self.ui_options = ui_options
        else:
            self.update_ui_options_key("button_label", value)

    @property
    def button_icon(self) -> str | None:
        return self.ui_options.get("button_icon")

    @button_icon.setter
    def button_icon(self, value: str | None) -> None:
        if value is None:
            ui_options = self.ui_options.copy()
            ui_options.pop("button_icon", None)
            self.ui_options = ui_options
        else:
            self.update_ui_options_key("button_icon", value)

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
