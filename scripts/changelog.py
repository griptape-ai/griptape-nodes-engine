"""Check, roll, render, and extract sections of CHANGELOG.md, which follows Keep a Changelog 2.0.0.

Usage:
    python scripts/changelog.py [--path CHANGELOG.md] [--fragments changelog.d] COMMAND

    check
    render
    roll VERSION [--date YYYY-MM-DD] [--allow-empty]
    extract VERSION

`--path` and `--fragments` belong to the parser, so they come before the command.

Unreleased entries live one per file in `changelog.d/`, so two pull requests adding entries never
touch the same line of the same file. `render` shows what they add up to. `roll` folds them into a
dated version section at release time and deletes them.

`check` validates structure only. Whether a change needs an entry, and whether an entry is worth
reading, stays a human call. `extract` prints one version's section for the GitHub release body.

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
DEFAULT_FRAGMENT_DIR = Path("changelog.d")

# An entry file is named for its change type, so the file holds the bullet and nothing else.
FRAGMENT_TYPES = {change_type.lower(): change_type for change_type in CHANGE_TYPES}
FRAGMENT_NAME = re.compile(r"^([a-z]+)-([a-z0-9][a-z0-9-]*)\.md$")
FRAGMENT_DOC = "README.md"
FRAGMENT_EXAMPLE = "fixed-short-slug.md"

# A roll empties [Unreleased], so it writes this back to keep the signpost in front of the next
# contributor who opens the file looking for somewhere to add an entry.
UNRELEASED_COMMENT = (
    f"<!-- Entries go in {DEFAULT_FRAGMENT_DIR}/, one file each. See {DEFAULT_FRAGMENT_DIR}/{FRAGMENT_DOC}. -->"
)

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
# An entry starts in column one; its continuation lines are indented, so they do not start a new one.
TOP_LEVEL_ENTRY = re.compile(r"^[-*] ")
BREAKING_ENTRY = re.compile(r"^[-*] \*\*Breaking:\*\*")
COMMENT = re.compile(r"^\s*<!--[\s\S]*?-->\s*$")
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
    # An entry file's problems point at that file rather than at the changelog.
    path: Path | None = None


@dataclass(frozen=True)
class Fragment:
    """One unreleased entry, read from its own file in `changelog.d`."""

    path: Path
    change_type: str
    body: str


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


def check_unreleased_is_empty(source: str) -> list[Problem]:
    """Report entries written under [Unreleased] rather than into an entry file.

    Separate from `check`, which also validates the folded document a release publishes. That one
    carries its entries under [Unreleased] on purpose, so the rule holds for the file on disk only.
    """
    lines = _Lines(source)
    heading_index = lines.find(UNRELEASED_HEADING_LINE)
    if heading_index == -1:
        return []

    problems: list[Problem] = []
    for index in range(heading_index + 1, lines.section_end(heading_index)):
        line = lines.lines[index]
        if not lines.fenced[index] and (line.startswith("### ") or TOP_LEVEL_ENTRY.match(line)):
            problems.append(
                Problem(line=index + 1, message=f"unreleased entries live in {DEFAULT_FRAGMENT_DIR}/, one file each")
            )
    return problems


def check_fragments(directory: Path) -> list[Problem]:
    """Report problems in every entry file in `directory`. Returns an empty list when all are valid."""
    if not directory.is_dir():
        return []

    problems: list[Problem] = []
    for path in _entry_paths(directory):
        problems.extend(_fragment_problems(path))
    return problems


def load_fragments(directory: Path) -> list[Fragment]:
    """Read every entry file in `directory`, in filename order so a fold is reproducible.

    Raises on a file it cannot classify rather than skipping it: a dropped entry means a change
    ships with no release note, which is worse than a release that stops to be fixed.
    """
    if not directory.is_dir():
        return []

    return [_read_fragment(path) for path in _entry_paths(directory)]


def fold(source: str, fragments: list[Fragment]) -> str:
    """Rewrite the [Unreleased] section to hold its own entries plus `fragments`.

    Types come out in Keep a Changelog order with breaking changes first within each type, whatever
    order the entries arrived in.
    """
    lines = _Lines(source)

    heading_index = lines.find(UNRELEASED_HEADING_LINE)
    if heading_index == -1:
        msg = f'Attempted to fold {len(fragments)} changelog entries. Failed because there is no "{UNRELEASED_HEADING}" heading.'
        raise ChangelogError(msg)

    section_end = lines.section_end(heading_index)
    grouped = _group_entries(lines, heading_index + 1, section_end)
    unknown = sorted(set(grouped) - set(CHANGE_TYPES))
    if unknown:
        msg = f"Attempted to fold changelog entries. Failed because {UNRELEASED_HEADING} holds unknown change types: {', '.join(unknown)}."
        raise ChangelogError(msg)

    for fragment in fragments:
        grouped.setdefault(fragment.change_type, []).append(fragment.body)

    folded = list(lines.lines)
    folded[heading_index + 1 : section_end] = _format_entries(grouped)
    return "\n".join(folded) + "\n"


def render(source: str, fragments: list[Fragment]) -> str:
    """Return the body of the [Unreleased] section as the next release will show it."""
    lines = _Lines(fold(source, fragments))
    return _section_body(lines, lines.find(UNRELEASED_HEADING_LINE))


def roll(
    source: str,
    version: str,
    date: str,
    *,
    fragments: list[Fragment] | None = None,
    allow_empty: bool = False,
) -> str:
    """Rename the [Unreleased] section to a dated version and open a fresh, empty [Unreleased].

    Entry files fold into the section first, so a release lists them whether they were written as
    files in `changelog.d` or added to the section by hand.

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

    lines = _Lines(fold(source, fragments or []))
    heading_index = lines.find(UNRELEASED_HEADING_LINE)

    has_entries = lines.find(ENTRY, heading_index + 1, lines.section_end(heading_index)) != -1
    if not has_entries and not allow_empty:
        msg = f"Attempted to roll the changelog into {version}. Failed because there is nothing to release: [Unreleased] is empty and {DEFAULT_FRAGMENT_DIR} holds no entries. Add one, or allow an empty release."
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
    rolled[heading_index : heading_index + 1] = [
        UNRELEASED_HEADING,
        "",
        UNRELEASED_COMMENT,
        "",
        f"## [{version}] - {date}",
    ]
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

    return _section_body(lines, heading_index)


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
        return _run_check(path, source, args.fragments)
    if args.command == "render":
        return _run_render(source, args.fragments)
    if args.command == "roll":
        return _run_roll(path, source, args.fragments, args.version, args.date, allow_empty=args.allow_empty)
    return _run_extract(source, args.version)


def _run_check(path: Path, source: str, directory: Path) -> int:
    unreleased = check_unreleased_is_empty(source)
    problems = check(source) + unreleased + check_fragments(directory)
    if problems:
        for problem in problems:
            sys.stderr.write(f"{problem.path or path}:{problem.line}: {problem.message}\n")
        # Both the entry files' contract and the rule that entries live in them are in that README;
        # only the changelog's own layout is in the spec.
        in_readme = unreleased or any(problem.path for problem in problems)
        reference = f"{directory}/{FRAGMENT_DOC}" if in_readme else SPEC_URL
        sys.stderr.write(f"{len(problems)} problem(s) found. See {reference}\n")
        return 1

    # Check the document the release will publish, not just the parts above. An entry file can be
    # well formed on its own and still fold into a changelog that does not parse, and a release is
    # the wrong place to find that out.
    try:
        folded = fold(source, load_fragments(directory))
    except ChangelogError as error:
        sys.stderr.write(f"{error}\n")
        return 1

    folded_problems = check(folded)
    if folded_problems:
        sys.stderr.write(f"The entry files in {directory} do not fold into a valid {path}:\n")
        for problem in folded_problems:
            sys.stderr.write(f"  {problem.message}\n")
        sys.stderr.write("Run 'make changelog' to see what they fold into.\n")
        return 1

    sys.stdout.write(f"{path} follows Keep a Changelog\n")
    return 0


def _run_render(source: str, directory: Path) -> int:
    try:
        body = render(source, load_fragments(directory))
    except ChangelogError as error:
        sys.stderr.write(f"{error}\n")
        return 1

    # On stdout the empty section reads as a broken command, at the step where a release manager is
    # reading what they are about to publish. The notes themselves stay on stdout alone.
    if not body:
        sys.stderr.write(f"Nothing to release: [Unreleased] is empty and {directory} holds no entries.\n")
    sys.stdout.write(body)
    return 0


def _run_roll(  # noqa: PLR0913
    path: Path, source: str, directory: Path, version: str, date: str | None, *, allow_empty: bool
) -> int:
    if not VERSION.match(version):
        sys.stderr.write(f"Not a release version: {version}\n")
        return 1

    if date is None:
        date = datetime.now(tz=UTC).astimezone().date().isoformat()
    if not DATE.match(date):
        sys.stderr.write(f"Not a YYYY-MM-DD date: {date}\n")
        return 1

    try:
        fragments = load_fragments(directory)
        rolled = roll(source, version, date, fragments=fragments, allow_empty=allow_empty)
    except ChangelogError as error:
        sys.stderr.write(f"{error}\n")
        return 1

    # A roll this command accepts is the one a release tags, and it is also the one that deletes the
    # entry files, so a document it got wrong would cost the text as well as the release.
    problems = check(rolled)
    if problems:
        sys.stderr.write(f"The roll would leave {path} invalid, so nothing was written or deleted:\n")
        for problem in problems:
            sys.stderr.write(f"  line {problem.line}: {problem.message}\n")
        return 1

    path.write_text(rolled, encoding="utf-8")
    # Delete only after the changelog is on disk, so a failed write leaves the entries to retry.
    for fragment in fragments:
        fragment.path.unlink()

    sys.stdout.write(f"Rolled [Unreleased] into [{version}] - {date}\n")
    sys.stdout.write(f"Folded {len(fragments)} entry file(s) from {directory} and deleted them\n")
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
    parser.add_argument(
        "--fragments",
        type=Path,
        help=f"directory of unreleased entry files (default: {DEFAULT_FRAGMENT_DIR} beside --path)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("check", help="validate the file's structure and every entry file")
    commands.add_parser("render", help="print the [Unreleased] section as the next release will show it")

    roll_parser = commands.add_parser("roll", help="rename [Unreleased] to a dated version")
    roll_parser.add_argument("version", help="release version, e.g. 0.102.0")
    roll_parser.add_argument("--date", help="release date as YYYY-MM-DD (default: today)")
    roll_parser.add_argument("--allow-empty", action="store_true", help="release even if [Unreleased] has no entries")

    extract_parser = commands.add_parser("extract", help="print one version's section")
    extract_parser.add_argument("version", help="release version, e.g. 0.102.0")

    args = parser.parse_args(argv)
    # The entries belong to the changelog they fold into. Rehearsing a roll against a copy elsewhere
    # would otherwise delete the real entry files, and an uncommitted one does not come back.
    if args.fragments is None:
        args.fragments = args.path.parent / DEFAULT_FRAGMENT_DIR
    return args


def _entry_paths(directory: Path) -> list[Path]:
    """Return everything in `directory` but its README and dotfiles, in name order.

    Everything else, not just `*.md`: a file this skipped would be neither checked nor folded, so a
    misnamed entry would pass review and then ship with no release note. An entry name cannot start
    with a dot, and `.DS_Store` is gitignored, so reporting dotfiles would stop a release on a file
    the release manager cannot see.
    """
    return [path for path in sorted(directory.iterdir()) if path.name != FRAGMENT_DOC and not path.name.startswith(".")]


def _read_fragment(path: Path) -> Fragment:
    """Return the entry in `path`, raising ChangelogError when the file does not hold exactly one."""
    problems = _fragment_problems(path)
    if problems:
        problem = problems[0]
        msg = f"Attempted to read the changelog entry {path}:{problem.line}. Failed because {problem.message}."
        raise ChangelogError(msg)

    text = _entry_text(path)
    return Fragment(path=path, change_type=FRAGMENT_TYPES[path.name.split("-", 1)[0]], body=text.strip("\n"))


def _entry_text(path: Path) -> str:
    """Return `path`'s text without the byte order mark some editors write ahead of it."""
    return path.read_text(encoding="utf-8").removeprefix("\ufeff")


def _fragment_problems(path: Path) -> list[Problem]:
    """Report everything wrong with one entry file, which holds a single bullet and nothing else."""
    if not path.is_file():
        return [Problem(line=1, message=f"only entry files belong here, named like {FRAGMENT_EXAMPLE}", path=path)]

    name = FRAGMENT_NAME.match(path.name)
    if name is None:
        return [Problem(line=1, message=f'name must look like "{FRAGMENT_EXAMPLE}"', path=path)]
    if name.group(1) not in FRAGMENT_TYPES:
        known = ", ".join(FRAGMENT_TYPES)
        return [Problem(line=1, message=f'"{name.group(1)}" is not a change type; name it one of {known}', path=path)]

    try:
        text = _entry_text(path)
    except UnicodeDecodeError:
        # Reported rather than raised, so one file saved as UTF-16 names itself instead of ending the
        # check over every other entry with a traceback.
        return [Problem(line=1, message="file must be UTF-8 text", path=path)]

    body = text.strip("\n")
    if not body:
        return [Problem(line=1, message="file is empty; it holds one changelog entry", path=path)]

    return _entry_body_problems(path, body.split("\n"))


def _entry_body_problems(path: Path, lines: list[str]) -> list[Problem]:
    """Report problems in the text of an entry file whose name and encoding already check out."""
    problems: list[Problem] = []
    if not TOP_LEVEL_ENTRY.match(lines[0]):
        problems.append(Problem(line=1, message='entry must start with "- "', path=path))
    elif not lines[0][2:].strip():
        # A bare bullet is well formed everywhere else, and ships as an empty line in the release notes.
        problems.append(Problem(line=1, message='entry has no text after "- "', path=path))

    for offset, line in enumerate(lines[1:], start=2):
        if TOP_LEVEL_ENTRY.match(line):
            problems.append(
                Problem(line=offset, message="one entry per file; a second entry needs its own file", path=path)
            )
        elif line.startswith("#"):
            problems.append(
                Problem(line=offset, message="entry holds no headings; its type is in the filename", path=path)
            )
        elif line.strip() and not line.startswith("  "):
            problems.append(Problem(line=offset, message="continuation lines are indented two spaces", path=path))
    return problems


def _group_entries(lines: _Lines, start: int, end: int) -> dict[str, list[str]]:
    """Collect the entries between `start` and `end`, keyed by the change type heading above them.

    Raises on a line it cannot attach to an entry instead of dropping it. The folded section replaces
    the one this read, so a line left out here is a line missing from the release notes. The one
    exception is the comment pointing at `changelog.d`, which `roll` writes back afterwards.
    """
    grouped: dict[str, list[str]] = {}
    change_type: str | None = None
    entry: list[str] = []
    entry_line = start

    for index in range(start, end):
        line = lines.lines[index]
        structural = not lines.fenced[index]
        if structural and line.startswith("### "):
            _append_entry(grouped, change_type, entry, entry_line)
            entry = []
            change_type = line[4:].strip()
        elif structural and TOP_LEVEL_ENTRY.match(line):
            _append_entry(grouped, change_type, entry, entry_line)
            entry = [line]
            entry_line = index
        elif entry:
            # Blank lines, indented continuations, and fenced code all belong to the entry above.
            entry.append(line)
        elif line.strip() and not COMMENT.match(line):
            msg = f"Attempted to fold changelog entries. Failed because line {index + 1} belongs to no entry and is not a one-line comment: {line.strip()!r}."
            raise ChangelogError(msg)

    _append_entry(grouped, change_type, entry, entry_line)
    return grouped


def _append_entry(grouped: dict[str, list[str]], change_type: str | None, entry: list[str], line: int) -> None:
    """Close the entry that started on `line`, dropping the blank lines that trailed it."""
    while entry and not entry[-1].strip():
        entry.pop()
    if not entry:
        return
    if change_type is None:
        msg = f"Attempted to fold changelog entries. Failed because the entry on line {line + 1} has no change type heading above it."
        raise ChangelogError(msg)

    grouped.setdefault(change_type, []).append("\n".join(entry))


def _format_entries(grouped: dict[str, list[str]]) -> list[str]:
    """Lay grouped entries out as section lines, headings in Keep a Changelog order."""
    section: list[str] = []
    for change_type in CHANGE_TYPES:
        entries = grouped.get(change_type)
        if not entries:
            continue
        section.extend(["", f"### {change_type}", ""])
        for entry in sorted(entries, key=lambda candidate: 0 if BREAKING_ENTRY.match(candidate) else 1):
            section.extend(entry.split("\n"))
    section.append("")
    return section


def _section_body(lines: _Lines, heading_index: int) -> str:
    """Return the text under a section heading, or an empty string when the section holds nothing."""
    body = "\n".join(lines.lines[heading_index + 1 : lines.section_end(heading_index)]).strip("\n")
    if not body:
        return ""
    return body + "\n"


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
