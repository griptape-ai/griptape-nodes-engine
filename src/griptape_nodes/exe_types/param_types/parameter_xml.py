"""ParameterXml component for XML inputs with enhanced UI options."""

import warnings
import xml.parsers.expat
from collections.abc import Callable
from typing import Any
from xml.etree.ElementTree import ParseError as XmlParseError
from xml.etree.ElementTree import fromstring as parse_xml

from griptape_nodes.exe_types.core_types import BadgeData, Parameter, ParameterMode, Trait

_MIN_EXPAT = (2, 6, 0)
if xml.parsers.expat.version_info < _MIN_EXPAT:
    _min = ".".join(str(v) for v in _MIN_EXPAT)
    warnings.warn(
        f"Your system's XML parser (libexpat {xml.parsers.expat.EXPAT_VERSION}) is out of date and has known security vulnerabilities. "
        f"XML and HTML nodes may be unsafe to use. "
        f"To fix this, please upgrade to a newer version of Python (libexpat {_min} or later is required) — libexpat is bundled with Python and cannot be upgraded separately.",
        stacklevel=1,
    )


class ParameterXml(Parameter):
    """A specialized Parameter class for XML inputs with enhanced UI options.

    Values are stored as XML-formatted strings. Strings are validated as XML
    and passed through. Other types are stringified.

    Example:
        param = ParameterXml(
            name="config",
            tooltip="Enter XML config",
            default_value="",
            button=True,
            button_label="Edit XML",
        )
    """

    _PARAM_TYPE: str = "xml"

    def __init__(  # noqa: PLR0913
        self,
        name: str,
        tooltip: str | None = None,
        *,
        type: str = "xml",  # noqa: A002, ARG002
        input_types: list[str] | None = None,  # noqa: ARG002
        output_type: str = "xml",  # noqa: ARG002
        default_value: Any = None,
        tooltip_as_input: str | None = None,
        tooltip_as_property: str | None = None,
        tooltip_as_output: str | None = None,
        allowed_modes: set[ParameterMode] | None = None,
        traits: set[type[Trait] | Trait] | None = None,
        converters: list[Callable[[Any], Any]] | None = None,
        validators: list[Callable[[Parameter, Any], None]] | None = None,
        ui_options: dict | None = None,
        placeholder_text: str | None = '<root>\n  <item key="value" />\n</root>',
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
            final_converters = [self._convert, *existing_converters]
        else:
            final_input_types = [self._PARAM_TYPE, "str"]
            final_converters = existing_converters

        super().__init__(
            name=name,
            tooltip=tooltip,
            type=self._PARAM_TYPE,
            input_types=final_input_types,
            output_type=self._PARAM_TYPE,
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

    def _convert(self, value: Any) -> str:
        """Convert any input value to a validated XML string.

        Strings are validated as XML and returned as-is.
        Other types are converted to their string representation.

        Raises:
            ValueError: If the string is not valid XML.
        """
        if value is None:
            return ""

        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                try:
                    parse_xml(stripped)  # noqa: S314 -- stdlib xml is safe in Python 3.12+; no external entities or DTD expansion by default (https://github.com/astral-sh/ruff/issues/23999)
                except (XmlParseError, UnicodeEncodeError) as e:
                    msg = f"ParameterXml: Failed to validate XML string: {e}. Input: {value[:200]!r}"
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
