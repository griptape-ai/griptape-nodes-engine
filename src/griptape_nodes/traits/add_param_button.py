from dataclasses import dataclass, field

from griptape_nodes.exe_types.core_types import Trait
from griptape_nodes.traits.button import Button


@dataclass(eq=False)
class AddParameterButton(Trait):
    type: str = field(default_factory=lambda: "AddParameter")
    element_id: str = field(default_factory=lambda: "Button")

    def __init__(self) -> None:
        super().__init__(element_id="AddParameterButton")
        # Assigned here because this class declares its own __init__, so the dataclass field
        # default above never runs and ui_options_for_trait would raise on a missing attribute.
        self.type = "AddParameter"
        self.add_child(Button(label="AddParameter"))

    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return ["button", "addbutton"]

    def ui_options_for_trait(self) -> dict:
        return {"button": self.type}
