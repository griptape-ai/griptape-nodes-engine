"""Utilities for file and directory operations."""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from fnmatch import fnmatch
from functools import partial
from pathlib import Path

import anyio
import anyio.to_thread

logger = logging.getLogger(__name__)

# Fallback ceiling on how deep recursive discovery walks when a caller has no
# operator-configured value of its own. Bounds boot-time scans against
# pathologically deep trees and symlink loops without a visited-set.
DEFAULT_MAX_SEARCH_DEPTH = 5


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


@dataclass
class _AsyncWalkParams:
    """Immutable walk settings shared across recursion levels of the async finder."""

    pattern: str
    skip_hidden: bool
    max_depth: int
    max_files: int | None
    matches: list[Path]


@dataclass(frozen=True)
class _ScannedEntry:
    """One directory entry, classified while its ``scandir`` iterator was still open.

    A ``DirEntry``'s cached stat is tied to the ``scandir`` iterator that produced it,
    so it cannot outlive the ``with`` block. Copying the three fields the walk needs
    lets the classification happen in the worker thread while the recursion stays on
    the event loop.
    """

    name: str
    path: str
    is_file: bool
    is_dir: bool


def _scan_directory(path: Path, *, skip_hidden: bool) -> list[_ScannedEntry]:
    """List one directory, classifying each entry, for a single thread hop.

    Uses os.scandir rather than Path.iterdir + is_file/is_dir: scandir carries the
    entry type along with the directory read, so classifying an entry costs no extra
    stat call. Hidden entries are dropped here so they are never classified.

    Symlinked directories are reported as directories (matching the DirEntry.is_dir
    default), because a workspace may reach its libraries or workflows through a link.
    """
    entries = []
    with os.scandir(path) as scan:
        for entry in scan:
            if skip_hidden and entry.name.startswith("."):
                continue

            # is_file/is_dir may still stat when the entry is a symlink, which can raise
            # on protected paths (e.g. macOS system caches). Skip the offending entry
            # rather than aborting the whole directory.
            try:
                entry_is_file = entry.is_file()
                entry_is_dir = entry.is_dir()
            except (PermissionError, OSError) as e:
                logger.debug("Cannot access entry %s: %s", entry.path, e)
                continue

            entries.append(_ScannedEntry(name=entry.name, path=entry.path, is_file=entry_is_file, is_dir=entry_is_dir))

    return sorted(entries, key=lambda entry: entry.name)


async def _arecurse_find(path: Path, depth: int, params: _AsyncWalkParams) -> None:
    """Depth-bounded async walk that appends matching files into ``params.matches``.

    Offloads one thread hop per directory rather than per entry. The per-entry variant
    (anyio.Path.iterdir plus an await pair per entry) dispatched two hops for every
    entry, which dominated boot-time discovery on large trees; hopping per directory
    recovers nearly all of that while keeping the walk a coroutine, so a caller can
    still bound discovery with a timeout and cancellation lands at a directory edge.

    max_depth is what bounds a symlink loop, so there is no visited-set.
    """
    # abandon_on_cancel: a cancelled await returns immediately instead of waiting for the
    # in-flight scandir, which is what makes the timeout above a real bound -- on a hung
    # mount, waiting for the thread would leave discovery unbounded. Safe here because the
    # scan is read-only; the shielding in async_utils.to_thread exists to protect a
    # partially-completed write, which this is not. run_sync is positional-only, hence the
    # partial for the keyword argument.
    try:
        entries = await anyio.to_thread.run_sync(
            partial(_scan_directory, path, skip_hidden=params.skip_hidden),
            abandon_on_cancel=True,
        )
    except (PermissionError, OSError) as e:
        logger.debug("Cannot access directory %s: %s", path, e)
        return

    for item in entries:
        if params.max_files is not None and len(params.matches) >= params.max_files:
            return

        if item.is_file:
            if fnmatch(item.name, params.pattern):
                params.matches.append(Path(item.path))
        elif item.is_dir and depth < params.max_depth:
            await _arecurse_find(Path(item.path), depth + 1, params)


async def find_files_recursive(
    directory: Path,
    pattern: str,
    *,
    max_depth: int,
    skip_hidden: bool = True,
    max_files: int | None = None,
) -> list[Path]:
    """Asynchronously search directory recursively for files matching pattern.

    Depth-bounded async finder suitable for the engine boot path: each directory read is
    offloaded to a worker thread so it never blocks the event loop, and `max_depth`
    bounds recursion so a pathologically deep tree or symlink loop can't stall startup.

    Args:
        directory: Directory to search in
        pattern: Glob pattern to match file names against (e.g., '*.json')
        max_depth: Ceiling on recursion depth, supplied by the caller (e.g. an
            operator-configured setting).
        skip_hidden: If True, skip hidden directories (those starting with .).
            This avoids descending into large hidden trees like .git or .venv.
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
    await _arecurse_find(Path(directory), 0, params)

    if not matches:
        logger.debug("No files matching pattern '%s' found in directory: %s", pattern, directory)
    else:
        logger.debug("Found %d file(s) matching pattern '%s' in directory: %s", len(matches), pattern, directory)

    if max_files is not None:
        return sorted(matches)[:max_files]
    return sorted(matches)
