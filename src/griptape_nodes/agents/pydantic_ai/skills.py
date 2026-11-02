"""Agent Skills from the workspace, offered to the sidebar agent.

`pydantic-ai-harness`'s `Skills` reads and validates each `SKILL.md`. It does not
expose the other files a skill ships, such as `references/` documents that its
instructions tell the model to read. This module lists those files in each skill's
instructions and serves them through the `read_skill_resource` tool.

Harness reads skills through a pydantic-ai workspace backend. Its local backend runs
commands and refuses to start outside POSIX, so skills are read through
`ReadOnlyDirectoryBackend`, which only reads files and works on every platform.
"""

from __future__ import annotations

import codecs
import json
import logging
import os
import posixpath
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Annotated, Any, Never

import yaml
from pydantic import BeforeValidator
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.capabilities import AbstractCapability, Capability
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.workspaces import FileEntry, WorkspaceReadOnlyError
from pydantic_ai_harness.skills import Skills

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic_ai_harness.skills import SkillDefinition

logger = logging.getLogger("griptape_nodes")

READ_SKILL_RESOURCE = "read_skill_resource"

# Placeholders replaced with a skill's directory in its instructions. `${CLAUDE_SKILL_DIR}`
# is the spelling published Anthropic skills use; `${SKILL_DIR}` is the portable one.
_SKILL_DIR_PLACEHOLDERS = ("${SKILL_DIR}", "${CLAUDE_SKILL_DIR}")

# A skill shipping more files than this gets a count instead of a wall of names. The
# not-found retry still lists every file.
_MAX_LISTED_FILES = 50

_RESOURCE_EXCLUDES = ("__pycache__", "*.pyc", "*.pyo", ".DS_Store", ".git")

# Files that look like scripts are not resources, even though skills here cannot run them.
_SCRIPT_EXTENSIONS = {".py", ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd"}
_WINDOWS_EXECUTABLE_EXTENSIONS = {".exe", ".bat", ".cmd", ".com", ".ps1"}
_IGNORED_SCRIPT_NAMES = {"__init__.py", "SKILL.md"}

# How much of a file is decoded to decide whether it is text.
_TEXT_SNIFF_BYTES = 65536

_READ_ONLY_REASON = "Skills are read-only."


@dataclass(frozen=True)
class SkillResource:
    """A text file bundled with a skill.

    Attributes:
        name: The path relative to the skill directory, with forward slashes. The model reads the file by this name.
        path: Where the file is on disk.
    """

    name: str
    path: Path


@dataclass(frozen=True)
class SkillFiles:
    """One skill's bundled text files, keyed by name."""

    name: str
    resources: dict[str, SkillResource]


@dataclass(frozen=True)
class WorkspaceSkills:
    """The skills one run offers the model.

    Attributes:
        names: The skills in the catalog, sorted.
        capabilities: One deferred capability per skill, plus the resource tool when a skill ships files.
    """

    names: list[str] = field(default_factory=list)
    capabilities: list[AbstractCapability[Any]] = field(default_factory=list)


class ReadOnlyDirectoryBackend:
    """A pydantic-ai workspace backend that reads one host directory and runs nothing.

    The directory appears as the workspace root `/`, so its paths are POSIX paths on
    every platform. Writes raise `WorkspaceReadOnlyError`.
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
        return self.host_path(path).read_bytes()

    async def stat(self, path: str) -> FileEntry:
        target = self.host_path(path)
        info = target.stat()
        is_dir = target.is_dir()
        size = None
        if not is_dir:
            size = info.st_size
        return FileEntry(name=posixpath.basename(path), path=path, is_dir=is_dir, size=size)

    async def list_dir(self, path: str) -> Sequence[FileEntry]:
        entries: list[FileEntry] = []
        for child in sorted(self.host_path(path).iterdir()):
            child_path = posixpath.join(path, child.name)
            try:
                is_dir = child.is_dir()
            except OSError:
                # A symlink loop has no target; keep the entry rather than fail the listing.
                is_dir = False
            entries.append(FileEntry(name=child.name, path=child_path, is_dir=is_dir, size=None))
        return entries

    async def exists(self, path: str) -> bool:
        return self.host_path(path).exists()

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


class SkillResourceToolset(FunctionToolset[Any]):
    """Serves the `read_skill_resource` tool for the skills one run offers.

    A skill's files stay behind its instructions: the tool refuses a skill the model has
    not loaded with `load_capability`.
    """

    def __init__(self, skills: dict[str, SkillFiles]) -> None:
        super().__init__()
        self._skills = skills
        self.add_function(self._read_skill_resource_tool(), name=READ_SKILL_RESOURCE)

    def _read_skill_resource_tool(self) -> Any:
        async def read_skill_resource(  # noqa: D417
            ctx: RunContext[Any],
            skill_name: str,
            resource_name: str,
            args: Annotated[dict[str, Any] | None, BeforeValidator(_coerce_to_dict)] = None,  # noqa: ARG001
        ) -> str:
            """Read a supplementary file bundled with a skill.

            Skill packages ship reference documentation, templates, schemas and data files
            alongside their instructions. A skill's instructions name the ones it needs;
            this tool reads them.

            When to use this:
            - When a loaded skill's instructions reference a specific file
            - To access form templates, reference documentation, or data schemas
            - When you need supplementary information beyond the skill instructions

            Args:
                skill_name: Name of the skill containing the resource, exactly as it
                    appears in the capability catalog.
                resource_name: Path of the resource relative to the skill directory, as
                    listed under "Bundled files" in the loaded skill's instructions.
                    Examples: "FORMS.md", "references/REFERENCE.md", "get_schema"
                    A file name on its own works when only one resource has it.
                args: Arguments for callable resources (optional for static files).
                    Keys must match the parameter names in the resource's schema.

            Returns:
                The resource content as a string.

            Important:
            - Load the skill with `load_capability` first; its instructions list the files
            - Prefer the full path the instructions give over a bare file name
            - Static files don't need args; callable resources may require them
            """
            skill = self._skill_for(ctx, skill_name)
            resource = self._resource_for(skill, resource_name)
            return _read_resource(resource)

        return read_skill_resource

    def _skill_for(self, ctx: RunContext[Any], skill_name: str) -> SkillFiles:
        skill = self._skills.get(skill_name)
        if skill is None:
            available = ", ".join(sorted(self._skills)) or "none"
            msg = (
                f"Skill '{skill_name}' has no bundled files available. Skills with bundled files: {available}. "
                "Use the exact skill name."
            )
            raise ModelRetry(msg)
        # Every skill is deferred, so it is active only once the model has loaded it.
        if skill_name not in ctx.active_capability_ids:
            msg = (
                f"Skill '{skill_name}' is not loaded. Call load_capability with id='{skill_name}' "
                "first, then read its files."
            )
            raise ModelRetry(msg)
        return skill

    @staticmethod
    def _resource_for(skill: SkillFiles, requested: str) -> SkillResource:
        """The resource `requested` names exactly, or the one file it names by file name or stem."""
        resource = skill.resources.get(requested)
        if resource is not None:
            return resource

        matches = _shorthand_matches(requested, skill.resources)
        if len(matches) == 1:
            return skill.resources[matches[0]]
        if matches:
            msg = (
                f"Resource '{requested}' is ambiguous in skill '{skill.name}': {matches}. "
                "Use the full path relative to the skill directory."
            )
            raise ModelRetry(msg)
        msg = (
            f"Resource '{requested}' not found in skill '{skill.name}'. "
            f"Available: {sorted(skill.resources)}. Use the exact name from the skill instructions."
        )
        raise ModelRetry(msg)


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

    offered = sorted((skill for skill in catalog.skills if skill.model_invocable), key=lambda skill: skill.name)
    if not offered:
        return WorkspaceSkills()

    capabilities: list[AbstractCapability[Any]] = []
    files: dict[str, SkillFiles] = {}
    for skill in offered:
        directory = backend.host_path(skill.directory)
        resources = _discover_resources(directory)
        files[skill.name] = SkillFiles(name=skill.name, resources=resources)
        capabilities.append(_skill_capability(skill, directory.resolve(), sorted(resources)))

    if any(skill.resources for skill in files.values()):
        capabilities.append(Capability(toolsets=[SkillResourceToolset(files)]))

    return WorkspaceSkills(names=[skill.name for skill in offered], capabilities=capabilities)


def _skill_capability(skill: SkillDefinition, directory: Path, resource_names: list[str]) -> Capability[Any]:
    """The deferred capability the model loads to get `skill`'s instructions."""
    rendered = skill.render()
    for placeholder in _SKILL_DIR_PLACEHOLDERS:
        rendered = rendered.replace(placeholder, str(directory))
    instructions = [rendered]
    if resource_names:
        instructions.append(_bundled_files_section(resource_names))
    return Capability(id=skill.name, description=skill.description, instructions=instructions, defer_loading=True)


def _bundled_files_section(names: list[str]) -> str:
    """List a skill's files by the names `read_skill_resource` expects.

    A `SKILL.md` usually names its files in prose, so without this the model would guess a path.
    """
    listed = [f"- `{name}`" for name in names[:_MAX_LISTED_FILES]]
    remaining = len(names) - len(listed)
    if remaining > 0:
        listed.append(f"- ...and {remaining} more")
    return (
        "## Bundled files\n\n"
        "Read with `read_skill_resource`, using these exact `resource_name` values:\n\n" + "\n".join(listed)
    )


def _discover_resources(skill_dir: Path) -> dict[str, SkillResource]:
    """Index the text files under `skill_dir` other than `SKILL.md`, scripts, and build noise.

    Files whose real path is outside the skill directory are skipped, so a symlink cannot
    expose a file elsewhere on disk.
    """
    root = skill_dir.resolve()
    scripts = _script_paths(skill_dir, root)
    resources: dict[str, SkillResource] = {}
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file() or path.name.upper() == "SKILL.MD":
            continue
        relative = path.relative_to(skill_dir)
        if _is_excluded(relative):
            continue
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            logger.warning("Skipped skill file %s because it resolves outside the skill directory.", path)
            continue
        if resolved in scripts or not _is_text_file(path):
            continue
        name = relative.as_posix()
        resources[name] = SkillResource(name=name, path=resolved)
    return resources


def _script_paths(skill_dir: Path, root: Path) -> set[Path]:
    """The resolved paths of the files in `skill_dir` and its `scripts/` that look like scripts."""
    directories = [skill_dir]
    if (skill_dir / "scripts").is_dir():
        directories.append(skill_dir / "scripts")
    scripts: set[Path] = set()
    for directory in directories:
        for path in directory.iterdir():
            if not _is_script(path):
                continue
            resolved = path.resolve()
            if resolved.is_relative_to(root):
                scripts.add(resolved)
    return scripts


def _is_script(path: Path) -> bool:
    if path.name in _IGNORED_SCRIPT_NAMES or not path.is_file():
        return False
    suffix = path.suffix.lower()
    if suffix in _SCRIPT_EXTENSIONS:
        return True
    if os.name == "nt":
        return suffix in _WINDOWS_EXECUTABLE_EXTENSIONS
    try:
        return bool(path.stat().st_mode & 0o111)
    except OSError:
        return False


def _is_excluded(relative: Path) -> bool:
    posix = relative.as_posix()
    for pattern in _RESOURCE_EXCLUDES:
        if fnmatch(posix, pattern) or any(fnmatch(part, pattern) for part in relative.parts):
            return True
    return False


def _is_text_file(path: Path) -> bool:
    """Whether `path` decodes as UTF-8, judged from its first `_TEXT_SNIFF_BYTES` bytes."""
    try:
        with path.open("rb") as handle:
            prefix = handle.read(_TEXT_SNIFF_BYTES)
            at_eof = handle.read(1) == b""
        codecs.getincrementaldecoder("utf-8")().decode(prefix, final=at_eof)
    except (UnicodeDecodeError, OSError):
        return False
    return True


def _read_resource(resource: SkillResource) -> Any:
    """The file's text, parsed when it is valid JSON or YAML."""
    content = resource.path.read_text(encoding="utf-8")
    suffix = PurePosixPath(resource.name).suffix.lower()
    if suffix == ".json":
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return content
    if suffix in (".yaml", ".yml"):
        try:
            return yaml.safe_load(content)
        except yaml.YAMLError:
            return content
    return content


def _shorthand_matches(requested: str, names: dict[str, SkillResource]) -> list[str]:
    """The resource names whose file name, with or without its extension, is `requested`."""
    wanted = PurePosixPath(requested.strip()).name
    if not wanted:
        return []
    return sorted(name for name in names if PurePosixPath(name).name == wanted or PurePosixPath(name).stem == wanted)


def _coerce_to_dict(value: Any) -> Any:
    """Accept `args` sent as a JSON string."""
    if not isinstance(value, str):
        return value
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as e:
        msg = f"Invalid JSON string. Error: {e.msg} at line {e.lineno} col {e.colno}. Input snippet: {value[:100]}"
        raise ValueError(msg) from e
    if not isinstance(parsed, dict):
        msg = f"args must be a JSON object, got {type(parsed).__name__}"
        raise ValueError(msg)  # noqa: TRY004
    return parsed
