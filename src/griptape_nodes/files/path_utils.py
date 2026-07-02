"""Path utilities for file operations.

Comprehensive path handling utilities including:
- Path sanitization (shell escapes, quotes, newlines)
- Path expansion (tilde, environment variables)
- Path resolution (relative paths, cross-platform)
- Path normalization (Windows long paths, etc.)
- Workspace operations (relative path conversions)
- file:// URI parsing

These utilities provide consistent path handling across the codebase
and are used by OSManager, FileDrivers, and workspace managers.
"""

import os
import re
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import NamedTuple
from urllib.parse import unquote, urlparse

# Path decomposition patterns
_WINDOWS_DRIVE_MATCH_PATTERN = r"^([A-Z]):"
_WINDOWS_DRIVE_STRIP_PATTERN = r"^[A-Z]:/"
_WINDOWS_UNC_MATCH_PATTERN = r"^//([^/]+)/([^/]+)(?:/(.+))?$"
_MACOS_VOLUME_MATCH_PATTERN = r"^/Volumes/([^/]+)"
_MACOS_VOLUME_STRIP_PATTERN = r"^/Volumes/[^/]+/?"
_LINUX_MOUNT_MATCH_PATTERN = r"^/(mnt|media)/([^/]+)"
_LINUX_MOUNT_STRIP_PATTERN = r"^/(mnt|media)/[^/]+/?"

_WINDOWS_DRIVE_PATTERN = re.compile(_WINDOWS_DRIVE_MATCH_PATTERN, re.IGNORECASE)
_WINDOWS_UNC_PATTERN = re.compile(_WINDOWS_UNC_MATCH_PATTERN)
_MACOS_VOLUME_PATTERN = re.compile(_MACOS_VOLUME_MATCH_PATTERN)
_LINUX_MOUNT_PATTERN = re.compile(_LINUX_MOUNT_MATCH_PATTERN)

# Windows MAX_PATH limit - paths at or above this length need the \\?\ prefix.
WINDOWS_MAX_PATH = 260


def _apply_windows_long_path_prefix(path_str: str) -> str:
    r"""Prepend the Windows long-path prefix (``\\?\``) when required.

    No-op on non-Windows platforms, on paths shorter than ``WINDOWS_MAX_PATH``,
    or on paths that already carry the prefix. UNC paths (``\\server\share``)
    get the ``\\?\UNC\`` variant.
    """
    # TODO: https://github.com/griptape-ai/griptape-nodes/issues/4418
    if not sys.platform.startswith("win"):
        return path_str
    if len(path_str) < WINDOWS_MAX_PATH or path_str.startswith("\\\\?\\"):
        return path_str
    if path_str.startswith("\\\\"):
        return f"\\\\?\\UNC\\{path_str[2:]}"
    return f"\\\\?\\{path_str}"


def derive_registry_key(file_path: str) -> str:
    """Derive a workflow registry key from a file path.

    Strips the file extension and normalizes directory separators to forward slashes,
    preserving directory components for uniqueness across different directories.

    Args:
        file_path: Path to the workflow file, e.g. "subdir/my_workflow.py"

    Returns:
        Registry key with directory components preserved, e.g. "subdir/my_workflow"

    Examples:
        >>> derive_registry_key("my_workflow.py")
        "my_workflow"
        >>> derive_registry_key("subdir/my_workflow.py")
        "subdir/my_workflow"
    """
    normalized = file_path.replace("\\", "/")
    return str(PurePosixPath(normalized).with_suffix(""))


class FilenameParts(NamedTuple):
    """Components of a filename split into directory, stem, and extension.

    Used for macro variable extraction and path decomposition.

    Attributes:
        directory: Parent directory path (e.g. Path("/some/dir") from "/some/dir/output.png",
            or Path(".") when the input has no directory component)
        stem: Filename without extension (e.g. "output" from "output.png")
        extension: Extension without leading dot (e.g. "png" from "output.png")
    """

    directory: Path
    stem: str
    extension: str

    @classmethod
    def from_filename(cls, file_name: str) -> "FilenameParts":
        """Split a filename or path into directory, stem, and extension.

        Args:
            file_name: Filename or path to split (e.g. "output.png", "archive.tar.gz",
                or "/some/dir/output.png")

        Returns:
            FilenameParts with directory, stem, and extension (extension has no leading dot)
        """
        path = Path(file_name)
        return cls(directory=path.parent, stem=path.stem, extension=path.suffix.lstrip("."))


def parse_file_uri(location: str) -> str | None:
    """Parse file:// URI and return local path, or None if not a valid file URI.

    Supports:
    - file:///path/to/file (Unix absolute path)
    - file://localhost/path/to/file (localhost)
    - file:///C:/path/to/file (Windows absolute path)

    Rejects:
    - file://hostname/path (non-localhost network paths)

    Args:
        location: Location string to parse

    Returns:
        Local file path if valid file:// URI, None otherwise

    Examples:
        parse_file_uri("file:///path/to/file.txt")
        -> "/path/to/file.txt"

        parse_file_uri("file://localhost/path/to/file.txt")
        -> "/path/to/file.txt"

        parse_file_uri("file:///C:/Users/test/file.txt")
        -> "C:/Users/test/file.txt"

        parse_file_uri("file:///path/with%20spaces.txt")
        -> "/path/with spaces.txt"

        parse_file_uri("file://remote-server/path")
        -> None
    """
    if not location.startswith("file://"):
        return None

    parsed = urlparse(location)

    if parsed.scheme != "file":
        return None

    # Reject non-localhost network paths
    if parsed.netloc and parsed.netloc.lower() not in ("", "localhost"):
        return None

    # Get the path component and decode percent-encoding
    path = unquote(parsed.path)

    # Windows paths in file:// URIs have format file:///C:/path
    # Unix paths have format file:///path
    # The path component includes the leading slash, so we need to handle Windows specially
    if path.startswith("/") and len(path) > 2 and path[2] == ":":  # noqa: PLR2004
        # Windows path like /C:/Users/... -> C:/Users/...
        path = path[1:]

    return path


def sanitize_path_string(path: str | Path) -> str:
    r"""Clean path strings by removing newlines, carriage returns, shell escapes, and quotes.

    This method handles multiple path cleaning concerns:
    1. Removes newlines/carriage returns that cause WinError 123 on Windows
       (from merge_texts nodes accidentally adding newlines between path components)
    2. Removes shell escape characters and quotes (from macOS Finder 'Copy as Pathname')
    3. Strips leading/trailing whitespace

    Handles macOS Finder's 'Copy as Pathname' format which escapes
    spaces, apostrophes, and other special characters with backslashes.
    Only removes backslashes before shell-special characters to avoid
    breaking Windows paths like C:\Users\file.txt.

    Examples:
        macOS Finder paths:
            "/Downloads/Dragon\'s\ Curse/screenshot.jpg"
            -> "/Downloads/Dragon's Curse/screenshot.jpg"

            "/Test\ Images/Level\ 1\ -\ Knight\'s\ Quest/file.png"
            -> "/Test Images/Level 1 - Knight's Quest/file.png"

        Quoted paths:
            '"/path/with spaces/file.txt"'
            -> "/path/with spaces/file.txt"

        Windows paths with newlines:
            "C:\\Users\\file\\n\\n.txt"
            -> "C:\\Users\\file.txt"

        Windows extended-length paths:
            r"\\?\C:\Very\ Long\ Path\file.txt"
            -> r"\\?\C:\Very Long Path\file.txt"

        Path objects:
            Path("/path/to/file")
            -> "/path/to/file"

    Args:
        path: Path string or Path object to sanitize

    Returns:
        Sanitized path string
    """
    # Convert Path objects to strings using POSIX format for cross-platform consistency
    if isinstance(path, Path):
        path = path.as_posix()

    if not isinstance(path, str):
        return path

    # First, strip surrounding quotes
    path_str = strip_surrounding_quotes(path)

    # Handle Windows extended-length paths (\\?\...) specially
    # These are used for paths longer than 260 characters on Windows
    # We need to sanitize the path part but preserve the prefix
    extended_length_prefix = ""
    if path_str.startswith("\\\\?\\"):
        extended_length_prefix = "\\\\?\\"
        path_str = path_str[4:]  # Remove prefix temporarily

    # Remove shell escape characters (backslashes before special chars only)
    # Matches: space ' " ( ) { } [ ] & | ; < > $ ` ! * ? /
    # Does NOT match: \U \t \f etc in Windows paths like C:\Users
    path_str = re.sub(r"\\([ '\"(){}[\]&|;<>$`!*?/])", r"\1", path_str)

    # Remove newlines and carriage returns from anywhere in the path
    path_str = path_str.replace("\n", "").replace("\r", "")

    # Strip leading/trailing whitespace
    path_str = path_str.strip()

    # Restore extended-length prefix if it was present
    if extended_length_prefix:
        path_str = extended_length_prefix + path_str

    return path_str


def strip_surrounding_quotes(path: str) -> str:
    """Remove surrounding quotes from path string.

    Args:
        path: Path string that may be quoted

    Returns:
        Path string without surrounding quotes
    """
    if (path.startswith('"') and path.endswith('"')) or (path.startswith("'") and path.endswith("'")):
        return path[1:-1]
    return path


def normalize_path_for_platform(path: Path) -> str:
    r"""Convert Path to string with Windows long path support if needed.

    Windows has a 260 character path limit (MAX_PATH). Paths longer than this
    need the \\?\ prefix to work correctly. This method transparently adds
    the prefix when needed on Windows.

    Also cleans paths to remove newlines/carriage returns that cause Windows errors.

    Note: This method assumes the path exists or will exist. For non-existent
    paths that need cross-platform normalization, use resolve_path_safely() first.

    Args:
        path: Path object to convert to string

    Returns:
        String representation of path, cleaned of newlines/carriage returns,
        with Windows long path prefix if needed
    """
    path_str = str(path.resolve())

    # Clean path to remove newlines/carriage returns, shell escapes, and quotes
    # This handles cases where merge_texts nodes accidentally add newlines between path components
    path_str = sanitize_path_string(path_str)

    return _apply_windows_long_path_prefix(path_str)


def expand_path(path_str: str) -> Path:
    """Expand ~ and environment variables in a path string.

    Handles tilde (~) expansion and environment variables ($HOME, %USERPROFILE%, etc.)
    for standard path expansion scenarios.

    Note: This function does NOT resolve Windows special folders (Desktop, Downloads,
    etc.) via Shell API. For workspace-aware path resolution with Windows special
    folder support, use OSManager methods instead.

    Args:
        path_str: Path string that may contain ~ or environment variables

    Returns:
        Expanded Path object

    Examples:
        expand_path("~/Documents")
        -> Path("/Users/username/Documents")

        expand_path("$HOME/file.txt")
        -> Path("/Users/username/file.txt")
    """
    expanded_vars = os.path.expandvars(path_str)
    expanded_user = os.path.expanduser(expanded_vars)  # noqa: PTH111
    return Path(expanded_user)


def path_needs_expansion(path_str: str) -> bool:
    """Return True if path contains env vars, is absolute, or starts with ~ (needs expand_path).

    Args:
        path_str: Path string to check

    Returns:
        True if path needs expansion
    """
    has_env_vars = "%" in path_str or "$" in path_str
    is_absolute = Path(path_str).is_absolute()
    starts_with_tilde = path_str.startswith("~")
    return has_env_vars or is_absolute or starts_with_tilde


def resolve_path_safely(path: Path) -> Path:
    """Resolve a path consistently across platforms.

    Unlike Path.resolve() which behaves differently on Windows vs Unix
    for non-existent paths, this method provides consistent behavior:
    - Converts relative paths to absolute (using CWD as base)
    - Normalizes path separators and removes . and ..
    - Does NOT resolve symlinks if path doesn't exist
    - Does NOT change path based on CWD for absolute paths

    Use this instead of .resolve() when:
    - Path might not exist (file creation, validation, user input)
    - You need consistent cross-platform comparison
    - You're about to create the file/directory

    Use .resolve() when:
    - Path definitely exists and you need symlink resolution
    - You're checking actual file locations

    Args:
        path: Path to resolve (relative or absolute, existing or not)

    Returns:
        Absolute, normalized Path object

    Examples:
        # Relative path
        resolve_path_safely(Path("relative/file.txt"))
        → Path("/current/dir/relative/file.txt")

        # Absolute non-existent path (Windows safe)
        resolve_path_safely(Path("/abs/nonexistent/path"))
        → Path("/abs/nonexistent/path")  # NOT resolved relative to CWD
    """
    # Convert to absolute if relative
    if not path.is_absolute():
        path = Path.cwd() / path

    # Normalize (remove . and .., collapse slashes) without resolving symlinks
    # This works consistently even for non-existent paths on Windows
    return Path(os.path.normpath(path))


def canonicalize_for_identity(path: str | Path, *, base: Path | None = None) -> Path:
    """Produce a stable path identity for use as a dict key, cache key, or ID.

    Sanitizes shell escapes/quotes, expands ~ and environment variables, anchors
    relative paths to ``base`` (defaults to CWD), normalizes ``.`` and ``..``,
    and follows symlinks via ``Path.resolve(strict=False)`` so two spellings of
    the same file collide on equality. Non-existent paths do not raise; the
    resolvable prefix is resolved and the remainder is appended verbatim.

    Use this whenever a path is about to become a key: project IDs, cache
    lookups, dedupe sets, workspace-containment checks.

    Args:
        path: Raw path string or Path object (may contain ~, env vars, quotes,
            shell escapes, or relative segments).
        base: Base directory for relative paths. Defaults to ``Path.cwd()``.

    Returns:
        Canonical absolute Path.
    """
    sanitized = sanitize_path_string(path)
    expanded = expand_path(sanitized)
    if not expanded.is_absolute():
        expanded = (base if base is not None else Path.cwd()) / expanded
    return resolve_path_safely(expanded).resolve(strict=False)


def canonicalize_for_io(path: str | Path, *, base: Path | None = None) -> Path:
    r"""Produce a path suitable for handing to the filesystem.

    Same sanitization, expansion, absolutization, and normalization as
    ``canonicalize_for_identity``, but does NOT follow symlinks (safe for
    paths that do not yet exist) and applies the Windows long-path
    (``\\?\``) prefix when the result exceeds MAX_PATH.

    Use this at the boundary that actually hands the path to the OS (driver
    or request handler). Do NOT call it before constructing a
    ``ReadFileRequest`` / ``WriteFileRequest`` — those handlers already
    canonicalize on the way in, so a caller-side call is redundant.

    Args:
        path: Raw path string or Path object.
        base: Base directory for relative paths. Defaults to ``Path.cwd()``.

    Returns:
        Canonical Path ready for filesystem operations.
    """
    sanitized = sanitize_path_string(path)
    expanded = expand_path(sanitized)
    if not expanded.is_absolute():
        expanded = (base if base is not None else Path.cwd()) / expanded
    normalized = resolve_path_safely(expanded)

    normalized_str = str(normalized)
    prefixed = _apply_windows_long_path_prefix(normalized_str)
    if prefixed == normalized_str:
        return normalized
    return Path(prefixed)


def canonicalize_for_reverse_match(path: str | Path) -> str:
    r"""Produce a POSIX-form string for reverse-matching against a macro template.

    Macro templates are author-written and use ``/`` as the path separator by
    convention (see ``docs/projects/macros.md``). Filesystem paths reaching
    the reverse-matcher — from ``Path.glob()`` output, ``str(WindowsPath)``,
    or user-bound variables holding a directory path — may use ``\``. The
    parser's reverse-match
    (``common.macro_parser.matching.extract_unknown_variables``) aligns
    static text byte-for-byte, so a single separator mismatch causes the
    whole match to fail.

    This helper routes the path through ``PureWindowsPath.as_posix()``,
    which correctly understands every Windows path form and preserves them
    under conversion:

    - Drive-letter: ``C:\path`` → ``C:/path``
    - UNC: ``\\server\share\file`` → ``//server/share/file``
    - Long-path prefix: ``\\?\C:\path`` → ``//?/C:/path``
    - Long-UNC: ``\\?\UNC\server\share`` → ``//?/UNC/server/share/``
      (``PureWindowsPath`` appends a trailing separator when the input is
      a bare share root; not a problem in practice because reverse-match
      inputs always have a file component past the root)
    - Mixed separators: ``C:\a/b\c`` → ``C:/a/b/c``

    Works on any host OS — ``PureWindowsPath`` parses Windows-shaped strings
    without needing an actual Windows filesystem, so cross-platform tests
    can exercise the Windows edge cases from macOS or Linux runners.

    NOT suitable for I/O — the returned string uses ``/`` on Windows, which
    most Windows APIs accept but not all. For handing a path to the OS, use
    ``canonicalize_for_io``.

    Args:
        path: Raw path string (possibly Windows-shaped) or Path object.

    Returns:
        Forward-slash-separated string suitable for byte-for-byte comparison
        against a macro template's static text.
    """
    # `Path.as_posix()` on Windows would give the right answer, but on POSIX
    # hosts a `Path("C:\foo")` becomes `PurePosixPath` and treats the whole
    # string as a filename. Routing through `PureWindowsPath(str(...))`
    # forces Windows-aware parsing on every host.
    if isinstance(path, Path):
        path = str(path)
    return PureWindowsPath(path).as_posix()


def resolve_file_path(path_str: str, base_dir: Path) -> Path:
    """Resolve a file path, handling absolute, relative, and tilde paths.

    Args:
        path_str: Path string that may be absolute, relative, or start with ~
        base_dir: Base directory for resolving relative paths

    Returns:
        Resolved Path object
    """
    if path_needs_expansion(path_str):
        expanded = expand_path(path_str)
        # Expansion can leave a path still relative when it doesn't match an env var
        # (e.g. URL-encoded filenames like "foo%20bar.png" which contain '%' but are not
        # Windows env var references). In that case we still need to anchor to base_dir.
        if expanded.is_absolute():
            return expanded
        return resolve_path_safely(base_dir / expanded)
    return resolve_path_safely(base_dir / path_str)


def resolve_workspace_path(path: Path, base_directory: Path) -> Path:
    """Resolve a path, treating relative paths as relative to a base directory.

    If the path is relative, it's resolved relative to the base directory.
    If the path is absolute, it's resolved as-is.

    This utility works with any base directory - workspace_directory, project_base_dir,
    or any other base path.

    Args:
        path: The path to resolve (can be relative or absolute)
        base_directory: The base directory to use for relative paths

    Returns:
        The resolved absolute path

    Example:
        >>> base = Path("/workspace")
        >>> resolve_workspace_path(Path("file.txt"), base)
        Path("/workspace/file.txt")
        >>> resolve_workspace_path(Path("/tmp/file.txt"), base)
        Path("/tmp/file.txt")
    """
    if not path.is_absolute():
        return (base_directory / path).resolve()
    return path.resolve()


def get_workspace_relative_path(path: Path, base_directory: Path) -> Path:
    """Convert a path to be relative to a base directory.

    Takes an absolute or relative path and returns it as a path relative to
    the base directory.

    This utility works with any base directory - workspace_directory, project_base_dir,
    or any other base path.

    Args:
        path: The path to convert (can be relative or absolute)
        base_directory: The base directory to make the path relative to

    Returns:
        Path relative to base_directory

    Example:
        >>> base = Path("/workspace")
        >>> get_workspace_relative_path(Path("/workspace/subdir/file.txt"), base)
        Path("subdir/file.txt")
        >>> get_workspace_relative_path(Path("file.txt"), base)
        Path("file.txt")
    """
    absolute_path = resolve_workspace_path(path, base_directory)
    return absolute_path.relative_to(base_directory.resolve())


class DecomposedPath(NamedTuple):
    """Components of a decomposed source path for sidecar/preview path generation.

    Attributes:
        drive_volume_mount: Optional drive/volume/mount (e.g., "C", "Volumes/Backup")
        source_relative_path: Optional subdirectories (e.g., "images/subdir")
        source_file_name: Source file basename with extension
    """

    drive_volume_mount: str | None
    source_relative_path: str | None
    source_file_name: str


def decompose_source_path(  # noqa: C901, PLR0912
    absolute_path: Path,
    workspace_dir: Path,
) -> DecomposedPath:
    r"""Decompose source path into semantic components for sidecar/preview path generation.

    This function breaks down a file path into three components:
    - Drive/volume/mount identifier (optional): For Windows drives, macOS volumes, Linux mounts
    - Subdirectories (optional): Directory path between the root/drive and the filename
    - Filename (required): The actual file name with extension

    Cross-platform support: This method detects path patterns from all platforms (Windows drives,
    macOS volumes, Linux mounts, UNC paths) regardless of the current OS. This is necessary because
    paths must be consistently decomposed even when a project created on one platform is
    opened on another (e.g., a Windows path "C:\temp\file.txt" stored in project metadata must
    be correctly decomposed when opened on macOS).

    Args:
        absolute_path: Source file path to decompose (should be absolute)
        workspace_dir: Workspace directory for relative path detection.
                      If path is within workspace, drive/volume component is omitted.

    Returns:
        DecomposedPath with three components
    """
    # Extract filename first (always present)
    source_file_name = absolute_path.name

    # Convert path to string for pattern matching
    path_str = str(absolute_path)

    # Normalize path - convert backslashes to forward slashes
    normalized_path = path_str.replace("\\", "/")

    # Strip Windows long path prefix (\\?\ or \\?\UNC\) if present
    # This ensures paths written with normalize_path_for_platform can be decomposed correctly
    if normalized_path.upper().startswith("//?/UNC/"):
        # Windows long UNC path: \\?\UNC\server\share → //server/share
        normalized_path = "//" + normalized_path[8:]
    elif normalized_path.startswith("//?/"):
        # Windows long path: \\?\C:\path → C:/path
        normalized_path = normalized_path[4:]

    # Initialize result variables
    drive_volume_mount: str | None = None
    source_relative_path: str | None = None

    # Check for UNC paths (Windows network paths like \\server\share\file.txt)
    unc_match = _WINDOWS_UNC_PATTERN.match(normalized_path)
    if unc_match:
        server = unc_match.group(1)
        share = unc_match.group(2)
        rest = unc_match.group(3) or ""  # Subdirectories after share (may be empty)

        drive_volume_mount = f"{server}/{share}"
        if rest:
            # Extract subdirectories (everything except the filename)
            rest_path = Path(rest)
            if rest_path.parent != Path():
                source_relative_path = rest_path.parent.as_posix()

        return DecomposedPath(
            drive_volume_mount=drive_volume_mount,
            source_relative_path=source_relative_path,
            source_file_name=source_file_name,
        )

    # Check if path is within workspace
    try:
        relative_to_workspace = absolute_path.relative_to(workspace_dir)

        if relative_to_workspace.parent != Path():
            source_relative_path = relative_to_workspace.parent.as_posix()

    # Path is outside workspace - detect drive/volume/mount prefix
    except ValueError:
        remaining_path = normalized_path

        # Check for Windows drive letter (C:, D:, etc.)
        drive_match = _WINDOWS_DRIVE_PATTERN.match(normalized_path)
        if drive_match:
            drive_volume_mount = drive_match.group(1).upper()
            remaining_path = re.sub(_WINDOWS_DRIVE_STRIP_PATTERN, "", normalized_path, flags=re.IGNORECASE)

        # Check for macOS volume (/Volumes/VolumeName/...)
        volume_match = _MACOS_VOLUME_PATTERN.match(normalized_path)
        if volume_match:
            drive_volume_mount = f"Volumes/{volume_match.group(1)}"
            remaining_path = re.sub(_MACOS_VOLUME_STRIP_PATTERN, "", normalized_path)

        # Check for Linux mount points (/mnt/... or /media/...)
        mount_match = _LINUX_MOUNT_PATTERN.match(normalized_path)
        if mount_match:
            mount_type = mount_match.group(1)  # "mnt" or "media"
            mount_name = mount_match.group(2)
            drive_volume_mount = f"{mount_type}/{mount_name}"
            remaining_path = re.sub(_LINUX_MOUNT_STRIP_PATTERN, "", normalized_path)

        # Extract subdirectories from remaining path
        if remaining_path and remaining_path != "/":
            remaining_path_obj = Path(remaining_path)
            parent_path = remaining_path_obj.parent
            # Check if there's an actual parent directory (not root, not current dir)
            if parent_path != Path() and str(parent_path) != ".":
                relative_str = parent_path.as_posix().lstrip("/")
                # Only set if we have a non-empty, non-dot path
                if relative_str and relative_str != ".":
                    source_relative_path = relative_str

    return DecomposedPath(
        drive_volume_mount=drive_volume_mount,
        source_relative_path=source_relative_path,
        source_file_name=source_file_name,
    )
