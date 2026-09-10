from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar

from griptape_nodes.exe_types.core_types import Trait


@dataclass(eq=False)
class Clamp(Trait):
    STATE_ALIASES: ClassVar[dict[str, str]] = {"min_val": "min", "max_val": "max"}

    def __init__(self, min_val: float | None = None, max_val: float | None = None) -> None:
        super().__init__()
        self.min: float | None = min_val
        self.max: float | None = max_val

    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return ["clamp"]

    def _clamp_number(self, value: float) -> float:
        # Keep this as a tiny helper so the converter stays readable and so we can
        # consistently apply one-sided bounds (min-only or max-only) everywhere.
        if self.max is not None and value > self.max:
            value = self.max
        if self.min is not None and value < self.min:
            value = self.min
        return value

    def _clamp_sequence(self, value: list[Any]) -> list[Any]:
        # Clamp historically applied to strings/lists by max length; we keep list handling
        # explicit here so bounds semantics remain obvious.
        if self.max is None:
            return value
        if len(value) <= self.max:
            return value
        # int() because a length cannot be fractional, and slicing by a float raises.
        return value[: int(self.max)]

    def _try_parse_numeric_string(self, value: str) -> float | None:
        # Trait converters run BEFORE parameter-level converters (e.g. ParameterInt/Float
        # parsing). That means UI inputs often arrive as strings here; to make min/max
        # clamping actually work with typical UI inputs, we parse numeric strings when
        # bounds are configured.
        if self.min is None and self.max is None:
            return None

        stripped = value.strip()
        if not stripped:
            return None

        try:
            return float(stripped)
        except ValueError:
            return None

    def converters_for_trait(self) -> list[Callable]:
        def clamp(value: Any) -> Any:
            if isinstance(value, list):
                return self._clamp_sequence(value)

            if isinstance(value, str):
                parsed = self._try_parse_numeric_string(value)
                if parsed is not None:
                    return self._clamp_number(parsed)

                # If it's not a numeric string (or no bounds configured), preserve the
                # historical string behavior: max length clamping only.
                if self.max is None or len(value) <= self.max:
                    return value
                return value[: int(self.max)]

            if isinstance(value, (int, float)):
                return self._clamp_number(float(value))

            return value

        return [clamp]
