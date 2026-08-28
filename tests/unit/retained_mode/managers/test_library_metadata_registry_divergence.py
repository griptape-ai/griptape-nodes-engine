"""Reproduction tests for the metadata/registry split behind the stale-library reports.

A user pointed a new project at a new libraries directory while an older install had
left its own library list on disk. The engine booted correctly -- its registry held
only the new project's libraries -- yet the editor listed the *old* install's
libraries, opened their old paths, and then logged one error per old library:

    ERROR  Attempted to check for updates for Library 'Griptape Nodes VOID Library'.
           Failed because no Library with that name was registered.

The editor only ever asks about names the engine handed it: every `checkLibraryUpdate`
call site in the GUI iterates the live `loadMetadataForAllLibraries` result. So the
engine must have returned those libraries from one request and disowned them in the
next. It does, because the two requests read different sources:

* `LoadMetadataForAllLibraries` -> `_discover_library_files()` -> `libraries_to_register`
  read from config at call time.
* `CheckLibraryUpdate` -> `LibraryRegistry.get_library()`, populated at boot.

Nothing in the metadata response says which entries the registry actually has, so the
GUI cannot tell a live library from a config leftover. These tests pin that gap.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from griptape_nodes.node_library.library_registry import (
    LibraryMetadata,
    LibraryRegistry,
    LibrarySchema,
)
from griptape_nodes.retained_mode.events.library_events import (
    CheckLibraryUpdateRequest,
    CheckLibraryUpdateResultFailure,
    LoadMetadataForAllLibrariesRequest,
    LoadMetadataForAllLibrariesResultSuccess,
)
from griptape_nodes.retained_mode.managers.settings import (
    LIBRARIES_TO_DOWNLOAD_KEY,
    LIBRARIES_TO_REGISTER_KEY,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path

    from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

# The library that the new project legitimately registers at boot.
REGISTERED_LIBRARY = "Nuke Nodes Library"

# Libraries that exist only in the previous install's tree. These are the three names
# the engine logged "no Library with that name was registered" for, after having just
# advertised them through LoadMetadataForAllLibraries.
ORPHANED_LIBRARIES = (
    "Griptape Nodes VOID Library",
    "Griptape Nodes CorridorKey Library",
    "Griptape Nodes SAM 3D Objects Library",
)


def _library_json(name: str) -> str:
    """Serialize a minimal but schema-valid library manifest."""
    schema = LibrarySchema(
        name=name,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="test",
            description=f"{name} used to reproduce the metadata/registry split",
            library_version="0.1.0",
            engine_version="0.98.0",
            tags=[],
        ),
        categories=[],
        nodes=[],
    )
    return json.dumps(schema.model_dump(mode="json"))


def _write_library(directory: Path, name: str) -> Path:
    """Write a library manifest into its own directory and return the manifest path."""
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "griptape_nodes_library.json"
    manifest.write_text(_library_json(name), encoding="utf-8")
    return manifest


def _register_only_config(libraries: object) -> Callable[..., object]:
    """A `get_config_value` side effect serving only `libraries_to_register`.

    Discovery also reads `libraries_to_download`, and `find_files_recursive` reads
    `discovery_max_depth` through the same ConfigManager, so every other key defers to
    the caller's own `default`.
    """

    def get_config_value(key: str, *, default: object = None, **_: object) -> object:
        if key == LIBRARIES_TO_REGISTER_KEY:
            return libraries
        if key == LIBRARIES_TO_DOWNLOAD_KEY:
            return []
        return default

    return get_config_value


class TestProjectLayerDropoutProducesTheReportedErrors:
    """End-to-end: a config-layer dropout drives the exact errors from the log.

    No patched `get_config_value` here -- real files on disk and the engine's own
    ConfigManager. `_activate_project` no longer performs this sequence for a project with
    no loaded template (it refuses before touching any layer), so this documents what the
    refusal prevents. Activating system defaults still drops the project layer, which is
    why a user config that has accumulated foreign library paths remains a hazard.
    """

    @pytest.fixture(autouse=True)
    def _clean_registry(self) -> Generator[None, None, None]:
        LibraryRegistry._clear()
        yield
        LibraryRegistry._clear()

    @pytest.fixture
    def two_installs_on_disk(self, isolate_user_config: Path, tmp_path: Path) -> Path:
        """Write both trees and both config layers, and return the project directory.

        Sync so the async test body performs no direct Path I/O (ASYNC240). The user layer
        carries the previous install's libraries, as the desktop app's additive
        registerLibraries() sweep leaves it; the project layer declares only its own.
        """
        previous_tree = tmp_path / "softwareLocal" / "griptape_nodes" / "linux" / "0.94.0" / "libraries"
        stale_paths = [
            str(_write_library(previous_tree / name.lower().replace(" ", "-"), name)) for name in ORPHANED_LIBRARIES
        ]
        isolate_user_config.write_text(
            json.dumps({"app_events": {"on_app_initialization_complete": {"libraries_to_register": stale_paths}}}),
            encoding="utf-8",
        )

        current_tree = tmp_path / "softwareLocal" / "griptape_nodes" / "libraries"
        current_path = str(_write_library(current_tree / "griptape-nodes-library-nuke", REGISTERED_LIBRARY))
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "griptape_nodes_config.json").write_text(
            json.dumps({"app_events": {"on_app_initialization_complete": {"libraries_to_register": [current_path]}}}),
            encoding="utf-8",
        )
        return project_dir

    @pytest.mark.asyncio
    async def test_dropping_the_project_layer_makes_the_engine_advertise_stale_libraries(
        self, griptape_nodes: GriptapeNodes, two_installs_on_disk: Path
    ) -> None:
        library_manager = griptape_nodes.LibraryManager()
        config_manager = griptape_nodes.ConfigManager()

        # Boot: project layer loaded, only the project's library is registered.
        config_manager.load_project_config(two_installs_on_disk)
        LibraryRegistry.generate_new_library(
            library_data=LibrarySchema(
                name=REGISTERED_LIBRARY,
                library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
                metadata=LibraryMetadata(
                    author="test",
                    description="the library the project actually loaded",
                    library_version="0.3.0",
                    engine_version="0.98.0",
                    tags=[],
                ),
                categories=[],
                nodes=[],
            )
        )

        booted = await library_manager.load_metadata_for_all_libraries_request(LoadMetadataForAllLibrariesRequest())
        assert isinstance(booted, LoadMetadataForAllLibrariesResultSuccess)
        assert {entry.library_schema.name for entry in booted.successful_libraries} == {REGISTERED_LIBRARY}

        # Activation of a project with no loaded template.
        config_manager.clear_project_layers()
        config_manager.load_configs()

        after = await library_manager.load_metadata_for_all_libraries_request(LoadMetadataForAllLibrariesRequest())
        assert isinstance(after, LoadMetadataForAllLibrariesResultSuccess)
        advertised = {entry.library_schema.name for entry in after.successful_libraries}

        # The engine now hands the editor the previous install's libraries.
        for orphan in ORPHANED_LIBRARIES:
            assert orphan in advertised
        assert REGISTERED_LIBRARY not in advertised

        # Each is reported enabled, because `_library_file_path_to_info` has no entry for a
        # path this engine never discovered and `enabled` defaults to True. That is what
        # gets them past the editor's `git_remote && enabled` filter (in the field these
        # trees are git clones, so `git_remote` is set).
        for entry in after.successful_libraries:
            assert entry.enabled is True

        # And the registry still holds only what boot loaded, so every follow-up check
        # fails with the error the user saw.
        for orphan in ORPHANED_LIBRARIES:
            result = await library_manager.check_library_update_request(CheckLibraryUpdateRequest(library_name=orphan))
            assert isinstance(result, CheckLibraryUpdateResultFailure)
            assert "no Library with that name was registered" in str(result.result_details)


class TestMetadataRegistryDivergence:
    """The engine advertises libraries through metadata that its registry disowns."""

    @pytest.fixture(autouse=True)
    def _clean_registry(self) -> Generator[None, None, None]:
        """LibraryRegistry holds class-level state that survives the engine reset fixture."""
        LibraryRegistry._clear()
        yield
        LibraryRegistry._clear()

    @pytest.fixture
    def stale_config_paths(self, tmp_path: Path) -> list[str]:
        """Lay out a new libraries tree and the previous install's tree beside it.

        Mirrors the reported layout: the new project registers from
        `<root>/libraries`, while the older install lived under
        `<root>/linux/0.94.0/libraries` and left its manifests on disk.
        """
        root = tmp_path / "softwareLocal" / "griptape_nodes"
        current_tree = root / "libraries" / "_all_"
        previous_tree = root / "linux" / "0.94.0" / "libraries" / "_all_"

        paths = [str(_write_library(current_tree / "griptape-nodes-library-nuke", REGISTERED_LIBRARY))]
        paths.extend(
            str(_write_library(previous_tree / name.lower().replace(" ", "-"), name)) for name in ORPHANED_LIBRARIES
        )
        return paths

    def _register_boot_library(self) -> None:
        """Register only the library the current project actually loaded at boot."""
        schema = LibrarySchema(
            name=REGISTERED_LIBRARY,
            library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
            metadata=LibraryMetadata(
                author="test",
                description="the one library this engine really loaded",
                library_version="0.3.0",
                engine_version="0.98.0",
                tags=[],
            ),
            categories=[],
            nodes=[],
        )
        LibraryRegistry.generate_new_library(library_data=schema)

    @pytest.mark.asyncio
    async def test_metadata_advertises_libraries_the_registry_does_not_have(
        self, griptape_nodes: GriptapeNodes, stale_config_paths: list[str]
    ) -> None:
        """LoadMetadataForAllLibraries returns the previous install's libraries verbatim.

        This is the step that feeds the editor its library list, and it happily reports
        libraries this engine never registered.
        """
        library_manager = griptape_nodes.LibraryManager()
        self._register_boot_library()

        with patch.object(
            griptape_nodes.ConfigManager(),
            "get_config_value",
            side_effect=_register_only_config(stale_config_paths),
        ):
            result = await library_manager.load_metadata_for_all_libraries_request(LoadMetadataForAllLibrariesRequest())

        assert isinstance(result, LoadMetadataForAllLibrariesResultSuccess)
        advertised = {entry.library_schema.name for entry in result.successful_libraries}

        # The engine hands the editor all four, though it only ever registered one.
        assert REGISTERED_LIBRARY in advertised
        for orphan in ORPHANED_LIBRARIES:
            assert orphan in advertised, f"{orphan} should be advertised to reproduce the report"

        registered = set(LibraryRegistry.list_libraries())
        assert registered == {REGISTERED_LIBRARY}

    @pytest.mark.asyncio
    async def test_metadata_response_cannot_distinguish_registered_from_orphaned(
        self, griptape_nodes: GriptapeNodes, stale_config_paths: list[str]
    ) -> None:
        """The payload carries no signal the GUI could use to filter orphans out.

        `enabled` reflects the config flag, not registry membership, so a leftover entry
        is indistinguishable from a live library. This is the field the fix needs to add.
        """
        library_manager = griptape_nodes.LibraryManager()
        self._register_boot_library()

        with patch.object(
            griptape_nodes.ConfigManager(),
            "get_config_value",
            side_effect=_register_only_config(stale_config_paths),
        ):
            result = await library_manager.load_metadata_for_all_libraries_request(LoadMetadataForAllLibrariesRequest())

        assert isinstance(result, LoadMetadataForAllLibrariesResultSuccess)
        by_name = {entry.library_schema.name: entry for entry in result.successful_libraries}

        live = by_name[REGISTERED_LIBRARY]
        orphan = by_name[ORPHANED_LIBRARIES[0]]

        # Both are reported enabled, so `enabled` cannot be used to tell them apart.
        assert live.enabled is True
        assert orphan.enabled is True

        # And no attribute on the payload reports registry membership.
        membership_fields = [
            field for field in ("registered", "loaded", "is_registered", "in_registry") if hasattr(orphan, field)
        ]
        assert membership_fields == [], (
            "LoadLibraryMetadataFromFileResultSuccess now exposes registry membership "
            f"({membership_fields}); update the GUI to filter on it and delete this assertion."
        )

    @pytest.mark.asyncio
    @pytest.mark.xfail(
        strict=True,
        reason="Metadata is discovered from config while update checks read the registry; "
        "the two are never reconciled, so the editor is handed libraries the engine disowns.",
    )
    async def test_every_advertised_library_is_answerable(
        self, griptape_nodes: GriptapeNodes, stale_config_paths: list[str]
    ) -> None:
        """The contract the fix must satisfy.

        Anything `LoadMetadataForAllLibraries` presents as an enabled library should be
        answerable by the follow-up calls the editor makes about it. Today it is not, so
        this is a strict xfail: when the metadata response starts reporting registry
        membership (or stops advertising unregistered entries), this test passes and the
        marker must be removed.
        """
        library_manager = griptape_nodes.LibraryManager()
        self._register_boot_library()

        with patch.object(
            griptape_nodes.ConfigManager(),
            "get_config_value",
            side_effect=_register_only_config(stale_config_paths),
        ):
            metadata = await library_manager.load_metadata_for_all_libraries_request(
                LoadMetadataForAllLibrariesRequest()
            )
            assert isinstance(metadata, LoadMetadataForAllLibrariesResultSuccess)

            disowned = []
            for entry in metadata.successful_libraries:
                name = entry.library_schema.name
                if not entry.enabled:
                    continue
                result = await library_manager.check_library_update_request(
                    CheckLibraryUpdateRequest(library_name=name)
                )
                if isinstance(
                    result, CheckLibraryUpdateResultFailure
                ) and "no Library with that name was registered" in str(result.result_details):
                    disowned.append(name)

        assert disowned == [], f"metadata advertised libraries the registry does not have: {disowned}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("orphan", ORPHANED_LIBRARIES)
    async def test_update_check_rejects_the_library_metadata_just_advertised(
        self, griptape_nodes: GriptapeNodes, stale_config_paths: list[str], orphan: str
    ) -> None:
        """The follow-up update check fails with the exact error from the user's log.

        Together with the test above this is the whole user-visible defect: the editor
        renders a library the engine advertised, then asks about it and is told it does
        not exist.
        """
        library_manager = griptape_nodes.LibraryManager()
        self._register_boot_library()

        with patch.object(
            griptape_nodes.ConfigManager(),
            "get_config_value",
            side_effect=_register_only_config(stale_config_paths),
        ):
            metadata = await library_manager.load_metadata_for_all_libraries_request(
                LoadMetadataForAllLibrariesRequest()
            )
            assert isinstance(metadata, LoadMetadataForAllLibrariesResultSuccess)
            assert orphan in {entry.library_schema.name for entry in metadata.successful_libraries}

            result = await library_manager.check_library_update_request(CheckLibraryUpdateRequest(library_name=orphan))

        assert isinstance(result, CheckLibraryUpdateResultFailure)
        assert f"Attempted to check for updates for Library '{orphan}'" in str(result.result_details)
        assert "no Library with that name was registered" in str(result.result_details)
