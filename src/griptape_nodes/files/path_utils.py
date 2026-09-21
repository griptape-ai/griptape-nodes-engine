"""Path utilities for file operations.

Comprehensive path handling utilities including:
- Path sanitization (shell escapes, quotes, newlines)
- Path expansion (tilde, environment variables)
- Path resolution (relative paths, cross-platform)
- Path normalization (Windows long paths, etc.)
- Workspace operations (relative path conversions)
- file:// URI parsing
- URL discrimination (telling a URL apart from a filesystem path)

These utilities provide consistent path handling across the codebase
and are used by OSManager, FileDrivers, and workspace managers.
"""

import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import NamedTuple
from urllib.parse import unquote, urlparse

from griptape_nodes.files.os_utils import is_windows

# Path decomposition patterns
_WINDOWS_DRIVE_MATCH_PATTERN = r"^([A-Z]):"
_WINDOWS_DRIVE_STRIP_PATTERN = r"^[A-Z]:/"
_WINDOWS_UNC_MATCH_PATTERN = r"^//([^/]+)/([^/]+)(?:/(.+))?$"
_MACOS_VOLUME_MATCH_PATTERN = r"^/Volumes/([^/]+)"
_MACOS_VOLUME_STRIP_PATTERN = r"^/Volumes/[^/]+/?"
_LINUX_MOUNT_MATCH_PATTERN = r"^/(mnt|media)/([^/]+)"
_LINUX_MOUNT_STRIP_PATTERN = r"^/(mnt|media)/[^/]+/?"

# A backslash-separated Windows path: a drive letter (`C:\`) or a UNC root (`\\server`)
# followed by a backslash separator. Used to decide how aggressively sanitize_path_string
# may treat `\` as a shell escape rather than a directory separator.
_WINDOWS_SEPARATOR_MATCH_PATTERN = r"^(?:[A-Z]:\\|\\\\)"

# A URL scheme followed by `://`, e.g. `http://`, `https://`, `s3://`.
#
# The scheme must be at least TWO characters. That is what keeps a Windows drive letter
# (`C:`, always exactly one character) from being read as a scheme -- so a path spelled
# `C://outputs/clip.mp4` stays a path. RFC 3986 permits single-character schemes, but no
# scheme this codebase handles is one character, and misreading a drive letter is the far
# more likely failure.
_URL_SCHEME_MATCH_PATTERN = r"^[A-Za-z][A-Za-z0-9+.\-]+://"

# The path segment that the static file server mounts the workspace directory under.
_STATIC_SERVER_WORKSPACE_SEGMENT = "/workspace/"

_WINDOWS_DRIVE_PATTERN = re.compile(_WINDOWS_DRIVE_MATCH_PATTERN, re.IGNORECASE)
_WINDOWS_SEPARATOR_PATTERN = re.compile(_WINDOWS_SEPARATOR_MATCH_PATTERN, re.IGNORECASE)
_URL_SCHEME_PATTERN = re.compile(_URL_SCHEME_MATCH_PATTERN)
_WINDOWS_UNC_PATTERN = re.compile(_WINDOWS_UNC_MATCH_PATTERN)
_MACOS_VOLUME_PATTERN = re.compile(_MACOS_VOLUME_MATCH_PATTERN)
_LINUX_MOUNT_PATTERN = re.compile(_LINUX_MOUNT_MATCH_PATTERN)

# Brace/percent-delimited references that survive `expand_path` because nothing supplied a value.
# Only the unambiguously DELIMITED env forms are matched: a bare `$NAME` is indistinguishable from a
# real directory name (`$Recycle.Bin`), so it stays literal. See `unexpanded_references`.
_UNEXPANDED_ENV_BRACED_MATCH_PATTERN = r"\$\{([^{}]+)\}"
_UNEXPANDED_ENV_PERCENT_MATCH_PATTERN = r"%([A-Za-z_][A-Za-z0-9_]*)%"
# A `{NAME}` macro reference, matched only when NOT preceded by `$` so that the `${NAME}` env form
# above is not reported twice. The lookbehind is what couples these three patterns: they are one scan.
_UNEXPANDED_MACRO_MATCH_PATTERN = r"(?<!\$)\{([^{}]+)\}"

_UNEXPANDED_ENV_BRACED_PATTERN = re.compile(_UNEXPANDED_ENV_BRACED_MATCH_PATTERN)
_UNEXPANDED_ENV_PERCENT_PATTERN = re.compile(_UNEXPANDED_ENV_PERCENT_MATCH_PATTERN)
_UNEXPANDED_MACRO_PATTERN = re.compile(_UNEXPANDED_MACRO_MATCH_PATTERN)

# Windows MAX_PATH limit. Retained for documentation/reference only: the historical
# 260-char threshold at which the \\?\ prefix becomes strictly necessary. We no longer
# gate on it -- _apply_windows_long_path_prefix applies the prefix unconditionally on
# Windows (see below) -- but the constant documents where the limit comes from.
WINDOWS_MAX_PATH = 260


def _apply_windows_long_path_prefix(path_str: str) -> str:
    r"""Prepend the Windows long-path prefix (``\\?\``) on Windows.

    No-op on non-Windows platforms and on paths that already carry the prefix.
    UNC paths (``\\server\share``) get the ``\\?\UNC\`` variant.

    The prefix is applied **unconditionally** on Windows, not only when the
    string already exceeds ``WINDOWS_MAX_PATH``. The old length gate meant a
    short root path (e.g. a destination under ``%TEMP%``) was handed to the
    filesystem unprefixed, and per-file leaf paths built from it later — during
    a recursive copy that descends into a deep source tree — grew past MAX_PATH
    without ever inheriting the prefix, raising ``WinError 206``. Applying the
    prefix at the root lets ``pathlib``/``os.path`` joins carry it through to
    every leaf. The ``\\?\`` prefix is safe to apply to a sub-MAX_PATH path on
    modern Windows.

    Precondition: ``path_str`` must be a fully-qualified, backslash-separated
    Windows path (absolute drive path like ``C:\\dir`` or UNC ``\\\\server\\share``).
    The ``\\?\`` prefix disables Win32 path normalization, so a relative path
    (``sub\\file``) or a forward-slash path (``C:/dir``) would become an invalid
    ``\\?\`` string. Both current callers (``normalize_path_for_platform``,
    ``canonicalize_for_io``) absolutize and normalize before calling, so this
    holds; the guard below returns such inputs unchanged rather than producing a
    broken prefix, so a future caller can't silently create one.
    """
    if not is_windows():
        return path_str
    if path_str.startswith("\\\\?\\"):
        return path_str
    # A relative or forward-slash path is not fully-qualified; prefixing it would
    # yield an invalid \\?\ string (the prefix disables normalization). Leave it
    # unchanged rather than corrupt it -- see the precondition above.
    if not PureWindowsPath(path_str).is_absolute() or "/" in path_str:
        return path_str
    if path_str.startswith("\\\\"):
        return f"\\\\?\\UNC\\{path_str[2:]}"
    return f"\\\\?\\{path_str}"


def strip_windows_long_path_prefix(path: str | Path) -> str:
    r"""Remove the Windows long-path prefix (``\\?\`` / ``\\?\UNC\``) if present.

    The inverse of ``_apply_windows_long_path_prefix``, and the counterpart every
    caller that COMPARES paths needs. ``canonicalize_for_io`` applies the prefix
    unconditionally on Windows, but the prefix changes a path's *anchor*, not just
    its spelling: ``PurePath(r"\\?\C:\ws\file").relative_to(r"C:\ws")`` raises
    ``ValueError`` even though the file is plainly inside ``C:\ws``. Any
    containment check that sees one prefixed side and one unprefixed side
    therefore reports "outside" for a file that is inside. Strip both sides first.

    Pure string manipulation, so it works on any host OS. That matters for
    ``decompose_source_path``, which must handle a Windows path stored in project
    metadata while running on macOS.

    Both separator spellings are recognized (``\\?\C:\ws`` and ``//?/C:/ws``) because
    pathlib only rewrites forward slashes to backslashes when the host is Windows, and
    a path read back from project metadata can arrive either way.

    Args:
        path: Path that may or may not carry the prefix.

    Returns:
        The path string without the prefix, keeping the caller's separator style.
        Inputs without the prefix, and non-Windows paths, are returned unchanged.
    """
    path_str = str(path)
    # Detect against a backslash-only form so one branch covers both spellings, but slice
    # the original so a forward-slash path does not come back with mixed separators.
    detection_form = path_str.replace("/", "\\")
    if not detection_form.startswith("\\\\?\\"):
        return path_str
    separator = path_str[0]
    if detection_form.upper().startswith("\\\\?\\UNC\\"):
        # \\?\UNC\server\share -> \\server\share
        return f"{separator * 2}{path_str[8:]}"
    # \\?\C:\path -> C:\path
    return path_str[4:]


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


def is_url(location: str) -> bool:
    r"""Return True if ``location`` carries a URL scheme rather than being a filesystem path.

    This is the discriminator to reach for before handing a string to any path
    helper in this module. Those helpers assume a filesystem path, so a URL that
    reaches them is silently mangled rather than rejected: ``resolve_file_path``
    anchors ``http://host/a.mp4`` to the base directory and collapses ``//`` to
    the platform separator, producing a string that is neither a URL nor a real
    path.

    A Windows drive letter is NOT a scheme -- see ``_URL_SCHEME_MATCH_PATTERN``
    for why the two-character minimum is what separates them.

    ``file://`` URLs count as URLs here. They are still *convertible* to a local
    path; use ``parse_file_uri`` for that. Callers that want "is this something I
    must not treat as a path" should test this function first and convert
    ``file://`` afterwards.

    Args:
        location: String to classify.

    Returns:
        True if the string begins with a URL scheme followed by ``://``.

    Examples:
        >>> is_url("http://localhost:8124/workspace/staticfiles/clip.mp4?t=1")
        True
        >>> is_url("https://example.com/clip.mp4")
        True
        >>> is_url("file:///outputs/clip.mp4")
        True
        >>> is_url(r"C:\Users\artist\clip.mp4")
        False
        >>> is_url("C://Users/artist/clip.mp4")
        False
        >>> is_url("/outputs/clip.mp4")
        False
        >>> is_url("outputs/clip.mp4")
        False
    """
    return _URL_SCHEME_PATTERN.match(location) is not None


def parse_static_server_url(location: str, workspace_path: Path) -> Path | None:
    """Map a static file server URL back to the workspace file it serves.

    The engine hands node outputs around as static server URLs
    (``http://localhost:8124/workspace/staticfiles/<name>.mp4?t=<cachebuster>``).
    Those URLs address a file that already exists inside the workspace, so a
    consumer that needs a real path -- to hand to a subprocess like FFmpeg, say --
    can have one without an HTTP round-trip.

    Only ``localhost`` URLs qualify. A remote host may serve a ``/workspace/``
    path too, but its files are not on this machine, so there is no local path to
    return.

    Args:
        location: Location string, which may or may not be a static server URL.
        workspace_path: The workspace directory the static server serves from.

    Returns:
        The local Path of the served file, or None if ``location`` is not a
        localhost static server URL.

    Examples:
        >>> parse_static_server_url(
        ...     "http://localhost:8124/workspace/staticfiles/clip.mp4?t=1786574231",
        ...     Path("/home/artist/GriptapeNodes"),
        ... )
        PosixPath('/home/artist/GriptapeNodes/staticfiles/clip.mp4')
        >>> parse_static_server_url("http://localhost:8124/api/health", Path("/ws")) is None
        True
        >>> parse_static_server_url("https://example.com/workspace/clip.mp4", Path("/ws")) is None
        True
    """
    if not location.startswith(("http://localhost:", "https://localhost:")):
        return None

    # Strip the cachebuster (`?t=...`) before parsing: it is addressing metadata for
    # the HTTP server, not part of the filename.
    url_without_query = location.split("?", maxsplit=1)[0]
    parsed = urlparse(url_without_query)

    if _STATIC_SERVER_WORKSPACE_SEGMENT not in parsed.path:
        return None

    workspace_relative_path = parsed.path.split(_STATIC_SERVER_WORKSPACE_SEGMENT, 1)[1]
    if not workspace_relative_path:
        return None

    # Deliberately NOT percent-decoded. `LocalStorageDriver.create_signed_download_url`
    # builds these URLs with `Path.as_posix()` and no encoding step, so the path segment
    # is already the literal filename -- decoding it would corrupt a file whose name
    # genuinely contains a `%`. This mirrors what the read path
    # (`StaticServerFileDriver`) does, which is the point: `File.resolve()` and
    # `File.read_bytes()` must agree on which file a URL names.
    return workspace_path / workspace_relative_path


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
    #
    # How much we strip depends on the SHAPE of the path, not on the host platform: a
    # path can be authored on one OS and consumed on another (pasted from WSL, stored in
    # a saved workflow), so `sys.platform` says nothing about which convention produced
    # the string.
    #
    # On a backslash-separated Windows path, `\` is the directory separator, and
    # stripping it wholesale turns `C:\outputs\!final\render.png` into
    # `C:\outputs!final\render.png` -- silently retargeting the write at a sibling of the
    # intended directory whenever a component begins with a shell-special character
    # (griptape-ai/internal#178). Only `\ ` stays unambiguous there, because a Windows
    # path component cannot begin with a space, so `\ ` can only be an escape.
    #
    # A stripped `\\?\` prefix is itself proof of Windows shape: the extended-length
    # remainder of a UNC path (`UNC\server\...`) matches neither the drive-letter nor
    # the `\\` form, so without this check it would fall into the aggressive branch
    # and lose separators exactly as above.
    if extended_length_prefix or _WINDOWS_SEPARATOR_PATTERN.search(path_str):
        path_str = re.sub(r"\\( )", r"\1", path_str)
    else:
        path_str = re.sub(r"\\([ '\"(){}[\]&|;<>$`!*?/])", r"\1", path_str)

    # Remove newlines and carriage returns from anywhere in the path.
    # These cause WinError 123 on Windows (merge_texts nodes can introduce them
    # between path components).
    path_str = path_str.replace("\n", "").replace("\r", "")

    # Strip leading/trailing whitespace
    path_str = path_str.strip()

    # Restore extended-length prefix if it was present
    if extended_length_prefix:
        path_str = extended_length_prefix + path_str

    return path_str


_QUOTE_CHARACTERS = ("'", '"')


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


def expansion_introduced_quoting(declared: str, expanded: str) -> bool:
    """Report whether variable expansion put quoting into a path that the author did not write.

    `strip_surrounding_quotes` only sees quotes wrapping the WHOLE string, which is the shape a
    quoted value has before expansion. Once a variable supplies only a PREFIX, its own quotes move
    into the interior -- `ROOT='"/mnt/studio"'` turns `${ROOT}/libs` into `"/mnt/studio"/libs`, where
    there is nothing left to strip. The damage is not cosmetic: a leading quote makes
    `Path.is_absolute()` False, so an absolute path silently becomes a relative one and gets anchored
    somewhere the author never named.

    Refusing beats cleaning, because an interior quote is ambiguous by then and the wrong guess is
    silent. Two rules keep the refusal from swallowing real directory names:

    - A `"` that expansion introduced is always quoting. Windows forbids it in a filename outright,
      and it is the character shells and macOS Finder wrap paths in, so it did not come from a
      directory called `He said "hi"`.
    - A quote of either kind is refused only in the LEADING position, which is the one that flips
      `is_absolute()`. An interior `'` is left alone: `/mnt/Dragon's Curse` is an ordinary directory
      that a variable may legitimately hold, and `sanitize_path_string` documents apostrophes as a
      supported case.

    The DECLARED text is exempt from both rules. A quote someone typed into their own project.yml is
    unambiguously theirs, and honoring it is what makes the refusal specific to expansion.

    Args:
        declared: The value as authored, after `sanitize_path_string` but BEFORE expansion.
        expanded: The fully expanded value, after `sanitize_path_string` has run on it again. Pass
            the settled value, not a single pass: a quote can arrive on any pass of
            `expand_path_fully`, and a check placed before the fixed point misses the later ones.

    Returns:
        True when the caller should refuse the value rather than resolve it.
    """
    if expanded.count('"') > declared.count('"'):
        return True
    return expanded.startswith(_QUOTE_CHARACTERS) and not declared.startswith(_QUOTE_CHARACTERS)


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


class FullyExpandedPath(NamedTuple):
    """Outcome of expanding a path string until it stops changing.

    Attributes:
        path: The expanded path.
        stabilized: True when expansion reached a fixed point -- another pass would change nothing.
            False means passes were still changing the value when the cap was reached, which in
            practice means the references expand into each other in a cycle. Callers that report
            WHY a value could not be resolved need this apart from "a variable is unset": for
            `A=${B}` / `B=${A}` both variables ARE set, and naming either one as missing is wrong.
    """

    path: Path
    stabilized: bool


def expand_path_fully(path_str: str, *, max_passes: int = 8) -> FullyExpandedPath:
    r"""Expand ~ and environment variables repeatedly until the value stops changing.

    `expand_path` runs a single pass. That is right for a raw path, but a variable's VALUE can
    itself contain a reference -- `LIBS=${ROOT}/libs` is an ordinary shape, and a `.env` read
    without interpolation stores exactly that verbatim -- and one pass leaves the inner `${ROOT}`
    behind.

    Use this instead of `expand_path` whenever the expansion is going to be VALIDATED (see
    `unexpanded_references`) and then used. Validating one pass and using another is how a
    perfectly resolvable value gets reported as declaring an unset variable: the check sees
    `${ROOT}`, while the path actually handed downstream has it filled in.

    Expansion is looped over the string rather than a `Path` so intermediate values are not
    renormalized (`/` to `\\` on Windows) between passes; the `Path` is built once at the end.

    A self-referential value (`A=${A}`) is not a cycle here: `os.path.expandvars` leaves it alone,
    so it stabilizes on the first pass and falls out as an ordinary unresolved reference. Only
    mutual references exhaust `max_passes`.

    Args:
        path_str: Path string that may contain ~ or environment variables.
        max_passes: Maximum expansion passes before giving up. The default is far above any real
            reference chain; it exists to bound mutual references, not to limit depth.

    Returns:
        A `FullyExpandedPath`; check `stabilized` before trusting `path` to be fully expanded.

    Examples:
        # ROOT=/srv, LIBS=${ROOT}/libs
        expand_path_fully("${LIBS}/griptape")
        -> FullyExpandedPath(Path("/srv/libs/griptape"), stabilized=True)

        # A=${B}, B=${A}
        expand_path_fully("${A}/x")
        -> FullyExpandedPath(..., stabilized=False)
    """
    current = path_str
    stabilized = False
    for _ in range(max_passes):
        expanded = os.path.expanduser(os.path.expandvars(current))  # noqa: PTH111
        if expanded == current:
            stabilized = True
            break
        current = expanded
    return FullyExpandedPath(path=Path(current), stabilized=stabilized)


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


class UnexpandedReferences(NamedTuple):
    """Delimited references still present in a path after `expand_path` ran over it.

    Attributes:
        variables: Names from `${NAME}` / `%NAME%` references that expanded to nothing.
        macro_tokens: Names from `{NAME}` tokens, which `expand_path` never touches.
    """

    variables: list[str]
    macro_tokens: list[str]


def unexpanded_references(path: str | Path) -> UnexpandedReferences:
    """Report the delimited references an `expand_path` call left behind.

    The counterpart to `path_needs_expansion`: that asks whether a raw value needs expanding, this
    asks what expansion could not supply. A caller that finds anything here knows the path has no
    real answer yet and can name what is missing, instead of treating `${LIBS}` or `{outputs}` as a
    directory name.

    Call it on `expand_path_fully`'s output, not `expand_path`'s: a single pass leaves the inner
    reference of `LIBS=${ROOT}/libs` behind, so scanning it reports `ROOT` as unsupplied when it is
    set and the value resolves fine.

    Reporting only, no policy: whether a `{NAME}` macro token is legal in a given field is the
    caller's rule (they are not legal in project path fields; see `resolve_project_path_field`), and
    whether an unsupplied variable is fatal depends on the caller too.

    Three deliberate limits:
    - A bare `$NAME` is NOT reported. `os.path.expandvars` accepts that form -- a set `$NAME` does
      expand -- but an unexpanded `$Recycle.Bin` is indistinguishable from a real directory of that
      name, so an unexpanded one stays literal. Only `${NAME}` and `%NAME%` are unambiguous enough to
      call out.
    - `%NAME%` is only expanded by `os.path.expandvars` on Windows, so on other platforms it is
      reported even when the variable IS set. That is the honest answer: the value did not expand.
      The cost is a POSIX path with two literal percent signs around a name-shaped run of characters
      (`/srv/%share%/x`) being called out as unresolved. Requiring a leading letter or underscore
      keeps the common encodings out of it -- `%20`, `%2F` and friends do not match -- which leaves
      the false positive rare enough to prefer over silently creating a `%NAME%` directory on the one
      platform where a cross-platform project.yml would not have expanded it.
    - A variable that is SET BUT EMPTY cannot be reported. It expands to nothing and leaves no
      delimiters behind, so by the time a value reaches here `${EMPTY}/libs` and `/libs` are the same
      string. Callers that care must compare against the pre-expansion value themselves.

    Args:
        path: The already-expanded path (or any string) to scan.

    Returns:
        An `UnexpandedReferences`; both lists empty means the value expanded fully.
    """
    path_str = str(path)
    variables = [
        *(match.group(1) for match in _UNEXPANDED_ENV_BRACED_PATTERN.finditer(path_str)),
        *(match.group(1) for match in _UNEXPANDED_ENV_PERCENT_PATTERN.finditer(path_str)),
    ]
    macro_tokens = [match.group(1) for match in _UNEXPANDED_MACRO_PATTERN.finditer(path_str)]
    return UnexpandedReferences(variables=variables, macro_tokens=macro_tokens)


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


def _anchor_and_resolve(expanded: Path, base: Path | None) -> Path:
    """Anchor a relative path to ``base`` (default CWD), then canonicalize it for identity.

    The tail shared by ``canonicalize_for_identity`` and
    ``canonicalize_expanded_for_identity``: the two differ only in whether they
    sanitize and expand first, and must not drift in what they do afterwards.
    """
    if not expanded.is_absolute():
        expanded = (base if base is not None else Path.cwd()) / expanded
    return resolve_path_safely(expanded).resolve(strict=False)


def canonicalize_expanded_for_identity(expanded: Path, *, base: Path | None = None) -> Path:
    """Canonicalize an ALREADY-expanded path for identity, without expanding it again.

    Identical to ``canonicalize_for_identity`` from the anchoring step onward; it
    just does not sanitize or expand on the way in.

    For callers that expanded the value themselves and must not have it expanded a
    second time -- specifically, callers that VALIDATED their expansion. Handing a
    validated string to ``canonicalize_for_identity`` would expand it again, so the
    path returned is not the path that was checked: a value whose expansion yields
    another reference gets reported as unresolvable while the path built from it
    resolves fine. See ``resolve_project_path_field``.

    Args:
        expanded: A path whose ``~`` and environment variables are already expanded.
        base: Base directory for relative paths. Defaults to ``Path.cwd()``.

    Returns:
        Canonical absolute Path.
    """
    return _anchor_and_resolve(expanded, base)


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
    return _anchor_and_resolve(expand_path(sanitize_path_string(path)), base)


def canonicalize_for_io(path: str | Path, *, base: Path | None = None) -> Path:
    r"""Produce a path suitable for handing to the filesystem.

    Same sanitization, expansion, absolutization, and normalization as
    ``canonicalize_for_identity``, but does NOT follow symlinks (safe for
    paths that do not yet exist) and applies the Windows long-path
    (``\\?\``) prefix unconditionally on Windows (see
    ``_apply_windows_long_path_prefix``).

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


def canonicalize_to_posix(path: str | Path) -> str:
    r"""Produce a POSIX-form (forward-slash) string from a maybe-Windows-shaped path.

    Routes the input through ``PureWindowsPath.as_posix()``, which understands
    every Windows path form and preserves them under conversion:

    - Drive-letter: ``C:\path`` → ``C:/path``
    - UNC: ``\\server\share\file`` → ``//server/share/file``
    - Long-path prefix: ``\\?\C:\path`` → ``//?/C:/path``
    - Long-UNC: ``\\?\UNC\server\share`` → ``//?/UNC/server/share/``
      (``PureWindowsPath`` appends a trailing separator when the input is
      a bare share root; harmless in practice because callers pass
      filenames or subpaths past the root)
    - Mixed separators: ``C:\a/b\c`` → ``C:/a/b/c``
    - POSIX input is a no-op: ``/some/path`` → ``/some/path``

    Works on any host OS — ``PureWindowsPath`` parses Windows-shaped strings
    without needing an actual Windows filesystem, so cross-platform tests
    can exercise the Windows edge cases from macOS or Linux runners.

    **When to reach for this.** Anywhere a filesystem-derived path needs to
    be compared, joined, or matched against text that uses ``/`` by
    convention. Concrete cases in the tree:

    - Reverse-matching a filesystem path against a macro template (see
      ``_extract_index_from_filename`` in ``os_manager.py``) — templates use
      ``/`` but ``Path.glob()`` output uses ``\`` on Windows.
    - Constructing URL-shaped strings from filesystem paths.
    - Deriving registry / cache keys from paths so the same file gets one
      key regardless of the host's native separator.

    **NOT suitable for I/O.** The returned string uses ``/`` on Windows,
    which most Windows APIs accept but not all. For handing a path to the
    OS, use ``canonicalize_for_io``.

    Args:
        path: Raw path string (possibly Windows-shaped) or Path object.

    Returns:
        Forward-slash-separated string.
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

    # Check if path is within workspace.
    # Both sides must have the \\?\ long-path prefix stripped before comparison, the same
    # normalization already applied to ``normalized_path`` above. The prefix changes a path's
    # anchor, so relative_to() reports a file sitting INSIDE the workspace as outside it, and
    # the outside-workspace branch then builds an absolute-form cache key
    # (``C/Users/<user>/.../file.png``) alongside the correct relative one. Since
    # canonicalize_for_io applies the prefix unconditionally on Windows, whether a caller
    # canonicalized for I/O on the way in decided which key a file got.
    try:
        relative_to_workspace = Path(strip_windows_long_path_prefix(absolute_path)).relative_to(
            strip_windows_long_path_prefix(workspace_dir)
        )

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
