from pathlib import Path

import pytest

from scripts.changelog import ChangelogError, check, extract, main, roll

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


def _messages(source: str) -> list[str]:
    return [problem.message for problem in check(source)]


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


class TestRoll:
    def test_renames_unreleased_and_its_link(self) -> None:
        rolled = roll(CHANGELOG, "0.102.0", "2026-09-22")

        assert "## [Unreleased]\n\n## [0.102.0] - 2026-09-22\n\n### Fixed\n\n- Unreleased fix." in rolled
        assert f"[Unreleased]: {REPO}/compare/v0.102.0...HEAD\n" in rolled
        assert f"[0.102.0]: {REPO}/compare/v0.101.0...v0.102.0\n" in rolled
        assert check(rolled) == []

    def test_refuses_an_empty_release_unless_allowed(self) -> None:
        empty = CHANGELOG.replace("### Fixed\n\n- Unreleased fix.\n\n", "")

        with pytest.raises(ChangelogError, match="has no entries"):
            roll(empty, "0.102.0", "2026-09-22")
        assert "## [0.102.0] - 2026-09-22" in roll(empty, "0.102.0", "2026-09-22", allow_empty=True)

    def test_refuses_a_version_that_already_has_a_section(self) -> None:
        with pytest.raises(ChangelogError, match="already has a section"):
            roll(CHANGELOG, "0.101.0", "2026-09-22")

    def test_refuses_an_unreleased_link_that_does_not_compare_to_head(self) -> None:
        source = CHANGELOG.replace("v0.101.0...HEAD", "v0.101.0...main")

        with pytest.raises(ChangelogError, match="must compare a tag to HEAD"):
            roll(source, "0.102.0", "2026-09-22")


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
    def test_roll_rewrites_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / "CHANGELOG.md"
        path.write_text(CHANGELOG, encoding="utf-8")

        exit_code = main(["--path", str(path), "roll", "0.102.0", "--date", "2026-09-22"])

        assert exit_code == 0
        assert "## [0.102.0] - 2026-09-22" in path.read_text(encoding="utf-8")

    def test_roll_leaves_the_file_alone_on_failure(self, tmp_path: Path) -> None:
        path = tmp_path / "CHANGELOG.md"
        path.write_text(CHANGELOG, encoding="utf-8")

        exit_code = main(["--path", str(path), "roll", "0.101.0", "--date", "2026-09-22"])

        assert exit_code == 1
        assert path.read_text(encoding="utf-8") == CHANGELOG

    def test_check_fails_on_problems(self, tmp_path: Path) -> None:
        path = tmp_path / "CHANGELOG.md"
        path.write_text(CHANGELOG.replace("### Fixed", "### Improved"), encoding="utf-8")

        assert main(["--path", str(path), "check"]) == 1
