"""Find the agent's skills across its skills folders, and add or remove them.

A skills folder holds one subfolder per skill, each with a ``SKILL.md``. Folders are
read with the loader the runner uses, so a skill reported as usable here is one the
agent is offered.
"""

from __future__ import annotations

import logging
import posixpath
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from griptape_nodes.agents.pydantic_ai.skills import LoadedSkill, read_skill_library
from griptape_nodes.files.path_utils import (
    canonicalize_for_identity,
    canonicalize_for_identity_preserving_symlinks,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from pydantic_ai_harness.skills import SkippedSkill

logger = logging.getLogger("griptape_nodes")

DEFAULT_SKILLS_DIRECTORY = ".agents/skills"
SKILL_FILE_NAME = "SKILL.md"


class SkillWriteError(Exception):
    """A skill could not be added or removed. The message is meant for the user."""


@dataclass(frozen=True)
class DiscoveredSkill:
    """One skill found in a skills folder.

    Attributes:
        name: The skill's name, which is also its folder name.
        description: The ``description`` from its frontmatter, or empty when the skill cannot load.
        library: The skills folder it was found in.
        folder: The skill's own folder.
        problem: Why the agent cannot use it, or ``None`` when it can.
        model_invocable: False when its ``SKILL.md`` sets ``disable-model-invocation: true``.
        loaded: The skill as read, when it has no problem.
    """

    name: str
    description: str
    library: Path
    folder: Path
    problem: str | None = None
    model_invocable: bool = True
    loaded: LoadedSkill | None = None


@dataclass(frozen=True)
class SkillSelection:
    """The skills one agent run offers the model."""

    skills: list[LoadedSkill]

    @property
    def names(self) -> list[str]:
        return [skill.definition.name for skill in self.skills]


def resolve_skills_directories(configured: Sequence[str], workspace: Path) -> list[Path]:
    """Turn the ``agent.skills_directories`` setting into absolute folders.

    Relative entries resolve against the workspace. Blank entries and repeats are
    dropped, keeping the first, so the first folder stays where new skills go.
    Entries reaching one folder through a link are repeats too.
    """
    directories: list[Path] = []
    seen: set[Path] = set()
    for raw in configured:
        if not raw.strip():
            continue
        identity = canonicalize_for_identity(raw, base=workspace)
        if identity in seen:
            continue
        seen.add(identity)
        directories.append(canonicalize_for_identity_preserving_symlinks(raw, base=workspace))
    return directories


async def discover_skills(libraries: Sequence[Path]) -> list[DiscoveredSkill]:
    """List every skill in ``libraries``, in folder order then by name.

    When two folders hold a skill with one name, the earlier folder's is used, so a
    user's own folder can override a shared one.
    """
    claimed: dict[str, Path] = {}
    discovered: list[DiscoveredSkill] = []
    for library in libraries:
        contents = await read_skill_library(library)
        found: list[DiscoveredSkill] = []
        for loaded in contents.skills:
            definition = loaded.definition
            winner = claimed.get(definition.name)
            if winner is not None:
                problem = f"The skill named '{definition.name}' in {winner} is used instead."
                found.append(_discovered(library, loaded.directory, definition.description, problem=problem))
                continue
            claimed[definition.name] = library
            found.append(
                DiscoveredSkill(
                    name=definition.name,
                    description=definition.description,
                    library=library,
                    folder=loaded.directory,
                    model_invocable=definition.model_invocable,
                    loaded=loaded,
                )
            )
        found.extend(_unloadable(library, skipped) for skipped in contents.skipped if skipped.reason == "invalid")
        discovered.extend(sorted(found, key=lambda skill: skill.name))
    return discovered


def select_skills(skills: Sequence[DiscoveredSkill], disabled: Collection[str]) -> SkillSelection:
    """Pick the skills a run offers: every one that loads, the model may use, and the user has on."""
    return SkillSelection(
        skills=[
            skill.loaded
            for skill in skills
            if skill.loaded is not None and skill.model_invocable and skill.name not in disabled
        ]
    )


def compose_skill_file(name: str, description: str, instructions: str) -> str:
    """Build the text of a ``SKILL.md`` from its parts."""
    frontmatter = yaml.safe_dump(
        {"name": name.strip(), "description": description.strip()},
        sort_keys=False,
        allow_unicode=True,
        width=1_000_000,
    )
    return f"---\n{frontmatter}---\n\n{instructions.strip()}\n"


async def add_skill(libraries: Sequence[Path], content: str) -> DiscoveredSkill:
    """Write ``content`` as a new skill in the first of ``libraries``, named by its frontmatter.

    The skill is checked before anything is written, so a skill that would not load
    never lands in the folder. A name used in any of ``libraries`` is refused, so the
    new skill is never hidden behind another.

    Raises:
        SkillWriteError: The content is not a valid skill, a skill of that name
            already exists, or the folder cannot be written.
    """
    if not libraries:
        msg = "No skills folder is set. Add one to the agent.skills_directories setting."
        raise SkillWriteError(msg)
    name = _declared_name(content)
    for existing in libraries:
        try:
            taken = (existing / name).exists()
        except OSError as e:
            msg = f"Could not check {existing} for a skill named '{name}': {e.strerror or e}."
            raise SkillWriteError(msg) from e
        if taken:
            msg = f"A skill named '{name}' already exists in {existing}. Pick another name or remove that one first."
            raise SkillWriteError(msg)

    with tempfile.TemporaryDirectory() as staging:
        staged = Path(staging) / name
        try:
            staged.mkdir()
            (staged / SKILL_FILE_NAME).write_text(content, encoding="utf-8")
        except OSError as e:
            msg = f"'{name}' cannot be used as a folder name: {e.strerror or e}."
            raise SkillWriteError(msg) from e
        (checked,) = await discover_skills([Path(staging)])
    if checked.problem is not None:
        raise SkillWriteError(checked.problem)

    library = libraries[0]
    target = library / name
    try:
        target.mkdir(parents=True)
        (target / SKILL_FILE_NAME).write_text(content, encoding="utf-8")
    except OSError as e:
        msg = f"Could not write the skill into {library}: {e.strerror or e}."
        raise SkillWriteError(msg) from e
    # By folder, not name: the loader normalizes a name, so the two can differ in Unicode form.
    (written,) = [skill for skill in await discover_skills([library]) if skill.folder == target]
    return written


def remove_skill(folder: Path) -> None:
    """Delete the skill in ``folder`` and everything it bundles.

    Raises:
        SkillWriteError: ``folder`` is not a skill, or cannot be deleted.
    """
    try:
        is_skill = (folder / SKILL_FILE_NAME).is_file()
    except OSError:
        is_skill = False
    if not is_skill:
        msg = f"{folder} is not a skill folder, so it was left alone."
        raise SkillWriteError(msg)
    try:
        shutil.rmtree(folder)
    except OSError as e:
        msg = f"Could not delete {folder}: {e.strerror or e}."
        raise SkillWriteError(msg) from e


def _discovered(library: Path, folder: Path, description: str, *, problem: str) -> DiscoveredSkill:
    return DiscoveredSkill(name=folder.name, description=description, library=library, folder=folder, problem=problem)


def _unloadable(library: Path, skipped: SkippedSkill) -> DiscoveredSkill:
    """The skill a ``SKILL.md`` the loader rejected belongs to, with why in plain words where possible."""
    # The loader names a rejected file by its path in the folder, such as `/my-skill/SKILL.md`.
    folder = library / posixpath.basename(posixpath.dirname(skipped.path))
    problem = _frontmatter_problem(folder / SKILL_FILE_NAME)
    if problem is None:
        problem = skipped.message.removeprefix(f"Skipping {skipped.path}: ").replace(skipped.path, SKILL_FILE_NAME)
    return _discovered(library, folder, "", problem=problem)


def _read_frontmatter(content: str) -> dict[str, Any] | None:
    """The frontmatter mapping, or ``None`` when there is no readable one."""
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    closing = next((index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
    if closing is None:
        return None
    try:
        # Text only, as the loader reads it: a value shaped like an impossible date would make safe_load raise.
        parsed = yaml.load("\n".join(lines[1:closing]), Loader=yaml.BaseLoader)  # noqa: S506
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _frontmatter_problem(skill_file: Path) -> str | None:
    """Explain the common mistakes in plain words, where the loader would report them its way."""
    try:
        content = skill_file.read_text(encoding="utf-8")
    except OSError as e:
        return f"{SKILL_FILE_NAME} could not be read: {e.strerror or e}."
    except UnicodeDecodeError:
        return f"{SKILL_FILE_NAME} is not saved as UTF-8 text. Save it as UTF-8 and try again."
    frontmatter = _read_frontmatter(content)
    if frontmatter is None:
        return f"{SKILL_FILE_NAME} must start with a block of settings between two '---' lines."
    if not str(frontmatter.get("description") or "").strip():
        return f"{SKILL_FILE_NAME} has no description. Add a 'description:' line saying when to use the skill."
    return None


def _declared_name(content: str) -> str:
    frontmatter = _read_frontmatter(content)
    if frontmatter is None:
        msg = (
            "The file is not a skill: it must start with a block of settings between two '---' lines, "
            f"as {SKILL_FILE_NAME} does."
        )
        raise SkillWriteError(msg)
    name = str(frontmatter.get("name") or "").strip()
    if not name:
        msg = "The skill has no name. Add a 'name:' line to the settings at the top of the file."
        raise SkillWriteError(msg)
    if "/" in name or "\\" in name or name in (".", ".."):
        msg = f"'{name}' is not a valid skill name. Use lowercase letters, numbers, and single hyphens."
        raise SkillWriteError(msg)
    return name
