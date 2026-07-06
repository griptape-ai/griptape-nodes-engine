from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from griptape_nodes.common.sequences.models import MissingItemPolicy, NoTokenBehavior, Sequence, SequenceScanOptions
from griptape_nodes.retained_mode.events.base_events import (
    RequestPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry
from griptape_nodes.retained_mode.events.project_events import MacroPath
from griptape_nodes.retained_mode.file_metadata.sidecar_metadata import SidecarContent


class ExistingFilePolicy(StrEnum):
    """Policy for handling existing files during write operations."""

    OVERWRITE = "overwrite"  # Replace existing file content
    FAIL = "fail"  # Fail if file exists
    CREATE_NEW = "create_new"  # Create new file with modified name (e.g., file_1.txt)


class FileIOFailureReason(StrEnum):
    """Classification of file I/O failure reasons.

    Used by read and write operations to provide structured error information.
    """

    # Policy violations
    POLICY_NO_OVERWRITE = "policy_no_overwrite"  # File exists and policy prohibits overwrite
    POLICY_NO_CREATE_PARENT_DIRS = "policy_no_create_parent_dirs"  # Parent dir missing and policy prohibits creation
    CODEC_NOT_PERMITTED = "codec_not_permitted"  # Sniffed media codec is denied by the authorization hook chain

    # Permission/access errors
    PERMISSION_DENIED = "permission_denied"  # No read/write permission
    FILE_NOT_FOUND = "file_not_found"  # File doesn't exist (read operations)
    FILE_LOCKED = "file_locked"  # File is locked by another process

    # Resource errors
    DISK_FULL = "disk_full"  # Insufficient disk space

    # Path errors
    INVALID_PATH = "invalid_path"  # Malformed or invalid path
    IS_DIRECTORY = "is_directory"  # Path is a directory, not a file
    MISSING_MACRO_VARIABLES = "missing_macro_variables"  # MacroPath has unresolved required variables

    # Content errors
    ENCODING_ERROR = "encoding_error"  # Text encoding/decoding failed
    EXTENSION_MISMATCH = (
        "extension_mismatch"  # Sniffed byte format disagrees with destination suffix and coercion is disabled
    )

    # Generic errors
    IO_ERROR = "io_error"  # Generic I/O error
    UNKNOWN = "unknown"  # Unexpected error

    # Recycle bin errors
    RECYCLE_BIN_UNAVAILABLE = "recycle_bin_unavailable"  # Recycle bin unavailable and behavior was RECYCLE_BIN_ONLY


class SequenceScanFailureReason(StrEnum):
    """Sequence-semantic failure reasons returned by `ScanSequencesRequest`.

    OS-layer failures (directory not found, permission denied, etc.) are reported
    using `FileIOFailureReason` instead; the request's failure payload accepts
    either enum so the handler can pick the right taxonomy per layer.

    A successful scan that simply found nothing is NOT a failure — it returns
    `ScanSequencesResultSuccess` with `sequences=[]` and `has_entries=False`.
    Failures are reserved for cases where the scan couldn't proceed.
    """

    INVALID_TEMPLATE = "invalid_template"  # Multi-token templates or fileseq parse errors.
    INVALID_BOUNDS = "invalid_bounds"  # `start_number` < 0, or `end_number` < `start_number`.
    ABORTED_AT_GAP = (
        "aborted_at_gap"  # `MissingItemPolicy.ABORT` hit at least one gap; payload lists every offending number.
    )


class DeletionBehavior(StrEnum):
    """How to handle file/directory deletion."""

    PERMANENTLY_DELETE = "permanently_delete"  # Permanently delete (default, current behavior)
    RECYCLE_BIN_ONLY = "recycle_bin_only"  # Send to recycle bin; fail if unavailable
    PREFER_RECYCLE_BIN = "prefer_recycle_bin"  # Try recycle bin; fall back to permanent deletion


class DeletionOutcome(StrEnum):
    """The actual outcome of a deletion operation."""

    PERMANENTLY_DELETED = "permanently_deleted"
    SENT_TO_RECYCLE_BIN = "sent_to_recycle_bin"


@dataclass
class FileSystemEntry:
    """Represents a file or directory in the file system."""

    name: str
    path: str  # Workspace-relative path (for portability)
    is_dir: bool
    size: int = 0  # File size in bytes (0 if not included)
    modified_time: float = 0.0  # Modification timestamp (0.0 if not included)
    absolute_path: str = ""  # Absolute resolved path (empty if not included)
    mime_type: str | None = None  # None for directories, mimetype for files (None if not included)


@dataclass
@PayloadRegistry.register
class OpenAssociatedFileRequest(RequestPayload):
    """Open a file or directory using the operating system's associated application.

    Use when: Opening generated files, launching external applications,
    providing file viewing capabilities, implementing file associations,
    opening folders in system explorer.

    Args:
        path_to_file: Path to the file or directory to open (mutually exclusive with file_entry)
        file_entry: FileSystemEntry object from directory listing (mutually exclusive with path_to_file)

    Results: OpenAssociatedFileResultSuccess | OpenAssociatedFileResultFailure (path not found, no association)
    """

    path_to_file: str | None = None
    file_entry: FileSystemEntry | None = None


@dataclass
@PayloadRegistry.register
class OpenAssociatedFileResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """File or directory opened successfully with associated application."""


@dataclass
@PayloadRegistry.register
class OpenAssociatedFileResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """File or directory opening failed.

    Attributes:
        failure_reason: Classification of why the open failed
        result_details: Human-readable error message (inherited from ResultPayloadFailure)
    """

    failure_reason: FileIOFailureReason


@dataclass
@PayloadRegistry.register
class ListDirectoryRequest(RequestPayload):
    """List contents of a directory.

    Use when: Browsing file system, showing directory contents,
    implementing file pickers, navigating folder structures.

    Args:
        directory_path: Path to the directory to list (None for current directory, supports macro syntax like {project_dir})
        show_hidden: Whether to show hidden files/folders
        workspace_only: If True, constrain to workspace directory. If False, allow system-wide browsing.
                        If None, workspace constraints don't apply (e.g., cloud environments).
        pattern: Optional glob pattern to filter entries (e.g., "*.txt", "file_*.json").
                 Only matches against file/directory names, not full paths.
        include_size: If True, include file size in results (default: True). Set to False for faster listing.
        include_modified_time: If True, include modified time in results (default: True). Set to False for faster listing.
        include_mime_type: If True, include MIME type in results (default: True). Set to False for faster listing.
        include_absolute_path: If True, include absolute resolved path in results (default: True). Set to False for faster listing.
        group_sequences: If True, files that form a numbered sequence are returned as
            ``Sequence`` objects in the ``sequences`` field instead of individual ``FileSystemEntry``
            objects in ``entries``. Defaults to False — sequence detection is opt-in.
        sequence_options: Controls sequence detection behaviour (policy, padding filter, frame bounds).
            Only used when ``group_sequences=True``. Defaults to ``SequenceScanOptions()`` when None.

    Results: ListDirectoryResultSuccess (with entries) | ListDirectoryResultFailure (access denied, not found)
    """

    directory_path: str | None = None
    show_hidden: bool = False
    workspace_only: bool | None = True
    pattern: str | None = None
    include_size: bool = True
    include_modified_time: bool = True
    include_mime_type: bool = True
    include_absolute_path: bool = True
    group_sequences: bool = False
    sequence_options: SequenceScanOptions | None = None


@dataclass
@PayloadRegistry.register
class ListDirectoryResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Directory listing retrieved successfully.

    Attributes:
        entries: Files and directories. When ``group_sequences=True`` (opt-in),
            sequence-member files are removed and appear in ``sequences`` instead;
            when ``group_sequences=False`` (default) all entries are returned here.
        sequences: ``Sequence`` objects detected when ``group_sequences=True``.
            Always an empty list when ``group_sequences=False``.
        current_path: The directory path used for the listing.
        is_workspace_path: True when the listed directory is inside the workspace.
    """

    entries: list[FileSystemEntry]
    current_path: str
    is_workspace_path: bool
    sequences: list[Sequence] = field(default_factory=list)


@dataclass
@PayloadRegistry.register
class ListDirectoryResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Directory listing failed.

    Attributes:
        failure_reason: Classification of why the listing failed. Sequence-semantic
            failures (bad bounds, ABORT-policy gaps) use ``SequenceScanFailureReason``;
            OS-layer failures use ``FileIOFailureReason``.
        missing_item_numbers: Populated only when ``failure_reason`` is
            ``SequenceScanFailureReason.ABORTED_AT_GAP``. Lists every missing frame
            number inside the active range, sorted ascending.
        result_details: Human-readable error message (inherited from ResultPayloadFailure)
    """

    failure_reason: SequenceScanFailureReason | FileIOFailureReason
    missing_item_numbers: list[int] = field(default_factory=list)


@dataclass
@PayloadRegistry.register
class ListDirectorySequencesRequest(ListDirectoryRequest):
    """List only file sequences in a directory.

    Returns sequence objects only — non-sequence files and directories are
    omitted from the result. Equivalent to issuing ``ListDirectoryRequest``
    with ``group_sequences=True`` but with a dedicated result type and
    without the flat-entry payload.

    Inherits all filtering arguments from ``ListDirectoryRequest``
    (``show_hidden``, ``pattern``, ``workspace_only``, etc.).

    Results: ListDirectorySequencesResultSuccess | ListDirectorySequencesResultFailure
    """


@dataclass
@PayloadRegistry.register
class ListDirectorySequencesResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Directory sequence listing retrieved successfully.

    Attributes:
        sequences: All ``Sequence`` objects detected in the directory.
            Empty list when the directory contains no sequences.
        current_path: The directory path used for the listing.
        is_workspace_path: True when the listed directory is inside the workspace.
    """

    sequences: list[Sequence]
    current_path: str
    is_workspace_path: bool


@dataclass
@PayloadRegistry.register
class ListDirectorySequencesResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Directory sequence listing failed.

    Attributes:
        failure_reason: Classification of why the listing failed. Sequence-semantic
            failures (bad bounds, ABORT-policy gaps) use ``SequenceScanFailureReason``;
            OS-layer failures use ``FileIOFailureReason``.
        missing_item_numbers: Populated only when ``failure_reason`` is
            ``SequenceScanFailureReason.ABORTED_AT_GAP``. Lists every missing frame
            number inside the active range, sorted ascending.
        result_details: Human-readable error message (inherited from ResultPayloadFailure).
    """

    failure_reason: SequenceScanFailureReason | FileIOFailureReason
    missing_item_numbers: list[int] = field(default_factory=list)


@dataclass
@PayloadRegistry.register
class DeduceSequencesFromFileListRequest(RequestPayload):
    """Detect file sequences within a caller-supplied list of file paths.

    No additional filesystem I/O is performed — pass paths already obtained
    from a directory listing or any other source. Callers are responsible for
    supplying only file paths; directory paths in the list will not be
    filtered out and may produce unexpected results. Files from different
    parent directories are grouped independently by their shared parent.

    Use when: Sequence grouping is needed for a file list that has already
    been collected, avoiding a redundant directory scan.

    Args:
        file_paths: Absolute (or workspace-relative) paths to inspect.
            Bare filenames without a directory component are allowed but
            result in ``Sequence.directory`` being an empty string.
        sequence_options: Policy, padding filter, and frame-range bounds.
            Defaults to ``SequenceScanOptions()`` when None.

    Results: DeduceSequencesFromFileListResultSuccess | DeduceSequencesFromFileListResultFailure
    """

    file_paths: list[str] = field(default_factory=list)
    sequence_options: SequenceScanOptions | None = None


@dataclass
@PayloadRegistry.register
class DeduceSequencesFromFileListResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Sequence deduction from file list completed successfully.

    Attributes:
        sequences: All ``Sequence`` objects detected. Empty list when no
            sequences were found in the provided file paths.
    """

    sequences: list[Sequence] = field(default_factory=list)


@dataclass
@PayloadRegistry.register
class DeduceSequencesFromFileListResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Sequence deduction from file list failed.

    Attributes:
        failure_reason: Classification of why the deduction failed. Sequence-semantic
            failures (bad bounds, ABORT-policy gaps) use ``SequenceScanFailureReason``;
            OS-layer failures use ``FileIOFailureReason``.
        missing_item_numbers: Populated only when ``failure_reason`` is
            ``SequenceScanFailureReason.ABORTED_AT_GAP``. Lists every missing frame
            number inside the active range, sorted ascending.
        result_details: Human-readable error message (inherited from ResultPayloadFailure).
    """

    failure_reason: SequenceScanFailureReason | FileIOFailureReason
    missing_item_numbers: list[int] = field(default_factory=list)


@dataclass
@PayloadRegistry.register
class ScanSequencesRequest(RequestPayload):
    """Scan a path or pattern; produce typed Sequence(s) with macro-preserving paths.

    Use when: A node or workflow needs to discover frame/item sequences on disk
    (e.g. a render output, a directory of dialogue takes). The engine handles
    macro resolution, the directory listing, and fileseq parsing — callers hand
    in a path or pattern and get back a Sequence whose paths are in the same
    macro shape they supplied. That makes scan results portable across machines
    where `{inputs}` may resolve to different absolute roots.

    Args:
        path: A path to a numbered file sequence. Can be:
            - A macro-form pattern: `{inputs}/render.####.png`. The macro head
              is preserved on every emitted entry path.
            - A plain absolute pattern: `/work/render.####.png`. Round-trips
              identically.
            - A literal single-file path: `/work/photo.png`. Behavior when the
              filename has no sequence token is controlled by
              `no_token_behavior` (see below).
            Sequence tokens (`####`, `%04d`, `@@@`, `$F4`) may appear at most
            once and only in the filename component. Multi-token paths are
            rejected with `INVALID_TEMPLATE`.
        policy: How to handle gaps within the matched range. Defaults to `SPLIT`.
        no_token_behavior: How to handle a path with zero sequence tokens.
            `SINGLE_FILE` (default) treats the whole filename as a literal —
            one-item sequence if the file exists, empty result otherwise.
            `EXPLORE_SEQUENCE` lets fileseq read digits in the filename as an
            implicit sequence — `render.0002.png` becomes one frame of an
            inferred `render.####.png` sequence and the scan walks every
            matching sibling. `REJECT` fails with `INVALID_TEMPLATE` for
            workflows that must not silently widen the artist's intent.
        start_number: Optional lower bound (inclusive) for the active range. Items
            below this are dropped. Must be >= 0 if supplied; rejected with
            `INVALID_BOUNDS` otherwise.
        end_number: Optional upper bound (inclusive). Items above this are dropped.
            Must be >= `start_number` if both supplied; rejected with `INVALID_BOUNDS`
            otherwise.

    Results: ScanSequencesResultSuccess (with sequences) | ScanSequencesResultFailure
    (sequence-semantic or OS-layer failure).
    """

    path: str
    policy: MissingItemPolicy = MissingItemPolicy.SPLIT
    no_token_behavior: NoTokenBehavior = NoTokenBehavior.SINGLE_FILE
    start_number: int | None = None
    end_number: int | None = None


@dataclass
@PayloadRegistry.register
class ScanSequencesResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Sequence scan completed successfully.

    A successful scan may legitimately return zero sequences (the directory
    had no files matching the path, the padding didn't line up, or the
    active range clipped them all out). The diagnostic fields let consumers
    distinguish those cases without inspecting `result_details` strings:

    - `directory_had_matching_files=False` → wrong path / wrong pattern /
      empty directory: nothing on disk matched the basename + extension.
    - `directory_had_matching_files=True`, `has_entries=False` → right path,
      but either the padding didn't line up with the pattern's token, or
      the active range (`start_number`/`end_number`) clipped everything out.
      `discovered_first`/`discovered_last` give the on-disk range so callers
      can show "asked for 90..100 but disk has 1..7."

    All emitted paths (`Sequence.directory` and `entry.path`) are in the
    same shape the caller supplied: a macro-form input round-trips with the
    macro head intact; a plain absolute input round-trips identically.

    Attributes:
        sequences: Zero or more `Sequence` objects produced by the scan.
            Under `SPLIT` policy this may contain multiple sub-sequences;
            under all other policies it has at most one. May be empty.
        has_entries: True iff at least one Sequence in `sequences` has at
            least one entry. Convenience for callers that just want to
            branch on "did the scan produce anything?" without iterating.
        directory_had_matching_files: True iff the directory listing
            produced ≥1 file matching the target's basename + extension
            (before fileseq parsing or subset clipping).
        discovered_first: Lowest item number actually found on disk that
            matched the pattern's full shape (basename + extension + zfill),
            ignoring any subset bounds. None when no sequences were inferred.
        discovered_last: Highest item number actually found on disk that
            matched the pattern's full shape, ignoring any subset bounds.
            None when no sequences were inferred.
    """

    sequences: list[Sequence]
    has_entries: bool
    directory_had_matching_files: bool
    discovered_first: int | None = None
    discovered_last: int | None = None


@dataclass
@PayloadRegistry.register
class ScanSequencesResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Sequence scan failed.

    Attributes:
        failure_reason: Either a sequence-semantic reason (`SequenceScanFailureReason`)
            or an OS-layer reason (`FileIOFailureReason`) when the underlying
            directory listing failed.
        missing_item_numbers: Populated only when `failure_reason` is
            `SequenceScanFailureReason.ABORTED_AT_GAP`. Lists every missing
            slot inside the active range, sorted ascending — UI consumers
            can show the artist all the gaps in one pass instead of fixing
            them one re-run at a time. Empty list for every other failure.
        result_details: Human-readable error message (inherited from ResultPayloadFailure).
    """

    failure_reason: SequenceScanFailureReason | FileIOFailureReason
    missing_item_numbers: list[int] = field(default_factory=list)


@dataclass
@PayloadRegistry.register
class ReadFileRequest(RequestPayload):
    """Read contents of a file, automatically detecting if it's text or binary using MIME types.

    Use when: Reading file contents for display, processing, or analysis.
    Automatically detects file type using MIME type detection and returns appropriate content format.

    Args:
        file_path: Path to the file to read (mutually exclusive with file_entry)
        file_entry: FileSystemEntry object from directory listing (mutually exclusive with file_path)
        encoding: Text encoding to use if file is detected as text (default: 'utf-8')
        workspace_only: If True, constrain to workspace directory. If False, allow system-wide access.
                        If None, workspace constraints don't apply (e.g., cloud environments).
                        TODO: Remove workspace_only parameter - see https://github.com/griptape-ai/griptape-nodes/issues/2753
        should_transform_image_content_to_thumbnail: If True, convert image files to thumbnail data URLs.
                        If False, return raw image bytes. Default True for backwards compatibility.

    Results: ReadFileResultSuccess (with content) | ReadFileResultFailure (file not found, permission denied)
    """

    broadcast_result: bool = False
    file_path: str | None = None
    file_entry: FileSystemEntry | None = None
    encoding: str = "utf-8"
    workspace_only: bool | None = True  # TODO: Remove - see https://github.com/griptape-ai/griptape-nodes/issues/2753
    should_transform_image_content_to_thumbnail: bool = True


@dataclass
@PayloadRegistry.register
class ReadFileResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """File contents read successfully."""

    content: str | bytes  # String for text files, bytes for binary files
    file_size: int
    mime_type: str  # e.g., "text/plain", "image/png", "application/pdf"
    encoding: str | None  # Text encoding used (None for binary files)
    compression_encoding: str | None = None  # Compression encoding (e.g., "gzip", "bzip2", None)
    is_text: bool = False  # Will be computed from content type

    def __post_init__(self) -> None:
        """Compute is_text from content type after initialization."""
        # For images, even though content is a string (base64), it's not text content
        if self.mime_type.startswith("image/"):
            self.is_text = False
        else:
            self.is_text = isinstance(self.content, str)


@dataclass
@PayloadRegistry.register
class ReadFileResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """File reading failed.

    Attributes:
        failure_reason: Classification of why the read failed
        result_details: Human-readable error message (inherited from ResultPayloadFailure)
    """

    failure_reason: FileIOFailureReason


@dataclass
@PayloadRegistry.register
class CreateFileRequest(RequestPayload):
    """Create a new file or directory.

    Use when: Creating files/directories through file picker,
    implementing file creation functionality.

    Args:
        path: Path where the file/directory should be created (legacy, use directory_path + name instead)
        directory_path: Directory where to create the file/directory (mutually exclusive with path)
        name: Name of the file/directory to create (mutually exclusive with path)
        is_directory: True to create a directory, False for a file
        content: Initial content for files (optional)
        encoding: Text encoding for file content (default: 'utf-8')
        workspace_only: If True, constrain to workspace directory

    Results: CreateFileResultSuccess | CreateFileResultFailure
    """

    path: str | None = None
    directory_path: str | None = None
    name: str | None = None
    is_directory: bool = False
    content: str | None = None
    encoding: str = "utf-8"
    workspace_only: bool | None = True

    def get_full_path(self) -> str:
        """Get the full path, constructing from directory_path + name if path is not provided."""
        if self.path is not None:
            return self.path
        if self.directory_path is not None and self.name is not None:
            from pathlib import Path

            return str(Path(self.directory_path) / self.name)
        msg = "Either 'path' or both 'directory_path' and 'name' must be provided"
        raise ValueError(msg)


@dataclass
@PayloadRegistry.register
class CreateFileResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """File/directory created successfully."""

    created_path: str


@dataclass
@PayloadRegistry.register
class CreateFileResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """File/directory creation failed.

    Attributes:
        failure_reason: Classification of why the creation failed
        result_details: Human-readable error message (inherited from ResultPayloadFailure)
    """

    failure_reason: FileIOFailureReason


@dataclass
@PayloadRegistry.register
class RenameFileRequest(RequestPayload):
    """Rename a file or directory.

    Use when: Renaming files/directories through file picker,
    implementing file rename functionality.

    Args:
        old_path: Current path of the file/directory to rename
        new_path: New path for the file/directory
        workspace_only: If True, constrain to workspace directory

    Results: RenameFileResultSuccess | RenameFileResultFailure
    """

    old_path: str
    new_path: str
    workspace_only: bool | None = True


@dataclass
@PayloadRegistry.register
class RenameFileResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """File/directory renamed successfully."""

    old_path: str
    new_path: str


@dataclass
@PayloadRegistry.register
class RenameFileResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """File/directory rename failed.

    Attributes:
        failure_reason: Classification of why the rename failed
        result_details: Human-readable error message (inherited from ResultPayloadFailure)
    """

    failure_reason: FileIOFailureReason


@dataclass
@PayloadRegistry.register
class GetNextUnusedFilenameRequest(RequestPayload):
    """Find the next available filename with auto-incrementing index (preview only - no file creation).

    Use when: Finding available filenames without file collision before actual write operations.

    This request scans the filesystem and returns the next available filename.
    This is a preview operation that DOES NOT create any files or acquire any locks.

    Args:
        file_path: Path to the file (str for direct path, MacroPath for macro resolution)

    Results: GetNextUnusedFilenameResultSuccess | GetNextUnusedFilenameResultFailure

    Examples:
        # Simple string path - cleanest for most use cases
        file_path = "/outputs/render.png"
        # Returns: "/outputs/render.png" if available
        #          "/outputs/render_1.png" if render.png exists
        #          "/outputs/render_2.png" if render_1.png exists, etc.

        # MacroPath with required {_index} and padding
        file_path = MacroPath(
            parsed_macro=ParsedMacro("{outputs}/frame_{_index:05}.png"),
            variables={"outputs": "/abs/path"}
        )
        # Returns: "/abs/path/frame_00001.png", "/abs/path/frame_00002.png", etc.
        # Note: Always includes index, cannot return "frame.png"

        # MacroPath with optional {_index} - limited by separator position
        file_path = MacroPath(
            parsed_macro=ParsedMacro("{outputs}/frame{_index?:_}.png"),
            variables={"outputs": "/abs/path"}
        )
        # Returns: "/abs/path/frame.png" if {_index} omitted
        #          "/abs/path/frame1_.png" if {_index}=1 (separator goes after value)
        # Note: Cannot achieve "frame.png" → "frame_1.png" with optional variable
    """

    file_path: str | MacroPath


@dataclass
@PayloadRegistry.register
class GetNextUnusedFilenameResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Next unused filename found (preview only - no file created).

    Attributes:
        available_filename: Absolute path to the available filename
        index_used: The index number that was used (e.g., 1, 2, 3...), or None if base filename is available
    """

    available_filename: str
    index_used: int | None


@dataclass
@PayloadRegistry.register
class GetNextUnusedFilenameResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Failed to find available filename.

    Attributes:
        failure_reason: Classification of why the operation failed
        result_details: Human-readable error message (inherited from ResultPayloadFailure)
    """

    failure_reason: FileIOFailureReason


@dataclass(kw_only=True)
@PayloadRegistry.register
class WriteFileRequest(RequestPayload):
    """Write content to a file.

    Automatically detects text vs binary mode based on content type.

    Use when: Saving generated content, writing output files,
    creating configuration files, writing binary data.

    Args:
        file_path: Path to the file to write (str for direct path, MacroPath for macro resolution)
        content: Content to write (str for text files, bytes for binary files)
        encoding: Text encoding for str content (default: 'utf-8', ignored for bytes)
        append: If True, append to existing file; if False, use existing_file_policy (default: False)
        existing_file_policy: How to handle existing files when append=False:
            - "overwrite": Replace file content (default)
            - "fail": Return failure if file exists
            - "create_new": Create new file with auto-incrementing index (e.g., file_1.txt, file_2.txt)
        create_parents: If True, create parent directories if missing (default: True)
        skip_metadata_injection: If True, skip automatic workflow metadata injection for supported file types
            (default: False). Use when the content already contains metadata to avoid double-injection.
        file_metadata: Optional caller-provided situation and variable context to include in the sidecar
            metadata file.
        coerce_extension_to_match_bytes: If True (default), when the sniffed format of the bytes disagrees
            with the destination suffix, the on-disk file is renamed to match the sniffed extension and a
            warning is logged. If False, a WriteFileResultFailure with EXTENSION_MISMATCH is returned and
            no file is left on disk.

    Results: WriteFileResultSuccess | WriteFileResultFailure

    Note: existing_file_policy is ignored when append=True (append always allows existing files)
    """

    broadcast_result: bool = False
    file_path: str | MacroPath
    content: str | bytes
    encoding: str = "utf-8"  # Ignored for bytes
    append: bool = False
    existing_file_policy: ExistingFilePolicy = ExistingFilePolicy.OVERWRITE
    create_parents: bool = True
    skip_metadata_injection: bool = False
    file_metadata: SidecarContent | None = None
    coerce_extension_to_match_bytes: bool = True


@dataclass
@PayloadRegistry.register
class WriteFileResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """File written successfully.

    Attributes:
        final_file_path: The actual path where file was written
                        (may differ from requested path if create_new policy used)
        bytes_written: Number of bytes written to the file
    """

    final_file_path: str
    bytes_written: int


@dataclass
@PayloadRegistry.register
class WriteFileResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """File write failed.

    Attributes:
        failure_reason: Classification of why the write failed
        missing_variables: Set of missing variable names (for MISSING_MACRO_VARIABLES failures)
        result_details: Human-readable error message (inherited from ResultPayloadFailure)
    """

    failure_reason: FileIOFailureReason
    missing_variables: set[str] | None = None


@dataclass
@PayloadRegistry.register
class CopyTreeRequest(RequestPayload):
    """Copy an entire directory tree from source to destination.

    Use when: Copying directories recursively, backing up directory structures,
    duplicating folder hierarchies with all contents.

    Args:
        source_path: Path to the source directory to copy
        destination_path: Path where the directory tree should be copied
        symlinks: If True, copy symbolic links as links (default: False)
        ignore_dangling_symlinks: If True, ignore dangling symlinks (default: False)
        dirs_exist_ok: If True, allow destination to exist (default: False)
        ignore_patterns: List of glob patterns to ignore (e.g., ["__pycache__", "*.pyc", ".git"])

    Results: CopyTreeResultSuccess | CopyTreeResultFailure
    """

    source_path: str
    destination_path: str
    symlinks: bool = False
    ignore_dangling_symlinks: bool = False
    dirs_exist_ok: bool = False
    ignore_patterns: list[str] | None = None


@dataclass
@PayloadRegistry.register
class CopyTreeResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Directory tree copied successfully.

    Attributes:
        source_path: Source path that was copied
        destination_path: Destination path where tree was copied
        files_copied: Number of files copied
        total_bytes_copied: Total bytes copied
    """

    source_path: str
    destination_path: str
    files_copied: int
    total_bytes_copied: int


@dataclass
@PayloadRegistry.register
class CopyTreeResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Directory tree copy failed.

    Attributes:
        failure_reason: Classification of why the copy failed
        result_details: Human-readable error message (inherited from ResultPayloadFailure)
    """

    failure_reason: FileIOFailureReason


@dataclass
@PayloadRegistry.register
class CopyFileRequest(RequestPayload):
    """Copy a single file from source to destination.

    Use when: Copying individual files, duplicating files,
    backing up single files.

    Args:
        source_path: Path to the source file to copy
        destination_path: Path where the file should be copied
        overwrite: If True, overwrite destination if it exists (default: False)

    Results: CopyFileResultSuccess | CopyFileResultFailure
    """

    source_path: str
    destination_path: str
    overwrite: bool = False


@dataclass
@PayloadRegistry.register
class CopyFileResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """File copied successfully.

    Attributes:
        source_path: Source path that was copied
        destination_path: Destination path where file was copied
        bytes_copied: Number of bytes copied
    """

    source_path: str
    destination_path: str
    bytes_copied: int


@dataclass
@PayloadRegistry.register
class CopyFileResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """File copy failed.

    Attributes:
        failure_reason: Classification of why the copy failed
        result_details: Human-readable error message (inherited from ResultPayloadFailure)
    """

    failure_reason: FileIOFailureReason


@dataclass
@PayloadRegistry.register
class DeleteFileRequest(RequestPayload):
    """Delete a file or directory.

    Use when: Deleting files/directories through file picker,
    implementing file deletion functionality, cleaning up temporary files.

    Note: Directories are always deleted with all their contents.

    Args:
        path: Path to file/directory to delete (mutually exclusive with file_entry)
        file_entry: FileSystemEntry from directory listing (mutually exclusive with path)
        workspace_only: If True, constrain to workspace directory
        deletion_behavior: How to handle deletion (permanent, recycle bin only, or prefer recycle bin)

    Results: DeleteFileResultSuccess | DeleteFileResultFailure
    """

    path: str | None = None
    file_entry: FileSystemEntry | None = None
    workspace_only: bool | None = True
    deletion_behavior: DeletionBehavior = DeletionBehavior.PREFER_RECYCLE_BIN


@dataclass
@PayloadRegistry.register
class DeleteFileResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """File/directory deleted successfully.

    Attributes:
        deleted_path: The absolute path that was deleted (primary path)
        was_directory: Whether the deleted item was a directory
        deleted_paths: List of all paths that were deleted (for recursive deletes, includes all files/dirs)
        outcome: The actual outcome of the deletion (permanently deleted or sent to recycle bin)
    """

    deleted_path: str
    was_directory: bool
    deleted_paths: list[str]
    outcome: DeletionOutcome


@dataclass
@PayloadRegistry.register
class DeleteFileResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """File/directory deletion failed.

    Attributes:
        failure_reason: Classification of why the deletion failed
        result_details: Human-readable error message (inherited from ResultPayloadFailure)
    """

    failure_reason: FileIOFailureReason


@dataclass
@PayloadRegistry.register
class GetFileInfoRequest(RequestPayload):
    """Get information about a file or directory.

    Use when: Checking if a path exists, determining if path is file/directory,
    getting file metadata before operations.

    Args:
        path: Path to file/directory to get info about
        workspace_only: If True, constrain to workspace directory

    Results: GetFileInfoResultSuccess | GetFileInfoResultFailure
    """

    path: str
    workspace_only: bool | None = True


@dataclass
@PayloadRegistry.register
class GetFileInfoResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """File/directory either did not exist (we do not treat this as failure), or the info was retrieved successfully.

    Attributes:
        file_entry: FileSystemEntry with complete metadata, or None if the file/directory doesn't exist
    """

    file_entry: FileSystemEntry | None


@dataclass
@PayloadRegistry.register
class GetFileInfoResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """File/directory info retrieval failed.

    Attributes:
        failure_reason: Classification of why retrieval failed
        result_details: Human-readable error message (inherited from ResultPayloadFailure)
    """

    failure_reason: FileIOFailureReason


@dataclass
@PayloadRegistry.register
class ResolveMacroPathRequest(RequestPayload):
    """Resolve a MacroPath to an absolute path string.

    Use when: Need to convert a MacroPath with variables to a concrete file path.

    Args:
        macro_path: MacroPath with parsed macro and variables

    Results: ResolveMacroPathResultSuccess | ResolveMacroPathResultFailure
    """

    macro_path: MacroPath


@dataclass
@PayloadRegistry.register
class ResolveMacroPathResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """MacroPath resolved successfully.

    Attributes:
        resolved_path: The resolved absolute path string
    """

    resolved_path: str


@dataclass
@PayloadRegistry.register
class ResolveMacroPathResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Failed to resolve MacroPath.

    Attributes:
        missing_variables: Set of variable names that were required but not provided
        result_details: Human-readable error message (inherited from ResultPayloadFailure)
    """

    missing_variables: set[str] | None = None


@dataclass
@PayloadRegistry.register
class GetNextVersionIndexRequest(RequestPayload):
    """Find the next available version index for a versioned path pattern.

    Use when: Allocating a new versioned output slot (e.g. ``v001``, ``v002``) without
    creating any files. The caller passes a ``MacroPath`` whose template contains an
    unresolved ``{_index}`` placeholder. The OS manager performs a single glob pass over
    the parent directory to discover which indices are already taken and returns the
    lowest unused one (gaps are filled first).

    This is a read-only preview operation — no files or directories are created.

    Args:
        macro_path: MacroPath whose template contains ``{_index}`` (required or optional).
            All variables other than ``_index`` must be resolved in ``macro_path.variables``
            so the manager can build a concrete glob pattern.

    Results: GetNextVersionIndexResultSuccess | GetNextVersionIndexResultFailure

    Examples:
        MacroPath with required ``{_index:03}``:
            Template: ``"{outputs}/render_v{_index:03}"``
            Variables: ``{"outputs": "/abs/path"}``
            Existing dirs: ``render_v001``, ``render_v002``, ``render_v004``
            Returns: ``GetNextVersionIndexResultSuccess(index=3)``  # fills the gap

        MacroPath with no existing entries:
            Returns: ``GetNextVersionIndexResultSuccess(index=1)``

        MacroPath with optional ``{_index?:_}`` and base path free:
            Returns: ``GetNextVersionIndexResultSuccess(index=None)``
            (callers that always need an integer should treat ``None`` as ``1``)
    """

    macro_path: MacroPath


@dataclass
@PayloadRegistry.register
class GetNextVersionIndexResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Next available version index found (read-only preview — no files created).

    Attributes:
        index: The lowest unused version index (1-based), or ``None`` when the
            ``{_index}`` placeholder is optional and the un-indexed base path is
            still available. Callers that always require an integer (e.g. directory
            versioning) should treat ``None`` as ``1``.
    """

    index: int | None


@dataclass
@PayloadRegistry.register
class GetNextVersionIndexResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Failed to determine the next available version index.

    Attributes:
        failure_reason: Classification of why the operation failed
        result_details: Human-readable error message (inherited from ResultPayloadFailure)
    """

    failure_reason: FileIOFailureReason


@dataclass
@PayloadRegistry.register
class MakeDirectoryRequest(RequestPayload):
    """Create a directory, optionally including intermediate parent directories.

    Use when: Creating output directories, setting up workspace folder structures,
    ensuring a directory exists before writing files into it. Prefer this over
    CreateFileRequest when the goal is purely directory creation.

    Args:
        path: Absolute path to the directory to create
        create_parents: If True, create intermediate directories as needed (default: True)
        exist_ok: If True, succeed silently when the directory already exists (default: True).
                  Set to False to get a POLICY_NO_OVERWRITE failure if the directory exists.

    Results: MakeDirectoryResultSuccess | MakeDirectoryResultFailure
    """

    path: str
    create_parents: bool = True
    exist_ok: bool = True


@dataclass
@PayloadRegistry.register
class MakeDirectoryResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Directory created (or already existed) successfully.

    Attributes:
        created_path: Absolute path of the directory
        already_existed: True when the directory already existed and exist_ok=True
    """

    created_path: str
    already_existed: bool = False


@dataclass
@PayloadRegistry.register
class MakeDirectoryResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Directory creation failed.

    Attributes:
        failure_reason: Classification of why the creation failed
        result_details: Human-readable error message (inherited from ResultPayloadFailure)
    """

    failure_reason: FileIOFailureReason
