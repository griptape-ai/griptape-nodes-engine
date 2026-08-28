import json
import logging
import os
import platform
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from griptape_nodes.common.project_templates.project_path import resolve_project_path_field
from griptape_nodes.retained_mode.events.app_events import ConfigChanged
from griptape_nodes.retained_mode.events.config_events import (
    GetConfigCategoryRequest,
    GetConfigCategoryResultSuccess,
    GetConfigLayersRequest,
    GetConfigLayersResultSuccess,
    GetConfigValueRequest,
    GetConfigValueResultSuccess,
    SetConfigCategoryRequest,
    SetConfigCategoryResultSuccess,
    SetConfigValueRequest,
    SetConfigValueResultSuccess,
)
from griptape_nodes.retained_mode.managers.config_manager import (
    DEFAULT_LIBRARIES_ROOT_ENV_VAR,
    ConfigManager,
)
from griptape_nodes.retained_mode.managers.event_manager import EventManager
from griptape_nodes.retained_mode.managers.project_manager import ProjectManager
from griptape_nodes.retained_mode.managers.settings import LIBRARIES_TO_REGISTER_KEY, REQUIRES_ENGINE_KEY
from griptape_nodes.utils.dict_utils import get_dot_value, set_dot_value


@pytest.mark.skipif(
    platform.system() == "Windows", reason="xdg_base_dirs cannot find XDG_CONFIG_HOME on Windows on GitHub Actions"
)
class TestConfigManager:
    """Test ConfigManager functionality including environment variable loading."""

    def test_load_config_from_env_vars_empty(self) -> None:
        """Test that no GTN_CONFIG_ env vars returns empty dict."""
        with patch.dict(os.environ, {}, clear=True):
            manager = ConfigManager()
            env_config = manager._load_config_from_env_vars()
            assert env_config == {}

    def test_load_config_from_env_vars_single(self) -> None:
        """Test loading a single GTN_CONFIG_ environment variable."""
        with patch.dict(os.environ, {"GTN_CONFIG_FOO": "bar"}, clear=True):
            manager = ConfigManager()
            env_config = manager._load_config_from_env_vars()
            assert env_config == {"foo": "bar"}

    def test_load_config_from_env_vars_multiple(self) -> None:
        """Test loading multiple GTN_CONFIG_ environment variables."""
        with patch.dict(
            os.environ,
            {
                "GTN_CONFIG_FOO": "bar",
                "GTN_CONFIG_STORAGE_BACKEND": "gtc",
                "GTN_CONFIG_LOG_LEVEL": "DEBUG",
                "REGULAR_ENV_VAR": "ignored",
            },
            clear=True,
        ):
            manager = ConfigManager()
            env_config = manager._load_config_from_env_vars()
            assert env_config == {"foo": "bar", "storage_backend": "gtc", "log_level": "DEBUG"}

    def test_load_config_from_env_vars_key_conversion(self) -> None:
        """Test that GTN_CONFIG_ prefix is removed and keys are lowercased."""
        with patch.dict(
            os.environ,
            {
                "GTN_CONFIG_SOME_LONG_KEY_NAME": "value1",
                "GTN_CONFIG_API_KEY": "value2",
                "GTN_CONFIG_123_NUMERIC": "value3",
            },
            clear=True,
        ):
            manager = ConfigManager()
            env_config = manager._load_config_from_env_vars()
            assert env_config == {"some_long_key_name": "value1", "api_key": "value2", "123_numeric": "value3"}

    def test_load_config_from_env_vars_nested_path(self) -> None:
        """A `__` separator maps to a nested dict, and the value is coerced to the field's type."""
        with patch.dict(os.environ, {"GTN_CONFIG_WORKER__HEARTBEAT_TIMEOUT_S": "30"}, clear=True):
            manager = ConfigManager()
            env_config = manager._load_config_from_env_vars()
            assert env_config == {"worker": {"heartbeat_timeout_s": 30.0}}

    def test_load_config_from_env_vars_three_level_path(self) -> None:
        """A three-level `__` path (app_events -> on_app_initialization_complete -> requires_engine) works."""
        with patch.dict(
            os.environ,
            {"GTN_CONFIG_APP_EVENTS__ON_APP_INITIALIZATION_COMPLETE__REQUIRES_ENGINE": ">=0.5,<0.6"},
            clear=True,
        ):
            manager = ConfigManager()
            env_config = manager._load_config_from_env_vars()
            assert env_config == {"app_events": {"on_app_initialization_complete": {"requires_engine": ">=0.5,<0.6"}}}

    def test_load_config_from_env_vars_sibling_nested_keys_merge(self) -> None:
        """Two `__` vars under the same parent merge into one dict instead of clobbering."""
        with patch.dict(
            os.environ,
            {
                "GTN_CONFIG_LIBRARY__LAZY_NODE_LOADING": "false",
                "GTN_CONFIG_LIBRARY__DEPENDENCY_INSTALL_BEHAVIOR": "never",
            },
            clear=True,
        ):
            manager = ConfigManager()
            env_config = manager._load_config_from_env_vars()
            assert env_config == {
                "library": {
                    "lazy_node_loading": False,
                    "dependency_install_behavior": "never",
                }
            }

    def test_load_config_from_env_vars_bool_coercion(self) -> None:
        """A bool-typed nested setting coerces 'false' to False, not the truthy string 'false'.

        library_manager.py reads `library.lazy_node_loading` with a bare `bool(...)` and no
        `cast_type`, so an uncoerced string would silently invert the setting.
        """
        with patch.dict(os.environ, {"GTN_CONFIG_LIBRARY__LAZY_NODE_LOADING": "false"}, clear=True):
            manager = ConfigManager()
            env_config = manager._load_config_from_env_vars()
            assert env_config["library"]["lazy_node_loading"] is False

    def test_load_config_from_env_vars_enum_coercion(self) -> None:
        """An enum-typed nested setting resolves to the enum's value."""
        with patch.dict(os.environ, {"GTN_CONFIG_LIBRARY__DEPENDENCY_INSTALL_BEHAVIOR": "never"}, clear=True):
            manager = ConfigManager()
            env_config = manager._load_config_from_env_vars()
            assert env_config["library"]["dependency_install_behavior"] == "never"

    def test_load_config_from_env_vars_invalid_typed_value_dropped(self) -> None:
        """A value the Settings model rejects for a typed setting is dropped, not merged."""
        with patch.dict(os.environ, {"GTN_CONFIG_MAX_NODES_IN_PARALLEL": "not-an-int"}, clear=True):
            manager = ConfigManager()
            env_config = manager._load_config_from_env_vars()
            assert env_config == {}

    def test_invalid_env_var_does_not_reset_the_rest_of_the_config(self, isolate_user_config: Path) -> None:
        """A single bad env var must not wipe the merged config back to defaults.

        `load_configs` calls `Settings.model_validate(merged_config)` on the whole merged dict, so
        an invalid value anywhere in it would fail validation for the entire config, resetting
        every setting, including the user's own config file, back to defaults. Validating each
        `GTN_CONFIG_` variable against its own single-key delta before it is merged keeps one bad
        variable's damage contained to that key instead.
        """
        isolate_user_config.write_text(json.dumps({"log_level": "ERROR"}), encoding="utf-8")

        with patch.dict(os.environ, {"GTN_CONFIG_MAX_NODES_IN_PARALLEL": "not-an-int"}, clear=True):
            manager = ConfigManager()
            manager.load_configs()

            assert manager.merged_config["log_level"] == "ERROR"

    def test_load_config_from_env_vars_unknown_nested_key_rejected(self, caplog: pytest.LogCaptureFixture) -> None:
        """A sub-key a declared nested model doesn't recognize is rejected, not kept as a raw string.

        `WorkerSettings` has no `heartbeat_timeout` field, only `heartbeat_timeout_s`, and being a
        strict model it drops an unrecognized sub-key from `model_dump()` rather than keeping it.
        That absence after validation is unambiguous: a free-form path, an entry in a
        mapping-valued setting, or one riding Settings' own `extra="allow"`, is retained through
        the dump instead, so an absent key here means the path names no real setting.
        """
        with patch.dict(os.environ, {"GTN_CONFIG_WORKER__BOGUS": "1"}, clear=True):
            with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
                manager = ConfigManager()
                env_config = manager._load_config_from_env_vars()

            assert env_config == {}
            warnings = [record for record in caplog.records if "there is no" in record.message]
            assert len(warnings) == 1

    def test_invalid_env_var_warning_logged_once_per_variable_and_value(self, caplog: pytest.LogCaptureFixture) -> None:
        """The same (variable, value) pair warns once even when the env layer is re-read.

        Several call sites re-read the env layer over a process's life (load_configs on every
        project switch, read_env_config for provisioning previews), so without deduping, one
        bad variable would re-warn every time.
        """
        with patch.dict(os.environ, {"GTN_CONFIG_MAX_NODES_IN_PARALLEL": "not-an-int"}, clear=True):
            with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
                manager = ConfigManager()
                manager._load_config_from_env_vars()
                manager.read_env_config()

            warnings = [record for record in caplog.records if "is not a valid value for the" in record.message]
            assert len(warnings) == 1

    def test_nested_env_var_reaches_merged_config_with_correct_type(self) -> None:
        """A nested env var survives merge_dicts and whole-config validation with the right type.

        The tests above check `_load_config_from_env_vars()` directly; this exercises the public
        pipeline (`load_configs()` -> `merged_config` / `get_config_value()`) that value actually
        has to travel through, proving it lands typed rather than as the raw string.
        """
        with patch.dict(os.environ, {"GTN_CONFIG_WORKER__HEARTBEAT_TIMEOUT_S": "30"}, clear=True):
            manager = ConfigManager()
            manager.load_configs()

            value = manager.get_config_value("worker.heartbeat_timeout_s")
            assert value == 30.0  # noqa: PLR2004
            assert isinstance(value, float)

    def test_nested_env_var_overrides_project_config_layer(self) -> None:
        """A `__` env var wins over the same nested key set in a project-adjacent config file.

        Mirrors test_workspace_config_overrides_project_config_but_not_env_vars above, but for a
        nested key, to prove GTN_CONFIG_ precedence holds through the full layer stack and not
        only for top-level settings.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "griptape_nodes_config.json").write_text(
                json.dumps({"worker": {"heartbeat_timeout_s": 99.0}})
            )

            with patch.dict(os.environ, {"GTN_CONFIG_WORKER__HEARTBEAT_TIMEOUT_S": "30"}, clear=True):
                manager = ConfigManager()
                manager.load_project_config(project_dir)

                assert manager.get_config_value("worker.heartbeat_timeout_s") == 30.0  # noqa: PLR2004

    def test_nested_env_var_does_not_clobber_sibling_defaults(self) -> None:
        """Setting one leaf under a parent must not blank out its siblings.

        `merge_dicts` overlays a nested dict onto the existing one rather than replacing the
        parent wholesale, so with only `heartbeat_timeout_s` exported, `heartbeat_interval_s`
        must still resolve to `WorkerSettings`'s own default.
        """
        with patch.dict(os.environ, {"GTN_CONFIG_WORKER__HEARTBEAT_TIMEOUT_S": "30"}, clear=True):
            manager = ConfigManager()
            manager.load_configs()

            assert manager.get_config_value("worker.heartbeat_timeout_s") == 30.0  # noqa: PLR2004
            assert manager.get_config_value("worker.heartbeat_interval_s") == 5.0  # noqa: PLR2004

    def test_flat_and_nested_worker_env_vars_together_nested_wins(self, caplog: pytest.LogCaptureFixture) -> None:
        """A flat GTN_CONFIG_WORKER cannot clobber GTN_CONFIG_WORKER__X, in either order.

        `worker` is a declared `WorkerSettings` field, so the bare string fails validation and is
        dropped for that reason rather than for overlapping. The deeper key therefore survives
        regardless of `os.environ` iteration order, and the prefix filter never has to weigh in.
        """
        for env in (
            {"GTN_CONFIG_WORKER": "x", "GTN_CONFIG_WORKER__HEARTBEAT_TIMEOUT_S": "30"},
            {"GTN_CONFIG_WORKER__HEARTBEAT_TIMEOUT_S": "30", "GTN_CONFIG_WORKER": "x"},
        ):
            caplog.clear()
            with patch.dict(os.environ, env, clear=True):
                with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
                    manager = ConfigManager()
                    env_config = manager._load_config_from_env_vars()

                assert env_config == {"worker": {"heartbeat_timeout_s": 30.0}}
                warnings = [
                    record for record in caplog.records if "is not a valid value for the 'worker'" in record.message
                ]
                assert len(warnings) == 1

    def test_invalid_deeper_path_does_not_discard_a_valid_shallower_override(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A rejected deeper variable leaves its valid prefix alone.

        Overlap is only a real conflict when both paths are individually valid, so a variable that
        fails validation must be dropped before overlap is judged. `agent.system_prompt` is a `str`
        with no sub-keys, so `…__OOPS` is rejected for its value and must not take its own prefix
        down with it. The trailing-separator case is rejected earlier still, at the split, and is
        kept here to pin that neither route reaches the prefix filter.
        """
        cases = [
            (
                {"GTN_CONFIG_AGENT__SYSTEM_PROMPT": "hi", "GTN_CONFIG_AGENT__SYSTEM_PROMPT__OOPS": "x"},
                {"agent": {"system_prompt": "hi"}},
            ),
            (
                {"GTN_CONFIG_WORKER__HEARTBEAT_TIMEOUT_S": "30", "GTN_CONFIG_WORKER__HEARTBEAT_TIMEOUT_S__": "99"},
                {"worker": {"heartbeat_timeout_s": 30.0}},
            ),
        ]
        for env, want in cases:
            caplog.clear()
            with patch.dict(os.environ, env, clear=True):
                with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
                    manager = ConfigManager()
                    env_config = manager._load_config_from_env_vars()

                assert env_config == want
                prefix_warnings = [
                    record for record in caplog.records if "is also the start of a longer path" in record.message
                ]
                assert prefix_warnings == []

    def test_empty_path_segment_is_rejected(self, caplog: pytest.LogCaptureFixture) -> None:
        """A separator that produces an empty path segment is ignored rather than applied.

        An empty segment is a legitimate key under a mapping or an undeclared tree, so validation
        accepts it. Left alone it would write garbage at the highest-priority layer, masking the
        config file underneath, and count as a longer path that discards its own correct prefix.
        A lone one has no prefix to discard and so would apply silently.
        """
        preview_format = "GTN_CONFIG_ARTIFACTS__IMAGE__PREVIEW_GENERATION__PREVIEW_FORMAT"
        cases = [
            ({f"{preview_format}__": "jpg"}, {}),
            (
                {preview_format: "png", f"{preview_format}__": "jpg"},
                {"artifacts": {"image": {"preview_generation": {"preview_format": "png"}}}},
            ),
            ({"GTN_CONFIG_ARTIFACTS____IMAGE": "y"}, {}),
            ({"GTN_CONFIG_": "x"}, {}),
            ({"GTN_CONFIG_NODES__FOO__BAR__": "b"}, {}),
        ]
        for env, want in cases:
            caplog.clear()
            with patch.dict(os.environ, env, clear=True):
                with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
                    manager = ConfigManager()
                    env_config = manager._load_config_from_env_vars()

                assert env_config == want
                assert any("has an empty path segment" in record.message for record in caplog.records)

    def test_mapping_entry_settable_via_env(self) -> None:
        """An entry in a mapping-valued setting (not a nested model) can be set with `__<KEY>`.

        `artifacts` is `dict[str, Any]`, not a Settings sub-model, so this exercises a different
        path than the worker/agent/library tests above: `Any` gives Pydantic nothing to drop, so
        an undeclared key survives `model_dump()` instead of being rejected the way an undeclared
        sub-key on a nested model is.
        """
        with patch.dict(os.environ, {"GTN_CONFIG_ARTIFACTS__SOME_KEY": "1"}, clear=True):
            manager = ConfigManager()
            env_config = manager._load_config_from_env_vars()
            assert env_config == {"artifacts": {"some_key": "1"}}

    def test_mapping_entry_key_is_lowercased(self) -> None:
        """A mapping key arrives lowercased, which is why some mappings are unreachable in practice.

        `project_workspaces` is the example the configuration guide uses. Its keys are project IDs
        or file paths, matched case-sensitively, so a mixed-case key set this way silently never
        matches the project it names. This pins the lowercasing that makes that true rather than
        endorsing it as a way to configure project workspaces.
        """
        with patch.dict(os.environ, {"GTN_CONFIG_PROJECT_WORKSPACES__MyProject": "/studio/ws"}, clear=True):
            manager = ConfigManager()
            env_config = manager._load_config_from_env_vars()
            assert env_config == {"project_workspaces": {"myproject": "/studio/ws"}}

    def test_overlapping_mapping_paths_resolve_identically_regardless_of_order(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Overlap under a mapping-valued parent is order-independent too, unlike a validation-based drop.

        `artifacts` is `dict[str, Any]`, so neither `artifacts.image` nor
        `artifacts.image.preview_format` is rejected by `Settings.model_validate` on its own: both
        are free-form entries an `Any`-typed mapping accepts. Without the prefix filter, whichever
        variable `os.environ` happened to yield last would silently win. The filter removes that
        dependency by dropping the shallower path outright, with a warning naming the longer one.
        """
        for env in (
            {"GTN_CONFIG_ARTIFACTS__IMAGE": "x", "GTN_CONFIG_ARTIFACTS__IMAGE__PREVIEW_FORMAT": "png"},
            {"GTN_CONFIG_ARTIFACTS__IMAGE__PREVIEW_FORMAT": "png", "GTN_CONFIG_ARTIFACTS__IMAGE": "x"},
        ):
            caplog.clear()
            with patch.dict(os.environ, env, clear=True):
                with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
                    manager = ConfigManager()
                    env_config = manager._load_config_from_env_vars()

                assert env_config == {"artifacts": {"image": {"preview_format": "png"}}}
                warnings = [
                    record for record in caplog.records if "is also the start of a longer path" in record.message
                ]
                assert len(warnings) == 1

    def test_free_form_path_kept_while_declared_model_typo_rejected(self) -> None:
        """The same absent-after-validation check tells a mapping entry apart from a model typo.

        `GTN_CONFIG_ARTIFACTS__SOME_KEY` (a `dict[str, Any]` entry) and `GTN_CONFIG_WORKER__BOGUS`
        (an unrecognized `WorkerSettings` sub-key) both validate against `Settings`, since neither
        is a fully unknown top-level key. Only the mapping entry survives `model_dump()`:
        `WorkerSettings` is a strict model and drops `bogus`, while `artifacts` being typed
        `dict[str, Any]` means Pydantic has nothing to drop.
        """
        with patch.dict(
            os.environ,
            {"GTN_CONFIG_ARTIFACTS__SOME_KEY": "1", "GTN_CONFIG_WORKER__BOGUS": "1"},
            clear=True,
        ):
            manager = ConfigManager()
            env_config = manager._load_config_from_env_vars()
            assert env_config == {"artifacts": {"some_key": "1"}}

    def test_invalid_enum_value_silently_becomes_the_built_in_default(self, isolate_user_config: Path) -> None:
        """An invalid `log_level` resolves to INFO rather than falling back to the config files.

        `log_level` has a `mode="before"` validator that catches its own lookup failure and
        returns LogLevel.INFO instead of raising, so an invalid GTN_CONFIG_LOG_LEVEL never
        reaches the reject-and-warn path this class exercises for other settings. Same for
        workflow_execution_mode, thread_storage_backend, and library.dependency_install_behavior.
        """
        isolate_user_config.write_text(json.dumps({"log_level": "DEBUG"}), encoding="utf-8")

        with patch.dict(os.environ, {"GTN_CONFIG_LOG_LEVEL": "NOTALEVEL"}, clear=True):
            manager = ConfigManager()

            # Silently becomes the schema default (INFO), not the user's file-configured DEBUG,
            # and with no warning logged -- unlike every other invalid-value case in this class.
            assert manager.get_config_value("log_level") == "INFO"

    def test_config_integration_with_env_vars(self) -> None:
        """Test that environment variables are integrated into merged config with highest priority."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Set up a temporary project directory with a project-adjacent config
            project_dir = Path(temp_dir)
            project_config_path = project_dir / "griptape_nodes_config.json"
            project_config_path.write_text('{"log_level": "ERROR"}')

            # Set environment variable that should override the project config
            with patch.dict(
                os.environ, {"GTN_CONFIG_LOG_LEVEL": "DEBUG", "GTN_CONFIG_STORAGE_BACKEND": "gtc"}, clear=True
            ):
                manager = ConfigManager()
                manager.load_project_config(project_dir)

                # Environment variable should override project config
                assert manager.get_config_value("log_level") == "DEBUG"
                assert manager.get_config_value("storage_backend") == "gtc"

    def test_load_project_config_sets_project_config_layer(self) -> None:
        """Test that load_project_config reads project-adjacent config and merges it."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            project_config_path = project_dir / "griptape_nodes_config.json"
            project_config_path.write_text('{"log_level": "ERROR"}')

            with patch.dict(os.environ, {}, clear=True):
                manager = ConfigManager()
                # Before loading project config, log_level is default
                assert manager.get_config_value("log_level") != "ERROR"

                manager.load_project_config(project_dir)

                # After loading, project config value takes effect
                assert manager.get_config_value("log_level") == "ERROR"
                assert manager.project_config == {"log_level": "ERROR"}

    def test_non_gtn_config_env_vars_ignored(self) -> None:
        """Test that environment variables not starting with GTN_CONFIG_ are ignored."""
        with patch.dict(
            os.environ,
            {
                "CONFIG_FOO": "should_be_ignored",
                "GTN_FOO": "should_be_ignored",
                "GTN_CONFIG_BAR": "should_be_loaded",
                "SOME_OTHER_VAR": "should_be_ignored",
            },
            clear=True,
        ):
            manager = ConfigManager()
            env_config = manager._load_config_from_env_vars()
            assert env_config == {"bar": "should_be_loaded"}

    def test_workspace_path_reassigned_after_env_var_override(self) -> None:
        """Test that workspace path is reassigned after environment variable config is loaded."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create initial workspace directory
            initial_workspace = Path(temp_dir) / "initial_workspace"
            initial_workspace.mkdir()

            # Create override workspace directory
            override_workspace = Path(temp_dir) / "override_workspace"
            override_workspace.mkdir()

            # Set environment variable to override workspace directory
            with patch.dict(os.environ, {"GTN_CONFIG_WORKSPACE_DIRECTORY": str(override_workspace)}, clear=True):
                manager = ConfigManager()
                # Initially set workspace to the initial directory
                manager.workspace_path = initial_workspace

                # Load configs which should reassign workspace path from env var
                manager.load_configs()

                # Verify workspace path was reassigned to the env var value
                assert manager.workspace_path == override_workspace.resolve()
                assert manager.get_config_value("workspace_directory") == str(override_workspace)

    def test_resolved_libraries_root_default_is_global_workspace_relative(self, isolate_user_config: Path) -> None:
        """With no override, the root is libraries_directory resolved against the GLOBAL workspace.

        The fallback uses configured_global_workspace_path() (the user/default config
        workspace_directory), NOT the active workspace_path override, so a self-contained project's
        workspace pin does not relocate the shared libraries dir.
        """
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {}, clear=True):
            workspace = Path(temp_dir) / "ws"
            workspace.mkdir()
            isolate_user_config.write_text(json.dumps({"workspace_directory": str(workspace)}), encoding="utf-8")
            manager = ConfigManager()

            # default libraries_directory is "libraries" (relative -> under the global workspace)
            assert manager.resolved_libraries_root() == (workspace / "libraries").resolve()

    def test_resolved_libraries_root_uses_override_verbatim(self, isolate_user_config: Path) -> None:
        """When an override is set, it is returned as-is, independent of the global workspace."""
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {}, clear=True):
            workspace = Path(temp_dir) / "ws"
            workspace.mkdir()
            shared = Path(temp_dir) / "shared-libs"
            shared.mkdir()
            isolate_user_config.write_text(json.dumps({"workspace_directory": str(workspace)}), encoding="utf-8")
            manager = ConfigManager()

            manager.set_libraries_root_override(shared)
            assert manager.resolved_libraries_root() == shared.resolve()

            # Clearing restores the global-workspace-relative default.
            manager.set_libraries_root_override(None)
            assert manager.resolved_libraries_root() == (workspace / "libraries").resolve()

    def test_clear_project_layers_clears_libraries_root_override(self, isolate_user_config: Path) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {}, clear=True):
            workspace = Path(temp_dir) / "ws"
            workspace.mkdir()
            shared = Path(temp_dir) / "shared-libs"
            shared.mkdir()
            isolate_user_config.write_text(json.dumps({"workspace_directory": str(workspace)}), encoding="utf-8")
            manager = ConfigManager()
            manager.set_libraries_root_override(shared)

            manager.clear_project_layers()

            assert manager.resolved_libraries_root() == (workspace / "libraries").resolve()

    def test_resolved_libraries_root_fallback_ignores_active_workspace_override(
        self, isolate_user_config: Path
    ) -> None:
        """A self-contained project's workspace override must NOT relocate the unset-libraries fallback.

        Regression guard: with no libraries override, resolved_libraries_root resolves against the
        GLOBAL configured workspace, not set_workspace_override's per-project pin. This is what keeps a
        v1 self-contained project (workspace_dir "./") sharing the global libraries dir.
        """
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {}, clear=True):
            global_ws = Path(temp_dir) / "global_ws"
            global_ws.mkdir()
            project_dir = Path(temp_dir) / "project"
            project_dir.mkdir()
            isolate_user_config.write_text(json.dumps({"workspace_directory": str(global_ws)}), encoding="utf-8")
            manager = ConfigManager()

            # Simulate activating a self-contained project: its workspace is pinned to its own dir.
            manager.set_workspace_override(project_dir)

            # Libraries still resolve under the GLOBAL workspace, not the project's own folder.
            assert manager.resolved_libraries_root() == (global_ws / "libraries").resolve()
            assert manager.resolved_libraries_root() != (project_dir / "libraries").resolve()

    def test_resolved_libraries_root_v0_style_is_noop(self, isolate_user_config: Path) -> None:
        """A v0-style project pins its workspace TO the global workspace, so the fallback is unchanged.

        A v0 project has no workspace_dir, so activation sets the override to the global workspace
        itself. The new global-workspace fallback then yields the same path the old workspace_path
        fallback did -- documenting that this change is a no-op for v0 and only affects self-contained
        v1 projects (whose override points at their own dir).
        """
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {}, clear=True):
            global_ws = Path(temp_dir) / "global_ws"
            global_ws.mkdir()
            isolate_user_config.write_text(json.dumps({"workspace_directory": str(global_ws)}), encoding="utf-8")
            manager = ConfigManager()

            # v0 activation pins the override to the global workspace (branch 5 of decide_workspace).
            manager.set_workspace_override(global_ws)

            assert manager.resolved_libraries_root() == (global_ws / "libraries").resolve()

    def test_default_libraries_root_falls_back_when_value_is_missing_or_empty(self, isolate_user_config: Path) -> None:
        """An absent, empty, or non-string libraries_directory resolves to `<global workspace>/libraries`.

        The fallback lives inside default_libraries_root so no caller can skip it. Without it, a
        caller that read the value with no default of its own would pass None (crash) or "" -- and
        `Path("")` is `Path(".")`, which would resolve the libraries root to the workspace itself and
        install libraries on top of the user's files.
        """
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {}, clear=True):
            workspace = Path(temp_dir) / "ws"
            workspace.mkdir()
            isolate_user_config.write_text(json.dumps({"workspace_directory": str(workspace)}), encoding="utf-8")
            manager = ConfigManager()
            expected = (workspace / "libraries").resolve()

            assert manager.default_libraries_root(None) == expected
            assert manager.default_libraries_root("") == expected
            # A hand-edited config can put any JSON type here; it must not become a path component.
            assert manager.default_libraries_root(5) == expected  # type: ignore[arg-type]

            # A real value is still honored, relative to the global workspace.
            assert manager.default_libraries_root("custom-libs") == (workspace / "custom-libs").resolve()

    def test_publishes_default_libraries_root_env_var(self, isolate_user_config: Path) -> None:
        """Construction publishes the default libraries root as an absolute path in os.environ.

        This is the variable project templates reference from `libraries_dir`, so it must be absolute:
        a relative value there would anchor to the project YAML's directory, not the workspace.
        """
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {}, clear=True):
            workspace = Path(temp_dir) / "ws"
            workspace.mkdir()
            isolate_user_config.write_text(json.dumps({"workspace_directory": str(workspace)}), encoding="utf-8")

            ConfigManager()

            published = os.environ[DEFAULT_LIBRARIES_ROOT_ENV_VAR]
            assert Path(published).is_absolute()
            assert Path(published) == (workspace / "libraries").resolve()

    def test_published_default_libraries_root_honors_config_env_overrides(self, isolate_user_config: Path) -> None:
        """The published value reflects GTN_CONFIG_ overrides of libraries_directory and the workspace.

        The variable describes where THIS engine installs libraries, so the customizations that move
        that location have to be visible in it. Only a project's own libraries_dir is excluded, and
        for a different reason (see test_published_default_libraries_root_ignores_libraries_root_override).
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "ws"
            workspace.mkdir()
            env_workspace = Path(temp_dir) / "env_ws"
            env_workspace.mkdir()
            isolate_user_config.write_text(json.dumps({"workspace_directory": str(workspace)}), encoding="utf-8")

            # A relative libraries_directory is resolved against the env-var workspace, not the config one.
            with patch.dict(
                os.environ,
                {
                    "GTN_CONFIG_WORKSPACE_DIRECTORY": str(env_workspace),
                    "GTN_CONFIG_LIBRARIES_DIRECTORY": "shared-libs",
                },
                clear=True,
            ):
                ConfigManager()
                assert Path(os.environ[DEFAULT_LIBRARIES_ROOT_ENV_VAR]) == (env_workspace / "shared-libs").resolve()

            # An absolute libraries_directory wins outright and ignores the workspace.
            absolute_libs = Path(temp_dir) / "absolute-libs"
            with patch.dict(os.environ, {"GTN_CONFIG_LIBRARIES_DIRECTORY": str(absolute_libs)}, clear=True):
                ConfigManager()
                assert Path(os.environ[DEFAULT_LIBRARIES_ROOT_ENV_VAR]) == absolute_libs.resolve()

    def test_published_default_libraries_root_ignores_libraries_root_override(self, isolate_user_config: Path) -> None:
        """A project's libraries_dir must NOT change the published value. This is the circularity guard.

        `libraries_dir` is the field that READS this variable, so if the variable tracked
        resolved_libraries_root() -- which returns the override -- its value would depend on the field
        consuming it. Publishing happens once at construction and carries default_libraries_root, so
        activating a project with its own libraries_dir leaves the variable alone.
        """
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {}, clear=True):
            workspace = Path(temp_dir) / "ws"
            workspace.mkdir()
            project_libs = Path(temp_dir) / "project-libs"
            project_libs.mkdir()
            isolate_user_config.write_text(json.dumps({"workspace_directory": str(workspace)}), encoding="utf-8")
            manager = ConfigManager()

            manager.set_libraries_root_override(project_libs)
            # The live root follows the project; the published default deliberately does not.
            assert manager.resolved_libraries_root() == project_libs.resolve()
            assert Path(os.environ[DEFAULT_LIBRARIES_ROOT_ENV_VAR]) == (workspace / "libraries").resolve()

            # A remerge triggered by activation must not republish either.
            manager.load_configs()
            assert Path(os.environ[DEFAULT_LIBRARIES_ROOT_ENV_VAR]) == (workspace / "libraries").resolve()

    def test_published_default_libraries_root_overwrites_stale_value(self, isolate_user_config: Path) -> None:
        """A pre-existing value in the environment is replaced, not preserved.

        The variable is published, not configured. A stale shell value would misreport where this
        engine installs libraries; libraries_directory and libraries_dir are the supported ways to
        move that location.
        """
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.dict(os.environ, {DEFAULT_LIBRARIES_ROOT_ENV_VAR: "/stale/from/the/users/shell"}, clear=True),
        ):
            workspace = Path(temp_dir) / "ws"
            workspace.mkdir()
            isolate_user_config.write_text(json.dumps({"workspace_directory": str(workspace)}), encoding="utf-8")

            ConfigManager()

            assert Path(os.environ[DEFAULT_LIBRARIES_ROOT_ENV_VAR]) == (workspace / "libraries").resolve()

    def test_published_default_libraries_root_resolves_in_a_project_path_field(self, isolate_user_config: Path) -> None:
        """End to end: the published variable is usable in `libraries_dir` with no user setup.

        This is the whole point of publishing it. A project naming an unset variable is refused at load
        time, so if publishing regressed, such a project would become unusable rather than degrade.
        """
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {}, clear=True):
            workspace = Path(temp_dir) / "ws"
            workspace.mkdir()
            isolate_user_config.write_text(json.dumps({"workspace_directory": str(workspace)}), encoding="utf-8")
            ConfigManager()

            resolution = resolve_project_path_field(
                f"${{{DEFAULT_LIBRARIES_ROOT_ENV_VAR}}}/shared", Path(temp_dir) / "project"
            )

            assert resolution.unresolved_variables == []
            assert resolution.path == (workspace / "libraries" / "shared").resolve()

    def test_coerce_to_type_bool_from_string(self) -> None:
        """Test that _coerce_to_type correctly converts string values to bool."""
        manager = ConfigManager()

        # Truthy string values
        assert manager._coerce_to_type("true", bool) is True
        assert manager._coerce_to_type("True", bool) is True
        assert manager._coerce_to_type("TRUE", bool) is True
        assert manager._coerce_to_type("yes", bool) is True
        assert manager._coerce_to_type("1", bool) is True
        assert manager._coerce_to_type("anything", bool) is True

        # Falsy string values
        assert manager._coerce_to_type("false", bool) is False
        assert manager._coerce_to_type("False", bool) is False
        assert manager._coerce_to_type("FALSE", bool) is False
        assert manager._coerce_to_type("no", bool) is False
        assert manager._coerce_to_type("No", bool) is False
        assert manager._coerce_to_type("0", bool) is False
        assert manager._coerce_to_type("", bool) is False

    def test_coerce_to_type_bool_from_bool(self) -> None:
        """Test that _coerce_to_type returns bool values unchanged."""
        manager = ConfigManager()

        assert manager._coerce_to_type(True, bool) is True
        assert manager._coerce_to_type(False, bool) is False

    def test_coerce_to_type_int(self) -> None:
        """Test that _coerce_to_type correctly converts string values to int."""
        manager = ConfigManager()

        assert manager._coerce_to_type("42", int) == int("42")
        assert manager._coerce_to_type("0", int) == int("0")
        assert manager._coerce_to_type("-10", int) == int("-10")

    def test_coerce_to_type_float(self) -> None:
        """Test that _coerce_to_type correctly converts string values to float."""
        manager = ConfigManager()

        assert manager._coerce_to_type("3.14", float) == float("3.14")
        assert manager._coerce_to_type("0.0", float) == float("0.0")
        assert manager._coerce_to_type("-2.5", float) == float("-2.5")
        assert manager._coerce_to_type("42", float) == float("42")

    def test_coerce_to_type_str(self) -> None:
        """Test that _coerce_to_type returns string values unchanged."""
        manager = ConfigManager()

        assert manager._coerce_to_type("hello", str) == "hello"
        assert manager._coerce_to_type("", str) == ""

    def test_get_config_value_with_cast_type_bool(self) -> None:
        """Test get_config_value with cast_type=bool for env var string values."""
        with patch.dict(os.environ, {"GTN_CONFIG_ENABLE_FEATURE": "false"}, clear=True):
            manager = ConfigManager()
            manager.load_configs()

            # Without cast_type, returns the string "false" (truthy)
            value_no_cast = manager.get_config_value("enable_feature")
            assert value_no_cast == "false"
            assert bool(value_no_cast) is True  # String "false" is truthy!

            # With cast_type=bool, returns False
            value_with_cast = manager.get_config_value("enable_feature", cast_type=bool)
            assert value_with_cast is False

    def test_get_config_value_with_cast_type_int(self) -> None:
        """Test get_config_value with cast_type=int for env var string values."""
        with patch.dict(os.environ, {"GTN_CONFIG_MAX_COUNT": "100"}, clear=True):
            manager = ConfigManager()
            manager.load_configs()

            # Without cast_type, returns the string "100"
            value_no_cast = manager.get_config_value("max_count")
            assert value_no_cast == "100"

            # With cast_type=int, returns 100
            value_with_cast = manager.get_config_value("max_count", cast_type=int)
            assert value_with_cast == int("100")
            assert isinstance(value_with_cast, int)

    def test_load_workspace_config_sets_workspace_layer(self) -> None:
        """Test that load_workspace_config reads workspace config and merges it above project config."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir) / "project"
            project_dir.mkdir()
            workspace_dir = Path(temp_dir) / "workspace"
            workspace_dir.mkdir()

            (project_dir / "griptape_nodes_config.json").write_text('{"log_level": "ERROR"}')
            (workspace_dir / "griptape_nodes_config.json").write_text('{"log_level": "DEBUG"}')

            with patch.dict(os.environ, {}, clear=True):
                manager = ConfigManager()
                manager.load_project_config(project_dir)
                assert manager.get_config_value("log_level") == "ERROR"

                manager.load_workspace_config(workspace_dir)
                # Workspace config overrides project-adjacent config
                assert manager.get_config_value("log_level") == "DEBUG"
                assert manager.workspace_config == {"log_level": "DEBUG"}

    def test_load_workspace_config_skips_duplicate_when_same_as_project(self) -> None:
        """Test that workspace config is skipped when workspace dir equals project dir."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "griptape_nodes_config.json").write_text('{"log_level": "WARNING"}')

            with patch.dict(os.environ, {}, clear=True):
                manager = ConfigManager()
                manager.load_project_config(project_dir)
                # Now load workspace config from same dir — should be skipped
                manager.load_workspace_config(project_dir)

                assert manager.get_config_value("log_level") == "WARNING"
                # workspace_config is empty because the duplicate was skipped
                assert manager.workspace_config == {}

    def test_workspace_config_overrides_project_config_but_not_env_vars(self) -> None:
        """Test that workspace config wins over project config but loses to env vars."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir) / "project"
            project_dir.mkdir()
            workspace_dir = Path(temp_dir) / "workspace"
            workspace_dir.mkdir()

            (project_dir / "griptape_nodes_config.json").write_text('{"log_level": "ERROR"}')
            (workspace_dir / "griptape_nodes_config.json").write_text('{"log_level": "WARNING"}')

            with patch.dict(os.environ, {"GTN_CONFIG_LOG_LEVEL": "DEBUG"}, clear=True):
                manager = ConfigManager()
                manager.load_project_config(project_dir)
                manager.load_workspace_config(workspace_dir)

                # Env var wins over workspace config
                assert manager.get_config_value("log_level") == "DEBUG"

    def test_get_config_value_workspace_config_source(self) -> None:
        """Test that get_config_value can read from workspace_config source specifically."""
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            (workspace_dir / "griptape_nodes_config.json").write_text('{"log_level": "DEBUG"}')

            with patch.dict(os.environ, {}, clear=True):
                manager = ConfigManager()
                manager.load_workspace_config(workspace_dir)

                value = manager.get_config_value("log_level", config_source="workspace_config")
                assert value == "DEBUG"

    def test_workspace_config_missing_file_is_empty(self) -> None:
        """Test that load_workspace_config with no config file results in empty workspace_config."""
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            # No griptape_nodes_config.json created

            with patch.dict(os.environ, {}, clear=True):
                manager = ConfigManager()
                manager.load_workspace_config(workspace_dir)

                assert manager.workspace_config == {}

    def test_workspace_override_survives_load_configs(self) -> None:
        """Test that set_workspace_override persists through load_configs() calls."""
        with tempfile.TemporaryDirectory() as temp_dir:
            override_dir = Path(temp_dir)

            with patch.dict(os.environ, {}, clear=True):
                manager = ConfigManager()
                manager.set_workspace_override(override_dir)

                manager.load_configs()

                assert manager.workspace_path == override_dir.resolve()
                assert manager.merged_config["workspace_directory"] == str(override_dir.resolve())

    def test_workspace_override_loses_to_env_var(self) -> None:
        """Test that GTN_CONFIG_WORKSPACE_DIRECTORY still wins over the runtime override."""
        with tempfile.TemporaryDirectory() as temp_dir:
            override_dir = Path(temp_dir) / "override"
            override_dir.mkdir()
            env_dir = Path(temp_dir) / "env"
            env_dir.mkdir()

            with patch.dict(os.environ, {"GTN_CONFIG_WORKSPACE_DIRECTORY": str(env_dir)}, clear=True):
                manager = ConfigManager()
                manager.set_workspace_override(override_dir)

                manager.load_configs()

                assert manager.workspace_path == env_dir.resolve()

    def test_workspace_override_cleared_on_reset(self) -> None:
        """Test that reset_user_config clears the runtime workspace override."""
        with tempfile.TemporaryDirectory() as temp_dir:
            override_dir = Path(temp_dir)

            with patch.dict(os.environ, {}, clear=True):
                manager = ConfigManager()
                manager.set_workspace_override(override_dir)
                assert manager.workspace_path == override_dir.resolve()

                manager.reset_user_config()

                assert manager._workspace_dir_override is None
                assert manager.workspace_path != override_dir.resolve()

    def test_set_workspace_override_none_clears(self) -> None:
        """Test that set_workspace_override(None) clears a previously set override."""
        with tempfile.TemporaryDirectory() as temp_dir:
            override_dir = Path(temp_dir)

            with patch.dict(os.environ, {}, clear=True):
                manager = ConfigManager()
                default_workspace = manager.workspace_path

                manager.set_workspace_override(override_dir)
                assert manager.workspace_path == override_dir.resolve()

                manager.set_workspace_override(None)
                assert manager._workspace_dir_override is None

                manager.load_configs()
                assert manager.workspace_path == default_workspace

    def test_clear_project_layers_resets_override_and_config_paths(self) -> None:
        """clear_project_layers() drops the override and both config-file paths to None.

        Regression guard for the per-activation state-leak: switching projects (or rolling
        back to one) must not inherit the prior project's workspace override or its
        project-adjacent/workspace config-file layers.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir) / "project"
            project_dir.mkdir()
            workspace_dir = Path(temp_dir) / "workspace"
            workspace_dir.mkdir()

            with patch.dict(os.environ, {}, clear=True):
                manager = ConfigManager()
                manager.set_workspace_override(project_dir)
                manager.load_project_config(project_dir)
                manager.load_workspace_config(workspace_dir)

                assert manager._workspace_dir_override is not None
                assert manager._project_config_path is not None
                assert manager._workspace_config_path is not None

                manager.clear_project_layers()

                assert manager._workspace_dir_override is None
                assert manager._project_config_path is None
                assert manager._workspace_config_path is None


@pytest.mark.skipif(
    platform.system() == "Windows", reason="xdg_base_dirs cannot find XDG_CONFIG_HOME on Windows on GitHub Actions"
)
class TestConfigManagerEventEmission:
    """Test that ConfigManager emits ConfigChanged events when config values change."""

    def test_set_config_value_emits_config_changed_event(self) -> None:
        """Test that set_config_value emits a ConfigChanged event."""
        event_manager = EventManager()
        config_manager = ConfigManager(event_manager=event_manager)

        received_events = []

        def listener(event: ConfigChanged) -> None:
            received_events.append(event)

        event_manager.add_listener_to_app_event(ConfigChanged, listener)

        # Set a config value
        config_manager.set_config_value(key="test_key", value="new_value")

        # Verify event was emitted
        assert len(received_events) == 1
        event = received_events[0]
        assert event.key == "test_key"
        assert event.new_value == "new_value"

    def test_set_config_value_captures_old_value(self) -> None:
        """Test that ConfigChanged event contains the old value before the change."""
        event_manager = EventManager()
        config_manager = ConfigManager(event_manager=event_manager)

        received_events = []

        def listener(event: ConfigChanged) -> None:
            received_events.append(event)

        event_manager.add_listener_to_app_event(ConfigChanged, listener)

        # Set initial value
        config_manager.set_config_value(key="test_key", value="initial_value")

        # Update to new value
        config_manager.set_config_value(key="test_key", value="updated_value")

        # Verify the second event has the correct old_value
        assert len(received_events) == 2  # noqa: PLR2004
        second_event = received_events[1]
        assert second_event.key == "test_key"
        assert second_event.old_value == "initial_value"
        assert second_event.new_value == "updated_value"

    def test_set_config_category_emits_config_changed_event(self) -> None:
        """Test that set_config_category emits a ConfigChanged event."""
        event_manager = EventManager()
        config_manager = ConfigManager(event_manager=event_manager)

        received_events = []

        def listener(event: ConfigChanged) -> None:
            received_events.append(event)

        event_manager.add_listener_to_app_event(ConfigChanged, listener)

        # Set a config category
        from griptape_nodes.retained_mode.events.config_events import SetConfigCategoryRequest

        request = SetConfigCategoryRequest(category="test_category", contents={"key1": "value1", "key2": "value2"})
        config_manager.on_handle_set_config_category_request(request)

        # Verify event was emitted
        assert len(received_events) == 1
        event = received_events[0]
        assert event.key == "test_category"
        assert event.new_value == {"key1": "value1", "key2": "value2"}

    def test_set_config_value_no_event_when_event_manager_is_none(self) -> None:
        """Test that no event is emitted when event_manager is None."""
        config_manager = ConfigManager(event_manager=None)

        # This should not raise any exceptions
        config_manager.set_config_value(key="test_key", value="new_value")

    def test_set_config_category_full_config_replacement_emits_event(self) -> None:
        """Test that setting the entire config (category=None) emits an event."""
        event_manager = EventManager()
        config_manager = ConfigManager(event_manager=event_manager)

        received_events = []

        def listener(event: ConfigChanged) -> None:
            received_events.append(event)

        event_manager.add_listener_to_app_event(ConfigChanged, listener)

        # Set entire config
        from griptape_nodes.retained_mode.events.config_events import SetConfigCategoryRequest

        full_config = {"workspace_directory": "/test/path", "log_level": "DEBUG"}
        request = SetConfigCategoryRequest(category=None, contents=full_config)
        config_manager.on_handle_set_config_category_request(request)

        # Verify event was emitted
        assert len(received_events) == 1
        event = received_events[0]
        assert event.key == ""
        assert event.new_value == full_config

    def test_multiple_config_changes_emit_multiple_events(self) -> None:
        """Test that multiple config changes emit separate events."""
        event_manager = EventManager()
        config_manager = ConfigManager(event_manager=event_manager)

        received_events = []

        def listener(event: ConfigChanged) -> None:
            received_events.append(event)

        event_manager.add_listener_to_app_event(ConfigChanged, listener)

        # Make multiple changes
        config_manager.set_config_value(key="key1", value="value1")
        config_manager.set_config_value(key="key2", value="value2")
        config_manager.set_config_value(key="key3", value="value3")

        # Verify all events were emitted
        assert len(received_events) == 3  # noqa: PLR2004
        assert received_events[0].key == "key1"
        assert received_events[1].key == "key2"
        assert received_events[2].key == "key3"

    def test_config_changed_event_includes_nested_key(self) -> None:
        """Test that ConfigChanged event correctly handles nested config keys."""
        event_manager = EventManager()
        config_manager = ConfigManager(event_manager=event_manager)

        received_events = []

        def listener(event: ConfigChanged) -> None:
            received_events.append(event)

        event_manager.add_listener_to_app_event(ConfigChanged, listener)

        # Set a nested config value
        config_manager.set_config_value(
            key="app_events.on_app_initialization_complete.libraries_to_register", value=["/path/to/lib"]
        )

        # Verify event has the full nested key
        assert len(received_events) == 1
        event = received_events[0]
        assert event.key == "app_events.on_app_initialization_complete.libraries_to_register"
        assert event.new_value == ["/path/to/lib"]

    def test_libraries_to_register_accepts_mixed_str_and_object_entries(self) -> None:
        """Settings validation accepts a mix of bare path strings and objects with `enabled`."""
        from griptape_nodes.retained_mode.managers.settings import LibraryRegistration, Settings

        validated = Settings.model_validate(
            {
                "app_events": {
                    "on_app_initialization_complete": {
                        "libraries_to_register": [
                            "/path/to/enabled.json",
                            {"path": "/path/to/disabled.json", "enabled": False},
                        ],
                    },
                },
            },
        )

        entries = validated.app_events.on_app_initialization_complete.libraries_to_register
        assert entries[0] == "/path/to/enabled.json"
        assert isinstance(entries[1], LibraryRegistration)
        assert entries[1].path == "/path/to/disabled.json"
        assert entries[1].enabled is False

        # Round-trip: bare strings stay strings, objects stay objects.
        # Object form serializes every field on LibraryRegistration: path, enabled,
        # and worker_mode_override (which defaults to None when the user didn't set one).
        dumped = validated.app_events.on_app_initialization_complete.model_dump()
        assert dumped["libraries_to_register"] == [
            "/path/to/enabled.json",
            {
                "path": "/path/to/disabled.json",
                "enabled": False,
                "worker_mode_override": None,
            },
        ]


@pytest.mark.skipif(
    platform.system() == "Windows", reason="xdg_base_dirs cannot find XDG_CONFIG_HOME on Windows on GitHub Actions"
)
class TestConfigManagerEventGating:
    """``ConfigChanged`` must only fire when the disk write actually landed.

    Listeners (in production: WorkerManager fans out ReloadConfigRequest to
    every registered worker) consume ConfigChanged. Emitting on a failed
    write would tell every consumer to act on a state that does not exist
    on disk -- e.g. workers reload the file and either see stale values or
    fail to find the new key.
    """

    def test_set_config_value_does_not_emit_when_write_fails(self) -> None:
        event_manager = EventManager()
        config_manager = ConfigManager(event_manager=event_manager)

        received: list[ConfigChanged] = []
        event_manager.add_listener_to_app_event(ConfigChanged, received.append)

        with patch.object(config_manager, "_write_user_config_delta", return_value=False):
            config_manager.set_config_value(key="test_key", value="new_value")

        assert received == []

    def test_set_config_value_emits_when_write_succeeds(self) -> None:
        event_manager = EventManager()
        config_manager = ConfigManager(event_manager=event_manager)

        received: list[ConfigChanged] = []
        event_manager.add_listener_to_app_event(ConfigChanged, received.append)

        with patch.object(config_manager, "_write_user_config_delta", return_value=True):
            config_manager.set_config_value(key="test_key", value="new_value")

        assert len(received) == 1
        assert received[0].key == "test_key"
        assert received[0].new_value == "new_value"

    def test_set_config_category_full_replacement_returns_failure_when_write_fails(self) -> None:
        from griptape_nodes.retained_mode.events.config_events import (
            SetConfigCategoryRequest,
            SetConfigCategoryResultFailure,
        )

        event_manager = EventManager()
        config_manager = ConfigManager(event_manager=event_manager)

        received: list[ConfigChanged] = []
        event_manager.add_listener_to_app_event(ConfigChanged, received.append)

        request = SetConfigCategoryRequest(category=None, contents={"any": "thing"})
        with patch.object(config_manager, "_write_user_config_delta", return_value=False):
            result = config_manager.on_handle_set_config_category_request(request)

        assert isinstance(result, SetConfigCategoryResultFailure)
        assert received == []

    def test_set_config_category_non_empty_category_returns_failure_when_write_fails(self) -> None:
        """The non-empty-category branch routes through ``set_config_value``; failure must propagate."""
        from griptape_nodes.retained_mode.events.config_events import (
            SetConfigCategoryRequest,
            SetConfigCategoryResultFailure,
        )

        event_manager = EventManager()
        config_manager = ConfigManager(event_manager=event_manager)

        received: list[ConfigChanged] = []
        event_manager.add_listener_to_app_event(ConfigChanged, received.append)

        request = SetConfigCategoryRequest(category="some_category", contents={"any": "thing"})
        with patch.object(config_manager, "_write_user_config_delta", return_value=False):
            result = config_manager.on_handle_set_config_category_request(request)

        assert isinstance(result, SetConfigCategoryResultFailure)
        assert received == []

    def test_set_config_value_request_returns_failure_when_write_fails(self) -> None:
        """The set-value handler must surface a failure result when the write didn't land."""
        from griptape_nodes.retained_mode.events.config_events import (
            SetConfigValueRequest,
            SetConfigValueResultFailure,
        )

        event_manager = EventManager()
        config_manager = ConfigManager(event_manager=event_manager)

        received: list[ConfigChanged] = []
        event_manager.add_listener_to_app_event(ConfigChanged, received.append)

        request = SetConfigValueRequest(category_and_key="some.key", value="v")
        with patch.object(config_manager, "_write_user_config_delta", return_value=False):
            result = config_manager.on_handle_set_config_value_request(request)

        assert isinstance(result, SetConfigValueResultFailure)
        assert received == []

    def test_set_config_value_returns_true_on_success_and_false_on_failure(self) -> None:
        """``set_config_value`` exposes the write outcome so handlers can propagate failure."""
        config_manager = ConfigManager()

        with patch.object(config_manager, "_write_user_config_delta", return_value=True):
            assert config_manager.set_config_value(key="k", value="v") is True

        with patch.object(config_manager, "_write_user_config_delta", return_value=False):
            assert config_manager.set_config_value(key="k", value="v") is False


class TestConfigManagerUtf8:
    """_load_config_from_file must read UTF-8 regardless of the platform locale."""

    def test_reads_utf8_config_when_locale_is_cp949(self, tmp_path: Path) -> None:
        config_data = {"workspace": "C:\\Users\\한국어\\griptape"}
        config_file = tmp_path / "griptape_nodes_config.json"
        config_file.write_text(json.dumps(config_data), encoding="utf-8")

        manager = ConfigManager.__new__(ConfigManager)

        with patch("locale.getpreferredencoding", return_value="cp949"):
            loaded = manager._load_config_from_file(config_file, "test")

        assert loaded.contents == config_data
        assert loaded.parse_error is None

    def test_returns_empty_dict_on_unicode_decode_error(self, tmp_path: Path) -> None:
        config_file = tmp_path / "griptape_nodes_config.json"
        config_file.write_bytes(b'{"key": "\xb9\xd9"}')  # cp949-encoded bytes, not valid UTF-8

        manager = ConfigManager.__new__(ConfigManager)
        loaded = manager._load_config_from_file(config_file, "test")

        assert loaded.contents == {}
        assert loaded.parse_error is not None


class TestComputeProjectProvisioningConfig:
    """`compute_project_provisioning_config` builds a project's merged config read-only.

    The provisioning preview uses it so its plan reflects the same effective
    `libraries_to_register` / `engine_version` the live reconcile reads after
    activation, instead of the project-adjacent file alone.
    """

    @staticmethod
    def _write_config(path: Path, dot_key: str, value: object) -> None:

        path.write_text(json.dumps(set_dot_value({}, dot_key, value)), encoding="utf-8")

    def test_workspace_layer_overrides_project_adjacent_libraries(self, tmp_path: Path) -> None:
        """A separate-dir workspace config's libraries_to_register wins over the project file.

        Mirrors load_configs's last-writer-wins replacement (merge_lists=False), so the
        preview must read the merged value, not the project-adjacent one.
        """
        project_dir = tmp_path / "project"
        workspace_dir = tmp_path / "workspace"
        project_dir.mkdir()
        workspace_dir.mkdir()
        self._write_config(project_dir / "griptape_nodes_config.json", LIBRARIES_TO_REGISTER_KEY, ["project-lib"])
        self._write_config(workspace_dir / "griptape_nodes_config.json", LIBRARIES_TO_REGISTER_KEY, ["workspace-lib"])

        with patch.dict(os.environ, {}, clear=True):
            manager = ConfigManager()
            merged = manager.compute_project_provisioning_config(project_dir, workspace_dir, apply_override=True)

        assert get_dot_value(merged, LIBRARIES_TO_REGISTER_KEY) == ["workspace-lib"]

    def test_env_var_overrides_all_file_layers(self, tmp_path: Path) -> None:
        """A GTN_CONFIG_ env var sits above every config-file layer, matching load_configs.

        storage_backend is Literal["local", "gtc"], so both layers must use valid values;
        the point under test is precedence, not the literal strings.
        """
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        self._write_config(project_dir / "griptape_nodes_config.json", "storage_backend", "local")

        with patch.dict(os.environ, {"GTN_CONFIG_STORAGE_BACKEND": "gtc"}, clear=True):
            manager = ConfigManager()
            merged = manager.compute_project_provisioning_config(project_dir, project_dir, apply_override=True)

        assert get_dot_value(merged, "storage_backend") == "gtc"

    def test_self_contained_project_skips_duplicate_workspace_layer(self, tmp_path: Path) -> None:
        """When workspace dir == project dir, the project-adjacent file is the only file layer.

        Matches load_configs's guard that skips loading the same file twice; the single
        file's value still lands in the merged config.
        """
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        self._write_config(project_dir / "griptape_nodes_config.json", LIBRARIES_TO_REGISTER_KEY, ["only-lib"])

        with patch.dict(os.environ, {}, clear=True):
            manager = ConfigManager()
            merged = manager.compute_project_provisioning_config(project_dir, project_dir, apply_override=True)

        assert get_dot_value(merged, LIBRARIES_TO_REGISTER_KEY) == ["only-lib"]
        # apply_override resolves the dir the same way set_workspace_override does.
        assert merged["workspace_directory"] == str(project_dir.expanduser().resolve())

    def test_does_not_mutate_live_config_state(self, tmp_path: Path) -> None:
        """The computation is read-only: it leaves the live merged config and layer paths intact."""
        project_dir = tmp_path / "project"
        workspace_dir = tmp_path / "workspace"
        project_dir.mkdir()
        workspace_dir.mkdir()
        self._write_config(project_dir / "griptape_nodes_config.json", "storage_backend", "from-project")

        with patch.dict(os.environ, {}, clear=True):
            manager = ConfigManager()
            merged_before = manager.merged_config.copy()
            project_path_before = manager._project_config_path
            workspace_path_before = manager._workspace_config_path
            override_before = manager._workspace_dir_override

            manager.compute_project_provisioning_config(project_dir, workspace_dir, apply_override=True)

        assert manager.merged_config == merged_before
        assert manager._project_config_path == project_path_before
        assert manager._workspace_config_path == workspace_path_before
        assert manager._workspace_dir_override == override_before

    def test_system_defaults_config_ignores_project_and_workspace_files(
        self, tmp_path: Path, isolate_user_config: Path
    ) -> None:
        """The system-defaults config reads only defaults->user->env, never a project/workspace file.

        The system-defaults activation path loads no project-adjacent or workspace
        griptape_nodes_config.json, so neither may leak into this preview, or the plan
        would diverge from what the switch actually reconciles.
        """
        # A stray config file sitting in cwd-adjacent dirs must not be consulted.
        self._write_config(tmp_path / "griptape_nodes_config.json", LIBRARIES_TO_REGISTER_KEY, ["stray-file-lib"])
        self._write_config(isolate_user_config, LIBRARIES_TO_REGISTER_KEY, ["user-pin-lib"])

        with patch.dict(os.environ, {}, clear=True):
            manager = ConfigManager()
            merged = manager.compute_system_defaults_provisioning_config()

        assert get_dot_value(merged, LIBRARIES_TO_REGISTER_KEY) == ["user-pin-lib"]


class TestProvisioningPreviewMatchesActivation:
    """The provisioning preview's merged config matches what activation actually produces.

    Defect #2 was the preview reading only the project-adjacent file while reconcile reads
    the fully-merged config, so the plan a user approved could differ from what activation
    did. The fix routes both through ConfigManager.compute_project_provisioning_config and a
    single ProjectManager.decide_workspace. These tests drive a real ConfigManager through the
    live _activate_project sequence (clear_project_layers -> load_project_config -> conditional
    set_workspace_override -> load_workspace_config) and, independently, through the preview path
    (read_config_file / read_env_config -> decide_workspace -> compute_project_provisioning_config),
    then assert the two agree on the only keys the preview consumes (libraries_to_register,
    engine_version) plus workspace_directory.

    Equality is asserted per-key, not as a blanket ==: the live merged config also carries
    unrelated layers (e.g. project_workspaces from the user config) that the preview legitimately
    includes too, so a blanket == would be hostage to that noise and to scalar normalization.
    All five decide_workspace branches are covered, since each resolves the workspace layer
    differently and is the surface where preview and live could drift.
    """

    @staticmethod
    def _write_config_file(path: Path, values: dict[str, object]) -> None:

        config: dict = {}
        for dot_key, value in values.items():
            set_dot_value(config, dot_key, value)
        path.write_text(json.dumps(config), encoding="utf-8")

    @staticmethod
    def _assert_preview_matches_live(  # noqa: PLR0913
        cm: ConfigManager,
        project_dir: Path,
        project_file: Path,
        *,
        expected_libraries: list,
        expected_engine_version: str,
        pm: ProjectManager | None = None,
    ) -> None:
        """Compute the preview and live-activation merged configs and assert they agree.

        Mirrors resolve_provisioning_config_dirs -> compute_project_provisioning_config for the
        preview (read-only, before any activation mutation) and _activate_project's workspace
        block for the live path, then cross-checks the consumed keys + workspace_directory. The
        expected-winner assertions prove the workspace layer was actually consumed, so a bug that
        made BOTH paths ignore it (preview == live but both wrong) still fails.

        `pm` lets a caller pass a ProjectManager whose registry already models a parent chain (the
        branch-4 walk needs registered ancestors); when None a fresh, registry-less manager is built.
        """
        if pm is None:
            pm = ProjectManager(Mock(), cm, Mock())

        # Preview path, read-only and before any live mutation.
        preview_project_config = cm.read_config_file(project_dir / "griptape_nodes_config.json")
        preview_env_config = cm.read_env_config()
        preview_decision = pm.decide_workspace(project_file, preview_project_config, preview_env_config)
        preview_merged = cm.compute_project_provisioning_config(
            project_dir, preview_decision.workspace_dir, apply_override=preview_decision.apply_override
        )

        # Live path, mirroring _activate_project's workspace block.
        cm.clear_project_layers()
        cm.load_project_config(project_dir)
        live_decision = pm.decide_workspace(project_file, cm.project_config, cm.env_config)
        if live_decision.apply_override:
            cm.set_workspace_override(live_decision.workspace_dir)
        cm.load_workspace_config(cm.workspace_path)
        live_merged = cm.merged_config

        # The preview and live paths must agree on every key the preview consumes.
        assert get_dot_value(preview_merged, LIBRARIES_TO_REGISTER_KEY) == get_dot_value(
            live_merged, LIBRARIES_TO_REGISTER_KEY
        )
        assert get_dot_value(preview_merged, REQUIRES_ENGINE_KEY) == get_dot_value(live_merged, REQUIRES_ENGINE_KEY)
        assert preview_merged["workspace_directory"] == live_merged["workspace_directory"]

        # The workspace layer was actually consumed (not a both-wrong pass).
        assert get_dot_value(live_merged, LIBRARIES_TO_REGISTER_KEY) == expected_libraries
        assert get_dot_value(live_merged, REQUIRES_ENGINE_KEY) == expected_engine_version

    # Live-vs-offline parity for the unset-libraries fallback is covered by REAL-path tests, not a
    # by-construction replica: the live side by
    # TestConfigManager.test_resolved_libraries_root_fallback_ignores_active_workspace_override
    # (resolved_libraries_root -> global, not the project override), and the offline side by
    # test_library_manager.TestPreviewProjectProvisioning.test_probes_global_workspace_for_unset_libraries_fallback
    # (drives the real on_preview_project_provisioning_request and fails if it stops probing the global
    # workspace). Both now resolve against configured_global_workspace_path(), so they agree.

    def test_project_workspaces_override_branch(self, tmp_path: Path, isolate_user_config: Path) -> None:
        """project_workspaces maps the project to a separate workspace dir (apply_override=True)."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        project_file = project_dir / "project.yml"
        project_file.touch()
        mapped_workspace = tmp_path / "mapped"
        mapped_workspace.mkdir()

        self._write_config_file(
            project_dir / "griptape_nodes_config.json",
            {LIBRARIES_TO_REGISTER_KEY: ["project-lib"], REQUIRES_ENGINE_KEY: ">=1.0"},
        )
        self._write_config_file(
            mapped_workspace / "griptape_nodes_config.json",
            {LIBRARIES_TO_REGISTER_KEY: ["workspace-lib"], REQUIRES_ENGINE_KEY: ">=2.0"},
        )
        isolate_user_config.write_text(
            json.dumps({"project_workspaces": {str(project_file): str(mapped_workspace)}}), encoding="utf-8"
        )

        with patch.dict(os.environ, {}, clear=True):
            cm = ConfigManager()
            self._assert_preview_matches_live(
                cm,
                project_dir,
                project_file,
                expected_libraries=["workspace-lib"],
                expected_engine_version=">=2.0",
            )

    def test_env_workspace_branch(self, tmp_path: Path) -> None:
        """GTN_CONFIG_WORKSPACE_DIRECTORY points at a separate workspace dir (apply_override=False)."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        project_file = project_dir / "project.yml"
        project_file.touch()
        env_workspace = tmp_path / "env_workspace"
        env_workspace.mkdir()

        self._write_config_file(
            project_dir / "griptape_nodes_config.json",
            {LIBRARIES_TO_REGISTER_KEY: ["project-lib"], REQUIRES_ENGINE_KEY: ">=1.0"},
        )
        self._write_config_file(
            env_workspace / "griptape_nodes_config.json",
            {LIBRARIES_TO_REGISTER_KEY: ["env-workspace-lib"], REQUIRES_ENGINE_KEY: ">=3.0"},
        )

        with patch.dict(os.environ, {"GTN_CONFIG_WORKSPACE_DIRECTORY": str(env_workspace)}, clear=True):
            cm = ConfigManager()
            self._assert_preview_matches_live(
                cm,
                project_dir,
                project_file,
                expected_libraries=["env-workspace-lib"],
                expected_engine_version=">=3.0",
            )

    def test_project_adjacent_workspace_branch(self, tmp_path: Path) -> None:
        """The project-adjacent config sets workspace_directory to a separate dir (apply_override=False)."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        project_file = project_dir / "project.yml"
        project_file.touch()
        adjacent_workspace = tmp_path / "adjacent_workspace"
        adjacent_workspace.mkdir()

        self._write_config_file(
            project_dir / "griptape_nodes_config.json",
            {
                LIBRARIES_TO_REGISTER_KEY: ["project-lib"],
                REQUIRES_ENGINE_KEY: ">=1.0",
                "workspace_directory": str(adjacent_workspace),
            },
        )
        self._write_config_file(
            adjacent_workspace / "griptape_nodes_config.json",
            {LIBRARIES_TO_REGISTER_KEY: ["adjacent-workspace-lib"], REQUIRES_ENGINE_KEY: ">=4.0"},
        )

        with patch.dict(os.environ, {}, clear=True):
            cm = ConfigManager()
            self._assert_preview_matches_live(
                cm,
                project_dir,
                project_file,
                expected_libraries=["adjacent-workspace-lib"],
                expected_engine_version=">=4.0",
            )

    def test_parent_chain_inheritance_branch(self, tmp_path: Path) -> None:
        """A child with no workspace inherits its registered parent's resolved workspace (apply_override=True).

        The child declares parent_project_id pointing at a registered parent whose project-adjacent
        config sets workspace_directory; decide_workspace's parent-chain walk inherits that workspace,
        and both paths must resolve the workspace layer to it.
        """
        from griptape_nodes.common.project_templates import ProjectValidationInfo, ProjectValidationStatus
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.retained_mode.managers.project_manager import ProjectInfo

        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        parent_dir = tmp_path / "parent"
        parent_dir.mkdir()
        parent_file = parent_dir / "griptape-nodes-project.yml"
        parent_file.touch()
        project_dir = tmp_path / "child"
        project_dir.mkdir()
        project_file = project_dir / "griptape-nodes-project.yml"
        project_file.touch()

        self._write_config_file(
            project_dir / "griptape_nodes_config.json",
            {LIBRARIES_TO_REGISTER_KEY: ["project-lib"], REQUIRES_ENGINE_KEY: ">=1.0"},
        )
        # The parent's adjacent config points its workspace at workspace_root.
        self._write_config_file(
            parent_dir / "griptape_nodes_config.json",
            {"workspace_directory": str(workspace_root)},
        )
        self._write_config_file(
            workspace_root / "griptape_nodes_config.json",
            {LIBRARIES_TO_REGISTER_KEY: ["root-workspace-lib"], REQUIRES_ENGINE_KEY: ">=5.0"},
        )

        with patch.dict(os.environ, {}, clear=True):
            cm = ConfigManager()
            pm = ProjectManager(Mock(), cm, Mock())
            validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
            for project_id, file_path, parent_id in (
                ("parent", parent_file, None),
                ("child", project_file, "parent"),
            ):
                pm._successfully_loaded_project_templates[project_id] = ProjectInfo(
                    project_id=project_id,
                    project_file_path=file_path,
                    project_base_dir=file_path.parent,
                    template=DEFAULT_PROJECT_TEMPLATE.model_copy(update={"parent_project_id": parent_id}),
                    validation=validation,
                    parsed_situation_schemas={},
                    parsed_directory_schemas={},
                )
            self._assert_preview_matches_live(
                cm,
                project_dir,
                project_file,
                expected_libraries=["root-workspace-lib"],
                expected_engine_version=">=5.0",
                pm=pm,
            )

    def test_global_default_branch(self, tmp_path: Path, isolate_user_config: Path) -> None:
        """Chain exhausted: the global configured workspace_directory is used unconditionally (apply_override=True).

        The user config sets workspace_directory to a root the parentless project does NOT live under,
        so decide_workspace's global-default branch (no containment guard) fires and both paths must
        resolve the workspace layer to that root rather than the project's own dir.
        """
        workspace_root = tmp_path / "global_ws"
        workspace_root.mkdir()
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        project_file = project_dir / "project.yml"
        project_file.touch()

        self._write_config_file(
            project_dir / "griptape_nodes_config.json",
            {LIBRARIES_TO_REGISTER_KEY: ["project-lib"], REQUIRES_ENGINE_KEY: ">=1.0"},
        )
        self._write_config_file(
            workspace_root / "griptape_nodes_config.json",
            {LIBRARIES_TO_REGISTER_KEY: ["global-workspace-lib"], REQUIRES_ENGINE_KEY: ">=5.0"},
        )
        isolate_user_config.write_text(json.dumps({"workspace_directory": str(workspace_root)}), encoding="utf-8")

        with patch.dict(os.environ, {}, clear=True):
            cm = ConfigManager()
            self._assert_preview_matches_live(
                cm,
                project_dir,
                project_file,
                expected_libraries=["global-workspace-lib"],
                expected_engine_version=">=5.0",
            )

    def test_system_defaults_branch(self, isolate_user_config: Path) -> None:
        """Switching to system defaults merges defaults->user->env with no project/workspace file.

        _activate_project's system-defaults branch runs clear_project_layers() then load_configs(),
        so the preview's compute_system_defaults_provisioning_config must agree on the keys it
        consumes. A user-config library pin proves the user layer is actually read (a both-empty
        pass would not), which is exactly the pin that can force a destructive reconcile on the
        switch to Default Project.
        """
        user_config: dict = {}
        set_dot_value(user_config, LIBRARIES_TO_REGISTER_KEY, ["user-pin-lib"])
        set_dot_value(user_config, REQUIRES_ENGINE_KEY, ">=9.0")
        isolate_user_config.write_text(json.dumps(user_config), encoding="utf-8")

        with patch.dict(os.environ, {}, clear=True):
            cm = ConfigManager()

            # Preview path, read-only.
            preview_merged = cm.compute_system_defaults_provisioning_config()

            # Live path, mirroring _activate_project's system-defaults branch.
            cm.clear_project_layers()
            cm.load_configs()
            live_merged = cm.merged_config

        assert get_dot_value(preview_merged, LIBRARIES_TO_REGISTER_KEY) == get_dot_value(
            live_merged, LIBRARIES_TO_REGISTER_KEY
        )
        assert get_dot_value(preview_merged, REQUIRES_ENGINE_KEY) == get_dot_value(live_merged, REQUIRES_ENGINE_KEY)
        # The user layer was actually consumed (not a both-empty pass).
        assert get_dot_value(live_merged, LIBRARIES_TO_REGISTER_KEY) == ["user-pin-lib"]
        assert get_dot_value(live_merged, REQUIRES_ENGINE_KEY) == ">=9.0"


@pytest.mark.skipif(
    platform.system() == "Windows", reason="xdg_base_dirs cannot find XDG_CONFIG_HOME on Windows on GitHub Actions"
)
class TestConfigProvenance:
    """Provenance across value_source, shadowed_by, category_sources, config_layers, and handlers.

    Ports the four scenarios reproduced against a live engine: a shadowed write reporting
    `applied=False`, a GUI write-back of the shown value still not becoming the truth,
    provenance resolving to each of the five layers in turn, and a malformed layer surfacing
    `parse_error` instead of only a log line.
    """

    @staticmethod
    def _write_layer_config(directory: Path, contents: dict | str) -> Path:
        """Write a griptape_nodes_config.json into `directory` and return its path.

        A str `contents` is written verbatim, for the malformed-JSON case; a dict is
        serialized. Returns the path so a test can assert provenance points at this file
        without rebuilding the filename.
        """
        path = directory / "griptape_nodes_config.json"
        if isinstance(contents, str):
            path.write_text(contents, encoding="utf-8")
        else:
            path.write_text(json.dumps(contents), encoding="utf-8")
        return path

    @staticmethod
    def _managed_dirs(tmp_path: Path) -> tuple[Path, Path]:
        """Create and return separate project and workspace directories under `tmp_path`."""
        project_dir = tmp_path / "project"
        workspace_dir = tmp_path / "workspace"
        project_dir.mkdir()
        workspace_dir.mkdir()
        return project_dir, workspace_dir

    # -- value_source: one layer per test, each winning over everything below it --

    def test_value_source_default_when_unset_anywhere(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            source = ConfigManager().value_source("log_level")

        assert source.layer == "default"
        assert source.path is None
        assert source.env_var is None

    def test_value_source_user_layer(self, isolate_user_config: Path) -> None:
        isolate_user_config.write_text(json.dumps({"log_level": "ERROR"}), encoding="utf-8")

        with patch.dict(os.environ, {}, clear=True):
            source = ConfigManager().value_source("log_level")

        assert source.layer == "user"
        assert source.path == str(isolate_user_config)

    def test_value_source_project_layer_wins_over_user(self, tmp_path: Path, isolate_user_config: Path) -> None:
        isolate_user_config.write_text(json.dumps({"log_level": "WARNING"}), encoding="utf-8")
        project_config = self._write_layer_config(tmp_path, {"log_level": "ERROR"})

        with patch.dict(os.environ, {}, clear=True):
            manager = ConfigManager()
            manager.load_project_config(tmp_path)
            source = manager.value_source("log_level")

        assert source.layer == "project"
        assert source.path == str(project_config)

    def test_value_source_workspace_layer_wins_over_project(self, tmp_path: Path) -> None:
        project_dir, workspace_dir = self._managed_dirs(tmp_path)
        self._write_layer_config(project_dir, {"log_level": "ERROR"})
        workspace_config = self._write_layer_config(workspace_dir, {"log_level": "DEBUG"})

        with patch.dict(os.environ, {}, clear=True):
            manager = ConfigManager()
            manager.load_project_config(project_dir)
            manager.load_workspace_config(workspace_dir)
            source = manager.value_source("log_level")

        assert source.layer == "workspace"
        assert source.path == str(workspace_config)

    def test_value_source_env_layer_wins_over_everything(self, tmp_path: Path, isolate_user_config: Path) -> None:
        isolate_user_config.write_text(json.dumps({"log_level": "WARNING"}), encoding="utf-8")
        self._write_layer_config(tmp_path, {"log_level": "ERROR"})

        with patch.dict(os.environ, {"GTN_CONFIG_LOG_LEVEL": "DEBUG"}, clear=True):
            manager = ConfigManager()
            manager.load_project_config(tmp_path)
            source = manager.value_source("log_level")

        assert source.layer == "env"
        assert source.env_var == "GTN_CONFIG_LOG_LEVEL"
        assert source.path is None

    # -- shadowed_by: only default/user are "not shadowed" --

    def test_shadowed_by_none_for_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert ConfigManager().shadowed_by("log_level") is None

    def test_shadowed_by_none_for_user(self, isolate_user_config: Path) -> None:
        isolate_user_config.write_text(json.dumps({"log_level": "ERROR"}), encoding="utf-8")

        with patch.dict(os.environ, {}, clear=True):
            assert ConfigManager().shadowed_by("log_level") is None

    def test_shadowed_by_returns_project_source(self, tmp_path: Path) -> None:
        self._write_layer_config(tmp_path, {"log_level": "ERROR"})

        with patch.dict(os.environ, {}, clear=True):
            manager = ConfigManager()
            manager.load_project_config(tmp_path)
            shadowed = manager.shadowed_by("log_level")

        assert shadowed is not None
        assert shadowed.layer == "project"

    # -- category_sources: root-relative keys, dicts recursed, lists are one leaf --

    def test_category_sources_full_dot_path_relative_to_root(self) -> None:
        """Fetching a sub-category still keys `sources` by the full path from the config root.

        This is the contract's own example: not relative to the requested category, so a
        caller can look a key up the same way no matter which category it came through.
        """
        with patch.dict(os.environ, {}, clear=True):
            sources = ConfigManager().category_sources("app_events.on_app_initialization_complete")

        assert LIBRARIES_TO_REGISTER_KEY in sources
        assert sources[LIBRARIES_TO_REGISTER_KEY].layer == "default"

    def test_category_sources_none_category_is_whole_config_root_relative(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            sources = ConfigManager().category_sources(None)

        assert "libraries_directory" in sources
        assert "workspace_directory" in sources

    def test_category_sources_list_value_is_single_leaf_not_descended(self, tmp_path: Path) -> None:
        """A list is one leaf entry: the source of the WHOLE list, never split per item."""
        self._write_layer_config(
            tmp_path, {"app_events": {"on_app_initialization_complete": {"libraries_to_register": ["a", "b"]}}}
        )

        with patch.dict(os.environ, {}, clear=True):
            manager = ConfigManager()
            manager.load_project_config(tmp_path)
            sources = manager.category_sources(None)

        assert LIBRARIES_TO_REGISTER_KEY in sources
        assert sources[LIBRARIES_TO_REGISTER_KEY].layer == "project"
        assert not any(k.startswith(f"{LIBRARIES_TO_REGISTER_KEY}.") for k in sources)

    # -- config_layers: fixed five-layer stack, and a malformed layer surfaces parse_error --

    def test_config_layers_returns_five_entries_in_fixed_order(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            layers = ConfigManager().config_layers()

        assert [layer.layer for layer in layers] == ["default", "user", "project", "workspace", "env"]

    def test_config_layers_project_absent_when_no_project_active(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            layers = {layer.layer: layer for layer in ConfigManager().config_layers()}

        assert layers["project"].path is None
        assert layers["project"].present is False
        assert layers["project"].parse_error is None

    def test_config_layers_surfaces_parse_error_for_malformed_project_config(self, tmp_path: Path) -> None:
        """A file that exists but fails to parse must be visible as `parse_error`, not just a log line.

        This is a project-adjacent file holding project-template content under a config
        filename, which silently fails to parse on every load.
        """
        self._write_layer_config(tmp_path, '"project_template_schema_version": "1.0.0"\n"name": "not valid json"\n')

        with patch.dict(os.environ, {}, clear=True):
            manager = ConfigManager()
            manager.load_project_config(tmp_path)
            layers = {layer.layer: layer for layer in manager.config_layers()}

        assert layers["project"].present is True
        assert layers["project"].parse_error is not None
        assert layers["project"].values == {}

    def test_config_layers_clears_stale_parse_error_when_project_switches(self, tmp_path: Path) -> None:
        """A layer's parse error must not outlive the project it came from.

        Switching to a project whose config parses (or to none at all) has to clear the
        previous project's error, or `gtn self info` keeps blaming a file that is no longer
        part of the merge.
        """
        broken_dir, good_dir = self._managed_dirs(tmp_path)
        self._write_layer_config(broken_dir, "not valid json")
        self._write_layer_config(good_dir, {"log_level": "ERROR"})

        with patch.dict(os.environ, {}, clear=True):
            manager = ConfigManager()
            manager.load_project_config(broken_dir)
            assert {layer.layer: layer for layer in manager.config_layers()}["project"].parse_error is not None

            manager.load_project_config(good_dir)
            layers = {layer.layer: layer for layer in manager.config_layers()}

        assert layers["project"].parse_error is None
        assert layers["project"].values == {"log_level": "ERROR"}

    def test_config_layers_env_present_reflects_gtn_config_vars(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            layers = {layer.layer: layer for layer in ConfigManager().config_layers()}
        assert layers["env"].present is False

        with patch.dict(os.environ, {"GTN_CONFIG_LOG_LEVEL": "DEBUG"}, clear=True):
            layers = {layer.layer: layer for layer in ConfigManager().config_layers()}
        assert layers["env"].present is True
        assert layers["env"].values == {"log_level": "DEBUG"}

    def test_config_layers_workspace_not_present_when_it_is_the_project_file(self, tmp_path: Path) -> None:
        """A workspace dir that IS the project dir names one file, loaded once as `project`.

        load_configs skips the duplicate, so the workspace layer must not claim to be
        contributing. It keeps its path (so a caller can see which file it would have
        been) but reports present=False with empty values.
        """
        shared_config = self._write_layer_config(tmp_path, {"log_level": "ERROR"})

        with patch.dict(os.environ, {}, clear=True):
            manager = ConfigManager()
            manager.load_project_config(tmp_path)
            manager.load_workspace_config(tmp_path)
            layers = {layer.layer: layer for layer in manager.config_layers()}

        assert layers["project"].present is True
        assert layers["project"].values == {"log_level": "ERROR"}
        assert layers["workspace"].present is False
        assert layers["workspace"].values == {}
        assert layers["workspace"].path == str(shared_config)

    # -- handler-level: the wire shape a settings UI actually receives --

    def test_set_config_value_request_reports_shadowed_write_as_not_applied(
        self, tmp_path: Path, isolate_user_config: Path
    ) -> None:
        """A shadowed write must not report unqualified success.

        The write still reaches disk (see the assertion at the end) -- only the REPORTING
        becomes honest; this change does not refuse the write or change where it lands.
        """
        project_config = self._write_layer_config(tmp_path, {"libraries_directory": "/from/project"})

        with patch.dict(os.environ, {}, clear=True):
            manager = ConfigManager()
            manager.load_project_config(tmp_path)
            result = manager.on_handle_set_config_value_request(
                SetConfigValueRequest(category_and_key="libraries_directory", value="/typed/by/user")
            )

        assert isinstance(result, SetConfigValueResultSuccess)
        assert result.applied is False
        assert result.effective_value == "/from/project"
        assert result.shadowed_by is not None
        assert result.shadowed_by.layer == "project"
        assert result.shadowed_by.path == str(project_config)
        # The user is told WHY, not just that the write "succeeded".
        assert "no visible effect" in str(result.result_details)

        # The write is not rejected -- it lands in the user layer, silently ignored.
        on_disk = json.loads(isolate_user_config.read_text())
        assert on_disk["libraries_directory"] == "/typed/by/user"

    def test_set_config_value_request_write_back_of_shadowed_value_stays_unapplied(self, tmp_path: Path) -> None:
        """Writing back exactly the value the merged config is showing does not make it 'yours'.

        Shadowing is about which LAYER wins, not whether the values agree. This is the
        settings-panel write-back from the bug thread, where re-saving the displayed value
        looked like a no-op but the key remained just as unowned as before.
        """
        self._write_layer_config(tmp_path, {"libraries_directory": "/from/project"})

        with patch.dict(os.environ, {}, clear=True):
            manager = ConfigManager()
            manager.load_project_config(tmp_path)

            shown_value = manager.get_config_value("libraries_directory")
            assert shown_value == "/from/project"

            result = manager.on_handle_set_config_value_request(
                SetConfigValueRequest(category_and_key="libraries_directory", value=shown_value)
            )

        assert isinstance(result, SetConfigValueResultSuccess)
        assert result.applied is False
        assert result.shadowed_by is not None
        assert result.shadowed_by.layer == "project"

    def test_set_config_value_request_applied_true_when_not_shadowed(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            result = ConfigManager().on_handle_set_config_value_request(
                SetConfigValueRequest(category_and_key="log_level", value="DEBUG")
            )

        assert isinstance(result, SetConfigValueResultSuccess)
        assert result.applied is True
        assert result.effective_value == "DEBUG"
        assert result.shadowed_by is None
        assert "no visible effect" not in str(result.result_details)

    def test_get_config_value_request_reports_source_and_editable(
        self, tmp_path: Path, isolate_user_config: Path
    ) -> None:
        self._write_layer_config(tmp_path, {"log_level": "ERROR"})

        with patch.dict(os.environ, {}, clear=True):
            manager = ConfigManager()
            manager.load_project_config(tmp_path)
            shadowed_result = manager.on_handle_get_config_value_request(
                GetConfigValueRequest(category_and_key="log_level")
            )

            isolate_user_config.write_text(json.dumps({"some_editable_key": "value"}), encoding="utf-8")
            manager.load_configs()
            editable_result = manager.on_handle_get_config_value_request(
                GetConfigValueRequest(category_and_key="some_editable_key")
            )

        assert isinstance(shadowed_result, GetConfigValueResultSuccess)
        assert isinstance(editable_result, GetConfigValueResultSuccess)
        assert shadowed_result.source.layer == "project"
        assert shadowed_result.editable is False

        assert editable_result.source.layer == "user"
        assert editable_result.editable is True

    def test_get_config_category_request_sources_keyed_by_full_root_relative_path(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            result = ConfigManager().on_handle_get_config_category_request(
                GetConfigCategoryRequest(category="app_events.on_app_initialization_complete")
            )

        assert isinstance(result, GetConfigCategoryResultSuccess)
        assert LIBRARIES_TO_REGISTER_KEY in result.sources
        assert result.sources[LIBRARIES_TO_REGISTER_KEY].layer == "default"

    def test_get_config_layers_request_handler_returns_five_layers(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            result = ConfigManager().on_handle_get_config_layers_request(GetConfigLayersRequest())

        assert isinstance(result, GetConfigLayersResultSuccess)
        assert [layer.layer for layer in result.layers] == ["default", "user", "project", "workspace", "env"]

    def test_set_config_category_request_non_empty_category_reports_shadowed(self, tmp_path: Path) -> None:
        self._write_layer_config(tmp_path, {"nuke": {"executable": "/from/project"}})

        with patch.dict(os.environ, {}, clear=True):
            manager = ConfigManager()
            manager.load_project_config(tmp_path)
            result = manager.on_handle_set_config_category_request(
                SetConfigCategoryRequest(category="nuke", contents={"executable": "/typed/by/user"})
            )

        assert isinstance(result, SetConfigCategoryResultSuccess)
        assert result.applied is False
        assert result.shadowed_by is not None
        assert result.shadowed_by.layer == "project"

    def test_set_config_category_request_full_replacement_leaves_new_fields_at_defaults(self) -> None:
        """A full-config replacement (category=None) has no single key to check for shadowing.

        applied/effective_value/shadowed_by stay at their neutral defaults rather than
        reporting something misleading for a case the contract doesn't cover.
        """
        with patch.dict(os.environ, {}, clear=True):
            result = ConfigManager().on_handle_set_config_category_request(
                SetConfigCategoryRequest(category=None, contents={"log_level": "DEBUG"})
            )

        assert isinstance(result, SetConfigCategoryResultSuccess)
        assert result.applied is True
        assert result.effective_value is None
        assert result.shadowed_by is None
