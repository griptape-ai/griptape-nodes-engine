from collections.abc import Callable
from typing import Any, ClassVar

from griptape_nodes.exe_types.core_types import Parameter, Trait


class Slider(Trait):
    STATE_ALIASES: ClassVar[dict[str, str]] = {"min_val": "min", "max_val": "max"}

    def __init__(self, min_val: float, max_val: float) -> None:
        super().__init__()
        self.min = min_val
        self.max = max_val

    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return ["slider"]

    def ui_options_for_trait(self) -> dict:
        return {"slider": {"min_val": self.min, "max_val": self.max}}

    def state_from_ui_options(self, ui_options: dict) -> dict[str, Any]:
        """Adopt slider bounds written straight into the parameter's ``ui_options``.

        The editor's parameter properties panel writes ``slider`` as the same pair
        ``ui_options_for_trait`` renders, so moving a bound there moves the validator with it.
        """
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
