from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode, Trait


@dataclass(eq=False)
class Slider(Trait):
    min: Any = 0
    max: Any = 100
    # When True the range only sizes the slider track: dragging stays within min/max,
    # but a value typed outside them is accepted instead of rejected. This is the
    # "soft limit" convention artists know from Nuke/Maya/Houdini.
    soft_limits: bool = False
    element_id: str = field(default_factory=lambda: "Slider")

    _allowed_modes: set = field(default_factory=lambda: {ParameterMode.PROPERTY})

    def __init__(self, min_val: float, max_val: float, *, soft_limits: bool = False) -> None:
        super().__init__()
        self.min = min_val
        self.max = max_val
        self.soft_limits = soft_limits

    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return ["slider"]

    def ui_options_for_trait(self) -> dict:
        slider_options: dict[str, Any] = {"min_val": self.min, "max_val": self.max}
        # Only emitted when soft, so hard-limited sliders keep their existing payload.
        if self.soft_limits:
            slider_options["soft_limits"] = True
        return {"slider": slider_options}

    def validators_for_trait(self) -> list[Callable[..., Any]]:
        if self.soft_limits:
            return []

        def validate(param: Parameter, value: Any) -> None:
            if hasattr(value, "__gt__") and hasattr(value, "__lt__") and (value > self.max or value < self.min):
                msg = (
                    f"Attempted to set '{param.name}' to {value}. "
                    f"Failed because it must be between {self.min} and {self.max}."
                )
                raise ValueError(msg)

        return [validate]


# These Traits get added to a list on the parameter. When they are added they apply their functions to the parameter.
