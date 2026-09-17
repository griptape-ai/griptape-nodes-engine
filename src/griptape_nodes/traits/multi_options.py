from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from griptape_nodes.exe_types.core_types import Parameter, Trait


@dataclass(eq=False)
class MultiOptions(Trait):
    # SERIALIZATION BUG FIX EXPLANATION:
    #
    # PROBLEM: Similar to Options trait, MultiOptions had a potential serialization bug
    # where dynamically populated multi-options lists would work correctly during runtime
    # but could revert after save/reload cycles. This happens because:
    # 1. trait.choices was the "source of truth" during runtime
    # 2. ui_options["multi_options"] was populated from trait.choices
    # 3. Only ui_options gets serialized/deserialized (not trait fields)
    # 4. After reload, trait.choices was stale but ui_options had correct data
    # 5. Converters used stale trait.choices, causing validation issues
    #
    # SOLUTION: Make ui_options the primary source of truth, with _choices as fallback
    # 1. choices property reads from ui_options["multi_options"] when available
    # 2. choices setter writes to BOTH _choices and ui_options (dual sync)
    # 3. This ensures serialized ui_options data is used after deserialization
    # 4. _choices provides safety fallback if ui_options is missing/corrupted

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
        # Set choices through property to ensure dual sync from the start
        if choices is not None:
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
        """Get multi-options choices with ui_options as primary source of truth.

        CRITICAL: This property prioritizes ui_options["multi_options"]["choices"] over _choices
        because ui_options gets properly serialized/deserialized while trait fields don't.

        Read priority:
        1. FIRST: ui_options["multi_options"]["choices"] (survives serialization cycles)
        2. FALLBACK: _choices field (safety net for edge cases)

        This prevents bugs where available choices could become stale after reload.
        """
        # Check if we have a parent parameter with ui_options (normal case after trait attachment)
        if self._parent and hasattr(self._parent, "ui_options"):
            ui_options = getattr(self._parent, "ui_options", None)
            if (
                isinstance(ui_options, dict)
                and "multi_options" in ui_options
                and isinstance(ui_options["multi_options"], dict)
                and "choices" in ui_options["multi_options"]
            ):
                # Use live ui_options data (this survives serialization)
                return ui_options["multi_options"]["choices"]

        # Fallback to internal field (used during initialization or if ui_options missing)
        return self._choices

    @choices.setter
    def choices(self, value: list) -> None:
        """Set multi-options choices with dual synchronization.

        CRITICAL: This setter writes to BOTH locations to maintain consistency:
        1. _choices field (for fallback and ui_options_for_trait())
        2. ui_options["multi_options"]["choices"] (for serialization and runtime use)

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

            # Ensure multi_options exists and is a dict
            if "multi_options" not in self._parent.ui_options or not isinstance(  # type: ignore[attr-defined]
                self._parent.ui_options["multi_options"],  # type: ignore[attr-defined]
                dict,
            ):
                self._parent.ui_options["multi_options"] = {}  # type: ignore[attr-defined]

            # Write choices to ui_options (this gets serialized and survives reload)
            self._parent.ui_options["multi_options"]["choices"] = value  # type: ignore[attr-defined]

    def converters_for_trait(self) -> list[Callable]:
        def converter(value: Any) -> Any:
            # CRITICAL: This converter uses self.choices property (not _choices field)
            # The property reads from ui_options first, ensuring we use post-deserialization
            # choices data instead of stale trait field data.

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
            # CRITICAL: This validator uses self.choices property (not _choices field)
            # Same reasoning as converter - use live ui_options data after deserialization

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
        """
        return {
            "multi_options": {
                "choices": self._choices,
                "placeholder": self.placeholder,
                "max_selected_display": self.max_selected_display,
                "show_search": self.show_search,
                "search_filter": self.search_filter,
                "icon_size": self.icon_size,
                "allow_user_created_options": self.allow_user_created_options,
            }
        }
