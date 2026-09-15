import attrs

from griptape_nodes.exe_types.core_types import WIRING, Trait, default_element_id
from griptape_nodes.traits.button import Button


class AddParameterButton(Trait):
    element_id: str = attrs.field(default="AddParameterButton", converter=default_element_id, metadata=WIRING)

    def __attrs_post_init__(self) -> None:
        super().__attrs_post_init__()
        self.type = "AddParameter"
        self.add_child(Button(label="AddParameter"))

    def ui_options_for_trait(self) -> dict:
        return {"button": self.type}
