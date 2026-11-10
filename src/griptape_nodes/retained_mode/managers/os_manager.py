import asyncio
import base64
import ctypes
import logging
import mimetypes
import os
import shlex
import shutil
import stat
import subprocess
import sys
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple

import anyio
import send2trash
from rich.console import Console

from griptape_nodes.common.macro_parser import (
    MacroResolutionError,
    MacroResolutionFailure,
    MacroSyntaxError,
    ParsedMacro,
)
from griptape_nodes.common.macro_parser.exceptions import MacroResolutionFailureReason
from griptape_nodes.common.sequences import (
    InvalidSubsetBoundsError,
    MissingItemError,
    Sequence,
    SequenceScanOptions,
)
from griptape_nodes.common.sequences.scan import (
    scan_sequences_from_filenames,
)
from griptape_nodes.files import os_utils
from griptape_nodes.files.drivers.base64_file_driver import Base64FileDriver
from griptape_nodes.files.drivers.data_uri_file_driver import DataUriFileDriver
from griptape_nodes.files.drivers.griptape_cloud_file_driver import GriptapeCloudFileDriver
from griptape_nodes.files.drivers.http_file_driver import HttpFileDriver
from griptape_nodes.files.drivers.local_file_driver import LocalFileDriver
from griptape_nodes.files.drivers.static_server_file_driver import StaticServerFileDriver
from griptape_nodes.files.file import File, FileLoadError, file_load_error_from_result
from griptape_nodes.files.file_driver import FileDriverNotFoundError, FileDriverRegistry
from griptape_nodes.files.path_utils import (
    canonicalize_for_identity,
    normalize_path_for_platform,
    path_needs_expansion,
    resolve_path_safely,
    sanitize_path_string,
    strip_surrounding_quotes,
)
from griptape_nodes.retained_mode.engine import Engine, EngineScoped
from griptape_nodes.retained_mode.events.base_events import ResultDetails, ResultPayload
from griptape_nodes.retained_mode.events.os_events import (
    CopyFileRequest,
    CopyFileResultFailure,
    CopyFileResultSuccess,
    CopyTreeRequest,
    CopyTreeResultFailure,
    CopyTreeResultSuccess,
    CreateFileRequest,
    CreateFileResultFailure,
    CreateFileResultSuccess,
    DeleteFileRequest,
    DeleteFileResultFailure,
    DeleteFileResultSuccess,
    DeletionBehavior,
    DeletionOutcome,
    FileIOFailureReason,
    FileSystemEntry,
    GetFileInfoRequest,
    GetFileInfoResultFailure,
    GetFileInfoResultSuccess,
    GetNextUnusedFilenameRequest,
    GetNextUnusedFilenameResultFailure,
    GetNextUnusedFilenameResultSuccess,
    GetNextVersionIndexRequest,
    GetNextVersionIndexResultFailure,
    GetNextVersionIndexResultSuccess,
    LaunchExternalViewerRequest,
    LaunchExternalViewerResultFailure,
    LaunchExternalViewerResultSuccess,
    ListDirectoryRequest,
    ListDirectoryResultFailure,
    ListDirectoryResultSuccess,
    MakeDirectoryRequest,
    MakeDirectoryResultFailure,
    MakeDirectoryResultSuccess,
    OpenAssociatedFileRequest,
    OpenAssociatedFileResultFailure,
    OpenAssociatedFileResultSuccess,
    ReadFileRequest,
    ReadFileResultFailure,
    ReadFileResultSuccess,
    RenameFileRequest,
    RenameFileResultFailure,
    RenameFileResultSuccess,
    ResolveMacroPathRequest,
    ResolveMacroPathResultFailure,
    ResolveMacroPathResultSuccess,
    SequenceScanFailureReason,
)
from griptape_nodes.retained_mode.events.project_events import (
    GetPathForMacroRequest,
    GetPathForMacroResultSuccess,
    MacroPath,
)
from griptape_nodes.retained_mode.managers.event_manager import EventManager
from griptape_nodes.retained_mode.managers.filesystem.file_writing import FileWriter
from griptape_nodes.retained_mode.managers.filesystem.indexed_filenames import IndexedFilenames
from griptape_nodes.retained_mode.managers.filesystem.sequences import FileSequences, describe_missing_items
from griptape_nodes.retained_mode.managers.resource_types.os_resource import current_platform
from griptape_nodes.retained_mode.request_handlers import handles
from griptape_nodes.utils.async_utils import to_thread
from griptape_nodes.utils.image_preview import create_image_preview_from_bytes

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime


console = Console()
logger = logging.getLogger("griptape_nodes")


@dataclass
class DiskSpaceInfo:
    """Information about disk space usage."""

    total: int
    used: int
    free: int


@dataclass
class CopyTreeValidationResult:
    """Result from validating copy tree paths."""

    source_normalized: str
    dest_normalized: str
    source_path: Path
    destination_path: Path


class WindowsSpecialFolderError(OSError):
    """Raised when Windows Shell API (SHGetFolderPathW) fails for a special folder.

    Callers (e.g. try_resolve_windows_special_folder) catch this to fall back
    to expanduser or other resolution.
    """


@dataclass
class CopyTreeStats:
    """Statistics from copying a directory tree."""

    files_copied: int
    total_bytes_copied: int


class WindowsSpecialFolderResult(NamedTuple):
    """Result of resolving a Windows special folder from path parts.

    Invariant: either both fields are None (not resolved), or both are set
    (resolved). When resolved, special_path is the folder Path and
    remaining_parts is the list of path components after the folder (may be
    empty). We never return (None, list) or (Path, None).
    """

    special_path: Path | None
    remaining_parts: list[str] | None


class OSManager(EngineScoped):
    """A class to manage OS-level scenarios.

    Making its own class as some runtime environments and some customer requirements may dictate this as optional.
    This lays the groundwork to exclude specific functionality on a configuration basis.
    """

    # Windows CSIDL constants for special folders (used by _expand_path)
    # https://learn.microsoft.com/en-us/windows/win32/shell/csidl
    WINDOWS_CSIDL_MAP: ClassVar[dict[str, int]] = {
        "desktop": 0x0000,  # CSIDL_DESKTOP
        "documents": 0x0005,  # CSIDL_PERSONAL (My Documents)
        "downloads": 0x0033,  # CSIDL_DOWNLOADS
        "pictures": 0x0027,  # CSIDL_MYPICTURES
        "videos": 0x000E,  # CSIDL_MYVIDEO
        "music": 0x000D,  # CSIDL_MYMUSIC
    }

    # Matches the File Browser dialog's own client-side preview cutoff. The sequence-frame
    # loader bypasses that client-side check, so this is what actually stops a 200+ MB frame
    # from being base64-encoded whole -- the real cost behind the engine hang in #6043.
    _MAX_PREVIEWABLE_IMAGE_BYTES: ClassVar[int] = 10 * 1024 * 1024

    # Returned in place of a preview for files over the size cap. A syntactically valid, empty
    # data URL: no image decoder can render it, so the editor shows its own "failed to load
    # preview" state instead of the raw file content.
    _NO_PREVIEW_DATA_URL: ClassVar[str] = "data:,"

    @staticmethod
    def normalize_path_parts_for_special_folder(path_str: str) -> list[str]:
        r"""Parse a path string into normalized parts for special folder detection.

        Strips leading ~ or ~/, or %UserProfile% / %USERPROFILE% (case-insensitive);
        expands env vars when %UserProfile% is present; returns lowercased path
        parts. Used to detect Windows special folder names (e.g. ~/Downloads,
        %UserProfile%/Desktop). Also strips Windows long path prefix (\\?\ or
        \\?\UNC\) so prefixed paths parse correctly instead of producing "?"
        as the first part.

        Args:
            path_str: Path string that may contain ~ or %UserProfile% (case-insensitive).

        Returns:
            List of lowercased path parts, e.g. ["downloads"] for "~/Downloads".
        """
        normalized = path_str.replace("\\", "/")
        # Strip Windows long path prefix so we don't get "?" as first part
        if normalized.upper().startswith("//?/UNC/"):
            normalized = "//" + normalized[8:]  # Keep UNC as //server/share
        elif normalized.startswith("//?/"):
            normalized = normalized[4:]
        if normalized.startswith("~/"):
            normalized = normalized[2:]
        elif normalized.startswith("~"):
            normalized = normalized[1:]
        if "%USERPROFILE%" in normalized.upper():
            normalized = os.path.expandvars(normalized)
            normalized = normalized.replace("\\", "/")  # expandvars can return backslashes on Windows
            userprofile = os.environ.get("USERPROFILE", "")
            if userprofile and normalized.lower().startswith(userprofile.lower().replace("\\", "/")):
                normalized = normalized[len(userprofile) :].lstrip("/\\")
        parts = [p.lower() for p in normalized.split("/") if p]
        return parts

    def try_resolve_windows_special_folder(self, parts: list[str]) -> WindowsSpecialFolderResult | None:
        """Resolve Windows special folder from path parts.

        If the first part matches a known special folder name (e.g. "desktop",
        "downloads"), calls _get_windows_special_folder_path and returns a
        result with special_path and remaining_parts. Returns None if parts are
        empty, the first part is unknown, or the Shell API raises
        WindowsSpecialFolderError (caller catches and falls back).

        Args:
            parts: Lowercased path parts from normalize_path_parts_for_special_folder.

        Returns:
            WindowsSpecialFolderResult when resolved (special_path and remaining_parts),
            or None when no special folder could be resolved.
        """
        if not parts or parts[0] not in OSManager.WINDOWS_CSIDL_MAP:
            return None
        csidl = OSManager.WINDOWS_CSIDL_MAP[parts[0]]
        try:
            special_path = self._get_windows_special_folder_path(csidl)
        except WindowsSpecialFolderError:
            # No warning: Shell API failure is an expected fallback path; not useful to users.
            return None
        remaining = parts[1:] if len(parts) > 1 else []
        return WindowsSpecialFolderResult(special_path=special_path, remaining_parts=remaining)

    def __init__(self, event_manager: EventManager | None = None, *, engine: Engine | None = None):
        super().__init__(engine)
        self.indexed_filenames = IndexedFilenames(engine)
        if event_manager is not None:
            event_manager.register_request_handlers(self)
            self.sequences = FileSequences(event_manager, engine=engine)
            self.writer = FileWriter(event_manager, engine=engine)

            # Initialize file read drivers for multi-source file reading
            self._initialize_file_drivers()

    def _initialize_file_drivers(self) -> None:
        """Initialize file drivers for multi-source file reading.

        Drivers are automatically sorted by priority on registration.
        """
        FileDriverRegistry.register(StaticServerFileDriver())
        FileDriverRegistry.register(HttpFileDriver())
        FileDriverRegistry.register(DataUriFileDriver())

        cloud_driver = GriptapeCloudFileDriver.create_from_env()
        if cloud_driver:
            FileDriverRegistry.register(cloud_driver)

        FileDriverRegistry.register(Base64FileDriver())
        FileDriverRegistry.register(LocalFileDriver())

    def _get_workspace_path(self) -> Path:
        """Get the workspace path from config."""
        return self.engine.config_manager.workspace_path

    def _get_windows_special_folder_path(self, csidl: int) -> Path:
        """Get Windows special folder path using Shell API.

        Source: https://stackoverflow.com/a/30924555
        Uses SHGetFolderPathW to get the actual location of special folders,
        handling OneDrive redirections and other Windows folder redirections.
        Callers (e.g. try_resolve_windows_special_folder) should catch
        WindowsSpecialFolderError and fall back to expanduser.

        Args:
            csidl: CSIDL constant for the special folder (e.g., CSIDL_DESKTOP)

        Returns:
            Path to the special folder.

        Raises:
            RuntimeError: If not on Windows (programming error).
            WindowsSpecialFolderError: If the Shell API fails (HRESULT or ctypes exception).
        """
        if not self.is_windows():
            msg = "_get_windows_special_folder_path may only be called on Windows"
            raise RuntimeError(msg)

        # Argtypes for SHGetFolderPathW (Windows Shell API)
        # https://learn.microsoft.com/en-us/windows/win32/shell/csidl
        sh_get_folder_path_argtypes = (
            wintypes.HWND,
            ctypes.c_int,
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPCWSTR,
        )

        def _call_shell_api() -> Path:
            # windll is Windows-only; code path is guarded by is_windows()
            sh_get_folder_path = ctypes.windll.shell32.SHGetFolderPathW  # pyright: ignore[reportAttributeAccessIssue]
            sh_get_folder_path.argtypes = sh_get_folder_path_argtypes

            path_buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            result = sh_get_folder_path(0, csidl, 0, 0, path_buf)
            if result != 0:  # S_OK is 0; non-zero is an HRESULT error code
                msg = f"Windows Shell API SHGetFolderPathW failed for CSIDL {csidl}: HRESULT {result}"
                raise WindowsSpecialFolderError(msg)
            return Path(path_buf.value)

        try:
            return _call_shell_api()
        except WindowsSpecialFolderError:
            raise
        except Exception as e:  # Broad catch: ctypes/Shell API can raise many types
            msg = f"Windows Shell API SHGetFolderPathW failed for CSIDL {csidl}: {e}"
            raise WindowsSpecialFolderError(msg) from e

    def _expand_path(self, path_str: str) -> Path:
        """Expand a path string, handling tilde, environment variables, and special folders.

        Handles Windows special folders (like Desktop) that may be redirected to OneDrive
        by using Windows Shell API (SHGetFolderPathW) to get the actual system paths.

        Args:
            path_str: Path string that may contain ~, environment variables, or special folder names

        Returns:
            Expanded, absolute Path object. A result that is still relative after expansion is
            anchored on the workspace directory. Windows special folders resolve to their
            actual system paths (e.g. OneDrive redirection).
        """
        resolved = None
        if self.is_windows():
            parts = self.normalize_path_parts_for_special_folder(path_str)
            resolved = self.try_resolve_windows_special_folder(parts)

        # Success path at the end - compute final path and return
        if resolved is not None and resolved.special_path is not None:
            extra_parts: list[str] = resolved.remaining_parts or []
            if extra_parts:
                final_path = resolved.special_path / Path(*extra_parts)
            else:
                final_path = resolved.special_path
        else:
            expanded_vars = os.path.expandvars(path_str)
            expanded_user = os.path.expanduser(expanded_vars)  # noqa: PTH111
            final_path = Path(expanded_user)

        # Anchor a relative expansion result on the workspace directory. Expansion leaves a
        # path relative when the string contains '%' or '$' but no resolvable variable
        # (e.g. "foo%20bar.txt", "report$.txt", "$UNSET_VAR/sub").
        if not final_path.is_absolute():
            final_path = self._get_workspace_path() / final_path

        return resolve_path_safely(final_path)

    def resolve_file_path(self, path_str: str, *, workspace_only: bool = False) -> Path:
        """Resolve a file path, handling absolute, relative, and tilde paths.

        Args:
            path_str: Path string that may be absolute, start with ~, or be a path relative
                to the workspace directory
            workspace_only: If True and path is invalid, fall back to workspace directory

        Returns:
            Absolute, resolved Path object. Relative paths resolve against the workspace
            directory.
        """
        try:
            if path_needs_expansion(path_str):
                return self._expand_path(path_str)
            return resolve_path_safely(self._get_workspace_path() / path_str)
        except (ValueError, RuntimeError):
            if workspace_only:
                msg = f"Path '{path_str}' not found, using workspace directory: {self._get_workspace_path()}"
                logger.warning(msg)
                return self._get_workspace_path()
            # Re-raise the exception for non-workspace mode
            raise

    def resolve_macro_path_to_string(
        self, macro_path: MacroPath, workflow: "WorkflowRuntime | None", *, failure_log_level: int = logging.ERROR
    ) -> str | MacroResolutionFailure:
        """Resolve MacroPath to absolute string via ProjectManager, for `workflow`.

        Pure resolver: routes through ``GetPathForMacroRequest`` so project
        directories, builtins, and env vars are applied uniformly. No
        write-policy awareness lives here — auto-index seeding for CREATE_NEW
        writes happens in ``FileWriter.on_write_file_request`` instead, where the policy
        is in scope.

        Args:
            macro_path: MacroPath containing parsed macro and variables
            workflow: The workflow `{workflow_dir}` resolves for, or None
            failure_log_level: Log level for the failure result's
                ``result_details``. Defaults to ``logging.ERROR`` — the
                natural level for a failed resolve. Callers that treat a
                resolution failure as an expected signal (e.g. the
                ``FileWriter.on_write_file_request`` seed-and-retry path, whose first
                attempt is intentionally probing for a missing sequence slot)
                should pass ``logging.DEBUG`` so the noise doesn't surface as
                a user-facing ERROR when the fallback succeeds.

        Returns:
            MacroResolutionFailure: Details about resolution failure (missing variables, etc.)
            str: Successfully resolved absolute path string (success path; last)
        """
        result = self.engine.project_manager.get_path_for_macro_in(
            workflow,
            GetPathForMacroRequest(
                parsed_macro=macro_path.parsed_macro,
                variables=macro_path.variables,
                failure_log_level=failure_log_level,
                broadcast_result=False,
            ),
        )
        if not isinstance(result, GetPathForMacroResultSuccess):
            missing = getattr(result, "missing_variables", None)
            return MacroResolutionFailure(
                failure_reason=MacroResolutionFailureReason.MISSING_REQUIRED_VARIABLES,
                variable_name=None,
                missing_variables=missing,
                error_details=str(getattr(result, "result_details", "")),
            )
        return str(result.absolute_path)

    def _validate_workspace_path(self, path: Path) -> tuple[bool, Path]:
        """Check if a path is within workspace and return relative path if it is.

        Args:
            path: Path to validate

        Returns:
            Tuple of (is_workspace_path, relative_or_absolute_path)
        """
        workspace = self.engine.config_manager.workspace_path

        # Canonicalize both sides so ~ / env vars / symlinks / relative spellings
        # all compare equal. Non-existent paths don't raise; the resolvable
        # prefix is resolved and the remainder is appended verbatim.
        path = canonicalize_for_identity(path)
        workspace = canonicalize_for_identity(workspace)

        msg = f"Validating path: {path} against workspace: {workspace}"
        logger.debug(msg)

        try:
            relative = path.relative_to(workspace)
        except ValueError:
            msg = f"Path is outside workspace: {path}"
            logger.debug(msg)
            return False, path

        msg = f"Path is within workspace, relative path: {relative}"
        logger.debug(msg)
        return True, relative

    def resolve_path_safely(self, path: Path) -> Path:
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
        return resolve_path_safely(path)

    def sanitize_path_string(self, path: str | Path | Any) -> str | Any:
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
            path: Path string, Path object, or any other type to sanitize

        Returns:
            Sanitized path string, or original value if not a string/Path
        """
        return sanitize_path_string(path)

    def normalize_path_for_platform(self, path: Path) -> str:
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
        return normalize_path_for_platform(path)

    @staticmethod
    def strip_surrounding_quotes(path_str: str) -> str:
        """Strip surrounding quotes only if they match (from 'Copy as Pathname').

        Args:
            path_str: The path string to process

        Returns:
            Path string with surrounding quotes removed if present
        """
        return strip_surrounding_quotes(path_str)

    @staticmethod
    def format_command_line(args: list[str]) -> str:
        """Format a list of arguments as a single command-line string safe to copy-paste into a shell.

        Uses subprocess.list2cmdline on Windows and shlex.quote on Unix; quotes are added
        only when required for correct parsing (e.g. paths with spaces).

        Args:
            args: List of command and arguments (e.g. [sys.executable, script_path]).

        Returns:
            Single string that can be pasted into a terminal.
        """
        if not args:
            return ""
        if OSManager.is_windows():
            return subprocess.list2cmdline(args)

        import shlex

        return " ".join(shlex.quote(arg) for arg in args)

    @staticmethod
    def platform() -> str:
        """Get `sys.platform` as-is, spellings and all ("win32", "darwin", "linux").

        For a value collapsed onto the platforms we support, use `platform_name()`.
        """
        return sys.platform

    @staticmethod
    def is_windows() -> bool:
        return os_utils.is_windows()

    @staticmethod
    def is_mac() -> bool:
        return os_utils.is_mac()

    @staticmethod
    def is_linux() -> bool:
        return os_utils.is_linux()

    @staticmethod
    def platform_name() -> str:
        """Get the platform as a `Platform` value, for anything keyed by which OS we are on.

        Unlike `platform()`, this collapses the `sys.platform` spellings onto the three
        platforms we support. See `current_platform()`.
        """
        return current_platform()

    def replace_process(self, args: list[Any]) -> None:
        """Replace the current process with a new one.

        Args:
            args: The command and arguments to execute.
        """
        if self.is_windows():
            # excecvp is a nightmare on Windows, so we use subprocess.Popen instead
            # https://stackoverflow.com/questions/7004687/os-exec-on-windows
            subprocess.Popen(args)  # noqa: S603
            sys.exit(0)
        else:
            sys.stdout.flush()  # Recommended here https://docs.python.org/3/library/os.html#os.execvpe
            os.execvp(args[0], args)  # noqa: S606

    @handles(OpenAssociatedFileRequest)
    def on_open_associated_file_request(self, request: OpenAssociatedFileRequest) -> ResultPayload:  # noqa: PLR0911, PLR0912, C901
        # Validate that exactly one of path_to_file or file_entry is provided
        if request.path_to_file is None and request.file_entry is None:
            msg = "Either path_to_file or file_entry must be provided"
            return OpenAssociatedFileResultFailure(failure_reason=FileIOFailureReason.INVALID_PATH, result_details=msg)

        if request.path_to_file is not None and request.file_entry is not None:
            msg = "Only one of path_to_file or file_entry should be provided, not both"
            return OpenAssociatedFileResultFailure(failure_reason=FileIOFailureReason.INVALID_PATH, result_details=msg)

        # Get the file path to open
        if request.file_entry is not None:
            # Use the path from the FileSystemEntry
            file_path_str = request.file_entry.path
        elif request.path_to_file is not None:
            # Use the provided path_to_file
            file_path_str = request.path_to_file
        else:
            # This should never happen due to validation above, but type checker needs it
            msg = "No valid file path provided"
            return OpenAssociatedFileResultFailure(failure_reason=FileIOFailureReason.INVALID_PATH, result_details=msg)

        # At this point, file_path_str is guaranteed to be a string
        if file_path_str is None:
            msg = "No valid file path provided"
            return OpenAssociatedFileResultFailure(failure_reason=FileIOFailureReason.INVALID_PATH, result_details=msg)

        # Sanitize and validate the path (file or directory)
        try:
            # Resolve the path (no workspace fallback for open requests)
            path = self.resolve_file_path(file_path_str, workspace_only=False)
        except (ValueError, RuntimeError):
            details = f"Invalid file path: '{file_path_str}'"
            return OpenAssociatedFileResultFailure(
                failure_reason=FileIOFailureReason.INVALID_PATH, result_details=details
            )

        if not path.exists():
            details = f"Path does not exist: '{path}'"
            return OpenAssociatedFileResultFailure(
                failure_reason=FileIOFailureReason.FILE_NOT_FOUND, result_details=details
            )

        logger.debug("Attempting to open path: %s on platform: %s", path, sys.platform)

        try:
            raw_platform = sys.platform
            if self.is_windows():
                # Linter complains but this is the recommended way on Windows
                # We can ignore this warning as we've validated the path
                #
                # NOTE: do NOT pass normalize_path_for_platform(path) here. os.startfile is
                # backed by ShellExecute, which does not understand the \\?\ extended-length
                # prefix that normalize_path_for_platform now applies unconditionally on
                # Windows -- a prefixed path makes ShellExecute fail to open the file. The
                # path is already validated to exist above, so hand it over unprefixed.
                os.startfile(os.fspath(path))  # noqa: S606 # pyright: ignore[reportAttributeAccessIssue]
                logger.debug("Opened path on Windows: %s", path)
            elif self.is_mac():
                # On macOS, open should be in a standard location
                subprocess.run(  # noqa: S603
                    ["/usr/bin/open", normalize_path_for_platform(path)],
                    check=True,  # Explicitly use check
                    capture_output=True,
                    text=True,
                )
                logger.debug("Opened path on macOS: %s", path)
            elif self.is_linux():
                # Use full path to xdg-open to satisfy linter
                # Common locations for xdg-open:
                xdg_paths = ["/usr/bin/xdg-open", "/bin/xdg-open", "/usr/local/bin/xdg-open"]

                xdg_path = next((p for p in xdg_paths if Path(p).exists()), None)
                if not xdg_path:
                    details = "xdg-open not found in standard locations"
                    return OpenAssociatedFileResultFailure(
                        failure_reason=FileIOFailureReason.IO_ERROR, result_details=details
                    )

                subprocess.run(  # noqa: S603
                    [xdg_path, normalize_path_for_platform(path)],
                    check=True,  # Explicitly use check
                    capture_output=True,
                    text=True,
                )
                logger.debug("Opened path on Linux: %s", path)
            else:
                details = f"Unsupported platform: '{raw_platform}'"
                return OpenAssociatedFileResultFailure(
                    failure_reason=FileIOFailureReason.IO_ERROR, result_details=details
                )

            return OpenAssociatedFileResultSuccess(result_details="File opened successfully in associated application.")
        except subprocess.CalledProcessError as e:
            details = (
                f"Process error when opening file: return code={e.returncode}, stdout={e.stdout}, stderr={e.stderr}"
            )
            return OpenAssociatedFileResultFailure(failure_reason=FileIOFailureReason.IO_ERROR, result_details=details)
        except Exception as e:
            details = f"Exception occurred when trying to open path: {e}"
            return OpenAssociatedFileResultFailure(failure_reason=FileIOFailureReason.UNKNOWN, result_details=details)

    @handles(LaunchExternalViewerRequest)
    def on_launch_external_viewer_request(self, request: LaunchExternalViewerRequest) -> ResultPayload:  # noqa: PLR0911
        executable_key = f"{request.config_category}.viewer_executable"
        args_key = f"{request.config_category}.viewer_args"
        attempt = f"Attempted to open '{request.path_to_file}' in the external viewer"

        try:
            path = self.resolve_file_path(request.path_to_file, workspace_only=False)
        except (ValueError, RuntimeError):
            details = f"{attempt}. Failed because the path is not valid."
            logger.info(details)
            return LaunchExternalViewerResultFailure(
                failure_reason=FileIOFailureReason.INVALID_PATH, result_details=details
            )

        if not path.exists():
            details = f"{attempt}. Failed because the file does not exist."
            logger.info(details)
            return LaunchExternalViewerResultFailure(
                failure_reason=FileIOFailureReason.FILE_NOT_FOUND, result_details=details
            )

        viewer_executable = self.engine.config_manager.get_config_value(executable_key, default="")
        viewer_executable = (viewer_executable or "").strip()
        if not viewer_executable:
            if not request.fallback_to_os_default:
                details = f"{attempt}. Failed because no viewer is set in the '{executable_key}' setting."
                logger.info(details)
                return LaunchExternalViewerResultFailure(
                    failure_reason=FileIOFailureReason.NOT_CONFIGURED, result_details=details
                )
            return self._open_with_os_default_for_viewer(path, attempt)

        viewer_args = self.engine.config_manager.get_config_value(args_key, default="")
        try:
            viewer_arg_list = self._split_viewer_args(viewer_args or "")
        except ValueError as e:
            details = f"{attempt}. Failed because the '{args_key}' setting has unbalanced quotes: {e}"
            logger.info(details)
            return LaunchExternalViewerResultFailure(
                failure_reason=FileIOFailureReason.INVALID_PATH, result_details=details
            )

        # The OS path is passed unprefixed: viewers do not understand the Windows \\?\ long-path form.
        argv = [viewer_executable, *viewer_arg_list, os.fspath(path)]
        try:
            self._spawn_detached(argv)
        except FileNotFoundError:
            details = f"{attempt}. Failed because the viewer '{viewer_executable}' was not found."
            logger.info(details)
            return LaunchExternalViewerResultFailure(
                failure_reason=FileIOFailureReason.FILE_NOT_FOUND, result_details=details
            )
        except PermissionError:
            details = (
                f"{attempt}. Failed because the viewer '{viewer_executable}' could not be run (permission denied)."
            )
            logger.info(details)
            return LaunchExternalViewerResultFailure(
                failure_reason=FileIOFailureReason.PERMISSION_DENIED, result_details=details
            )
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            details = f"{attempt}. Failed because the viewer '{viewer_executable}' could not be started: {e}"
            logger.info(details)
            return LaunchExternalViewerResultFailure(
                failure_reason=FileIOFailureReason.IO_ERROR, result_details=details
            )

        return LaunchExternalViewerResultSuccess(
            used_fallback=False, result_details=f"Opened '{path}' in the external viewer '{viewer_executable}'."
        )

    def _open_with_os_default_for_viewer(self, path: Path, attempt: str) -> ResultPayload:
        open_result = self.on_open_associated_file_request(OpenAssociatedFileRequest(path_to_file=os.fspath(path)))
        if isinstance(open_result, OpenAssociatedFileResultFailure):
            details = f"{attempt}. No viewer is set, and opening it with the default application failed: {open_result.result_details}"
            return LaunchExternalViewerResultFailure(failure_reason=open_result.failure_reason, result_details=details)
        return LaunchExternalViewerResultSuccess(
            used_fallback=True,
            result_details=f"No external viewer is set, so '{path}' was opened with the default application.",
        )

    def _split_viewer_args(self, viewer_args: str) -> list[str]:
        """Split the configured viewer arguments with shell-style quoting.

        POSIX mode would treat Windows backslashes as escapes, but non-POSIX mode keeps the
        quotes on each token, so they are stripped here before the tokens reach Popen.
        """
        if not self.is_windows():
            return shlex.split(viewer_args)
        tokens = shlex.split(viewer_args, posix=False)
        unquoted = []
        for token in tokens:
            if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":  # noqa: PLR2004
                unquoted.append(token[1:-1])
            else:
                unquoted.append(token)
        return unquoted

    def _spawn_detached(self, argv: list[str]) -> None:
        """Start ``argv`` without waiting for it, so it outlives the engine and never blocks it."""
        if self.is_windows():
            creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # pyright: ignore[reportAttributeAccessIssue]
            subprocess.Popen(  # noqa: S603
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            return
        subprocess.Popen(  # noqa: S603
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def _is_hidden(self, dir_entry: os.DirEntry, stat_result: os.stat_result | None = None) -> bool:
        """Check if a directory entry is hidden in an OS-independent way.

        On Unix/Linux/macOS: Files are considered hidden if their name starts with a dot (.).
        On Windows: Files have a special "hidden" file attribute (FILE_ATTRIBUTE_HIDDEN).

        Args:
            dir_entry: The directory entry to check
            stat_result: Optional pre-fetched stat result (to avoid redundant stat() calls on Windows)

        Returns:
            True if the entry is hidden, False otherwise
        """
        if sys.platform == "win32":
            # Windows: Check name prefix first (fast heuristic for most hidden files)
            # Most hidden files on Windows have dot prefix, so this avoids many stat() calls
            if dir_entry.name.startswith("."):
                return True
            # For files without dot prefix, check FILE_ATTRIBUTE_HIDDEN via stat()
            if stat_result is None:
                stat_result = dir_entry.stat(follow_symlinks=False)
            return bool(stat_result.st_file_attributes & stat.FILE_ATTRIBUTE_HIDDEN)
        # Unix/Linux/macOS: Files are hidden if name starts with dot
        return dir_entry.name.startswith(".")

    def _detect_mime_type(self, file_path: Path) -> str | None:
        """Detect MIME type for a file. Returns None for directories or if detection fails.

        Args:
            file_path: Original file path (used for is_dir() check and filename extraction)
        """
        if file_path.is_dir():
            return None

        # mimetypes.guess_type() only needs the filename, not the full path
        # Using just the filename is ~2x faster and avoids path normalization overhead
        filename = file_path.name
        try:
            mime_type, _ = mimetypes.guess_type(filename, strict=True)
        except Exception as e:
            msg = f"MIME type detection failed for {file_path} (filename: {filename}): {e}"
            logger.warning(msg)
            return "text/plain"

        if mime_type is None:
            mime_type = "text/plain"
        return mime_type

    @handles(ListDirectoryRequest)
    def on_list_directory_request(  # noqa: C901, PLR0911, PLR0912, PLR0915
        self, request: ListDirectoryRequest, workflow: "WorkflowRuntime | None" = None
    ) -> ResultPayload:
        """Handle a request to list directory contents. Macros in the path resolve for `workflow`."""
        try:
            # Resolve path: strings support macro syntax like "{project_dir}".
            directory_path_str: str | None
            if request.directory_path is not None:
                try:
                    directory_path_str = self._resolve_directory_location(request.directory_path, workflow)
                except FileLoadError as e:
                    return ListDirectoryResultFailure(
                        failure_reason=e.failure_reason,
                        result_details=e.result_details,
                    )
            else:
                directory_path_str = None

            # Get the directory path to list
            if directory_path_str is None:
                directory = self._get_workspace_path()
            elif path_needs_expansion(directory_path_str):
                directory = self._expand_path(directory_path_str)
            else:
                directory = resolve_path_safely(self._get_workspace_path() / directory_path_str)

            # Check if directory exists
            if not directory.exists():
                msg = f"Directory does not exist: {directory}"
                return ListDirectoryResultFailure(failure_reason=FileIOFailureReason.FILE_NOT_FOUND, result_details=msg)
            if not directory.is_dir():
                msg = f"Path is not a directory: {directory}"
                return ListDirectoryResultFailure(failure_reason=FileIOFailureReason.INVALID_PATH, result_details=msg)

            # Check workspace constraints
            is_workspace_path, relative_or_abs_path = self._validate_workspace_path(directory)
            if request.workspace_only and not is_workspace_path:
                msg = f"Directory is outside workspace: {directory}"
                return ListDirectoryResultFailure(failure_reason=FileIOFailureReason.INVALID_PATH, result_details=msg)

            # Cache workspace path and resolved workspace to avoid repeated lookups/resolutions
            # Only resolve workspace if we need it for relative paths or absolute paths
            need_relative_paths = request.workspace_only is True
            workspace_path = self.engine.config_manager.workspace_path
            if need_relative_paths or request.include_absolute_path:
                resolved_workspace = canonicalize_for_identity(workspace_path)
            else:
                resolved_workspace = None

            entries = []
            try:
                # Pre-compute whether we need stat() calls (constant for all entries)
                need_stat_for_metadata = request.include_size or request.include_modified_time
                # On Windows, we need stat() to check FILE_ATTRIBUTE_HIDDEN when filtering hidden files
                # (only for files without dot prefix, since dot-prefix files are handled by name check)
                need_stat_for_hidden = not request.show_hidden and sys.platform == "win32"

                # Use os.scandir() instead of Path.iterdir() for better performance
                # os.scandir() is ~3.7x faster and provides cached stat info
                with os.scandir(str(directory)) as scan_iter:
                    for dir_entry in scan_iter:
                        # Initialize stat - we'll get it once if needed for hidden check and/or metadata
                        stat = None

                        # Skip hidden files if not requested (OS-independent check)
                        if not request.show_hidden:
                            # On Windows, files without dot prefix need stat() to check FILE_ATTRIBUTE_HIDDEN
                            # Get stat() once if needed (for hidden check and/or metadata)
                            if need_stat_for_hidden and not dir_entry.name.startswith("."):
                                stat = dir_entry.stat(follow_symlinks=False)

                            if self._is_hidden(dir_entry, stat_result=stat):
                                continue

                        # Apply pattern filter if specified, or create Path object if needed
                        if request.pattern is not None:
                            # Convert DirEntry to Path for pattern matching
                            entry_path_obj = Path(dir_entry.path)
                            if not entry_path_obj.match(request.pattern):
                                continue
                        elif request.include_absolute_path or request.include_mime_type or need_relative_paths:
                            # Only create Path object if we need it
                            entry_path_obj = Path(dir_entry.path)
                        else:
                            entry_path_obj = None

                        try:
                            # Get stat() if needed for metadata (reuse if we already have it from hidden check)
                            if need_stat_for_metadata and stat is None:
                                stat = dir_entry.stat(follow_symlinks=False)

                            # Use the path as seen (preserve symlinks - don't resolve to target)
                            # dir_entry.path is the full path to the entry (symlink path if it's a symlink)
                            if request.include_absolute_path or need_relative_paths:
                                if entry_path_obj is None:
                                    entry_path_obj = Path(dir_entry.path)
                                entry_path_absolute = entry_path_obj.absolute()
                            else:
                                entry_path_absolute = None

                            # Determine entry_path based on what we need
                            if (
                                need_relative_paths
                                and entry_path_absolute is not None
                                and resolved_workspace is not None
                            ):
                                try:
                                    relative = entry_path_absolute.relative_to(resolved_workspace)
                                    entry_path = relative
                                except ValueError:
                                    # Entry is outside workspace
                                    entry_path = entry_path_absolute
                            elif request.include_absolute_path and entry_path_absolute is not None:
                                entry_path = entry_path_absolute
                            else:
                                # Use the path from dir_entry (may be relative or absolute depending on system)
                                entry_path = dir_entry.path

                            absolute_path_str = (
                                str(entry_path_absolute)
                                if entry_path_absolute is not None and request.include_absolute_path
                                else ""
                            )

                            # Only detect MIME type if requested
                            mime_type = None
                            if request.include_mime_type:
                                if entry_path_obj is None:
                                    entry_path_obj = Path(dir_entry.path)
                                # Use resolved_entry if available, otherwise just entry_path_obj
                                mime_type = self._detect_mime_type(entry_path_obj)

                            # Determine size and modified_time values
                            entry_size = 0
                            if stat and request.include_size:
                                entry_size = stat.st_size

                            entry_modified_time = 0.0
                            if stat and request.include_modified_time:
                                entry_modified_time = stat.st_mtime

                            entries.append(
                                FileSystemEntry(
                                    name=dir_entry.name,
                                    path=str(entry_path),
                                    is_dir=dir_entry.is_dir(),
                                    size=entry_size,
                                    modified_time=entry_modified_time,
                                    mime_type=mime_type,
                                    absolute_path=absolute_path_str,
                                )
                            )
                        except (OSError, PermissionError) as e:
                            msg = f"Could not process entry {dir_entry.name}: {e}"
                            logger.warning(msg)
                            continue

            except PermissionError as e:
                msg = f"Permission denied listing directory {directory}: {e}"
                return ListDirectoryResultFailure(
                    failure_reason=FileIOFailureReason.PERMISSION_DENIED, result_details=msg
                )
            except OSError as e:
                msg = f"I/O error listing directory {directory}: {e}"
                return ListDirectoryResultFailure(failure_reason=FileIOFailureReason.IO_ERROR, result_details=msg)

            # Group sequence files into Sequence objects when requested.
            sequences: list[Sequence] = []
            if request.group_sequences:
                options = request.sequence_options or SequenceScanOptions()
                bare_names = [e.name for e in entries if not e.is_dir]
                seq_directory = str(relative_or_abs_path) if request.workspace_only else str(directory)
                try:
                    sequences, consumed = scan_sequences_from_filenames(bare_names, seq_directory, options)
                except InvalidSubsetBoundsError as e:
                    return ListDirectoryResultFailure(
                        failure_reason=SequenceScanFailureReason.INVALID_BOUNDS,
                        result_details=str(e),
                    )
                except MissingItemError as e:
                    summary = describe_missing_items(e.numbers)
                    return ListDirectoryResultFailure(
                        failure_reason=SequenceScanFailureReason.ABORTED_AT_GAP,
                        missing_item_numbers=e.numbers,
                        result_details=(
                            f"Attempted to list directory {str(directory)!r} with group_sequences=True, "
                            f"policy=ABORT. Failed because {summary}."
                        ),
                    )
                entries = [e for e in entries if e.name not in consumed]

            # Return appropriate path format based on mode
            if request.workspace_only:
                # In workspace mode, return relative path if within workspace, absolute if outside
                return ListDirectoryResultSuccess(
                    entries=entries,
                    current_path=str(relative_or_abs_path),
                    is_workspace_path=is_workspace_path,
                    sequences=sequences,
                    result_details="Directory listing retrieved successfully.",
                )
            # In system-wide mode, always return the full absolute path
            return ListDirectoryResultSuccess(
                entries=entries,
                current_path=str(directory),
                is_workspace_path=is_workspace_path,
                sequences=sequences,
                result_details="Directory listing retrieved successfully.",
            )

        except Exception as e:
            msg = f"Unexpected error in list_directory: {type(e).__name__}: {e}"
            return ListDirectoryResultFailure(failure_reason=FileIOFailureReason.UNKNOWN, result_details=msg)

    def _resolve_directory_location(self, directory_path: str, workflow: "WorkflowRuntime | None") -> str:
        """Resolve a listed directory's location; a path with macros resolves for `workflow`.

        Raises:
            FileLoadError: The path's macros could not be resolved.
        """
        try:
            parsed = ParsedMacro(directory_path)
        except MacroSyntaxError:
            return File(directory_path).resolve()
        if not parsed.get_variables():
            return File(directory_path).resolve()
        result = self.engine.project_manager.get_path_for_macro_in(
            workflow, GetPathForMacroRequest(parsed_macro=parsed, variables={})
        )
        if not isinstance(result, GetPathForMacroResultSuccess):
            raise file_load_error_from_result(result)
        return str(result.absolute_path)

    def _detect_mime_type_from_location(self, location: str) -> str:
        """Detect MIME type from location string.

        Args:
            location: URL, data URI, or file path

        Returns:
            MIME type string (default: "text/plain")
        """
        if location.startswith("data:"):
            # Extract MIME type from data URI (e.g., "data:image/png;base64,...")
            if ";" in location:
                mime_part = location.split(";", maxsplit=1)[0].replace("data:", "")
                return mime_part or "text/plain"
            return "text/plain"

        # Use mimetypes module for URLs and paths
        mime_type, _ = mimetypes.guess_type(location, strict=True)
        return mime_type or "text/plain"

    def _is_text_content(self, content: bytes, mime_type: str) -> bool:
        """Check if content is text based on MIME type and content analysis.

        Args:
            content: File content as bytes
            mime_type: MIME type string

        Returns:
            True if content should be treated as text
        """
        # Check MIME type first
        if mime_type.startswith(("text/", "application/json", "application/xml", "application/yaml")):
            return True

        # For binary MIME types, return False
        if mime_type.startswith(("image/", "audio/", "video/", "application/octet-stream")):
            return False

        # For unknown types, try detecting from content
        try:
            content.decode("utf-8")
        except (UnicodeDecodeError, AttributeError):
            return False
        else:
            return True

    async def _read_via_driver(
        self, location: str, request: ReadFileRequest
    ) -> ReadFileResultSuccess | ReadFileResultFailure:
        """Read file using FileDriver system.

        Driver handles validation (existence, permissions, format).
        OSManager adds metadata enrichment and thumbnail generation for images.

        Args:
            location: Location string (URL, data URI, cloud path, or local path)
            request: ReadFileRequest containing options like should_transform_image_content_to_thumbnail

        Returns:
            ReadFileResultSuccess with content and metadata, or ReadFileResultFailure
        """
        try:
            # Get appropriate driver
            driver = FileDriverRegistry.get_driver(location)

            # Driver validates and reads
            content = await driver.read(location, timeout=120.0)

            # Add basic metadata
            file_size = len(content)
            mime_type = self._detect_mime_type_from_location(location)
            is_text = self._is_text_content(content, mime_type)
            encoding = "utf-8" if is_text else None

            # Handle image thumbnail generation (if requested)
            if mime_type.startswith("image/") and request.should_transform_image_content_to_thumbnail and not is_text:
                content = await self._generate_thumbnail_from_image_content(content, location, mime_type)
                # Thumbnail returns a string (URL or data URI), not bytes
                decoded_content: str | bytes = content
                encoding = None
            # Decode text content to str (API contract: text files return str, binary returns bytes)
            elif is_text and encoding:
                try:
                    decoded_content = content.decode(encoding)
                except UnicodeDecodeError:
                    # If decoding fails, fall back to binary
                    decoded_content = content
                    encoding = None
            else:
                decoded_content = content

            return ReadFileResultSuccess(
                content=decoded_content,
                file_size=file_size,
                mime_type=mime_type,
                encoding=encoding,
                compression_encoding=None,
                result_details="File read successfully.",
            )
        except (FileDriverNotFoundError, ValueError) as e:
            return ReadFileResultFailure(
                failure_reason=FileIOFailureReason.INVALID_PATH,
                result_details=str(e),
            )
        except FileNotFoundError as e:
            return ReadFileResultFailure(failure_reason=FileIOFailureReason.FILE_NOT_FOUND, result_details=str(e))
        except PermissionError as e:
            return ReadFileResultFailure(failure_reason=FileIOFailureReason.PERMISSION_DENIED, result_details=str(e))
        except IsADirectoryError as e:
            return ReadFileResultFailure(failure_reason=FileIOFailureReason.IS_DIRECTORY, result_details=str(e))
        except Exception as e:
            return ReadFileResultFailure(
                failure_reason=FileIOFailureReason.IO_ERROR, result_details=f"Error reading from {location}: {e}"
            )

    @handles(ReadFileRequest)
    async def on_read_file_request(self, request: ReadFileRequest) -> ResultPayload:
        """Handle a request to read file contents with automatic text/binary detection.

        All file reading is delegated to FileDriver system.
        """
        # Get location string from request
        if request.file_entry is not None:
            location = request.file_entry.path
        elif request.file_path is not None:
            location = request.file_path
        else:
            msg = "Either file_path or file_entry must be provided"
            return ReadFileResultFailure(failure_reason=FileIOFailureReason.INVALID_PATH, result_details=msg)

        # Sanitize path string (basic cleanup)
        location = sanitize_path_string(location)

        # Read via driver system (driver handles all validation and I/O)
        return await self._read_via_driver(location, request)

    async def _generate_thumbnail_from_image_content(
        self, content: bytes, file_path: Path | str, mime_type: str
    ) -> str:
        """Handle image content by creating previews or returning static URLs.

        The workspace check reads engine state, so it runs on the loop; only the image encode
        runs on a worker thread. Pillow is tried first regardless of file size, since decoding a
        large-but-valid image (a big JPEG or TIFF, say) is cheap and still worth a thumbnail. Only
        when Pillow can't decode the file does size matter: under ``_MAX_PREVIEWABLE_IMAGE_BYTES``
        the original bytes are returned undecoded; over it, a no-preview sentinel is returned
        instead, since base64-encoding a multi-hundred-MB file whole is what has hung the engine
        on large or multi-part images Pillow can't open (EXR sequences, for one).

        Args:
            content: Image bytes
            file_path: File location (Path object, local path string, URL, or data URI)
            mime_type: Image MIME type

        Returns:
            URL string ({static_server_base_url}/workspace/...) or data URI
        """
        # Store original bytes for preview creation
        original_image_bytes = content

        # Check if file is already in the static files directory (only for local paths)
        try:
            # Convert to Path object if it's a string
            path_obj = Path(file_path) if isinstance(file_path, str) else file_path

            # Only check workspace directory for absolute local paths
            if path_obj.is_absolute():
                config_manager = self.engine.config_manager
                static_dir = config_manager.workspace_path

                try:
                    # Check if file is within the static files directory
                    file_relative_to_static = path_obj.relative_to(static_dir)
                except ValueError:
                    # File is not in static directory, continue to preview creation
                    pass
                else:
                    # File is in static directory, construct URL directly
                    static_base_url = self.engine.static_files_manager.static_server_base_url
                    static_url = f"{static_base_url}/workspace/{file_relative_to_static}"
                    msg = f"Image already in workspace directory, returning URL: {static_url}"
                    logger.debug(msg)
                    return static_url
        except (ValueError, OSError, TypeError):
            # Not a valid local path (might be URL or data URI), continue to preview
            pass

        path_obj = Path(file_path) if isinstance(file_path, str) else file_path

        pillow_preview = await to_thread(self._image_bytes_to_preview_data_url, original_image_bytes)
        if pillow_preview is not None:
            return pillow_preview

        if len(original_image_bytes) > self._MAX_PREVIEWABLE_IMAGE_BYTES:
            # Pillow couldn't decode this file, and it's large enough that base64-encoding it
            # whole is the actual cost that has hung the engine on large/multi-part images it
            # can't open (EXR sequences, for one). A 200+ MB EXR sequence frame reaches this
            # unconditionally via the sequence-frame loader, which bypasses the File Browser
            # dialog's own 10MB client-side preview cutoff. Skip encoding it entirely.
            logger.debug(
                "%s exceeds the %d byte preview cap and Pillow could not decode it; skipping preview.",
                path_obj,
                self._MAX_PREVIEWABLE_IMAGE_BYTES,
            )
            return self._NO_PREVIEW_DATA_URL

        # Pillow couldn't decode this file, but it's small enough that returning it undecoded
        # costs nothing worth guarding against.
        logger.debug("No preview could be generated for %s; returning the original bytes.", path_obj)
        return f"data:{mime_type};base64,{base64.b64encode(original_image_bytes).decode('utf-8')}"

    @staticmethod
    def _image_bytes_to_preview_data_url(image_bytes: bytes) -> str | None:
        """Encode a small WEBP preview of the image as a data URL. CPU-bound and touches no engine state.

        Returns:
            The preview data URL, or None if Pillow could not decode the image (the caller then
            returns the original bytes under the size cap, or a no-preview sentinel over it).
        """
        preview_data_url = create_image_preview_from_bytes(
            image_bytes,  # type: ignore[arg-type]
            max_width=200,
            max_height=200,
            quality=85,
            image_format="WEBP",
        )

        if preview_data_url:
            logger.debug("Image preview created (file not moved)")
            return preview_data_url

        return None

    @handles(GetNextUnusedFilenameRequest)
    def on_get_next_unused_filename_request(self, request: GetNextUnusedFilenameRequest) -> ResultPayload:
        """Handle a request to find the next available filename (preview only - no file creation)."""
        # Handle string paths specially: try base path first, then indexed
        if isinstance(request.file_path, str):
            # First, check if base path is available
            try:
                base_path = self.resolve_file_path(request.file_path, workspace_only=False)
            except (ValueError, RuntimeError) as e:
                msg = f"Invalid path: {e}"
                return GetNextUnusedFilenameResultFailure(
                    failure_reason=FileIOFailureReason.INVALID_PATH,
                    result_details=msg,
                )

            if not base_path.exists():
                # Base filename is available - use it
                return GetNextUnusedFilenameResultSuccess(
                    available_filename=str(base_path),
                    index_used=None,
                    result_details="Found available filename (no index needed)",
                )

            # Base filename taken - convert to indexed MacroPath and scan
            macro_path = self.indexed_filenames.convert_str_path_to_macro_with_index(request.file_path)
        else:
            # MacroPath provided directly
            macro_path = request.file_path

        parsed_macro = macro_path.parsed_macro
        variables = macro_path.variables

        # Identify index variable
        try:
            index_info = self.indexed_filenames.identify_index_variable(parsed_macro, variables)
        except ValueError as e:
            msg = f"Failed to identify index variable in path template: {e}"
            return GetNextUnusedFilenameResultFailure(
                failure_reason=FileIOFailureReason.INVALID_PATH,
                result_details=msg,
            )

        if index_info is None:
            # No unresolved variables - cannot auto-increment
            msg = "No index variable found in path template"
            return GetNextUnusedFilenameResultFailure(
                failure_reason=FileIOFailureReason.INVALID_PATH,
                result_details=msg,
            )

        # Scan for next available index (preview only - no file creation)
        next_index = self.indexed_filenames.scan_for_next_available_index(parsed_macro, variables, index_info)

        # Resolve path with the index
        secrets_manager = self.engine.secrets_manager
        try:
            if next_index is None:
                # Optional index variable with base filename available
                available_filename = parsed_macro.resolve(variables, secrets_manager)
            else:
                # Use indexed filename
                index_vars = {**variables, index_info.info.name: next_index}
                available_filename = parsed_macro.resolve(index_vars, secrets_manager)
        except MacroResolutionError as e:
            msg = f"Failed to resolve path template: {e}"
            return GetNextUnusedFilenameResultFailure(
                failure_reason=FileIOFailureReason.MISSING_MACRO_VARIABLES,
                result_details=msg,
            )

        return GetNextUnusedFilenameResultSuccess(
            available_filename=available_filename,
            index_used=next_index,
            result_details=f"Found available filename with index {next_index}"
            if next_index
            else "Found available filename (no index needed)",
        )

    @handles(GetNextVersionIndexRequest)
    def on_get_next_version_index_request(
        self, request: GetNextVersionIndexRequest, workflow: "WorkflowRuntime | None" = None
    ) -> ResultPayload:
        """Find the next available version index via a single glob pass; project macros resolve for `workflow`."""
        scan_macro_path = self.indexed_filenames.bind_project_variables_for_index_scan(request.macro_path, workflow)
        parsed_macro = scan_macro_path.parsed_macro
        variables = scan_macro_path.variables

        try:
            index_info = self.indexed_filenames.identify_index_variable(parsed_macro, variables)
        except ValueError as e:
            msg = f"Attempted to find next version index. Failed: {e}"
            return GetNextVersionIndexResultFailure(
                failure_reason=FileIOFailureReason.INVALID_PATH,
                result_details=msg,
            )

        if index_info is None:
            msg = "Attempted to find next version index. Failed because no unresolved {_index} variable was found in the macro template."
            return GetNextVersionIndexResultFailure(
                failure_reason=FileIOFailureReason.INVALID_PATH,
                result_details=msg,
            )

        next_index = self.indexed_filenames.scan_for_next_available_index(parsed_macro, variables, index_info)

        return GetNextVersionIndexResultSuccess(
            index=next_index,
            result_details=f"Next available version index is {next_index}"
            if next_index is not None
            else "Base path is available (no index needed)",
        )

    def _copy_file(self, src_path: Path, dest_path: Path) -> int:
        """Copy a single file from source to destination with platform path normalization.

        Args:
            src_path: Source file path (Path object)
            dest_path: Destination file path (Path object)

        Returns:
            Number of bytes copied

        Raises:
            OSError: If copy operation fails
            PermissionError: If permission denied
        """
        # Normalize both paths for platform (handles Windows long paths)
        src_normalized = normalize_path_for_platform(src_path)
        dest_normalized = normalize_path_for_platform(dest_path)

        # Copy file preserving metadata.
        #
        # We deliberately avoid shutil.copy2 here. copy2 copies the contents and
        # then calls shutil.copystat to replicate metadata, which on BSD-derived
        # platforms (macOS) includes BSD file flags via os.chflags(). SMB mounts
        # surface the DOS "archive" attribute as SF_ARCHIVED, and that flag can
        # only be set by root on a local volume -- so chflags(dst, SF_ARCHIVED)
        # raises EPERM. copystat only swallows EOPNOTSUPP/ENOTSUP, not EPERM, so
        # the exception propagates and the whole copy is reported as failed even
        # though the file contents copied fine (see issue #5109).
        #
        # Instead, copy contents explicitly and then best-effort the metadata,
        # ignoring an EPERM raised by the flags step so metadata that can't be
        # replicated on the destination filesystem doesn't fail the copy.
        shutil.copyfile(src_normalized, dest_normalized)
        try:
            shutil.copystat(src_normalized, dest_normalized)
        except PermissionError:
            # chflags() couldn't replicate BSD file flags on the destination
            # (e.g. SF_ARCHIVED on an SMB mount). Contents and the permission
            # bits that matter are already in place; the copy has effectively
            # succeeded, so warn rather than fail.
            logger.warning(
                "Could not replicate all file metadata from %s to %s (EPERM); file contents copied successfully.",
                src_normalized,
                dest_normalized,
            )

        # Return size of copied file
        return Path(src_normalized).stat().st_size

    @staticmethod
    def get_disk_space_info(path: Path) -> DiskSpaceInfo:
        """Get disk space information for a given path.

        Args:
            path: The path to check disk space for.

        Returns:
            DiskSpaceInfo with total, used, and free disk space in bytes.
        """
        stat = shutil.disk_usage(path)
        return DiskSpaceInfo(total=stat.total, used=stat.used, free=stat.free)

    @staticmethod
    def check_available_disk_space(path: Path, required_gb: float) -> bool:
        """Check if there is sufficient disk space available.

        Args:
            path: The path to check disk space for.
            required_gb: The minimum disk space required in GB.

        Returns:
            True if sufficient space is available, False otherwise.
        """
        # Callers routinely pass a target write path whose directory hasn't been
        # created yet (save situations create parent dirs on write). Walk up to
        # the nearest existing ancestor so disk_usage resolves to the same mount
        # the write will land on rather than raising FileNotFoundError.
        probe = path
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            disk_info = OSManager.get_disk_space_info(probe)
            required_bytes = int(required_gb * 1024 * 1024 * 1024)  # Convert GB to bytes
            return disk_info.free >= required_bytes  # noqa: TRY300
        except OSError:
            return False

    @staticmethod
    def format_disk_space_error(path: Path, exception: Exception | None = None) -> str:
        """Format a user-friendly disk space error message.

        Args:
            path: The path where the disk space issue occurred.
            exception: The original exception, if any.

        Returns:
            A formatted error message with disk space information.
        """
        # Mirror check_available_disk_space: if the path is a yet-to-be-created
        # target, probe the nearest existing ancestor so the reported free/used
        # numbers reflect the mount the write would land on.
        probe = path
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            disk_info = OSManager.get_disk_space_info(probe)
            free_gb = disk_info.free / (1024**3)
            used_gb = disk_info.used / (1024**3)
            total_gb = disk_info.total / (1024**3)

            error_msg = f"Insufficient disk space at {path}. "
            error_msg += f"Available: {free_gb:.2f} GB, Used: {used_gb:.2f} GB, Total: {total_gb:.2f} GB. "

            if exception:
                error_msg += f"Error: {exception}"
            else:
                error_msg += "Please free up disk space and try again."

            return error_msg  # noqa: TRY300
        except OSError:
            return f"Could not determine disk space at {path}. Please check disk space manually."

    @staticmethod
    def cleanup_directory_if_needed(full_directory_path: Path, max_size_gb: float) -> bool:
        """Check directory size and cleanup old files if needed.

        Args:
            full_directory_path: Path to the directory to check and clean
            max_size_gb: Target size in GB

        Returns:
            True if cleanup was performed, False otherwise
        """
        if max_size_gb < 0:
            logger.warning(
                "Asked to clean up directory to be below a negative threshold. Overriding to a size of 0 GB."
            )
            max_size_gb = 0

        # Calculate current directory size
        current_size_gb = OSManager._get_directory_size_gb(full_directory_path)

        if current_size_gb <= max_size_gb:
            return False

        logger.info(
            "Directory %s size (%.1f GB) exceeds limit (%s GB). Starting cleanup...",
            full_directory_path,
            current_size_gb,
            max_size_gb,
        )

        # Perform cleanup
        return OSManager._cleanup_old_files(full_directory_path, max_size_gb)

    @staticmethod
    def _get_directory_size_gb(path: Path) -> float:
        """Get total size of directory in GB.

        Args:
            path: Path to the directory

        Returns:
            Total size in GB
        """
        total_size = 0.0

        if not path.exists():
            logger.error("Directory %s does not exist. Skipping cleanup.", path)
            return 0.0

        # os.walk yields file names relative to each iteration's root, so they
        # must be rejoined against that root rather than the top-level path.
        for root, _, files in os.walk(path):
            root_path = Path(root)
            for f in files:
                fp = root_path / f
                if not fp.is_symlink():
                    total_size += fp.stat().st_size
        return total_size / (1024 * 1024 * 1024)  # Convert to GB

    @staticmethod
    def _cleanup_old_files(directory_path: Path, target_size_gb: float) -> bool:
        """Remove oldest files until directory is under target size.

        Args:
            directory_path: Path to the directory to clean
            target_size_gb: Target size in GB

        Returns:
            True if files were removed, False otherwise
        """
        if not directory_path.exists():
            logger.error("Directory %s does not exist. Skipping cleanup.", directory_path)
            return False

        # Get all files with their modification times
        files_with_times: list[tuple[Path, float]] = []

        for file_path in directory_path.rglob("*"):
            if file_path.is_file():
                try:
                    mtime = file_path.stat().st_mtime
                    files_with_times.append((file_path, mtime))
                except (OSError, FileNotFoundError) as err:
                    # Skip files that can't be accessed
                    logger.error(
                        "While cleaning up old files, saw file %s. File could not be accessed; skipping. Error: %s",
                        file_path,
                        err,
                    )
                    continue

        if not files_with_times:
            logger.error(
                "Attempted to clean up files to get below a target directory size, but no suitable files were found that could be deleted."
            )
            return False

        # Sort by modification time (oldest first)
        files_with_times.sort(key=lambda x: x[1])

        # Remove files until we're under the target size
        removed_count = 0

        for file_path, _ in files_with_times:
            try:
                # Delete the file.
                # TODO: Replace with DeleteFileRequest https://github.com/griptape-ai/griptape-nodes-engine/issues/3765
                file_path.unlink()
                removed_count += 1

                # Check if we're now under the target size
                current_size_gb = OSManager._get_directory_size_gb(directory_path)
                if current_size_gb <= target_size_gb:
                    # We're done!
                    break

            except (OSError, FileNotFoundError) as err:
                # Skip files that can't be deleted
                logger.error(
                    "While cleaning up old files, attempted to delete file %s. File could not be deleted; skipping. Deletion error: %s",
                    file_path,
                    err,
                )

        if removed_count > 0:
            final_size_gb = OSManager._get_directory_size_gb(directory_path)
            logger.info(
                "Cleaned up %d old files from %s. Directory size reduced to %.1f GB",
                removed_count,
                directory_path,
                final_size_gb,
            )
        else:
            # None deleted.
            logger.error("Attempted to clean up old files from %s, but no files could be deleted.")

        return removed_count > 0

    @handles(MakeDirectoryRequest)
    def on_make_directory_request(self, request: MakeDirectoryRequest) -> ResultPayload:  # noqa: PLR0911
        """Handle a request to create a directory."""
        sanitized = sanitize_path_string(request.path)
        try:
            dir_path = self.resolve_file_path(sanitized, workspace_only=False)
        except (ValueError, RuntimeError) as e:
            msg = f"Attempted to create directory '{sanitized}'. Failed due to invalid path: {e}"
            return MakeDirectoryResultFailure(failure_reason=FileIOFailureReason.INVALID_PATH, result_details=msg)

        if dir_path.is_file():
            msg = f"Attempted to create directory '{dir_path}'. Failed because a file already exists at that path."
            return MakeDirectoryResultFailure(failure_reason=FileIOFailureReason.INVALID_PATH, result_details=msg)

        if dir_path.is_dir() and not request.exist_ok:
            msg = f"Attempted to create directory '{dir_path}'. Failed because directory already exists."
            return MakeDirectoryResultFailure(
                failure_reason=FileIOFailureReason.POLICY_NO_OVERWRITE, result_details=msg
            )

        if dir_path.is_dir():
            return MakeDirectoryResultSuccess(
                created_path=str(dir_path),
                already_existed=True,
                result_details=f"Directory already exists at {dir_path}",
            )

        normalized = normalize_path_for_platform(dir_path)
        try:
            Path(normalized).mkdir(parents=request.create_parents, exist_ok=request.exist_ok)
        except FileNotFoundError as e:
            msg = f"Attempted to create directory '{dir_path}'. Failed because parent directory does not exist and create_parents is False: {e}"
            return MakeDirectoryResultFailure(
                failure_reason=FileIOFailureReason.POLICY_NO_CREATE_PARENT_DIRS, result_details=msg
            )
        except PermissionError as e:
            msg = f"Attempted to create directory '{dir_path}'. Failed due to permission denied: {e}"
            return MakeDirectoryResultFailure(failure_reason=FileIOFailureReason.PERMISSION_DENIED, result_details=msg)
        except OSError as e:
            if "No space left" in str(e) or "Disk full" in str(e):
                msg = f"Attempted to create directory '{dir_path}'. Failed due to disk full: {e}"
                return MakeDirectoryResultFailure(failure_reason=FileIOFailureReason.DISK_FULL, result_details=msg)
            msg = f"Attempted to create directory '{dir_path}'. Failed due to I/O error: {e}"
            return MakeDirectoryResultFailure(failure_reason=FileIOFailureReason.IO_ERROR, result_details=msg)

        return MakeDirectoryResultSuccess(
            created_path=str(dir_path),
            already_existed=False,
            result_details=f"Directory created successfully at {dir_path}",
        )

    @handles(CreateFileRequest)
    def on_create_file_request(self, request: CreateFileRequest) -> ResultPayload:  # noqa: PLR0911, PLR0912, C901
        """Handle a request to create a file or directory."""
        # Get the full path
        try:
            full_path_str = request.get_full_path()
        except ValueError as e:
            msg = f"Invalid path specification: {e}"
            return CreateFileResultFailure(failure_reason=FileIOFailureReason.INVALID_PATH, result_details=msg)

        # Determine if path is absolute (not constrained to workspace)
        is_absolute = Path(full_path_str).is_absolute()

        # If workspace_only is True and path is absolute, it's outside workspace
        if request.workspace_only and is_absolute:
            msg = f"Absolute path is outside workspace: {full_path_str}"
            return CreateFileResultFailure(failure_reason=FileIOFailureReason.INVALID_PATH, result_details=msg)

        # Resolve path - if absolute, use as-is; if relative, align to workspace
        if is_absolute:
            file_path = resolve_path_safely(Path(full_path_str))
        else:
            file_path = resolve_path_safely(self._get_workspace_path() / full_path_str)

        # Check if it already exists - warn but treat as success
        if file_path.exists():
            msg = f"Path already exists: {file_path}"
            return CreateFileResultSuccess(
                created_path=str(file_path), result_details=ResultDetails(message=msg, level=logging.WARNING)
            )

        # Create parent directories if needed
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError as e:
            msg = f"Permission denied creating parent directory for {file_path}: {e}"
            return CreateFileResultFailure(failure_reason=FileIOFailureReason.PERMISSION_DENIED, result_details=msg)
        except OSError as e:
            msg = f"I/O error creating parent directory for {file_path}: {e}"
            return CreateFileResultFailure(failure_reason=FileIOFailureReason.IO_ERROR, result_details=msg)

        # Create file or directory
        try:
            if request.is_directory:
                file_path.mkdir()
                logger.debug("Created directory: %s", file_path)
            # Create file with optional content
            elif request.content is not None:
                with file_path.open("w", encoding=request.encoding) as f:
                    f.write(request.content)
                logger.debug("Created file with content: %s", file_path)
            else:
                file_path.touch()
                logger.debug("Created empty file: %s", file_path)
        except PermissionError as e:
            msg = f"Permission denied creating {file_path}: {e}"
            return CreateFileResultFailure(failure_reason=FileIOFailureReason.PERMISSION_DENIED, result_details=msg)
        except OSError as e:
            # Check for disk full
            if "No space left" in str(e) or "Disk full" in str(e):
                msg = f"Disk full creating {file_path}: {e}"
                return CreateFileResultFailure(failure_reason=FileIOFailureReason.DISK_FULL, result_details=msg)

            msg = f"I/O error creating {file_path}: {e}"
            return CreateFileResultFailure(failure_reason=FileIOFailureReason.IO_ERROR, result_details=msg)
        except Exception as e:
            msg = f"Unexpected error creating {file_path}: {type(e).__name__}: {e}"
            return CreateFileResultFailure(failure_reason=FileIOFailureReason.UNKNOWN, result_details=msg)

        # SUCCESS PATH
        return CreateFileResultSuccess(
            created_path=str(file_path),
            result_details=f"{'Directory' if request.is_directory else 'File'} created successfully at {file_path}",
        )

    @handles(RenameFileRequest)
    def on_rename_file_request(self, request: RenameFileRequest) -> ResultPayload:  # noqa: PLR0911, C901
        """Handle a request to rename a file or directory."""
        # Resolve and validate paths
        try:
            old_path = self.resolve_file_path(request.old_path, workspace_only=request.workspace_only is True)
        except (ValueError, RuntimeError) as e:
            msg = f"Invalid source path: {e}"
            return RenameFileResultFailure(failure_reason=FileIOFailureReason.INVALID_PATH, result_details=msg)

        try:
            new_path = self.resolve_file_path(request.new_path, workspace_only=request.workspace_only is True)
        except (ValueError, RuntimeError) as e:
            msg = f"Invalid destination path: {e}"
            return RenameFileResultFailure(failure_reason=FileIOFailureReason.INVALID_PATH, result_details=msg)

        # Check if old path exists
        if not old_path.exists():
            msg = f"Source path does not exist: {old_path}"
            return RenameFileResultFailure(failure_reason=FileIOFailureReason.FILE_NOT_FOUND, result_details=msg)

        # Check if new path already exists
        if new_path.exists():
            msg = f"Destination path already exists: {new_path}"
            return RenameFileResultFailure(failure_reason=FileIOFailureReason.INVALID_PATH, result_details=msg)

        # Check workspace constraints for both paths
        is_old_in_workspace, _ = self._validate_workspace_path(old_path)
        is_new_in_workspace, _ = self._validate_workspace_path(new_path)

        if request.workspace_only and (not is_old_in_workspace or not is_new_in_workspace):
            msg = f"One or both paths are outside workspace: {old_path} -> {new_path}"
            return RenameFileResultFailure(failure_reason=FileIOFailureReason.INVALID_PATH, result_details=msg)

        # Create parent directories for new path if needed
        try:
            new_path.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError as e:
            msg = f"Permission denied creating parent directory for {new_path}: {e}"
            return RenameFileResultFailure(failure_reason=FileIOFailureReason.PERMISSION_DENIED, result_details=msg)
        except OSError as e:
            msg = f"I/O error creating parent directory for {new_path}: {e}"
            return RenameFileResultFailure(failure_reason=FileIOFailureReason.IO_ERROR, result_details=msg)

        # Perform the rename operation
        try:
            old_path.rename(new_path)
        except PermissionError as e:
            msg = f"Permission denied renaming {old_path} to {new_path}: {e}"
            return RenameFileResultFailure(failure_reason=FileIOFailureReason.PERMISSION_DENIED, result_details=msg)
        except OSError as e:
            msg = f"I/O error renaming {old_path} to {new_path}: {e}"
            return RenameFileResultFailure(failure_reason=FileIOFailureReason.IO_ERROR, result_details=msg)
        except Exception as e:
            msg = f"Unexpected error renaming {old_path} to {new_path}: {type(e).__name__}: {e}"
            return RenameFileResultFailure(failure_reason=FileIOFailureReason.UNKNOWN, result_details=msg)

        # SUCCESS PATH
        details = f"Renamed: {old_path} -> {new_path}"
        return RenameFileResultSuccess(
            old_path=str(old_path),
            new_path=str(new_path),
            result_details=ResultDetails(message=details, level=logging.INFO),
        )

    @handles(CopyFileRequest)
    def on_copy_file_request(self, request: CopyFileRequest) -> ResultPayload:  # noqa: PLR0911, C901
        """Handle a request to copy a single file."""
        # Resolve source path
        try:
            source_path = self.resolve_file_path(request.source_path, workspace_only=False)
            source_normalized = normalize_path_for_platform(source_path)
        except (ValueError, RuntimeError) as e:
            msg = f"Invalid source path: {e}"
            return CopyFileResultFailure(failure_reason=FileIOFailureReason.INVALID_PATH, result_details=msg)

        # Check if source exists
        if not Path(source_normalized).exists():
            msg = f"Source file does not exist: {source_path}"
            return CopyFileResultFailure(failure_reason=FileIOFailureReason.FILE_NOT_FOUND, result_details=msg)

        # Check if source is a file (not a directory)
        if not Path(source_normalized).is_file():
            msg = f"Source path is not a file: {source_path}"
            return CopyFileResultFailure(failure_reason=FileIOFailureReason.INVALID_PATH, result_details=msg)

        # Resolve destination path
        try:
            destination_path = self.resolve_file_path(request.destination_path, workspace_only=False)
            dest_normalized = normalize_path_for_platform(destination_path)
        except (ValueError, RuntimeError) as e:
            msg = f"Invalid destination path: {e}"
            return CopyFileResultFailure(failure_reason=FileIOFailureReason.INVALID_PATH, result_details=msg)

        # Check if destination already exists (unless overwrite is True)
        if Path(dest_normalized).exists() and not request.overwrite:
            msg = f"Destination file already exists: {destination_path}"
            return CopyFileResultFailure(failure_reason=FileIOFailureReason.INVALID_PATH, result_details=msg)

        # Create parent directory if it doesn't exist
        dest_parent = Path(dest_normalized).parent
        if not dest_parent.exists():
            try:
                dest_parent.mkdir(parents=True)
            except PermissionError as e:
                msg = f"Permission denied creating parent directory {dest_parent}: {e}"
                return CopyFileResultFailure(failure_reason=FileIOFailureReason.PERMISSION_DENIED, result_details=msg)
            except OSError as e:
                msg = f"I/O error creating parent directory {dest_parent}: {e}"
                return CopyFileResultFailure(failure_reason=FileIOFailureReason.IO_ERROR, result_details=msg)

        # Copy the file
        try:
            bytes_copied = self._copy_file(source_path, destination_path)
        except PermissionError as e:
            msg = f"Permission denied copying {source_path} to {destination_path}: {e}"
            return CopyFileResultFailure(failure_reason=FileIOFailureReason.PERMISSION_DENIED, result_details=msg)
        except OSError as e:
            if "No space left" in str(e) or "Disk full" in str(e):
                msg = f"Disk full copying {source_path} to {destination_path}: {e}"
                return CopyFileResultFailure(failure_reason=FileIOFailureReason.DISK_FULL, result_details=msg)

            msg = f"I/O error copying {source_path} to {destination_path}: {e}"
            return CopyFileResultFailure(failure_reason=FileIOFailureReason.IO_ERROR, result_details=msg)
        except Exception as e:
            msg = f"Unexpected error copying {source_path} to {destination_path}: {type(e).__name__}: {e}"
            return CopyFileResultFailure(failure_reason=FileIOFailureReason.UNKNOWN, result_details=msg)

        # SUCCESS PATH
        return CopyFileResultSuccess(
            source_path=str(source_path),
            destination_path=str(destination_path),
            bytes_copied=bytes_copied,
            result_details=f"File copied successfully: {source_path} -> {destination_path}",
        )

    @staticmethod
    def remove_readonly(func, path, excinfo) -> None:  # noqa: ANN001, ARG004
        """Handles read-only files and long paths on Windows during shutil.rmtree.

        https://stackoverflow.com/a/50924863
        """
        if not OSManager.is_windows():
            return

        long_path = Path(normalize_path_for_platform(Path(path)))

        try:
            Path.chmod(long_path, stat.S_IWRITE)
            func(long_path)
        except Exception as e:
            console.print(f"[red]Error removing read-only file: {path}[/red]")
            console.print(f"[red]Details: {e}[/red]")
            raise

    @handles(DeleteFileRequest)
    async def on_delete_file_request(  # noqa: PLR0911, PLR0912, PLR0915, C901
        self, request: DeleteFileRequest
    ) -> DeleteFileResultSuccess | DeleteFileResultFailure:
        """Handle a request to delete a file or directory."""
        # Validate exactly one of path or file_entry provided and determine path to delete
        if request.path is not None and request.file_entry is not None:
            msg = "Attempted to delete file with both path and file_entry. Failed due to invalid parameters"
            return DeleteFileResultFailure(failure_reason=FileIOFailureReason.INVALID_PATH, result_details=msg)

        if request.path is not None:
            path_to_delete = request.path
        elif request.file_entry is not None:
            path_to_delete = request.file_entry.path
        else:
            msg = "Attempted to delete file with neither path nor file_entry. Failed due to invalid parameters"
            return DeleteFileResultFailure(failure_reason=FileIOFailureReason.INVALID_PATH, result_details=msg)

        # Resolve and validate path
        try:
            resolved_path = self.resolve_file_path(path_to_delete, workspace_only=request.workspace_only is True)
        except (ValueError, RuntimeError) as e:
            msg = f"Attempted to delete file at path {path_to_delete}. Failed due to invalid path: {e}"
            return DeleteFileResultFailure(failure_reason=FileIOFailureReason.INVALID_PATH, result_details=msg)

        # Check if path exists
        if not await anyio.Path(resolved_path).exists():
            msg = f"Attempted to delete file at path {path_to_delete}. Failed due to path not found"
            return DeleteFileResultFailure(failure_reason=FileIOFailureReason.FILE_NOT_FOUND, result_details=msg)

        # Determine if this is a directory
        is_directory = await anyio.Path(resolved_path).is_dir()

        # Collect all paths that will be deleted (for reporting)
        if is_directory and request.collect_deleted_paths:
            # Collect all file and directory paths before deletion
            deleted_paths = [str(item) async for item in anyio.Path(resolved_path).rglob("*")]
            deleted_paths.append(str(resolved_path))
        else:
            deleted_paths = [str(resolved_path)]

        # Helper function for permanent deletion
        async def attempt_permanent_delete() -> DeleteFileResultFailure | None:
            """Permanently delete the file/directory. Returns failure result or None on success."""
            try:
                if is_directory:
                    await asyncio.to_thread(shutil.rmtree, resolved_path, onexc=OSManager.remove_readonly)
                else:
                    await anyio.Path(resolved_path).unlink()
            except PermissionError as e:
                msg = f"Attempted to delete {'directory' if is_directory else 'file'} at path {path_to_delete}. Failed due to permission denied: {e}"
                return DeleteFileResultFailure(failure_reason=FileIOFailureReason.PERMISSION_DENIED, result_details=msg)
            except OSError as e:
                msg = f"Attempted to delete {'directory' if is_directory else 'file'} at path {path_to_delete}. Failed due to I/O error: {e}"
                return DeleteFileResultFailure(failure_reason=FileIOFailureReason.IO_ERROR, result_details=msg)
            except Exception as e:
                msg = f"Attempted to delete {'directory' if is_directory else 'file'} at path {path_to_delete}. Failed due to unexpected error: {type(e).__name__}: {e}"
                return DeleteFileResultFailure(failure_reason=FileIOFailureReason.UNKNOWN, result_details=msg)
            return None

        # Helper function for recycle bin deletion
        async def attempt_recycle_bin_delete() -> DeleteFileResultFailure | None:
            """Send to recycle bin. Returns failure result or None on success."""
            try:
                await asyncio.to_thread(send2trash.send2trash, str(resolved_path))
            except send2trash.TrashPermissionError as e:
                msg = f"Attempted to send {'directory' if is_directory else 'file'} at path {path_to_delete} to the recycle bin. Failed due to recycle bin unavailable: {e}"
                return DeleteFileResultFailure(
                    failure_reason=FileIOFailureReason.RECYCLE_BIN_UNAVAILABLE, result_details=msg
                )
            except OSError as e:
                msg = f"Attempted to send {'directory' if is_directory else 'file'} at path {path_to_delete} to the recycle bin. Failed due to I/O error: {e}"
                return DeleteFileResultFailure(failure_reason=FileIOFailureReason.IO_ERROR, result_details=msg)
            except Exception as e:
                msg = f"Attempted to send {'directory' if is_directory else 'file'} at path {path_to_delete} to the recycle bin. Failed due to unexpected error: {type(e).__name__}: {e}"
                return DeleteFileResultFailure(failure_reason=FileIOFailureReason.UNKNOWN, result_details=msg)
            return None

        # Perform deletion based on requested behavior
        match request.deletion_behavior:
            case DeletionBehavior.PERMANENTLY_DELETE:
                failure = await attempt_permanent_delete()
                if failure:
                    return failure
                outcome = DeletionOutcome.PERMANENTLY_DELETED
                result_details = (
                    f"Successfully deleted {'directory' if is_directory else 'file'} at path {path_to_delete}"
                )

            case DeletionBehavior.RECYCLE_BIN_ONLY:
                failure = await attempt_recycle_bin_delete()
                if failure:
                    return failure
                outcome = DeletionOutcome.SENT_TO_RECYCLE_BIN
                result_details = f"Successfully sent {'directory' if is_directory else 'file'} at path {path_to_delete} to the recycle bin"

            case DeletionBehavior.PREFER_RECYCLE_BIN:
                failure = await attempt_recycle_bin_delete()
                if failure:
                    # Fall back to permanent deletion
                    failure = await attempt_permanent_delete()
                    if failure:
                        return failure
                    outcome = DeletionOutcome.PERMANENTLY_DELETED
                    result_details = ResultDetails(
                        message=f"Attempted to send {'directory' if is_directory else 'file'} at path {path_to_delete} to the recycle bin, but this failed; fell back to permanent deletion, which succeeded.",
                        level=logging.WARNING,
                    )
                else:
                    outcome = DeletionOutcome.SENT_TO_RECYCLE_BIN
                    result_details = f"Successfully sent {'directory' if is_directory else 'file'} at path {path_to_delete} to the recycle bin"

            case _:
                msg = f"Unknown/unsupported deletion behavior: {request.deletion_behavior}"
                raise ValueError(msg)

        # SUCCESS PATH AT END
        return DeleteFileResultSuccess(
            deleted_path=str(resolved_path),
            was_directory=is_directory,
            deleted_paths=deleted_paths,
            outcome=outcome,
            result_details=result_details,
        )

    @handles(GetFileInfoRequest)
    def on_get_file_info_request(  # noqa: PLR0911
        self, request: GetFileInfoRequest
    ) -> GetFileInfoResultSuccess | GetFileInfoResultFailure:
        """Handle a request to get file/directory information."""
        # FAILURE CASES FIRST (per CLAUDE.md)

        # Validate path provided
        if not request.path:
            msg = "Attempted to get file info with empty path. Failed due to invalid parameters"
            return GetFileInfoResultFailure(failure_reason=FileIOFailureReason.INVALID_PATH, result_details=msg)

        # Resolve and validate path
        try:
            resolved_path = self.resolve_file_path(request.path, workspace_only=request.workspace_only is True)
        except (ValueError, RuntimeError) as e:
            msg = f"Attempted to get file info at path {request.path}. Failed due to invalid path: {e}"
            return GetFileInfoResultFailure(failure_reason=FileIOFailureReason.INVALID_PATH, result_details=msg)

        # Check if path exists - if not, return success with None (file doesn't exist)
        if not resolved_path.exists():
            msg = f"File info retrieved for path {request.path}: file does not exist"
            return GetFileInfoResultSuccess(file_entry=None, result_details=msg)

        # Get file information
        try:
            is_dir = resolved_path.is_dir()
            # One stat() call for both fields: two calls can straddle a concurrent
            # replace and pair one file's size with another's mtime, poisoning the
            # preview staleness check that compares both against recorded values.
            stat_result = resolved_path.stat()
            size = 0 if is_dir else stat_result.st_size
            modified_time = stat_result.st_mtime

            # Get MIME type for files only
            mime_type = None
            if not is_dir:
                mime_type = self._detect_mime_type(resolved_path)

            # Get path relative to workspace if within workspace
            _, file_path = self._validate_workspace_path(resolved_path)

            # Also get absolute resolved path
            absolute_resolved_path = str(canonicalize_for_identity(resolved_path))

            file_entry = FileSystemEntry(
                name=resolved_path.name,
                path=str(file_path),
                is_dir=is_dir,
                size=size,
                modified_time=modified_time,
                mime_type=mime_type,
                absolute_path=absolute_resolved_path,
            )
        except PermissionError as e:
            msg = f"Attempted to get file info at path {request.path}. Failed due to permission denied: {e}"
            return GetFileInfoResultFailure(failure_reason=FileIOFailureReason.PERMISSION_DENIED, result_details=msg)
        except OSError as e:
            msg = f"Attempted to get file info at path {request.path}. Failed due to I/O error: {e}"
            return GetFileInfoResultFailure(failure_reason=FileIOFailureReason.IO_ERROR, result_details=msg)
        except Exception as e:
            msg = f"Attempted to get file info at path {request.path}. Failed due to unexpected error: {type(e).__name__}: {e}"
            return GetFileInfoResultFailure(failure_reason=FileIOFailureReason.UNKNOWN, result_details=msg)

        # SUCCESS PATH AT END
        return GetFileInfoResultSuccess(
            file_entry=file_entry,
            result_details=f"Successfully retrieved file info for path {request.path}",
        )

    @handles(ResolveMacroPathRequest)
    def on_handle_resolve_macro_path_request(
        self, request: ResolveMacroPathRequest, workflow: "WorkflowRuntime | None" = None
    ) -> ResolveMacroPathResultSuccess | ResolveMacroPathResultFailure:
        """Handle macro path resolution request.

        Args:
            request: The request containing macro_path to resolve
            workflow: The workflow `{workflow_dir}` resolves for, or None

        Returns:
            Success with resolved path or failure with details
        """
        resolution_result = self.resolve_macro_path_to_string(request.macro_path, workflow)

        if isinstance(resolution_result, MacroResolutionFailure):
            return ResolveMacroPathResultFailure(
                result_details=resolution_result.error_details,
                missing_variables=resolution_result.missing_variables,
            )

        return ResolveMacroPathResultSuccess(
            result_details="Macro path resolved successfully",
            resolved_path=resolution_result,
        )

    def resolve_macro_path_in(
        self, workflow: "WorkflowRuntime | None", request: ResolveMacroPathRequest
    ) -> ResultPayload:
        """Resolve `request` for `workflow`, for a handler that holds the workflow it resolves for, or None.

        Dispatched, so pre-dispatch hooks and the request's `failure_log_level` apply. With no
        workflow, the request is unaddressed and resolves for its caller's workflow, if any.
        """
        if workflow is None:
            return self.engine.handle_request(request)
        return workflow.handle_request(request)

    def _validate_copy_tree_paths(
        self, source_str: str, dest_str: str, *, dirs_exist_ok: bool
    ) -> CopyTreeValidationResult | CopyTreeResultFailure:
        """Validate and normalize source and destination paths for copy tree operation.

        Returns:
            CopyTreeValidationResult on success, CopyTreeResultFailure on validation failure
        """
        # Resolve and normalize source path
        try:
            source_path = self.resolve_file_path(source_str, workspace_only=False)
            source_normalized = normalize_path_for_platform(source_path)
        except (ValueError, RuntimeError) as e:
            msg = f"Invalid source path: {e}"
            return CopyTreeResultFailure(failure_reason=FileIOFailureReason.INVALID_PATH, result_details=msg)

        # Check if source exists
        if not Path(source_normalized).exists():
            msg = f"Source path does not exist: {source_path}"
            return CopyTreeResultFailure(failure_reason=FileIOFailureReason.FILE_NOT_FOUND, result_details=msg)

        # Check if source is a directory
        if not Path(source_normalized).is_dir():
            msg = f"Source path is not a directory: {source_path}"
            return CopyTreeResultFailure(failure_reason=FileIOFailureReason.INVALID_PATH, result_details=msg)

        # Resolve and normalize destination path
        try:
            destination_path = self.resolve_file_path(dest_str, workspace_only=False)
            dest_normalized = normalize_path_for_platform(destination_path)
        except (ValueError, RuntimeError) as e:
            msg = f"Invalid destination path: {e}"
            return CopyTreeResultFailure(failure_reason=FileIOFailureReason.INVALID_PATH, result_details=msg)

        # Check if destination already exists (unless dirs_exist_ok is True)
        if Path(dest_normalized).exists() and not dirs_exist_ok:
            msg = f"Destination path already exists: {destination_path}"
            return CopyTreeResultFailure(failure_reason=FileIOFailureReason.INVALID_PATH, result_details=msg)

        return CopyTreeValidationResult(
            source_normalized=source_normalized,
            dest_normalized=dest_normalized,
            source_path=source_path,
            destination_path=destination_path,
        )

    def _copy_directory_tree(  # noqa: PLR0912, C901
        self,
        source_normalized: str,
        dest_normalized: str,
        *,
        symlinks: bool,
        ignore_dangling_symlinks: bool,
        ignore_patterns: list[str] | None = None,
    ) -> CopyTreeStats:
        """Copy directory tree from source to destination.

        Args:
            source_normalized: Normalized source path
            dest_normalized: Normalized destination path
            symlinks: If True, copy symbolic links as links
            ignore_dangling_symlinks: If True, ignore dangling symlinks
            ignore_patterns: List of glob patterns to ignore (e.g., ["__pycache__", "*.pyc"])

        Returns:
            CopyTreeStats with files copied and bytes copied

        Raises:
            OSError: If copy operation fails
            PermissionError: If permission denied
        """
        from fnmatch import fnmatch

        files_copied = 0
        total_bytes_copied = 0
        ignore_patterns = ignore_patterns or []

        def should_ignore(name: str) -> bool:
            """Check if a file/directory name matches any ignore pattern."""
            return any(fnmatch(name, pattern) for pattern in ignore_patterns)

        # Create destination directory if it doesn't exist
        dest_path_obj = Path(dest_normalized)
        if not dest_path_obj.exists():
            dest_path_obj.mkdir(parents=True)

        # Walk through source directory and copy files/directories
        for root, dirs, files in os.walk(source_normalized):
            # Calculate relative path from source
            root_path = Path(root)
            source_path_obj = Path(source_normalized)
            rel_path = root_path.relative_to(source_path_obj)

            # Create corresponding directory in destination
            if str(rel_path) != ".":
                dest_dir = dest_path_obj / rel_path
            else:
                dest_dir = dest_path_obj

            # Filter out ignored directories and create remaining ones
            dirs_to_remove = []
            for dir_name in dirs:
                if should_ignore(dir_name):
                    dirs_to_remove.append(dir_name)
                    continue

                src_dir = root_path / dir_name
                dst_dir = dest_dir / dir_name

                # Handle symlinks if requested
                if src_dir.is_symlink():
                    if symlinks:
                        link_target = src_dir.readlink()
                        dst_dir.symlink_to(link_target)
                    continue

                if not dst_dir.exists():
                    dst_dir.mkdir(parents=True)

            # Remove ignored directories from dirs list to prevent os.walk from descending into them
            for dir_name in dirs_to_remove:
                dirs.remove(dir_name)

            # Copy files
            for file_name in files:
                # Skip ignored files
                if should_ignore(file_name):
                    continue

                src_file = root_path / file_name
                dst_file = dest_dir / file_name

                # Handle symlinks if requested
                if src_file.is_symlink():
                    if symlinks:
                        try:
                            link_target = src_file.readlink()
                            dst_file.symlink_to(link_target)
                        except OSError:
                            if not ignore_dangling_symlinks:
                                raise
                    continue

                # Copy file
                bytes_copied = self._copy_file(src_file, dst_file)
                files_copied += 1
                total_bytes_copied += bytes_copied

        return CopyTreeStats(files_copied=files_copied, total_bytes_copied=total_bytes_copied)

    @handles(CopyTreeRequest)
    def on_copy_tree_request(self, request: CopyTreeRequest) -> ResultPayload:
        """Handle a request to copy a directory tree."""
        # Validate paths
        validation_result = self._validate_copy_tree_paths(
            request.source_path,
            request.destination_path,
            dirs_exist_ok=request.dirs_exist_ok,
        )

        if isinstance(validation_result, CopyTreeResultFailure):
            return validation_result

        source_normalized = validation_result.source_normalized
        dest_normalized = validation_result.dest_normalized
        source_path = validation_result.source_path
        destination_path = validation_result.destination_path

        # Copy directory tree
        try:
            stats = self._copy_directory_tree(
                source_normalized,
                dest_normalized,
                symlinks=request.symlinks,
                ignore_dangling_symlinks=request.ignore_dangling_symlinks,
                ignore_patterns=request.ignore_patterns,
            )
        except PermissionError as e:
            msg = f"Permission denied copying {source_path} to {destination_path}: {e}"
            return CopyTreeResultFailure(failure_reason=FileIOFailureReason.PERMISSION_DENIED, result_details=msg)
        except OSError as e:
            if "No space left" in str(e) or "Disk full" in str(e):
                msg = f"Disk full copying {source_path} to {destination_path}: {e}"
                return CopyTreeResultFailure(failure_reason=FileIOFailureReason.DISK_FULL, result_details=msg)

            msg = f"I/O error copying {source_path} to {destination_path}: {e}"
            return CopyTreeResultFailure(failure_reason=FileIOFailureReason.IO_ERROR, result_details=msg)
        except Exception as e:
            msg = f"Unexpected error copying {source_path} to {destination_path}: {type(e).__name__}: {e}"
            return CopyTreeResultFailure(failure_reason=FileIOFailureReason.UNKNOWN, result_details=msg)

        # SUCCESS PATH
        return CopyTreeResultSuccess(
            source_path=str(source_path),
            destination_path=str(destination_path),
            files_copied=stats.files_copied,
            total_bytes_copied=stats.total_bytes_copied,
            result_details=f"Directory tree copied successfully: {source_path} -> {destination_path}",
        )
