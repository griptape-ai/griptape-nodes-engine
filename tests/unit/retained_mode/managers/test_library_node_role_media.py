"""Tests for the role and media fields on node metadata: the schema, the registration check, and the results."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import pytest

from griptape_nodes.node_library.library_registry import (
    NODE_MEDIA,
    NODE_ROLES,
    PRESET_ROLES,
    LibraryMetadata,
    LibraryRegistry,
    LibrarySchema,
    NodeDefinition,
    NodeMetadata,
)
from griptape_nodes.retained_mode.events.library_events import (
    GetAllInfoForLibraryRequest,
    GetAllInfoForLibraryResultSuccess,
    GetNodeMetadataFromLibraryRequest,
    GetNodeMetadataFromLibraryResultSuccess,
    ListNodeTypesInLibraryRequest,
    ListNodeTypesInLibraryResultSuccess,
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.managers.fitness_problems.libraries import InvalidNodeMetadataProblem
from griptape_nodes.retained_mode.managers.library.common import LibraryFitness
from griptape_nodes.retained_mode.managers.library.node_role_media import check_node_role_media_at_registration

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine

LIBRARY_NAME = "Role Media Test Library"

_NODE_SOURCE = """\
from griptape_nodes.exe_types.node_types import DataNode


class {name}(DataNode):
    def process(self):
        pass
"""


def _manifest(node_metadata: dict[str, dict[str, Any]], *, schema_version: str | None = None) -> dict[str, Any]:
    """A manifest with one node per entry; each entry's dict is merged into that node's metadata."""
    schema = LibrarySchema(
        name=LIBRARY_NAME,
        library_schema_version=schema_version or LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="test", description="role media", library_version="0.1.0", engine_version="0.98.0", tags=[]
        ),
        categories=[],
        nodes=[
            NodeDefinition(
                class_name=name,
                file_path=f"{name.lower()}.py",
                metadata=NodeMetadata(category="image", description=f"{name} node", display_name=name),
            )
            for name in node_metadata
        ],
    )
    manifest = schema.model_dump(mode="json", exclude_none=True)
    for node in manifest["nodes"]:
        node["metadata"].update(node_metadata[node["class_name"]])
    return manifest


def _write_library(directory: Path, node_metadata: dict[str, dict[str, Any]]) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    for name in node_metadata:
        (directory / f"{name.lower()}.py").write_text(_NODE_SOURCE.format(name=name), encoding="utf-8")
    manifest_path = directory / "griptape_nodes_library.json"
    manifest_path.write_text(json.dumps(_manifest(node_metadata)), encoding="utf-8")
    return str(manifest_path)


async def _register(engine: Engine, manifest: str) -> None:
    registered = await engine.ahandle_request(RegisterLibraryFromFileRequest(file_path=manifest))
    assert isinstance(registered, RegisterLibraryFromFileResultSuccess), registered.result_details


def _problems(engine: Engine) -> list[InvalidNodeMetadataProblem]:
    library_info = engine.library_manager.get_library_info_by_library_name(LIBRARY_NAME)
    assert library_info is not None
    return [problem for problem in library_info.problems if isinstance(problem, InvalidNodeMetadataProblem)]


@pytest.fixture(autouse=True)
def _clean_registry() -> Generator[None, None, None]:
    """LibraryRegistry holds class-level state that survives between tests."""
    LibraryRegistry._clear()
    yield
    LibraryRegistry._clear()


class TestSchema:
    def test_roles_are_the_preset_roles(self) -> None:
        assert NODE_ROLES == PRESET_ROLES

    def test_fields_default_to_none(self) -> None:
        metadata = NodeMetadata(category="image", description="d", display_name="n")

        assert metadata.role is None
        assert metadata.media is None

    def test_fields_parse(self) -> None:
        schema = LibrarySchema.model_validate(_manifest({"Blur": {"role": "Edit", "media": ["image", "video"]}}))

        metadata = schema.nodes[0].metadata
        assert metadata.role == "Edit"
        assert metadata.media == ["image", "video"]

    def test_old_manifest_without_fields_validates(self) -> None:
        schema = LibrarySchema.model_validate(_manifest({"Blur": {}}, schema_version="0.15.0"))

        assert schema.nodes[0].metadata.role is None
        assert schema.nodes[0].metadata.media is None


class TestRegistrationCheck:
    def test_valid_values_are_kept(self) -> None:
        schema = LibrarySchema.model_validate(
            _manifest({"Blur": {"role": "Edit", "media": list(NODE_MEDIA)}, "Plain": {}})
        )

        assert check_node_role_media_at_registration(schema) == []
        assert schema.nodes[0].metadata.role == "Edit"
        assert schema.nodes[0].metadata.media == list(NODE_MEDIA)
        assert schema.nodes[1].metadata.role is None
        assert schema.nodes[1].metadata.media is None

    def test_unknown_values_are_dropped_and_reported(self, caplog: pytest.LogCaptureFixture) -> None:
        schema = LibrarySchema.model_validate(
            _manifest(
                {
                    "Blur": {"role": "Mangle", "media": ["image", "hologram", "image", "video"]},
                    "Odd": {"media": ["smell"]},
                }
            )
        )

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            problems = check_node_role_media_at_registration(schema)

        assert [problem.node_type for problem in problems] == ["Blur", "Blur", "Odd"]
        assert "role 'Mangle'" in problems[0].reason
        assert "'hologram'" in problems[1].reason
        assert "'smell'" in problems[2].reason
        assert schema.nodes[0].metadata.role is None
        assert schema.nodes[0].metadata.media == ["image", "video"]
        assert schema.nodes[1].metadata.media is None
        assert f"Library '{LIBRARY_NAME}' node 'Blur' metadata" in caplog.text


class TestResults:
    @pytest.mark.asyncio
    async def test_fields_flow_through_listing_and_metadata_results(self, engine: Engine, tmp_path: Path) -> None:
        manifest = _write_library(tmp_path / "lib", {"Blur": {"role": "Edit", "media": ["image", "video"]}})
        await _register(engine, manifest)

        listed = engine.library_manager.catalog.on_list_node_types_in_library_request(
            ListNodeTypesInLibraryRequest(library=LIBRARY_NAME, include_metadata=True)
        )
        assert isinstance(listed, ListNodeTypesInLibraryResultSuccess)
        assert listed.node_metadata is not None
        assert listed.node_metadata["Blur"].role == "Edit"
        assert listed.node_metadata["Blur"].media == ["image", "video"]

        single = engine.library_manager.catalog.get_node_metadata_from_library_request(
            GetNodeMetadataFromLibraryRequest(library=LIBRARY_NAME, node_type="Blur")
        )
        assert isinstance(single, GetNodeMetadataFromLibraryResultSuccess)
        assert single.metadata.role == "Edit"
        assert single.metadata.media == ["image", "video"]

        all_info = await engine.library_manager.catalog.on_get_all_info_for_library_request(
            GetAllInfoForLibraryRequest(library=LIBRARY_NAME)
        )
        assert isinstance(all_info, GetAllInfoForLibraryResultSuccess)
        blur = all_info.node_type_name_to_node_metadata_details["Blur"].metadata
        assert (blur.role, blur.media) == ("Edit", ["image", "video"])
        assert _problems(engine) == []

    @pytest.mark.asyncio
    async def test_bad_values_are_dropped_without_failing_registration(self, engine: Engine, tmp_path: Path) -> None:
        manifest = _write_library(tmp_path / "lib", {"Blur": {"role": "Mangle", "media": ["image", "hologram"]}})
        await _register(engine, manifest)

        listed = engine.library_manager.catalog.on_list_node_types_in_library_request(
            ListNodeTypesInLibraryRequest(library=LIBRARY_NAME, include_metadata=True)
        )
        assert isinstance(listed, ListNodeTypesInLibraryResultSuccess)
        assert listed.node_types == ["Blur"]
        assert listed.node_metadata is not None
        assert listed.node_metadata["Blur"].role is None
        assert listed.node_metadata["Blur"].media == ["image"]
        assert [problem.node_type for problem in _problems(engine)] == ["Blur", "Blur"]
        library_info = engine.library_manager.get_library_info_by_library_name(LIBRARY_NAME)
        assert library_info is not None
        assert library_info.fitness == LibraryFitness.FLAWED
