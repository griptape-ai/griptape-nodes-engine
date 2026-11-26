"""LibraryManager checks a deprecated node's replacement once every library has loaded and as libraries change."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.node_library.library_registry import (
    CategoryDefinition,
    LibraryMetadata,
    LibraryRegistry,
    LibrarySchema,
    NodeDefinition,
    NodeDeprecationMetadata,
    NodeDeprecationReplacement,
    NodeMetadata,
)
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
    ReloadAllLibrariesRequest,
    ReloadAllLibrariesResultSuccess,
    UnloadLibraryFromRegistryRequest,
    UnloadLibraryFromRegistryResultSuccess,
)
from griptape_nodes.retained_mode.managers.fitness_problems.libraries import UnresolvedDeprecationReplacementProblem
from griptape_nodes.retained_mode.managers.library.common import LibraryFitness
from griptape_nodes.retained_mode.managers.settings import LIBRARIES_TO_DOWNLOAD_KEY, LIBRARIES_TO_REGISTER_KEY
from griptape_nodes.retained_mode.managers.version_compatibility_manager import _replacement_names

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.managers.library.common import LibraryInfo

_NODE_SOURCE = """
from griptape_nodes.exe_types.node_types import BaseNode


class {class_name}(BaseNode):
    def process(self):
        return None
"""


@pytest.fixture(autouse=True)
def _clean_registry() -> Generator[None, None, None]:
    LibraryRegistry._clear()
    yield
    LibraryRegistry._clear()


def _write_library(
    directory: Path,
    name: str,
    class_name: str,
    *,
    display_name: str | None = None,
    replacements: list[NodeDeprecationReplacement] | None = None,
) -> Path:
    deprecation = None
    if replacements is not None:
        deprecation = NodeDeprecationMetadata(replacements=replacements)
    schema = LibrarySchema(
        name=name,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="test",
            description=f"{name} manifest",
            library_version="1.0.0",
            engine_version="0.98.0",
            tags=[],
        ),
        categories=[{"Test": CategoryDefinition(title="Test", description="test", color="#000", icon="Folder")}],
        nodes=[
            NodeDefinition(
                class_name=class_name,
                file_path="node.py",
                metadata=NodeMetadata(
                    category="Test",
                    description="test node",
                    display_name=display_name or class_name,
                    deprecation=deprecation,
                ),
            )
        ],
    )
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "node.py").write_text(_NODE_SOURCE.format(class_name=class_name), encoding="utf-8")
    manifest = directory / "griptape_nodes_library.json"
    manifest.write_text(json.dumps(schema.model_dump(mode="json")), encoding="utf-8")
    return manifest


async def _load(engine: Engine, tmp_path: Path, manifests: list[Path]) -> None:
    config = engine.config_manager
    config.set_config_value(LIBRARIES_TO_REGISTER_KEY, [str(path) for path in manifests])
    config.set_config_value(LIBRARIES_TO_DOWNLOAD_KEY, [])
    config.set_config_value("sandbox_library_directory", str(tmp_path / "sandbox"))
    config.load_configs()
    await engine.library_manager.load_all_libraries_from_config()


def _replacement_problems(info: LibraryInfo) -> list[UnresolvedDeprecationReplacementProblem]:
    return [problem for problem in info.problems if isinstance(problem, UnresolvedDeprecationReplacementProblem)]


class TestDeprecationReplacementAtLoad:
    @pytest.mark.asyncio
    async def test_replacement_in_a_library_loaded_later_resolves(self, engine: Engine, tmp_path: Path) -> None:
        # The deprecated node's library loads first, so its replacement isn't registered yet when it does.
        old = _write_library(
            tmp_path / "old_lib",
            "Old Library",
            "OldNode",
            replacements=[NodeDeprecationReplacement(library="New Library", node_type="NewNode")],
        )
        new = _write_library(tmp_path / "new_lib", "New Library", "NewNode")

        await _load(engine, tmp_path, [old, new])

        info = engine.library_manager.get_library_info_for_attempted_load(str(old))
        assert info.library_name == "Old Library"
        assert _replacement_problems(info) == []

    @pytest.mark.asyncio
    async def test_missing_replacement_is_a_library_problem(self, engine: Engine, tmp_path: Path) -> None:
        old = _write_library(
            tmp_path / "old_lib",
            "Old Library",
            "OldNode",
            replacements=[NodeDeprecationReplacement(library="New Library", node_type="MissingNode")],
        )
        new = _write_library(tmp_path / "new_lib", "New Library", "NewNode")

        await _load(engine, tmp_path, [old, new])

        info = engine.library_manager.get_library_info_for_attempted_load(str(old))
        assert _replacement_problems(info) == [
            UnresolvedDeprecationReplacementProblem(
                display_name="OldNode",
                replacement_library="New Library",
                replacement_node_type="MissingNode",
                replacement_library_loaded=True,
            )
        ]
        assert info.fitness == LibraryFitness.FLAWED
        collated = engine.library_manager.catalog.collate_problems_for_lib_info(info)
        assert collated is not None
        assert "has no node type 'MissingNode'" in str(collated)
        assert "OldNode" in LibraryRegistry.get_library(name="Old Library").get_registered_nodes()

    @pytest.mark.asyncio
    async def test_replacement_in_a_library_that_is_not_loaded_is_a_library_problem(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        old = _write_library(
            tmp_path / "old_lib",
            "Old Library",
            "OldNode",
            replacements=[NodeDeprecationReplacement(library="Absent Library", node_type="NewNode")],
        )

        await _load(engine, tmp_path, [old])

        info = engine.library_manager.get_library_info_for_attempted_load(str(old))
        problems = _replacement_problems(info)
        assert len(problems) == 1
        assert not problems[0].replacement_library_loaded

    @pytest.mark.asyncio
    async def test_registering_the_replacement_mid_session_clears_the_problem(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        old = _write_library(
            tmp_path / "old_lib",
            "Old Library",
            "OldNode",
            replacements=[NodeDeprecationReplacement(library="New Library", node_type="NewNode")],
        )
        new = _write_library(tmp_path / "new_lib", "New Library", "NewNode")
        await _load(engine, tmp_path, [old])
        library_manager = engine.library_manager
        assert len(_replacement_problems(library_manager.get_library_info_for_attempted_load(str(old)))) == 1

        result = await library_manager.registration.register_library_from_file_request(
            RegisterLibraryFromFileRequest(file_path=str(new))
        )

        assert isinstance(result, RegisterLibraryFromFileResultSuccess), result.result_details
        assert _replacement_problems(library_manager.get_library_info_for_attempted_load(str(old))) == []

    @pytest.mark.asyncio
    async def test_unloading_the_replacement_reports_the_problem(self, engine: Engine, tmp_path: Path) -> None:
        old = _write_library(
            tmp_path / "old_lib",
            "Old Library",
            "OldNode",
            replacements=[NodeDeprecationReplacement(library="New Library", node_type="NewNode")],
        )
        new = _write_library(tmp_path / "new_lib", "New Library", "NewNode")
        await _load(engine, tmp_path, [old, new])
        library_manager = engine.library_manager

        result = library_manager.registration.unload_library_from_registry_request(
            UnloadLibraryFromRegistryRequest(library_name="New Library")
        )

        assert isinstance(result, UnloadLibraryFromRegistryResultSuccess), result.result_details
        problems = _replacement_problems(library_manager.get_library_info_for_attempted_load(str(old)))
        assert len(problems) == 1
        assert not problems[0].replacement_library_loaded

    @pytest.mark.asyncio
    async def test_reload_reports_each_problem_once(self, engine: Engine, tmp_path: Path) -> None:
        old = _write_library(
            tmp_path / "old_lib",
            "Old Library",
            "OldNode",
            replacements=[NodeDeprecationReplacement(library="Absent Library", node_type="NewNode")],
        )
        await _load(engine, tmp_path, [old])

        result = await engine.library_manager.reload_libraries_request(ReloadAllLibrariesRequest())

        assert isinstance(result, ReloadAllLibrariesResultSuccess), result.result_details
        info = engine.library_manager.get_library_info_for_attempted_load(str(old))
        assert len(_replacement_problems(info)) == 1

    @pytest.mark.asyncio
    async def test_library_registered_mid_session_reports_its_own_missing_replacement(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        old = _write_library(
            tmp_path / "old_lib",
            "Old Library",
            "OldNode",
            replacements=[NodeDeprecationReplacement(library="Absent Library", node_type="NewNode")],
        )
        await _load(engine, tmp_path, [])

        result = await engine.library_manager.registration.register_library_from_file_request(
            RegisterLibraryFromFileRequest(file_path=str(old))
        )

        assert isinstance(result, RegisterLibraryFromFileResultSuccess), result.result_details
        info = engine.library_manager.get_library_info_for_attempted_load(str(old))
        assert len(_replacement_problems(info)) == 1

    @pytest.mark.asyncio
    async def test_replacement_without_a_library_is_looked_up_in_the_deprecated_nodes_library(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        old = _write_library(
            tmp_path / "old_lib",
            "Old Library",
            "OldNode",
            replacements=[NodeDeprecationReplacement(node_type="MissingNode")],
        )

        await _load(engine, tmp_path, [old])

        info = engine.library_manager.get_library_info_for_attempted_load(str(old))
        assert [(p.replacement_library, p.replacement_library_loaded) for p in _replacement_problems(info)] == [
            ("Old Library", True)
        ]

    @pytest.mark.asyncio
    async def test_each_missing_replacement_is_reported(self, engine: Engine, tmp_path: Path) -> None:
        old = _write_library(
            tmp_path / "old_lib",
            "Old Library",
            "OldNode",
            replacements=[
                NodeDeprecationReplacement(library="New Library", node_type="NewNode"),
                NodeDeprecationReplacement(library="New Library", node_type="MissingNode"),
            ],
        )
        new = _write_library(tmp_path / "new_lib", "New Library", "NewNode")

        await _load(engine, tmp_path, [old, new])

        info = engine.library_manager.get_library_info_for_attempted_load(str(old))
        assert [p.replacement_node_type for p in _replacement_problems(info)] == ["MissingNode"]


class TestReplacementNames:
    @pytest.mark.asyncio
    async def test_replacements_are_named_by_display_name(self, engine: Engine, tmp_path: Path) -> None:
        old = _write_library(tmp_path / "old_lib", "Old Library", "SameNode", display_name="Same Library Node")
        new = _write_library(tmp_path / "new_lib", "New Library", "NewNode", display_name="New Node 2.0")
        await _load(engine, tmp_path, [old, new])

        names = _replacement_names(
            "Old Library",
            [
                NodeDeprecationReplacement(node_type="SameNode"),
                NodeDeprecationReplacement(library="New Library", node_type="NewNode"),
            ],
        )

        assert names == ["'Same Library Node'", "'New Node 2.0' from library 'New Library'"]

    @pytest.mark.asyncio
    async def test_unregistered_replacement_falls_back_to_its_node_type(self, engine: Engine, tmp_path: Path) -> None:
        await _load(engine, tmp_path, [])

        names = _replacement_names("Old Library", [NodeDeprecationReplacement(library="Absent", node_type="Gone")])

        assert names == ["'Gone' from library 'Absent'"]
