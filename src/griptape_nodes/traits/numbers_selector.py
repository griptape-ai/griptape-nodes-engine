from collections.abc import Callable
from typing import Any

import attrs

from griptape_nodes.exe_types.core_types import Parameter, Trait


class NumbersSelector(Trait):
    defaults: dict[str, float] = attrs.field()
    step: float = attrs.field(default=1.0)
    overall_min: float | None = attrs.field(default=None)
    overall_max: float | None = attrs.field(default=None)

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
