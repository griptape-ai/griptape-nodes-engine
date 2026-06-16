"""Library-specific utilities for managing node libraries."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, NamedTuple

from pydantic import ValidationError
from xdg_base_dirs import xdg_data_home

from griptape_nodes.retained_mode.managers.settings import LibraryDownload, LibraryRegistration
from griptape_nodes.utils.file_utils import find_all_files_in_directory
from griptape_nodes.utils.git_utils import (
    get_git_repository_root,
    sparse_checkout_library_json,
)

logger = logging.getLogger(__name__)


class LibraryVersionInfo(NamedTuple):
    """Library version information from git repository."""

    library_version: str
    commit_sha: str
    engine_version: str


# Mapping of old XDG library names to their git URLs
LIBRARY_GIT_URLS = {
    "griptape_nodes_library": "https://github.com/griptape-ai/griptape-nodes-library-standard@stable",
    "griptape_nodes_advanced_media_library": "https://github.com/griptape-ai/griptape-nodes-library-advanced-media@stable",
    "griptape_cloud": "https://github.com/griptape-ai/griptape-nodes-library-griptape-cloud@stable",
}


def is_monorepo(library_path: Path) -> bool:
    """Check if a library is in a monorepo (git repository with multiple library JSON files).

    Args:
        library_path: The path to the library directory.

    Returns:
        bool: True if the git repository contains multiple library JSON files, False otherwise.
    """
    # Get the git repository root
    repo_root = get_git_repository_root(library_path)
    if repo_root is None:
        return False

    # Search for all library JSON files in the repository
    library_json_files = find_all_files_in_directory(repo_root, "griptape[-_]nodes[-_]library.json")

    # Monorepo if more than 1 library JSON file exists
    return len(library_json_files) > 1


def clone_and_get_library_version(remote_url: str, ref: str = "HEAD") -> LibraryVersionInfo:
    """Fetch library version, commit SHA, and engine version using sparse checkout for efficiency.

    Uses sparse checkout to download only the library JSON file instead of the entire repository,
    significantly reducing bandwidth and time for update checks.

    Args:
        remote_url: The git remote URL (HTTPS or SSH).
        ref: The git reference (branch, tag, or commit SHA) to check. Defaults to "HEAD".

    Returns:
        LibraryVersionInfo: Library version, commit SHA, and engine version from the repository.

    Raises:
        GitCloneError: If sparse checkout fails or library metadata is invalid.
    """
    library_version, commit_sha, library_data = sparse_checkout_library_json(remote_url, ref=ref)
    engine_version = library_data.get("metadata", {}).get("engine_version", "")
    return LibraryVersionInfo(library_version=library_version, commit_sha=commit_sha, engine_version=engine_version)


def filter_old_xdg_library_paths(library_paths: list[str]) -> tuple[list[str], set[str]]:
    """Filter out old XDG library paths from a list of library paths.

    Removes library paths that were stored in the deprecated XDG data home location
    (~/.local/share/griptape_nodes/libraries/) for the following libraries:
    - griptape_nodes_library
    - griptape_nodes_advanced_media_library
    - griptape_cloud

    Args:
        library_paths: List of library paths to filter.

    Returns:
        Tuple of (filtered_list, set of removed library names).
    """
    if not library_paths:
        return library_paths, set()

    # Build list of old XDG path prefixes to remove
    xdg_libraries_base = xdg_data_home() / "griptape_nodes" / "libraries"
    old_library_names = [
        "griptape_nodes_library",
        "griptape_nodes_advanced_media_library",
        "griptape_cloud",
    ]

    old_path_prefixes = {lib_name: str(xdg_libraries_base / lib_name) for lib_name in old_library_names}

    # Filter and track which libraries were removed
    filtered_libraries = []
    removed_library_names = set()

    for library in library_paths:
        is_old_path = False
        # Normalize library path for cross-platform comparison
        normalized_library = str(Path(library))

        for lib_name, prefix in old_path_prefixes.items():
            if normalized_library.startswith(prefix):
                is_old_path = True
                removed_library_names.add(lib_name)
                break

        if not is_old_path:
            filtered_libraries.append(library)

    return filtered_libraries, removed_library_names


def extract_library_path(entry: Any) -> str:
    """Extract the path string from a libraries_to_register entry.

    Accepts a bare path string, a dict-shaped entry from raw config, or an already
    parsed LibraryRegistration. Returns an empty string for entries that contain no
    usable path.
    """
    if isinstance(entry, str):
        return entry
    if isinstance(entry, LibraryRegistration):
        return entry.path if entry.path is not None else ""
    if isinstance(entry, dict):
        path = entry.get("path")
        return path if isinstance(path, str) else ""
    return ""


def normalize_library_registrations(raw: list[Any]) -> list[LibraryRegistration]:
    """Normalize a libraries_to_register config list into LibraryRegistration entries.

    Bare strings become enabled entries. Dict-shaped entries are validated against
    the LibraryRegistration schema. Already parsed LibraryRegistration instances pass
    through unchanged. Malformed entries are skipped with a warning so a single bad
    entry cannot block startup.
    """
    entries: list[LibraryRegistration] = []
    for item in raw:
        if isinstance(item, LibraryRegistration):
            entries.append(item)
        elif isinstance(item, str):
            if item:
                entries.append(LibraryRegistration(path=item))
        elif isinstance(item, dict):
            try:
                entries.append(LibraryRegistration.model_validate(item))
            except ValidationError as err:
                logger.warning("Skipping malformed libraries_to_register entry %r: %s", item, err)
        else:
            logger.warning(
                "Skipping libraries_to_register entry of unexpected type %s: %r",
                type(item).__name__,
                item,
            )
    return entries


def normalize_library_downloads(raw: list[Any]) -> list[LibraryDownload]:
    """Normalize a libraries_to_download config list into LibraryDownload entries.

    Bare strings become git-URL-only entries. Dict-shaped entries are validated
    against the LibraryDownload schema. Already parsed LibraryDownload instances
    pass through unchanged. Malformed entries are skipped with a warning so a
    single bad entry cannot block startup.
    """
    entries: list[LibraryDownload] = []
    for item in raw:
        if isinstance(item, LibraryDownload):
            entries.append(item)
        elif isinstance(item, str):
            if item:
                entries.append(LibraryDownload(git_url=item))
        elif isinstance(item, dict):
            try:
                entries.append(LibraryDownload.model_validate(item))
            except ValidationError as err:
                logger.warning("Skipping malformed libraries_to_download entry %r: %s", item, err)
        else:
            logger.warning(
                "Skipping libraries_to_download entry of unexpected type %s: %r",
                type(item).__name__,
                item,
            )
    return entries
