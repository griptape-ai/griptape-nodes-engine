"""Agent Skills read from skills folders and offered to the sidebar agent.

`pydantic-ai-harness`'s `Skills` reads and validates each `SKILL.md`, and its
`FileSystem` tools let the model read the files a skill ships, such as `references/`
documents its instructions point to. Nothing here runs a skill's scripts.

Both read through a pydantic-ai workspace backend. Its local backend runs commands and
refuses to start outside POSIX, so skills are read through `ReadOnlySkillsBackend`,
which only reads files and works on every platform.
"""

from __future__ import annotations

import errno
import logging
import os
import posixpath
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Never

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.workspaces import FileEntry, ReadOnlyWorkspace, Workspace, WorkspaceReadOnlyError
from pydantic_ai_harness.filesystem import FileSystem
from pydantic_ai_harness.skills import Skills

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from pydantic_ai import RunContext
    from pydantic_ai.workspaces import WorkspaceRef
    from pydantic_ai_harness.skills import SkillDefinition, SkippedSkill

logger = logging.getLogger("griptape_nodes")

# The Agent Skills spec caps `description` at this many characters. Harness only checks it
# when a run emits warnings, which `Skills.load` does not.
_MAX_DESCRIPTION_LENGTH = 1024

_READ_ONLY_REASON = "Skills are read-only."


@dataclass(frozen=True)
class LoadedSkill:
    """A skill read from a skills folder.

    Attributes:
        definition: The skill as `pydantic-ai-harness` parsed it.
        directory: The skill's folder on disk.
    """

    definition: SkillDefinition
    directory: Path


@dataclass(frozen=True)
class SkillLibrary:
    """What one skills folder holds.

    Attributes:
        skills: The skills that loaded, including ones whose `SKILL.md` keeps them from the model.
        skipped: What could not load, with paths relative to the folder, such as `/my-skill/SKILL.md`.
    """

    skills: list[LoadedSkill] = field(default_factory=list)
    skipped: list[SkippedSkill] = field(default_factory=list)


class ReadOnlySkillsBackend:
    """A pydantic-ai workspace backend that reads chosen host paths and runs nothing.

    Implements the `pydantic_ai.workspaces.WorkspaceBackend` protocol. Each entry appears
    under the workspace root by name, as `/<name>`, so paths are POSIX paths on every
    platform. Writes raise `WorkspaceReadOnlyError`.

    Each entry bounds the paths beneath it. An entry may itself be a symlink, so a skill
    folder can live elsewhere, but a link inside it that points out of it reads as missing.
    That keeps a skill from exposing other files on disk.
    """

    def __init__(self, entries: Mapping[str, Path]) -> None:
        self._entries = dict(entries)

    @property
    def ref(self) -> None:
        return None

    async def working_dir(self) -> str:
        return "/"

    def host_path(self, path: str) -> Path:
        """The host path for a workspace path under one of the entries.

        Raises:
            FileNotFoundError: The path is the root, names no entry, or resolves outside its entry.
        """
        parts = _parts(path)
        if not parts or parts[0] not in self._entries:
            raise FileNotFoundError(errno.ENOENT, "No such file in the skills directory", path)
        entry = self._entries[parts[0]]
        # Windows also splits on `\` and reads `C:` as a drive, which `_parts` cannot see.
        target = Path(os.path.normpath(entry.joinpath(*parts[1:])))
        if not target.is_relative_to(os.path.normpath(entry)) or not _resolves_inside(target, entry):
            raise FileNotFoundError(errno.ENOENT, "No such file in the skills directory", path)
        return target

    async def read_bytes(self, path: str) -> bytes:
        return self.host_path(path).read_bytes()

    async def stat(self, path: str) -> FileEntry:
        if not _parts(path):
            return FileEntry(name="", path="/", is_dir=True, size=None)
        target = self.host_path(path)
        info = target.stat()
        is_dir = target.is_dir()
        size = None
        if not is_dir:
            size = info.st_size
        return FileEntry(name=posixpath.basename(path), path=path, is_dir=is_dir, size=size)

    async def list_dir(self, path: str) -> Sequence[FileEntry]:
        names = sorted(self._entries)
        if _parts(path):
            names = sorted(child.name for child in self.host_path(path).iterdir())
        entries: list[FileEntry] = []
        for name in names:
            child_path = posixpath.join(path, name)
            try:
                is_dir = self.host_path(child_path).is_dir()
            except FileNotFoundError:
                # Outside its entry, so not part of the workspace.
                continue
            entries.append(FileEntry(name=name, path=child_path, is_dir=is_dir, size=None))
        return entries

    async def exists(self, path: str) -> bool:
        if not _parts(path):
            return True
        try:
            target = self.host_path(path)
        except FileNotFoundError:
            return False
        return target.exists()

    async def realpath(self, path: str) -> str:
        """Follow symlinks within an entry. A path that is missing or leaves its entry is returned as written."""
        parts = _parts(path)
        try:
            resolved = self.host_path(path).resolve()
        except FileNotFoundError:
            return posixpath.normpath("/" + path.lstrip("/"))
        relative = resolved.relative_to(self._entries[parts[0]].resolve())
        return posixpath.join("/", parts[0], *relative.parts)

    async def write_bytes(self, path: str, data: bytes) -> Never:  # noqa: ARG002
        raise WorkspaceReadOnlyError(_READ_ONLY_REASON)

    async def make_dir(self, path: str) -> Never:  # noqa: ARG002
        raise WorkspaceReadOnlyError(_READ_ONLY_REASON)

    async def remove(self, path: str) -> Never:  # noqa: ARG002
        raise WorkspaceReadOnlyError(_READ_ONLY_REASON)

    @classmethod
    def for_library(cls, library: Path) -> ReadOnlySkillsBackend:
        """A backend whose entries are what `library` holds, for reading its skills.

        Raises:
            OSError: `library` cannot be listed.
        """
        return cls({child.name: child for child in library.iterdir()})


@dataclass
class SkillsWorkspace(AbstractCapability[Any]):
    """Makes the offered skills the run's workspace, read-only, for the `FileSystem` tools."""

    backend: ReadOnlySkillsBackend

    def get_workspace(self, ctx: RunContext[Any], *, ref: WorkspaceRef | None) -> Workspace | None:  # noqa: ARG002
        # The backend has no ref, so a ref from elsewhere names a workspace it does not own.
        if ref is not None:
            return None
        return ReadOnlyWorkspace(Workspace(self.backend))


async def read_skill_library(library: Path) -> SkillLibrary:
    """Read the skills in one skills folder, and the ones it holds that cannot load.

    A folder that doesn't exist holds nothing. Any other failure that affects the whole
    folder, such as a folder that can't be listed, costs its skills but is not raised.
    """
    try:
        backend = ReadOnlySkillsBackend.for_library(library)
        catalog = await Skills(".", workspace=backend, missing_directories="skip").load()
    except FileNotFoundError:
        return SkillLibrary()
    except (OSError, ValueError) as e:
        logger.warning("Attempted to load skills from %s. Failed because of: %s", library, e)
        return SkillLibrary()

    for skipped in catalog.skipped:
        logger.warning("Attempted to load skills from %s. %s", library, skipped.message)
    for skill in catalog.skills:
        _warn_about_spec_gaps(skill, library)

    skills = [LoadedSkill(definition=skill, directory=backend.host_path(skill.directory)) for skill in catalog.skills]
    return SkillLibrary(skills=skills, skipped=list(catalog.skipped))


def offer_skills(folders: Sequence[Path]) -> list[AbstractCapability[Any]]:
    """The capabilities that offer the model the skills in `folders`, already read and chosen.

    Each folder appears at `/<folder name>` in the run's read-only workspace, where harness
    `Skills` reads it and the `FileSystem` tools reach its bundled files.
    """
    if not folders:
        return []
    backend = ReadOnlySkillsBackend({folder.name: folder for folder in folders})
    return [SkillsWorkspace(backend), FileSystem(), Skills("/")]


def _warn_about_spec_gaps(skill: SkillDefinition, library: Path) -> None:
    """Log what the Agent Skills spec asks of `skill` that is accepted but not honored."""
    if len(skill.description) > _MAX_DESCRIPTION_LENGTH:
        logger.warning(
            "Skill '%s' in %s has a description of %d characters, over the %d-character limit. "
            "The agent still sees all of it.",
            skill.name,
            library,
            len(skill.description),
            _MAX_DESCRIPTION_LENGTH,
        )
    if skill.ignored_behavioral_fields:
        logger.warning(
            "Skill '%s' in %s sets frontmatter fields that Griptape Nodes ignores: %s.",
            skill.name,
            library,
            ", ".join(skill.ignored_behavioral_fields),
        )


def _parts(path: str) -> list[str]:
    """The components of an absolute workspace path. `..` cannot climb above the root."""
    return list(PurePosixPath(posixpath.normpath("/" + path.lstrip("/"))).parts[1:])


def _resolves_inside(target: Path, entry: Path) -> bool:
    """Whether `target`, with symlinks followed, is inside `entry`."""
    try:
        boundary = entry.resolve()
        resolved = target.resolve()
    # A symlink loop raises `RuntimeError` on Python 3.12 and `OSError` from 3.13.
    except (OSError, RuntimeError):
        return False
    return resolved.is_relative_to(boundary)
