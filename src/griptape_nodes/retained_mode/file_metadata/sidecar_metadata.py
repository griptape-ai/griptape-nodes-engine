"""Sidecar metadata file creation for files written through the retained mode API.

When a file is saved, a sidecar JSON file is written to the project's metadata
directory (`.griptape-nodes-metadata/`) with preserved path hierarchy. The sidecar captures
caller-provided project context (situation name, macro template, variable values)
merged with auto-collected workflow metadata (workflow name, flow context, node
parameters).

Example layout (for a file at <workspace>/outputs/image.png):
    .griptape-nodes-metadata/
      outputs/
        image.png.json
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel

from griptape_nodes.common.macro_parser import ParsedMacro
from griptape_nodes.common.project_templates.situation import BuiltInSituation, SituationFilePolicy
from griptape_nodes.files.path_utils import decompose_source_path
from griptape_nodes.retained_mode.events.project_events import (
    GetCurrentProjectRequest,
    GetCurrentProjectResultSuccess,
    GetPathForMacroRequest,
    GetPathForMacroResultSuccess,
    GetSituationRequest,
    GetSituationResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

if TYPE_CHECKING:
    from pathlib import Path


logger = logging.getLogger("griptape_nodes")

SCHEMA_VERSION = "0.1.0"


class SituationPolicy(BaseModel):
    """File collision and directory creation policy from the situation template."""

    on_collision: SituationFilePolicy | None = None
    create_dirs: bool | None = None


class SituationMetadata(BaseModel):
    """Situation context captured at save time."""

    name: str | None = None
    macro: str | None = None
    policy: SituationPolicy | None = None
    variables: dict[str, str] | None = None


class SidecarContent(BaseModel):
    """Caller-provided context written to the sidecar JSON file alongside saved files."""

    situation: SituationMetadata | None = None


def _resolve_sidecar_path(file_path: Path) -> Path:
    """Resolve the sidecar path for a given file via the project template system.

    Uses the 'save_griptape_nodes_metadata' situation from the current project template to determine
    where the sidecar JSON file should be written, preserving directory hierarchy
    relative to the project workspace.

    Args:
        file_path: Absolute path to the saved file.

    Returns:
        Absolute path to the sidecar JSON file.

    Raises:
        RuntimeError: If project not loaded, situation not found, or path resolution fails.
    """
    get_project_result = GriptapeNodes.handle_request(GetCurrentProjectRequest())
    if not isinstance(get_project_result, GetCurrentProjectResultSuccess):
        msg = "No current project loaded"
        raise RuntimeError(msg)  # noqa: TRY004

    workspace_dir = get_project_result.project_info.project_base_dir
    decomposed = decompose_source_path(file_path, workspace_dir)

    get_situation_result = GriptapeNodes.handle_request(
        GetSituationRequest(situation_name=BuiltInSituation.SAVE_GRIPTAPE_NODES_METADATA)
    )
    if not isinstance(get_situation_result, GetSituationResultSuccess):
        msg = f"{BuiltInSituation.SAVE_GRIPTAPE_NODES_METADATA} situation not found in project template"
        raise RuntimeError(msg)  # noqa: TRY004

    variables: dict[str, str | int] = {"source_file_name": decomposed.source_file_name}
    if decomposed.source_relative_path:
        variables["source_relative_path"] = decomposed.source_relative_path

    situation = get_situation_result.situation
    parsed_macro = ParsedMacro(situation.macro)
    path_result = GriptapeNodes.handle_request(
        GetPathForMacroRequest(
            parsed_macro=parsed_macro,
            variables=variables,
        )
    )
    if not isinstance(path_result, GetPathForMacroResultSuccess):
        msg = f"Failed to resolve sidecar path macro: {path_result.result_details}"
        raise RuntimeError(msg)  # noqa: TRY004

    return path_result.absolute_path


def write_sidecar(file_path: Path, metadata: SidecarContent | None) -> None:
    """Write a sidecar JSON metadata file for the saved file.

    Resolves the sidecar path via the project template's 'save_griptape_nodes_metadata' situation,
    placing the file in the project's centralized metadata directory with preserved
    path hierarchy. Best-effort: failures are logged as warnings and never propagated
    to callers.

    Args:
        file_path: Absolute path to the file that was just saved.
        metadata: Caller-provided situation and variable context (may be None).
    """
    try:
        sidecar_path = _resolve_sidecar_path(file_path)
        content = metadata or SidecarContent()
        output = {
            "schema_version": SCHEMA_VERSION,
            "saved_at": datetime.now(UTC).isoformat(),
            **content.model_dump(exclude_none=True),
        }
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        sidecar_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("Failed to write sidecar metadata for '%s': %s", file_path, e)
