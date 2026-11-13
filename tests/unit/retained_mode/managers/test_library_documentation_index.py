"""Finding and reading the docs a library ships, against a library built in a temporary folder."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from griptape_nodes.node_library.library_registry import (
    LibraryMetadata,
    LibrarySchema,
    NodeDefinition,
    NodeMetadata,
    WorkflowNodeDefinition,
)
from griptape_nodes.retained_mode.events.library_documentation_events import (
    DocumentationEntryKind,
    DocumentationNodeKind,
    DocumentationToolEntry,
)
from griptape_nodes.retained_mode.managers.library.documentation_index import (
    MAX_PAGE_BYTES,
    DocsPageError,
    DocsProblemKind,
    LibraryDocs,
    parse_page,
)

if TYPE_CHECKING:
    from pathlib import Path

CONFLICTING_PAGE_COUNT = 3


def _node(class_name: str, display_name: str, category: str = "Audio") -> NodeDefinition:
    return NodeDefinition(
        class_name=class_name,
        file_path=f"nodes/{class_name}.py",
        metadata=NodeMetadata(
            category=category, description=f"{display_name} from the manifest", display_name=display_name
        ),
    )


def _workflow_node(node_type: str, display_name: str) -> WorkflowNodeDefinition:
    return WorkflowNodeDefinition(
        node_type=node_type,
        workflow_path=f"workflows/{node_type}.py",
        metadata=NodeMetadata(
            category="Preconfigured", description=f"{display_name} workflow", display_name=display_name
        ),
    )


def _schema(
    nodes: list[NodeDefinition] | None = None,
    workflow_nodes: list[WorkflowNodeDefinition] | None = None,
    workflows: list[str] | None = None,
    documentation_path: str | None = None,
) -> LibrarySchema:
    return LibrarySchema(
        name="Docs Test Library",
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="test", description="test", library_version="0.1.0", engine_version="0.98.0", tags=[]
        ),
        categories=[],
        nodes=nodes or [],
        workflow_nodes=workflow_nodes,
        workflows=workflows,
        documentation_path=documentation_path,
    )


def _write(root: Path, relative: str, text: str = "Some text.\n") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _write_template(root: Path, relative: str, description: str | None = None, image: str | None = None) -> None:
    lines = [
        "# /// script",
        "# [tool.griptape-nodes]",
        '# name = "Template"',
        '# schema_version = "0.21.0"',
        '# engine_version_created_with = "0.98.0"',
        "# node_libraries_referenced = []",
    ]
    if description is not None:
        lines.append(f'# description = "{description}"')
    if image is not None:
        lines.append(f'# image = "{image}"')
    lines.append("# ///")
    _write(root, relative, "\n".join(lines) + "\n")


class TestMatchingPagesToNodes:
    def test_page_named_after_class_name(self, tmp_path: Path) -> None:
        _write(tmp_path, "docs/nodes/LoadAudio.md")
        docs = LibraryDocs(tmp_path, _schema(nodes=[_node("LoadAudio", "Load Audio")]))

        assert docs.node_page("LoadAudio") == (tmp_path / "docs/nodes/LoadAudio.md").resolve()
        assert docs.problems == []

    @pytest.mark.parametrize("filename", ["pipeline_builder.md", "pipeline-builder.md", "Pipeline Builder.md"])
    def test_page_named_after_display_name(self, tmp_path: Path, filename: str) -> None:
        _write(tmp_path, f"docs/nodes/{filename}")
        docs = LibraryDocs(tmp_path, _schema(nodes=[_node("LatentDiffusionPipelineBuilderNode", "Pipeline Builder")]))

        assert docs.node_page("LatentDiffusionPipelineBuilderNode") is not None
        assert docs.problems == []

    def test_pages_in_subfolders_match(self, tmp_path: Path) -> None:
        _write(tmp_path, "docs/nodes/audio/LoadAudio.md")
        docs = LibraryDocs(tmp_path, _schema(nodes=[_node("LoadAudio", "Load Audio")]))

        assert docs.node_page("LoadAudio") is not None

    def test_workflow_node_pages_live_in_their_own_folder(self, tmp_path: Path) -> None:
        _write(tmp_path, "docs/workflow_nodes/ShoutText.md")
        _write(tmp_path, "docs/nodes/ShoutText.md")
        docs = LibraryDocs(tmp_path, _schema(workflow_nodes=[_workflow_node("ShoutText", "Shout Text")]))

        assert docs.node_page("ShoutText") == (tmp_path / "docs/workflow_nodes/ShoutText.md").resolve()
        assert [problem.kind for problem in docs.problems] == [DocsProblemKind.ORPHAN_PAGE]

    def test_template_pages_match_the_file_name_without_extension(self, tmp_path: Path) -> None:
        _write(tmp_path, "docs/workflows/ControlnetText2Image.md")
        docs = LibraryDocs(tmp_path, _schema(workflows=["workflows/templates/ControlnetText2Image.py"]))

        assert docs.template_page("ControlnetText2Image") is not None
        assert docs.problems == []

    def test_class_name_wins_over_a_display_name_that_looks_the_same(self, tmp_path: Path) -> None:
        _write(tmp_path, "docs/nodes/MixAudio.md")
        nodes = [_node("MixAudio", "Mix Audio"), _node("AudioMixer", "MixAudio")]
        docs = LibraryDocs(tmp_path, _schema(nodes=nodes))

        assert docs.node_page("MixAudio") is not None
        assert docs.node_page("AudioMixer") is None

    def test_locale_variants_are_ignored_for_now(self, tmp_path: Path) -> None:
        _write(tmp_path, "docs/nodes/LoadAudio.md")
        _write(tmp_path, "docs/nodes/LoadAudio.fr.md")
        docs = LibraryDocs(tmp_path, _schema(nodes=[_node("LoadAudio", "Load Audio")]))

        assert docs.node_page("LoadAudio") == (tmp_path / "docs/nodes/LoadAudio.md").resolve()
        assert docs.problems == []


class TestProblems:
    def test_a_page_that_matches_nothing_is_an_orphan(self, tmp_path: Path) -> None:
        _write(tmp_path, "docs/nodes/Nothing.md")
        docs = LibraryDocs(tmp_path, _schema(nodes=[_node("LoadAudio", "Load Audio")]))

        assert [problem.kind for problem in docs.problems] == [DocsProblemKind.ORPHAN_PAGE]
        assert docs.problems[0].paths == ["nodes/Nothing.md"]

    def test_a_class_name_that_differs_only_by_case_is_reported(self, tmp_path: Path) -> None:
        _write(tmp_path, "docs/nodes/loadaudio.md")
        docs = LibraryDocs(tmp_path, _schema(nodes=[_node("LoadAudio", "Load Audio")]))

        assert docs.node_page("LoadAudio") is None
        assert [problem.kind for problem in docs.problems] == [DocsProblemKind.NEAR_MISS_NAME]
        assert "LoadAudio.md" in docs.problems[0].message

    def test_two_pages_for_one_node_use_the_one_nearest_the_top(self, tmp_path: Path) -> None:
        _write(tmp_path, "docs/nodes/legacy/old/LoadAudio.md")
        _write(tmp_path, "docs/nodes/audio/LoadAudio.md")
        _write(tmp_path, "docs/nodes/LoadAudio.md")
        docs = LibraryDocs(tmp_path, _schema(nodes=[_node("LoadAudio", "Load Audio")]))

        assert docs.node_page("LoadAudio") == (tmp_path / "docs/nodes/LoadAudio.md").resolve()
        assert [problem.kind for problem in docs.problems] == [DocsProblemKind.CONFLICTING_PAGES]
        assert len(docs.problems[0].paths) == CONFLICTING_PAGE_COUNT

    def test_equal_depth_goes_to_the_first_path_alphabetically(self, tmp_path: Path) -> None:
        _write(tmp_path, "docs/nodes/b/LoadAudio.md")
        _write(tmp_path, "docs/nodes/a/LoadAudio.md")
        docs = LibraryDocs(tmp_path, _schema(nodes=[_node("LoadAudio", "Load Audio")]))

        assert docs.node_page("LoadAudio") == (tmp_path / "docs/nodes/a/LoadAudio.md").resolve()

    def test_a_display_name_two_nodes_share_is_ambiguous(self, tmp_path: Path) -> None:
        _write(tmp_path, "docs/nodes/load.md")
        nodes = [_node("LoadAudio", "Load"), _node("LoadImage", "Load")]
        docs = LibraryDocs(tmp_path, _schema(nodes=nodes))

        assert docs.node_page("LoadAudio") is None
        assert docs.node_page("LoadImage") is None
        assert [problem.kind for problem in docs.problems] == [DocsProblemKind.AMBIGUOUS_DISPLAY_NAME]

    def test_pages_in_hidden_folders_under_a_reserved_folder_are_skipped(self, tmp_path: Path) -> None:
        _write(tmp_path, "docs/nodes/.drafts/LoadAudio.md")
        docs = LibraryDocs(tmp_path, _schema(nodes=[_node("LoadAudio", "Load Audio")]))

        assert docs.node_page("LoadAudio") is None
        assert docs.problems == []


class TestListings:
    def test_node_entries_use_the_page_when_there_is_one_and_the_manifest_when_there_is_not(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "docs/nodes/LoadAudio.md",
            "---\ntitle: Bring in audio\ndescription: Loads a sound file.\nwhen_to_use: You have a recording.\n---\nText\n",
        )
        docs = LibraryDocs(tmp_path, _schema(nodes=[_node("LoadAudio", "Load Audio"), _node("MixAudio", "Mix Audio")]))

        documented, undocumented = docs.node_entries()

        assert documented.title == "Bring in audio"
        assert documented.description == "Loads a sound file."
        assert documented.when_to_use == "You have a recording."
        assert documented.has_page is True
        assert documented.page_path == "nodes/LoadAudio.md"
        assert undocumented.page_path is None
        assert undocumented.title == "Mix Audio"
        assert undocumented.description == "Mix Audio from the manifest"
        assert undocumented.has_page is False
        assert undocumented.category == "Audio"

    def test_workflow_nodes_are_listed_with_the_nodes(self, tmp_path: Path) -> None:
        docs = LibraryDocs(
            tmp_path,
            _schema(
                nodes=[_node("LoadAudio", "Load Audio")], workflow_nodes=[_workflow_node("ShoutText", "Shout Text")]
            ),
        )

        assert [entry.node_type for entry in docs.node_entries()] == ["LoadAudio", "ShoutText"]

    def test_a_workflow_node_is_marked_apart_from_a_node(self, tmp_path: Path) -> None:
        docs = LibraryDocs(
            tmp_path,
            _schema(
                nodes=[_node("LoadAudio", "Load Audio")], workflow_nodes=[_workflow_node("ShoutText", "Shout Text")]
            ),
        )

        kinds = {entry.node_type: entry.kind for entry in docs.node_entries()}

        assert kinds == {"LoadAudio": DocumentationNodeKind.NODE, "ShoutText": DocumentationNodeKind.WORKFLOW_NODE}

    def test_template_entries(self, tmp_path: Path) -> None:
        _write(tmp_path, "docs/workflows/Text2Image.md", "---\ntitle: Text to image\n---\nText\n")
        docs = LibraryDocs(
            tmp_path, _schema(workflows=["workflows/templates/Text2Image.py", "workflows/templates/Other.py"])
        )

        documented, undocumented = docs.template_entries()

        assert (documented.name, documented.title, documented.has_page) == ("Text2Image", "Text to image", True)
        assert documented.page_path == "workflows/Text2Image.md"
        assert undocumented.page_path is None
        assert (undocumented.name, undocumented.title, undocumented.has_page) == ("Other", "Other", False)

    def test_a_template_without_a_page_takes_its_description_and_thumbnail_from_its_own_metadata(
        self, tmp_path: Path
    ) -> None:
        _write_template(tmp_path, "workflows/templates/Poster.py", description="Makes a poster.", image="poster.png")
        (tmp_path / "workflows/templates/poster.png").write_bytes(b"png")
        docs = LibraryDocs(tmp_path, _schema(workflows=["workflows/templates/Poster.py"]))

        (entry,) = docs.template_entries()

        assert entry.description == "Makes a poster."
        assert entry.image == str((tmp_path / "workflows/templates/poster.png").resolve())

    def test_a_pages_description_wins_over_the_templates_metadata(self, tmp_path: Path) -> None:
        _write_template(tmp_path, "workflows/templates/Poster.py", description="From the file.")
        _write(tmp_path, "docs/workflows/Poster.md", "---\ndescription: From the page.\n---\nText\n")
        docs = LibraryDocs(tmp_path, _schema(workflows=["workflows/templates/Poster.py"]))

        (entry,) = docs.template_entries()

        assert entry.description == "From the page."

    def test_a_thumbnail_that_is_a_url_is_kept_and_one_that_is_missing_is_dropped(self, tmp_path: Path) -> None:
        _write_template(tmp_path, "workflows/templates/Web.py", image="https://example.com/a.png")
        _write_template(tmp_path, "workflows/templates/Gone.py", image="missing.png")
        docs = LibraryDocs(tmp_path, _schema(workflows=["workflows/templates/Web.py", "workflows/templates/Gone.py"]))

        web, gone = docs.template_entries()

        assert web.image == "https://example.com/a.png"
        assert gone.image is None

    def test_a_template_file_with_no_readable_header_is_still_listed(self, tmp_path: Path) -> None:
        (tmp_path / "workflows/templates").mkdir(parents=True)
        (tmp_path / "workflows/templates/Broken.py").write_text("print('hi')\n", encoding="utf-8")
        docs = LibraryDocs(tmp_path, _schema(workflows=["workflows/templates/Broken.py"]))

        (entry,) = docs.template_entries()

        assert (entry.name, entry.description, entry.image) == ("Broken", None, None)

    def test_library_with_no_docs_folder_still_lists_its_nodes(self, tmp_path: Path) -> None:
        docs = LibraryDocs(tmp_path, _schema(nodes=[_node("LoadAudio", "Load Audio")]))

        assert docs.docs_root is None
        assert docs.page_entries() == []
        assert docs.overview_path() is None
        assert docs.skill_entries() == []
        assert [entry.has_page for entry in docs.node_entries()] == [False]


class TestToolPages:
    @staticmethod
    def _tool(name: str, description: str = "Generated.") -> DocumentationToolEntry:
        return DocumentationToolEntry(name=name, description=description, read_only=True)

    def test_a_tool_page_is_matched_by_request_type_and_overrides_title_and_description(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "docs/mcp_tools/SearchDocsRequest.md",
            "---\ntitle: Search the docs\ndescription: Finds pages.\n---\nBody\n",
        )
        docs = LibraryDocs(tmp_path, _schema(), ["SearchDocsRequest", "Other"])

        documented, undocumented = docs.tool_entries([self._tool("SearchDocsRequest"), self._tool("Other")])

        assert (documented.title, documented.description, documented.has_page) == (
            "Search the docs",
            "Finds pages.",
            True,
        )
        assert documented.page_path == "mcp_tools/SearchDocsRequest.md"
        assert (undocumented.title, undocumented.description, undocumented.has_page) == ("Other", "Generated.", False)
        assert undocumented.page_path is None

    def test_a_tool_page_that_matches_no_tool_is_reported_and_not_a_library_page(self, tmp_path: Path) -> None:
        _write(tmp_path, "docs/mcp_tools/Gone.md", "Text\n")
        docs = LibraryDocs(tmp_path, _schema(), ["SearchDocsRequest"])

        assert [problem.kind for problem in docs.problems] == [DocsProblemKind.ORPHAN_PAGE]
        assert docs.page_entries() == []

    def test_a_folder_called_tools_is_an_ordinary_page_folder(self, tmp_path: Path) -> None:
        _write(tmp_path, "docs/tools/preparing-audio.md", "Text\n")
        docs = LibraryDocs(tmp_path, _schema(), ["SearchDocsRequest"])

        assert [(entry.kind, entry.path) for entry in docs.page_entries()] == [
            (DocumentationEntryKind.FOLDER, "tools"),
            (DocumentationEntryKind.PAGE, "tools/preparing-audio.md"),
        ]
        assert docs.problems == []

    def test_a_library_with_no_docs_folder_still_lists_its_tools(self, tmp_path: Path) -> None:
        docs = LibraryDocs(tmp_path, _schema(), ["SearchDocsRequest"])

        (entry,) = docs.tool_entries([self._tool("SearchDocsRequest")])

        assert (entry.title, entry.has_page) == ("SearchDocsRequest", False)


class TestPageTree:
    def test_tree_follows_folders_in_display_order(self, tmp_path: Path) -> None:
        _write(tmp_path, "docs/index.md", "# Overview\n")
        _write(tmp_path, "docs/02-reference/formats.md", "# File formats\n")
        _write(tmp_path, "docs/01-guides/getting-started.md", "---\ntitle: Start here\n---\n")
        _write(tmp_path, "docs/01-guides/color_management.md")
        _write(tmp_path, "docs/about.md")
        docs = LibraryDocs(tmp_path, _schema())

        entries = [(entry.kind, entry.path, entry.title) for entry in docs.page_entries()]

        assert entries == [
            (DocumentationEntryKind.PAGE, "index.md", "Overview"),
            (DocumentationEntryKind.FOLDER, "01-guides", "Guides"),
            (DocumentationEntryKind.PAGE, "01-guides/color_management.md", "color_management"),
            (DocumentationEntryKind.PAGE, "01-guides/getting-started.md", "Start here"),
            (DocumentationEntryKind.FOLDER, "02-reference", "Reference"),
            (DocumentationEntryKind.PAGE, "02-reference/formats.md", "File formats"),
            (DocumentationEntryKind.PAGE, "about.md", "about"),
        ]

    def test_node_pages_template_pages_skills_and_hidden_folders_are_not_library_pages(self, tmp_path: Path) -> None:
        _write(tmp_path, "docs/guide.md")
        _write(tmp_path, "docs/nodes/LoadAudio.md")
        _write(tmp_path, "docs/workflow_nodes/ShoutText.md")
        _write(tmp_path, "docs/workflows/Text2Image.md")
        _write(tmp_path, "docs/.agents/skills/color/SKILL.md")
        _write(tmp_path, "docs/.hidden/notes.md")
        docs = LibraryDocs(tmp_path, _schema())

        assert [entry.path for entry in docs.page_entries()] == ["guide.md"]

    def test_a_nested_folder_named_nodes_is_an_ordinary_folder(self, tmp_path: Path) -> None:
        _write(tmp_path, "docs/guides/nodes/overview.md", "# Overview\n")
        docs = LibraryDocs(tmp_path, _schema())

        assert [entry.path for entry in docs.page_entries()] == ["guides", "guides/nodes", "guides/nodes/overview.md"]

    def test_folders_with_no_pages_are_left_out(self, tmp_path: Path) -> None:
        (tmp_path / "docs/empty").mkdir(parents=True)
        _write(tmp_path, "docs/assets/screenshot.png", "not markdown")
        docs = LibraryDocs(tmp_path, _schema())

        assert docs.page_entries() == []

    def test_page_descriptions_come_from_frontmatter(self, tmp_path: Path) -> None:
        _write(tmp_path, "docs/guide.md", "---\ndescription: How to start.\nwhen_to_use: First.\n---\n# Guide\n")
        docs = LibraryDocs(tmp_path, _schema())

        entry = docs.page_entries()[0]

        assert (entry.description, entry.when_to_use) == ("How to start.", "First.")


class TestOverviewAndSkills:
    def test_index_page_is_the_overview(self, tmp_path: Path) -> None:
        _write(tmp_path, "docs/index.md", "# Home\n")
        _write(tmp_path, "README.md", "# Readme\n")
        docs = LibraryDocs(tmp_path, _schema())

        assert docs.overview_path() == "index.md"
        assert docs.read_page("index.md").markdown == "# Home\n"

    def test_readme_stands_in_when_there_is_no_index_page(self, tmp_path: Path) -> None:
        (tmp_path / "docs").mkdir()
        _write(tmp_path, "README.md", "# Readme\n")
        docs = LibraryDocs(tmp_path, _schema())

        assert docs.overview_path() == "index.md"
        assert docs.read_page("index.md").markdown == "# Readme\n"

    def test_a_readme_overview_says_it_is_a_readme_and_an_index_page_does_not(self, tmp_path: Path) -> None:
        (tmp_path / "docs").mkdir()
        _write(tmp_path, "README.md", "# Readme\n")
        assert LibraryDocs(tmp_path, _schema()).overview_is_readme() is True

        _write(tmp_path, "docs/index.md", "# Index\n")
        with_index = LibraryDocs(tmp_path, _schema())
        assert with_index.overview_is_readme() is False
        assert with_index.read_page("index.md").markdown == "# Index\n"

    def test_a_library_with_no_overview_says_it_is_not_a_readme(self, tmp_path: Path) -> None:
        (tmp_path / "docs").mkdir()

        docs = LibraryDocs(tmp_path, _schema())

        assert (docs.overview_path(), docs.overview_is_readme()) == (None, False)

    def test_readme_stands_in_even_when_the_library_has_no_docs_folder(self, tmp_path: Path) -> None:
        _write(tmp_path, "README.md", "# Readme\n")
        docs = LibraryDocs(tmp_path, _schema())

        assert docs.docs_root is None
        assert docs.overview_path() == "index.md"
        assert docs.overview_is_readme() is True
        assert docs.read_page("index.md").markdown == "# Readme\n"

    def test_other_pages_are_still_missing_when_the_library_has_no_docs_folder(self, tmp_path: Path) -> None:
        _write(tmp_path, "README.md", "# Readme\n")
        docs = LibraryDocs(tmp_path, _schema())

        with pytest.raises(DocsPageError, match="no docs folder"):
            docs.read_page("guides/start.md")

    def test_a_readme_inside_the_docs_folder_is_an_ordinary_page_and_not_the_overview(self, tmp_path: Path) -> None:
        _write(tmp_path, "docs/README.md", "# Docs readme\n")
        docs = LibraryDocs(tmp_path, _schema())

        assert docs.overview_path() is None
        assert [entry.path for entry in docs.page_entries()] == ["README.md"]

    def test_a_readmes_images_resolve_against_the_folder_it_sits_in(self, tmp_path: Path) -> None:
        (tmp_path / "docs").mkdir()
        _write(tmp_path, "README.md", "![](images/shot.png)\n")
        docs = LibraryDocs(tmp_path, _schema())

        assert docs.assets_root("index.md") == tmp_path

    def test_an_index_pages_images_resolve_against_the_docs_folder(self, tmp_path: Path) -> None:
        _write(tmp_path, "docs/index.md", "![](images/shot.png)\n")
        _write(tmp_path, "README.md", "# Readme\n")
        docs = LibraryDocs(tmp_path, _schema())

        assert docs.assets_root("index.md") == docs.docs_root
        assert docs.assets_root("guides/start.md") == docs.docs_root

    def test_readme_one_folder_up_stands_in_for_a_manifest_in_a_package_folder(self, tmp_path: Path) -> None:
        package = tmp_path / "my_library"
        (package / "docs").mkdir(parents=True)
        _write(tmp_path, "README.md", "# Repo readme\n")
        docs = LibraryDocs(package, _schema())

        assert docs.read_page("index.md").markdown == "# Repo readme\n"

    def test_no_overview_when_there_is_neither(self, tmp_path: Path) -> None:
        (tmp_path / "docs").mkdir()
        docs = LibraryDocs(tmp_path, _schema())

        assert docs.overview_path() is None

    def test_skills_are_listed_with_their_descriptions(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "docs/.agents/skills/color-managed-workflow/SKILL.md",
            "---\nname: color-managed-workflow\ndescription: Use for color work.\n---\nSteps\n",
        )
        _write(tmp_path, "docs/.agents/skills/no-frontmatter/SKILL.md", "Steps\n")
        _write(tmp_path, "docs/.agents/skills/not-a-skill/notes.md")
        docs = LibraryDocs(tmp_path, _schema())

        skills = docs.skill_entries()

        assert [(skill.name, skill.description, skill.path) for skill in skills] == [
            ("color-managed-workflow", "Use for color work.", ".agents/skills/color-managed-workflow/SKILL.md"),
            ("no-frontmatter", "", ".agents/skills/no-frontmatter/SKILL.md"),
        ]
        assert "Steps" in docs.read_page(skills[0].path).markdown

    def test_skills_next_to_the_manifest_are_not_the_user_skills(self, tmp_path: Path) -> None:
        _write(tmp_path, ".agents/skills/run-the-tests/SKILL.md", "---\nname: run-the-tests\n---\n")
        (tmp_path / "docs").mkdir()
        docs = LibraryDocs(tmp_path, _schema())

        assert docs.skill_entries() == []


class TestDocsLocation:
    def test_documentation_path_can_reach_a_folder_beside_the_manifest_folder(self, tmp_path: Path) -> None:
        package = tmp_path / "my_library"
        package.mkdir()
        _write(tmp_path, "docs/nodes/LoadAudio.md")
        docs = LibraryDocs(package, _schema(nodes=[_node("LoadAudio", "Load Audio")], documentation_path="../docs"))

        assert docs.docs_root == (tmp_path / "docs").resolve()
        assert docs.node_page("LoadAudio") is not None

    def test_docs_folder_defaults_to_docs_next_to_the_manifest(self, tmp_path: Path) -> None:
        (tmp_path / "docs").mkdir()
        docs = LibraryDocs(tmp_path, _schema())

        assert docs.docs_root == (tmp_path / "docs").resolve()


class TestReadingPages:
    def test_frontmatter_is_removed_from_the_text(self, tmp_path: Path) -> None:
        _write(tmp_path, "docs/guide.md", "---\ntitle: Guide\n---\n\n# Heading\n\nBody.\n")
        docs = LibraryDocs(tmp_path, _schema())

        page = docs.read_page("guide.md")

        assert page.markdown == "# Heading\n\nBody.\n"
        assert page.frontmatter_title == "Guide"

    @pytest.mark.parametrize("path", ["../secret.md", "guides/../../secret.md", "/etc/passwd.md", ""])
    def test_a_path_that_leaves_the_docs_folder_is_refused(self, tmp_path: Path, path: str) -> None:
        _write(tmp_path, "secret.md", "private")
        (tmp_path / "docs").mkdir()
        docs = LibraryDocs(tmp_path, _schema())

        with pytest.raises(DocsPageError, match="docs folder"):
            docs.read_page(path)

    def test_a_link_pointing_outside_the_docs_folder_is_refused(self, tmp_path: Path) -> None:
        _write(tmp_path, "secret.md", "private")
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs/escape.md").symlink_to(tmp_path / "secret.md")
        docs = LibraryDocs(tmp_path, _schema())

        with pytest.raises(DocsPageError, match="outside"):
            docs.read_page("escape.md")

    def test_a_link_out_of_the_docs_folder_is_left_out_of_the_listing_and_reported(self, tmp_path: Path) -> None:
        _write(tmp_path, "secret.md", "private")
        _write(tmp_path, "docs/guide.md", "Text\n")
        (tmp_path / "docs/escape.md").symlink_to(tmp_path / "secret.md")
        docs = LibraryDocs(tmp_path, _schema())

        assert [entry.path for entry in docs.page_entries()] == ["guide.md"]
        assert [(problem.kind, problem.paths) for problem in docs.problems] == [
            (DocsProblemKind.LINK_OUTSIDE_DOCS, ["escape.md"])
        ]

    def test_a_linked_folder_out_of_the_docs_folder_is_left_out(self, tmp_path: Path) -> None:
        _write(tmp_path, "elsewhere/page.md", "private")
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs/linked").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
        docs = LibraryDocs(tmp_path, _schema())

        assert docs.page_entries() == []
        assert [problem.kind for problem in docs.problems] == [DocsProblemKind.LINK_OUTSIDE_DOCS]

    def test_a_link_that_stays_inside_the_docs_folder_is_fine(self, tmp_path: Path) -> None:
        _write(tmp_path, "docs/real.md", "Text\n")
        (tmp_path / "docs/alias.md").symlink_to(tmp_path / "docs/real.md")
        docs = LibraryDocs(tmp_path, _schema())

        assert [entry.path for entry in docs.page_entries()] == ["alias.md", "real.md"]
        assert docs.problems == []

    def test_a_node_page_that_is_a_link_out_of_the_docs_folder_is_not_read(self, tmp_path: Path) -> None:
        _write(tmp_path, "secret.md", "---\ntitle: Stolen\n---\nprivate")
        (tmp_path / "docs/nodes").mkdir(parents=True)
        (tmp_path / "docs/nodes/LoadAudio.md").symlink_to(tmp_path / "secret.md")
        docs = LibraryDocs(tmp_path, _schema(nodes=[_node("LoadAudio", "Load Audio")]))

        (entry,) = docs.node_entries()

        assert (entry.has_page, entry.title) == (False, "Load Audio")
        assert docs.node_page("LoadAudio") is None

    def test_a_skill_that_is_a_link_out_of_the_docs_folder_is_not_listed(self, tmp_path: Path) -> None:
        _write(tmp_path, "secret/SKILL.md", "---\ndescription: Stolen.\n---\n")
        (tmp_path / "docs/.agents/skills").mkdir(parents=True)
        (tmp_path / "docs/.agents/skills/stolen").symlink_to(tmp_path / "secret", target_is_directory=True)
        docs = LibraryDocs(tmp_path, _schema())

        assert docs.skill_entries() == []
        assert [problem.kind for problem in docs.problems] == [DocsProblemKind.LINK_OUTSIDE_DOCS]

    def test_only_markdown_pages_can_be_read(self, tmp_path: Path) -> None:
        _write(tmp_path, "docs/notes.txt")
        docs = LibraryDocs(tmp_path, _schema())

        with pytest.raises(DocsPageError, match=r"\.md"):
            docs.read_page("notes.txt")

    def test_a_missing_page_is_reported(self, tmp_path: Path) -> None:
        (tmp_path / "docs").mkdir()
        docs = LibraryDocs(tmp_path, _schema())

        with pytest.raises(DocsPageError, match="no page at that path"):
            docs.read_page("missing.md")

    def test_a_library_with_no_docs_folder_has_no_pages_to_read(self, tmp_path: Path) -> None:
        docs = LibraryDocs(tmp_path, _schema())

        with pytest.raises(DocsPageError, match="no docs folder"):
            docs.read_page("index.md")

    def test_a_page_over_the_size_limit_is_refused(self, tmp_path: Path) -> None:
        _write(tmp_path, "docs/big.md", "x" * (MAX_PAGE_BYTES + 1))
        docs = LibraryDocs(tmp_path, _schema())

        with pytest.raises(DocsPageError, match="1 MB"):
            docs.read_page("big.md")

    def test_a_page_that_is_not_text_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs/binary.md").write_bytes(b"\xff\xfe\x00")
        docs = LibraryDocs(tmp_path, _schema())

        with pytest.raises(DocsPageError, match="as text"):
            docs.read_page("binary.md")


class TestParsePage:
    def test_page_without_frontmatter(self) -> None:
        page = parse_page("# Title\n\nBody\n")

        assert page.markdown == "# Title\n\nBody\n"
        assert page.frontmatter_title is None
        assert page.first_heading == "Title"

    def test_the_first_heading_skips_code_blocks(self) -> None:
        page = parse_page("```bash\n# not a heading\n```\n\n# Real heading\n")

        assert page.first_heading == "Real heading"

    def test_unreadable_frontmatter_is_reported_and_the_text_is_kept(self) -> None:
        page = parse_page("---\ntitle: [unclosed\n---\nBody\n")

        assert page.frontmatter_error is not None
        assert page.markdown == "Body\n"
        assert page.frontmatter_title is None

    def test_frontmatter_that_is_not_key_value_lines_is_reported(self) -> None:
        page = parse_page("---\n- one\n- two\n---\nBody\n")

        assert page.frontmatter_error is not None
        assert page.markdown == "Body\n"

    def test_unknown_frontmatter_keys_are_ignored(self) -> None:
        page = parse_page("---\ntitle: Hi\nfuture_field: whatever\n---\nBody\n")

        assert page.frontmatter_title == "Hi"
        assert page.frontmatter_error is None

    def test_a_thematic_break_is_not_frontmatter(self) -> None:
        page = parse_page("Intro\n\n---\n\nMore\n")

        assert page.markdown == "Intro\n\n---\n\nMore\n"


class TestListingsAreReadOnce:
    def test_a_page_added_after_the_first_listing_waits_for_the_library_to_load_again(self, tmp_path: Path) -> None:
        _write(tmp_path, "docs/first.md", "# First\n")
        docs = LibraryDocs(tmp_path, _schema())
        assert [entry.path for entry in docs.page_entries()] == ["first.md"]

        _write(tmp_path, "docs/second.md", "# Second\n")

        assert [entry.path for entry in docs.page_entries()] == ["first.md"]
        assert [entry.path for entry in LibraryDocs(tmp_path, _schema()).page_entries()] == ["first.md", "second.md"]

    def test_an_edited_title_waits_for_the_library_to_load_again_but_page_text_does_not(self, tmp_path: Path) -> None:
        _write(tmp_path, "docs/guide.md", "---\ntitle: Old title\n---\nOld text\n")
        docs = LibraryDocs(tmp_path, _schema())
        assert docs.page_entries()[0].title == "Old title"

        _write(tmp_path, "docs/guide.md", "---\ntitle: New title\n---\nNew text\n")

        assert docs.page_entries()[0].title == "Old title"
        assert docs.read_page("guide.md").markdown == "New text\n"

    def test_changing_a_returned_list_does_not_change_the_next_one(self, tmp_path: Path) -> None:
        _write(tmp_path, "docs/guide.md", "# Guide\n")
        docs = LibraryDocs(tmp_path, _schema(nodes=[_node("LoadAudio", "Load Audio")]))

        docs.page_entries().clear()
        docs.node_entries().clear()

        assert len(docs.page_entries()) == 1
        assert len(docs.node_entries()) == 1


class TestNodePagePaths:
    def test_page_path_is_relative_to_the_docs_folder_wherever_the_page_sits(self, tmp_path: Path) -> None:
        _write(tmp_path, "docs/nodes/audio/LoadAudio.md")
        _write(tmp_path, "docs/nodes/mix_audio.md")
        _write(tmp_path, "docs/workflow_nodes/ShoutText.md")
        docs = LibraryDocs(
            tmp_path,
            _schema(
                nodes=[_node("LoadAudio", "Load Audio"), _node("MixAudio", "Mix Audio")],
                workflow_nodes=[_workflow_node("ShoutText", "Shout Text")],
            ),
        )

        paths = {entry.node_type: entry.page_path for entry in docs.node_entries()}

        assert paths == {
            "LoadAudio": "nodes/audio/LoadAudio.md",
            "MixAudio": "nodes/mix_audio.md",
            "ShoutText": "workflow_nodes/ShoutText.md",
        }

    def test_a_page_path_can_be_read_back_with_read_page(self, tmp_path: Path) -> None:
        _write(tmp_path, "docs/nodes/audio/LoadAudio.md", "Body.\n")
        docs = LibraryDocs(tmp_path, _schema(nodes=[_node("LoadAudio", "Load Audio")]))

        page_path = docs.node_entries()[0].page_path

        assert page_path is not None
        assert docs.read_page(page_path).markdown == "Body.\n"
