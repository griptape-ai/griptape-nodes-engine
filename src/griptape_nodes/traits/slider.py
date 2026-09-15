from collections.abc import Callable
from typing import Any

import attrs

from griptape_nodes.exe_types.core_types import Parameter, Trait


class Slider(Trait):
    min: float = attrs.field(alias="min_val")
    max: float = attrs.field(alias="max_val")

    def ui_options_for_trait(self) -> dict:
        return {"slider": {"min_val": self.min, "max_val": self.max}}

    @classmethod
    def state_from_ui_options(cls, ui_options: dict) -> dict[str, Any]:
        """Map flat slider options to trait state."""
        written = ui_options.get("slider")
        if not isinstance(written, dict):
            return {}
        return {key: written[key] for key in ("min_val", "max_val") if key in written}

    def validators_for_trait(self) -> list[Callable[..., Any]]:
        def validate(param: Parameter, value: Any) -> None:  # noqa: ARG001
            if hasattr(value, "__gt__") and hasattr(value, "__lt__") and (value > self.max or value < self.min):
                msg = "Value out of range"
                raise ValueError(msg)

        return [validate]


# These Traits get added to a list on the parameter. When they are added they apply their functions to the parameter.
