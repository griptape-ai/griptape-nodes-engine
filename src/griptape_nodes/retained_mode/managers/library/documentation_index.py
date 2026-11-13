"""Find and read the documentation a node library ships.

A library keeps its docs in a `docs/` folder next to its manifest. `docs/nodes/` holds one page per
node, `docs/workflow_nodes/` one page per workflow node, and `docs/workflows/` one page per workflow
template. Every other page under `docs/` is about the library as a whole.

Nothing here touches the engine. `LibraryDocs` takes a library's root folder and its manifest, so it
can be tested against a folder of files. See docs/development/designs/library_documentation.md.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, NamedTuple

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from griptape_nodes.files.path_utils import canonicalize_for_identity
from griptape_nodes.node_library.workflow_registry import (
    WorkflowMetadata,
    WorkflowMetadataError,
    read_workflow_metadata,
)
from griptape_nodes.retained_mode.events.library_documentation_events import (
    DocumentationEntryKind,
    DocumentationNodeEntry,
    DocumentationNodeKind,
    DocumentationPageEntry,
    DocumentationSkillEntry,
    DocumentationTemplateEntry,
    DocumentationToolEntry,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from griptape_nodes.node_library.library_registry import LibrarySchema, NodeMetadata

DEFAULT_DOCS_FOLDER = "docs"
NODES_FOLDER = "nodes"
WORKFLOW_NODES_FOLDER = "workflow_nodes"
WORKFLOWS_FOLDER = "workflows"
TOOLS_FOLDER = "mcp_tools"
SKILLS_FOLDER = ".agents/skills"
OVERVIEW_PAGE = "index.md"
SKILL_FILE = "SKILL.md"
MAX_PAGE_BYTES = 1_048_576

# Folders directly under docs/ that hold per-node and per-template pages, not library pages.
_RESERVED_TOP_LEVEL_FOLDERS = frozenset({NODES_FOLDER, WORKFLOW_NODES_FOLDER, WORKFLOWS_FOLDER, TOOLS_FOLDER})
_README_NAMES = ("README.md", "readme.md")
_NUMBER_PREFIX = re.compile(r"^\d+[-_.\s]+")
_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")
_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(?P<block>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_HEADING = re.compile(r"^#[ \t]+(?P<text>.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^[ \t]*(```|~~~)")


_URL_PREFIX = re.compile(r"^(https?|data):", re.IGNORECASE)


class DocsProblemKind(StrEnum):
    """The kinds of problem found in a library's docs folder."""

    ORPHAN_PAGE = "orphan_page"
    CONFLICTING_PAGES = "conflicting_pages"
    NEAR_MISS_NAME = "near_miss_name"
    AMBIGUOUS_DISPLAY_NAME = "ambiguous_display_name"
    LINK_OUTSIDE_DOCS = "link_outside_docs"


@dataclass
class DocsProblem:
    """Something wrong with the docs a library ships, worded for the library's author.

    Args:
        kind: What kind of problem this is
        message: What is wrong and what to do about it
        paths: The doc files involved, relative to the docs folder
    """

    kind: DocsProblemKind
    message: str
    paths: list[str] = field(default_factory=list)


class DocsPageError(Exception):
    """A doc page could not be found or read. The message is worded for the person who asked."""


@dataclass
class ParsedPage:
    """A doc page split into its frontmatter and its text.

    Args:
        markdown: The page text with the frontmatter removed
        frontmatter_title: `title` from the frontmatter. None when unset.
        description: `description` from the frontmatter. None when unset.
        when_to_use: `when_to_use` from the frontmatter. None when unset.
        name: `name` from the frontmatter, which a skill uses. None when unset.
        first_heading: Text of the first `# ` heading outside a code block. None when there is none.
        frontmatter_error: Why the frontmatter could not be read. None when it read fine or is absent.
    """

    markdown: str
    frontmatter_title: str | None = None
    description: str | None = None
    when_to_use: str | None = None
    name: str | None = None
    first_heading: str | None = None
    frontmatter_error: str | None = None


class _NodeKey(NamedTuple):
    """Something a page can document: the key its page is named after, and its display name if it has one."""

    key: str
    display_name: str | None


@dataclass
class _PageIndex:
    """Which page documents each node and template, and what is wrong with the pages that match nothing."""

    node_pages: dict[str, Path] = field(default_factory=dict)
    template_pages: dict[str, Path] = field(default_factory=dict)
    tool_pages: dict[str, Path] = field(default_factory=dict)
    problems: list[DocsProblem] = field(default_factory=list)


@dataclass
class _Listings:
    """What a library's docs list, read once so repeated listings cost no file reads."""

    overview_path: str | None
    pages: list[DocumentationPageEntry]
    nodes: list[DocumentationNodeEntry]
    templates: list[DocumentationTemplateEntry]
    skills: list[DocumentationSkillEntry]


class LibraryDocs:
    """The docs a library ships, found from its manifest and its docs folder.

    The docs folder is scanned and every page's frontmatter read the first time a listing is
    asked for, and the result is kept. A page's text is read from disk each time it is asked for,
    so an edit to a page shows up at once. A page added, renamed, or removed shows up when the
    library is loaded again, which builds a new `LibraryDocs`.
    """

    def __init__(self, library_root: Path, schema: LibrarySchema, tool_names: Sequence[str] = ()) -> None:
        self._library_root = library_root
        self._schema = schema
        self._tool_names = list(tool_names)
        self.docs_root: Path | None = self._find_docs_root()
        self._index = _PageIndex()
        self._listings_snapshot: _Listings | None = None
        if self.docs_root is not None:
            self._index = _PageIndexer(self.docs_root, schema, self._tool_names).build()

    @property
    def problems(self) -> list[DocsProblem]:
        """What is wrong with the docs folder, such as pages that match no node."""
        return list(self._index.problems)

    def node_page(self, node_type: str) -> Path | None:
        """The page that documents a node or a workflow node, or None when the library ships none."""
        return self._index.node_pages.get(node_type)

    def template_page(self, name: str) -> Path | None:
        """The page that documents a workflow template, or None when the library ships none."""
        return self._index.template_pages.get(name)

    def tool_page(self, name: str) -> Path | None:
        """The page that documents an MCP tool, named after its request type, or None when the library ships none."""
        return self._index.tool_pages.get(name)

    def tool_entries(self, tools: Sequence[DocumentationToolEntry]) -> list[DocumentationToolEntry]:
        """The tools the engine describes, each with its page when the library ships one.

        A page's title and description replace the generated ones, which come from the request's docstring.
        """
        entries: list[DocumentationToolEntry] = []
        for tool in tools:
            page_path = self._index.tool_pages.get(tool.name)
            page = _read_for_listing(page_path)
            entries.append(
                replace(
                    tool,
                    title=_first_text(page.frontmatter_title if page else None, tool.name),
                    description=_first_text(page.description if page else None, tool.description),
                    has_page=page_path is not None,
                    page_path=self._relative_to_docs(page_path),
                )
            )
        return entries

    def overview_path(self) -> str | None:
        """Path of the library's overview page: `index.md`, or the README standing in for it.

        Read it with `read_page` either way. `overview_is_readme` says which one it is.
        """
        return self._listings().overview_path

    def overview_is_readme(self) -> bool:
        """True when the overview is the library's README because there is no `docs/index.md`."""
        return self.overview_path() is not None and not self._has_index_page()

    def assets_root(self, path: str) -> Path | None:
        """The folder a page's relative images resolve against, or None when the library has no such folder.

        That is the docs folder, except for a README standing in as the overview, whose images sit next to it.
        """
        readme = self._readme_standing_in(path)
        if readme is not None:
            return readme.parent
        return self.docs_root

    def page_entries(self) -> list[DocumentationPageEntry]:
        """The library's pages and folders in display order, leaving out node pages, template pages, and skills."""
        return list(self._listings().pages)

    def node_entries(self) -> list[DocumentationNodeEntry]:
        """Every node and workflow node the library lists, with whether it has a page."""
        return list(self._listings().nodes)

    def template_entries(self) -> list[DocumentationTemplateEntry]:
        """Every workflow template the library lists, with whether it has a page."""
        return list(self._listings().templates)

    def skill_entries(self) -> list[DocumentationSkillEntry]:
        """The skills the library ships for agents that use it."""
        return list(self._listings().skills)

    def read_page(self, path: str) -> ParsedPage:
        """Read a page by its path relative to the docs folder.

        Raises:
            DocsPageError: If the library has no docs folder, the path leaves it, or the page is missing or unreadable.
        """
        return read_markdown_file(self._resolve_page_path(path))

    def _listings(self) -> _Listings:
        if self._listings_snapshot is None:
            self._listings_snapshot = _Listings(
                overview_path=self._build_overview_path(),
                pages=self._build_page_entries(),
                nodes=self._build_node_entries(),
                templates=self._build_template_entries(),
                skills=self._build_skill_entries(),
            )
        return self._listings_snapshot

    def _build_overview_path(self) -> str | None:
        if self._has_index_page():
            return OVERVIEW_PAGE
        if self._find_readme() is not None:
            return OVERVIEW_PAGE
        return None

    def _has_index_page(self) -> bool:
        if self.docs_root is None:
            return False
        index = self.docs_root / OVERVIEW_PAGE
        return index.is_file() and self._is_inside_docs(index)

    def _readme_standing_in(self, path: str) -> Path | None:
        """The README, when `path` is the overview and there is no `docs/index.md` for it to be."""
        if path != OVERVIEW_PAGE or self._has_index_page():
            return None
        return self._find_readme()

    def _is_inside_docs(self, path: Path) -> bool:
        """Whether a file or folder really lives in the docs folder, once any link it is reached through is followed."""
        if self.docs_root is None:
            return False
        return _is_inside(self.docs_root, path)

    def _build_page_entries(self) -> list[DocumentationPageEntry]:
        if self.docs_root is None:
            return []
        entries: list[DocumentationPageEntry] = []
        self._collect_page_entries(self.docs_root, PurePosixPath(), entries)
        return entries

    def _build_node_entries(self) -> list[DocumentationNodeEntry]:
        entries = [self._node_entry(node.class_name, node.metadata) for node in self._schema.nodes]
        entries.extend(
            self._node_entry(workflow_node.node_type, workflow_node.metadata, DocumentationNodeKind.WORKFLOW_NODE)
            for workflow_node in self._schema.workflow_nodes or []
        )
        return entries

    def _build_template_entries(self) -> list[DocumentationTemplateEntry]:
        entries: list[DocumentationTemplateEntry] = []
        for workflow_path in self._schema.workflows or []:
            name = Path(workflow_path).stem
            page_path = self._index.template_pages.get(name)
            page = _read_for_listing(page_path)
            file_path = canonicalize_for_identity(workflow_path, base=self._library_root)
            metadata = _read_template_metadata(file_path)
            entries.append(
                DocumentationTemplateEntry(
                    name=name,
                    title=_first_text(page.frontmatter_title if page else None, name),
                    has_page=page_path is not None,
                    page_path=self._relative_to_docs(page_path),
                    description=_first_text(
                        page.description if page else None, metadata.description if metadata else None
                    )
                    or None,
                    when_to_use=page.when_to_use if page else None,
                    image=_template_image(metadata, file_path),
                )
            )
        return entries

    def _build_skill_entries(self) -> list[DocumentationSkillEntry]:
        if self.docs_root is None:
            return []
        skills_root = self.docs_root / SKILLS_FOLDER
        if not skills_root.is_dir():
            return []

        entries: list[DocumentationSkillEntry] = []
        skill_dirs = (d for d in skills_root.iterdir() if d.is_dir() and self._is_inside_docs(d))
        for skill_dir in sorted(skill_dirs, key=lambda d: d.name.lower()):
            skill_file = skill_dir / SKILL_FILE
            if not skill_file.is_file() or not self._is_inside_docs(skill_file):
                continue
            page = _read_for_listing(skill_file)
            relative = PurePosixPath(SKILLS_FOLDER) / skill_dir.name / SKILL_FILE
            entries.append(
                DocumentationSkillEntry(
                    name=_first_text(page.name if page else None, skill_dir.name),
                    description=(page.description if page else None) or "",
                    path=relative.as_posix(),
                )
            )
        return entries

    def _find_docs_root(self) -> Path | None:
        configured = self._schema.documentation_path or DEFAULT_DOCS_FOLDER
        candidate = canonicalize_for_identity(configured, base=self._library_root)
        if not candidate.is_dir():
            return None
        return candidate

    def _find_readme(self) -> Path | None:
        # The manifest sits at the repo root or in a package folder one level down.
        for folder in (self._library_root, self._library_root.parent):
            for name in _README_NAMES:
                candidate = folder / name
                if candidate.is_file():
                    return candidate
        return None

    def _resolve_page_path(self, path: str) -> Path:
        relative = PurePosixPath(path.replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            msg = f"Attempted to read the page '{path}'. Failed because a page path must stay inside the library's docs folder."
            raise DocsPageError(msg)

        readme = self._readme_standing_in(path)
        if readme is not None:
            return readme

        if self.docs_root is None:
            msg = f"Attempted to read the page '{path}'. Failed because the library has no docs folder."
            raise DocsPageError(msg)

        candidate = canonicalize_for_identity(self.docs_root / Path(*relative.parts))
        if not candidate.is_relative_to(self.docs_root):
            msg = f"Attempted to read the page '{path}'. Failed because it points outside the library's docs folder."
            raise DocsPageError(msg)

        if candidate.suffix != ".md":
            msg = f"Attempted to read the page '{path}'. Failed because only markdown pages ending in .md can be read."
            raise DocsPageError(msg)

        if candidate.is_file():
            return candidate

        msg = f"Attempted to read the page '{path}'. Failed because the library has no page at that path."
        raise DocsPageError(msg)

    def _collect_page_entries(
        self, directory: Path, relative: PurePosixPath, entries: list[DocumentationPageEntry]
    ) -> None:
        is_top_level = relative == PurePosixPath()
        for child in _sorted_children(directory, is_top_level=is_top_level):
            if not self._is_inside_docs(child):
                continue
            child_relative = relative / child.name
            if child.is_dir():
                folder_entries: list[DocumentationPageEntry] = []
                self._collect_page_entries(child, child_relative, folder_entries)
                if folder_entries:
                    entries.append(
                        DocumentationPageEntry(
                            title=_humanize_folder(child.name),
                            path=child_relative.as_posix(),
                            kind=DocumentationEntryKind.FOLDER,
                        )
                    )
                    entries.extend(folder_entries)
                continue

            page = _read_for_listing(child)
            entries.append(
                DocumentationPageEntry(
                    title=page_title(page, child.stem),
                    path=child_relative.as_posix(),
                    kind=DocumentationEntryKind.PAGE,
                    description=page.description if page else None,
                    when_to_use=page.when_to_use if page else None,
                )
            )

    def _relative_to_docs(self, page_path: Path | None) -> str | None:
        """A page's path relative to the docs folder, or None when there is no page."""
        if page_path is None or self.docs_root is None:
            return None
        return page_path.relative_to(self.docs_root).as_posix()

    def _node_entry(
        self, node_type: str, metadata: NodeMetadata, kind: DocumentationNodeKind = DocumentationNodeKind.NODE
    ) -> DocumentationNodeEntry:
        page_path = self._index.node_pages.get(node_type)
        page = _read_for_listing(page_path)
        return DocumentationNodeEntry(
            node_type=node_type,
            title=_first_text(page.frontmatter_title if page else None, metadata.display_name),
            category=metadata.category,
            has_page=page_path is not None,
            page_path=self._relative_to_docs(page_path),
            description=_first_text(page.description if page else None, metadata.description),
            when_to_use=page.when_to_use if page else None,
            kind=kind,
        )


class _PageIndexer:
    """Matches the pages under the reserved docs folders to the nodes and templates they document."""

    def __init__(self, docs_root: Path, schema: LibrarySchema, tool_names: Sequence[str] = ()) -> None:
        self._tool_names = list(tool_names)
        self._docs_root = docs_root
        self._schema = schema
        self._problems: list[DocsProblem] = []

    def build(self) -> _PageIndex:
        self._report_links_outside_docs()

        nodes = [_NodeKey(node.class_name, node.metadata.display_name) for node in self._schema.nodes]
        node_pages = self._index_folder(NODES_FOLDER, nodes)

        workflow_nodes = [
            _NodeKey(node.node_type, node.metadata.display_name) for node in self._schema.workflow_nodes or []
        ]
        node_pages.update(self._index_folder(WORKFLOW_NODES_FOLDER, workflow_nodes))

        templates = [_NodeKey(Path(path).stem, None) for path in self._schema.workflows or []]
        template_pages = self._index_folder(WORKFLOWS_FOLDER, templates)

        tool_pages = self._index_folder(TOOLS_FOLDER, [_NodeKey(name, None) for name in self._tool_names])

        return _PageIndex(
            node_pages=node_pages, template_pages=template_pages, tool_pages=tool_pages, problems=self._problems
        )

    def _index_folder(self, folder_name: str, keys: list[_NodeKey]) -> dict[str, Path]:
        """Match every page under one reserved folder to the node or template it documents."""
        folder = self._docs_root / folder_name
        if not folder.is_dir():
            return {}

        by_key = {node.key: node for node in keys}
        by_display_name: dict[str, list[str]] = defaultdict(list)
        for node in by_key.values():
            if node.display_name is not None:
                by_display_name[_normalize(node.display_name)].append(node.key)

        claims: dict[str, list[Path]] = defaultdict(list)
        for page in sorted(folder.rglob("*.md")):
            if not page.is_file() or self._is_hidden(page) or not _is_inside(self._docs_root, page):
                continue
            # A name with a dot is a locale variant such as LoadAudio.fr.md. Locales are not read yet.
            if "." in page.stem:
                continue
            key = self._match_page_to_key(page, by_key, by_display_name)
            if key is not None:
                claims[key].append(page)

        return {key: self._pick_page(key, pages) for key, pages in claims.items()}

    def _match_page_to_key(
        self, page: Path, by_key: dict[str, _NodeKey], by_display_name: dict[str, list[str]]
    ) -> str | None:
        stem = page.stem
        if stem in by_key:
            return stem

        display_matches = by_display_name.get(_normalize(stem), [])
        if len(display_matches) == 1:
            return display_matches[0]
        if len(display_matches) > 1:
            names = ", ".join(sorted(display_matches))
            self._add_problem(
                DocsProblemKind.AMBIGUOUS_DISPLAY_NAME,
                f"The page '{self._relative(page)}' is named after a display name that {names} all share, so it "
                f"cannot tell which one it documents. Rename it after the class name of the one it documents.",
                [page],
            )
            return None

        near_misses = [key for key in by_key if key.lower() == stem.lower()]
        if near_misses:
            self._add_problem(
                DocsProblemKind.NEAR_MISS_NAME,
                f"The page '{self._relative(page)}' matches '{near_misses[0]}' except for capitalization. It would "
                f"work on a case-insensitive filesystem and fail on others. Rename it to '{near_misses[0]}.md'.",
                [page],
            )
            return None

        self._add_problem(
            DocsProblemKind.ORPHAN_PAGE,
            f"The page '{self._relative(page)}' does not match any node, template or tool in the library, so "
            f"it is never shown. Rename it after the class name or display name of the node, or the request type of the tool, it documents.",
            [page],
        )
        return None

    def _pick_page(self, key: str, pages: list[Path]) -> Path:
        """The page that documents a key. With several, the one nearest the top wins, then the first by path."""
        if len(pages) == 1:
            return pages[0]

        ranked = sorted(
            pages, key=lambda page: (len(page.relative_to(self._docs_root).parts), self._relative(page).lower())
        )
        names = ", ".join(f"'{self._relative(page)}'" for page in ranked)
        self._add_problem(
            DocsProblemKind.CONFLICTING_PAGES,
            f"More than one page documents '{key}': {names}. The engine uses '{self._relative(ranked[0])}'. "
            f"Delete or rename the others.",
            ranked,
        )
        return ranked[0]

    def _report_links_outside_docs(self) -> None:
        """Report every link under the docs folder that leads out of it. Such a page or folder is never shown."""
        for path in sorted(self._docs_root.rglob("*")):
            if not path.is_symlink() or _is_inside(self._docs_root, path):
                continue
            self._add_problem(
                DocsProblemKind.LINK_OUTSIDE_DOCS,
                f"'{self._relative(path)}' is a link to somewhere outside the docs folder, so it is never shown. "
                f"Copy the file or folder into the docs folder instead.",
                [path],
            )

    def _is_hidden(self, page: Path) -> bool:
        return any(part.startswith(".") for part in page.relative_to(self._docs_root).parts)

    def _relative(self, page: Path) -> str:
        return page.relative_to(self._docs_root).as_posix()

    def _add_problem(self, kind: DocsProblemKind, message: str, pages: list[Path]) -> None:
        self._problems.append(DocsProblem(kind=kind, message=message, paths=[self._relative(page) for page in pages]))


def read_markdown_file(path: Path) -> ParsedPage:
    """Read a markdown file and split off its frontmatter.

    Raises:
        DocsPageError: If the file is larger than the page limit or cannot be read as text.
    """
    try:
        size = path.stat().st_size
    except OSError as err:
        msg = f"Attempted to read the page '{path.name}'. Failed because the file could not be opened: {err}"
        raise DocsPageError(msg) from err

    if size > MAX_PAGE_BYTES:
        msg = f"Attempted to read the page '{path.name}'. Failed because it is larger than the 1 MB page limit."
        raise DocsPageError(msg)

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        msg = f"Attempted to read the page '{path.name}'. Failed because the file could not be read as text: {err}"
        raise DocsPageError(msg) from err

    return parse_page(text)


def parse_page(text: str) -> ParsedPage:
    """Split a page into its frontmatter fields and its text."""
    match = _FRONTMATTER.match(text)
    if match is None:
        return ParsedPage(markdown=text, first_heading=_find_first_heading(text))

    body = text[match.end() :].lstrip("\r\n")
    heading = _find_first_heading(body)
    try:
        loaded = YAML(typ="safe").load(match.group("block"))
    except YAMLError as err:
        return ParsedPage(markdown=body, first_heading=heading, frontmatter_error=str(err))

    if loaded is None:
        return ParsedPage(markdown=body, first_heading=heading)
    if not isinstance(loaded, dict):
        return ParsedPage(
            markdown=body,
            first_heading=heading,
            frontmatter_error="The frontmatter must be a list of key: value lines.",
        )

    return ParsedPage(
        markdown=body,
        frontmatter_title=_as_text(loaded.get("title")),
        description=_as_text(loaded.get("description")),
        when_to_use=_as_text(loaded.get("when_to_use")),
        name=_as_text(loaded.get("name")),
        first_heading=heading,
    )


def page_title(page: ParsedPage | None, filename_stem: str) -> str:
    """A library page's title: its frontmatter title, then its first heading, then its filename."""
    return _first_text(
        page.frontmatter_title if page else None,
        page.first_heading if page else None,
        _NUMBER_PREFIX.sub("", filename_stem),
    )


def _read_for_listing(path: Path | None) -> ParsedPage | None:
    """Read a page's frontmatter for a listing, leaving it out when the page is missing or unreadable."""
    if path is None:
        return None
    try:
        return read_markdown_file(path)
    except DocsPageError:
        return None


def _is_inside(root: Path, path: Path) -> bool:
    """Whether `path` is under `root` once any links in it are followed, so a link out of the folder is not."""
    return canonicalize_for_identity(path).is_relative_to(root)


def _sorted_children(directory: Path, *, is_top_level: bool) -> list[Path]:
    children = []
    for child in directory.iterdir():
        if child.name.startswith("."):
            continue
        if child.is_dir():
            if is_top_level and child.name in _RESERVED_TOP_LEVEL_FOLDERS:
                continue
            children.append(child)
        elif child.is_file() and child.suffix == ".md":
            children.append(child)
    return sorted(children, key=lambda child: (child.name != OVERVIEW_PAGE, child.name.lower()))


def _read_template_metadata(file_path: Path) -> WorkflowMetadata | None:
    """The metadata header of a template file, or None when it can't be read. A bad header never blocks docs."""
    try:
        return read_workflow_metadata(file_path)
    except WorkflowMetadataError:
        return None


def _template_image(metadata: WorkflowMetadata | None, file_path: Path) -> str | None:
    """The template's thumbnail: a URL as written, or the absolute path of a file that exists."""
    if metadata is None or not metadata.image:
        return None
    if _URL_PREFIX.match(metadata.image):
        return metadata.image
    candidate = canonicalize_for_identity(metadata.image, base=file_path.parent)
    if candidate.is_file():
        return str(candidate)
    return None


def _first_text(*candidates: str | None) -> str:
    for candidate in candidates:
        if candidate:
            return candidate
    return ""


def _as_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text


def _find_first_heading(text: str) -> str | None:
    in_fence = False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING.match(line)
        if match is not None:
            return match.group("text")
    return None


def _normalize(name: str) -> str:
    """A name reduced to lowercase words joined by underscores, so 'Create Noise-Latents' is 'create_noise_latents'."""
    return _NON_ALPHANUMERIC.sub("_", name.lower()).strip("_")


def _humanize_folder(name: str) -> str:
    words = _NUMBER_PREFIX.sub("", name).replace("-", " ").replace("_", " ").strip()
    if not words:
        return name
    return words[0].upper() + words[1:]
