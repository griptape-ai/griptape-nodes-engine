"""Checks for the ``role`` and ``media`` fields a library JSON declares on node metadata.

Both are checked when the library registers, from the manifest alone, so lazy node loading still
imports nothing. An unknown value is dropped from the node's metadata and reported as an
``InvalidNodeMetadataProblem``; the node itself still registers.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from griptape_nodes.node_library.library_registry import NODE_MEDIA, NODE_ROLES
from griptape_nodes.retained_mode.managers.fitness_problems.libraries import InvalidNodeMetadataProblem

if TYPE_CHECKING:
    from griptape_nodes.node_library.library_registry import LibrarySchema, NodeMetadata

logger = logging.getLogger("griptape_nodes")


def check_node_role_media_at_registration(library_data: LibrarySchema) -> list[InvalidNodeMetadataProblem]:
    """Drop unknown role and media values from each node's metadata, in place, returning a problem for each."""
    problems: list[InvalidNodeMetadataProblem] = []
    nodes: list[tuple[str, NodeMetadata]] = [(node.class_name, node.metadata) for node in library_data.nodes]
    nodes.extend((node.node_type, node.metadata) for node in library_data.workflow_nodes or [])
    for node_type, metadata in nodes:
        for reason in _sanitize(metadata):
            logger.warning("Library '%s' node '%s' metadata: %s.", library_data.name, node_type, reason)
            problems.append(InvalidNodeMetadataProblem(node_type=node_type, reason=reason))
    return problems


def _sanitize(metadata: NodeMetadata) -> list[str]:
    """Drop the unknown values from one node's metadata and return why each was dropped."""
    reasons: list[str] = []
    if metadata.role is not None and metadata.role not in NODE_ROLES:
        roles = ", ".join(f"'{role}'" for role in NODE_ROLES)
        reasons.append(f"role '{metadata.role}' was ignored because it isn't one of {roles}")
        metadata.role = None
    if metadata.media is not None:
        unknown = [value for value in metadata.media if value not in NODE_MEDIA]
        if unknown:
            unknown_list = ", ".join(f"'{value}'" for value in unknown)
            known = ", ".join(f"'{value}'" for value in NODE_MEDIA)
            noun = "value" if len(unknown) == 1 else "values"
            reasons.append(f"media {noun} {unknown_list} ignored because media must be from {known}")
        # Keep the declared order, without unknowns or repeats.
        kept = list(dict.fromkeys(value for value in metadata.media if value in NODE_MEDIA))
        metadata.media = kept or None
    return reasons
