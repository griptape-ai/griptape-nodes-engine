"""ParameterHtml component for HTML inputs with enhanced UI options."""

from collections.abc import Callable
from typing import Any

from griptape_nodes.exe_types.core_types import BadgeData, Parameter, ParameterMode, Trait
from griptape_nodes.exe_types.param_types.parameter_xml import ParameterXml

_HTML_PLACEHOLDER = "<div>\n  <p>Hello, <strong>world</strong>!</p>\n</div>"


class ParameterHtml(ParameterXml):
    """A specialized Parameter class for HTML inputs with enhanced UI options.

    Subclass of ParameterXml. HTML is stored as a string but not validated as
    strict XML, since HTML5 allows many constructs that are not well-formed XML.

    Example:
        param = ParameterHtml(
            name="content",
            tooltip="Enter HTML content",
            default_value="",
        )
    """

    _PARAM_TYPE: str = "html"

    def __init__(  # noqa: PLR0913
        self,
        name: str,
        tooltip: str | None = None,
        *,
        type: str = "html",  # noqa: A002, ARG002
        input_types: list[str] | None = None,  # noqa: ARG002
        output_type: str = "html",  # noqa: ARG002
        default_value: Any = None,
        tooltip_as_input: str | None = None,
        tooltip_as_property: str | None = None,
        tooltip_as_output: str | None = None,
        allowed_modes: set[ParameterMode] | None = None,
        traits: set[type[Trait] | Trait] | None = None,
        converters: list[Callable[[Any], Any]] | None = None,
        validators: list[Callable[[Parameter, Any], None]] | None = None,
        ui_options: dict | None = None,
        placeholder_text: str | None = _HTML_PLACEHOLDER,
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
        super().__init__(
            name=name,
            tooltip=tooltip,
            default_value=default_value,
            tooltip_as_input=tooltip_as_input,
            tooltip_as_property=tooltip_as_property,
            tooltip_as_output=tooltip_as_output,
            allowed_modes=allowed_modes,
            traits=traits,
            converters=converters,
            validators=validators,
            ui_options=ui_options,
            placeholder_text=placeholder_text,
            accept_any=accept_any,
            button=button,
            button_label=button_label,
            button_icon=button_icon,
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

    def _convert(self, value: Any) -> str:
        """Convert any input value to an HTML string.

        HTML is not validated as strict XML — strings are passed through as-is.
        Other types are stringified.
        """
        if value is None:
            return ""
        return value if isinstance(value, str) else str(value)
