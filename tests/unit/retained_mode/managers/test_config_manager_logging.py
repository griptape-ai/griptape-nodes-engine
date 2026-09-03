"""Tests for how a config load reaches the shared logger and its diagnostic sinks.

Logging is configured from several related settings at once, and any config file can supply
one of them — the user file, a project file, or a workspace file. So the sinks are applied
at the end of every load rather than by whichever caller happened to write a setting; a
load that skipped it left the engine logging somewhere other than where `report.json` said
it did.

The other half is that re-applying is not free. Every config write reloads, and each apply
re-scans the log directory for files to age out, so an unrelated write must be a no-op.
"""

from __future__ import annotations

import json
import logging
import platform
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from griptape_nodes.common.log_capture import default_log_directory
from griptape_nodes.retained_mode.events.config_events import (
    SetConfigCategoryRequest,
    SetConfigCategoryResultSuccess,
)
from griptape_nodes.retained_mode.managers.config_manager import ConfigManager

if TYPE_CHECKING:
    from pathlib import Path

_CONFIGURE = "griptape_nodes.retained_mode.managers.config_manager.configure_diagnostic_logging"

_skip_on_windows = pytest.mark.skipif(
    platform.system() == "Windows", reason="xdg_base_dirs cannot find XDG_CONFIG_HOME on Windows on GitHub Actions"
)


@pytest.fixture
def manager() -> ConfigManager:
    """A manager whose engine is already built, so nothing builds one inside a patch.

    `ConfigManager()` resolves its engine lazily, and building the root engine constructs a
    second `ConfigManager` that applies logging settings of its own. Warming it here keeps a
    test counting only the calls its own manager made.
    """
    manager = ConfigManager()
    _ = manager.engine
    return manager


@_skip_on_windows
@pytest.mark.usefixtures("isolate_user_config")
class TestLogDirectory:
    def test_an_unset_setting_means_the_default_location(self) -> None:
        assert ConfigManager().log_directory == default_log_directory()

    def test_an_absolute_setting_is_used_as_given(self, tmp_path: Path) -> None:
        manager = ConfigManager()

        manager.set_config_value("logging.log_directory", str(tmp_path))

        assert manager.log_directory == tmp_path

    def test_a_relative_setting_falls_back_to_the_default(self) -> None:
        """Relative resolves against the working directory, which would scatter logs."""
        manager = ConfigManager()

        manager.set_config_value("logging.log_directory", "logs")

        assert manager.log_directory == default_log_directory()


@_skip_on_windows
@pytest.mark.usefixtures("isolate_user_config")
class TestApplyLoggingSettings:
    @pytest.mark.usefixtures("manager")
    def test_the_sinks_are_configured_from_the_first_load(self) -> None:
        """Loading applies them itself, rather than waiting for a caller to write a setting."""
        with patch(_CONFIGURE) as configure:
            first_load = ConfigManager()

        assert configure.call_count == 1
        assert configure.call_args.kwargs["log_directory"] == first_load.log_directory

    def test_a_reload_applies_a_setting_no_caller_wrote(self, manager: ConfigManager, tmp_path: Path) -> None:
        """A workspace file can supply one of these, with no call to `set_config_value` at all."""
        workspace_directory = tmp_path / "workspace"
        log_directory = tmp_path / "logs"
        workspace_directory.mkdir()
        (workspace_directory / "griptape_nodes_config.json").write_text(
            json.dumps({"logging": {"log_directory": str(log_directory)}}), encoding="utf-8"
        )

        with patch(_CONFIGURE) as configure:
            manager.load_workspace_config(workspace_directory)

        assert configure.call_count == 1
        assert configure.call_args.kwargs["log_directory"] == log_directory

    def test_a_write_that_changes_nothing_relevant_does_not_reconfigure(self, manager: ConfigManager) -> None:
        """Every write reloads, and every apply re-scans the log directory for aged-out files."""
        with patch(_CONFIGURE) as configure:
            manager.set_config_value("max_nodes_in_parallel", 4)

        assert configure.call_count == 0

    def test_turning_file_logging_off_reaches_the_sinks(self, manager: ConfigManager) -> None:
        with patch(_CONFIGURE) as configure:
            manager.set_config_value("logging.log_to_file", value=False)

        assert configure.call_count == 1
        assert configure.call_args.kwargs["log_to_file"] is False

    def test_resizing_the_session_buffer_reaches_the_sinks(self, manager: ConfigManager) -> None:
        with patch(_CONFIGURE) as configure:
            manager.set_config_value("logging.session_log_buffer_lines", 25)

        assert configure.call_count == 1
        assert configure.call_args.kwargs["buffer_lines"] == 25  # noqa: PLR2004

    def test_writing_the_same_logging_value_twice_only_reconfigures_once(
        self, manager: ConfigManager, tmp_path: Path
    ) -> None:
        manager.set_config_value("logging.log_directory", str(tmp_path))

        with patch(_CONFIGURE) as configure:
            manager.set_config_value("logging.log_directory", str(tmp_path))

        assert configure.call_count == 0

    def test_settings_that_could_not_be_installed_are_applied_again_on_the_next_load(
        self, manager: ConfigManager, tmp_path: Path
    ) -> None:
        """Nothing is remembered as applied until the sinks asked for are really installed.

        The reasons a log file fails to open are the temporary kind -- a volume not mounted
        yet, a directory someone is about to fix the permissions on. Remembered as done, the
        no-op check above would then skip every later load, and the engine would go the rest
        of its life without writing a log file.
        """
        with patch(_CONFIGURE, return_value=False) as failed:
            manager.set_config_value("logging.log_directory", str(tmp_path))
        assert failed.call_count == 1

        with patch(_CONFIGURE, return_value=True) as retried:
            manager.load_configs()

        assert retried.call_count == 1
        assert retried.call_args.kwargs["log_directory"] == tmp_path

    def test_settings_that_were_installed_are_not_applied_again(self, manager: ConfigManager, tmp_path: Path) -> None:
        """The contrast to the retry above: a successful apply is what the no-op check remembers."""
        with patch(_CONFIGURE, return_value=True):
            manager.set_config_value("logging.log_directory", str(tmp_path))

        with patch(_CONFIGURE) as configure:
            manager.load_configs()

        assert configure.call_count == 0

    def test_writing_the_log_level_still_reaches_the_shared_logger(self) -> None:
        """`set_config_value` no longer sets it directly; the reload at the end of it does."""
        manager = ConfigManager()
        shared_logger = logging.getLogger("griptape_nodes")
        previous_level = shared_logger.level

        try:
            manager.set_config_value("log_level", "DEBUG")

            assert shared_logger.level == logging.DEBUG
        finally:
            shared_logger.setLevel(previous_level)


@_skip_on_windows
@pytest.mark.usefixtures("isolate_user_config")
class TestSetConfigCategory:
    def test_replacing_the_whole_config_reloads_so_readers_agree_with_the_engine(
        self, manager: ConfigManager, tmp_path: Path
    ) -> None:
        """Without the reload the engine keeps its old config while every reader reports the new one."""
        request = SetConfigCategoryRequest(category=None, contents={"logging": {"log_directory": str(tmp_path)}})

        with patch(_CONFIGURE) as configure:
            result = manager.on_handle_set_config_category_request(request)

        assert isinstance(result, SetConfigCategoryResultSuccess)
        assert manager.get_config_value("logging.log_directory", default="") == str(tmp_path)
        assert configure.call_count == 1
        assert configure.call_args.kwargs["log_directory"] == tmp_path
