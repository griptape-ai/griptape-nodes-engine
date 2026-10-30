"""The cache of node type parameter descriptions that DescribeNodeTypeRequest reads, and the trimming it applies."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from griptape_nodes.exe_types.core_types import ParameterTypeBuiltin
from griptape_nodes.node_library.library_registry import LibraryRegistry

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.events.library_events import ParameterDescription


# The standard control parameters nearly every node has. A request can leave them out and report
# `exec_pins` instead; other control parameters (loop signals, branches, failure) stay listed.
EXEC_IN_NAME = "exec_in"
EXEC_OUT_NAME = "exec_out"


class NodeTypeSpecCache:
    """Parameter descriptions per node type, kept until any library or node type registration changes.

    Descriptions are keyed by library and node type. The whole cache is dropped as soon as
    `LibraryRegistry.get_generation` moves, which every load, reload, unload and sandbox
    re-registration does, so a description is never served for a node class that is no longer
    registered.
    """

    def __init__(self) -> None:
        self._generation = LibraryRegistry.get_generation()
        self._specs: dict[str, dict[str, list[ParameterDescription]]] = {}

    def get(self, library_name: str, node_type: str) -> list[ParameterDescription] | None:
        """The cached descriptions of a node type, or None when there are none for the current registrations."""
        self._drop_if_registrations_changed()
        return self._specs.get(library_name, {}).get(node_type)

    def put(self, library_name: str, node_type: str, specs: list[ParameterDescription], *, generation: int) -> None:
        """Keep a node type's parameter descriptions, built while the registry was at `generation`.

        Descriptions built before a registration change are discarded rather than kept, since they
        may describe a node class that has since been replaced.

        Args:
            library_name: Library that provides the node type
            node_type: Name of the node type
            specs: The node type's parameter descriptions
            generation: `LibraryRegistry.get_generation()` read before the descriptions were built
        """
        self._drop_if_registrations_changed()
        if generation != self._generation:
            return
        self._specs.setdefault(library_name, {})[node_type] = specs

    def _drop_if_registrations_changed(self) -> None:
        current_generation = LibraryRegistry.get_generation()
        if current_generation == self._generation:
            return
        self._specs.clear()
        self._generation = current_generation


@dataclass
class TrimmedParameters:
    """What a DescribeNodeTypeRequest returns about a node type's parameters.

    Args:
        parameters: The descriptions, with the parts the request left out set to None
        exec_pins: Whether the standard exec_in/exec_out pair was there and left out. None when the
            request included them.
    """

    parameters: list[ParameterDescription]
    exec_pins: bool | None


def trim_parameters(
    parameters: list[ParameterDescription],
    *,
    include_tooltips: bool,
    include_ui_options: bool,
    include_exec_pins: bool,
) -> TrimmedParameters:
    """Apply a request's include_* flags to a node type's descriptions. `options` is always kept.

    A request that leaves anything out gets descriptions that leave their null fields out on the wire.
    """
    if include_tooltips and include_ui_options and include_exec_pins:
        return TrimmedParameters(parameters=parameters, exec_pins=None)

    blanked: dict[str, Any] = {"omit_null_fields": True}
    if not include_tooltips:
        blanked.update(tooltip=None, tooltip_as_input=None, tooltip_as_property=None, tooltip_as_output=None)
    if not include_ui_options:
        blanked["ui_options"] = None

    exec_pins = None
    if not include_exec_pins:
        exec_pins = _has_standard_exec_pins(parameters)
    kept = [
        dataclasses.replace(param, **blanked)
        for param in parameters
        if not (exec_pins and _is_standard_exec_pin(param))
    ]
    return TrimmedParameters(parameters=kept, exec_pins=exec_pins)


def _has_standard_exec_pins(parameters: list[ParameterDescription]) -> bool:
    """Whether both standard control parameters are there. With only one, it stays listed."""
    names = {param.name for param in parameters if _is_standard_exec_pin(param)}
    return names == {EXEC_IN_NAME, EXEC_OUT_NAME}


def _is_standard_exec_pin(param: ParameterDescription) -> bool:
    return param.type == ParameterTypeBuiltin.CONTROL_TYPE.value and param.name in (EXEC_IN_NAME, EXEC_OUT_NAME)
