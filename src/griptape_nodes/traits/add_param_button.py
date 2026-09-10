from dataclasses import dataclass

from griptape_nodes.exe_types.core_types import Trait
from griptape_nodes.traits.button import Button


@dataclass(eq=False)
class AddParameterButton(Trait):
    def __init__(self) -> None:
        super().__init__(element_id="AddParameterButton")
        self.type = "AddParameter"
        self.add_child(Button(label="AddParameter"))

    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return ["button", "addbutton"]

    def ui_options_for_trait(self) -> dict:
        return {"button": self.type}
