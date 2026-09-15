from collections.abc import Callable
from typing import Any, ClassVar

import attrs

from griptape_nodes.exe_types.core_types import Parameter, Trait


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

    # Rendered keys carrying the choices, newest first.
    CHOICES_KEYS: ClassVar[tuple[str, ...]] = ("simple_dropdown", "enum_choices")

    # Preserve ``choices`` as the constructor and saved-state key behind the property.
    _choices: list = attrs.field(factory=lambda: list(Options.DEFAULT_CHOICES), alias="choices")
    show_search: bool = attrs.field(default=True)
    search_filter: str = attrs.field(default="")
    allow_custom: bool = attrs.field(default=False)

    @property
    def choices(self) -> list:
        return self._choices

    @choices.setter
    def choices(self, value: list) -> None:
        self._choices = value

    @classmethod
    def state_from_ui_options(cls, ui_options: dict[str, Any]) -> dict[str, Any]:
        """Map flat dropdown options to trait state.

        ``choices`` renders under its own key, so the field loop never picks it up.
        """
        state = {key: ui_options[key] for key in cls.state_keys() if key in ui_options}
        for choices_key in cls.CHOICES_KEYS:
            if choices_key in ui_options:
                state["choices"] = ui_options[choices_key]
                break
        return state

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
        """Render the dropdown.

        Omit false ``allow_custom`` to preserve the existing serialized shape.
        """
        options: dict[str, Any] = {
            "simple_dropdown": self.choices,
            "show_search": self.show_search,
            "search_filter": self.search_filter,
        }
        if self.allow_custom:
            options["allow_custom"] = True
        return options
