"""FileSequence and FileSequenceDestination for numbered file collections (frames, audio takes, etc.).

Templates like ``{outputs}/dialogue_v{_index:03}/####.wav`` drive both reading
(FileSequence) and writing (FileSequenceDestination). The ``####`` slot is a
literal string that the sequence scanning layer (fileseq) understands natively;
it is NOT a macro variable, so it passes through ``GetPathForMacroRequest``
resolution unchanged. Scanning delegates to the engine via ScanSequencesRequest;
versioned destinations lock a free directory index via
build_versioned_sequence_destination.
"""

from __future__ import annotations

import pathlib
import typing

from fileseq import constants as fileseq_constants
from fileseq import filesequence as fileseq_filesequence

from griptape_nodes.common import macro_parser, sequences
from griptape_nodes.files import directory as directory_mod
from griptape_nodes.files import file as file_mod
from griptape_nodes.files import project_file
from griptape_nodes.retained_mode import griptape_nodes as griptape_nodes_mod
from griptape_nodes.retained_mode.events import os_events, project_events

if typing.TYPE_CHECKING:
    import collections.abc


class FileSequenceError(Exception):
    """Raised when a file sequence operation fails."""

    def __init__(self, result_details: str) -> None:
        self.result_details = result_details
        super().__init__(result_details)


def _resolve_entry_path(macro_path: project_events.MacroPath, entry_number: int) -> str:
    """Resolve macro variables then use fileseq to format the entry number.

    Supports all sequence token formats fileseq understands: ``####``,
    ``%04d``, ``@@@@``, ``$F4``, etc.

    Args:
        macro_path: MacroPath whose template contains a sequence token slot.
        entry_number: Frame/entry index to substitute.

    Returns:
        Absolute path string for the given entry.

    Raises:
        FileSequenceError: If the macro path cannot be resolved.
    """
    resolve_result = griptape_nodes_mod.GriptapeNodes.handle_request(
        project_events.GetPathForMacroRequest(
            parsed_macro=macro_path.parsed_macro,
            variables=macro_path.variables,
        )
    )
    if not isinstance(resolve_result, project_events.GetPathForMacroResultSuccess):
        msg = f"Attempted to get entry {entry_number}. Failed to resolve sequence path: {resolve_result.result_details}"
        raise FileSequenceError(msg)
    fseq = fileseq_filesequence.FileSequence(
        str(resolve_result.absolute_path),
        pad_style=fileseq_constants.PAD_STYLE_HASH1,
    )
    return fseq.frame(entry_number)


class FileSequence:
    """A collection of files identified by a #### hash-pattern slot.

    Stores a MacroPath whose template contains a run of ``#`` characters
    (e.g. ``####``) representing the sequence number.  All other variables
    in the template (``{outputs}``, ``{_index}``, …) are resolved through
    the normal macro system; the ``####`` slot is plain literal text that
    fileseq understands natively.

    Use ``entry(n)`` to get a ``File`` for reading a specific entry, and
    ``scan()`` to discover what is present on disk.
    """

    def __init__(self, macro_path: project_events.MacroPath) -> None:
        """Store the hash-pattern macro. No I/O is performed.

        Args:
            macro_path: MacroPath whose template contains a ``####`` slot and
                whose variables dict holds all resolved values (including a
                locked ``_index`` when versioning is in effect).
        """
        self._macro_path = macro_path

    @property
    def location(self) -> str:
        """Return the raw macro template for this sequence.

        Unresolved placeholders (including ``{_index}`` when versioning is in
        effect) remain in the returned string.  The ``####`` slot is also
        returned as-is.

        Example: ``"{outputs}/dialogue_v{_index:03}/####.wav"``
        """
        return self._macro_path.parsed_macro.template

    @property
    def directory(self) -> directory_mod.Directory:
        """Return the containing directory as a Directory.

        No I/O is performed; the directory path is derived from the macro
        template by stripping the filename component.  The locked variables
        (e.g. ``_index``) are preserved so the returned Directory can be resolved.
        """
        dir_template = str(pathlib.PurePosixPath(self.location).parent)
        return directory_mod.Directory(
            project_events.MacroPath(macro_parser.ParsedMacro(dir_template), self._macro_path.variables)
        )

    def entry(self, entry_number: int) -> file_mod.File:
        """Return a File for reading a specific entry.

        Args:
            entry_number: Entry index (caller's convention, e.g. 0-based or 1-based).

        Returns:
            File that resolves to the absolute path of that entry.

        Raises:
            FileSequenceError: If the macro path cannot be resolved.
        """
        return file_mod.File(_resolve_entry_path(self._macro_path, entry_number))

    def scan(
        self,
        *,
        policy: sequences.MissingItemPolicy = sequences.MissingItemPolicy.SPLIT,
        start: int | None = None,
        end: int | None = None,
    ) -> list[sequences.Sequence]:
        """Scan the sequence directory and return what's on disk.

        The ``####`` slot in the template passes through macro resolution
        unchanged, so the resolved path is handed directly to
        ``ScanSequencesRequest`` without any string manipulation.

        Args:
            policy: How to handle gaps in the number range. Defaults to SPLIT.
            start: Optional lower bound (inclusive) for the active subset.
            end: Optional upper bound (inclusive) for the active subset.

        Returns:
            List of Sequence objects. Empty if the directory exists but contains
            no matching files.

        Raises:
            FileSequenceError: If the macro path cannot be resolved.
        """
        resolve_result = griptape_nodes_mod.GriptapeNodes.handle_request(
            project_events.GetPathForMacroRequest(
                parsed_macro=self._macro_path.parsed_macro,
                variables=self._macro_path.variables,
            )
        )
        if not isinstance(resolve_result, project_events.GetPathForMacroResultSuccess):
            msg = f"Attempted to scan sequence. Failed to resolve macro path: {resolve_result.result_details}"
            raise FileSequenceError(msg)
        scan_result = griptape_nodes_mod.GriptapeNodes.handle_request(
            os_events.ScanSequencesRequest(
                path=str(resolve_result.absolute_path),
                policy=policy,
                start_number=start,
                end_number=end,
            )
        )
        if not isinstance(scan_result, os_events.ScanSequencesResultSuccess):
            return []
        return scan_result.sequences


class _EntryWriteDestination(project_file.ProjectFileDestination):
    """FileDestination subclass that fires a callback after each successful write."""

    def __init__(
        self,
        entry_path: str | project_events.MacroPath,
        *,
        existing_file_policy: os_events.ExistingFilePolicy,
        create_parents: bool,
        on_written: collections.abc.Callable[[file_mod.File], None],
    ) -> None:
        super().__init__(
            entry_path,
            existing_file_policy=existing_file_policy,
            create_parents=create_parents,
        )
        self._on_written = on_written

    def write_bytes(self, content: bytes) -> file_mod.File:
        result = super().write_bytes(content)
        self._on_written(result)
        return result

    async def awrite_bytes(self, content: bytes) -> file_mod.File:
        result = await super().awrite_bytes(content)
        self._on_written(result)
        return result

    def write_text(self, content: str, encoding: str = "utf-8") -> file_mod.File:
        result = super().write_text(content, encoding)
        self._on_written(result)
        return result

    async def awrite_text(self, content: str, encoding: str = "utf-8") -> file_mod.File:
        result = await super().awrite_text(content, encoding)
        self._on_written(result)
        return result


class FileSequenceDestination:
    """A pre-configured write handle for a file sequence.

    Bundles a hash-pattern macro path and write policy.  The caller resolves a
    version index once (via ``build_versioned_sequence_destination``), then
    calls ``entry(n)`` to get a ``FileDestination`` for each entry.

    The ``file_sequence`` property becomes non-None after the first entry write.
    """

    def __init__(
        self,
        macro_path: project_events.MacroPath,
        *,
        existing_file_policy: os_events.ExistingFilePolicy = os_events.ExistingFilePolicy.OVERWRITE,
        create_parents: bool = True,
    ) -> None:
        """Store the hash-pattern macro and write configuration. No I/O is performed.

        Args:
            macro_path: MacroPath with template containing a ``####`` slot.
                Should already have ``_index`` locked in the variables dict
                when versioning is in effect.
            existing_file_policy: How to handle existing entry files. Defaults to OVERWRITE.
            create_parents: If True, create parent directories automatically. Defaults to True.
        """
        self._macro_path = macro_path
        self._existing_file_policy = existing_file_policy
        self._create_parents = create_parents
        self._written_sequence: FileSequence | None = None
        self._fseq: fileseq_filesequence.FileSequence | None = None

    @property
    def file_sequence(self) -> FileSequence | None:
        """Return the FileSequence descriptor after at least one entry has been written.

        Returns None before any entry write.
        """
        return self._written_sequence

    def entry(self, entry_number: int) -> file_mod.FileDestination:
        """Return a FileDestination for writing a specific entry.

        The macro path is resolved on the first call and cached for all
        subsequent calls, so writing many entries fires only one
        ``GetPathForMacroRequest``.

        After the returned destination is used to write, the ``file_sequence``
        property becomes available.

        Args:
            entry_number: Entry index to write.

        Returns:
            FileDestination pre-configured with the resolved entry path and policy.

        Raises:
            FileSequenceError: If the macro path cannot be resolved (first call only).
        """
        return _EntryWriteDestination(
            self._resolve_fseq().frame(entry_number),
            existing_file_policy=self._existing_file_policy,
            create_parents=self._create_parents,
            on_written=self._on_entry_written,
        )

    def _resolve_fseq(self) -> fileseq_filesequence.FileSequence:
        """Resolve the macro path to a fileseq FileSequence, caching the result."""
        if self._fseq is None:
            resolve_result = griptape_nodes_mod.GriptapeNodes.handle_request(
                project_events.GetPathForMacroRequest(
                    parsed_macro=self._macro_path.parsed_macro,
                    variables=self._macro_path.variables,
                )
            )
            if not isinstance(resolve_result, project_events.GetPathForMacroResultSuccess):
                msg = f"Attempted to prepare sequence for writing. Failed to resolve macro path: {resolve_result.result_details}"
                raise FileSequenceError(msg)
            self._fseq = fileseq_filesequence.FileSequence(
                str(resolve_result.absolute_path),
                pad_style=fileseq_constants.PAD_STYLE_HASH1,
            )
        return self._fseq

    def _on_entry_written(self, written_file: file_mod.File) -> None:  # noqa: ARG002
        """Record that an entry was written to expose the FileSequence descriptor."""
        if self._written_sequence is None:
            self._written_sequence = FileSequence(self._macro_path)


def build_versioned_sequence_destination(
    macro_path: project_events.MacroPath,
    *,
    existing_file_policy: os_events.ExistingFilePolicy = os_events.ExistingFilePolicy.OVERWRITE,
    create_parents: bool = True,
) -> FileSequenceDestination:
    """Find the next available version index and return a locked FileSequenceDestination.

    Delegates index discovery to GetNextVersionIndexRequest (a single glob pass),
    then locks the returned index into the macro variables.

    Args:
        macro_path: MacroPath template with a ``####`` slot and a ``{_index}``
            variable for versioning.
        existing_file_policy: Policy for individual entry files. Defaults to OVERWRITE.
        create_parents: Whether to create parent directories. Defaults to True.

    Returns:
        FileSequenceDestination with a locked _index version.

    Raises:
        FileSequenceError: If the engine cannot determine the next available version index.
    """
    dir_template = str(pathlib.PurePosixPath(macro_path.parsed_macro.template).parent)
    dir_macro = project_events.MacroPath(macro_parser.ParsedMacro(dir_template), macro_path.variables)

    index_result = griptape_nodes_mod.GriptapeNodes.handle_request(
        os_events.GetNextVersionIndexRequest(macro_path=dir_macro)
    )
    if not isinstance(index_result, os_events.GetNextVersionIndexResultSuccess):
        msg = (
            f"Attempted to find available sequence version. Failed to find version index: {index_result.result_details}"
        )
        raise FileSequenceError(msg)

    index = index_result.index if index_result.index is not None else 1
    locked_vars = {**macro_path.variables, "_index": index}
    locked_macro = project_events.MacroPath(macro_path.parsed_macro, locked_vars)
    return FileSequenceDestination(
        locked_macro,
        existing_file_policy=existing_file_policy,
        create_parents=create_parents,
    )
