"""Writing an element's UI options, and reporting a constructor argument that fights one."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("griptape_nodes")


class UIOptionsMixin:
    """Mixin providing UI options update functionality for classes with ui_options."""

    def _validate_ui_option_conflict(
        self,
        ui_options_dict: dict,
        param_name: str,
        param_value: Any,
    ) -> None:
        """Validate that explicit parameter doesn't conflict with ui_options dict.

        Logs a warning if there's a conflict and the ui_options value will be used.

        Args:
            ui_options_dict: The ui_options dictionary to check
            param_name: Name of the parameter (e.g., "hide", "markdown")
            param_value: Value of the explicit parameter
        """
        if param_name not in ui_options_dict:
            return

        dict_value = ui_options_dict[param_name]

        if param_value != dict_value:
            # Get element name for better error messages
            element_name = getattr(self, "name", None)
            class_name = self.__class__.__name__

            # Build element part
            if element_name:
                element_part = f"{class_name} '{element_name}'"
            else:
                element_part = class_name

            msg = (
                f"{element_part}: Conflicting values for '{param_name}'. "
                f'Explicit parameter {param_name}={param_value!r} conflicts with ui_options["{param_name}"]={dict_value!r}. '
                f"The value from ui_options will be used. Please contact the library author to fix this issue."
            )
            logger.warning(msg)

    def update_ui_options_key(self, key: str, value: Any) -> None:
        """Update a single UI option key."""
        ui_options = self.ui_options
        ui_options[key] = value
        self.ui_options = ui_options

    def update_ui_options(self, updates: dict[str, Any]) -> None:
        """Update multiple UI options at once."""
        ui_options = self.ui_options
        ui_options.update(updates)
        self.ui_options = ui_options
