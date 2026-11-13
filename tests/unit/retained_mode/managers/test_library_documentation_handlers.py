"""The requests that read the docs a library ships, against a library registered from a temporary folder."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import BaseNode, DataNode
from griptape_nodes.node_library.library_registry import (
    LibraryMetadata,
    LibraryRegistry,
    LibrarySchema,
    NodeDefinition,
    NodeDeprecationMetadata,
    NodeMetadata,
    WorkflowNodeDefinition,
)
from griptape_nodes.retained_mode.events.library_documentation_events import (
    GetLibraryDocumentationPageRequest,
    GetLibraryDocumentationPageResultFailure,
    GetLibraryDocumentationPageResultSuccess,
    GetNodeDocumentationRequest,
    GetNodeDocumentationResultFailure,
    GetNodeDocumentationResultSuccess,
    ListLibraryDocumentationRequest,
    ListLibraryDocumentationResultFailure,
    ListLibraryDocumentationResultSuccess,
)
from griptape_nodes.retained_mode.events.library_events import (
    DescribeNodeTypeResultFailure,
    DescribeNodeTypeResultSuccess,
)
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine

LIBRARY_NAME = "Docs Test Library"
NODE_COUNT = 3
AUDIO_METADATA = NodeMetadata(
    category="Audio",
    description="Loads a sound file from the manifest.",
    display_name="Load Audio",
    deprecation=NodeDeprecationMetadata(deprecation_message="Use Load Sound instead.", removal_version="2.0.0"),
)


def _schema() -> LibrarySchema:
    return LibrarySchema(
        name=LIBRARY_NAME,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="test", description="test", library_version="0.1.0", engine_version="0.98.0", tags=[]
        ),
        categories=[],
        nodes=[
            NodeDefinition(class_name="LoadAudio", file_path="nodes/load_audio.py", metadata=AUDIO_METADATA),
            NodeDefinition(
                class_name="MixAudio",
                file_path="nodes/mix_audio.py",
                metadata=NodeMetadata(category="Audio", description="Mixes two sounds.", display_name="Mix Audio"),
            ),
        ],
        workflow_nodes=[
            WorkflowNodeDefinition(
                node_type="ShoutText",
                workflow_path="workflows/shout_text.py",
                metadata=NodeMetadata(category="Text", description="Shouts.", display_name="Shout Text"),
            )
        ],
        workflows=["workflows/templates/Text2Image.py"],
    )


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean_registry() -> Generator[None, None, None]:
    LibraryRegistry._clear()
    yield
    LibraryRegistry._clear()


@pytest.fixture
def library_root(engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A registered library whose manifest sits in a temporary folder."""
    root = tmp_path / "docs_test_library"
    root.mkdir()
    manifest = root / "griptape_nodes_library.json"
    manifest.write_text("{}", encoding="utf-8")

    LibraryRegistry.generate_new_library(library_data=_schema())
    info = LibraryManager.LibraryInfo(
        lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
        fitness=LibraryManager.LibraryFitness.GOOD,
        library_path=str(manifest),
        is_sandbox=False,
        library_name=LIBRARY_NAME,
        library_version="0.1.0",
    )
    monkeypatch.setitem(engine.library_manager._library_file_path_to_info, str(manifest), info)
    return root


def _describe_success(node_type: str, metadata: NodeMetadata) -> DescribeNodeTypeResultSuccess:
    return DescribeNodeTypeResultSuccess(
        library=LIBRARY_NAME, node_type=node_type, metadata=metadata, parameters=[], result_details="described"
    )


class _DynamicNode(DataNode):
    """A node whose parameters depend on the instance: `with_extra` adds one the type does not declare."""

    def __init__(self, name: str, metadata: dict | None = None, *, with_extra: bool = False) -> None:
        super().__init__(name, metadata=metadata)
        modes = {ParameterMode.INPUT, ParameterMode.PROPERTY, ParameterMode.OUTPUT}
        self.add_parameter(Parameter(name="text", tooltip="Text", type="str", default_value="", allowed_modes=modes))
        if with_extra:
            self.add_parameter(
                Parameter(name="extra", tooltip="Extra", type="str", default_value="", allowed_modes=modes)
            )


def _canvas_node(name: str, *, with_extra: bool = False) -> _DynamicNode:
    with LibraryRegistry.constructing_node(throwaway=True):
        return _DynamicNode(name, metadata={"node_type": "LoadAudio", "library": LIBRARY_NAME}, with_extra=with_extra)


def _workflow_with(*nodes: BaseNode) -> Any:
    """Stands in for the open workflow: finds a node on the canvas by name."""
    by_name = {node.name: node for node in nodes}

    def get_as_type(name: str, cast_type: type) -> Any:
        found = by_name.get(name)
        if isinstance(found, cast_type):
            return found
        return None

    return SimpleNamespace(objects=SimpleNamespace(get_as_type=get_as_type))


class TestListLibraryDocumentation:
    def test_lists_pages_nodes_templates_and_skills(self, engine: Engine, library_root: Path) -> None:
        _write(library_root, "docs/index.md", "# Welcome\n")
        _write(library_root, "docs/guides/getting-started.md", "# Getting started\n")
        _write(library_root, "docs/nodes/LoadAudio.md", "---\ntitle: Bring in audio\n---\nText\n")
        _write(library_root, "docs/workflows/Text2Image.md", "Text\n")
        _write(library_root, "docs/.agents/skills/color/SKILL.md", "---\ndescription: Color work.\n---\nSteps\n")

        result = engine.library_manager.documentation.list_library_documentation_request(
            ListLibraryDocumentationRequest(library=LIBRARY_NAME)
        )

        assert isinstance(result, ListLibraryDocumentationResultSuccess)
        assert result.overview_path == "index.md"
        assert [entry.path for entry in result.pages] == ["index.md", "guides", "guides/getting-started.md"]
        assert [(entry.node_type, entry.title, entry.has_page) for entry in result.nodes] == [
            ("LoadAudio", "Bring in audio", True),
            ("MixAudio", "Mix Audio", False),
            ("ShoutText", "Shout Text", False),
        ]
        assert [(entry.name, entry.has_page) for entry in result.templates] == [("Text2Image", True)]
        assert [(skill.name, skill.description) for skill in result.skills] == [("color", "Color work.")]

    @pytest.mark.usefixtures("library_root")
    def test_a_library_without_docs_still_lists_its_nodes(self, engine: Engine) -> None:
        result = engine.library_manager.documentation.list_library_documentation_request(
            ListLibraryDocumentationRequest(library=LIBRARY_NAME)
        )

        assert isinstance(result, ListLibraryDocumentationResultSuccess)
        assert result.overview_path is None
        assert result.pages == []
        assert len(result.nodes) == NODE_COUNT
        assert not any(entry.has_page for entry in result.nodes)

    def test_unknown_library_fails(self, engine: Engine) -> None:
        result = engine.library_manager.documentation.list_library_documentation_request(
            ListLibraryDocumentationRequest(library="No Such Library")
        )

        assert isinstance(result, ListLibraryDocumentationResultFailure)
        assert "No Such Library" in str(result.result_details)


class TestRefresh:
    def test_a_page_added_after_the_first_listing_shows_up_once_the_library_is_loaded_again(
        self, engine: Engine, library_root: Path
    ) -> None:
        _write(library_root, "docs/first.md", "# First\n")
        documentation = engine.library_manager.documentation
        request = ListLibraryDocumentationRequest(library=LIBRARY_NAME)

        first = documentation.list_library_documentation_request(request)
        _write(library_root, "docs/second.md", "# Second\n")
        second = documentation.list_library_documentation_request(request)

        LibraryRegistry._clear()
        LibraryRegistry.generate_new_library(library_data=_schema())
        after_reload = documentation.list_library_documentation_request(request)

        assert isinstance(first, ListLibraryDocumentationResultSuccess)
        assert isinstance(second, ListLibraryDocumentationResultSuccess)
        assert isinstance(after_reload, ListLibraryDocumentationResultSuccess)
        assert [entry.path for entry in first.pages] == ["first.md"]
        assert [entry.path for entry in second.pages] == ["first.md"]
        assert [entry.path for entry in after_reload.pages] == ["first.md", "second.md"]

    def test_an_edit_to_a_page_shows_up_without_loading_the_library_again(
        self, engine: Engine, library_root: Path
    ) -> None:
        _write(library_root, "docs/guide.md", "Old text\n")
        documentation = engine.library_manager.documentation
        request = GetLibraryDocumentationPageRequest(library=LIBRARY_NAME, path="guide.md")

        before = documentation.get_library_documentation_page_request(request)
        _write(library_root, "docs/guide.md", "New text\n")
        after = documentation.get_library_documentation_page_request(request)

        assert isinstance(before, GetLibraryDocumentationPageResultSuccess)
        assert isinstance(after, GetLibraryDocumentationPageResultSuccess)
        assert (before.markdown, after.markdown) == ("Old text\n", "New text\n")

    def test_the_index_is_built_once_per_load(self, engine: Engine, library_root: Path) -> None:
        _write(library_root, "docs/guide.md", "# Guide\n")
        documentation = engine.library_manager.documentation
        request = ListLibraryDocumentationRequest(library=LIBRARY_NAME)

        documentation.list_library_documentation_request(request)
        documentation.list_library_documentation_request(request)

        assert len(documentation._docs_by_library) == 1


class TestUnknownLibraryMessage:
    @pytest.mark.usefixtures("library_root")
    def test_the_message_lists_the_registered_library_names(self, engine: Engine) -> None:
        result = engine.library_manager.documentation.list_library_documentation_request(
            ListLibraryDocumentationRequest(library="No Such Library")
        )

        assert isinstance(result, ListLibraryDocumentationResultFailure)
        assert f"'{LIBRARY_NAME}'" in str(result.result_details)

    def test_a_folder_name_is_pointed_at_the_library_it_holds(self, engine: Engine, library_root: Path) -> None:
        result = engine.library_manager.documentation.list_library_documentation_request(
            ListLibraryDocumentationRequest(library=library_root.name)
        )

        assert isinstance(result, ListLibraryDocumentationResultFailure)
        assert f"the library named '{LIBRARY_NAME}'" in str(result.result_details)

    @pytest.mark.usefixtures("library_root")
    def test_a_long_list_of_names_is_cut_short(self, engine: Engine) -> None:
        for number in range(20):
            LibraryRegistry.generate_new_library(
                library_data=_schema().model_copy(update={"name": f"Library {number}"})
            )

        result = engine.library_manager.documentation.list_library_documentation_request(
            ListLibraryDocumentationRequest(library="No Such Library")
        )

        assert isinstance(result, ListLibraryDocumentationResultFailure)
        assert "and 6 more" in str(result.result_details)


class TestGetLibraryDocumentationPage:
    def test_reads_a_page(self, engine: Engine, library_root: Path) -> None:
        _write(library_root, "docs/guides/start.md", "---\ndescription: How to begin.\n---\n# Start here\n\nBody.\n")

        result = engine.library_manager.documentation.get_library_documentation_page_request(
            GetLibraryDocumentationPageRequest(library=LIBRARY_NAME, path="guides/start.md")
        )

        assert isinstance(result, GetLibraryDocumentationPageResultSuccess)
        assert result.title == "Start here"
        assert result.markdown == "# Start here\n\nBody.\n"
        assert result.description == "How to begin."
        assert result.truncated is False
        assert result.assets_root == str((library_root / "docs").resolve())

    def test_a_long_page_can_be_read_in_parts(self, engine: Engine, library_root: Path) -> None:
        _write(library_root, "docs/long.md", "0123456789")
        documentation = engine.library_manager.documentation

        first = documentation.get_library_documentation_page_request(
            GetLibraryDocumentationPageRequest(library=LIBRARY_NAME, path="long.md", limit=4)
        )
        rest = documentation.get_library_documentation_page_request(
            GetLibraryDocumentationPageRequest(library=LIBRARY_NAME, path="long.md", offset=4)
        )

        assert isinstance(first, GetLibraryDocumentationPageResultSuccess)
        assert (first.markdown, first.total_chars, first.truncated) == ("0123", 10, True)
        assert isinstance(rest, GetLibraryDocumentationPageResultSuccess)
        assert (rest.markdown, rest.truncated) == ("456789", False)

    @pytest.mark.parametrize(("offset", "limit"), [(-1, None), (0, 0), (0, -5)])
    def test_an_unusable_window_fails(self, engine: Engine, library_root: Path, offset: int, limit: int | None) -> None:
        _write(library_root, "docs/long.md", "text")

        result = engine.library_manager.documentation.get_library_documentation_page_request(
            GetLibraryDocumentationPageRequest(library=LIBRARY_NAME, path="long.md", offset=offset, limit=limit)
        )

        assert isinstance(result, GetLibraryDocumentationPageResultFailure)

    def test_a_path_outside_the_docs_folder_fails(self, engine: Engine, library_root: Path) -> None:
        _write(library_root, "secret.md", "private")
        (library_root / "docs").mkdir()

        result = engine.library_manager.documentation.get_library_documentation_page_request(
            GetLibraryDocumentationPageRequest(library=LIBRARY_NAME, path="../secret.md")
        )

        assert isinstance(result, GetLibraryDocumentationPageResultFailure)

    def test_a_missing_page_fails(self, engine: Engine, library_root: Path) -> None:
        (library_root / "docs").mkdir()

        result = engine.library_manager.documentation.get_library_documentation_page_request(
            GetLibraryDocumentationPageRequest(library=LIBRARY_NAME, path="missing.md")
        )

        assert isinstance(result, GetLibraryDocumentationPageResultFailure)
        assert "missing.md" in str(result.result_details)

    def test_unknown_library_fails(self, engine: Engine) -> None:
        result = engine.library_manager.documentation.get_library_documentation_page_request(
            GetLibraryDocumentationPageRequest(library="No Such Library", path="index.md")
        )

        assert isinstance(result, GetLibraryDocumentationPageResultFailure)

    def test_a_sandbox_library_reads_docs_from_its_own_folder(
        self, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sandbox = tmp_path / "sandbox"
        _write(sandbox, "docs/index.md", "# Sandbox docs\n")
        LibraryRegistry.generate_new_library(library_data=_schema())
        info = LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
            fitness=LibraryManager.LibraryFitness.GOOD,
            library_path=str(sandbox),
            is_sandbox=True,
            library_name=LIBRARY_NAME,
            library_version="0.1.0",
        )
        monkeypatch.setitem(engine.library_manager._library_file_path_to_info, str(sandbox), info)

        result = engine.library_manager.documentation.get_library_documentation_page_request(
            GetLibraryDocumentationPageRequest(library=LIBRARY_NAME, path="index.md")
        )

        assert isinstance(result, GetLibraryDocumentationPageResultSuccess)
        assert result.title == "Sandbox docs"


class TestGetNodeDocumentation:
    def test_node_with_a_page(self, engine: Engine, library_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write(
            library_root,
            "docs/nodes/LoadAudio.md",
            "---\ndescription: Brings a sound file in.\nwhen_to_use: You have a recording.\n---\n## When to use it\n\nAlways.\n",
        )
        monkeypatch.setattr(
            engine.library_manager.catalog,
            "describe_node_type_request",
            lambda _request: _describe_success("LoadAudio", AUDIO_METADATA),
        )

        result = engine.library_manager.documentation.get_node_documentation_request(
            GetNodeDocumentationRequest(node_type="LoadAudio")
        )

        assert isinstance(result, GetNodeDocumentationResultSuccess)
        assert result.library == LIBRARY_NAME
        assert result.title == "Load Audio"
        assert result.markdown == "## When to use it\n\nAlways.\n"
        assert result.description == "Brings a sound file in."
        assert result.when_to_use == "You have a recording."
        assert result.deprecation is not None
        assert result.deprecation.removal_version == "2.0.0"
        assert result.assets_root == str((library_root / "docs").resolve())

    def test_a_frontmatter_title_replaces_the_display_name(
        self, engine: Engine, library_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(library_root, "docs/nodes/LoadAudio.md", "---\ntitle: Bring in audio\n---\nText\n")
        monkeypatch.setattr(
            engine.library_manager.catalog,
            "describe_node_type_request",
            lambda _request: _describe_success("LoadAudio", AUDIO_METADATA),
        )

        result = engine.library_manager.documentation.get_node_documentation_request(
            GetNodeDocumentationRequest(node_type="LoadAudio")
        )

        assert isinstance(result, GetNodeDocumentationResultSuccess)
        assert result.title == "Bring in audio"

    @pytest.mark.usefixtures("library_root")
    def test_node_without_a_page_falls_back_to_the_manifest(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        metadata = NodeMetadata(category="Audio", description="Mixes two sounds.", display_name="Mix Audio")
        monkeypatch.setattr(
            engine.library_manager.catalog,
            "describe_node_type_request",
            lambda _request: _describe_success("MixAudio", metadata),
        )

        result = engine.library_manager.documentation.get_node_documentation_request(
            GetNodeDocumentationRequest(node_type="MixAudio")
        )

        assert isinstance(result, GetNodeDocumentationResultSuccess)
        assert result.markdown is None
        assert result.total_chars == 0
        assert result.title == "Mix Audio"
        assert result.description == "Mixes two sounds."
        assert result.deprecation is None

    def test_node_page_is_read_in_parts(
        self, engine: Engine, library_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(library_root, "docs/nodes/LoadAudio.md", "0123456789")
        monkeypatch.setattr(
            engine.library_manager.catalog,
            "describe_node_type_request",
            lambda _request: _describe_success("LoadAudio", AUDIO_METADATA),
        )

        result = engine.library_manager.documentation.get_node_documentation_request(
            GetNodeDocumentationRequest(node_type="LoadAudio", offset=2, limit=3)
        )

        assert isinstance(result, GetNodeDocumentationResultSuccess)
        assert (result.markdown, result.total_chars, result.truncated) == ("234", 10, True)

    @pytest.mark.usefixtures("library_root")
    def test_a_node_type_the_engine_cannot_find_fails(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            engine.library_manager.catalog,
            "describe_node_type_request",
            lambda _request: DescribeNodeTypeResultFailure(result_details="No node type called Nope."),
        )

        result = engine.library_manager.documentation.get_node_documentation_request(
            GetNodeDocumentationRequest(node_type="Nope")
        )

        assert isinstance(result, GetNodeDocumentationResultFailure)
        assert "Nope" in str(result.result_details)

    def test_an_unusable_window_fails_before_anything_is_looked_up(self, engine: Engine) -> None:
        result = engine.library_manager.documentation.get_node_documentation_request(
            GetNodeDocumentationRequest(node_type="LoadAudio", offset=-1)
        )

        assert isinstance(result, GetNodeDocumentationResultFailure)

    def test_a_request_with_neither_a_node_nor_a_node_type_fails(self, engine: Engine) -> None:
        result = engine.library_manager.documentation.get_node_documentation_request(GetNodeDocumentationRequest())

        assert isinstance(result, GetNodeDocumentationResultFailure)
        assert "neither" in str(result.result_details)


class TestGetNodeDocumentationForANodeOnTheCanvas:
    @pytest.fixture
    def described(self, engine: Engine, library_root: Path, monkeypatch: pytest.MonkeyPatch) -> list[Any]:  # noqa: ARG002
        """Records the DescribeNodeTypeRequest each documentation request makes."""
        seen: list[Any] = []

        def describe(request: Any) -> DescribeNodeTypeResultSuccess:
            seen.append(request)
            return _describe_success(request.node_type, AUDIO_METADATA)

        monkeypatch.setattr(engine.library_manager.catalog, "describe_node_type_request", describe)
        return seen

    def test_the_node_type_and_library_come_from_the_node(self, engine: Engine, described: list[Any]) -> None:
        workflow = _workflow_with(_canvas_node("audio_1"))

        result = engine.library_manager.documentation.get_node_documentation_request(
            GetNodeDocumentationRequest(node_name="audio_1"), workflow
        )

        assert isinstance(result, GetNodeDocumentationResultSuccess)
        assert result.node_type == "LoadAudio"
        assert (described[0].node_type, described[0].library) == ("LoadAudio", LIBRARY_NAME)

    @pytest.mark.usefixtures("described")
    def test_two_nodes_of_one_type_report_their_own_parameters(self, engine: Engine) -> None:
        workflow = _workflow_with(_canvas_node("plain"), _canvas_node("grown", with_extra=True))
        documentation = engine.library_manager.documentation

        plain = documentation.get_node_documentation_request(GetNodeDocumentationRequest(node_name="plain"), workflow)
        grown = documentation.get_node_documentation_request(GetNodeDocumentationRequest(node_name="grown"), workflow)

        assert isinstance(plain, GetNodeDocumentationResultSuccess)
        assert isinstance(grown, GetNodeDocumentationResultSuccess)
        plain_names = [param.name for param in plain.parameters]
        grown_names = [param.name for param in grown.parameters]
        assert "text" in plain_names
        assert "extra" not in plain_names
        assert "extra" in grown_names

    @pytest.mark.usefixtures("described")
    def test_the_page_comes_with_the_nodes_own_parameters(self, engine: Engine, library_root: Path) -> None:
        _write(library_root, "docs/nodes/LoadAudio.md", "## When to use it\n\nAlways.\n")
        workflow = _workflow_with(_canvas_node("grown", with_extra=True))

        result = engine.library_manager.documentation.get_node_documentation_request(
            GetNodeDocumentationRequest(node_name="grown"), workflow
        )

        assert isinstance(result, GetNodeDocumentationResultSuccess)
        assert result.markdown == "## When to use it\n\nAlways.\n"
        assert "extra" in [param.name for param in result.parameters]

    @pytest.mark.usefixtures("described")
    def test_a_node_type_that_matches_the_node_is_accepted(self, engine: Engine) -> None:
        workflow = _workflow_with(_canvas_node("audio_1"))

        result = engine.library_manager.documentation.get_node_documentation_request(
            GetNodeDocumentationRequest(node_name="audio_1", node_type="LoadAudio"), workflow
        )

        assert isinstance(result, GetNodeDocumentationResultSuccess)

    def test_a_node_type_that_disagrees_with_the_node_fails(self, engine: Engine, described: list[Any]) -> None:
        workflow = _workflow_with(_canvas_node("audio_1"))

        result = engine.library_manager.documentation.get_node_documentation_request(
            GetNodeDocumentationRequest(node_name="audio_1", node_type="MixAudio"), workflow
        )

        assert isinstance(result, GetNodeDocumentationResultFailure)
        assert "LoadAudio" in str(result.result_details)
        assert described == []

    @pytest.mark.usefixtures("described")
    def test_an_unknown_node_name_fails(self, engine: Engine) -> None:
        workflow = _workflow_with(_canvas_node("audio_1"))

        result = engine.library_manager.documentation.get_node_documentation_request(
            GetNodeDocumentationRequest(node_name="nope"), workflow
        )

        assert isinstance(result, GetNodeDocumentationResultFailure)
        assert "nope" in str(result.result_details)

    @pytest.mark.usefixtures("described")
    def test_a_node_name_with_no_open_workflow_fails(self, engine: Engine) -> None:
        result = engine.library_manager.documentation.get_node_documentation_request(
            GetNodeDocumentationRequest(node_name="audio_1")
        )

        assert isinstance(result, GetNodeDocumentationResultFailure)
        assert "no workflow is open" in str(result.result_details)
