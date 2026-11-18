"""Which id each workflow file is registered under.

A workflow's id is saved in its file's header, so it survives restarts, renames and moves. Files
saved before ids were written, and files a library provides, get ids derived from where they are.
`reconcile_workflow_ids` decides every file's id in one place, for the workspace scan, library
registration, and a worker registering its orchestrator's workflow.
"""

from __future__ import annotations

import posixpath
import uuid
from enum import StrEnum
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from pathlib import Path

# Namespace of the ids derived for workflow files, so the same file gets the same id in every engine.
_DERIVED_WORKFLOW_ID_NAMESPACE = uuid.UUID("6f4b6c0e-2c1e-4a8e-9b1a-3d2f5e7c9a10")


def new_workflow_id() -> str:
    """Mint the id of a workflow that has none yet, such as a new one or a copy. Saving it writes the id to its file."""
    return uuid.uuid4().hex


def path_workflow_id(absolute_path: Path) -> str:
    """The id of the workflow file at `absolute_path` while its header carries none."""
    return uuid.uuid5(_DERIVED_WORKFLOW_ID_NAMESPACE, f"path:{absolute_path}").hex


def library_workflow_id(library_name: str, relative_path: str | Path) -> str:
    """The id of the workflow a library provides at `relative_path`, relative to the library's directory.

    Derived rather than read from the file, so two libraries that ship copies of one file, or a
    library file copied from a user's workflow, never share an id.
    """
    path = PurePosixPath(posixpath.normpath(str(relative_path).replace("\\", "/")))
    return uuid.uuid5(_DERIVED_WORKFLOW_ID_NAMESPACE, f"library:{library_name}:{path}").hex


def is_workflow_id(candidate: str) -> bool:
    """Whether `candidate` is in the form every workflow id takes: 32 lowercase hex digits."""
    try:
        workflow_id = uuid.UUID(hex=candidate).hex
    except ValueError:
        return False
    return workflow_id == candidate


class FoundWorkflowFile(NamedTuple):
    """A workflow file to register, and what says which id it should have.

    Args:
        path: The file's canonical absolute path.
        header_id: The id its header carries, or None when it carries none.
        library_id: The id of the library workflow it is (`library_workflow_id`), or None.
        peer_id: The id another engine knows it by, which wins over everything else. Set by a
            worker registering its orchestrator's workflow.
    """

    path: Path
    header_id: str | None = None
    library_id: str | None = None
    peer_id: str | None = None


class RegisteredWorkflowFile(NamedTuple):
    """A registry entry, as reconciling sees it.

    Args:
        workflow_id: The entry's id.
        paths: Every path its file is registered by. Empty for an unsaved workflow.
        file_present: Whether its file is still on disk.
    """

    workflow_id: str
    paths: tuple[Path, ...]
    file_present: bool


class WorkflowIdReason(StrEnum):
    """Why a file gets the id it gets."""

    # The file is already registered, and keeps its entry's id.
    REGISTERED = "registered"
    # Another engine knows the file by this id.
    PEER = "peer"
    # A library provides the file.
    LIBRARY = "library"
    # The file's header carries the id.
    HEADER = "header"
    # The file's header carries no id.
    PATH = "path"
    # The file carries the id of an entry whose file is gone, so it is that file, moved.
    MOVED = "moved"
    # The id the file should have belongs to another file, as with a copy made outside the editor.
    DUPLICATE = "duplicate"


class WorkflowIdDecision(NamedTuple):
    """The id a found file is registered under.

    Args:
        file: The file.
        workflow_id: The id it is registered under.
        reason: Why.
        entry_id: The registered entry it lands on: the one at its path, or the one it was moved
            from. None registers a new entry.
        displaced_id: An entry registered at its path under another id, which goes. Only another
            engine's id displaces an entry.
    """

    file: FoundWorkflowFile
    workflow_id: str
    reason: WorkflowIdReason
    entry_id: str | None = None
    displaced_id: str | None = None


def reconcile_workflow_ids(
    found: Sequence[FoundWorkflowFile], registered: Iterable[RegisteredWorkflowFile]
) -> list[WorkflowIdDecision]:
    """Decide the id of every file in `found` against what is `registered`, in path order.

    A file registered already keeps its entry. Otherwise a file takes, in order of precedence, the id
    another engine gives it, its library's id, its header's id, or its path's id. When that id
    belongs to an entry whose file is gone, the file is that file moved, and takes over the entry.
    When it belongs to an entry whose file is present, or to a file decided earlier in `found`, the
    file is a copy and gets its path's id instead, so the file already registered keeps the id.

    Reads nothing and changes nothing: `registered` says whose file is present. It needs only the
    entries holding the paths in `found` and the ids they could take.
    """
    entries = list(registered)
    by_id: dict[str, RegisteredWorkflowFile] = {entry.workflow_id: entry for entry in entries}
    by_path: dict[Path, RegisteredWorkflowFile] = {path: entry for entry in entries for path in entry.paths}
    # Ids whose entry's file is present belong to that file. Ids decided below are added as they go.
    claimed = {entry.workflow_id for entry in entries if entry.file_present}
    decisions = []
    for file in sorted(found, key=lambda candidate: str(candidate.path)):
        decision = _decide(file, by_id=by_id, by_path=by_path, claimed=claimed)
        claimed.add(decision.workflow_id)
        decisions.append(decision)
    return decisions


def _decide(
    file: FoundWorkflowFile,
    *,
    by_id: Mapping[str, RegisteredWorkflowFile],
    by_path: Mapping[Path, RegisteredWorkflowFile],
    claimed: set[str],
) -> WorkflowIdDecision:
    at_path = by_path.get(file.path)
    if file.peer_id is not None:
        return _decide_peer(file, file.peer_id, at_path=at_path, by_id=by_id)
    if at_path is not None:
        return WorkflowIdDecision(file, at_path.workflow_id, WorkflowIdReason.REGISTERED, entry_id=at_path.workflow_id)

    path_id = path_workflow_id(file.path)
    if file.library_id is not None:
        wanted, reason = file.library_id, WorkflowIdReason.LIBRARY
    elif file.header_id is not None:
        wanted, reason = file.header_id, WorkflowIdReason.HEADER
    else:
        wanted, reason = path_id, WorkflowIdReason.PATH

    if wanted not in claimed:
        holder = by_id.get(wanted)
        if holder is not None and holder.paths:
            return WorkflowIdDecision(file, wanted, WorkflowIdReason.MOVED, entry_id=wanted)
        if holder is None:
            return WorkflowIdDecision(file, wanted, reason)

    duplicate_id = path_id
    if duplicate_id == wanted or duplicate_id in claimed or duplicate_id in by_id:
        duplicate_id = uuid.uuid5(_DERIVED_WORKFLOW_ID_NAMESPACE, f"duplicate:{file.path}:{wanted}").hex
    return WorkflowIdDecision(file, duplicate_id, WorkflowIdReason.DUPLICATE)


def _decide_peer(
    file: FoundWorkflowFile,
    peer_id: str,
    *,
    at_path: RegisteredWorkflowFile | None,
    by_id: Mapping[str, RegisteredWorkflowFile],
) -> WorkflowIdDecision:
    """Another engine's id wins: an entry at the path under another id goes, and one with the id elsewhere moves here."""
    displaced_id = None
    if at_path is not None and at_path.workflow_id != peer_id:
        displaced_id = at_path.workflow_id
    entry_id = None
    if peer_id in by_id:
        entry_id = peer_id
    return WorkflowIdDecision(file, peer_id, WorkflowIdReason.PEER, entry_id=entry_id, displaced_id=displaced_id)
