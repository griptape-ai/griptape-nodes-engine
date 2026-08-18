"""Core utilities for normalizing artifact inputs (images, videos, audio).

This module provides normalization functions that convert string paths to
their respective artifact types (ImageUrlArtifact, VideoUrlArtifact, AudioUrlArtifact).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from griptape_nodes.files.path_utils import parse_static_server_url
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

logger = logging.getLogger(__name__)


def _resolve_localhost_url_to_path(url: str) -> str:
    """Resolve localhost static file URLs to workspace file paths.

    Converts URLs like http://localhost:8124/workspace/static_files/file.jpg
    to actual workspace file paths like static_files/file.jpg

    Args:
        url: URL string that may be a localhost URL

    Returns:
        Resolved file path relative to workspace, or original string if not a localhost URL
    """
    if not isinstance(url, str):
        return url

    # `parse_static_server_url` needs a workspace to join against, but this function's
    # contract is to return a *workspace-relative* path (callers anchor it themselves via
    # `_resolve_file_path`). Passing an empty base makes the result the relative remainder.
    local_path = parse_static_server_url(url, Path())
    if local_path is None:
        return url
    return str(local_path)


def _resolve_file_path(file_path: str) -> Path | None:  # noqa: PLR0911
    """Resolve file path to absolute path relative to workspace.

    Args:
        file_path: File path (may be absolute, relative, or localhost URL)

    Returns:
        Resolved Path object, or None if path cannot be resolved
    """
    # First resolve localhost URLs
    file_path = _resolve_localhost_url_to_path(file_path)

    # Get workspace path (can raise exceptions from ConfigManager)
    try:
        workspace_path = GriptapeNodes.ConfigManager().workspace_path
    except (AttributeError, RuntimeError, KeyError) as e:
        logger.debug("Failed to get workspace path: %s", e)
        return None

    # Create Path object (Path() constructor doesn't raise exceptions, but we validate)
    path = Path(file_path)

    # Check if path is absolute (is_absolute() doesn't raise exceptions)
    if not path.is_absolute():
        # Relative path - resolve relative to workspace (path operations don't raise exceptions)
        return workspace_path / path

    # Absolute path - check if relative to workspace
    is_relative_to_workspace = False
    try:
        is_relative_to_workspace = path.is_relative_to(workspace_path)
    except (ValueError, AttributeError):
        # Path.is_relative_to() not available in older Python versions, use relative_to() instead
        try:
            path.relative_to(workspace_path)
            is_relative_to_workspace = True
        except ValueError:
            # Absolute path outside workspace
            is_relative_to_workspace = False
        except (OSError, RuntimeError) as e:
            # Unexpected errors from relative_to()
            logger.debug("Unexpected error calling relative_to() for '%s': %s", file_path, e)
            return None

    if is_relative_to_workspace:
        return path

    # Absolute path outside workspace - return as-is (might be a system path)
    # exists() can raise OSError or PermissionError
    try:
        path_exists = path.exists()
    except (OSError, PermissionError) as e:
        logger.debug("Failed to check if path exists for '%s': %s", file_path, e)
        return None

    if path_exists:
        return path

    return None


def _upload_file_to_static_storage(file_path: Path, artifact_type: type[Any]) -> Any | None:
    """Upload a file to static storage and return an artifact.

    Args:
        file_path: Path to the file to upload
        artifact_type: The artifact class to create (ImageUrlArtifact, VideoUrlArtifact, AudioUrlArtifact)

    Returns:
        Artifact object with localhost URL, or None if upload fails
    """
    if not file_path.exists() or not file_path.is_file():
        return None

    try:
        file_data = file_path.read_bytes()
        file_name = file_path.name
        static_files_manager = GriptapeNodes.StaticFilesManager()
        url = static_files_manager.save_static_file(file_data, file_name)
        return artifact_type(url)
    except Exception as e:
        logger.debug("Failed to upload file '%s' to static storage: %s", file_path, e)
        return None


def _normalize_string_input(artifact_input: str, artifact_type: type[Any]) -> Any:  # noqa: PLR0911
    """Normalize a string input to an artifact.

    Args:
        artifact_input: String input (URL or file path)
        artifact_type: The artifact class to create

    Returns:
        Artifact object or original input if normalization fails
    """
    # If it's already a URL (http/https), return it as-is
    if artifact_input.startswith(("http://", "https://")):
        # Check if it's a localhost URL that needs resolving
        if artifact_input.startswith(("http://localhost:", "https://localhost:")):
            resolved_path = _resolve_localhost_url_to_path(artifact_input)
            # If path wasn't resolved, return as URL artifact
            if resolved_path == artifact_input:
                return artifact_type(artifact_input)

            # Try to resolve and upload the resolved path
            file_path = _resolve_file_path(resolved_path)
            if not file_path:
                return artifact_type(artifact_input)

            artifact = _upload_file_to_static_storage(file_path, artifact_type)
            if not artifact:
                return artifact_type(artifact_input)

            # Success path: return the uploaded artifact
            return artifact
        # Regular URL, return as-is
        return artifact_type(artifact_input)

    # Try to resolve and upload file path
    file_path = _resolve_file_path(artifact_input)
    if file_path:
        artifact = _upload_file_to_static_storage(file_path, artifact_type)
        if artifact:
            return artifact

    return artifact_input


def normalize_artifact_input(
    artifact_input: Any,
    artifact_type: type[Any],
    *,
    accepted_types: tuple[type[Any], ...] | None = None,
) -> Any:
    """Normalize an artifact input, converting string paths to the specified artifact type.

    This ensures consistency whether values come from user input or node connections.
    String paths are uploaded to static storage and converted to artifact objects.
    Objects that are already the correct artifact type are returned unchanged.

    Args:
        artifact_input: Artifact input (may be string, artifact object, etc.)
        artifact_type: The artifact class to create (ImageUrlArtifact, VideoUrlArtifact, AudioUrlArtifact)
        accepted_types: Optional tuple of artifact types that should be passed through unchanged.
            For example, for images, both ImageUrlArtifact and ImageArtifact are valid.

    Returns:
        Artifact of the specified type if input was a string path, otherwise returns input unchanged
    """
    # Return unchanged if already the correct artifact type
    if isinstance(artifact_input, artifact_type):
        return artifact_input

    # Also return unchanged if it's one of the accepted types (e.g., ImageArtifact for images)
    if accepted_types and isinstance(artifact_input, accepted_types):
        return artifact_input

    # Process string paths
    if isinstance(artifact_input, str) and artifact_input:
        return _normalize_string_input(artifact_input, artifact_type)

    return artifact_input


def normalize_artifact_list(
    artifact_list: list[Any],
    artifact_type: type[Any],
    *,
    accepted_types: tuple[type[Any], ...] | None = None,
) -> list[Any]:
    """Normalize a list of artifact inputs, converting string paths to the specified artifact type.

    This ensures consistency whether values come from user input or node connections.
    String paths are uploaded to static storage and converted to artifact objects.
    Objects that are already the correct artifact type are passed through unchanged.

    Args:
        artifact_list: List of artifact inputs (may contain strings, artifact objects, etc.)
        artifact_type: The artifact class to create (ImageUrlArtifact, VideoUrlArtifact, AudioUrlArtifact)
        accepted_types: Optional tuple of artifact types that should be passed through unchanged.
            For example, for images, both ImageUrlArtifact and ImageArtifact are valid.

    Returns:
        List with string paths converted to artifacts of the specified type
    """
    if not artifact_list:
        return artifact_list

    normalized_list = []
    for item in artifact_list:
        normalized_item = normalize_artifact_input(item, artifact_type, accepted_types=accepted_types)
        normalized_list.append(normalized_item)
    return normalized_list


__all__ = ["normalize_artifact_input", "normalize_artifact_list"]
