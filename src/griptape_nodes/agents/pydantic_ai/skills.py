"""Agent Skills from the workspace, offered to the sidebar agent.

`pydantic-ai-harness`'s `Skills` reads and validates each `SKILL.md`, and its
`FileSystem` tools let the model read the files a skill ships, such as `references/`
documents its instructions point to. Nothing here runs a skill's scripts.

Both read through a pydantic-ai workspace backend. Its local backend runs commands and
refuses to start outside POSIX, so skills are read through `ReadOnlyDirectoryBackend`,
which only reads files and works on every platform.
"""

from __future__ import annotations

import errno
import logging
import posixpath
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Never

from pydantic_ai.capabilities import AbstractCapability, Capability
from pydantic_ai.workspaces import FileEntry, ReadOnlyWorkspace, Workspace, WorkspaceReadOnlyError
from pydantic_ai_harness.filesystem import FileSystem
from pydantic_ai_harness.skills import Skills

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic_ai import RunContext
    from pydantic_ai.workspaces import WorkspaceRef
    from pydantic_ai_harness.skills import SkillDefinition

logger = logging.getLogger("griptape_nodes")

# The Agent Skills spec caps `description` at this many characters. Harness only checks it
# when a run emits warnings, which `Skills.load` does not.
_MAX_DESCRIPTION_LENGTH = 1024

_READ_ONLY_REASON = "Skills are read-only."


@dataclass(frozen=True)
class WorkspaceSkills:
    """The skills one run offers the model.

    Attributes:
        names: The skills in the catalog, sorted.
        capabilities: One deferred capability per skill, plus the read-only file tools over the skills library.
    """

    names: list[str] = field(default_factory=list)
    capabilities: list[AbstractCapability[Any]] = field(default_factory=list)


class ReadOnlyDirectoryBackend:
    """A pydantic-ai workspace backend that reads one host directory and runs nothing.

    Implements the `pydantic_ai.workspaces.WorkspaceBackend` protocol. The directory
    appears as the workspace root `/`, so its paths are POSIX paths on every platform.
    Writes raise `WorkspaceReadOnlyError`.

    Each entry directly under the root bounds the paths beneath it. The entry itself may be
    a symlink, so a skill folder can live elsewhere, but a link inside it that points out of
    it reads as missing. That keeps a skill from exposing other files on disk.
    """

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    @property
    def ref(self) -> None:
        return None

    async def working_dir(self) -> str:
        return "/"

    def host_path(self, path: str) -> Path:
        """The host path for an absolute workspace path. `..` cannot climb above the root."""
        parts = PurePosixPath(posixpath.normpath("/" + path.lstrip("/"))).parts[1:]
        return self._root.joinpath(*parts)

    async def read_bytes(self, path: str) -> bytes:
        return self._contained_path(path).read_bytes()

    async def stat(self, path: str) -> FileEntry:
        target = self._contained_path(path)
        info = target.stat()
        is_dir = target.is_dir()
        size = None
        if not is_dir:
            size = info.st_size
        return FileEntry(name=posixpath.basename(path), path=path, is_dir=is_dir, size=size)

    async def list_dir(self, path: str) -> Sequence[FileEntry]:
        entries: list[FileEntry] = []
        for child in sorted(self._contained_path(path).iterdir()):
            if not self._is_contained(child):
                continue
            child_path = posixpath.join(path, child.name)
            try:
                is_dir = child.is_dir()
            except OSError:
                # A symlink loop has no target; keep the entry rather than fail the listing.
                is_dir = False
            entries.append(FileEntry(name=child.name, path=child_path, is_dir=is_dir, size=None))
        return entries

    async def exists(self, path: str) -> bool:
        target = self.host_path(path)
        return self._is_contained(target) and target.exists()

    async def realpath(self, path: str) -> str:
        """Follow symlinks within the root. A path whose target is outside the root is returned as written."""
        resolved = self.host_path(path).resolve()
        try:
            relative = resolved.relative_to(self._root)
        except ValueError:
            return posixpath.normpath(path)
        return "/" + relative.as_posix() if relative.parts else "/"

    async def write_bytes(self, path: str, data: bytes) -> Never:  # noqa: ARG002
        raise WorkspaceReadOnlyError(_READ_ONLY_REASON)

    async def make_dir(self, path: str) -> Never:  # noqa: ARG002
        raise WorkspaceReadOnlyError(_READ_ONLY_REASON)

    async def remove(self, path: str) -> Never:  # noqa: ARG002
        raise WorkspaceReadOnlyError(_READ_ONLY_REASON)

    def _contained_path(self, path: str) -> Path:
        """The host path for `path`, which must stay inside the root entry it is under.

        Raises:
            FileNotFoundError: The path resolves outside that entry.
        """
        target = self.host_path(path)
        if not self._is_contained(target):
            raise FileNotFoundError(errno.ENOENT, "No such file in the skills directory", path)
        return target

    def _is_contained(self, target: Path) -> bool:
        """Whether `target` resolves inside the root entry it is under."""
        parts = target.relative_to(self._root).parts
        if not parts:
            return True
        try:
            boundary = (self._root / parts[0]).resolve()
            resolved = target.resolve()
        # A symlink loop raises `RuntimeError` on Python 3.12 and `OSError` from 3.13.
        except (OSError, RuntimeError):
            return False
        return resolved.is_relative_to(boundary)


@dataclass
class SkillsWorkspace(AbstractCapability[Any]):
    """Makes the skills library the run's workspace, read-only, for the `FileSystem` tools."""

    backend: ReadOnlyDirectoryBackend

    def get_workspace(self, ctx: RunContext[Any], *, ref: WorkspaceRef | None) -> Workspace | None:  # noqa: ARG002
        # The backend has no ref, so a ref from elsewhere names a workspace it does not own.
        if ref is not None:
            return None
        return ReadOnlyWorkspace(Workspace(self.backend))


async def load_workspace_skills(library: Path) -> WorkspaceSkills:
    """Read the skills in `library` for one run.

    A skill whose `SKILL.md` is invalid is left out with a warning. A failure that
    affects the whole library, such as an unreadable `SKILL.md`, costs every skill but
    not the run.
    """
    backend = ReadOnlyDirectoryBackend(library)
    try:
        catalog = await Skills(".", workspace=backend).load()
    except (OSError, ValueError) as e:
        logger.warning("Attempted to load skills from %s. Failed because of: %s", library, e)
        return WorkspaceSkills()

    for skipped in catalog.skipped:
        logger.warning("Attempted to load skills from %s. %s", library, skipped.message)
    for skill in catalog.skills:
        _warn_about_spec_gaps(skill, library)

    offered = sorted((skill for skill in catalog.skills if skill.model_invocable), key=lambda skill: skill.name)
    if not offered:
        return WorkspaceSkills()

    capabilities: list[AbstractCapability[Any]] = [SkillsWorkspace(backend), FileSystem()]
    capabilities.extend(_skill_capability(skill) for skill in offered)
    return WorkspaceSkills(names=[skill.name for skill in offered], capabilities=capabilities)


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


def _skill_capability(skill: SkillDefinition) -> Capability[Any]:
    """The deferred capability the model loads to get `skill`'s instructions."""
    # Read through `workspace=`, harness leaves the skill's directory out of its instructions.
    # `SkillsWorkspace` makes that same library the run's workspace, so the directory applies.
    instructions = replace(skill, in_run_workspace=True).render()
    # The catalog lists each skill as a `- name: description` line. Indenting continuation lines,
    # as harness does, keeps a multiline description from starting a line that reads as another entry.
    description = skill.description.replace("\n", "\n  ")
    return Capability(id=skill.name, description=description, instructions=[instructions], defer_loading=True)
