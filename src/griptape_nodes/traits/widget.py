from typing import Any, ClassVar

import attrs

from griptape_nodes.exe_types.core_types import Trait


class Widget(Trait):
    """Associates a parameter with a UI widget from a library.

    Widgets are JavaScript modules that render parameter UI.
    The widget must be registered in the library's widgets list.
    """

    # Named apart from the element's own ``name``, which is engine wiring. Which widget to
    # render is this trait's state, and a keyword the element base already takes cannot also be
    # this trait's: attrs would generate a constructor with the argument twice.
    widget_name: str = attrs.field()
    library: str = attrs.field()

    # The key a file saved before the widget's name had a field of its own, when it was written
    # into the element's ``name`` instead.
    LEGACY_NAME_KEY: ClassVar[str] = "name"

    @classmethod
    def migrate_state(cls, state: dict[str, Any]) -> dict[str, Any]:
        """Read a file that saved the widget's name as ``name``."""
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
