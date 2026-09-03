from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode, Trait


@dataclass(eq=False)
class Slider(Trait):
    STATE_ALIASES: ClassVar[dict[str, str]] = {"min_val": "min", "max_val": "max"}

    min: Any = 0
    max: Any = 100
    element_id: str = field(default_factory=lambda: "Slider")

    _allowed_modes: set = field(default_factory=lambda: {ParameterMode.PROPERTY})

    def __init__(self, min_val: float, max_val: float) -> None:
        super().__init__()
        self.min = min_val
        self.max = max_val

    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return ["slider"]

    def ui_options_for_trait(self) -> dict:
        return {"slider": {"min_val": self.min, "max_val": self.max}}

    def validators_for_trait(self) -> list[Callable[..., Any]]:
        def validate(param: Parameter, value: Any) -> None:  # noqa: ARG001
            if hasattr(value, "__gt__") and hasattr(value, "__lt__") and (value > self.max or value < self.min):
                msg = "Value out of range"
                raise ValueError(msg)

        return [validate]


# These Traits get added to a list on the parameter. When they are added they apply their functions to the parameter.
