"""The trimming DescribeNodeTypeRequest's include_* flags apply to a node type's parameter descriptions."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.events.library_events import ParameterDescription


def trim_parameters(
    parameters: list[ParameterDescription], *, include_tooltips: bool, include_ui_options: bool
) -> list[ParameterDescription]:
    """The descriptions with the parts a request left out set to None. `options` is always kept."""
    blanked: dict[str, Any] = {}
    if not include_tooltips:
        blanked.update(tooltip=None, tooltip_as_input=None, tooltip_as_property=None, tooltip_as_output=None)
    if not include_ui_options:
        blanked["ui_options"] = None
    if not blanked:
        return parameters
    return [dataclasses.replace(param, **blanked) for param in parameters]
