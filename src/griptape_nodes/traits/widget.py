from typing import Any, ClassVar

import attrs

from griptape_nodes.exe_types.core_types import Trait


class Widget(Trait):
    """Associates a parameter with a UI widget from a library.

    Widgets are JavaScript modules that render parameter UI.
    The widget must be registered in the library's widgets list.
    """

    # Avoid collision with the element wiring field named ``name``.
    widget_name: str = attrs.field()
    library: str = attrs.field()

    # Saved-state key used before ``widget_name``.
    LEGACY_NAME_KEY: ClassVar[str] = "name"

    @classmethod
    def migrate_state(cls, state: dict[str, Any]) -> dict[str, Any]:
        if cls.LEGACY_NAME_KEY not in state or "widget_name" in state:
            return state
        migrated = dict(state)
        migrated["widget_name"] = migrated.pop(cls.LEGACY_NAME_KEY)
        return migrated

    def ui_options_for_trait(self) -> dict:
        return {
            "widget": self.widget_name,
            "library": self.library,
        }
