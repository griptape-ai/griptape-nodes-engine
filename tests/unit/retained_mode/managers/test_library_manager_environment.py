"""LibraryManager under an externally managed environment.

A fake environment stands in for whatever tool prepared it: library manifests written to a temporary
directory and listed on GTN_LIBRARY_PATHS, and GTN_CONFIG_LIBRARY__PROVISIONED_BY set the way a
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
from griptape_nodes.retained_mode.events.config_events import (
    GetConfigValueRequest,
    GetConfigValueResultSuccess,
)
from griptape_nodes.retained_mode.events.library_events import (
    CheckLibraryUpdateRequest,
    CheckLibraryUpdateResultSuccess,
    DownloadLibraryRequest,
    DownloadLibraryResultFailure,
    LoadMetadataForAllLibrariesRequest,
    LoadMetadataForAllLibrariesResultSuccess,
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultFailure,
    RegisterLibraryFromRequirementSpecifierRequest,
    RegisterLibraryFromRequirementSpecifierResultFailure,
    RegisterSandboxNodeFromSourceRequest,
    RegisterSandboxNodeFromSourceResultFailure,
    SwitchLibraryRefRequest,
    SwitchLibraryRefResultFailure,
    SyncLibrariesRequest,
    SyncLibrariesResultFailure,
    UnloadLibraryFromRegistryRequest,
    UpdateLibraryRequest,
    UpdateLibraryResultFailure,
)
from griptape_nodes.retained_mode.managers.external_environment import LIBRARY_PATHS_ENV_VAR
from griptape_nodes.retained_mode.managers.fitness_problems.libraries import (
    LibraryDependencyProblem,
    LibraryNotProvidedByEnvironmentProblem,
)
from griptape_nodes.retained_mode.managers.library.managed_environment import LibrariesProvidedByEnvironmentError
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
    ) -> None:
        """Configure the engine's libraries and provisioner for one test."""


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
            monkeypatch.setenv("GTN_CONFIG_LIBRARY__PROVISIONED_BY", "environment")
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

        entries = await engine.library_manager.discovery.discover_library_files()

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

        entries = await engine.library_manager.discovery.discover_library_files()

        assert [entry.registration.path for entry in entries] == [str(manifest)]

    @pytest.mark.asyncio
    async def test_an_entry_with_no_library_is_skipped_with_a_warning(
        self, engine: Engine, configure: Configure, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        manifest = _write_library(tmp_path / "env" / "b_lib", "B Library")
        missing = tmp_path / "env" / "not_there" / "griptape_nodes_library.json"
        configure(environment_paths=[missing, manifest], environment_mode=True)

        entries = await engine.library_manager.discovery.discover_library_files()

        assert [entry.registration.path for entry in entries] == [str(manifest)]
        assert str(missing) in caplog.text
        assert LIBRARY_PATHS_ENV_VAR in caplog.text

    @pytest.mark.asyncio
    async def test_engine_mode_still_loads_configured_libraries(
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
        problems = library_manager.catalog.collate_problems_for_lib_info(refused)
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
    async def test_a_listed_library_registers_through_a_symlinked_path(
        self, engine: Engine, configure: Configure, tmp_path: Path
    ) -> None:
        manifest = _write_library(tmp_path / "env" / "d_lib", "D Library")
        link = tmp_path / "linked_env"
        link.symlink_to(tmp_path / "env", target_is_directory=True)
        configure(environment_paths=[manifest], environment_mode=True)
        library_manager = engine.library_manager
        await library_manager.load_all_libraries_from_config()
        library_manager.registration.unload_library_from_registry_request(
            UnloadLibraryFromRegistryRequest(library_name="D Library")
        )

        result = await library_manager.registration.register_library_from_file_request(
            RegisterLibraryFromFileRequest(file_path=str(link / "d_lib" / manifest.name))
        )

        assert not isinstance(result, RegisterLibraryFromFileResultFailure), result.result_details
        assert "D Library" in LibraryRegistry.list_libraries()

    @pytest.mark.asyncio
    async def test_a_listed_library_registers_by_path_before_any_discovery(
        self, engine: Engine, configure: Configure, tmp_path: Path
    ) -> None:
        manifest = _write_library(tmp_path / "env" / "e_lib", "E Library")
        configure(environment_paths=[manifest], environment_mode=True)

        result = await engine.library_manager.registration.register_library_from_file_request(
            RegisterLibraryFromFileRequest(file_path=str(manifest))
        )

        assert not isinstance(result, RegisterLibraryFromFileResultFailure), result.result_details
        assert "E Library" in LibraryRegistry.list_libraries()

    @pytest.mark.asyncio
    async def test_a_project_template_cannot_change_the_environment_libraries(
        self, engine: Engine, configure: Configure, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provided = _write_library(tmp_path / "env" / "f_lib", "F Library")
        from_project = _write_library(tmp_path / "project" / "g_lib", "G Library")
        configure(environment_paths=[provided], environment_mode=True)
        # What _apply_project_env leaves behind: the project's value in os.environ, and the
        # startup value in the snapshot get_pre_project_environ restores.
        monkeypatch.setenv(LIBRARY_PATHS_ENV_VAR, str(from_project))
        monkeypatch.setattr(engine.project_manager, "_applied_env_snapshot", {LIBRARY_PATHS_ENV_VAR: str(provided)})

        entries = await engine.library_manager.discovery.discover_library_files()

        assert [entry.registration.path for entry in entries] == [str(provided)]

    @pytest.mark.asyncio
    async def test_registering_a_library_by_path_outside_the_environment_fails(
        self, engine: Engine, configure: Configure, tmp_path: Path
    ) -> None:
        outside = _write_library(tmp_path / "elsewhere" / "c_lib", "C Library")
        configure(environment_paths=[], environment_mode=True)
        library_manager = engine.library_manager
        await library_manager.load_all_libraries_from_config()

        result = await library_manager.registration.register_library_from_file_request(
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
        monkeypatch.setattr(library_manager.provisioning, "download_libraries_from_git_urls", downloads)
        monkeypatch.setattr(library_manager.provisioning, "_provision_one_library", provisions)

        await library_manager.provisioning.ensure_libraries_from_config()
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
        # Left over from an earlier run provisioned by the engine; it must not front the environment's packages.
        (manifest.parent / ".venv-exec").mkdir()
        configure(environment_paths=[manifest], environment_mode=True)
        library_manager = engine.library_manager
        init_venv = AsyncMock()
        install = AsyncMock()
        monkeypatch.setattr(library_manager.environment, "init_library_venv", init_venv)
        monkeypatch.setattr(library_manager.dependencies, "_install_dependency_set", install)

        await library_manager.load_all_libraries_from_config()

        assert "E Library" in LibraryRegistry.list_libraries()
        init_venv.assert_not_awaited()
        install.assert_not_awaited()
        assert not (manifest.parent / ".venv").exists()
        assert (manifest.parent / ".venv-exec").exists()
        assert library_manager.environment.execution_site_packages("E Library") is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("request_payload", "failure_type"),
        [
            (DownloadLibraryRequest(git_url="https://github.com/example/some-library"), DownloadLibraryResultFailure),
            (SyncLibrariesRequest(), SyncLibrariesResultFailure),
            (
                RegisterLibraryFromRequirementSpecifierRequest(requirement_specifier="griptape-nodes-library-x"),
                RegisterLibraryFromRequirementSpecifierResultFailure,
            ),
            (UpdateLibraryRequest(library_name="B Library"), UpdateLibraryResultFailure),
            (SwitchLibraryRefRequest(library_name="B Library", ref_name="main"), SwitchLibraryRefResultFailure),
        ],
    )
    async def test_library_changes_are_refused(
        self, engine: Engine, configure: Configure, request_payload: object, failure_type: type
    ) -> None:
        configure(environment_paths=[], environment_mode=True)

        result = await engine.ahandle_request(request_payload)  # type: ignore[arg-type]

        assert isinstance(result, failure_type)
        assert "environment" in str(result.result_details)


class TestSharedHelpersRefuseInEnvironmentMode:
    """The helpers every download, build, install, and git path goes through refuse on their own.

    Each handler has its own environment-mode check; these guard the work itself, so a caller that
    forgets its check still cannot reach uv, pip, or git. Every side effect behind a guard is
    replaced with one that fails the test if it runs.
    """

    @staticmethod
    def _must_not_run(*_: object, **__: object) -> None:
        msg = "an environment-mode guard let the work through"
        raise AssertionError(msg)

    @pytest.mark.asyncio
    async def test_building_a_library_environment_is_refused(
        self, engine: Engine, configure: Configure, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        configure(environment_paths=[], environment_mode=True)
        monkeypatch.setattr(
            "griptape_nodes.retained_mode.managers.library.environment.subprocess_run", self._must_not_run
        )

        with pytest.raises(LibrariesProvidedByEnvironmentError, match="build a library environment"):
            await engine.library_manager.environment.init_library_venv(tmp_path / ".venv")

        assert not (tmp_path / ".venv").exists()

    @pytest.mark.asyncio
    async def test_installing_library_packages_is_refused(
        self, engine: Engine, configure: Configure, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        configure(environment_paths=[], environment_mode=True)
        monkeypatch.setattr(
            "griptape_nodes.retained_mode.managers.library.dependencies.subprocess_run", self._must_not_run
        )

        with pytest.raises(LibrariesProvidedByEnvironmentError, match="install library packages"):
            await engine.library_manager.dependencies.install_under_engine_floors(
                ["uv", "pip", "install", "some-dep"], tmp_path / "python", capture_output=True
            )

    @pytest.mark.asyncio
    async def test_downloading_a_library_is_refused_for_every_url(
        self, engine: Engine, configure: Configure, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        configure(environment_paths=[], environment_mode=True)
        monkeypatch.setattr(engine, "ahandle_request", self._must_not_run)
        urls = ["https://github.com/example/one", "https://github.com/example/two@main"]

        results = await engine.library_manager.provisioning.download_libraries_from_git_urls(urls)

        assert set(results) == set(urls)
        for url, result in results.items():
            assert result["success"] is False
            assert result["library_name"] is None
            assert url in result["error"]
            assert "environment" in result["error"]

    @pytest.mark.asyncio
    async def test_a_git_operation_on_a_library_is_refused(
        self, engine: Engine, configure: Configure, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        configure(environment_paths=[], environment_mode=True)
        monkeypatch.setattr(LibraryRegistry, "get_library", self._must_not_run)

        result = await engine.library_manager.git_operations._validate_and_prepare_library_for_git_operation(
            library_name="B Library", failure_result_class=UpdateLibraryResultFailure, operation_description="update"
        )

        assert isinstance(result, UpdateLibraryResultFailure)
        assert "B Library" in str(result.result_details)
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
        monkeypatch.setattr(library_manager.git_operations, "download_library_request", download)

        await library_manager.load_all_libraries_from_config()

        download.assert_not_awaited()
        main_info = _info_for(library_manager, main)
        assert not any(isinstance(problem, LibraryDependencyProblem) for problem in main_info.problems)
        assert main_info.fitness == LibraryManager.LibraryFitness.GOOD

    @pytest.mark.asyncio
    async def test_a_provided_dependency_that_failed_to_load_is_named_as_failed(
        self, engine: Engine, configure: Configure, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The environment does provide it, so the artist is pointed at its load failure, not told to add it.
        dependency = _write_library(tmp_path / "env" / "griptape_nodes_library_openexr", "OpenEXR Library")
        dependency.write_text("{ not json", encoding="utf-8")
        main = _write_library(
            tmp_path / "env" / "main_lib",
            "Main Library",
            declarations=[LibraryDependencyDeclaration(url=DEPENDENCY_URL)],
        )
        configure(environment_paths=[dependency, main], environment_mode=True)
        library_manager = engine.library_manager
        download = AsyncMock()
        monkeypatch.setattr(library_manager.git_operations, "download_library_request", download)

        await library_manager.load_all_libraries_from_config()

        download.assert_not_awaited()
        main_info = _info_for(library_manager, main)
        dependency_problems = [p for p in main_info.problems if isinstance(p, LibraryDependencyProblem)]
        assert [problem.dependency_name for problem in dependency_problems] == [DEPENDENCY_URL]
        assert "failed to load" in dependency_problems[0].error_message
        assert "does not provide it" not in dependency_problems[0].error_message
        assert main_info.fitness == LibraryManager.LibraryFitness.FLAWED

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
        monkeypatch.setattr(library_manager.git_operations, "download_library_request", download)

        await library_manager.load_all_libraries_from_config()

        download.assert_not_awaited()
        assert "OpenEXR Library" not in LibraryRegistry.list_libraries()
        main_info = _info_for(library_manager, main)
        dependency_problems = [p for p in main_info.problems if isinstance(p, LibraryDependencyProblem)]
        assert [problem.dependency_name for problem in dependency_problems] == [DEPENDENCY_URL]
        assert "does not provide it" in dependency_problems[0].error_message
        assert main_info.fitness == LibraryManager.LibraryFitness.FLAWED

    @pytest.mark.asyncio
    async def test_a_disabled_configured_copy_does_not_satisfy_it(
        self, engine: Engine, configure: Configure, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A disabled entry is recorded without a problem, so it must not count as the dependency.
        configured_dependency = _write_library(
            tmp_path / "config" / "griptape-nodes-library-openexr", "OpenEXR Library"
        )
        main = _write_library(
            tmp_path / "env" / "main_lib",
            "Main Library",
            declarations=[LibraryDependencyDeclaration(url=DEPENDENCY_URL)],
        )
        configure(environment_paths=[main], environment_mode=True)
        engine.config_manager.set_config_value(
            LIBRARIES_TO_REGISTER_KEY, [{"path": str(configured_dependency), "enabled": False}]
        )
        library_manager = engine.library_manager
        monkeypatch.setattr(library_manager.git_operations, "download_library_request", AsyncMock())

        await library_manager.load_all_libraries_from_config()

        main_info = _info_for(library_manager, main)
        dependency_problems = [p for p in main_info.problems if isinstance(p, LibraryDependencyProblem)]
        assert [problem.dependency_name for problem in dependency_problems] == [DEPENDENCY_URL]
        assert main_info.fitness == LibraryManager.LibraryFitness.FLAWED


class TestWhatTheEditorReads:
    """The editor shows environment libraries from the metadata listing and reads the mode as a setting."""

    @pytest.mark.asyncio
    async def test_environment_libraries_are_listed_under_their_environment_entry(
        self, engine: Engine, configure: Configure, tmp_path: Path
    ) -> None:
        configured = _write_library(tmp_path / "config" / "a_lib", "A Library")
        from_environment = _write_library(tmp_path / "env" / "b_lib", "B Library")
        configure(environment_paths=[from_environment], registered=[configured], environment_mode=True)
        library_manager = engine.library_manager
        await library_manager.load_all_libraries_from_config()

        result = await library_manager.metadata_loading.load_metadata_for_all_libraries_request(
            LoadMetadataForAllLibrariesRequest()
        )

        assert isinstance(result, LoadMetadataForAllLibrariesResultSuccess)
        by_name = {entry.library_schema.name: entry for entry in result.successful_libraries}
        assert by_name["B Library"].registered_path == str(from_environment)
        assert by_name["B Library"].file_path == str(from_environment)
        assert by_name["B Library"].is_registered is True
        # A configured library is still listed, under its own config entry, and reads as not loaded.
        assert by_name["A Library"].registered_path == str(configured)
        assert by_name["A Library"].is_registered is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("environment_mode", "expected"), [(True, "environment"), (False, "engine")])
    async def test_provisioned_by_reads_as_plain_text(
        self, engine: Engine, configure: Configure, *, environment_mode: bool, expected: str
    ) -> None:
        configure(environment_paths=[], environment_mode=environment_mode)

        result = await engine.ahandle_request(GetConfigValueRequest(category_and_key="library.provisioned_by"))

        assert isinstance(result, GetConfigValueResultSuccess)
        assert result.value == expected
        assert json.loads(json.dumps(result.value)) == expected


class TestSameNameInBothPlaces:
    @pytest.mark.asyncio
    async def test_a_refused_copy_with_the_environment_library_s_name_reads_as_not_loaded(
        self, engine: Engine, configure: Configure, tmp_path: Path
    ) -> None:
        """is_registered is answered by name, which both copies share; only the environment's copy loaded."""
        configured = _write_library(tmp_path / "config" / "foo_lib", "Foo Library")
        from_environment = _write_library(tmp_path / "env" / "foo_lib", "Foo Library")
        configure(environment_paths=[from_environment], registered=[configured], environment_mode=True)
        library_manager = engine.library_manager
        await library_manager.load_all_libraries_from_config()

        result = await library_manager.metadata_loading.load_metadata_for_all_libraries_request(
            LoadMetadataForAllLibrariesRequest()
        )

        assert isinstance(result, LoadMetadataForAllLibrariesResultSuccess)
        by_path = {entry.registered_path: entry for entry in result.successful_libraries}
        assert by_path[str(from_environment)].is_registered is True
        assert by_path[str(configured)].is_registered is False
        # The name lookups everything else uses resolve to the environment's copy.
        info = library_manager.get_library_info_by_library_name("Foo Library")
        assert info is not None
        assert info.library_path == str(from_environment)


class TestNothingOutsideTheEnvironmentMixesIn:
    """Environment mode never adds a node type, a sandbox manifest, or an update the environment did not provide."""

    @pytest.mark.asyncio
    async def test_a_sandbox_node_cannot_be_added(self, engine: Engine, configure: Configure, tmp_path: Path) -> None:
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        source = sandbox / "my_node.py"
        source.write_text(_NODE_SOURCE.format(class_name="LooseNode"), encoding="utf-8")
        configure(environment_paths=[], environment_mode=True)

        result = await engine.ahandle_request(RegisterSandboxNodeFromSourceRequest(file_path=str(source)))

        assert isinstance(result, RegisterSandboxNodeFromSourceResultFailure)
        assert "environment" in str(result.result_details)
        assert LibraryManager.SANDBOX_LIBRARY_NAME not in LibraryRegistry.list_libraries()

    @pytest.mark.asyncio
    async def test_the_metadata_listing_does_not_scan_the_sandbox(
        self, engine: Engine, configure: Configure, tmp_path: Path
    ) -> None:
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        (sandbox / "my_node.py").write_text(_NODE_SOURCE.format(class_name="LooseNode"), encoding="utf-8")
        configure(environment_paths=[], environment_mode=True)

        result = await engine.library_manager.metadata_loading.load_metadata_for_all_libraries_request(
            LoadMetadataForAllLibrariesRequest()
        )

        assert isinstance(result, LoadMetadataForAllLibrariesResultSuccess)
        assert not (sandbox / LibraryManager.LIBRARY_CONFIG_FILENAME).exists()
        listed = [entry.library_schema.name for entry in result.successful_libraries]
        assert LibraryManager.SANDBOX_LIBRARY_NAME not in listed
        assert result.failed_libraries == []

    @pytest.mark.asyncio
    async def test_an_update_check_answers_without_git(
        self, engine: Engine, configure: Configure, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manifest = _write_library(tmp_path / "env" / "b_lib", "B Library")
        configure(environment_paths=[manifest], environment_mode=True)
        library_manager = engine.library_manager
        await library_manager.load_all_libraries_from_config()

        def no_git(*_: object, **__: object) -> None:
            msg = "the update check must not touch git in environment mode"
            raise AssertionError(msg)

        for name in ("is_monorepo", "get_git_remote", "get_git_info", "get_local_commit_sha"):
            monkeypatch.setattr(f"griptape_nodes.retained_mode.managers.library.git_operations.{name}", no_git)

        result = await library_manager.git_operations.check_library_update_request(
            CheckLibraryUpdateRequest(library_name="B Library")
        )

        assert isinstance(result, CheckLibraryUpdateResultSuccess)
        assert result.has_update is False
        assert result.current_version == result.latest_version == "1.0.0"
        assert "environment" in str(result.result_details)
