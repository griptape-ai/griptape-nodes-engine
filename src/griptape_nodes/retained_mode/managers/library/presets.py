"""Checks for the presets a library JSON declares.

A preset is checked in two passes. When the library registers, its node must be one the library
JSON declares (for presets of the same library) and its role must be one of ``PRESET_ROLES``; that
needs nothing but the manifest. Its params are checked the first time presets are listed, because
the node's parameters only exist once the node class is imported and constructed, and lazy node
loading puts off that import until a node is first needed.

A preset that fails either pass is dropped and reported as an ``InvalidPresetProblem``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from griptape_nodes.node_library.library_registry import PRESET_ROLES, PresetDefinition
from griptape_nodes.retained_mode.managers.fitness_problems.libraries import InvalidPresetProblem

if TYPE_CHECKING:
    from collections.abc import Collection

    from griptape_nodes.node_library.library_registry import LibrarySchema

logger = logging.getLogger("griptape_nodes")


@dataclass
class PresetCheckResult:
    """The presets that passed a check, and a problem for each one that didn't."""

    accepted: list[PresetDefinition] = field(default_factory=list)
    problems: list[InvalidPresetProblem] = field(default_factory=list)

    def reject(self, library_name: str, preset: PresetDefinition, reason: str) -> None:
        """Drop a preset, logging it and recording a problem for it."""
        logger.warning("Library '%s' preset '%s' was skipped because %s.", library_name, preset.name, reason)
        self.problems.append(InvalidPresetProblem(preset_name=preset.name, reason=reason))


def check_presets_at_registration(library_data: LibrarySchema) -> PresetCheckResult:
    """Check each declared preset's role and, for a same-library preset, that its node is declared."""
    result = PresetCheckResult()
    declared_node_types = _declared_node_types(library_data)
    for preset in library_data.presets or []:
        reason = _registration_rejection_reason(library_data.name, preset, declared_node_types)
        if reason is not None:
            result.reject(library_data.name, preset, reason)
            continue
        result.accepted.append(preset)
    return result


def preset_param_rejection_reason(preset: PresetDefinition, parameter_names: Collection[str]) -> str | None:
    """Return why the preset's params don't fit its node's parameters, or None if they all do."""
    unknown = sorted(name for name in preset.params if name not in parameter_names)
    if not unknown:
        return None
    unknown_list = ", ".join(f"'{name}'" for name in unknown)
    if len(unknown) == 1:
        return f"node type '{preset.node}' has no parameter named {unknown_list}"
    return f"node type '{preset.node}' has no parameters named {unknown_list}"


def _is_same_library_preset(library_name: str, preset: PresetDefinition) -> bool:
    return preset.library is None or preset.library == library_name


def _registration_rejection_reason(
    library_name: str, preset: PresetDefinition, declared_node_types: Collection[str]
) -> str | None:
    if preset.role not in PRESET_ROLES:
        roles = ", ".join(f"'{role}'" for role in PRESET_ROLES)
        return f"its role '{preset.role}' isn't one of {roles}"
    if _is_same_library_preset(library_name, preset) and preset.node not in declared_node_types:
        return f"the library has no node type '{preset.node}'"
    return None


def _declared_node_types(library_data: LibrarySchema) -> set[str]:
    node_types = {node.class_name for node in library_data.nodes}
    node_types.update(workflow_node.node_type for workflow_node in library_data.workflow_nodes or [])
    return node_types
