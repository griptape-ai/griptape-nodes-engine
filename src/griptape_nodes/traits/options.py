from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from griptape_nodes.exe_types.core_types import Parameter, Trait


@dataclass(eq=False)
class Options(Trait):
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

    _choices: list = field(default_factory=lambda: ["choice 1", "choice 2", "choice 3"])
    element_id: str = field(default_factory=lambda: "Options")
    show_search: bool = field(default=True)
    search_filter: str = field(default="")
    _allow_user_created_options: bool = field(default=False)

    def __init__(
        self,
        *,
        choices: list | None = None,
        show_search: bool = True,
        search_filter: str = "",
        allow_user_created_options: bool = False,
    ) -> None:
        super().__init__()
        # Set choices through property to ensure dual sync from the start
        if choices is not None:
            self.choices = choices
        self.show_search = show_search
        self.search_filter = search_filter
        self._allow_user_created_options = allow_user_created_options

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

    @property
    def allow_user_created_options(self) -> bool:
        """Whether a value outside ``choices`` is kept, with ui_options as primary source of truth.

        Read priority matches the choices property, for the same reason plus one more:
        Parameter.ui_options lets a manually-set entry override what a trait publishes, so
        ui_options is what the editor actually renders from. Reading it here keeps the engine
        from snapping back a value the widget just offered.

        1. FIRST: ui_options["allow_user_created_options"] (what the editor sees)
        2. FALLBACK: the constructor argument (used before trait attachment)
        """
        if self._parent and hasattr(self._parent, "ui_options"):
            ui_options = getattr(self._parent, "ui_options", None)
            if isinstance(ui_options, dict) and "allow_user_created_options" in ui_options:
                return bool(ui_options["allow_user_created_options"])

        return self._allow_user_created_options

    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return ["options", "models"]

    def converters_for_trait(self) -> list[Callable]:
        def converter(value: Any) -> Any:
            # CRITICAL: This converter uses self.choices property (not _choices field)
            # The property reads from ui_options first, ensuring we use post-deserialization
            # choices data instead of stale trait field data. This prevents the bug where
            # selected values revert to first choice after save/reload.

            # In user-created mode the choices are suggestions, so a value the user typed
            # is kept as-is instead of being snapped back to the first choice.
            if self.allow_user_created_options:
                return value

            if value not in self.choices:
                return self.choices[0]
            return value

        return [converter]

    def validators_for_trait(self) -> list[Callable[[Parameter, Any], Any]]:
        def validator(param: Parameter, value: Any) -> None:  # noqa: ARG001
            # CRITICAL: This validator uses self.choices property (not _choices field)
            # Same reasoning as converter - use live ui_options data after deserialization

            # In user-created mode any value is allowed, so there is nothing to check.
            if self.allow_user_created_options:
                return

            if value not in self.choices:
                msg = "Choice not allowed"
                raise ValueError(msg)

        return [validator]

    def ui_options_for_trait(self) -> dict:
        """Provide UI options for trait initialization.

        IMPORTANT: This method uses _choices (not self.choices property) to avoid
        circular dependency during Parameter.ui_options property construction:

        Circular dependency would be:
        1. Parameter.ui_options calls trait.ui_options_for_trait()
        2. ui_options_for_trait() calls self.choices property
        3. choices property tries to read parent.ui_options
        4. This triggers Parameter.ui_options again → infinite recursion

        Using _choices directly breaks this cycle while still providing the correct
        initial choices for UI rendering. The property-based sync handles runtime updates.
        _allow_user_created_options is read directly for the same reason.
        """
        return {
            "simple_dropdown": self._choices,
            "show_search": self.show_search,
            "search_filter": self.search_filter,
            "allow_user_created_options": self._allow_user_created_options,
        }
