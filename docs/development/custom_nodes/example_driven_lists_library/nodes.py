from typing import Any

from griptape_nodes.exe_types.core_types import ParameterList, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode
from griptape_nodes.exe_types.param_types.parameter_json import ParameterJson


class DrivenListsDemo(DataNode):
    """Two lists driven by `Cases`: `Values` is labelled with each case, `Notes` keeps its own numbering."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

        self.add_parameter(
            ParameterList(
                name="cases",
                type="str",
                tooltip="The rows you edit. Each one gets a row in Values and in Notes.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                child_prefix="Case",
            )
        )
        self.add_parameter(
            ParameterList(
                name="values",
                type="str",
                tooltip="One row per case, labelled with the case's value.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                driven_by="cases",
                label_from_driver_value=True,
            )
        )
        self.add_parameter(
            ParameterList(
                name="notes",
                type="str",
                tooltip="One row per case, numbered on its own.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                driven_by="cases",
                child_prefix="Note",
            )
        )
        self.add_parameter(
            ParameterJson(
                name="json",
                tooltip="One object per case, with its value and note.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        cases = self.get_parameter_by_name("cases")
        if not isinstance(cases, ParameterList):
            return
        # Read row by row: a list's value skips empty rows, which would shift the pairing.
        rows = [
            {
                "case": self.get_parameter_value(row.name),
                "value": self.get_parameter_value(f"values_for_{row.name}"),
                "note": self.get_parameter_value(f"notes_for_{row.name}"),
            }
            for row in cases.get_child_parameters()
        ]
        self.parameter_output_values["json"] = rows
