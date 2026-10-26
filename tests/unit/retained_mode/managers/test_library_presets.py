"""Tests for library presets: the manifest field, the registration checks, and the listing."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from griptape_nodes.node_library.library_registry import (
    LibraryMetadata,
    LibraryRegistry,
    LibrarySchema,
    NodeDefinition,
    NodeMetadata,
    PresetDefinition,
)
from griptape_nodes.retained_mode.events.library_events import (
    ListNodeTypesInLibraryRequest,
    ListNodeTypesInLibraryResultSuccess,
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.managers.fitness_problems.libraries import InvalidPresetProblem
from griptape_nodes.retained_mode.managers.library.catalog import LibraryCatalog
from griptape_nodes.retained_mode.managers.library.common import LibraryFitness
from griptape_nodes.retained_mode.managers.library.presets import check_presets_at_registration

if TYPE_CHECKING:
    from collections.abc import Generator

    from griptape_nodes.retained_mode.engine import Engine

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "library_presets.json"
LIBRARY_NAME = "Preset Test Library"

_REFORMAT_SOURCE = """\
from griptape_nodes.exe_types.core_types import Parameter
from griptape_nodes.exe_types.node_types import DataNode


class Reformat(DataNode):
    def __init__(self, name, metadata=None):
        super().__init__(name, metadata)
        self.add_parameter(Parameter(name="operation", type="str", default_value="crop", tooltip="Operation"))
        self.add_parameter(Parameter(name="width", type="int", default_value=0, tooltip="Width"))

    def process(self):
        pass
"""


def _fixture_presets() -> list[dict[str, Any]]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["presets"]


def _manifest(presets: list[dict[str, Any]] | None, *, schema_version: str | None = None) -> dict[str, Any]:
    schema = LibrarySchema(
        name=LIBRARY_NAME,
        library_schema_version=schema_version or LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="test", description="presets", library_version="0.1.0", engine_version="0.98.0", tags=[]
        ),
        categories=[],
        nodes=[
            NodeDefinition(
                class_name="Reformat",
                file_path="reformat.py",
                metadata=NodeMetadata(category="image", description="Reformats an image", display_name="Reformat"),
            )
        ],
    )
    manifest = schema.model_dump(mode="json", exclude_none=True)
    if presets is not None:
        manifest["presets"] = presets
    return manifest


def _write_library(directory: Path, presets: list[dict[str, Any]] | None) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "reformat.py").write_text(_REFORMAT_SOURCE, encoding="utf-8")
    manifest_path = directory / "griptape_nodes_library.json"
    manifest_path.write_text(json.dumps(_manifest(presets)), encoding="utf-8")
    return str(manifest_path)


def _list(engine: Engine, *, include_metadata: bool) -> ListNodeTypesInLibraryResultSuccess:
    result = engine.library_manager.catalog.on_list_node_types_in_library_request(
        ListNodeTypesInLibraryRequest(library=LIBRARY_NAME, include_metadata=include_metadata)
    )
    assert isinstance(result, ListNodeTypesInLibraryResultSuccess), result.result_details
    return result


def _preset_problems(engine: Engine) -> list[InvalidPresetProblem]:
    library_info = engine.library_manager.get_library_info_by_library_name(LIBRARY_NAME)
    assert library_info is not None
    return [problem for problem in library_info.problems if isinstance(problem, InvalidPresetProblem)]


@pytest.fixture(autouse=True)
def _clean_registry() -> Generator[None, None, None]:
    """LibraryRegistry holds class-level state that survives between tests."""
    LibraryRegistry._clear()
    yield
    LibraryRegistry._clear()


class TestPresetSchema:
    def test_manifest_without_presets_still_validates(self) -> None:
        schema = LibrarySchema.model_validate(_manifest(None, schema_version="0.14.0"))

        assert schema.presets is None

    def test_fixture_presets_parse(self) -> None:
        schema = LibrarySchema.model_validate(_manifest(_fixture_presets()))

        assert schema.presets is not None
        assert [(p.node, p.name, p.role, p.params) for p in schema.presets] == [
            ("Reformat", "Crop", "Edit", {"operation": "crop"}),
            ("Reformat", "Resize", "Edit", {"operation": "resize"}),
            ("Reformat", "Extend", "Edit", {"operation": "extend"}),
            ("Reformat", "Split grid", "Edit", {"operation": "split_grid"}),
        ]
        assert all(p.library is None for p in schema.presets)

    def test_optional_fields_default(self) -> None:
        preset = PresetDefinition.model_validate({"node": "Reformat", "name": "Plain", "role": "Edit"})

        assert preset.params == {}
        assert preset.description is None
        assert preset.library is None


class TestRegistrationCheck:
    def test_accepts_fixture_presets(self) -> None:
        schema = LibrarySchema.model_validate(_manifest(_fixture_presets()))

        result = check_presets_at_registration(schema)

        assert [p.name for p in result.accepted] == ["Crop", "Resize", "Extend", "Split grid"]
        assert result.problems == []

    def test_rejects_unknown_node_and_role(self, caplog: pytest.LogCaptureFixture) -> None:
        presets = [
            *_fixture_presets(),
            {"node": "NoSuchNode", "name": "Ghost", "role": "Edit"},
            {"node": "Reformat", "name": "Odd role", "role": "Mangle"},
        ]
        schema = LibrarySchema.model_validate(_manifest(presets))

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            result = check_presets_at_registration(schema)

        assert [p.name for p in result.accepted] == ["Crop", "Resize", "Extend", "Split grid"]
        assert [(p.preset_name, p.reason) for p in result.problems] == [
            ("Ghost", "the library has no node type 'NoSuchNode'"),
            ("Odd role", result.problems[1].reason),
        ]
        assert "'Mangle'" in result.problems[1].reason
        assert f"Library '{LIBRARY_NAME}' preset 'Ghost' was skipped" in caplog.text

    def test_other_library_node_is_not_checked_at_registration(self) -> None:
        presets = [{"node": "Elsewhere", "name": "Borrowed", "role": "Edit", "library": "Other Library"}]
        schema = LibrarySchema.model_validate(_manifest(presets))

        result = check_presets_at_registration(schema)

        assert [p.name for p in result.accepted] == ["Borrowed"]


class TestPresetsInListing:
    @pytest.mark.asyncio
    async def test_valid_presets_returned_only_with_metadata(self, engine: Engine, tmp_path: Path) -> None:
        manifest = _write_library(tmp_path / "lib", _fixture_presets())

        registered = await engine.ahandle_request(RegisterLibraryFromFileRequest(file_path=manifest))
        assert isinstance(registered, RegisterLibraryFromFileResultSuccess), registered.result_details

        assert _list(engine, include_metadata=False).presets is None
        presets = _list(engine, include_metadata=True).presets
        assert presets is not None
        assert [p.model_dump(mode="json") for p in presets] == [
            {**preset, "library": None} for preset in _fixture_presets()
        ]
        assert _preset_problems(engine) == []

    @pytest.mark.asyncio
    async def test_bad_presets_are_dropped_and_reported_without_failing_registration(
        self, engine: Engine, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        presets = [
            *_fixture_presets(),
            {"node": "NoSuchNode", "name": "Ghost", "role": "Edit"},
            {"node": "Reformat", "name": "Bad param", "role": "Edit", "params": {"colour": "red"}},
        ]
        manifest = _write_library(tmp_path / "lib", presets)

        registered = await engine.ahandle_request(RegisterLibraryFromFileRequest(file_path=manifest))
        assert isinstance(registered, RegisterLibraryFromFileResultSuccess), registered.result_details
        assert [p.preset_name for p in _preset_problems(engine)] == ["Ghost"]

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            listed = _list(engine, include_metadata=True).presets

        assert listed is not None
        assert [p.name for p in listed] == ["Crop", "Resize", "Extend", "Split grid"]
        problems = {p.preset_name: p.reason for p in _preset_problems(engine)}
        assert problems == {
            "Ghost": "the library has no node type 'NoSuchNode'",
            "Bad param": "node type 'Reformat' has no parameter named 'colour'",
        }
        assert f"Library '{LIBRARY_NAME}' preset 'Bad param' was skipped" in caplog.text
        library_info = engine.library_manager.get_library_info_by_library_name(LIBRARY_NAME)
        assert library_info is not None
        assert library_info.fitness == LibraryFitness.FLAWED

    @pytest.mark.asyncio
    async def test_param_check_runs_once(self, engine: Engine, tmp_path: Path) -> None:
        manifest = _write_library(tmp_path / "lib", _fixture_presets())
        await engine.ahandle_request(RegisterLibraryFromFileRequest(file_path=manifest))

        with patch.object(
            LibraryCatalog, "_construct_probe_node", wraps=LibraryCatalog._construct_probe_node
        ) as construct:
            _list(engine, include_metadata=True)
            first_count = construct.call_count
            _list(engine, include_metadata=True)

        assert first_count == len(_fixture_presets())
        assert construct.call_count == first_count

    @pytest.mark.asyncio
    async def test_preset_for_unregistered_library_waits(self, engine: Engine, tmp_path: Path) -> None:
        presets = [{"node": "Elsewhere", "name": "Borrowed", "role": "Edit", "library": "Other Library"}]
        manifest = _write_library(tmp_path / "lib", presets)
        await engine.ahandle_request(RegisterLibraryFromFileRequest(file_path=manifest))

        listed = _list(engine, include_metadata=True).presets

        assert listed == []
        assert _preset_problems(engine) == []
        library = LibraryRegistry.get_library(LIBRARY_NAME)
        assert [p.name for p in library.get_presets()] == ["Borrowed"]
        assert not library.are_preset_params_checked()
