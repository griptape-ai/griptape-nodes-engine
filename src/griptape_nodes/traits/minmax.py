from collections.abc import Callable
from typing import Any

import attrs

from griptape_nodes.exe_types.core_types import Parameter, Trait


class MinMax(Trait):
    min: float = attrs.field(alias="min_val")
    max: float = attrs.field(alias="max_val")

    def ui_options_for_trait(self) -> dict:
        return {"multiline": True}

    def display_options_for_trait(self) -> dict:
        return {}

    def converters_for_trait(self) -> list[Callable]:
        return []

    def validators_for_trait(self) -> list[Callable[..., Any]]:
        def validate(param: Parameter, value: Any) -> None:  # noqa: ARG001
            if value > self.max or value < self.min:
                msg = "Value out of range"
                raise ValueError(msg)

        return [validate]


# These Traits get added to a list on the parameter. When they are added they apply their functions to the parameter.
