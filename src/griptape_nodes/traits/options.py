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

    # SERIALIZATION BUG FIX EXPLANATION:
    #
    # PROBLEM: Options trait had a serialization bug where dynamically populated dropdown
    # lists would work correctly during runtime but revert to the first choice after
    # save/reload cycles. This happened because:
    # 1. trait.choices was the "source of truth" during runtime
    # 2. ui_options["simple_dropdown"] was populated from trait.choices
    # 3. Only ui_options gets serialized/deserialized (not trait fields)
    # 4. After reload, trait.choices was stale but ui_options had correct data
    # 5. Converters used stale trait.choices, causing values to revert to first choice
    #
    # SOLUTION: Make ui_options the primary source of truth, with _choices as fallback
    # 1. choices property reads from ui_options["simple_dropdown"] when available
    # 2. choices setter writes to BOTH _choices and ui_options (dual sync)
    # 3. This ensures serialized ui_options data is used after deserialization
    # 4. _choices provides safety fallback if ui_options is missing/corrupted

    DEFAULT_CHOICES: ClassVar[list[str]] = ["choice 1", "choice 2", "choice 3"]

    # Preserve ``choices`` as the constructor and saved-state key behind the property.
    _choices: list = attrs.field(factory=lambda: list(Options.DEFAULT_CHOICES), alias="choices")
    show_search: bool = attrs.field(default=True)
    search_filter: str = attrs.field(default="")
    allow_custom: bool = attrs.field(default=False)

    @property
    def choices(self) -> list:
        """Get dropdown choices with ui_options as primary source of truth.

        CRITICAL: This property prioritizes ui_options["simple_dropdown"] over _choices
        because ui_options gets properly serialized/deserialized while trait fields don't.

        Read priority:
        1. FIRST: ui_options["simple_dropdown"] (survives serialization cycles)
        2. FALLBACK: _choices field (safety net for edge cases)

        This fixes the bug where selected values reverted to first choice after reload.
        """
        # Check if we have a parent parameter with ui_options (normal case after trait attachment)
        if self._parent and hasattr(self._parent, "ui_options"):
            ui_options = getattr(self._parent, "ui_options", None)
            if isinstance(ui_options, dict) and "simple_dropdown" in ui_options:
                # Use live ui_options data (this survives serialization)
                return ui_options["simple_dropdown"]

        # Fallback to internal field (used during initialization or if ui_options missing)
        return self._choices

    @choices.setter
    def choices(self, value: list) -> None:
        """Set dropdown choices with dual synchronization.

        CRITICAL: This setter writes to BOTH locations to maintain consistency:
        1. _choices field (for fallback and ui_options_for_trait())
        2. ui_options["simple_dropdown"] (for serialization and runtime use)

        This dual sync ensures:
        - Immediate runtime consistency
        - Proper serialization of choices data
        - Fallback safety if either location fails
        """
        # Always update internal field first (provides fallback safety)
        self._choices = value

        # Sync to ui_options if we have a parent parameter (normal case after trait attachment)
        if self._parent and hasattr(self._parent, "ui_options"):
            ui_options = getattr(self._parent, "ui_options", None)
            if not isinstance(ui_options, dict):
                # Initialize ui_options if it doesn't exist or isn't a dict
                self._parent.ui_options = {}  # type: ignore[attr-defined]
            # Write choices to ui_options (this gets serialized and survives reload)
            self._parent.ui_options["simple_dropdown"] = value  # type: ignore[attr-defined]

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
            "simple_dropdown": self._choices,
            "show_search": self.show_search,
            "search_filter": self.search_filter,
        }
        if self.allow_custom:
            options["allow_custom"] = True
        return options
