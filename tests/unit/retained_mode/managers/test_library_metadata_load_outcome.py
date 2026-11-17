"""Tests that library metadata carries the engine's record of the last attempt to load each library.

A manifest that parses says nothing about whether the library then loaded, so without that record a
library whose dependency install failed reads exactly like one added since the last refresh.
"""

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
    NodeMetadata,
)
from griptape_nodes.retained_mode.events.library_events import (
    LoadMetadataForAllLibrariesRequest,
    LoadMetadataForAllLibrariesResultSuccess,
)
from griptape_nodes.retained_mode.managers.fitness_problems.libraries import DependencyInstallationFailedProblem
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager
from griptape_nodes.retained_mode.managers.settings import LIBRARIES_TO_REGISTER_KEY

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine

LIBRARY_NAME = "Test Library"


def _write_manifest(directory: Path) -> Path:
    schema = LibrarySchema(
        name=LIBRARY_NAME,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="test",
            description="test manifest",
            library_version="1.0.0",
            engine_version="0.98.0",
            tags=[],
        ),
        categories=[{"Test": CategoryDefinition(title="Test", description="test", color="#000", icon="Folder")}],
        nodes=[
            NodeDefinition(
                class_name="TestNode",
                file_path="node.py",
                metadata=NodeMetadata(category="Test", description="test node", display_name="Test"),
            )
        ],
    )
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "griptape_nodes_library.json"
    manifest.write_text(json.dumps(schema.model_dump(mode="json")), encoding="utf-8")
    return manifest


@pytest.fixture(autouse=True)
def _clean_registry() -> Generator[None, None, None]:
    LibraryRegistry._clear()
    yield
    LibraryRegistry._clear()


@pytest.fixture
def manifest(engine: Engine, tmp_path: Path) -> Path:
    """A library manifest listed in libraries_to_register, with the sandbox scan kept in the test's directory."""
    manifest = _write_manifest(tmp_path / "test_library")
    config = engine.config_manager
    config.set_config_value(LIBRARIES_TO_REGISTER_KEY, [str(manifest)])
    config.set_config_value("sandbox_library_directory", str(tmp_path / "sandbox"))
    return manifest


class TestMetadataCarriesLoadOutcome:
    @pytest.mark.asyncio
    async def test_a_library_that_failed_to_load_reports_why(self, engine: Engine, manifest: Path) -> None:
        library_info = LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.FAILURE,
            fitness=LibraryManager.LibraryFitness.UNUSABLE,
            library_path=str(manifest),
            is_sandbox=False,
            library_name=LIBRARY_NAME,
            library_version="1.0.0",
            problems=[DependencyInstallationFailedProblem(error_details="no wheel for this platform")],
            execution_env_failure="its execution dependencies could not be installed.",
        )
        engine.library_manager._library_file_path_to_info[str(manifest)] = library_info

        result = await engine.ahandle_request(LoadMetadataForAllLibrariesRequest())

        assert isinstance(result, LoadMetadataForAllLibrariesResultSuccess)
        [entry] = [entry for entry in result.successful_libraries if entry.file_path == str(manifest)]
        assert entry.is_registered is False
        assert entry.lifecycle_state == "failure"
        assert entry.fitness == "UNUSABLE"
        assert entry.problems is not None
        assert "no wheel for this platform" in entry.problems
        assert entry.execution_env_failure == "its execution dependencies could not be installed."

    @pytest.mark.asyncio
    async def test_a_library_the_engine_has_not_tried_reports_nothing(self, engine: Engine, manifest: Path) -> None:
        """Added since the last load: waiting for a refresh, not failed."""
        result = await engine.ahandle_request(LoadMetadataForAllLibrariesRequest())

        assert isinstance(result, LoadMetadataForAllLibrariesResultSuccess)
        [entry] = [entry for entry in result.successful_libraries if entry.file_path == str(manifest)]
        assert entry.lifecycle_state is None
        assert entry.fitness is None
        assert entry.problems is None
        assert entry.execution_env_failure is None

    @pytest.mark.asyncio
    async def test_a_sandbox_that_failed_to_load_reports_why(self, engine: Engine, tmp_path: Path) -> None:
        """The sandbox entry is built outside the configured-library loop and is stamped all the same."""
        sandbox_manifest = _write_manifest(tmp_path / "sandbox")
        engine.config_manager.set_config_value("sandbox_library_directory", str(tmp_path / "sandbox"))
        engine.library_manager._library_file_path_to_info[str(sandbox_manifest)] = LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.FAILURE,
            fitness=LibraryManager.LibraryFitness.UNUSABLE,
            library_path=str(sandbox_manifest),
            is_sandbox=True,
            library_name=LIBRARY_NAME,
            problems=[DependencyInstallationFailedProblem(error_details="no wheel for this platform")],
        )

        result = await engine.ahandle_request(LoadMetadataForAllLibrariesRequest())

        assert isinstance(result, LoadMetadataForAllLibrariesResultSuccess)
        [entry] = [entry for entry in result.successful_libraries if entry.file_path == str(sandbox_manifest)]
        assert entry.lifecycle_state == "failure"
        assert entry.problems is not None
        assert "no wheel for this platform" in entry.problems
