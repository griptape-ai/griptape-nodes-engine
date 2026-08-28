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

3. `_activate_project` calls `clear_project_layers()` and then, when the requested project
   has no loaded template (unknown project id, or system defaults), falls through to a bare
   `load_configs()`. With `_project_config_path` now None there is no project layer to do
   the replacing, so the user layer's list becomes the effective config.

The user layer gets polluted from outside the engine: the desktop app's `registerLibraries()`
sweeps the filesystem for every `griptape_nodes_library.json` it can find and merges the
results into `libraries_to_register` in the user config file, additively and never
subtractively. Libraries from a previous install therefore accumulate there permanently.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from griptape_nodes.retained_mode.managers.config_manager import ConfigManager
from griptape_nodes.retained_mode.managers.settings import LIBRARIES_TO_REGISTER_KEY

# What a previous install left welded into the user config file.
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
