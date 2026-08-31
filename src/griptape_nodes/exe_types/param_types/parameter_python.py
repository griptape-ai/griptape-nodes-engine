"""ParameterPython component for Python code inputs with enhanced UI options."""

import ast
from collections.abc import Callable
from typing import Any

from griptape_nodes.exe_types.core_types import BadgeData, Parameter, ParameterMode, Trait


class ParameterPython(Parameter):
    """A specialized Parameter class for Python code inputs with enhanced UI options.

    Values are stored as Python source strings. Strings are validated with
    ast.parse and passed through. Other types are stringified.

    Example:
        param = ParameterPython(
            name="script",
            tooltip="Enter Python code",
            default_value="",
        )
    """

    def __init__(  # noqa: PLR0913
        self,
        name: str,
        tooltip: str | None = None,
        *,
        type: str = "python",  # noqa: A002, ARG002
        input_types: list[str] | None = None,  # noqa: ARG002
        output_type: str = "python",  # noqa: ARG002
        default_value: Any = None,
        tooltip_as_input: str | None = None,
        tooltip_as_property: str | None = None,
        tooltip_as_output: str | None = None,
        allowed_modes: set[ParameterMode] | None = None,
        traits: set[type[Trait] | Trait] | None = None,
        converters: list[Callable[[Any], Any]] | None = None,
        validators: list[Callable[[Parameter, Any], None]] | None = None,
        ui_options: dict | None = None,
        placeholder_text: str | None = "# Enter Python code\nresult = value * 2",
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
        if ui_options is None:
            ui_options = {}
        else:
            ui_options = ui_options.copy()

        if placeholder_text is not None:
            ui_options["placeholder_text"] = placeholder_text

        if converters is None:
            existing_converters = []
        else:
            existing_converters = converters

        if accept_any:
            final_input_types = ["any"]
            final_converters = [self._convert_to_python, *existing_converters]
        else:
            final_input_types = ["python", "str"]
            final_converters = existing_converters

        super().__init__(
            name=name,
            tooltip=tooltip,
            type="python",
            input_types=final_input_types,
            output_type="python",
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

    def _convert_to_python(self, value: Any) -> str:
        """Convert any input value to a validated Python source string.

        Strings are validated with ast.parse and returned as-is.
        Other types are stringified without validation.

        Raises:
            ValueError: If the string is not valid Python syntax.
        """
        if value is None:
            return ""

        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                try:
                    ast.parse(stripped)
                except (SyntaxError, ValueError, UnicodeEncodeError) as e:
                    msg = f"ParameterPython: Invalid Python syntax: {e}. Input: {value[:200]!r}"
                    raise ValueError(msg) from e
            return value

        return str(value)

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
