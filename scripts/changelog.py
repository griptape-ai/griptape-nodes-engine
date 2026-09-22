"""Check, roll, and extract sections of CHANGELOG.md, which follows Keep a Changelog 2.0.0.

Usage:
    python scripts/changelog.py check
    python scripts/changelog.py roll VERSION [--date YYYY-MM-DD] [--allow-empty]
    python scripts/changelog.py extract VERSION

`check` validates structure only. Whether a change needs an entry, and whether an entry is worth
reading, stays a human call. `roll` renames `[Unreleased]` to a dated version at release time.
`extract` prints one version's section for the GitHub release body.

Standard library only, so CI can run it without installing the project.
"""

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SPEC_URL = "https://keepachangelog.com/en/2.0.0/"
CHANGE_TYPES = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")
DEFAULT_PATH = Path("CHANGELOG.md")

TITLE = "# Changelog"
UNRELEASED_HEADING = "## [Unreleased]"
UNRELEASED_LABEL = "unreleased"
UNRELEASED_HEADING_LINE = re.compile(r"^## \[Unreleased\]$")
UNRELEASED_DEFINITION = re.compile(r"^\[unreleased\]: ", re.IGNORECASE)
SECTION_HEADING = re.compile(r"^## ")

VERSION = re.compile(r"^\d+\.\d+\.\d+$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VERSION_HEADING = re.compile(r"^## \[(\d+\.\d+\.\d+)\](?: - (\d{4}-\d{2}-\d{2}))?( \[YANKED\])?$")
LINK_DEFINITION = re.compile(r"^\[([^\]]+)\]: (\S+)$")
ENTRY = re.compile(r"^\s*[-*] ")
CODE_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
# The Unreleased link compares the latest release tag to HEAD, so it carries the base URL and the
# previous tag the rolled version compares against.
UNRELEASED_COMPARE = re.compile(r"^(.*)/compare/(.+)\.\.\.HEAD$")


class ChangelogError(Exception):
    pass


@dataclass(frozen=True)
class Problem:
    line: int
    message: str


@dataclass
class _OpenType:
    name: str
    line: int
    entries: int = 0


@dataclass(frozen=True)
class _VersionHeading:
    version: str
    line: int


class _Lines:
    """A changelog split into lines, with lines inside fenced code blocks marked."""

    def __init__(self, source: str) -> None:
        normalized = source.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
        self.lines = normalized.split("\n")
        self.fenced = _mark_fenced(self.lines)

    def find(self, pattern: re.Pattern[str], start: int = 0, end: int | None = None) -> int:
        """Return the index of the first line outside a code block matching `pattern`, or -1."""
        if end is None:
            end = len(self.lines)
        for index in range(start, end):
            if not self.fenced[index] and pattern.match(self.lines[index]):
                return index
        return -1

    def section_end(self, heading_index: int) -> int:
        """Return the index just past the section that starts at `heading_index`."""
        for index in range(heading_index + 1, len(self.lines)):
            if self.fenced[index]:
                continue
            line = self.lines[index]
            if line.startswith("## ") or LINK_DEFINITION.match(line):
                return index
        return len(self.lines)

    def version_heading_index(self, version: str) -> int:
        for index, line in enumerate(self.lines):
            if self.fenced[index]:
                continue
            heading = VERSION_HEADING.match(line)
            if heading and heading.group(1) == version:
                return index
        return -1


class _Checker:
    """Walks a changelog once and collects structural problems."""

    def __init__(self, source: str) -> None:
        self._lines = _Lines(source)
        self._problems: list[Problem] = []
        self._versions: list[_VersionHeading] = []
        self._labels: set[str] = set()
        self._unreleased_line: int | None = None
        self._section: str | None = None
        self._section_types: set[str] = set()
        self._open_type: _OpenType | None = None

    def run(self) -> list[Problem]:
        lines = self._lines.lines
        if lines[0] != TITLE:
            self._report(0, f'file must open with "{TITLE}"')

        first_section = self._lines.find(SECTION_HEADING)
        preamble_end = len(lines) if first_section == -1 else first_section
        if "keepachangelog.com" not in "\n".join(lines[:preamble_end]):
            self._report(0, "preamble must link the Keep a Changelog version this file follows")

        for index, line in enumerate(lines):
            if self._lines.fenced[index]:
                continue
            self._check_line(index, line, first_section)
        self._close_type()

        self._check_versions()
        if self._unreleased_line is None:
            self._report(0, f"no {UNRELEASED_HEADING} section to collect upcoming changes")
        elif UNRELEASED_LABEL not in self._labels:
            self._report(self._unreleased_line, "Unreleased has no link definition")
        return self._problems

    def _check_line(self, index: int, line: str, first_section: int) -> None:
        link = LINK_DEFINITION.match(line)
        if link:
            self._check_link(index, link.group(1), link.group(2))
            return
        if line.startswith("## "):
            self._check_section_heading(index, line, first_section)
            return
        if line.startswith("### "):
            self._check_type_heading(index, line[4:].strip())
            return
        if line.startswith("#### "):
            self._report(index, "entries are bullet points, not headings")
            return
        if ENTRY.match(line):
            self._check_entry(index)

    def _check_link(self, index: int, label: str, url: str) -> None:
        self._labels.add(label.lower())
        if label.lower() == UNRELEASED_LABEL and not UNRELEASED_COMPARE.match(url):
            self._report(index, "the Unreleased link must compare the latest release tag to HEAD")

    def _check_section_heading(self, index: int, line: str, first_section: int) -> None:
        self._close_type()
        self._section_types = set()

        if line == UNRELEASED_HEADING:
            if self._unreleased_line is not None:
                self._report(index, f"duplicate {UNRELEASED_HEADING} section")
            elif index != first_section:
                self._report(index, f"{UNRELEASED_HEADING} must be the first section")
            self._unreleased_line = index
            self._section = "Unreleased"
            return

        heading = VERSION_HEADING.match(line)
        if heading is None:
            self._report(index, 'version heading must look like "## [1.2.3] - YYYY-MM-DD"')
            self._section = None
            return

        version = heading.group(1)
        if heading.group(2) is None:
            self._report(index, f"version {version} must show its release date as YYYY-MM-DD")
        self._versions.append(_VersionHeading(version=version, line=index))
        self._section = version

    def _check_type_heading(self, index: int, name: str) -> None:
        self._close_type()
        if self._section is None:
            self._report(index, f'"### {name}" is not inside a version section')
        if name not in CHANGE_TYPES:
            self._report(index, f'unknown change type "{name}"; use one of {", ".join(CHANGE_TYPES)}')
        if name in self._section_types:
            self._report(index, f'duplicate "### {name}" in the same section')
        self._section_types.add(name)
        self._open_type = _OpenType(name=name, line=index)

    def _check_entry(self, index: int) -> None:
        if self._open_type is not None:
            self._open_type.entries += 1
            return
        if self._section is not None:
            self._report(index, "entry must be grouped under a change type heading")

    def _check_versions(self) -> None:
        previous: _VersionHeading | None = None
        for heading in self._versions:
            if previous is not None and _version_key(previous.version) <= _version_key(heading.version):
                self._report(
                    heading.line,
                    f"version {heading.version} must come after {previous.version}; list the latest first",
                )
            if heading.version.lower() not in self._labels:
                self._report(heading.line, f"version {heading.version} has no link definition")
            previous = heading

    def _close_type(self) -> None:
        if self._open_type is not None and self._open_type.entries == 0:
            self._report(self._open_type.line, f'"### {self._open_type.name}" has no entries')
        self._open_type = None

    def _report(self, index: int, message: str) -> None:
        self._problems.append(Problem(line=index + 1, message=message))


def check(source: str) -> list[Problem]:
    """Report structural problems in a changelog. Returns an empty list for a well-formed file."""
    return _Checker(source).run()


def roll(source: str, version: str, date: str, *, allow_empty: bool = False) -> str:
    """Rename the [Unreleased] section to a dated version and open a fresh, empty [Unreleased].

    The rolled version's link compares against whatever tag the Unreleased link pointed at, and the
    Unreleased link moves on to compare the rolled version to HEAD.
    """
    lines = _Lines(source)

    heading_index = lines.find(UNRELEASED_HEADING_LINE)
    if heading_index == -1:
        msg = f'Attempted to roll the changelog into {version}. Failed because there is no "{UNRELEASED_HEADING}" heading.'
        raise ChangelogError(msg)

    existing = lines.version_heading_index(version)
    if existing != -1:
        msg = f"Attempted to roll the changelog into {version}. Failed because line {existing + 1} already has a section for {version}."
        raise ChangelogError(msg)

    has_entries = lines.find(ENTRY, heading_index + 1, lines.section_end(heading_index)) != -1
    if not has_entries and not allow_empty:
        msg = f"Attempted to roll the changelog into {version}. Failed because [Unreleased] has no entries. Add one, or allow an empty release."
        raise ChangelogError(msg)

    definition_index = lines.find(UNRELEASED_DEFINITION)
    if definition_index == -1:
        msg = f'Attempted to roll the changelog into {version}. Failed because there is no "[Unreleased]:" link definition.'
        raise ChangelogError(msg)

    url = lines.lines[definition_index].split(": ", 1)[1]
    compare = UNRELEASED_COMPARE.match(url)
    if compare is None:
        msg = f'Attempted to roll the changelog into {version}. Failed because the [Unreleased] link must compare a tag to HEAD, got "{url}".'
        raise ChangelogError(msg)

    base = compare.group(1)
    previous_tag = compare.group(2)
    rolled = list(lines.lines)
    # Rewrite the link definitions first: they sit below the heading, so editing them second would
    # need a shifted index.
    rolled[definition_index : definition_index + 1] = [
        f"[Unreleased]: {base}/compare/v{version}...HEAD",
        f"[{version}]: {base}/compare/{previous_tag}...v{version}",
    ]
    rolled[heading_index : heading_index + 1] = [UNRELEASED_HEADING, "", f"## [{version}] - {date}"]
    return "\n".join(rolled) + "\n"


def extract(source: str, version: str) -> str:
    """Return the body of one version's section, without its heading."""
    lines = _Lines(source)
    heading_index = lines.version_heading_index(version)
    if heading_index == -1:
        msg = (
            f"Attempted to read the changelog section for {version}. Failed because there is no section for {version}."
        )
        raise ChangelogError(msg)

    body = "\n".join(lines.lines[heading_index + 1 : lines.section_end(heading_index)]).strip("\n")
    if not body:
        return ""
    return body + "\n"


def main(argv: list[str] | None = None) -> int:
    """Run one command against the changelog and return the process exit code."""
    args = _parse_args(argv)
    path: Path = args.path

    try:
        source = path.read_text(encoding="utf-8")
    except OSError as error:
        sys.stderr.write(f"Attempted to read {path}. Failed due to {error.strerror}.\n")
        return 1

    if args.command == "check":
        return _run_check(path, source)
    if args.command == "roll":
        return _run_roll(path, source, args.version, args.date, allow_empty=args.allow_empty)
    return _run_extract(source, args.version)


def _run_check(path: Path, source: str) -> int:
    problems = check(source)
    if problems:
        for problem in problems:
            sys.stderr.write(f"{path}:{problem.line}: {problem.message}\n")
        sys.stderr.write(f"{len(problems)} problem(s) found. See {SPEC_URL}\n")
        return 1

    sys.stdout.write(f"{path} follows Keep a Changelog\n")
    return 0


def _run_roll(path: Path, source: str, version: str, date: str | None, *, allow_empty: bool) -> int:
    if not VERSION.match(version):
        sys.stderr.write(f"Not a release version: {version}\n")
        return 1

    if date is None:
        date = datetime.now(tz=UTC).astimezone().date().isoformat()
    if not DATE.match(date):
        sys.stderr.write(f"Not a YYYY-MM-DD date: {date}\n")
        return 1

    try:
        rolled = roll(source, version, date, allow_empty=allow_empty)
    except ChangelogError as error:
        sys.stderr.write(f"{error}\n")
        return 1

    path.write_text(rolled, encoding="utf-8")
    sys.stdout.write(f"Rolled [Unreleased] into [{version}] - {date}\n")
    return 0


def _run_extract(source: str, version: str) -> int:
    try:
        body = extract(source, version)
    except ChangelogError as error:
        sys.stderr.write(f"{error}\n")
        return 1

    sys.stdout.write(body)
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=f"Maintain CHANGELOG.md, which follows {SPEC_URL}")
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH, help="changelog file (default: CHANGELOG.md)")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("check", help="validate the file's structure")

    roll_parser = commands.add_parser("roll", help="rename [Unreleased] to a dated version")
    roll_parser.add_argument("version", help="release version, e.g. 0.102.0")
    roll_parser.add_argument("--date", help="release date as YYYY-MM-DD (default: today)")
    roll_parser.add_argument("--allow-empty", action="store_true", help="release even if [Unreleased] has no entries")

    extract_parser = commands.add_parser("extract", help="print one version's section")
    extract_parser.add_argument("version", help="release version, e.g. 0.102.0")

    return parser.parse_args(argv)


def _mark_fenced(lines: list[str]) -> list[bool]:
    """Mark lines inside fenced code blocks, so examples are not read as structure."""
    fenced: list[bool] = []
    fence: str | None = None
    for line in lines:
        match = CODE_FENCE.match(line)
        delimiter = None
        if match:
            delimiter = match.group(1)

        if fence is None:
            if delimiter is not None:
                fence = delimiter
            fenced.append(delimiter is not None)
            continue

        if delimiter is not None and delimiter.startswith(fence[0]) and len(delimiter) >= len(fence):
            fence = None
        fenced.append(True)
    return fenced


def _version_key(version: str) -> list[int]:
    return [int(part) for part in version.split(".")]


if __name__ == "__main__":
    raise SystemExit(main())
