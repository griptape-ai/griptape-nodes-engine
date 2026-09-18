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

    def ui_options_for_trait(self) -> dict:
        return {
            "widget": self.widget_name,
            "library": self.library,
        }
