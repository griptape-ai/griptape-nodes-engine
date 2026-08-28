"""Root-cause tests: dropping the project config layer exposes a contaminated user layer.

This is the mechanism behind the stale-library report. Three facts combine:

1. `libraries_to_register` lives at
   `app_events.on_app_initialization_complete.libraries_to_register` -- a *nested* key.
   `_load_config_from_env_vars` only ever produces flat top-level keys
   (`config_key = key[11:].lower()`, no dot-splitting), so no `GTN_CONFIG_*` variable can
   set or shadow it. A user who pins `GTN_CONFIG_PROJECT_FILE`,
   `GTN_CONFIG_WORKSPACE_DIRECTORY` and `GTN_CONFIG_LIBRARIES_DIRECTORY` is still fully
   exposed on the register list.

2. `merge_dicts` is called with the default `merge_lists=False`, so a project layer that
   declares `libraries_to_register` *replaces* the user layer's list. While the project
   layer is loaded, a polluted user layer is invisible -- which is why boot looked clean.

3. `_activate_project` calls `clear_project_layers()` and then, for a project with no
   project-adjacent config to layer on, falls through to a bare `load_configs()`. With
   `_project_config_path` now None there is no project layer to do the replacing, so the
   user layer's list becomes the effective config. Activation now refuses an id with no
   loaded template outright, but **system defaults still takes this path**, so the exposure
   is narrowed rather than removed.

What writes those entries into the user layer is NOT established. `gtn init` is the
remaining live candidate (it writes the user config and the desktop app invokes it), but
that is unconfirmed. Two other suspects were ruled out: the desktop app's
`registerLibraries()` filesystem sweep has had no callers since Nov 2025, and
`_write_user_config_delta` merges only the delta into the on-disk file rather than
round-tripping the merged config. Until the writer is known, these tests pin the exposure
mechanism only -- they do not prove how a given user's config came to hold stale paths.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
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
from griptape_nodes.retained_mode.managers.config_manager import ConfigManager
from griptape_nodes.retained_mode.managers.settings import LIBRARIES_TO_REGISTER_KEY

if TYPE_CHECKING:
    from collections.abc import Generator

    from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

# The library the current project legitimately registers at boot.
REGISTERED_LIBRARY = "Nuke Nodes Library"

# Libraries present only in the previous install's tree. These are the three names the
# engine logged "no Library with that name was registered" for, right after advertising
# them through LoadMetadataForAllLibraries.
ORPHANED_LIBRARIES = (
    "Griptape Nodes VOID Library",
    "Griptape Nodes CorridorKey Library",
    "Griptape Nodes SAM 3D Objects Library",
)

# What a previous install left in the user config file.
PREVIOUS_INSTALL_LIBRARIES = [
    "/softwareLocal/griptape_nodes/linux/0.94.0/libraries/_all_/griptape-nodes-library-void/griptape-nodes-library.json",
    "/softwareLocal/griptape_nodes/linux/0.94.0/libraries/_all_/griptape-nodes-library-corridorkey/griptape-nodes-library.json",
    "/softwareLocal/griptape_nodes/linux/0.94.0/libraries/_all_/griptape-nodes-sam-3d-objects-library/griptape-nodes-library.json",
]

# What the current project declares.
CURRENT_PROJECT_LIBRARIES = [
    "/softwareLocal/griptape_nodes/libraries/_all_/griptape-nodes-library-nuke/griptape-nodes-library.json",
    "/softwareLocal/griptape_nodes/libraries/_all_/griptape-nodes-library-openexr/griptape-nodes-library.json",
]

OLD_WORKSPACE = "/mnt/users/madesjardins/griptape_nodes/0.94.0/MyProject/workspace"
NEW_WORKSPACE = "/rdo/shows/pwt/_project_ref/_config/gtn"


def _register_list_config(libraries: list[str], **extra: object) -> str:
    """Serialize a config file carrying a `libraries_to_register` list."""
    return json.dumps(
        {
            "app_events": {"on_app_initialization_complete": {"libraries_to_register": libraries}},
            **extra,
        }
    )


def _library_json(name: str) -> str:
    """Serialize a minimal but schema-valid library manifest."""
    schema = LibrarySchema(
        name=name,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="test",
            description=f"{name} used to reproduce the config-layer dropout",
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


class TestProjectLayerDropoutExposesUserLayer:
    """A cleared project layer promotes the user layer's stale library list."""

    def test_project_layer_hides_a_polluted_user_layer(self, isolate_user_config: Path) -> None:
        """With the project layer loaded, the user layer's stale entries are invisible.

        This is why the engine booted correctly and why `gtn self info` looks clean.
        """
        isolate_user_config.write_text(_register_list_config(PREVIOUS_INSTALL_LIBRARIES))

        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "griptape_nodes_config.json").write_text(_register_list_config(CURRENT_PROJECT_LIBRARIES))

            with patch.dict(os.environ, {}, clear=True):
                manager = ConfigManager()
                manager.load_project_config(project_dir)

                # Lists are replaced, not merged, so only the project's entries survive.
                assert manager.get_config_value(LIBRARIES_TO_REGISTER_KEY) == CURRENT_PROJECT_LIBRARIES

    def test_clearing_the_project_layer_promotes_the_stale_list(self, isolate_user_config: Path) -> None:
        """Clearing the project layer and remerging promotes the user layer's list.

        `clear_project_layers()` followed by a bare `load_configs()` leaves
        defaults -> user -> env, so the previous install's libraries become effective.
        This is the config-layer behavior that made the reported failure possible;
        `_activate_project` no longer runs this sequence for a project with no loaded
        template (it refuses before touching any layer), but system defaults still does,
        which is why the user layer must not accumulate foreign library paths.
        """
        isolate_user_config.write_text(_register_list_config(PREVIOUS_INSTALL_LIBRARIES))

        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "griptape_nodes_config.json").write_text(_register_list_config(CURRENT_PROJECT_LIBRARIES))

            with patch.dict(os.environ, {}, clear=True):
                manager = ConfigManager()
                manager.load_project_config(project_dir)
                assert manager.get_config_value(LIBRARIES_TO_REGISTER_KEY) == CURRENT_PROJECT_LIBRARIES

                # project_manager._activate_project: clear_project_layers() then, for a
                # project with no loaded template, load_configs().
                manager.clear_project_layers()
                manager.load_configs()

                effective = manager.get_config_value(LIBRARIES_TO_REGISTER_KEY)

                # The previous install's libraries are now what the engine will discover.
                assert effective == PREVIOUS_INSTALL_LIBRARIES
                assert any("0.94.0" in entry for entry in effective)
                assert not any("0.94.0" in entry for entry in CURRENT_PROJECT_LIBRARIES)

    def test_env_vars_cannot_protect_the_register_list(self, isolate_user_config: Path) -> None:
        """The user's GTN_CONFIG_* pins are structurally unable to shadow the nested key.

        Env vars become flat top-level keys, so `libraries_to_register` -- nested two levels
        deep -- is untouched no matter what is exported.
        """
        isolate_user_config.write_text(
            _register_list_config(PREVIOUS_INSTALL_LIBRARIES, workspace_directory=OLD_WORKSPACE)
        )

        env = {
            "GTN_CONFIG_WORKSPACE_DIRECTORY": NEW_WORKSPACE,
            "GTN_CONFIG_LIBRARIES_DIRECTORY": "/softwareLocal/griptape_nodes/libraries",
            "GTN_CONFIG_PROJECT_FILE": f"{NEW_WORKSPACE}/griptape-nodes-project.yml",
        }

        with patch.dict(os.environ, env, clear=True):
            manager = ConfigManager()
            manager.clear_project_layers()
            manager.load_configs()

            # The flat keys the user pinned do take effect...
            assert manager.get_config_value("workspace_directory") == NEW_WORKSPACE
            assert manager.get_config_value("libraries_directory") == "/softwareLocal/griptape_nodes/libraries"

            # ...but the nested register list is still the previous install's.
            assert manager.get_config_value(LIBRARIES_TO_REGISTER_KEY) == PREVIOUS_INSTALL_LIBRARIES

            # No env-derived key can reach it: the loader never splits on dots.
            assert all("." not in key for key in manager.env_config)
            assert LIBRARIES_TO_REGISTER_KEY not in manager.env_config

    def test_env_config_only_produces_flat_keys(self) -> None:
        """Pin the loader behavior the test above depends on."""
        with patch.dict(
            os.environ,
            {"GTN_CONFIG_APP_EVENTS__ON_APP_INITIALIZATION_COMPLETE__LIBRARIES_TO_REGISTER": "[]"},
            clear=True,
        ):
            manager = ConfigManager()
            env_config = manager._load_config_from_env_vars()

        # Flattened to a single meaningless key rather than a nested path.
        assert list(env_config) == ["app_events__on_app_initialization_complete__libraries_to_register"]
        assert "app_events" not in env_config


class TestDropoutProducesTheReportedErrors:
    """End-to-end: the dropout drives the exact errors from the reported log.

    No patched `get_config_value` here -- real files on disk and the engine's own
    ConfigManager. Activation no longer runs this sequence for an id with no loaded
    template (it refuses before touching any layer), so this documents what that refusal
    prevents. Switching to system defaults still drops the project layer, which is why a
    user config holding foreign library paths remains a hazard.
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
        carries the previous install's libraries; the project layer declares only its own.
        """
        previous_tree = tmp_path / "softwareLocal" / "griptape_nodes" / "linux" / "0.94.0" / "libraries"
        stale_paths = [
            str(_write_library(previous_tree / name.lower().replace(" ", "-"), name)) for name in ORPHANED_LIBRARIES
        ]
        isolate_user_config.write_text(_register_list_config(stale_paths), encoding="utf-8")

        current_tree = tmp_path / "softwareLocal" / "griptape_nodes" / "libraries"
        current_path = str(_write_library(current_tree / "griptape-nodes-library-nuke", REGISTERED_LIBRARY))
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "griptape_nodes_config.json").write_text(_register_list_config([current_path]), encoding="utf-8")
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

        # The dropout: project layer cleared, config remerged without one.
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
