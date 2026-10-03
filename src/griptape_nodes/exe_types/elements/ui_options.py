"""Writing an element's UI options, and reporting a constructor argument that fights one."""

from __future__ import annotations

import logging
from typing import Any, NamedTuple

logger = logging.getLogger("griptape_nodes")


class UIOptionConflict(NamedTuple):
    """A constructor argument that disagreed with the element's ``ui_options``."""

    param_name: str
    param_value: Any
    dict_value: Any


class UIOptionsMixin:
    """Mixin providing UI options update functionality for classes with ui_options."""

    def _validate_ui_option_conflict(
        self,
        ui_options_dict: dict,
        param_name: str,
        param_value: Any,
    ) -> None:
        """Validate that explicit parameter doesn't conflict with ui_options dict.

        Logs a warning if there's a conflict and the ui_options value will be used. The warning
        waits until the element belongs to a node (see ``report_ui_option_conflicts``).

        Args:
            ui_options_dict: The ui_options dictionary to check
            param_name: Name of the parameter (e.g., "hide", "markdown")
            param_value: Value of the explicit parameter
        """
        if param_name not in ui_options_dict:
            return

        dict_value = ui_options_dict[param_name]
        if param_value == dict_value:
            return

        conflict = UIOptionConflict(param_name=param_name, param_value=param_value, dict_value=dict_value)
        pending = getattr(self, "_pending_ui_option_conflicts", None)
        if pending is None:
            pending = []
            self._pending_ui_option_conflicts = pending
        pending.append(conflict)
        self.report_ui_option_conflicts()

    def report_ui_option_conflicts(self) -> None:
        """Log conflicts found at construction once the element belongs to a node.

        Elements are usually built before they are added to a node, so a conflict waits here
        until the warning can name the node and the library that defined it.
        """
        node = getattr(self, "_node_context", None)
        pending = getattr(self, "_pending_ui_option_conflicts", None)
        if node is None or not pending:
            return

        self._pending_ui_option_conflicts = []
        element_name = getattr(self, "name", None)
        class_name = self.__class__.__name__
        if element_name:
            element_part = f"{class_name} '{element_name}'"
        else:
            element_part = class_name

        node_type = node.metadata.get("node_type") or node.__class__.__name__
        library_name = node.metadata.get("library")
        if library_name:
            node_part = f"Node '{node.name}' ({node_type} from library '{library_name}')"
            contact = f"Please contact the author of library '{library_name}' to fix this issue."
        else:
            node_part = f"Node '{node.name}' ({node_type})"
            contact = "Please contact the library author to fix this issue."

        for conflict in pending:
            msg = (
                f"{node_part}, {element_part}: Conflicting values for '{conflict.param_name}'. "
                f"Explicit parameter {conflict.param_name}={conflict.param_value!r} conflicts with "
                f'ui_options["{conflict.param_name}"]={conflict.dict_value!r}. '
                f"The value from ui_options will be used. {contact}"
            )
            logger.warning(msg)

    def authored_ui_options(self) -> dict[str, Any]:
        """Return stored options without values derived by subclasses."""
        return dict(self._ui_options)  # type: ignore[attr-defined]

    def update_ui_options_key(self, key: str, value: Any) -> None:
        """Update a single UI option key."""
        self.update_ui_options({key: value})

    def update_ui_options(self, updates: dict[str, Any]) -> None:
        """Update stored options without copying derived options into them."""
        authored = self.authored_ui_options()
        authored.update(updates)
        self.ui_options = authored  # type: ignore[attr-defined]

    def remove_ui_options_key(self, key: str) -> None:
        """Remove a stored option without copying derived options into storage."""
        authored = self.authored_ui_options()
        authored.pop(key, None)
        self.ui_options = authored  # type: ignore[attr-defined]

    def report_ui_options_change(self) -> None:
        """Report derived UI options without storing them."""
        self.track_change("ui_options", self.ui_options)  # type: ignore[attr-defined]


def seed_ui_options(element: UIOptionsMixin, ui_options: dict, values: dict[str, Any]) -> None:
    """Write the display arguments a constructor took into ``ui_options``.

    A value left as ``None`` was not asked for. ``ui_options`` wins a conflict, and the
    element reports one.
    """
    for key, value in values.items():
        if value is None:
            continue
        element._validate_ui_option_conflict(ui_options_dict=ui_options, param_name=key, param_value=value)
        if key not in ui_options:
            ui_options[key] = value
