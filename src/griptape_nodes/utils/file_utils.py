"""Utilities for file and directory operations."""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

import anyio

logger = logging.getLogger(__name__)

# Default ceiling on how deep recursive discovery walks. Bounds boot-time scans
# against pathologically deep trees and symlink loops without a visited-set.
DEFAULT_MAX_SEARCH_DEPTH = 10


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically.

    Writes to a temp file in the same directory and renames it into place via
    ``Path.replace`` (an atomic rename on the same filesystem), so a crash
    mid-write leaves the previous file intact rather than a truncated one. The
    temp file is removed if the write or rename fails.

    Args:
        path: Destination file path. Its parent directory must already exist.
        data: Bytes to write.
    """
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "wb") as tmp_file:
            tmp_file.write(data)
        tmp_path.replace(path)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


def find_file_in_directory(directory: Path, pattern: str) -> Path | None:
    """Search directory recursively for a file matching the given pattern.

    Args:
        directory: Directory to search in
        pattern: Glob pattern to match files against (e.g., '*.json', '*library*.json')

    Returns:
        Path to the first matching file if found, None otherwise.
        Logs a warning if multiple files match the pattern.

    Examples:
        >>> find_file_in_directory(Path("/workspace"), "config.json")
        Path("/workspace/subdir/config.json")
        >>> find_file_in_directory(Path("/workspace"), "*library*.json")
        Path("/workspace/libs/my_library.json")
        >>> find_file_in_directory(Path("/empty"), "missing.txt")
        None
    """
    if not directory.exists():
        logger.debug("Directory does not exist: %s", directory)
        return None

    if not directory.is_dir():
        logger.debug("Path is not a directory: %s", directory)
        return None

    matches = []
    for root, _, files_found in os.walk(directory):
        for file in files_found:
            if fnmatch(file, pattern):
                found_path = Path(root) / file
                matches.append(found_path)

    if not matches:
        logger.debug("No files matching pattern '%s' found in directory: %s", pattern, directory)
        return None

    if len(matches) > 1:
        for _match in matches:
            pass
        logger.warning(
            "Found multiple files matching pattern '%s' in %s, using first one at %s",
            pattern,
            directory,
            matches[0],
        )

    logger.debug("Found file matching pattern '%s' at: %s", pattern, matches[0])
    return matches[0]


def find_all_files_in_directory(directory: Path, pattern: str) -> list[Path]:
    """Search directory recursively for all files matching the given pattern.

    Args:
        directory: Directory to search in
        pattern: Glob pattern to match files against (e.g., '*.json', '*library*.json')

    Returns:
        List of all matching file paths. Returns empty list if none found.

    Examples:
        >>> find_all_files_in_directory(Path("/workspace"), "*.json")
        [Path("/workspace/a.json"), Path("/workspace/sub/b.json")]
        >>> find_all_files_in_directory(Path("/empty"), "*.txt")
        []
    """
    if not directory.exists():
        logger.debug("Directory does not exist: %s", directory)
        return []

    if not directory.is_dir():
        logger.debug("Path is not a directory: %s", directory)
        return []

    matches = []
    for root, _, files_found in os.walk(directory):
        for file in files_found:
            if fnmatch(file, pattern):
                found_path = Path(root) / file
                matches.append(found_path)

    if not matches:
        logger.debug("No files matching pattern '%s' found in directory: %s", pattern, directory)
    else:
        logger.debug("Found %d file(s) matching pattern '%s' in directory: %s", len(matches), pattern, directory)

    return matches


def find_files_recursive(directory: Path, pattern: str, *, skip_hidden: bool = True) -> list[Path]:
    """Search directory recursively for files matching pattern.

    Args:
        directory: Directory to search in
        pattern: Glob pattern to match files against (e.g., '*.json', '*library*.json')
        skip_hidden: If True, skip hidden directories (those starting with .). Default is True.
            This is more efficient when dealing with large hidden directories like .git, .venv, etc.

    Returns:
        Sorted list of all matching file paths. Returns empty list if none found.

    Examples:
        >>> find_files_recursive(Path("/workspace"), "*.json")
        [Path("/workspace/a.json"), Path("/workspace/sub/b.json")]
        >>> find_files_recursive(Path("/workspace"), "*.json", skip_hidden=False)
        [Path("/workspace/.config/b.json"), Path("/workspace/a.json")]
        >>> find_files_recursive(Path("/empty"), "*.txt")
        []
    """
    if not directory.exists():
        logger.debug("Directory does not exist: %s", directory)
        return []

    if not directory.is_dir():
        logger.debug("Path is not a directory: %s", directory)
        return []

    def _recurse(path: Path) -> list[Path]:
        """Recursively find files."""
        results = []
        try:
            for item in sorted(path.iterdir()):
                # Skip hidden files/directories if requested
                if skip_hidden and item.name.startswith("."):
                    continue

                if item.is_file() and fnmatch(item.name, pattern):
                    results.append(item)
                elif item.is_dir():
                    # Recurse into directories
                    results.extend(_recurse(item))
        except (PermissionError, OSError) as e:
            # Skip directories we can't access
            logger.debug("Cannot access directory %s: %s", path, e)

        return results

    matches = _recurse(directory)

    if not matches:
        logger.debug("No files matching pattern '%s' found in directory: %s", pattern, directory)
    else:
        logger.debug("Found %d file(s) matching pattern '%s' in directory: %s", len(matches), pattern, directory)

    return sorted(matches)


@dataclass
class _AsyncWalkParams:
    """Immutable walk settings shared across recursion levels of the async finder."""

    pattern: str
    skip_hidden: bool
    max_depth: int
    max_files: int | None
    matches: list[Path]


async def _arecurse_find(path: anyio.Path, depth: int, params: _AsyncWalkParams) -> None:
    """Depth-bounded async walk that appends matching files into ``params.matches``.

    Manual recursion via iterdir, because anyio.Path.rglob cannot express a
    max_depth limit.
    """
    try:
        entries = [entry async for entry in path.iterdir()]
    except (PermissionError, OSError) as e:
        logger.debug("Cannot access directory %s: %s", path, e)
        return

    for item in sorted(entries):
        if params.max_files is not None and len(params.matches) >= params.max_files:
            return
        if params.skip_hidden and item.name.startswith("."):
            continue

        # is_file/is_dir stat the entry, which can raise on protected paths
        # (e.g. macOS system caches). Skip the offending entry rather than
        # aborting the whole directory.
        try:
            item_is_file = await item.is_file()
            item_is_dir = await item.is_dir()
        except (PermissionError, OSError) as e:
            logger.debug("Cannot access entry %s: %s", item, e)
            continue

        if item_is_file:
            if fnmatch(item.name, params.pattern):
                params.matches.append(Path(item))
        elif item_is_dir and depth < params.max_depth:
            await _arecurse_find(item, depth + 1, params)


async def afind_files_recursive(
    directory: Path,
    pattern: str,
    *,
    skip_hidden: bool = True,
    max_depth: int = DEFAULT_MAX_SEARCH_DEPTH,
    max_files: int | None = None,
) -> list[Path]:
    """Asynchronously search directory recursively for files matching pattern.

    Async, depth-bounded counterpart to find_files_recursive, suitable for the
    engine boot path: it walks via anyio so it yields to the event loop instead
    of blocking it, and max_depth bounds recursion so a pathologically deep tree
    or symlink loop can't stall startup.

    Args:
        directory: Directory to search in
        pattern: Glob pattern to match file names against (e.g., '*.json')
        skip_hidden: If True, skip hidden directories (those starting with .).
            This avoids descending into large hidden trees like .git or .venv.
        max_depth: Maximum directory depth to descend. 0 scans only the top-level
            directory; each nested level adds 1. Defaults to DEFAULT_MAX_SEARCH_DEPTH.
        max_files: If set, stop and return as soon as this many matches are found.

    Returns:
        Sorted list of matching file paths. Returns empty list if none found.
    """
    if not await anyio.Path(directory).exists():
        logger.debug("Directory does not exist: %s", directory)
        return []

    if not await anyio.Path(directory).is_dir():
        logger.debug("Path is not a directory: %s", directory)
        return []

    matches: list[Path] = []
    params = _AsyncWalkParams(
        pattern=pattern,
        skip_hidden=skip_hidden,
        max_depth=max_depth,
        max_files=max_files,
        matches=matches,
    )
    await _arecurse_find(anyio.Path(directory), 0, params)

    if not matches:
        logger.debug("No files matching pattern '%s' found in directory: %s", pattern, directory)
    else:
        logger.debug("Found %d file(s) matching pattern '%s' in directory: %s", len(matches), pattern, directory)

    if max_files is not None:
        return sorted(matches)[:max_files]
    return sorted(matches)
