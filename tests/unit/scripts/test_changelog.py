from pathlib import Path

import pytest

from scripts.changelog import (
    UNRELEASED_COMMENT,
    ChangelogError,
    Fragment,
    check,
    check_fragments,
    check_unreleased_is_empty,
    extract,
    fold,
    load_fragments,
    main,
    render,
    roll,
)

REPO = "https://github.com/griptape-ai/griptape-nodes-engine"

PREAMBLE = """# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/2.0.0/).
"""

CHANGELOG = f"""{PREAMBLE}
## [Unreleased]

### Fixed

- Unreleased fix.

## [0.101.0] - 2026-09-15

### Added

- Released feature.

[Unreleased]: {REPO}/compare/v0.101.0...HEAD
[0.101.0]: {REPO}/compare/v0.100.0...v0.101.0
"""

# What the file on disk looks like: entries live in changelog.d, so [Unreleased] carries none of its
# own. CHANGELOG keeps one, since fold has to merge entry files into a section that already has them.
EMPTY_UNRELEASED = CHANGELOG.replace("### Fixed\n\n- Unreleased fix.\n\n", "")


def _messages(source: str) -> list[str]:
    return [problem.message for problem in check(source)]


def _entries(tmp_path: Path, files: dict[str, str]) -> Path:
    """Write entry files into a `changelog.d` under `tmp_path` and return the directory."""
    directory = tmp_path / "changelog.d"
    directory.mkdir(exist_ok=True)
    for name, text in files.items():
        (directory / name).write_text(text, encoding="utf-8")
    return directory


def _fragment_messages(directory: Path) -> list[str]:
    return [problem.message for problem in check_fragments(directory)]


class TestCheck:
    def test_accepts_a_well_formed_changelog(self) -> None:
        assert check(CHANGELOG) == []

    def test_accepts_an_empty_unreleased_section(self) -> None:
        source = f"{PREAMBLE}\n## [Unreleased]\n\n[Unreleased]: {REPO}/compare/v0.101.0...HEAD\n"

        assert check(source) == []

    def test_requires_the_title_and_spec_link(self) -> None:
        source = CHANGELOG.replace("# Changelog", "# History").replace("keepachangelog.com", "example.com")

        assert _messages(source) == [
            'file must open with "# Changelog"',
            "preamble must link the Keep a Changelog version this file follows",
        ]

    def test_rejects_unknown_change_types(self) -> None:
        source = CHANGELOG.replace("### Fixed", "### Improved")

        assert _messages(source) == [
            'unknown change type "Improved"; use one of Added, Changed, Deprecated, Removed, Fixed, Security'
        ]

    def test_rejects_entries_outside_a_change_type(self) -> None:
        source = CHANGELOG.replace("### Fixed\n\n", "")

        assert _messages(source) == ["entry must be grouped under a change type heading"]

    def test_rejects_empty_and_duplicate_change_types(self) -> None:
        source = CHANGELOG.replace("### Fixed\n", "### Fixed\n\n### Fixed\n")

        assert _messages(source) == ['"### Fixed" has no entries', 'duplicate "### Fixed" in the same section']

    def test_requires_dated_versions_latest_first(self) -> None:
        source = CHANGELOG.replace(
            "## [0.101.0] - 2026-09-15", "## [0.99.0]\n\n### Fixed\n\n- Old.\n\n## [0.101.0] - 2026-09-15"
        )
        source = source.replace("[0.101.0]: ", f"[0.99.0]: {REPO}/compare/v0.98.0...v0.99.0\n[0.101.0]: ")

        assert _messages(source) == [
            "version 0.99.0 must show its release date as YYYY-MM-DD",
            "version 0.101.0 must come after 0.99.0; list the latest first",
        ]

    def test_requires_unreleased_first(self) -> None:
        source = CHANGELOG.replace("## [Unreleased]\n\n### Fixed\n\n- Unreleased fix.\n\n", "")
        source = source.replace("\n[Unreleased]", "\n## [Unreleased]\n\n[Unreleased]")

        assert _messages(source) == ["## [Unreleased] must be the first section"]

    def test_requires_a_link_for_every_section(self) -> None:
        source = CHANGELOG.replace(f"[0.101.0]: {REPO}/compare/v0.100.0...v0.101.0\n", "")

        assert _messages(source) == ["version 0.101.0 has no link definition"]

    def test_requires_the_unreleased_link_to_compare_against_head(self) -> None:
        source = CHANGELOG.replace("v0.101.0...HEAD", "v0.101.0...main")

        assert _messages(source) == ["the Unreleased link must compare the latest release tag to HEAD"]

    def test_ignores_structure_inside_code_fences(self) -> None:
        source = CHANGELOG.replace("- Unreleased fix.\n", "- Unreleased fix:\n\n  ```md\n  ### Improved\n  ```\n")

        assert check(source) == []

    def test_reports_one_based_line_numbers(self) -> None:
        source = CHANGELOG.replace("### Fixed", "### Improved")

        assert [problem.line for problem in check(source)] == [7]


class TestCheckUnreleasedIsEmpty:
    def test_accepts_an_empty_section(self) -> None:
        assert check_unreleased_is_empty(EMPTY_UNRELEASED) == []

    def test_accepts_the_signpost_comment(self) -> None:
        source = EMPTY_UNRELEASED.replace("## [Unreleased]\n", f"## [Unreleased]\n\n{UNRELEASED_COMMENT}\n")

        assert check_unreleased_is_empty(source) == []

    def test_rejects_a_type_heading_and_an_entry(self) -> None:
        message = "unreleased entries live in changelog.d/, one file each"

        assert [(problem.line, problem.message) for problem in check_unreleased_is_empty(CHANGELOG)] == [
            (7, message),
            (9, message),
        ]

    def test_ignores_entries_inside_a_code_fence(self) -> None:
        source = EMPTY_UNRELEASED.replace("## [Unreleased]\n", "## [Unreleased]\n\n```md\n- Not an entry.\n```\n")

        assert check_unreleased_is_empty(source) == []

    def test_leaves_a_missing_section_to_check(self) -> None:
        source = EMPTY_UNRELEASED.replace("## [Unreleased]\n\n", "")

        assert check_unreleased_is_empty(source) == []


class TestCheckFragments:
    def test_accepts_a_well_formed_entry(self, tmp_path: Path) -> None:
        directory = _entries(
            tmp_path,
            {"fixed-group-node-ports.md": f"- Group nodes show their ports again.\n  [#5563]({REPO}/issues/5563)\n"},
        )

        assert check_fragments(directory) == []

    def test_ignores_the_readme(self, tmp_path: Path) -> None:
        directory = _entries(tmp_path, {"README.md": "# changelog.d\n\nHow to add an entry.\n"})

        assert check_fragments(directory) == []

    def test_accepts_a_missing_directory(self, tmp_path: Path) -> None:
        assert check_fragments(tmp_path / "changelog.d") == []

    def test_rejects_an_unknown_type(self, tmp_path: Path) -> None:
        directory = _entries(tmp_path, {"improved-group-node-ports.md": "- Entry.\n"})

        assert _fragment_messages(directory) == [
            '"improved" is not a change type; name it one of added, changed, deprecated, removed, fixed, security'
        ]

    def test_rejects_a_name_that_is_not_type_and_slug(self, tmp_path: Path) -> None:
        directory = _entries(tmp_path, {"fixed.md": "- Entry.\n", "fixed_group_ports.md": "- Entry.\n"})

        assert _fragment_messages(directory) == ['name must look like "fixed-short-slug.md"'] * 2

    def test_rejects_a_file_the_roll_would_never_fold(self, tmp_path: Path) -> None:
        directory = _entries(tmp_path, {"fixed-stray.txt": "- Entry.\n", "fixed-no-extension": "- Entry.\n"})

        assert _fragment_messages(directory) == ['name must look like "fixed-short-slug.md"'] * 2

    def test_accepts_an_entry_with_a_byte_order_mark(self, tmp_path: Path) -> None:
        directory = _entries(tmp_path, {"fixed-bom.md": "\ufeff- A fix.\n"})

        assert check_fragments(directory) == []
        assert load_fragments(directory) == [
            Fragment(path=directory / "fixed-bom.md", change_type="Fixed", body="- A fix.")
        ]

    def test_rejects_an_entry_that_is_not_utf8(self, tmp_path: Path) -> None:
        directory = _entries(tmp_path, {})
        (directory / "fixed-utf16.md").write_bytes("- A fix.\n".encode("utf-16"))

        assert _fragment_messages(directory) == ["file must be UTF-8 text"]

    def test_ignores_a_dotfile(self, tmp_path: Path) -> None:
        # .DS_Store is gitignored, so reporting it would stop a release on a file nobody can see.
        directory = _entries(tmp_path, {".DS_Store": "\x00binary\n"})

        assert check_fragments(directory) == []
        assert load_fragments(directory) == []

    def test_rejects_a_subdirectory(self, tmp_path: Path) -> None:
        directory = _entries(tmp_path, {})
        (directory / "nested").mkdir()

        assert _fragment_messages(directory) == ["only entry files belong here, named like fixed-short-slug.md"]

    def test_rejects_an_empty_file(self, tmp_path: Path) -> None:
        directory = _entries(tmp_path, {"fixed-nothing.md": "\n\n"})

        assert _fragment_messages(directory) == ["file is empty; it holds one changelog entry"]

    def test_requires_a_bullet(self, tmp_path: Path) -> None:
        directory = _entries(tmp_path, {"fixed-no-bullet.md": "Group nodes show their ports again.\n"})

        assert _fragment_messages(directory) == ['entry must start with "- "']

    def test_rejects_a_bullet_with_no_text(self, tmp_path: Path) -> None:
        # Well formed everywhere else, and the release notes would carry an empty bullet.
        directory = _entries(
            tmp_path, {"fixed-placeholder.md": "- \n", "fixed-wrapped-placeholder.md": "- \n  Text.\n"}
        )

        assert _fragment_messages(directory) == ['entry has no text after "- "'] * 2

    def test_rejects_a_second_entry(self, tmp_path: Path) -> None:
        directory = _entries(tmp_path, {"fixed-two.md": "- One fix.\n- Another fix.\n"})

        assert _fragment_messages(directory) == ["one entry per file; a second entry needs its own file"]

    def test_rejects_a_heading(self, tmp_path: Path) -> None:
        directory = _entries(tmp_path, {"fixed-heading.md": "- A fix.\n### Fixed\n"})

        assert _fragment_messages(directory) == ["entry holds no headings; its type is in the filename"]

    def test_rejects_an_unindented_continuation(self, tmp_path: Path) -> None:
        directory = _entries(tmp_path, {"fixed-wrapped.md": "- A fix that wraps\nonto the next line.\n"})

        assert _fragment_messages(directory) == ["continuation lines are indented two spaces"]

    def test_points_problems_at_the_file(self, tmp_path: Path) -> None:
        directory = _entries(tmp_path, {"fixed-two.md": "- One fix.\n- Another fix.\n"})

        assert [(problem.path, problem.line) for problem in check_fragments(directory)] == [
            (directory / "fixed-two.md", 2)
        ]


class TestLoadFragments:
    def test_reads_entries_in_filename_order(self, tmp_path: Path) -> None:
        directory = _entries(tmp_path, {"fixed-b.md": "- Second fix.\n", "added-a.md": "- A feature.\n"})

        assert load_fragments(directory) == [
            Fragment(path=directory / "added-a.md", change_type="Added", body="- A feature."),
            Fragment(path=directory / "fixed-b.md", change_type="Fixed", body="- Second fix."),
        ]

    def test_accepts_a_missing_directory(self, tmp_path: Path) -> None:
        assert load_fragments(tmp_path / "changelog.d") == []

    def test_refuses_an_entry_it_cannot_read(self, tmp_path: Path) -> None:
        directory = _entries(tmp_path, {"improved-a.md": "- A feature.\n"})

        with pytest.raises(ChangelogError, match="is not a change type"):
            load_fragments(directory)


class TestFold:
    def test_leaves_the_section_alone_with_no_entries(self) -> None:
        assert fold(CHANGELOG, []) == CHANGELOG

    def test_adds_an_entry_under_a_new_type_heading(self) -> None:
        fragment = Fragment(path=Path("added-feature.md"), change_type="Added", body="- A feature.")

        folded = fold(CHANGELOG, [fragment])

        assert "## [Unreleased]\n\n### Added\n\n- A feature.\n\n### Fixed\n\n- Unreleased fix.\n" in folded
        assert check(folded) == []

    def test_merges_with_an_entry_written_into_the_section(self) -> None:
        fragment = Fragment(path=Path("fixed-another.md"), change_type="Fixed", body="- Another fix.")

        folded = fold(CHANGELOG, [fragment])

        assert "### Fixed\n\n- Unreleased fix.\n- Another fix.\n" in folded

    def test_keeps_an_entry_that_wraps(self) -> None:
        fragment = Fragment(
            path=Path("added-feature.md"), change_type="Added", body="- A feature that wraps\n  onto two lines."
        )

        assert "### Added\n\n- A feature that wraps\n  onto two lines.\n" in fold(CHANGELOG, [fragment])

    def test_puts_breaking_changes_first_within_a_type(self) -> None:
        fragments = [
            Fragment(path=Path("changed-plain.md"), change_type="Changed", body="- A plain change."),
            Fragment(
                path=Path("changed-breaking.md"), change_type="Changed", body="- **Breaking:** A breaking change."
            ),
        ]

        folded = fold(CHANGELOG, fragments)

        assert "### Changed\n\n- **Breaking:** A breaking change.\n- A plain change.\n" in folded

    def test_drops_the_comment_pointing_at_the_entry_directory(self) -> None:
        source = CHANGELOG.replace("## [Unreleased]\n", "## [Unreleased]\n\n<!-- Entries go in changelog.d/. -->\n")

        assert "<!--" not in fold(source, [])

    def test_keeps_an_entry_that_holds_a_code_fence(self) -> None:
        entry = "- Unreleased fix:\n\n  ```md\n  ### Improved\n  ```\n"
        source = CHANGELOG.replace("- Unreleased fix.\n", entry)

        folded = fold(source, [])

        assert "### Fixed\n\n" + entry in folded
        assert check(folded) == []

    def test_keeps_an_entry_that_holds_a_second_paragraph(self) -> None:
        entry = "- Unreleased fix.\n\n  A second paragraph the release notes need.\n"
        source = CHANGELOG.replace("- Unreleased fix.\n", entry)

        assert "### Fixed\n\n" + entry in fold(source, [])

    def test_refuses_a_line_it_cannot_attach_to_an_entry(self) -> None:
        source = CHANGELOG.replace("### Fixed\n", "### Fixed\n\nLoose prose nobody can place.\n")

        with pytest.raises(ChangelogError, match="belongs to no entry"):
            fold(source, [])

    def test_refuses_an_entry_with_no_change_type_above_it(self) -> None:
        source = CHANGELOG.replace("### Fixed\n\n", "")

        with pytest.raises(ChangelogError, match="no change type heading"):
            fold(source, [])

    def test_refuses_a_file_with_no_unreleased_heading(self) -> None:
        source = CHANGELOG.replace("## [Unreleased]\n", "")

        with pytest.raises(ChangelogError, match="there is no"):
            fold(source, [])

    def test_refuses_an_unknown_change_type_in_the_section(self) -> None:
        source = CHANGELOG.replace("### Fixed", "### Improved")

        with pytest.raises(ChangelogError, match="unknown change types: Improved"):
            fold(source, [])


class TestRender:
    def test_shows_the_section_as_the_release_will(self) -> None:
        fragment = Fragment(path=Path("added-feature.md"), change_type="Added", body="- A feature.")

        assert render(CHANGELOG, [fragment]) == "### Added\n\n- A feature.\n\n### Fixed\n\n- Unreleased fix.\n"

    def test_shows_nothing_when_there_is_nothing_to_release(self) -> None:
        empty = CHANGELOG.replace("### Fixed\n\n- Unreleased fix.\n\n", "")

        assert render(empty, []) == ""


class TestRoll:
    def test_renames_unreleased_and_its_link(self) -> None:
        rolled = roll(CHANGELOG, "0.102.0", "2026-09-22")

        assert "## [0.102.0] - 2026-09-22\n\n### Fixed\n\n- Unreleased fix." in rolled
        assert f"[Unreleased]: {REPO}/compare/v0.102.0...HEAD\n" in rolled
        assert f"[0.102.0]: {REPO}/compare/v0.101.0...v0.102.0\n" in rolled
        assert check(rolled) == []

    def test_refuses_an_empty_release_unless_allowed(self) -> None:
        empty = CHANGELOG.replace("### Fixed\n\n- Unreleased fix.\n\n", "")

        with pytest.raises(ChangelogError, match="nothing to release"):
            roll(empty, "0.102.0", "2026-09-22")
        assert "## [0.102.0] - 2026-09-22" in roll(empty, "0.102.0", "2026-09-22", allow_empty=True)

    def test_refuses_a_version_that_already_has_a_section(self) -> None:
        with pytest.raises(ChangelogError, match="already has a section"):
            roll(CHANGELOG, "0.101.0", "2026-09-22")

    def test_refuses_an_unreleased_link_that_does_not_compare_to_head(self) -> None:
        source = CHANGELOG.replace("v0.101.0...HEAD", "v0.101.0...main")

        with pytest.raises(ChangelogError, match="must compare a tag to HEAD"):
            roll(source, "0.102.0", "2026-09-22")

    def test_points_the_fresh_unreleased_section_at_the_entry_directory(self) -> None:
        rolled = roll(CHANGELOG, "0.102.0", "2026-09-22")

        assert f"## [Unreleased]\n\n{UNRELEASED_COMMENT}\n\n## [0.102.0] - 2026-09-22\n" in rolled
        assert check(rolled) == []
        # The comment is a signpost for the next contributor, not something a release publishes.
        assert UNRELEASED_COMMENT not in extract(rolled, "0.102.0")
        assert render(rolled, []) == ""

    def test_folds_entry_files_into_the_released_section(self) -> None:
        fragments = [
            Fragment(path=Path("added-feature.md"), change_type="Added", body="- A feature."),
            Fragment(path=Path("changed-breaking.md"), change_type="Changed", body="- **Breaking:** A break."),
        ]

        rolled = roll(CHANGELOG, "0.102.0", "2026-09-22", fragments=fragments)

        assert extract(rolled, "0.102.0") == (
            "### Added\n\n- A feature.\n\n### Changed\n\n- **Breaking:** A break.\n\n### Fixed\n\n- Unreleased fix.\n"
        )
        assert extract(rolled, "0.101.0") == "### Added\n\n- Released feature.\n"
        assert check(rolled) == []

    def test_counts_entry_files_as_something_to_release(self) -> None:
        empty = CHANGELOG.replace("### Fixed\n\n- Unreleased fix.\n\n", "")
        fragments = [Fragment(path=Path("added-feature.md"), change_type="Added", body="- A feature.")]

        assert "### Added\n\n- A feature." in roll(empty, "0.102.0", "2026-09-22", fragments=fragments)


class TestExtract:
    def test_returns_the_section_body(self) -> None:
        assert extract(CHANGELOG, "0.101.0") == "### Added\n\n- Released feature.\n"

    def test_stops_before_the_next_section(self) -> None:
        rolled = roll(CHANGELOG, "0.102.0", "2026-09-22")

        assert extract(rolled, "0.102.0") == "### Fixed\n\n- Unreleased fix.\n"

    def test_returns_nothing_for_an_empty_section(self) -> None:
        empty = CHANGELOG.replace("### Added\n\n- Released feature.\n\n", "")

        assert extract(empty, "0.101.0") == ""

    def test_refuses_a_missing_version(self) -> None:
        with pytest.raises(ChangelogError, match=r"no section for 0\.102\.0"):
            extract(CHANGELOG, "0.102.0")


class TestMain:
    # Every call points --path and --fragments inside tmp_path, so a test never reads or deletes the
    # repo's own changelog.d. The one call that omits --fragments relies on it deriving from --path.
    def _argv(self, path: Path, directory: Path, *command: str) -> list[str]:
        return ["--path", str(path), "--fragments", str(directory), *command]

    def test_roll_rewrites_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / "CHANGELOG.md"
        path.write_text(CHANGELOG, encoding="utf-8")
        directory = _entries(tmp_path, {})

        exit_code = main(self._argv(path, directory, "roll", "0.102.0", "--date", "2026-09-22"))

        assert exit_code == 0
        assert "## [0.102.0] - 2026-09-22" in path.read_text(encoding="utf-8")

    def test_roll_folds_entry_files_and_deletes_them(self, tmp_path: Path) -> None:
        path = tmp_path / "CHANGELOG.md"
        path.write_text(CHANGELOG, encoding="utf-8")
        directory = _entries(tmp_path, {"added-feature.md": "- A feature.\n", "README.md": "# changelog.d\n"})

        exit_code = main(self._argv(path, directory, "roll", "0.102.0", "--date", "2026-09-22"))

        assert exit_code == 0
        assert "### Added\n\n- A feature." in path.read_text(encoding="utf-8")
        assert sorted(entry.name for entry in directory.iterdir()) == ["README.md"]

    def test_roll_leaves_the_file_alone_on_failure(self, tmp_path: Path) -> None:
        path = tmp_path / "CHANGELOG.md"
        path.write_text(CHANGELOG, encoding="utf-8")
        directory = _entries(tmp_path, {"added-feature.md": "- A feature.\n"})

        exit_code = main(self._argv(path, directory, "roll", "0.101.0", "--date", "2026-09-22"))

        assert exit_code == 1
        assert path.read_text(encoding="utf-8") == CHANGELOG
        assert (directory / "added-feature.md").exists()

    def test_roll_refuses_a_document_it_would_leave_invalid(self, tmp_path: Path) -> None:
        # check accepts link definitions above the sections, and the roll's splices assume they sit
        # below, so they land on the wrong lines. The roll is the step that deletes the entry files
        # and the one a release tags, so it has to refuse rather than hand that on.
        path = tmp_path / "CHANGELOG.md"
        source = (
            f"{PREAMBLE}\n[Unreleased]: {REPO}/compare/v0.101.0...HEAD\n"
            f"[0.101.0]: {REPO}/compare/v0.100.0...v0.101.0\n"
            "\n## [Unreleased]\n\n## [0.101.0] - 2026-09-15\n\n### Added\n\n- Released feature.\n"
        )
        path.write_text(source, encoding="utf-8")
        directory = _entries(tmp_path, {"added-feature.md": "- A feature.\n"})

        exit_code = main(self._argv(path, directory, "roll", "0.102.0", "--date", "2026-09-22"))

        assert check(source) == []
        assert exit_code == 1
        assert path.read_text(encoding="utf-8") == source
        assert (directory / "added-feature.md").exists()

    def test_check_fails_on_problems(self, tmp_path: Path) -> None:
        path = tmp_path / "CHANGELOG.md"
        path.write_text(EMPTY_UNRELEASED.replace("### Added", "### Improved"), encoding="utf-8")

        assert main(self._argv(path, _entries(tmp_path, {}), "check")) == 1

    def test_check_fails_on_an_entry_written_under_unreleased(self, tmp_path: Path) -> None:
        path = tmp_path / "CHANGELOG.md"
        path.write_text(CHANGELOG, encoding="utf-8")

        assert main(self._argv(path, _entries(tmp_path, {}), "check")) == 1

    def test_check_fails_on_a_bad_entry_file(self, tmp_path: Path) -> None:
        path = tmp_path / "CHANGELOG.md"
        path.write_text(EMPTY_UNRELEASED, encoding="utf-8")
        directory = _entries(tmp_path, {"improved-feature.md": "- A feature.\n"})

        assert main(self._argv(path, directory, "check")) == 1

    def test_takes_the_entry_directory_from_the_changelog_it_rolls(self, tmp_path: Path) -> None:
        # Without --fragments, a roll against a copy elsewhere must not reach the real changelog.d.
        path = tmp_path / "CHANGELOG.md"
        path.write_text(CHANGELOG, encoding="utf-8")
        directory = _entries(tmp_path, {"added-feature.md": "- A feature.\n"})

        exit_code = main(["--path", str(path), "roll", "0.102.0", "--date", "2026-09-22"])

        assert exit_code == 0
        assert "### Added\n\n- A feature." in path.read_text(encoding="utf-8")
        assert not (directory / "added-feature.md").exists()

    def test_check_passes_a_well_formed_file(self, tmp_path: Path) -> None:
        path = tmp_path / "CHANGELOG.md"
        path.write_text(EMPTY_UNRELEASED, encoding="utf-8")
        directory = _entries(tmp_path, {"added-feature.md": "- A feature.\n"})

        assert main(self._argv(path, directory, "check")) == 0

    def test_check_fails_on_entries_that_fold_into_an_invalid_file(self, tmp_path: Path) -> None:
        # An unclosed fence is a well-formed entry on its own, and swallows the rest of the changelog
        # once folded in. The release is the wrong place to find that out.
        path = tmp_path / "CHANGELOG.md"
        path.write_text(EMPTY_UNRELEASED, encoding="utf-8")
        directory = _entries(tmp_path, {"added-fence.md": "- A feature:\n\n  ```python\n  do_thing()\n"})

        assert check_fragments(directory) == []
        assert main(self._argv(path, directory, "check")) == 1

    def test_check_fails_on_a_section_the_release_could_not_fold(self, tmp_path: Path) -> None:
        # The structural checks accept loose prose under [Unreleased], and it is not an entry, so the
        # rule above does not see it either. The fold refuses it, and a release is too late to learn that.
        path = tmp_path / "CHANGELOG.md"
        source = EMPTY_UNRELEASED.replace("## [Unreleased]\n", "## [Unreleased]\n\nLoose prose nobody can place.\n")
        path.write_text(source, encoding="utf-8")
        directory = _entries(tmp_path, {})

        assert check(source) == []
        assert check_unreleased_is_empty(source) == []
        assert main(self._argv(path, directory, "check")) == 1

    def test_render_says_so_when_there_is_nothing_to_release(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "CHANGELOG.md"
        path.write_text(EMPTY_UNRELEASED, encoding="utf-8")

        exit_code = main(self._argv(path, _entries(tmp_path, {}), "render"))

        output = capsys.readouterr()
        assert exit_code == 0
        # The notes go to stdout alone, so redirecting them still yields a usable file.
        assert output.out == ""
        assert "Nothing to release" in output.err

    def test_render_prints_the_folded_section(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        path = tmp_path / "CHANGELOG.md"
        path.write_text(CHANGELOG, encoding="utf-8")
        directory = _entries(tmp_path, {"added-feature.md": "- A feature.\n"})

        exit_code = main(self._argv(path, directory, "render"))

        assert exit_code == 0
        assert capsys.readouterr().out == "### Added\n\n- A feature.\n\n### Fixed\n\n- Unreleased fix.\n"
        assert (directory / "added-feature.md").exists()
