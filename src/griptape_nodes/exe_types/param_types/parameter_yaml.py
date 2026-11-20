"""ParameterYaml component for YAML inputs with enhanced UI options."""

from collections.abc import Callable
from io import StringIO
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import MarkedYAMLError, YAMLError

from griptape_nodes.exe_types.core_types import BadgeData, InvalidParameterValueError, Parameter, ParameterMode, Trait

_MAPPING_VALUES_PROBLEM = "mapping values are not allowed here"


def _colon_in_value_hint(line: str) -> str:
    block_hint = "put the text on its own indented line after key: |"
    # Text that already holds a double quote can't be wrapped in one, and prose often holds apostrophes.
    if '"' in line:
        return f"To keep the colon, {block_hint}."
    return f'To keep the colon, wrap the value in quotes (key: "text: more text") or {block_hint}.'


def _describe_yaml_error(value: str, error: Exception) -> str:
    """Describe a YAML parse error in terms an artist can act on: where it is, and how to fix it."""
    if not isinstance(error, MarkedYAMLError) or error.problem_mark is None:
        return f"Invalid YAML: {error}"

    line_index = error.problem_mark.line
    lines = value.splitlines()
    line = lines[line_index] if line_index < len(lines) else ""
    location = f"Invalid YAML on line {line_index + 1}"
    heading = f"{location}: {line.strip()}" if line.strip() else location

    # Several words before the colon read as prose; a lone word is more likely a misplaced key.
    if error.problem == _MAPPING_VALUES_PROBLEM and " " in line[: error.problem_mark.column].strip():
        return f"{heading}\nA colon followed by a space starts a new key. {_colon_in_value_hint(line)}"

    problem = error.problem or "invalid syntax"
    return f"{heading}\n{problem[0].upper()}{problem[1:]}."


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
        exclude_from_metadata: bool = False,
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
            exclude_from_metadata=exclude_from_metadata,
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
            InvalidParameterValueError: If the value cannot be represented as valid YAML
        """
        if value is None:
            return ""

        if isinstance(value, str):
            yaml = YAML()
            yaml.preserve_quotes = True
            try:
                yaml.load(value)
            except (YAMLError, ValueError, RecursionError) as e:
                raise InvalidParameterValueError(_describe_yaml_error(value, e)) from e
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
            self.remove_ui_options_key("button")

    @property
    def button_label(self) -> str | None:
        return self.ui_options.get("button_label")

    @button_label.setter
    def button_label(self, value: str | None) -> None:
        if value is None:
            self.remove_ui_options_key("button_label")
        else:
            self.update_ui_options_key("button_label", value)

    @property
    def button_icon(self) -> str | None:
        return self.ui_options.get("button_icon")

    @button_icon.setter
    def button_icon(self, value: str | None) -> None:
        if value is None:
            self.remove_ui_options_key("button_icon")
        else:
            self.update_ui_options_key("button_icon", value)

    @property
    def placeholder_text(self) -> str | None:
        return self.ui_options.get("placeholder_text")

    @placeholder_text.setter
    def placeholder_text(self, value: str | None) -> None:
        if value is None:
            self.remove_ui_options_key("placeholder_text")
        else:
            self.update_ui_options_key("placeholder_text", value)
