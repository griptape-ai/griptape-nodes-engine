from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from griptape_nodes.exe_types.core_types import Parameter, Trait


@dataclass(eq=False)
class MultiOptions(Trait):
    DEFAULT_CHOICES: ClassVar[list[str]] = ["choice 1", "choice 2", "choice 3"]

    _choices: list = field(default_factory=lambda: list(MultiOptions.DEFAULT_CHOICES))
    placeholder: str = "Select options..."
    max_selected_display: int = 3
    show_search: bool = True
    search_filter: str = ""
    icon_size: str = "small"
    allow_user_created_options: bool = False
    element_id: str = field(default_factory=lambda: "MultiOptions")

    def __init__(  # noqa: PLR0913
        self,
        *,
        choices: list | None = None,
        placeholder: str = "Select options...",
        max_selected_display: int = 3,
        show_search: bool = True,
        search_filter: str = "",
        icon_size: str = "small",
        allow_user_created_options: bool = False,
    ) -> None:
        super().__init__(element_id="MultiOptions")
        self._choices = list(self.DEFAULT_CHOICES) if choices is None else choices
        self.placeholder = placeholder
        self.max_selected_display = max_selected_display
        self.show_search = show_search
        self.search_filter = search_filter
        self.icon_size = icon_size if icon_size in ("small", "large") else "small"
        self.allow_user_created_options = allow_user_created_options

    @property
    def choices(self) -> list:
        return self._choices

    @choices.setter
    def choices(self, value: list) -> None:
        self._choices = value

    def to_state(self) -> dict[str, Any]:
        return {
            "choices": self.choices,
            "placeholder": self.placeholder,
            "max_selected_display": self.max_selected_display,
            "show_search": self.show_search,
            "search_filter": self.search_filter,
            "icon_size": self.icon_size,
            "allow_user_created_options": self.allow_user_created_options,
        }

    def apply_state(self, state: dict[str, Any]) -> None:
        if "choices" in state:
            self.choices = state["choices"]
        for name in (
            "placeholder",
            "max_selected_display",
            "show_search",
            "search_filter",
            "allow_user_created_options",
        ):
            if name in state:
                setattr(self, name, state[name])
        if "icon_size" in state:
            icon_size = state["icon_size"]
            self.icon_size = icon_size if icon_size in ("small", "large") else "small"

    @classmethod
    def state_from_ui_options(cls, ui_options: dict[str, Any]) -> dict[str, Any]:
        written = ui_options.get("multi_options")
        if not isinstance(written, dict):
            return {}
        keys = (
            "choices",
            "placeholder",
            "max_selected_display",
            "show_search",
            "search_filter",
            "icon_size",
            "allow_user_created_options",
        )
        return {key: written[key] for key in keys if key in written}

    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return ["multi_options"]

    def converters_for_trait(self) -> list[Callable]:
        def converter(value: Any) -> Any:
            if not isinstance(value, list):
                if value is None:
                    return []
                value = [value]

            if self.allow_user_created_options:
                return [str(option) for option in value if option is not None and str(option).strip()]

            return [option for option in value if option in self.choices]

        return [converter]

    def validators_for_trait(self) -> list[Callable[[Parameter, Any], Any]]:
        def validator(param: Parameter, value: Any) -> None:  # noqa: ARG001
            if value is None or value == []:
                return
            if not isinstance(value, list):
                msg = "MultiOptions value must be a list"
                raise TypeError(msg)
            if self.allow_user_created_options:
                for option in value:
                    if not isinstance(option, str):
                        msg = f"All options must be strings, found: {type(option).__name__}"
                        raise TypeError(msg)
                    if not option.strip():
                        msg = "Options cannot be empty strings"
                        raise ValueError(msg)
                return

            invalid_choices = [option for option in value if option not in self.choices]
            if invalid_choices:
                msg = f"Invalid choices: {invalid_choices}"
                raise ValueError(msg)

        return [validator]

    def ui_options_for_trait(self) -> dict:
        return {
            "multi_options": {
                "choices": self.choices,
                "placeholder": self.placeholder,
                "max_selected_display": self.max_selected_display,
                "show_search": self.show_search,
                "search_filter": self.search_filter,
                "icon_size": self.icon_size,
                "allow_user_created_options": self.allow_user_created_options,
            }
        }
