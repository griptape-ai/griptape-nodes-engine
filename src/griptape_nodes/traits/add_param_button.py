from dataclasses import dataclass, field

from griptape_nodes.exe_types.core_types import Trait
from griptape_nodes.traits.button import Button


@dataclass(eq=False)
class AddParameterButton(Trait):
    type: str = field(default_factory=lambda: "AddParameter")
    element_id: str = field(default_factory=lambda: "Button")

    def __init__(self) -> None:
        super().__init__(element_id="AddParameterButton")
        self.add_child(Button(label="AddParameter"))

    def ui_options_for_trait(self) -> dict:
        return {"button": self.type}
