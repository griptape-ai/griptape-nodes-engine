from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar

from griptape_nodes.exe_types.core_types import Parameter, Trait


@dataclass(eq=False)
class Options(Trait):
    """Offers a parameter's value as a list of choices.

    By default the list is the whole set of valid values: the parameter renders as a
    dropdown, and a value outside ``choices`` is snapped back to the first choice and
    fails validation. Use this when an unrecognized value would fail at run time --
    rejecting bad input up front beats a node that errors mid-flow.

    ``allow_custom=True`` turns the list into hints instead. The parameter renders as a
    text field that offers matching choices as the user types, and stores whatever they
    type. Use it when the list is a convenience rather than the full set of valid values,
    such as a model id the provider added after the node shipped, or a user's own
    fine-tune. The flag drops the converter and the validator, so updating ``choices`` at
    run time cannot invalidate a value the node already holds.
    """

    DEFAULT_CHOICES: ClassVar[list[str]] = ["choice 1", "choice 2", "choice 3"]

    def __init__(
        self,
        *,
        choices: list | None = None,
        show_search: bool = True,
        search_filter: str = "",
        allow_custom: bool = False,
    ) -> None:
        super().__init__()
        # Assigned unconditionally: this class declares its own __init__, so the dataclass
        # field default above never runs.
        if choices is None:
            self.choices = list(self.DEFAULT_CHOICES)
        else:
            self.choices = choices
        self.show_search = show_search
        self.search_filter = search_filter
        self.allow_custom = allow_custom

    @property
    def choices(self) -> list:
        return self._choices

    @choices.setter
    def choices(self, value: list) -> None:
        self._choices = value

    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return ["options", "models"]

    def converters_for_trait(self) -> list[Callable]:
        # The choices are hints, so there is nothing to snap a typed value back to.
        if self.allow_custom:
            return []

        def converter(value: Any) -> Any:
            if value not in self.choices:
                return self.choices[0]
            return value

        return [converter]

    def validators_for_trait(self) -> list[Callable[[Parameter, Any], Any]]:
        # The choices are hints, so any value is allowed and there is nothing to check.
        if self.allow_custom:
            return []

        def validator(param: Parameter, value: Any) -> None:  # noqa: ARG001
            if value not in self.choices:
                msg = "Choice not allowed"
                raise ValueError(msg)

        return [validator]

    def ui_options_for_trait(self) -> dict:
        """Render the dropdown for the editor.

        ``allow_custom`` is published only when set so that every already-saved dropdown
        keeps serializing exactly the keys it does today.
        """
        options: dict[str, Any] = {
            "simple_dropdown": self.choices,
            "show_search": self.show_search,
            "search_filter": self.search_filter,
        }
        if self.allow_custom:
            options["allow_custom"] = True
        return options
