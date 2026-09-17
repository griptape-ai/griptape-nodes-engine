"""Standalone text elements: notices, warnings, and deprecation prompts."""

from __future__ import annotations

from dataclasses import field
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from griptape_nodes.exe_types.elements.base import BaseNodeElement
from griptape_nodes.exe_types.elements.ui_options import UIOptionsMixin

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.exe_types.elements.trait import Trait
    from griptape_nodes.exe_types.node_types import BaseNode


class ParameterMessage(BaseNodeElement, UIOptionsMixin):
    """Represents a UI message element, such as a warning or informational text."""

    # Define default titles as a class-level constant
    DEFAULT_TITLES: ClassVar[dict[str, str]] = {
        "info": "Info",
        "warning": "Warning",
        "error": "Error",
        "success": "Success",
        "tip": "Tip",
        "link": "Link",
        "docs": "Documentation",
        "help": "Help",
        "note": "Note",
        "cloud-upload": "Upload",
        "none": "",
    }

    # Define default icons as a class-level constant (based on Lucide icons)
    DEFAULT_ICONS: ClassVar[dict[str, str]] = {
        "info": "info",
        "warning": "alert-triangle",
        "error": "x-circle",
        "success": "check-circle",
        "tip": "lightbulb",
        "link": "external-link",
        "docs": "book-open",
        "help": "help-circle",
        "note": "sticky-note",
        "cloud-upload": "cloud-upload",
        "none": "",
    }

    # Create a type alias using the keys from DEFAULT_TITLES
    type VariantType = Literal[
        "info", "warning", "error", "success", "tip", "link", "docs", "help", "note", "cloud-upload", "none"
    ]
    type ButtonAlignType = Literal["full-width", "left", "center", "right"]
    type ButtonVariantType = Literal["default", "destructive", "outline", "secondary", "ghost", "link"]

    element_type: str = field(default_factory=lambda: ParameterMessage.__name__)
    _variant: VariantType = field(init=False)
    _title: str | None = field(default=None, init=False)
    _value: str = field(init=False)
    _message_icon: str | None = field(default="__DEFAULT__", init=False)
    _button_link: str | None = field(default=None, init=False)
    _button_text: str | None = field(default=None, init=False)
    _button_icon: str | None = field(default=None, init=False)
    _button_variant: ButtonVariantType = field(default="outline", init=False)
    _button_align: ButtonAlignType = field(default="full-width", init=False)
    _full_width: bool = field(default=False, init=False)
    _ui_options: dict = field(default_factory=dict, init=False)

    def __init__(  # noqa: PLR0913
        self,
        variant: VariantType,
        value: str,
        *,
        title: str | None = None,
        message_icon: str | None = "__DEFAULT__",
        button_link: str | None = None,
        button_text: str | None = None,
        button_icon: str | None = None,
        button_variant: ButtonVariantType = "outline",
        button_align: ButtonAlignType = "full-width",
        full_width: bool = False,
        markdown: bool | None = None,
        hide: bool | None = None,
        ui_options: dict | None = None,
        traits: set[Trait.__class__ | Trait] | None = None,
        **kwargs,
    ):
        # Remove markdown and hide from kwargs to prevent passing them to parent class
        kwargs.pop("markdown", None)
        kwargs.pop("hide", None)
        super().__init__(element_type=ParameterMessage.__name__, **kwargs)
        self._variant = variant
        self._title = title
        self._value = value
        self._message_icon = message_icon
        self._button_link = button_link
        self._button_text = button_text
        self._button_icon = button_icon
        self._button_variant = button_variant
        self._button_align = button_align
        self._full_width = full_width
        self._ui_options = ui_options or {}

        # Validate that explicit parameters don't conflict with ui_options (only if not None)
        if markdown is not None:
            self._validate_ui_option_conflict(
                ui_options_dict=self._ui_options, param_name="markdown", param_value=markdown
            )
        if hide is not None:
            self._validate_ui_option_conflict(ui_options_dict=self._ui_options, param_name="hide", param_value=hide)

        # Add common UI options if explicitly provided (not None) and NOT already in ui_options
        # (ui_options always wins in case of conflict)
        if markdown is not None and "markdown" not in self._ui_options:
            self._ui_options["markdown"] = markdown
        if hide is not None and "hide" not in self._ui_options:
            self._ui_options["hide"] = hide

        # Handle traits if provided
        if traits:
            for trait in traits:
                if isinstance(trait, type):
                    # It's a trait class, instantiate it
                    trait_instance = trait()
                else:
                    # It's already a trait instance
                    trait_instance = trait
                self.add_child(trait_instance)

    @property
    def variant(self) -> VariantType:
        return self._variant

    @variant.setter
    @BaseNodeElement.emits_update_on_write
    def variant(self, value: VariantType) -> None:
        self._variant = value

    @property
    def title(self) -> str | None:
        return self._title

    @title.setter
    @BaseNodeElement.emits_update_on_write
    def title(self, value: str | None) -> None:
        self._title = value

    @property
    def value(self) -> str:
        return self._value

    @value.setter
    @BaseNodeElement.emits_update_on_write
    def value(self, value: str) -> None:
        self._value = value

    @property
    def button_link(self) -> str | None:
        return self._button_link

    @button_link.setter
    @BaseNodeElement.emits_update_on_write
    def button_link(self, value: str | None) -> None:
        self._button_link = value

    @property
    def button_text(self) -> str | None:
        return self._button_text

    @button_text.setter
    @BaseNodeElement.emits_update_on_write
    def button_text(self, value: str | None) -> None:
        self._button_text = value

    @property
    def full_width(self) -> bool:
        return self._full_width

    @full_width.setter
    @BaseNodeElement.emits_update_on_write
    def full_width(self, value: bool) -> None:
        self._full_width = value

    @property
    def message_icon(self) -> str | None:
        return self._message_icon

    @message_icon.setter
    @BaseNodeElement.emits_update_on_write
    def message_icon(self, value: str | None) -> None:
        self._message_icon = value

    @property
    def button_icon(self) -> str | None:
        return self._button_icon

    @button_icon.setter
    @BaseNodeElement.emits_update_on_write
    def button_icon(self, value: str | None) -> None:
        self._button_icon = value

    @property
    def button_variant(self) -> ButtonVariantType:
        return self._button_variant

    @button_variant.setter
    @BaseNodeElement.emits_update_on_write
    def button_variant(self, value: ButtonVariantType) -> None:
        self._button_variant = value

    @property
    def button_align(self) -> ButtonAlignType:
        return self._button_align

    @button_align.setter
    @BaseNodeElement.emits_update_on_write
    def button_align(self, value: ButtonAlignType) -> None:
        self._button_align = value

    @property
    def markdown(self) -> bool:
        """Get whether markdown rendering is enabled.

        Returns:
            True if markdown rendering is enabled, False otherwise
        """
        return self.ui_options.get("markdown", False)

    @markdown.setter
    @BaseNodeElement.emits_update_on_write
    def markdown(self, value: bool) -> None:
        """Set whether to enable markdown rendering.

        Args:
            value: True to enable markdown rendering, False to disable it
        """
        self.update_ui_options_key("markdown", value)

    @property
    def hide(self) -> bool:
        """Get whether the message is hidden in the UI.

        Returns:
            True if the message should be hidden, False otherwise
        """
        return self.ui_options.get("hide", False)

    @hide.setter
    @BaseNodeElement.emits_update_on_write
    def hide(self, value: bool) -> None:
        """Set whether to hide the message in the UI.

        Args:
            value: True to hide the message, False to show it
        """
        self.update_ui_options_key("hide", value)

    @property
    def ui_options(self) -> dict:
        return self._ui_options

    @ui_options.setter
    @BaseNodeElement.emits_update_on_write
    def ui_options(self, value: dict) -> None:
        self._ui_options = value

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()

        # Use class-level default titles and icons
        title = self.title or self.DEFAULT_TITLES.get(str(self.variant), "")

        # Handle message_icon logic:
        # - "__DEFAULT__" means use the default icon for the variant
        # - None means explicitly no icon (empty string)
        # - Any other string means use that icon
        if self.message_icon == "__DEFAULT__":
            message_icon = self.DEFAULT_ICONS.get(str(self.variant), "")
        elif self.message_icon is None:
            message_icon = ""
        else:
            message_icon = self.message_icon

        # Handle button_icon logic:
        # - None means no icon
        # - Empty string means no icon
        # - Any other string means use that icon
        if self.button_icon is None or self.button_icon == "":
            button_icon = ""
        else:
            button_icon = self.button_icon

        # Check if there are any Button traits with on_click callbacks
        has_button_callback = False
        for child in self.children:
            # Import here to avoid circular imports
            from griptape_nodes.traits.button import Button

            if isinstance(child, Button) and child.on_click_callback is not None:
                has_button_callback = True
                break

        # Merge the UI options with the message-specific options
        # Always include these fields, even if they're None or empty
        message_ui_options = {
            "title": title,
            "variant": self.variant,
            "message_icon": message_icon,
            "button_link": self.button_link,
            "button_text": self.button_text,
            "button_icon": button_icon,
            "button_variant": self.button_variant,
            "button_align": self.button_align,
            "button_on_click": has_button_callback,
            "full_width": self.full_width,
        }

        merged_ui_options = {
            **self.ui_options,
            **message_ui_options,
        }

        data["name"] = self.name
        data["value"] = self.value
        data["default_value"] = self.value  # for compatibility
        data["ui_options"] = merged_ui_options

        return data

    def to_event(self, node: BaseNode) -> dict:
        event_data = super().to_event(node)
        dict_data = self.to_dict()
        # Combine them both to get what we need for the UI.
        event_data.update(dict_data)
        return event_data


class DeprecationMessage(ParameterMessage):
    """A specialized ParameterMessage for deprecation warnings with default warning styling."""

    # Keep the same element_type as ParameterMessage so UI recognizes it
    element_type: str = "ParameterMessage"

    def __init__(
        self,
        value: str,
        button_text: str,
        migrate_function: Callable[[Any, Any], Any],
        **kwargs,
    ):
        """Initialize a deprecation message with default warning styling.

        Args:
            value: The deprecation message text
            button_text: Text for the migration button
            migrate_function: Function to call when migration button is clicked
            **kwargs: Additional arguments passed to ParameterMessage
        """
        # Set defaults for deprecation messages
        kwargs.setdefault("variant", "warning")
        kwargs.setdefault("full_width", True)

        # Add the button trait
        from griptape_nodes.traits.button import Button

        kwargs.setdefault("traits", {})
        kwargs["traits"][Button(label=button_text, icon="plus", variant="secondary", on_click=migrate_function)] = None

        super().__init__(value=value, button_text=button_text, **kwargs)

    def to_dict(self) -> dict:
        """Override to_dict to use element_type instead of class name.

        The base to_dict() method uses self.__class__.__name__ which would return
        "DeprecationMessage", but the UI expects element_type to be "ParameterMessage"
        to recognize it as a valid ParameterMessage element.
        """
        data = super().to_dict()
        data["element_type"] = self.element_type  # Use "ParameterMessage" not "DeprecationMessage"
        return data
