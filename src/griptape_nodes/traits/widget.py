from griptape_nodes.exe_types.core_types import Trait


class Widget(Trait):
    """Associates a parameter with a UI widget from a library.

    Widgets are JavaScript modules that render parameter UI.
    The widget must be registered in the library's widgets list.
    """

    def __init__(self, name: str, library: str) -> None:
        super().__init__()
        self.name = name
        self.library = library

    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return ["widget"]

    def ui_options_for_trait(self) -> dict:
        return {
            "widget": self.name,
            "library": self.library,
        }
