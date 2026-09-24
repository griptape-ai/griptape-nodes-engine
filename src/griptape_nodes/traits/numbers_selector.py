from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode, Trait
from griptape_nodes.exe_types.trait_state import state_from_rendered_keys


@dataclass(eq=False)
class NumbersSelector(Trait):
    defaults: dict[str, float] = field(kw_only=True)
    step: float = 1.0
    overall_min: float | None = None
    overall_max: float | None = None
    element_id: str = field(default_factory=lambda: "NumbersSelector")

    _allowed_modes: set = field(default_factory=lambda: {ParameterMode.PROPERTY})

    def __init__(
        self,
        defaults: dict[str, float],
        step: float = 1.0,
        overall_min: float | None = None,
        overall_max: float | None = None,
    ) -> None:
        super().__init__()
        self.defaults = defaults
        self.step = step
        self.overall_min = overall_min
        self.overall_max = overall_max

    def to_state(self) -> dict[str, Any]:
        return {
            "defaults": self.defaults,
            "step": self.step,
            "overall_min": self.overall_min,
            "overall_max": self.overall_max,
        }

    def apply_state(self, state: dict[str, Any]) -> None:
        for name in ("defaults", "step", "overall_min", "overall_max"):
            if name in state:
                setattr(self, name, state[name])

    @classmethod
    def state_from_ui_options(cls, ui_options: dict[str, Any]) -> dict[str, Any]:
        keys = ("defaults", "step", "overall_min", "overall_max")
        return state_from_rendered_keys(ui_options.get("numbers_selector"), {key: key for key in keys})

    def ui_options_for_trait(self) -> dict:
        return {
            "numbers_selector": {
                "step": self.step,
                "overall_min": self.overall_min,
                "overall_max": self.overall_max,
                "defaults": self.defaults,
            }
        }

    def display_options_for_trait(self) -> dict:
        return {}

    def converters_for_trait(self) -> list[Callable]:
        return []

    def validators_for_trait(self) -> list[Callable[..., Any]]:
        def validate(_param: Parameter, value: Any) -> None:
            if value is None:
                return

            if not isinstance(value, dict):
                msg = "NumbersSelector value must be a dictionary"
                raise TypeError(msg)

            for key, val in value.items():
                if not isinstance(key, str):
                    msg = f"NumbersSelector keys must be strings, got {type(key)}"
                    raise TypeError(msg)

                if not isinstance(val, (int, float)):
                    msg = f"NumbersSelector values must be numbers, got {type(val)} for key '{key}'"
                    raise TypeError(msg)

                if self.overall_min is not None and val < self.overall_min:
                    msg = f"Value {val} for key '{key}' is below minimum {self.overall_min}"
                    raise ValueError(msg)

                if self.overall_max is not None and val > self.overall_max:
                    msg = f"Value {val} for key '{key}' is above maximum {self.overall_max}"
                    raise ValueError(msg)

        return [validate]
