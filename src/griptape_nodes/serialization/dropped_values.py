"""Values left out of something read back later, and how to tell the user about them."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from griptape_nodes.retained_mode.events.base_events import ResultDetail, ResultDetails
from griptape_nodes.serialization.values import Unencodable, try_encode

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

logger = logging.getLogger("griptape_nodes")


@dataclass(frozen=True)
class DroppedValue:
    """A parameter value, or default value, that was left out because it has no plain-data form."""

    node_name: str | None
    parameter_name: str
    reason: str
    is_default: bool = False


def unique_dropped_values(dropped_values: Iterable[DroppedValue]) -> list[DroppedValue]:
    """The values in first-seen order, so a node serialized twice is reported once."""
    return list(dict.fromkeys(dropped_values))


def keep_encodable_default(
    default_value: Any, node_name: str | None, parameter_name: str, dropped_values: list[DroppedValue]
) -> Any:
    """Return ``default_value``, or None if it has no plain-data form, which is logged and added to ``dropped_values``."""
    encoded = try_encode(default_value)
    if not isinstance(encoded, Unencodable):
        return default_value
    logger.warning(
        "Attempted to save the default value of parameter '%s' on node '%s'. Failed because %s "
        "The parameter will reopen without that default.",
        parameter_name,
        node_name,
        encoded.reason,
    )
    dropped_values.append(DroppedValue(node_name, parameter_name, encoded.reason, is_default=True))
    return None


def dropped_values_detail(dropped_values: Sequence[DroppedValue], action: str) -> ResultDetail:
    """A warning that names each left-out value, for an operation described by ``action``, such as "save the workflow"."""
    lines = [
        f"Attempted to {action}. Failed to keep these values because they have no plain-data form, so they were left out:"
    ]
    lines.extend(f"- {_describe(dropped_value)}" for dropped_value in dropped_values)
    return ResultDetail(level=logging.WARNING, message="\n".join(lines))


def success_details(
    message: str, dropped_values: Sequence[DroppedValue], action: str, *, level: int = logging.DEBUG
) -> ResultDetails:
    """``message`` at ``level``, followed by a warning for the left-out values if there are any."""
    details = [ResultDetail(level=level, message=message)]
    if dropped_values:
        details.append(dropped_values_detail(dropped_values, action))
    return ResultDetails(*details)


def _describe(dropped_value: DroppedValue) -> str:
    what = f"parameter '{dropped_value.parameter_name}'"
    if dropped_value.is_default:
        what = f"default of {what}"
    if dropped_value.node_name is not None:
        what = f"{what} on node '{dropped_value.node_name}'"
    return f"{what} ({dropped_value.reason})"
