from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode, Trait
from griptape_nodes.exe_types.trait_state import state_from_rendered_keys

DATE_FORMAT = "%Y-%m-%d"
DATE_TIME_FORMAT = "%Y-%m-%dT%H:%M"


@dataclass(eq=False)
class DatePicker(Trait):
    """Calendar picker for a `str` parameter holding an ISO 8601 local date.

    Values are `2026-10-14`, or `2026-10-14T09:30` when `include_time` is set, so
    downstream nodes can read them with `date.fromisoformat` / `datetime.fromisoformat`.
    `min` and `max` are optional bounds in the same format as the value.
    """

    include_time: bool = False
    min: str | None = None
    max: str | None = None
    element_id: str = field(default_factory=lambda: "DatePicker")

    _allowed_modes: set = field(default_factory=lambda: {ParameterMode.PROPERTY})

    def __init__(
        self,
        *,
        include_time: bool = False,
        min: str | None = None,  # noqa: A002
        max: str | None = None,  # noqa: A002
    ) -> None:
        super().__init__()
        self.include_time = include_time
        self.min = min
        self.max = max

    def to_state(self) -> dict[str, Any]:
        return {"include_time": self.include_time, "min": self.min, "max": self.max}

    def apply_state(self, state: dict[str, Any]) -> None:
        if "include_time" in state:
            self.include_time = state["include_time"]
        if "min" in state:
            self.min = state["min"]
        if "max" in state:
            self.max = state["max"]

    @classmethod
    def state_from_ui_options(cls, ui_options: dict[str, Any]) -> dict[str, Any]:
        return state_from_rendered_keys(
            ui_options.get("date_picker"), {"include_time": "include_time", "min": "min", "max": "max"}
        )

    def ui_options_for_trait(self) -> dict:
        options: dict[str, Any] = {"include_time": self.include_time}
        if self.min is not None:
            options["min"] = self.min
        if self.max is not None:
            options["max"] = self.max
        return {"date_picker": options}

    def validators_for_trait(self) -> list[Callable[..., Any]]:
        def validate(param: Parameter, value: Any) -> None:
            if value is None or value == "":
                return
            if not isinstance(value, str):
                msg = (
                    f"Attempted to set '{param.name}' to {value!r}. "
                    f"Failed because a date must be text like {self._example()}."
                )
                raise TypeError(msg)
            parsed = self._parse(value)
            if parsed is None:
                msg = (
                    f"Attempted to set '{param.name}' to '{value}'. "
                    f"Failed because it is not a date in the form {self._example()}."
                )
                raise ValueError(msg)
            lower = self._parse_bound(self.min, "min")
            if lower is not None and parsed < lower:
                msg = f"Attempted to set '{param.name}' to '{value}'. Failed because it is earlier than {self.min}."
                raise ValueError(msg)
            upper = self._parse_bound(self.max, "max")
            if upper is not None and parsed > upper:
                msg = f"Attempted to set '{param.name}' to '{value}'. Failed because it is later than {self.max}."
                raise ValueError(msg)

        return [validate]

    def _parse(self, value: str) -> date | datetime | None:
        """Parse a value in this picker's fixed format, or return None if it doesn't match."""
        parsed = _strict_parse(value, self._format())
        if parsed is None:
            return None
        if self.include_time:
            return parsed
        return parsed.date()

    def _parse_bound(self, bound: str | None, name: str) -> date | datetime | None:
        if bound is None:
            return None
        parsed = self._parse(bound)
        if parsed is None and self.include_time:
            # A date-only bound covers its whole day, so it keeps working when include_time is turned on.
            day = _strict_parse(bound, DATE_FORMAT)
            if day is not None:
                parsed = datetime.combine(day.date(), time(23, 59) if name == "max" else time(0, 0))
        if parsed is None:
            msg = (
                f"Attempted to check a date against the DatePicker's {name} '{bound}'. "
                f"Failed because the {name} is not in the form {self._example()}."
            )
            raise ValueError(msg)
        return parsed

    def _format(self) -> str:
        if self.include_time:
            return DATE_TIME_FORMAT
        return DATE_FORMAT

    def _example(self) -> str:
        if self.include_time:
            return "2026-10-14T09:30"
        return "2026-10-14"


def _strict_parse(value: str, fmt: str) -> datetime | None:
    try:
        parsed = datetime.strptime(value, fmt)  # noqa: DTZ007 - values are local, with no offset
    except ValueError:
        return None
    # strptime accepts unpadded fields like "2026-1-5", which would not round-trip.
    if parsed.strftime(fmt) != value:
        return None
    return parsed
