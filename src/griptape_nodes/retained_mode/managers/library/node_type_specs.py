"""The cache of node type parameter descriptions that DescribeNodeTypeRequest reads, and the trimming it applies."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from griptape_nodes.node_library.library_registry import LibraryRegistry

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.events.library_events import ParameterDescription


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


def trim_parameters(
    parameters: list[ParameterDescription], *, include_tooltips: bool, include_ui_options: bool
) -> list[ParameterDescription]:
    """The descriptions with the parts a request left out set to None. `options` is always kept."""
    blanked: dict[str, None] = {}
    if not include_tooltips:
        blanked.update(tooltip=None, tooltip_as_input=None, tooltip_as_property=None, tooltip_as_output=None)
    if not include_ui_options:
        blanked["ui_options"] = None
    if not blanked:
        return parameters
    return [dataclasses.replace(param, **blanked) for param in parameters]
