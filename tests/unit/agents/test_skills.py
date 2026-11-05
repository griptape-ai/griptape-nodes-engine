"""Tests for finding, choosing, adding, and removing the agent's skills."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.agents.skills import (
    SkillWriteError,
    add_skill,
    compose_skill_file,
    discover_skills,
    remove_skill,
    resolve_skills_directories,
    select_skills,
)

if TYPE_CHECKING:
    from pathlib import Path


def _write_skill(library: Path, name: str, text: str | None = None) -> Path:
    folder = library / name
    folder.mkdir(parents=True)
    content = text if text is not None else f"---\nname: {name}\ndescription: Use for {name}.\n---\n\nGuidance."
    (folder / "SKILL.md").write_text(content, encoding="utf-8")
    return folder


class TestResolveSkillsDirectories:
    def test_relative_entries_resolve_against_the_workspace(self, tmp_path: Path) -> None:
        shared = tmp_path / "shared"

        directories = resolve_skills_directories([".agents/skills", str(shared)], tmp_path / "ws")

        assert directories == [tmp_path / "ws" / ".agents/skills", shared]

    def test_blank_and_repeated_entries_are_dropped(self, tmp_path: Path) -> None:
        directories = resolve_skills_directories(["a", " ", "a", "b"], tmp_path)

        assert directories == [tmp_path / "a", tmp_path / "b"]

    def test_a_link_to_a_listed_folder_is_a_repeat(self, tmp_path: Path) -> None:
        shared = tmp_path / "shared"
        shared.mkdir()
        (tmp_path / "link").symlink_to(shared)

        directories = resolve_skills_directories(["link", str(shared)], tmp_path)

        assert directories == [tmp_path / "link"]


@pytest.mark.asyncio
class TestDiscoverSkills:
    async def test_lists_skills_from_every_folder_in_order(self, tmp_path: Path) -> None:
        own = tmp_path / "own"
        shared = tmp_path / "shared"
        _write_skill(own, "zeta")
        _write_skill(own, "alpha")
        _write_skill(shared, "house-style")

        skills = await discover_skills([own, shared])

        assert [(skill.name, skill.library) for skill in skills] == [
            ("alpha", own),
            ("zeta", own),
            ("house-style", shared),
        ]
        assert skills[0].description == "Use for alpha."
        assert skills[0].folder == own / "alpha"
        assert all(skill.problem is None for skill in skills)

    async def test_missing_folders_and_stray_files_are_ignored(self, tmp_path: Path) -> None:
        library = tmp_path / "skills"
        library.mkdir()
        (library / "README.md").write_text("notes")
        (library / "not-a-skill").mkdir()

        assert await discover_skills([tmp_path / "missing", library]) == []

    async def test_the_earlier_folder_wins_a_name(self, tmp_path: Path) -> None:
        own = tmp_path / "own"
        shared = tmp_path / "shared"
        _write_skill(own, "house-style", "---\nname: house-style\ndescription: Ours.\n---\n")
        _write_skill(shared, "house-style", "---\nname: house-style\ndescription: Theirs.\n---\n")

        winner, loser = await discover_skills([own, shared])

        assert (winner.library, winner.description, winner.problem) == (own, "Ours.", None)
        assert loser.library == shared
        assert str(own) in (loser.problem or "")

    async def test_a_broken_skill_does_not_hide_a_later_one_with_its_name(self, tmp_path: Path) -> None:
        own = tmp_path / "own"
        shared = tmp_path / "shared"
        _write_skill(own, "house-style", "no frontmatter")
        _write_skill(shared, "house-style")

        broken, used = await discover_skills([own, shared])

        assert broken.problem is not None
        assert used.problem is None

    async def test_missing_frontmatter_is_explained(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "broken", "Just some text.")

        (skill,) = await discover_skills([tmp_path])

        assert skill.name == "broken"
        assert "'---'" in (skill.problem or "")

    async def test_missing_description_is_explained(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "vague", "---\nname: vague\n---\n\nGuidance.")

        (skill,) = await discover_skills([tmp_path])

        assert "no description" in (skill.problem or "")

    async def test_a_file_that_is_not_utf8_is_a_problem_on_that_skill(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "kept")
        folder = tmp_path / "latin"
        folder.mkdir()
        (folder / "SKILL.md").write_bytes("---\nname: latin\ndescription: \u00e9t\u00e9\n---\n".encode("utf-16"))

        skills = await discover_skills([tmp_path])

        assert [(skill.name, skill.problem is None) for skill in skills] == [("kept", True), ("latin", False)]
        assert "UTF-8" in (skills[1].problem or "")

    async def test_a_date_shaped_description_is_read_as_text(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "dated", "---\nname: dated\ndescription: 2024-13-45\n---\n\nGuidance.")

        (skill,) = await discover_skills([tmp_path])

        assert skill.problem is None
        assert skill.description == "2024-13-45"

    async def test_loader_rejection_is_reported_without_the_path(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "folder-name", "---\nname: other-name\ndescription: Mismatch.\n---\n")

        (skill,) = await discover_skills([tmp_path])

        assert "must match its parent directory" in (skill.problem or "")
        assert "/folder-name/SKILL.md" not in (skill.problem or "")

    async def test_a_skill_kept_from_the_model_by_its_file_is_flagged(self, tmp_path: Path) -> None:
        _write_skill(
            tmp_path, "manual", "---\nname: manual\ndescription: Run by hand.\ndisable-model-invocation: true\n---\n"
        )

        (skill,) = await discover_skills([tmp_path])

        assert skill.problem is None
        assert skill.model_invocable is False

    async def test_a_folder_that_is_itself_a_skill_is_ignored(self, tmp_path: Path) -> None:
        skill = _write_skill(tmp_path, "house-style")

        assert await discover_skills([skill]) == []

    @pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs POSIX permissions that bind this user")
    async def test_an_unreadable_folder_costs_only_its_skills(self, tmp_path: Path) -> None:
        own = tmp_path / "own"
        _write_skill(own, "kept")
        locked = tmp_path / "locked"
        _write_skill(locked / "skills", "hidden")
        locked.chmod(0)
        try:
            skills = await discover_skills([own, locked / "skills"])
        finally:
            locked.chmod(0o755)

        assert [skill.name for skill in skills] == ["kept"]


@pytest.mark.asyncio
class TestSelectSkills:
    async def test_selects_usable_skills_the_model_may_use_and_the_user_has_on(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "kept")
        _write_skill(tmp_path, "turned-off")
        _write_skill(tmp_path, "broken", "no frontmatter")
        _write_skill(
            tmp_path, "manual", "---\nname: manual\ndescription: By hand.\ndisable-model-invocation: true\n---\n"
        )

        selection = select_skills(await discover_skills([tmp_path]), {"turned-off"})

        assert [skill.folder for skill in selection] == [tmp_path / "kept"]


@pytest.mark.asyncio
class TestAddSkill:
    async def test_writes_a_composed_skill_to_the_first_folder(self, tmp_path: Path) -> None:
        own = tmp_path / "own"
        content = compose_skill_file("house-style", "Apply our house style: always.", "Use sentence case.")

        skill = await add_skill([own, tmp_path / "shared"], content)

        assert skill.folder == own / "house-style"
        assert skill.problem is None
        assert skill.description == "Apply our house style: always."
        assert "Use sentence case." in (skill.folder / "SKILL.md").read_text(encoding="utf-8")

    async def test_a_name_in_another_unicode_form_is_found_after_writing(self, tmp_path: Path) -> None:
        name = "x-e\u0301"  # a decomposed accent, which the loader normalizes to "x-\u00e9"

        skill = await add_skill([tmp_path], compose_skill_file(name, "Coffee.", "Body."))

        assert skill.folder == tmp_path / name
        assert skill.problem is None

    async def test_an_invalid_skill_is_never_written(self, tmp_path: Path) -> None:
        with pytest.raises(SkillWriteError, match="no description"):
            await add_skill([tmp_path], compose_skill_file("vague", "", "Body."))

        assert not (tmp_path / "vague").exists()

    async def test_an_invalid_name_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(SkillWriteError, match="Invalid skill name"):
            await add_skill([tmp_path], compose_skill_file("Bad Name", "Something.", "Body."))

    async def test_content_without_a_name_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(SkillWriteError, match="no name"):
            await add_skill([tmp_path], "---\ndescription: Something.\n---\n")

    async def test_a_name_taken_in_any_folder_is_refused(self, tmp_path: Path) -> None:
        own = tmp_path / "own"
        shared = tmp_path / "shared"
        _write_skill(shared, "house-style")

        with pytest.raises(SkillWriteError, match="already exists"):
            await add_skill([own, shared], compose_skill_file("house-style", "Something.", "Body."))

    async def test_a_name_taken_in_another_unicode_form_is_refused(self, tmp_path: Path) -> None:
        own = tmp_path / "own"
        shared = tmp_path / "shared"
        _write_skill(shared, "house-\ufb01le")  # the "fi" ligature, which the loader normalizes to "fi"

        with pytest.raises(SkillWriteError, match="already exists"):
            await add_skill([own, shared], compose_skill_file("house-file", "Something.", "Body."))

        assert not (own / "house-file").exists()

    async def test_no_folder_is_refused(self) -> None:
        with pytest.raises(SkillWriteError, match="No skills folder"):
            await add_skill([], compose_skill_file("house-style", "Something.", "Body."))


class TestRemoveSkill:
    def test_removes_the_skill_and_its_files(self, tmp_path: Path) -> None:
        folder = _write_skill(tmp_path, "doomed")
        (folder / "notes.md").write_text("extra")

        remove_skill(folder)

        assert not folder.exists()

    def test_refuses_a_folder_that_is_not_a_skill(self, tmp_path: Path) -> None:
        (tmp_path / "other").mkdir()

        with pytest.raises(SkillWriteError):
            remove_skill(tmp_path / "other")

        assert (tmp_path / "other").exists()
