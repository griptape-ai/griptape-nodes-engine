from dataclasses import dataclass, field
from typing import Any

from griptape_nodes.exe_types.core_types import Trait
from griptape_nodes.exe_types.trait_state import state_from_rendered_keys


@dataclass(eq=False, kw_only=True)
class Widget(Trait):
    """Associates a parameter with a UI widget from a library.

    Widgets are JavaScript modules that render parameter UI.
    The widget must be registered in the library's widgets list.
    """

    library: str  # Library that provides the widget (e.g., "example_nodes_template")
    element_id: str = field(default_factory=lambda: "Widget")
    synced_parameters: list[str] = field(default_factory=list)

    def __init__(self, name: str, library: str, synced_parameters: list[str] | None = None) -> None:
        super().__init__()
        self.name = name
        self.library = library
        self.synced_parameters = synced_parameters if synced_parameters is not None else []

    def to_state(self) -> dict[str, Any]:
        return {"name": self.name, "library": self.library, "synced_parameters": list(self.synced_parameters)}

    def apply_state(self, state: dict[str, Any]) -> None:
        if "name" in state:
            self.name = state["name"]
        if "library" in state:
            self.library = state["library"]
        if "synced_parameters" in state:
            self.synced_parameters = list(state["synced_parameters"])

    @classmethod
    def state_from_ui_options(cls, ui_options: dict[str, Any]) -> dict[str, Any]:
        return state_from_rendered_keys(
            ui_options, {"widget": "name", "library": "library", "synced_parameters": "synced_parameters"}
        )

    def ui_options_for_trait(self) -> dict:
        options: dict = {
            "widget": self.name,
            "library": self.library,
        }
        if self.synced_parameters:
            options["synced_parameters"] = self.synced_parameters
        return options
