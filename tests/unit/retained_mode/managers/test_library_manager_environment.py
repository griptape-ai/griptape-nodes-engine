"""LibraryManager under an externally managed environment.

A fake environment stands in for whatever tool prepared it: library manifests written to a temporary
directory and listed on GTN_LIBRARY_PATHS, and GTN_CONFIG_LIBRARY__DEPENDENCY_SOURCE set the way a
launcher would set it. The unit-test conftest clears these variables first, so nothing here reads the
environment the suite happens to run in.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Protocol
from unittest.mock import AsyncMock

import pytest

from griptape_nodes.node_library.library_declarations import LibraryDependencyDeclaration
from griptape_nodes.node_library.library_registry import (
    CategoryDefinition,
    Dependencies,
    LibraryMetadata,
    LibraryRegistry,
    LibrarySchema,
    NodeDefinition,
    NodeMetadata,
)
from griptape_nodes.retained_mode.events.library_events import (
    DownloadLibraryRequest,
    DownloadLibraryResultFailure,
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultFailure,
    SyncLibrariesRequest,
    SyncLibrariesResultFailure,
)
from griptape_nodes.retained_mode.managers.external_environment import LIBRARY_PATHS_ENV_VAR
from griptape_nodes.retained_mode.managers.fitness_problems.libraries import (
    LibraryDependencyProblem,
    LibraryNotProvidedByEnvironmentProblem,
)
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager
from griptape_nodes.retained_mode.managers.settings import LIBRARIES_TO_DOWNLOAD_KEY, LIBRARIES_TO_REGISTER_KEY

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine

DEPENDENCY_URL = "https://github.com/example/griptape-nodes-library-openexr"

_NODE_SOURCE = """
from griptape_nodes.exe_types.node_types import BaseNode


class {class_name}(BaseNode):
    def process(self):
        return None
"""


def _write_library(
    directory: Path,
    name: str,
    *,
    declarations: list[LibraryDependencyDeclaration] | None = None,
    dependencies: Dependencies | None = None,
) -> Path:
    """Write a manifest with one node, since a library that loads no nodes is UNUSABLE."""
    class_name = "".join(name.split()) + "Node"
    schema = LibrarySchema(
        name=name,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="test",
            description=f"{name} manifest",
            library_version="1.0.0",
            engine_version="0.98.0",
            tags=[],
            declarations=list(declarations or []),
            dependencies=dependencies,
        ),
        categories=[{"Test": CategoryDefinition(title="Test", description="test", color="#000", icon="Folder")}],
        nodes=[
            NodeDefinition(
                class_name=class_name,
                file_path="node.py",
                metadata=NodeMetadata(category="Test", description="test node", display_name=name),
            )
        ],
    )
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "node.py").write_text(_NODE_SOURCE.format(class_name=class_name), encoding="utf-8")
    manifest = directory / "griptape_nodes_library.json"
    manifest.write_text(json.dumps(schema.model_dump(mode="json")), encoding="utf-8")
    return manifest


class Configure(Protocol):
    def __call__(
        self,
        *,
        environment_paths: list[Path],
        registered: list[Path] | None = None,
        environment_mode: bool,
        downloads: list[str] | None = None,
    ) -> None: ...


@pytest.fixture(autouse=True)
def _clean_registry() -> Generator[None, None, None]:
    LibraryRegistry._clear()
    yield
    LibraryRegistry._clear()


@pytest.fixture
def configure(engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Configure:
    """Apply a fake environment and config, then reload config the way a fresh engine would read it."""

    def apply(
        *,
        environment_paths: list[Path],
        registered: list[Path] | None = None,
        environment_mode: bool,
        downloads: list[str] | None = None,
    ) -> None:
        monkeypatch.setenv(LIBRARY_PATHS_ENV_VAR, os.pathsep.join(str(path) for path in environment_paths))
        if environment_mode:
            monkeypatch.setenv("GTN_CONFIG_LIBRARY__DEPENDENCY_SOURCE", "environment")
        config = engine.config_manager
        config.set_config_value(LIBRARIES_TO_REGISTER_KEY, [str(path) for path in registered or []])
        config.set_config_value(LIBRARIES_TO_DOWNLOAD_KEY, list(downloads or []))
        config.set_config_value("sandbox_library_directory", str(tmp_path / "sandbox"))
        config.load_configs()

    return apply


def _info_for(library_manager: LibraryManager, manifest: Path) -> LibraryManager.LibraryInfo:
    return library_manager.get_library_info_for_attempted_load(str(manifest))


class TestDiscoveryOrder:
    @pytest.mark.asyncio
    async def test_environment_libraries_come_before_configured_ones(
        self, engine: Engine, configure: Configure, tmp_path: Path
    ) -> None:
        configured = _write_library(tmp_path / "config" / "a_lib", "A Library")
        from_environment = [
            _write_library(tmp_path / "env" / "z_lib", "Z Library"),
            _write_library(tmp_path / "env" / "m_lib", "M Library"),
        ]
        configure(environment_paths=from_environment, registered=[configured], environment_mode=False)

        entries = await engine.library_manager._discover_library_files()

        assert [entry.registration.path for entry in entries] == [str(path) for path in [*from_environment, configured]]
        assert [entry.from_environment for entry in entries] == [True, True, False]
        # The GUI finds where each library came from by the verbatim entry it was listed under.
        assert entries[0].registered_path == str(from_environment[0])

    @pytest.mark.asyncio
    async def test_a_directory_entry_is_scanned_like_a_libraries_to_register_folder(
        self, engine: Engine, configure: Configure, tmp_path: Path
    ) -> None:
        manifest = _write_library(tmp_path / "env" / "pkg" / "nested", "Nested Library")
        configure(environment_paths=[tmp_path / "env" / "pkg"], environment_mode=True)

        entries = await engine.library_manager._discover_library_files()

        assert [entry.registration.path for entry in entries] == [str(manifest)]

    @pytest.mark.asyncio
    async def test_venv_mode_still_loads_configured_libraries(
        self, engine: Engine, configure: Configure, tmp_path: Path
    ) -> None:
        configured = _write_library(tmp_path / "config" / "a_lib", "A Library")
        from_environment = _write_library(tmp_path / "env" / "b_lib", "B Library")
        configure(environment_paths=[from_environment], registered=[configured], environment_mode=False)

        await engine.library_manager.load_all_libraries_from_config()

        assert set(LibraryRegistry.list_libraries()) >= {"A Library", "B Library"}


class TestEnvironmentModeRefusesOtherLibraries:
    @pytest.mark.asyncio
    async def test_only_environment_libraries_load(self, engine: Engine, configure: Configure, tmp_path: Path) -> None:
        configured = _write_library(tmp_path / "config" / "a_lib", "A Library")
        from_environment = _write_library(tmp_path / "env" / "b_lib", "B Library")
        configure(environment_paths=[from_environment], registered=[configured], environment_mode=True)
        library_manager = engine.library_manager

        await library_manager.load_all_libraries_from_config()

        assert "B Library" in LibraryRegistry.list_libraries()
        assert "A Library" not in LibraryRegistry.list_libraries()
        refused = _info_for(library_manager, configured)
        assert refused.fitness == LibraryManager.LibraryFitness.UNUSABLE
        assert refused.library_name == "A Library"
        assert refused.registered_path == str(configured)
        assert any(isinstance(problem, LibraryNotProvidedByEnvironmentProblem) for problem in refused.problems)
        problems = library_manager.collate_problems_for_lib_info(refused)
        assert problems is not None
        assert "not one of them" in problems

    @pytest.mark.asyncio
    async def test_the_sandbox_is_reported_and_not_written_to(
        self, engine: Engine, configure: Configure, tmp_path: Path
    ) -> None:
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        (sandbox / "my_node.py").write_text("x = 1\n", encoding="utf-8")
        configure(environment_paths=[], environment_mode=True)
        library_manager = engine.library_manager

        await library_manager.load_all_libraries_from_config()

        sandbox_manifest = sandbox / LibraryManager.LIBRARY_CONFIG_FILENAME
        assert not sandbox_manifest.exists()
        refused = _info_for(library_manager, sandbox_manifest)
        assert refused.is_sandbox
        assert any(isinstance(problem, LibraryNotProvidedByEnvironmentProblem) for problem in refused.problems)

    @pytest.mark.asyncio
    async def test_registering_a_library_by_path_outside_the_environment_fails(
        self, engine: Engine, configure: Configure, tmp_path: Path
    ) -> None:
        outside = _write_library(tmp_path / "elsewhere" / "c_lib", "C Library")
        configure(environment_paths=[], environment_mode=True)
        library_manager = engine.library_manager
        await library_manager.load_all_libraries_from_config()

        result = await library_manager.register_library_from_file_request(
            RegisterLibraryFromFileRequest(file_path=str(outside))
        )

        assert isinstance(result, RegisterLibraryFromFileResultFailure)
        assert LIBRARY_PATHS_ENV_VAR in str(result.result_details)
        assert "C Library" not in LibraryRegistry.list_libraries()

    @pytest.mark.asyncio
    async def test_the_same_manifest_listed_in_both_places_loads_once_without_a_problem(
        self, engine: Engine, configure: Configure, tmp_path: Path
    ) -> None:
        manifest = _write_library(tmp_path / "shared" / "d_lib", "D Library")
        configure(environment_paths=[manifest], registered=[manifest], environment_mode=True)
        library_manager = engine.library_manager

        await library_manager.load_all_libraries_from_config()

        info = _info_for(library_manager, manifest)
        assert info.lifecycle_state == LibraryManager.LibraryLifecycleState.LOADED
        assert not any(isinstance(problem, LibraryNotProvidedByEnvironmentProblem) for problem in info.problems)

    @pytest.mark.asyncio
    async def test_configured_entries_are_left_in_the_artist_config(
        self, engine: Engine, configure: Configure, tmp_path: Path
    ) -> None:
        configured = _write_library(tmp_path / "config" / "a_lib", "A Library")
        configure(environment_paths=[], registered=[configured], environment_mode=True)

        await engine.library_manager.load_all_libraries_from_config()

        assert engine.config_manager.get_config_value(LIBRARIES_TO_REGISTER_KEY) == [str(configured)]


class TestEnvironmentModeNeverDownloadsOrBuilds:
    @pytest.mark.asyncio
    async def test_libraries_to_download_are_not_downloaded(
        self, engine: Engine, configure: Configure, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        configure(environment_paths=[], environment_mode=True, downloads=["https://github.com/example/some-library"])
        library_manager = engine.library_manager
        downloads = AsyncMock()
        provisions = AsyncMock()
        monkeypatch.setattr(library_manager, "_download_libraries_from_git_urls", downloads)
        monkeypatch.setattr(library_manager, "_provision_one_library", provisions)

        await library_manager._ensure_libraries_from_config()
        await library_manager.load_all_libraries_from_config()

        downloads.assert_not_awaited()
        provisions.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_virtual_environment_is_built(
        self, engine: Engine, configure: Configure, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manifest = _write_library(
            tmp_path / "env" / "e_lib",
            "E Library",
            dependencies=Dependencies(pip_dependencies=["some-edit-dep"], pip_dependencies_exec=["some-exec-dep"]),
        )
        # Left over from an earlier run in venv mode; it must not front the environment's packages.
        (manifest.parent / ".venv-exec").mkdir()
        configure(environment_paths=[manifest], environment_mode=True)
        library_manager = engine.library_manager
        init_venv = AsyncMock()
        install = AsyncMock()
        monkeypatch.setattr(library_manager, "_init_library_venv", init_venv)
        monkeypatch.setattr(library_manager, "_install_dependency_set", install)

        await library_manager.load_all_libraries_from_config()

        assert "E Library" in LibraryRegistry.list_libraries()
        init_venv.assert_not_awaited()
        install.assert_not_awaited()
        assert not (manifest.parent / ".venv").exists()
        assert (manifest.parent / ".venv-exec").exists()
        assert library_manager.execution_site_packages("E Library") is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("request_payload", "failure_type"),
        [
            (DownloadLibraryRequest(git_url="https://github.com/example/some-library"), DownloadLibraryResultFailure),
            (SyncLibrariesRequest(), SyncLibrariesResultFailure),
        ],
    )
    async def test_library_changes_are_refused(
        self, engine: Engine, configure: Configure, request_payload: object, failure_type: type
    ) -> None:
        configure(environment_paths=[], environment_mode=True)

        result = await engine.ahandle_request(request_payload)  # type: ignore[arg-type]

        assert isinstance(result, failure_type)
        assert "environment" in str(result.result_details)


class TestLibraryDependencies:
    """In environment mode a library dependency is satisfied only by a library the environment provides."""

    @pytest.mark.asyncio
    async def test_a_dependency_the_environment_provides_is_satisfied(
        self, engine: Engine, configure: Configure, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Named the way a package tool normalizes the repository name, not the repository's own spelling.
        dependency = _write_library(tmp_path / "env" / "griptape_nodes_library_openexr", "OpenEXR Library")
        main = _write_library(
            tmp_path / "env" / "main_lib",
            "Main Library",
            declarations=[LibraryDependencyDeclaration(url=DEPENDENCY_URL)],
        )
        configure(environment_paths=[dependency, main], environment_mode=True)
        library_manager = engine.library_manager
        download = AsyncMock()
        monkeypatch.setattr(library_manager, "download_library_request", download)

        await library_manager.load_all_libraries_from_config()

        download.assert_not_awaited()
        main_info = _info_for(library_manager, main)
        assert not any(isinstance(problem, LibraryDependencyProblem) for problem in main_info.problems)
        assert main_info.fitness == LibraryManager.LibraryFitness.GOOD

    @pytest.mark.asyncio
    async def test_a_configured_copy_does_not_satisfy_it_and_nothing_is_downloaded(
        self, engine: Engine, configure: Configure, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        configured_dependency = _write_library(
            tmp_path / "config" / "griptape-nodes-library-openexr", "OpenEXR Library"
        )
        main = _write_library(
            tmp_path / "env" / "main_lib",
            "Main Library",
            declarations=[LibraryDependencyDeclaration(url=DEPENDENCY_URL)],
        )
        configure(environment_paths=[main], registered=[configured_dependency], environment_mode=True)
        library_manager = engine.library_manager
        download = AsyncMock()
        monkeypatch.setattr(library_manager, "download_library_request", download)

        await library_manager.load_all_libraries_from_config()

        download.assert_not_awaited()
        assert "OpenEXR Library" not in LibraryRegistry.list_libraries()
        main_info = _info_for(library_manager, main)
        dependency_problems = [p for p in main_info.problems if isinstance(p, LibraryDependencyProblem)]
        assert [problem.dependency_name for problem in dependency_problems] == [DEPENDENCY_URL]
        assert "does not provide it" in dependency_problems[0].error_message
        assert main_info.fitness == LibraryManager.LibraryFitness.FLAWED
