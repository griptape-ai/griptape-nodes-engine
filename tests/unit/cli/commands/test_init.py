"""Tests for gtn init workspace config mirroring."""

import json
from pathlib import Path
from unittest.mock import patch

import griptape_nodes.retained_mode.managers.config_manager as config_manager_module
from griptape_nodes.cli.commands import init as init_module
from griptape_nodes.cli.commands.init import _run_init
from griptape_nodes.cli.shared import InitConfig
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

_STALE_LIBRARIES_CONFIG = {
    "app_events": {
        "on_app_initialization_complete": {
            "libraries_to_register": ["/stale/lib"],
        }
    }
}


class TestInitWorkspaceConfigMirroring:
    """Integration tests: _run_init writes library settings to the workspace config when one exists."""

    def _fresh_config_manager(self) -> config_manager_module.ConfigManager:
        # SingletonMeta._instances was cleared by the isolate_user_config autouse fixture,
        # so this creates a fresh ConfigManager that reads from the patched USER_CONFIG_PATH.
        return GriptapeNodes.ConfigManager()

    def test_workspace_config_updated_when_exists(self, tmp_path: Path) -> None:
        workspace_config = tmp_path / "griptape_nodes_config.json"
        workspace_config.write_text(json.dumps(_STALE_LIBRARIES_CONFIG, indent=2))

        fresh_config_manager = self._fresh_config_manager()
        with (
            patch.object(init_module, "config_manager", fresh_config_manager),
            patch.object(init_module, "console"),
            patch.object(init_module, "_init_system_config"),
        ):
            _run_init(
                InitConfig(
                    interactive=False,
                    workspace_directory=str(tmp_path),
                    register_advanced_library=False,
                )
            )

        workspace_result = json.loads(workspace_config.read_text())
        workspace_libs = workspace_result["app_events"]["on_app_initialization_complete"]["libraries_to_register"]

        user_result = json.loads(config_manager_module.USER_CONFIG_PATH.read_text())
        user_libs = user_result["app_events"]["on_app_initialization_complete"]["libraries_to_register"]

        assert "/stale/lib" not in workspace_libs
        assert workspace_libs == user_libs

    def test_no_workspace_config_no_file_created(self, tmp_path: Path) -> None:
        workspace_config = tmp_path / "griptape_nodes_config.json"
        assert not workspace_config.exists()

        fresh_config_manager = self._fresh_config_manager()
        with (
            patch.object(init_module, "config_manager", fresh_config_manager),
            patch.object(init_module, "console"),
            patch.object(init_module, "_init_system_config"),
        ):
            _run_init(
                InitConfig(
                    interactive=False,
                    workspace_directory=str(tmp_path),
                    register_advanced_library=False,
                )
            )

        assert not workspace_config.exists()

        user_result = json.loads(config_manager_module.USER_CONFIG_PATH.read_text())
        user_libs = user_result["app_events"]["on_app_initialization_complete"]["libraries_to_register"]
        assert isinstance(user_libs, list)


class TestSetWorkspaceConfigValue:
    """Unit tests for ConfigManager.set_workspace_config_value."""

    def test_file_exists_returns_true_and_updates(self, tmp_path: Path) -> None:
        config_file = tmp_path / "workspace_config.json"
        config_file.write_text(json.dumps({"foo": "old"}))

        fresh_config_manager = GriptapeNodes.ConfigManager()
        fresh_config_manager._workspace_config_path = config_file

        result = fresh_config_manager.set_workspace_config_value("foo", "new")

        assert result is True
        written = json.loads(config_file.read_text())
        assert written["foo"] == "new"

    def test_no_file_returns_false_and_no_file_created(self, tmp_path: Path) -> None:
        non_existent = tmp_path / "does_not_exist.json"

        fresh_config_manager = GriptapeNodes.ConfigManager()
        fresh_config_manager._workspace_config_path = non_existent

        result = fresh_config_manager.set_workspace_config_value("foo", "bar")

        assert result is False
        assert not non_existent.exists()
