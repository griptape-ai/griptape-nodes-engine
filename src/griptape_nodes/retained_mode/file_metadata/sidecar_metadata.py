"""Sidecar metadata file creation for files written through the retained mode API.

When a file is saved, a sidecar JSON file is written to the project's metadata
directory (`.griptape-nodes-metadata/`) with preserved path hierarchy. The sidecar
captures the situation that triggered the save (name, macro, policy, variables) plus
auto-collected workflow provenance (workflow name and dates, flow name, resolving node
name, and node parameter values, with parameters marked exclude_from_metadata=True omitted).

Example layout (for a file at <workspace>/outputs/image.png):
    .griptape-nodes-metadata/
      outputs/
        image.png.json
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

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
from griptape_nodes.retained_mode.file_metadata.workflow_metadata import collect_sidecar_provenance

if TYPE_CHECKING:
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine


logger = logging.getLogger("griptape_nodes")

SCHEMA_VERSION = "0.2.0"


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


class WorkflowInfo(BaseModel):
    """Workflow-level provenance captured at save time."""

    name: str | None = None
    created: str | None = None
    modified: str | None = None
    engine_version: str | None = None
    description: str | None = None


class FlowInfo(BaseModel):
    """Flow and node provenance captured at save time."""

    name: str | None = None
    node_name: str | None = None


class SidecarContent(BaseModel):
    """Context written to the sidecar JSON file alongside saved files."""

    situation: SituationMetadata | None = None
    workflow: WorkflowInfo | None = None
    flow: FlowInfo | None = None
    parameters: dict[str, Any] | None = None
    parameters_omitted: list[str] | None = None


def _resolve_sidecar_path(file_path: Path, engine: Engine) -> Path:
    """Resolve the sidecar path for a given file via the project template system.

    Uses the 'save_griptape_nodes_metadata' situation from the current project template to determine
    where the sidecar JSON file should be written, preserving directory hierarchy
    relative to the project workspace.

    Args:
        file_path: Absolute path to the saved file.
        engine: The engine whose request bus resolves the project and situation.

    Returns:
        Absolute path to the sidecar JSON file.

    Raises:
        RuntimeError: If project not loaded, situation not found, or path resolution fails.
    """
    get_project_result = engine.handle_request(GetCurrentProjectRequest())
    if not isinstance(get_project_result, GetCurrentProjectResultSuccess):
        msg = "No current project loaded"
        raise RuntimeError(msg)  # noqa: TRY004

    workspace_dir = get_project_result.project_info.project_base_dir
    decomposed = decompose_source_path(file_path, workspace_dir)

    get_situation_result = engine.handle_request(
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
    path_result = engine.handle_request(
        GetPathForMacroRequest(
            parsed_macro=parsed_macro,
            variables=variables,
        )
    )
    if not isinstance(path_result, GetPathForMacroResultSuccess):
        msg = f"Failed to resolve sidecar path macro: {path_result.result_details}"
        raise RuntimeError(msg)  # noqa: TRY004

    return path_result.absolute_path


def write_sidecar(file_path: Path, metadata: SidecarContent | None, engine: Engine) -> None:
    """Write a sidecar JSON metadata file for the saved file.

    Resolves the sidecar path via the project template's 'save_griptape_nodes_metadata' situation,
    placing the file in the project's centralized metadata directory with preserved
    path hierarchy. Best-effort: failures are logged as warnings and never propagated
    to callers.

    Args:
        file_path: Absolute path to the file that was just saved.
        metadata: Caller-provided situation and variable context (may be None).
        engine: The engine whose request bus resolves the sidecar path.
    """
    try:
        sidecar_path = _resolve_sidecar_path(file_path, engine)
        base = metadata or SidecarContent()
        provenance = collect_sidecar_provenance(engine)
        content = SidecarContent(
            situation=base.situation,
            workflow=WorkflowInfo(**provenance["workflow"]) if "workflow" in provenance else None,
            flow=FlowInfo(**provenance["flow"]) if "flow" in provenance else None,
            parameters=provenance.get("parameters"),
            parameters_omitted=provenance.get("parameters_omitted"),
        )
        output = {
            "schema_version": SCHEMA_VERSION,
            "saved_at": datetime.now(UTC).isoformat(),
            **content.model_dump(exclude_none=True),
        }
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        sidecar_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("Failed to write sidecar metadata for '%s': %s", file_path, e)
