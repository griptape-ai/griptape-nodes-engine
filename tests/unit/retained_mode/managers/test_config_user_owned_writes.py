"""Tests that a config write does not copy other layers' list entries into the user config.

The editor renders the MERGED config, so a write to a list-valued key hands the whole
merged list back to `set_config_value`. Persisting that verbatim wrote entries owned by the
project or workspace layer into the user config file, where they outlive the project that
supplied them: the next time no project layer is loaded to shadow them (system defaults, for
instance) they become the engine's effective library set. That is how a user config ends up
registering libraries from a project the user no longer has open, which then get advertised
to the editor and reported as unregistered when the registry disagrees.

Only the user's own contribution is persisted now.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from griptape_nodes.retained_mode.managers.config_manager import ConfigManager
from griptape_nodes.retained_mode.managers.settings import LIBRARIES_TO_REGISTER_KEY

PROJECT_LIBRARY = "/show/project/libraries/griptape-nodes-library-nuke/griptape_nodes_library.json"
OTHER_PROJECT_LIBRARY = "/show/project/libraries/griptape-nodes-library-void/griptape_nodes_library.json"
MY_OWN_LIBRARY = "/home/me/libraries/my-library/griptape_nodes_library.json"


def _register_list_config(libraries: list[str]) -> str:
    return json.dumps({"app_events": {"on_app_initialization_complete": {"libraries_to_register": libraries}}})


def _read_user_layer(user_config_path: Path) -> list[str]:
    """The `libraries_to_register` actually persisted to the user config file."""
    raw = json.loads(user_config_path.read_text(encoding="utf-8"))
    return raw.get("app_events", {}).get("on_app_initialization_complete", {}).get("libraries_to_register", [])


class TestUserOwnedWrites:
    def test_a_project_supplied_list_is_not_copied_into_the_user_config(self, isolate_user_config: Path) -> None:
        """Saving the rendered list back must not adopt the project's entries.

        This is the write the editor performs whenever a row is toggled or removed: it sends
        the array it is rendering, which is the merged one.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "griptape_nodes_config.json").write_text(
                _register_list_config([PROJECT_LIBRARY, OTHER_PROJECT_LIBRARY])
            )

            with patch.dict(os.environ, {}, clear=True):
                manager = ConfigManager()
                manager.load_project_config(project_dir)

                rendered = manager.get_config_value(LIBRARIES_TO_REGISTER_KEY)
                assert rendered == [PROJECT_LIBRARY, OTHER_PROJECT_LIBRARY]

                # The editor saves the list it is rendering.
                manager.set_config_value(LIBRARIES_TO_REGISTER_KEY, list(rendered))

                # Nothing of the project's landed in the user config.
                assert _read_user_layer(isolate_user_config) == []

    def test_the_users_own_addition_is_persisted(self, isolate_user_config: Path) -> None:
        """Stripping foreign entries must not strip the entry the user actually added."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "griptape_nodes_config.json").write_text(_register_list_config([PROJECT_LIBRARY]))

            with patch.dict(os.environ, {}, clear=True):
                manager = ConfigManager()
                manager.load_project_config(project_dir)

                manager.set_config_value(LIBRARIES_TO_REGISTER_KEY, [PROJECT_LIBRARY, MY_OWN_LIBRARY])

                assert _read_user_layer(isolate_user_config) == [MY_OWN_LIBRARY]

    def test_with_no_project_layer_the_write_is_unchanged(self, isolate_user_config: Path) -> None:
        """The common case must behave exactly as before: nothing else supplies the key."""
        with patch.dict(os.environ, {}, clear=True):
            manager = ConfigManager()

            manager.set_config_value(LIBRARIES_TO_REGISTER_KEY, [MY_OWN_LIBRARY, PROJECT_LIBRARY])

            assert _read_user_layer(isolate_user_config) == [MY_OWN_LIBRARY, PROJECT_LIBRARY]

    def test_in_place_mutation_of_a_read_value_does_not_drop_the_new_entry(self, isolate_user_config: Path) -> None:
        """Guards the read-modify-write callers, which mutate the list they were handed.

        `get_config_value` returns references straight into the config layers, so appending
        to what it returns mutates those layers in place. A shadow check that recomputed
        from the live layers would then see the new entry as something another layer already
        supplied and drop it, silently losing a library download's registration.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "griptape_nodes_config.json").write_text(_register_list_config([PROJECT_LIBRARY]))

            with patch.dict(os.environ, {}, clear=True):
                manager = ConfigManager()
                manager.load_project_config(project_dir)

                # Exactly what download_library_request does: read, append in place, write.
                entries = manager.get_config_value(LIBRARIES_TO_REGISTER_KEY, default=[])
                entries.append(MY_OWN_LIBRARY)
                manager.set_config_value(LIBRARIES_TO_REGISTER_KEY, entries)

                assert _read_user_layer(isolate_user_config) == [MY_OWN_LIBRARY]

    def test_scalars_are_left_alone(self, isolate_user_config: Path) -> None:
        """Only list keys are filtered; a scalar write still persists verbatim.

        A scalar the project also defines stays a separate problem: the write lands in the
        user layer but the project keeps winning, so the edit reads as ignored.
        """
        with patch.dict(os.environ, {}, clear=True):
            manager = ConfigManager()

            manager.set_config_value("log_level", "DEBUG")

            raw = json.loads(isolate_user_config.read_text(encoding="utf-8"))
            assert raw["log_level"] == "DEBUG"


class TestLayerOrderingStaysInSync:
    def test_merge_helper_agrees_with_the_loaded_merged_config(self, isolate_user_config: Path) -> None:
        """`_merge_config_layers` is the ordering used by load_configs, not a second copy.

        If someone adds a layer to one and not the other, the shadow check starts consulting
        a different precedence than the engine actually resolves.
        """
        isolate_user_config.write_text(_register_list_config([MY_OWN_LIBRARY]))

        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "griptape_nodes_config.json").write_text(_register_list_config([PROJECT_LIBRARY]))

            with patch.dict(os.environ, {"GTN_CONFIG_STORAGE_BACKEND": "gtc"}, clear=True):
                manager = ConfigManager()
                manager.load_project_config(project_dir)

                assert manager._merge_config_layers(include_user_layer=True) == manager.merged_config

    def test_excluding_the_user_layer_drops_only_the_user_layer(self, isolate_user_config: Path) -> None:
        isolate_user_config.write_text(_register_list_config([MY_OWN_LIBRARY]))

        with patch.dict(os.environ, {}, clear=True):
            manager = ConfigManager()

            with_user = manager._merge_config_layers(include_user_layer=True)
            without_user = manager._merge_config_layers(include_user_layer=False)

            from griptape_nodes.utils.dict_utils import get_dot_value

            assert get_dot_value(with_user, LIBRARIES_TO_REGISTER_KEY, None) == [MY_OWN_LIBRARY]
            assert get_dot_value(without_user, LIBRARIES_TO_REGISTER_KEY, None) == []
