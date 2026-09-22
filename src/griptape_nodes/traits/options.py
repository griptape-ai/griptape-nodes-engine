from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from griptape_nodes.exe_types.core_types import Parameter, Trait


@dataclass(eq=False)
class Options(Trait):
    """Offer a parameter's value as a list of choices."""

    DEFAULT_CHOICES: ClassVar[list[str]] = ["choice 1", "choice 2", "choice 3"]
    CHOICES_KEYS: ClassVar[tuple[str, ...]] = ("simple_dropdown", "enum_choices")

    _choices: list = field(default_factory=lambda: list(Options.DEFAULT_CHOICES))
    show_search: bool = True
    search_filter: str = ""
    allow_custom: bool = False
    element_id: str = field(default_factory=lambda: "Options")

    def __init__(
        self,
        *,
        choices: list | None = None,
        show_search: bool = True,
        search_filter: str = "",
        allow_custom: bool = False,
    ) -> None:
        super().__init__(element_id="Options")
        self._choices = list(self.DEFAULT_CHOICES) if choices is None else choices
        self.show_search = show_search
        self.search_filter = search_filter
        self.allow_custom = allow_custom

    @property
    def choices(self) -> list:
        return self._choices

    @choices.setter
    def choices(self, value: list) -> None:
        self._choices = value

    def to_state(self) -> dict[str, Any]:
        return {
            "choices": self.choices,
            "show_search": self.show_search,
            "search_filter": self.search_filter,
            "allow_custom": self.allow_custom,
        }

    def apply_state(self, state: dict[str, Any]) -> None:
        if "choices" in state:
            self.choices = state["choices"]
        for name in ("show_search", "search_filter", "allow_custom"):
            if name in state:
                setattr(self, name, state[name])

    @classmethod
    def state_from_ui_options(cls, ui_options: dict[str, Any]) -> dict[str, Any]:
        state = {key: ui_options[key] for key in ("show_search", "search_filter", "allow_custom") if key in ui_options}
        for choices_key in cls.CHOICES_KEYS:
            if choices_key in ui_options:
                state["choices"] = ui_options[choices_key]
                break
        return state

    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return ["options", "models"]

    def converters_for_trait(self) -> list[Callable]:
        if self.allow_custom:
            return []

        def converter(value: Any) -> Any:
            if value not in self.choices:
                return self.choices[0]
            return value

        return [converter]

    def validators_for_trait(self) -> list[Callable[[Parameter, Any], Any]]:
        if self.allow_custom:
            return []

        def validator(param: Parameter, value: Any) -> None:  # noqa: ARG001
            if value not in self.choices:
                msg = "Choice not allowed"
                raise ValueError(msg)

        return [validator]

    def ui_options_for_trait(self) -> dict:
        options: dict[str, Any] = {
            "simple_dropdown": self.choices,
            "show_search": self.show_search,
            "search_filter": self.search_filter,
        }
        if self.allow_custom:
            options["allow_custom"] = True
        return options
