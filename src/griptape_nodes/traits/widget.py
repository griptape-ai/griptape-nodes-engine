from dataclasses import dataclass, field

from griptape_nodes.exe_types.core_types import Trait


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

    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return ["widget"]

    def ui_options_for_trait(self) -> dict:
        options: dict = {
            "widget": self.name,
            "library": self.library,
        }
        if self.synced_parameters:
            options["synced_parameters"] = self.synced_parameters
        return options
