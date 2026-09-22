from dataclasses import dataclass, field
from typing import Any

from griptape_nodes.exe_types.core_types import Trait


@dataclass(eq=False, kw_only=True)
class Widget(Trait):
    """Associates a parameter with a UI widget from a library.

    Widgets are JavaScript modules that render parameter UI.
    The widget must be registered in the library's widgets list.
    """

    library: str  # Library that provides the widget (e.g., "example_nodes_template")
    element_id: str = field(default_factory=lambda: "Widget")

    def __init__(self, name: str, library: str) -> None:
        super().__init__()
        self.name = name
        self.library = library

    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return ["widget"]

    def to_state(self) -> dict[str, Any]:
        return {"name": self.name, "library": self.library}

    def apply_state(self, state: dict[str, Any]) -> None:
        if "name" in state:
            self.name = state["name"]
        if "library" in state:
            self.library = state["library"]

    def ui_options_for_trait(self) -> dict:
        return {
            "widget": self.name,
            "library": self.library,
        }
