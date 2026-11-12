"""Integration tests for uninstalling a library: config entry, registry unload, directory delete.

Exercises the resolution and the real filesystem/config so the class of bug the client-side
implementation hit repeatedly -- guessing at a path the engine should have resolved -- cannot
recur here silently. See `UninstallLibraryRequest` and `PreviewUninstallLibraryRequest` in
`library_events.py` for the contract.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING

import anyio
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
    EnvironmentManagedBlocker,
    NotFoundBlocker,
    PreviewUninstallLibraryRequest,
    PreviewUninstallLibraryResultFailure,
    PreviewUninstallLibraryResultSuccess,
    SandboxLibraryBlocker,
    SharedDirectoryEntryRemaining,
    UninstallLibraryRequest,
    UninstallLibraryResultFailure,
    UninstallLibraryResultSuccess,
    UnmatchedEntryBlocker,
    UnsafePathBlocker,
    UnwritableConfigBlocker,
)
from griptape_nodes.retained_mode.managers.library.uninstall import _unsafe_install_directory
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager
from griptape_nodes.retained_mode.managers.settings import (
    LIBRARIES_TO_DOWNLOAD_KEY,
    LIBRARIES_TO_REGISTER_KEY,
)

if TYPE_CHECKING:
    from collections.abc import Generator

    from griptape_nodes.retained_mode.engine import Engine

# Both path flavors, exercised on every host. `Path` is the host's own flavor, so asserting
# through `Path` alone answers only half the question: these cases passed on Linux while a
# Windows host read `/home/user/libs/Nuke` (whose `str()` is `\home\user\libs\Nuke` there) as a
# filesystem root and refused an ordinary uninstall. Pinning the flavor keeps both answers
# honest wherever the suite runs.
_PATH_FLAVORS = [
    pytest.param(PurePosixPath, id="posix_flavor"),
    pytest.param(PureWindowsPath, id="windows_flavor"),
]


def _write_manifest(directory: Path, name: str, *, version: str = "1.0.0") -> Path:
    schema = LibrarySchema(
        name=name,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="test",
            description="test manifest",
            library_version=version,
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


def _track(
    library_manager: LibraryManager,
    manifest: Path,
    *,
    library_name: str,
    lifecycle_state: LibraryManager.LibraryLifecycleState = LibraryManager.LibraryLifecycleState.FAILURE,
    registered_path: str | None = None,
) -> LibraryManager.LibraryInfo:
    library_info = LibraryManager.LibraryInfo(
        lifecycle_state=lifecycle_state,
        fitness=LibraryManager.LibraryFitness.UNUSABLE,
        library_path=str(manifest),
        is_sandbox=False,
        library_name=library_name,
        library_version="1.0.0",
        registered_path=registered_path,
    )
    library_manager._library_file_path_to_info[str(manifest)] = library_info
    return library_info


def _register_in_registry(name: str) -> None:
    """Load the library into LibraryRegistry so it counts as loaded for the unload step."""
    LibraryRegistry.generate_new_library(
        library_data=LibrarySchema(
            name=name,
            library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
            metadata=LibraryMetadata(
                author="test", description="test", library_version="1.0.0", engine_version="0.98.0", tags=[]
            ),
            categories=[],
            nodes=[],
        )
    )


async def _preview(engine: Engine, library_name: str, manifest: Path) -> PreviewUninstallLibraryResultSuccess:
    result = await engine.ahandle_request(
        PreviewUninstallLibraryRequest(library_name=library_name, library_path=str(manifest))
    )
    assert isinstance(result, PreviewUninstallLibraryResultSuccess), result.result_details
    return result


async def _uninstall_after_preview(engine: Engine, library_name: str, manifest: Path):  # noqa: ANN202 (either result shape)
    """Preview then uninstall, as the editor does."""
    await _preview(engine, library_name, manifest)
    return await engine.ahandle_request(UninstallLibraryRequest(library_name=library_name, library_path=str(manifest)))


@pytest.fixture(autouse=True)
def _clean_registry() -> Generator[None, None, None]:
    LibraryRegistry._clear()
    yield
    LibraryRegistry._clear()


@pytest.fixture
def workspace(tmp_path: Path, engine: Engine) -> Path:
    """A temporary workspace directory, set as the engine's active workspace.

    Set through `workspace_directory` config, not the `workspace_path` property directly:
    `set_config_value` ends with `load_configs()`, which recomputes `workspace_path` from the
    config layers and would silently discard a property-only assignment on the next config write.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    engine.config_manager.set_config_value("workspace_directory", str(workspace))
    return workspace


class TestResolutionNotFound:
    @pytest.mark.asyncio
    async def test_unknown_library_fails(self, engine: Engine, workspace: Path) -> None:
        result = await engine.ahandle_request(
            UninstallLibraryRequest(
                library_name="Nope",
                library_path=str(workspace / "nope" / "griptape_nodes_library.json"),
            )
        )

        assert isinstance(result, UninstallLibraryResultFailure)
        assert isinstance(result.blocked_by, NotFoundBlocker)

    @pytest.mark.asyncio
    async def test_preview_of_unknown_library_fails(self, engine: Engine, workspace: Path) -> None:
        result = await engine.ahandle_request(
            PreviewUninstallLibraryRequest(
                library_name="Nope", library_path=str(workspace / "nope" / "griptape_nodes_library.json")
            )
        )

        assert isinstance(result, PreviewUninstallLibraryResultFailure)

    @pytest.mark.asyncio
    async def test_name_matching_a_different_copy_is_not_uninstalled(self, engine: Engine, workspace: Path) -> None:
        """`library_path` identifies the copy, so a path naming no tracked install is refused."""
        installed = _write_manifest(workspace / "libs" / "real", "Dup Library")
        _track(engine.library_manager, installed, library_name="Dup Library")
        other_path = workspace / "libs" / "imposter" / "griptape_nodes_library.json"

        result = await engine.ahandle_request(
            PreviewUninstallLibraryRequest(library_name="Dup Library", library_path=str(other_path))
        )

        assert isinstance(result, PreviewUninstallLibraryResultFailure)
        assert await anyio.Path(installed).exists()


class TestBlockedTargets:
    """Blockers are structured verdicts; the editor owns the sentence for each `kind`."""

    @pytest.mark.asyncio
    async def test_sandbox_library_is_blocked(self, engine: Engine, workspace: Path) -> None:
        manifest = _write_manifest(workspace / "sandbox", LibraryManager.SANDBOX_LIBRARY_NAME)
        library_info = LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
            fitness=LibraryManager.LibraryFitness.GOOD,
            library_path=str(manifest),
            is_sandbox=True,
            library_name=LibraryManager.SANDBOX_LIBRARY_NAME,
        )
        engine.library_manager._library_file_path_to_info[str(manifest)] = library_info

        preview = await _preview(engine, LibraryManager.SANDBOX_LIBRARY_NAME, manifest)

        assert isinstance(preview.blocked_by, SandboxLibraryBlocker)
        assert preview.blocked_by.kind == "sandbox_library"
        assert await anyio.Path(manifest).exists()

    @pytest.mark.asyncio
    async def test_environment_mode_is_blocked_and_touches_nothing(
        self, engine: Engine, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        library_dir = workspace / "libraries" / "test_lib"
        manifest = _write_manifest(library_dir, "Test Library")
        _track(engine.library_manager, manifest, library_name="Test Library")
        monkeypatch.setenv("GTN_CONFIG_LIBRARY__PROVISIONED_BY", "environment")
        engine.config_manager.load_configs()

        result = await _uninstall_after_preview(engine, "Test Library", manifest)

        assert isinstance(result, UninstallLibraryResultFailure)
        assert isinstance(result.blocked_by, EnvironmentManagedBlocker)
        assert await anyio.Path(manifest).exists()

    @pytest.mark.parametrize("flavor", _PATH_FLAVORS)
    @pytest.mark.parametrize(
        "unsafe_path",
        [
            pytest.param("C:\\", id="windows_drive_root"),
            pytest.param("C:/", id="windows_drive_root_forward_slash"),
            pytest.param("\\\\server\\share", id="unc_share"),
            pytest.param("\\\\server\\share\\lib", id="unc_share_subfolder"),
            pytest.param("\\\\?\\UNC\\server\\share", id="long_unc_share"),
            pytest.param("\\\\?\\unc\\server\\share", id="long_unc_share_lowercase_prefix"),
            pytest.param("/", id="posix_root"),
        ],
    )
    def test_unsafe_install_directory_is_refused(self, unsafe_path: str, flavor: type[PurePath]) -> None:
        """Direct unit coverage of the root/UNC safety check, independent of the real filesystem."""
        assert _unsafe_install_directory(flavor(unsafe_path)) is True

    @pytest.mark.parametrize("flavor", _PATH_FLAVORS)
    @pytest.mark.parametrize(
        "ordinary_path",
        [
            pytest.param("C:\\libs\\Nuke", id="windows_library_folder"),
            pytest.param("/home/user/libs/Nuke", id="posix_library_folder"),
            pytest.param("/libs", id="posix_single_segment"),
        ],
    )
    def test_ordinary_directory_is_not_flagged_unsafe(self, ordinary_path: str, flavor: type[PurePath]) -> None:
        assert _unsafe_install_directory(flavor(ordinary_path)) is False

    @pytest.mark.asyncio
    async def test_traversal_segment_in_register_entry_is_blocked(self, engine: Engine, workspace: Path) -> None:
        """`libs/Nuke/..` in a hand-typed entry is refused rather than resolved to the parent."""
        nuke_dir = workspace / "libs" / "Nuke"
        manifest = _write_manifest(nuke_dir, "Nuke Library")
        traversal_entry = str(workspace / "libs" / "Nuke" / "..")
        engine.config_manager.set_config_value(LIBRARIES_TO_REGISTER_KEY, [traversal_entry])
        _track(engine.library_manager, manifest, library_name="Nuke Library", registered_path=traversal_entry)

        preview = await _preview(engine, "Nuke Library", manifest)

        assert isinstance(preview.blocked_by, UnsafePathBlocker)
        assert preview.blocked_by.path == traversal_entry
        assert await anyio.Path(manifest).exists()


class TestManifestEntry:
    """A libraries_to_register entry naming the manifest file directly."""

    @pytest.mark.asyncio
    async def test_preview_matches_real_uninstall_target(self, engine: Engine, workspace: Path) -> None:
        library_dir = workspace / "libs" / "test_lib"
        manifest = _write_manifest(library_dir, "Test Library")
        engine.config_manager.set_config_value(LIBRARIES_TO_REGISTER_KEY, [str(manifest)])
        _track(engine.library_manager, manifest, library_name="Test Library", registered_path=str(manifest))

        preview = await _preview(engine, "Test Library", manifest)

        assert preview.install_directory == str(library_dir)
        assert preview.registered_in == "register"
        assert preview.blocked_by is None
        assert preview.remaining_registrations == []

    @pytest.mark.asyncio
    async def test_uninstall_removes_config_entry_unloads_and_deletes(self, engine: Engine, workspace: Path) -> None:
        library_dir = workspace / "libs" / "test_lib"
        manifest = _write_manifest(library_dir, "Test Library")
        engine.config_manager.set_config_value(LIBRARIES_TO_REGISTER_KEY, [str(manifest)])
        _track(
            engine.library_manager,
            manifest,
            library_name="Test Library",
            lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
            registered_path=str(manifest),
        )
        _register_in_registry("Test Library")

        result = await _uninstall_after_preview(engine, "Test Library", manifest)

        assert isinstance(result, UninstallLibraryResultSuccess), result.result_details
        assert result.config_entry_removed is True
        assert result.unloaded is True
        assert result.directory_deleted is True
        assert result.remaining_registrations == []
        assert not await anyio.Path(library_dir).exists()
        assert engine.config_manager.get_config_value(LIBRARIES_TO_REGISTER_KEY, default=[]) == []
        assert "Test Library" not in LibraryRegistry.list_libraries()

    @pytest.mark.asyncio
    async def test_relative_register_entry_resolves_against_workspace(self, engine: Engine, workspace: Path) -> None:
        """A relative libraries_to_register entry resolves to the same workspace-relative folder."""
        library_dir = workspace / "libs" / "relative_lib"
        manifest = _write_manifest(library_dir, "Relative Library")
        relative_entry = str(Path("libs") / "relative_lib" / "griptape_nodes_library.json")
        engine.config_manager.set_config_value(LIBRARIES_TO_REGISTER_KEY, [relative_entry])
        _track(engine.library_manager, manifest, library_name="Relative Library", registered_path=relative_entry)

        result = await _uninstall_after_preview(engine, "Relative Library", manifest)

        assert isinstance(result, UninstallLibraryResultSuccess), result.result_details
        assert result.install_directory == str(library_dir)
        assert not await anyio.Path(library_dir).exists()
        assert engine.config_manager.get_config_value(LIBRARIES_TO_REGISTER_KEY, default=[]) == []

    @pytest.mark.asyncio
    async def test_entry_nothing_in_config_matches_is_refused(self, engine: Engine, workspace: Path) -> None:
        """With no entry to remove, the uninstall refuses rather than deleting the files.

        Something registered this library that the engine cannot trace back to a setting, so
        deleting its folder would leave that registration pointing at nothing.
        """
        library_dir = workspace / "libs" / "orphan"
        manifest = _write_manifest(library_dir, "Orphan Library")
        engine.config_manager.set_config_value(LIBRARIES_TO_REGISTER_KEY, [])
        _track(engine.library_manager, manifest, library_name="Orphan Library")

        preview = await _preview(engine, "Orphan Library", manifest)
        result = await engine.ahandle_request(
            UninstallLibraryRequest(library_name="Orphan Library", library_path=str(manifest))
        )

        # The preview says so too, so the editor can disable the control before it is clicked.
        assert isinstance(preview.blocked_by, UnmatchedEntryBlocker)
        assert isinstance(result, UninstallLibraryResultFailure)
        assert isinstance(result.blocked_by, UnmatchedEntryBlocker)
        assert await anyio.Path(library_dir).exists()


class TestDirectoryEntryWithSiblings:
    """A libraries_to_register entry naming a directory that holds several manifests."""

    @pytest.mark.asyncio
    async def test_siblings_are_reported_and_directory_is_kept(self, engine: Engine, workspace: Path) -> None:
        libs_root = workspace / "libs"
        manifest = _write_manifest(libs_root / "lib_a", "Library A")
        sibling = _write_manifest(libs_root / "lib_b", "Library B")
        (libs_root / "lib_c").mkdir(parents=True)
        (libs_root / "lib_c" / "griptape_nodes_library.json").write_text("{not valid json", encoding="utf-8")

        engine.config_manager.set_config_value(LIBRARIES_TO_REGISTER_KEY, [str(libs_root)])
        _track(engine.library_manager, manifest, library_name="Library A", registered_path=str(libs_root))
        _track(engine.library_manager, sibling, library_name="Library B", registered_path=str(libs_root))

        result = await _uninstall_after_preview(engine, "Library A", manifest)

        assert isinstance(result, UninstallLibraryResultSuccess), result.result_details
        # Only Library A's own folder goes. The scan root, its config entry, and the siblings
        # living in their own folders under it are all left alone.
        assert result.directory_deleted is True
        assert not await anyio.Path(libs_root / "lib_a").exists()
        assert await anyio.Path(libs_root / "lib_b").exists()
        assert await anyio.Path(libs_root / "lib_c").exists()
        assert result.collateral_library_names == []
        assert result.config_entry_removed is False
        assert engine.config_manager.get_config_value(LIBRARIES_TO_REGISTER_KEY, default=[]) == [str(libs_root)]
        [remaining] = [e for e in result.remaining_registrations if isinstance(e, SharedDirectoryEntryRemaining)]
        assert remaining.entry_path == str(libs_root)
        # The entry registers every manifest under it, so the broken one counts too; with no
        # readable name it is reported by path.
        assert "Library B" in remaining.library_names
        assert str(libs_root / "lib_c" / "griptape_nodes_library.json") in remaining.library_names

    @pytest.mark.asyncio
    async def test_the_only_library_under_the_entry_reports_nothing_remaining(
        self, engine: Engine, workspace: Path
    ) -> None:
        """A scan root holding one library: no sibling to name, so no shared-entry verdict.

        The entry stays in config -- it is a scan root that may hold libraries again later --
        but with nothing left registering this library, the editor shows a clean uninstall
        rather than a sentence naming an empty list of siblings.
        """
        libs_root = workspace / "libs"
        manifest = _write_manifest(libs_root / "lib_a", "Library A")
        engine.config_manager.set_config_value(LIBRARIES_TO_REGISTER_KEY, [str(libs_root)])
        _track(engine.library_manager, manifest, library_name="Library A", registered_path=str(libs_root))

        result = await _uninstall_after_preview(engine, "Library A", manifest)

        assert isinstance(result, UninstallLibraryResultSuccess), result.result_details
        assert result.remaining_registrations == []
        assert result.directory_deleted is True
        assert not await anyio.Path(libs_root / "lib_a").exists()
        # The scan root itself survives, as does the entry naming it.
        assert await anyio.Path(libs_root).exists()
        assert engine.config_manager.get_config_value(LIBRARIES_TO_REGISTER_KEY, default=[]) == [str(libs_root)]

    @pytest.mark.asyncio
    async def test_preview_reports_the_same_collateral_as_uninstall(self, engine: Engine, workspace: Path) -> None:
        libs_root = workspace / "libs"
        manifest = _write_manifest(libs_root / "lib_a", "Library A")
        _write_manifest(libs_root / "lib_b", "Library B")
        engine.config_manager.set_config_value(LIBRARIES_TO_REGISTER_KEY, [str(libs_root)])
        _track(engine.library_manager, manifest, library_name="Library A", registered_path=str(libs_root))

        preview = await _preview(engine, "Library A", manifest)

        # The sibling lives in its own folder beside the target, so it is not collateral.
        assert preview.install_directory == str(libs_root / "lib_a")
        assert preview.collateral_library_names == []
        assert any(isinstance(e, SharedDirectoryEntryRemaining) for e in preview.remaining_registrations)


class TestDirectoryEntryNamingOneLibrary:
    """A libraries_to_register entry naming the library's OWN folder, not a scan root above it.

    The entry is a directory, so it classifies as a directory entry, but deleting the folder
    leaves it naming a path that no longer exists. It has to go with the folder.
    """

    @pytest.mark.asyncio
    async def test_entry_naming_the_deleted_folder_is_removed_with_it(self, engine: Engine, workspace: Path) -> None:
        lib_dir = workspace / "mylib"
        manifest = _write_manifest(lib_dir, "My Library")
        engine.config_manager.set_config_value(LIBRARIES_TO_REGISTER_KEY, [str(lib_dir)])
        _track(engine.library_manager, manifest, library_name="My Library", registered_path=str(lib_dir))

        result = await _uninstall_after_preview(engine, "My Library", manifest)

        assert isinstance(result, UninstallLibraryResultSuccess), result.result_details
        assert result.directory_deleted is True
        assert not await anyio.Path(lib_dir).exists()
        # Keeping the entry would leave config naming a folder that is gone, which is the
        # broken half-uninstalled state the whole request exists to avoid.
        assert result.config_entry_removed is True
        assert engine.config_manager.get_config_value(LIBRARIES_TO_REGISTER_KEY, default=[]) == []
        assert result.remaining_registrations == []

    @pytest.mark.asyncio
    async def test_entry_naming_a_folder_of_several_goes_too(self, engine: Engine, workspace: Path) -> None:
        """Two manifests inside the entry's own folder: the delete takes both, so the entry goes."""
        lib_dir = workspace / "bundle"
        manifest = _write_manifest(lib_dir, "Bundle Main")
        nested = _write_manifest(lib_dir / "extra", "Bundle Extra")
        engine.config_manager.set_config_value(LIBRARIES_TO_REGISTER_KEY, [str(lib_dir)])
        _track(engine.library_manager, manifest, library_name="Bundle Main", registered_path=str(lib_dir))
        _track(engine.library_manager, nested, library_name="Bundle Extra", registered_path=str(lib_dir))

        result = await _uninstall_after_preview(engine, "Bundle Main", manifest)

        assert isinstance(result, UninstallLibraryResultSuccess), result.result_details
        assert not await anyio.Path(lib_dir).exists()
        assert result.collateral_library_names == ["Bundle Extra"]
        # The sibling is deleted with the folder, so warning that the entry still registers it
        # would point the user at an entry that is also gone.
        assert result.config_entry_removed is True
        assert result.remaining_registrations == []


class TestDownloadProvisionedLibrary:
    """A libraries_to_download entry whose installed manifest is what the user clicked."""

    @pytest.mark.asyncio
    async def test_uninstall_removes_download_entry_and_deletes_repo_directory(
        self, engine: Engine, workspace: Path
    ) -> None:
        libraries_root = workspace / "libraries"
        repo_dir = libraries_root / "griptape-nodes-library-sample"
        manifest = _write_manifest(repo_dir, "Sample Library")
        engine.config_manager.set_config_value(
            LIBRARIES_TO_DOWNLOAD_KEY, ["griptape-ai/griptape-nodes-library-sample@stable"]
        )
        _track(engine.library_manager, manifest, library_name="Sample Library", registered_path=str(manifest))

        result = await _uninstall_after_preview(engine, "Sample Library", manifest)

        assert isinstance(result, UninstallLibraryResultSuccess), result.result_details
        assert result.registered_in == "download"
        assert result.install_directory == str(repo_dir)
        assert result.config_entry_removed is True
        assert not await anyio.Path(repo_dir).exists()
        assert engine.config_manager.get_config_value(LIBRARIES_TO_DOWNLOAD_KEY, default=[]) == []
        # The entry is gone, so nothing re-downloads it and the uninstall is clean. Warning that
        # the download entry survives would describe the state before the entry was removed.
        assert result.remaining_registrations == []


class TestUnwritableConfigLayer:
    """A config layer the engine cannot write supplies the entry, so the uninstall refuses.

    `set_config_value` only writes the user layer, and these settings are lists, which
    `merge_dicts` replaces wholesale, so the entry would survive. Deleting the files anyway
    would leave the engine looking for a library that is gone, and the file holding the entry
    is usually one a team shares.
    """

    @pytest.mark.asyncio
    async def test_project_layer_entry_is_refused(self, engine: Engine, workspace: Path, tmp_path: Path) -> None:
        config = engine.config_manager
        library_dir = workspace / "libs" / "shadowed"
        manifest = _write_manifest(library_dir, "Shadowed Library")

        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        (project_dir / "griptape_nodes_config.json").write_text(
            json.dumps({"app_events": {"on_app_initialization_complete": {"libraries_to_register": [str(manifest)]}}}),
            encoding="utf-8",
        )
        config.load_project_config(project_dir)
        config.load_configs()
        _track(engine.library_manager, manifest, library_name="Shadowed Library", registered_path=str(manifest))

        preview = await _preview(engine, "Shadowed Library", manifest)

        assert isinstance(preview.blocked_by, UnwritableConfigBlocker)
        assert preview.blocked_by.layer == "project"
        assert preview.blocked_by.config_key == LIBRARIES_TO_REGISTER_KEY

    @pytest.mark.asyncio
    async def test_user_layer_only_entry_is_not_refused(self, engine: Engine, workspace: Path) -> None:
        library_dir = workspace / "libs" / "plain"
        manifest = _write_manifest(library_dir, "Plain Library")
        engine.config_manager.set_config_value(LIBRARIES_TO_REGISTER_KEY, [str(manifest)])
        _track(engine.library_manager, manifest, library_name="Plain Library", registered_path=str(manifest))

        preview = await _preview(engine, "Plain Library", manifest)

        assert preview.blocked_by is None

    @pytest.mark.asyncio
    async def test_a_refused_uninstall_changes_nothing_at_all(
        self, engine: Engine, workspace: Path, tmp_path: Path
    ) -> None:
        """Nothing is written, unloaded or deleted, and the project's own file is untouched."""
        config = engine.config_manager
        mine = _write_manifest(workspace / "libs" / "mine", "Mine")
        theirs = _write_manifest(workspace / "libs" / "theirs", "Theirs")
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        (project_dir / "griptape_nodes_config.json").write_text(
            json.dumps(
                {"app_events": {"on_app_initialization_complete": {"libraries_to_register": [str(mine), str(theirs)]}}}
            ),
            encoding="utf-8",
        )
        config.load_project_config(project_dir)
        config.load_configs()
        _track(engine.library_manager, mine, library_name="Mine", registered_path=str(mine))

        result = await engine.ahandle_request(UninstallLibraryRequest(library_name="Mine", library_path=str(mine)))

        assert isinstance(result, UninstallLibraryResultFailure)
        assert isinstance(result.blocked_by, UnwritableConfigBlocker)
        assert await anyio.Path(mine.parent).exists()
        user_entries = config.get_config_value(LIBRARIES_TO_REGISTER_KEY, config_source="user_config", default=[]) or []
        assert user_entries == [], "the project layer's libraries leaked into the user config"
        assert (
            str(theirs)
            in json.loads((project_dir / "griptape_nodes_config.json").read_text(encoding="utf-8"))["app_events"][
                "on_app_initialization_complete"
            ]["libraries_to_register"]
        )


class TestProtectedDirectories:
    """A manifest sitting directly in a shared folder must not make that folder the delete target.

    The install directory is meant to be the library's OWN folder. A hand-added
    `libraries_to_register` entry pointing at a manifest in the workspace, the libraries root, or
    the home directory would otherwise resolve to that shared folder, and the delete would take
    every workflow or every other installed library with it. The collateral list names only other
    manifests, so the confirmation would never show those files.
    """

    @pytest.mark.asyncio
    async def test_a_manifest_at_the_workspace_root_is_blocked(self, engine: Engine, workspace: Path) -> None:
        workflow = workspace / "my_workflow.py"
        workflow.write_text("# a user's workflow", encoding="utf-8")
        manifest = _write_manifest(workspace, "Workspace Root Library")
        engine.config_manager.set_config_value(LIBRARIES_TO_REGISTER_KEY, [str(manifest)])
        _track(engine.library_manager, manifest, library_name="Workspace Root Library", registered_path=str(manifest))

        result = await _uninstall_after_preview(engine, "Workspace Root Library", manifest)

        assert isinstance(result, UninstallLibraryResultFailure)
        assert isinstance(result.blocked_by, UnsafePathBlocker)
        assert await anyio.Path(workflow).exists()
        assert await anyio.Path(workspace).exists()

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("workspace")
    async def test_a_manifest_in_the_libraries_root_is_blocked(self, engine: Engine) -> None:
        libraries_root = engine.config_manager.resolved_libraries_root()
        libraries_root.mkdir(parents=True, exist_ok=True)
        neighbour = _write_manifest(libraries_root / "other_lib", "Other Library")
        manifest = _write_manifest(libraries_root, "Libraries Root Library")
        engine.config_manager.set_config_value(LIBRARIES_TO_REGISTER_KEY, [str(manifest)])
        _track(engine.library_manager, manifest, library_name="Libraries Root Library", registered_path=str(manifest))

        result = await _uninstall_after_preview(engine, "Libraries Root Library", manifest)

        assert isinstance(result, UninstallLibraryResultFailure)
        assert isinstance(result.blocked_by, UnsafePathBlocker)
        assert await anyio.Path(neighbour).exists()

    @pytest.mark.asyncio
    async def test_a_library_in_its_own_subfolder_is_still_allowed(self, engine: Engine, workspace: Path) -> None:
        """The guard must not catch the normal case: a library in a folder under the workspace."""
        library_dir = workspace / "libs" / "ordinary"
        manifest = _write_manifest(library_dir, "Ordinary Library")
        engine.config_manager.set_config_value(LIBRARIES_TO_REGISTER_KEY, [str(manifest)])
        _track(engine.library_manager, manifest, library_name="Ordinary Library", registered_path=str(manifest))

        result = await _uninstall_after_preview(engine, "Ordinary Library", manifest)

        assert isinstance(result, UninstallLibraryResultSuccess), result.result_details
        assert not await anyio.Path(library_dir).exists()


class TestDuplicateNameCopies:
    """Two installs can share a name; the registry is keyed by name alone."""

    @pytest.mark.asyncio
    async def test_uninstalling_a_duplicate_leaves_the_loaded_copy_alone(self, engine: Engine, workspace: Path) -> None:
        """Uninstalling the failed duplicate must not unregister the copy that actually loaded.

        A duplicate that failed with `DuplicateLibraryProblem` is still tracked. Unloading by
        name would find the OTHER copy registered under that name and unregister it, dropping
        its held objects and workers, while its own files and config entry stayed put.
        """
        loaded_manifest = _write_manifest(workspace / "libs" / "loaded", "Dup Library")
        duplicate_manifest = _write_manifest(workspace / "libs" / "duplicate", "Dup Library")
        engine.config_manager.set_config_value(
            LIBRARIES_TO_REGISTER_KEY, [str(loaded_manifest), str(duplicate_manifest)]
        )
        _track(
            engine.library_manager,
            loaded_manifest,
            library_name="Dup Library",
            lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
            registered_path=str(loaded_manifest),
        )
        _track(
            engine.library_manager,
            duplicate_manifest,
            library_name="Dup Library",
            registered_path=str(duplicate_manifest),
        )
        _register_in_registry("Dup Library")

        result = await _uninstall_after_preview(engine, "Dup Library", duplicate_manifest)

        assert isinstance(result, UninstallLibraryResultSuccess), result.result_details
        assert result.unloaded is False
        # The live copy keeps its registry entry, its tracking record, and its files.
        assert "Dup Library" in LibraryRegistry.list_libraries()
        assert str(loaded_manifest) in engine.library_manager._library_file_path_to_info
        assert await anyio.Path(loaded_manifest).exists()
        # Only the duplicate is gone.
        assert not await anyio.Path(duplicate_manifest.parent).exists()
        assert str(duplicate_manifest) not in engine.library_manager._library_file_path_to_info

    @pytest.mark.asyncio
    async def test_uninstalling_the_loaded_copy_does_unload_it(self, engine: Engine, workspace: Path) -> None:
        loaded_manifest = _write_manifest(workspace / "libs" / "loaded", "Dup Library")
        _write_manifest(workspace / "libs" / "duplicate", "Dup Library")
        engine.config_manager.set_config_value(LIBRARIES_TO_REGISTER_KEY, [str(loaded_manifest)])
        _track(
            engine.library_manager,
            loaded_manifest,
            library_name="Dup Library",
            lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
            registered_path=str(loaded_manifest),
        )
        _register_in_registry("Dup Library")

        result = await _uninstall_after_preview(engine, "Dup Library", loaded_manifest)

        assert isinstance(result, UninstallLibraryResultSuccess), result.result_details
        assert result.unloaded is True
        assert "Dup Library" not in LibraryRegistry.list_libraries()


class TestAlreadyDeletedFolder:
    @pytest.mark.asyncio
    async def test_already_missing_directory_is_success(self, engine: Engine, workspace: Path) -> None:
        library_dir = workspace / "libs" / "gone_lib"
        manifest = _write_manifest(library_dir, "Gone Library")
        engine.config_manager.set_config_value(LIBRARIES_TO_REGISTER_KEY, [str(manifest)])
        _track(engine.library_manager, manifest, library_name="Gone Library", registered_path=str(manifest))
        await _preview(engine, "Gone Library", manifest)
        # The user deletes the folder by hand while the confirmation is open.
        shutil.rmtree(library_dir)

        result = await engine.ahandle_request(
            UninstallLibraryRequest(library_name="Gone Library", library_path=str(manifest))
        )

        assert isinstance(result, UninstallLibraryResultSuccess), result.result_details
        assert result.directory_deleted is True
        assert engine.config_manager.get_config_value(LIBRARIES_TO_REGISTER_KEY, default=[]) == []


class TestRepeatedUninstall:
    @pytest.mark.asyncio
    async def test_a_second_uninstall_finds_nothing_left(self, engine: Engine, workspace: Path) -> None:
        """A double-click cannot run the delete twice: the first one untracks the library."""
        manifest = _write_manifest(workspace / "libs" / "once", "Once Library")
        engine.config_manager.set_config_value(LIBRARIES_TO_REGISTER_KEY, [str(manifest)])
        _track(engine.library_manager, manifest, library_name="Once Library", registered_path=str(manifest))

        first = await _uninstall_after_preview(engine, "Once Library", manifest)
        second = await engine.ahandle_request(
            UninstallLibraryRequest(library_name="Once Library", library_path=str(manifest))
        )

        assert isinstance(first, UninstallLibraryResultSuccess), first.result_details
        assert isinstance(second, UninstallLibraryResultFailure)
        assert isinstance(second.blocked_by, NotFoundBlocker)


class TestConcurrentUninstalls:
    @pytest.mark.asyncio
    async def test_two_concurrent_uninstalls_do_not_clobber_each_others_config_write(
        self, engine: Engine, workspace: Path
    ) -> None:
        manifest_a = _write_manifest(workspace / "libs" / "lib_a", "Library A")
        manifest_b = _write_manifest(workspace / "libs" / "lib_b", "Library B")
        engine.config_manager.set_config_value(LIBRARIES_TO_REGISTER_KEY, [str(manifest_a), str(manifest_b)])
        _track(engine.library_manager, manifest_a, library_name="Library A", registered_path=str(manifest_a))
        _track(engine.library_manager, manifest_b, library_name="Library B", registered_path=str(manifest_b))
        results = await asyncio.gather(
            engine.ahandle_request(UninstallLibraryRequest(library_name="Library A", library_path=str(manifest_a))),
            engine.ahandle_request(UninstallLibraryRequest(library_name="Library B", library_path=str(manifest_b))),
        )

        for result in results:
            assert isinstance(result, UninstallLibraryResultSuccess), result.result_details
        assert engine.config_manager.get_config_value(LIBRARIES_TO_REGISTER_KEY, default=[]) == []


class TestStructuredVerdictsAreWireReady:
    """The editor switches on `kind`, so each variant must carry it over the wire.

    The serialization assertion is the load-bearing one. These fields were first written typed
    as a shared marker base class, which looks right and type-checks, but cattrs builds its hook
    from the DECLARED type: a plain base passed the dataclass through unconverted and a dataclass
    base emitted `{}`, so the editor would have received no `kind` to switch on. The fields are
    declared as unions of the concrete variants for exactly that reason, and this test fails if
    anyone reintroduces a base class.
    """

    def test_the_payload_serializes_as_the_editors_discriminated_union(self) -> None:
        from cattrs.preconf.json import make_converter

        from griptape_nodes.serialization.hooks import configure_converter

        converter = make_converter()
        configure_converter(converter)
        payload = PreviewUninstallLibraryResultSuccess(
            library_name="Comp",
            library_path="/libs/comp/griptape_nodes_library.json",
            install_directory="/libs/comp",
            registered_path="/libs",
            registered_in="register",
            collateral_library_names=["Roto"],
            blocked_by=UnsafePathBlocker(path="/"),
            remaining_registrations=[SharedDirectoryEntryRemaining(entry_path="/libs", library_names=["Roto"])],
            result_details="x",
        )

        wire = converter.unstructure(payload)

        assert wire["blocked_by"] == {"kind": "unsafe_path", "path": "/"}
        assert wire["remaining_registrations"] == [
            {"kind": "shared_directory_entry", "entry_path": "/libs", "library_names": ["Roto"]}
        ]

    def test_an_absent_blocker_serializes_as_null(self) -> None:
        from cattrs.preconf.json import make_converter

        from griptape_nodes.serialization.hooks import configure_converter

        converter = make_converter()
        configure_converter(converter)
        payload = PreviewUninstallLibraryResultSuccess(
            library_name="Comp",
            library_path="/libs/comp/griptape_nodes_library.json",
            install_directory="/libs/comp",
            registered_path=None,
            registered_in=None,
            result_details="x",
        )

        wire = converter.unstructure(payload)

        assert wire["blocked_by"] is None
        assert wire["remaining_registrations"] == []

    def test_each_blocker_carries_its_kind(self) -> None:
        assert EnvironmentManagedBlocker().kind == "environment_managed"
        assert SandboxLibraryBlocker().kind == "sandbox_library"
        assert UnsafePathBlocker(path="/").kind == "unsafe_path"

    def test_each_remaining_registration_carries_its_kind(self) -> None:
        assert SharedDirectoryEntryRemaining(entry_path="/libs").kind == "shared_directory_entry"
        assert UnmatchedEntryBlocker().kind == "unmatched_entry"
        assert UnwritableConfigBlocker(layer="project", config_key="x").kind == "unwritable_config"
