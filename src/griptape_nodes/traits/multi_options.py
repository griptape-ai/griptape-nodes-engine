from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from griptape_nodes.exe_types.core_types import Parameter, Trait


@dataclass(eq=False)
class MultiOptions(Trait):
    DEFAULT_CHOICES: ClassVar[list[str]] = ["choice 1", "choice 2", "choice 3"]

    _choices: list = field(default_factory=lambda: ["choice 1", "choice 2", "choice 3"])
    element_id: str = field(default_factory=lambda: "MultiOptions")
    placeholder: str = field(default="Select options...")
    max_selected_display: int = field(default=3)
    show_search: bool = field(default=True)
    search_filter: str = field(default="")
    icon_size: str = field(default="small")
    allow_user_created_options: bool = field(default=False)

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
        super().__init__()
        # Assigned unconditionally: this class declares its own __init__, so the dataclass
        # field default above never runs.
        if choices is None:
            self.choices = list(self.DEFAULT_CHOICES)
        else:
            self.choices = choices

        self.placeholder = placeholder
        self.max_selected_display = max_selected_display
        self.show_search = show_search
        self.search_filter = search_filter
        self.allow_user_created_options = allow_user_created_options

        # Validate icon_size
        if icon_size not in ["small", "large"]:
            self.icon_size = "small"
        else:
            self.icon_size = icon_size

    @property
    def choices(self) -> list:
        return self._choices

    @choices.setter
    def choices(self, value: list) -> None:
        self._choices = value

    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return ["multi_options"]

    def converters_for_trait(self) -> list[Callable]:
        def converter(value: Any) -> Any:
            # Handle case where value is not a list (convert single values to list)
            if not isinstance(value, list):
                if value is None:
                    return []
                value = [value]

            # When allow_user_created_options is enabled, accept any string values
            # without validating against predefined choices
            if self.allow_user_created_options:
                # Filter out non-string values and ensure all options are valid strings
                valid_options = [str(v) for v in value if v is not None and str(v).strip()]
                return valid_options

            # Standard multi-options mode: filter out invalid choices and return valid ones
            valid_choices = [v for v in value if v in self.choices]

            # If no valid choices, return empty list (allow empty selection)
            return valid_choices

        return [converter]

    def validators_for_trait(self) -> list[Callable[[Parameter, Any], Any]]:
        def validator(param: Parameter, value: Any) -> None:  # noqa: ARG001
            # Allow None or empty list as valid (no selection)
            if value is None or value == []:
                return

            # Ensure value is a list
            if not isinstance(value, list):
                msg = "MultiOptions value must be a list"
                raise TypeError(msg)

            # When allow_user_created_options is enabled, validate that all values are strings
            # but don't validate against predefined choices
            if self.allow_user_created_options:
                for option in value:
                    if not isinstance(option, str):
                        msg = f"All options must be strings, found: {type(option).__name__}"
                        raise TypeError(msg)

                    if not option.strip():
                        msg = "Options cannot be empty strings"
                        raise ValueError(msg)
                return

            # Standard multi-options mode: check that all selected values are valid choices
            invalid_choices = [v for v in value if v not in self.choices]
            if invalid_choices:
                msg = f"Invalid choices: {invalid_choices}"
                raise ValueError(msg)

        return [validator]

    def ui_options_for_trait(self) -> dict:
        """Render the multi-select for the editor."""
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
