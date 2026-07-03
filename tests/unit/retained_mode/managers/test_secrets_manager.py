import logging
import os
import platform
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from griptape_nodes.retained_mode.managers.config_manager import ConfigManager
from griptape_nodes.retained_mode.managers.secrets_manager import SecretsManager


@pytest.mark.skipif(
    platform.system() == "Windows", reason="xdg_base_dirs cannot find XDG_CONFIG_HOME on Windows on GitHub Actions"
)
class TestSecretsManager:
    """Test SecretsManager functionality including search order precedence."""

    def test_secret_search_order_env_var_highest_priority(self) -> None:
        """Test that environment variables have highest priority over .env files."""
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_path = Path(temp_dir)

            # Create workspace .env file
            workspace_env = workspace_path / ".env"
            workspace_env.write_text("TEST_SECRET=workspace_value\n")

            # Create global .env file
            global_env = workspace_path / "global.env"  # Use temp dir for test isolation
            global_env.write_text("TEST_SECRET=global_value\n")

            # Set environment variable (should have highest priority)
            with patch.dict(os.environ, {"TEST_SECRET": "env_value"}, clear=False):
                config_manager = ConfigManager()
                config_manager.workspace_path = workspace_path

                # Patch the global env path for test isolation
                with patch("griptape_nodes.retained_mode.managers.secrets_manager.ENV_VAR_PATH", global_env):
                    secrets_manager = SecretsManager(config_manager)

                    # Environment variable should win
                    assert secrets_manager.get_secret("TEST_SECRET") == "env_value"

    def test_secret_search_order_workspace_over_global(self) -> None:
        """Test that workspace .env takes priority over global .env when no env var is set."""
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_path = Path(temp_dir)

            # Create workspace .env file
            workspace_env = workspace_path / ".env"
            workspace_env.write_text("TEST_SECRET=workspace_value\n")

            # Create global .env file
            global_env = workspace_path / "global.env"  # Use temp dir for test isolation
            global_env.write_text("TEST_SECRET=global_value\n")

            # Ensure no environment variable is set
            with patch.dict(os.environ, {}, clear=True):
                config_manager = ConfigManager()
                config_manager.workspace_path = workspace_path

                # Patch the global env path for test isolation
                with patch("griptape_nodes.retained_mode.managers.secrets_manager.ENV_VAR_PATH", global_env):
                    secrets_manager = SecretsManager(config_manager)

                    # Workspace should win over global
                    assert secrets_manager.get_secret("TEST_SECRET") == "workspace_value"

    def test_secret_search_order_global_as_fallback(self) -> None:
        """Test that global .env is used when neither env var nor workspace .env exist."""
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_path = Path(temp_dir)

            # Create global .env file (no workspace .env)
            global_env = workspace_path / "global.env"  # Use temp dir for test isolation
            global_env.write_text("TEST_SECRET=global_value\n")

            # Ensure no environment variable is set
            with patch.dict(os.environ, {}, clear=True):
                config_manager = ConfigManager()
                config_manager.workspace_path = workspace_path

                # Patch the global env path for test isolation
                with patch("griptape_nodes.retained_mode.managers.secrets_manager.ENV_VAR_PATH", global_env):
                    secrets_manager = SecretsManager(config_manager)

                    # Global should be used as fallback
                    assert secrets_manager.get_secret("TEST_SECRET") == "global_value"

    def test_secret_not_found_returns_none(self) -> None:
        """Test that missing secrets return None when should_error_on_not_found=False."""
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_path = Path(temp_dir)

            # Ensure no environment variable is set and no .env files exist
            with patch.dict(os.environ, {}, clear=True):
                config_manager = ConfigManager()
                config_manager.workspace_path = workspace_path

                secrets_manager = SecretsManager(config_manager)

                # Should return None for missing secret
                assert secrets_manager.get_secret("NONEXISTENT_SECRET", should_error_on_not_found=False) is None

    def test_secret_name_compliance(self) -> None:
        """Test that secret names are properly transformed to uppercase with underscores."""
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_path = Path(temp_dir)

            # Set environment variable with compliant name
            with patch.dict(os.environ, {"MY_TEST_SECRET": "test_value"}, clear=False):
                config_manager = ConfigManager()
                config_manager.workspace_path = workspace_path

                secrets_manager = SecretsManager(config_manager)

                # Different input formats should all resolve to the same compliant name
                assert secrets_manager.get_secret("my test secret") == "test_value"
                assert secrets_manager.get_secret("my-test-secret") == "test_value"
                assert secrets_manager.get_secret("MY_TEST_SECRET") == "test_value"

    def test_search_order_partial_overlap(self) -> None:
        """Test search order when secrets exist in some but not all sources."""
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_path = Path(temp_dir)

            # Create workspace .env with one secret
            workspace_env = workspace_path / ".env"
            workspace_env.write_text("SECRET_A=workspace_a\nSECRET_B=workspace_b\n")

            # Create global .env with different secrets
            global_env = workspace_path / "global.env"
            global_env.write_text("SECRET_B=global_b\nSECRET_C=global_c\n")

            # Set environment variable for one secret
            with patch.dict(os.environ, {"SECRET_A": "env_a"}, clear=False):
                config_manager = ConfigManager()
                config_manager.workspace_path = workspace_path

                with patch("griptape_nodes.retained_mode.managers.secrets_manager.ENV_VAR_PATH", global_env):
                    secrets_manager = SecretsManager(config_manager)

                    # SECRET_A: env var should win
                    assert secrets_manager.get_secret("SECRET_A") == "env_a"

                    # SECRET_B: workspace should win over global
                    assert secrets_manager.get_secret("SECRET_B") == "workspace_b"

                    # SECRET_C: only in global, should use global
                    assert secrets_manager.get_secret("SECRET_C") == "global_c"

    def test_secrets_to_register_with_list_format(self) -> None:
        """Test that list format is normalized to dict with empty defaults."""
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_path = Path(temp_dir)
            config_manager = ConfigManager()
            config_manager.workspace_path = workspace_path

            with patch.object(config_manager, "get_config_value", return_value=["KEY1", "KEY2"]):
                secrets_manager = SecretsManager(config_manager)
                result = secrets_manager.secrets_to_register

                assert result == {"KEY1": "", "KEY2": ""}

    def test_secrets_to_register_with_dict_format(self) -> None:
        """Test that dict format preserves default values."""
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_path = Path(temp_dir)
            config_manager = ConfigManager()
            config_manager.workspace_path = workspace_path

            with patch.object(
                config_manager,
                "get_config_value",
                return_value={"KEY1": "default1", "KEY2": "", "KEY3": "default3"},
            ):
                secrets_manager = SecretsManager(config_manager)
                result = secrets_manager.secrets_to_register

                assert result == {"KEY1": "default1", "KEY2": "", "KEY3": "default3"}

    def test_refresh_from_env_file_overrides_stale_environ(self) -> None:
        """refresh_from_env_file re-reads the global .env and overrides os.environ for managed keys.

        This is the behavior that makes same-machine secret propagation work: a
        worker boots with the file contents installed into os.environ via
        ``load_dotenv``, then the orchestrator updates the shared file, and the
        worker must see the new value on the next get_secret() call -- which
        reads os.environ first.

        The key here is *managed* (the manager itself installed it from the
        file at boot, since no OS-set var pre-existed). Refresh therefore
        owns the override.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_path = Path(temp_dir)
            global_env = workspace_path / "global.env"
            global_env.write_text("REFRESH_KEY=old_value\n")

            # Make sure no OS-set REFRESH_KEY pre-exists; the manager must
            # install it from the file at boot for it to count as managed.
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("REFRESH_KEY", None)
                config_manager = ConfigManager()
                config_manager.workspace_path = workspace_path

                with patch("griptape_nodes.retained_mode.managers.secrets_manager.ENV_VAR_PATH", global_env):
                    secrets_manager = SecretsManager(config_manager)
                    # Boot installed the file value into os.environ.
                    assert os.environ["REFRESH_KEY"] == "old_value"

                    # Simulate the orchestrator rewriting the shared file.
                    global_env.write_text("REFRESH_KEY=new_value\n")
                    # Without refresh, os.environ still holds the boot value.
                    assert os.environ["REFRESH_KEY"] == "old_value"

                    secrets_manager.refresh_from_env_file()

                    assert os.environ["REFRESH_KEY"] == "new_value"
                    assert secrets_manager.get_secret("REFRESH_KEY") == "new_value"

    def test_set_secret_broadcasts_secret_changed(self) -> None:
        """Setting a secret emits a SecretChanged app event carrying the normalized key.

        Worker fan-out lives elsewhere (WorkerManager listens for SecretChanged
        and schedules RefreshSecretsRequest); this manager only owns the
        domain event.
        """
        from griptape_nodes.retained_mode.events.app_events import SecretChanged
        from griptape_nodes.retained_mode.events.secrets_events import SetSecretValueRequest

        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_path = Path(temp_dir)
            global_env = workspace_path / "global.env"
            config_manager = ConfigManager()
            config_manager.workspace_path = workspace_path
            event_manager = MagicMock()

            with patch("griptape_nodes.retained_mode.managers.secrets_manager.ENV_VAR_PATH", global_env):
                secrets_manager = SecretsManager(config_manager, event_manager=event_manager)
                secrets_manager.on_handle_set_secret_request(
                    SetSecretValueRequest(key="my api key", value="xyz"),
                )

            broadcast_calls = [
                call
                for call in event_manager.broadcast_app_event.call_args_list
                if isinstance(call.args[0], SecretChanged)
            ]
            assert len(broadcast_calls) == 1
            assert broadcast_calls[0].args[0].key == "MY_API_KEY"

    def test_refresh_preserves_os_set_env_var(self) -> None:
        """A real OS env var that collides with a .env entry must survive refresh.

        Boot uses ``override=False`` so a container-injected value (e.g.
        ``OPENAI_API_KEY`` set on the container, not by the manager) wins over
        the file at startup. ``refresh_from_env_file`` must preserve that
        precedence: an unrelated secret-change broadcast cannot silently
        replace the operator-set value with the file's value.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_path = Path(temp_dir)
            global_env = workspace_path / "global.env"
            global_env.write_text("CONTAINER_INJECTED_KEY=file_value\n")

            with patch.dict(os.environ, {"CONTAINER_INJECTED_KEY": "operator_value"}, clear=False):
                config_manager = ConfigManager()
                config_manager.workspace_path = workspace_path

                with patch("griptape_nodes.retained_mode.managers.secrets_manager.ENV_VAR_PATH", global_env):
                    secrets_manager = SecretsManager(config_manager)
                    # Boot did not overwrite the OS-set value, and the
                    # bookkeeping invariant says the manager does not own it.
                    assert os.environ["CONTAINER_INJECTED_KEY"] == "operator_value"
                    assert "CONTAINER_INJECTED_KEY" not in secrets_manager._managed_env_keys

                    # Simulate the orchestrator rewriting the file. (The
                    # rewrite is unrelated to CONTAINER_INJECTED_KEY in
                    # production -- it could be any other secret -- but the
                    # refresh runs the same code path against this key.)
                    global_env.write_text("CONTAINER_INJECTED_KEY=changed_in_file\n")

                    secrets_manager.refresh_from_env_file()

                    # The operator-set value still wins, and the manager
                    # still does not claim ownership of it.
                    assert os.environ["CONTAINER_INJECTED_KEY"] == "operator_value"
                    assert secrets_manager.get_secret("CONTAINER_INJECTED_KEY") == "operator_value"
                    assert "CONTAINER_INJECTED_KEY" not in secrets_manager._managed_env_keys

    def test_refresh_does_not_pop_os_set_env_var_when_file_changes(self) -> None:
        """A previously-tracked key removed from the file must not pop a real OS env var.

        Pre-fix, ``_loaded_env_keys`` tracked every key the manager had ever
        seen in a file. If a key was both OS-set *and* in a .env file, removing
        it from the file caused refresh to pop ``os.environ[key]``, deleting
        operator-set state the manager never owned.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_path = Path(temp_dir)
            global_env = workspace_path / "global.env"
            global_env.write_text("DUAL_HOMED_KEY=file_value\n")

            with patch.dict(os.environ, {"DUAL_HOMED_KEY": "operator_value"}, clear=False):
                config_manager = ConfigManager()
                config_manager.workspace_path = workspace_path

                with patch("griptape_nodes.retained_mode.managers.secrets_manager.ENV_VAR_PATH", global_env):
                    secrets_manager = SecretsManager(config_manager)
                    # File no longer has the key (removed externally between
                    # boot and refresh).
                    global_env.write_text("UNRELATED_KEY=anything\n")

                    secrets_manager.refresh_from_env_file()

                    # Operator-set value survives, and the manager never
                    # claimed ownership of it.
                    assert os.environ.get("DUAL_HOMED_KEY") == "operator_value"
                    assert "DUAL_HOMED_KEY" not in secrets_manager._managed_env_keys

    def test_refresh_with_no_files_present_does_not_wipe_managed_keys(self) -> None:
        """If both .env files vanish at refresh time, do not pop anything.

        Pre-fix, an empty ``present_anywhere`` set caused every previously
        tracked key to be popped in one pass -- a transient missing file
        could wipe the worker's entire secret cache.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_path = Path(temp_dir)
            global_env = workspace_path / "global.env"
            global_env.write_text("MANAGED_KEY=installed_value\n")

            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("MANAGED_KEY", None)
                config_manager = ConfigManager()
                config_manager.workspace_path = workspace_path

                with patch("griptape_nodes.retained_mode.managers.secrets_manager.ENV_VAR_PATH", global_env):
                    secrets_manager = SecretsManager(config_manager)
                    # Boot installed the value; key is managed.
                    assert os.environ["MANAGED_KEY"] == "installed_value"
                    assert "MANAGED_KEY" in secrets_manager._managed_env_keys

                    # Both files vanish (transient state during a swap, etc.).
                    global_env.unlink()

                    secrets_manager.refresh_from_env_file()

                    # The managed value survives -- a missing file is not
                    # license to wipe state -- and ownership is preserved.
                    assert os.environ.get("MANAGED_KEY") == "installed_value"
                    assert "MANAGED_KEY" in secrets_manager._managed_env_keys

    def test_refresh_pops_managed_key_when_file_actually_removes_it(self) -> None:
        """The pop pass *does* run for managed keys when files exist but the key vanishes.

        This is the productive case the original code was reaching for:
        if the manager installed a key from a file at boot, and that key
        is later removed from the file, refresh should clear it from
        ``os.environ`` so the worker's view tracks the file. The fix
        must not regress this.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_path = Path(temp_dir)
            global_env = workspace_path / "global.env"
            global_env.write_text("DELETABLE_KEY=installed_value\n")

            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("DELETABLE_KEY", None)
                config_manager = ConfigManager()
                config_manager.workspace_path = workspace_path

                with patch("griptape_nodes.retained_mode.managers.secrets_manager.ENV_VAR_PATH", global_env):
                    secrets_manager = SecretsManager(config_manager)
                    assert os.environ["DELETABLE_KEY"] == "installed_value"
                    assert "DELETABLE_KEY" in secrets_manager._managed_env_keys

                    # File still exists, but the key is gone.
                    global_env.write_text("OTHER_KEY=other_value\n")

                    secrets_manager.refresh_from_env_file()

                    # Managed key dropped from os.environ and from the
                    # ownership set; OTHER_KEY installed and now owned.
                    assert "DELETABLE_KEY" not in os.environ
                    assert "DELETABLE_KEY" not in secrets_manager._managed_env_keys
                    assert os.environ["OTHER_KEY"] == "other_value"
                    assert "OTHER_KEY" in secrets_manager._managed_env_keys

    def test_delete_secret_does_not_pop_os_set_env_var(self) -> None:
        """``DeleteSecretValueRequest`` must not pop a colliding OS-set env var.

        Same Finding-2 shape on the explicit delete path: pre-fix, the
        handler called ``os.environ.pop(secret_name, None)`` unconditionally,
        so deleting a secret from the file would also wipe a colliding
        operator-set env var. The new code only pops keys the manager owns.
        """
        from griptape_nodes.retained_mode.events.secrets_events import DeleteSecretValueRequest

        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_path = Path(temp_dir)
            global_env = workspace_path / "global.env"
            global_env.write_text("DELETE_DUAL_KEY=file_value\n")

            with patch.dict(os.environ, {"DELETE_DUAL_KEY": "operator_value"}, clear=False):
                config_manager = ConfigManager()
                config_manager.workspace_path = workspace_path

                with patch("griptape_nodes.retained_mode.managers.secrets_manager.ENV_VAR_PATH", global_env):
                    secrets_manager = SecretsManager(config_manager)
                    # Operator value survived boot, manager does not own it.
                    assert os.environ["DELETE_DUAL_KEY"] == "operator_value"
                    assert "DELETE_DUAL_KEY" not in secrets_manager._managed_env_keys

                    secrets_manager.on_handle_delete_secret_value_request(
                        DeleteSecretValueRequest(key="DELETE_DUAL_KEY"),
                    )

                    # File entry is gone (the handler unset it), but the
                    # operator's OS env var survives. Ownership is unchanged.
                    assert os.environ.get("DELETE_DUAL_KEY") == "operator_value"
                    assert "DELETE_DUAL_KEY" not in secrets_manager._managed_env_keys

    def test_set_secret_overrides_os_set_env_var(self) -> None:
        """``set_secret`` is an explicit user write -- override is intended.

        Unlike refresh and delete, ``set_secret`` is the user stating
        "this value, from now on." It must overwrite a colliding OS-set
        env var (the inverse-policy case is delegated to operators
        managing their environment) and claim ownership of the key for
        future refresh-time decisions.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_path = Path(temp_dir)
            global_env = workspace_path / "global.env"

            with patch.dict(os.environ, {"USER_SET_KEY": "stale_operator_value"}, clear=False):
                config_manager = ConfigManager()
                config_manager.workspace_path = workspace_path

                with patch("griptape_nodes.retained_mode.managers.secrets_manager.ENV_VAR_PATH", global_env):
                    secrets_manager = SecretsManager(config_manager)
                    assert "USER_SET_KEY" not in secrets_manager._managed_env_keys
                    secrets_manager.set_secret("USER_SET_KEY", "explicit_new_value")

                    assert os.environ["USER_SET_KEY"] == "explicit_new_value"
                    # set_secret claims ownership.
                    assert "USER_SET_KEY" in secrets_manager._managed_env_keys
                    # Subsequent refresh keeps the manager-owned value.
                    secrets_manager.refresh_from_env_file()
                    assert os.environ["USER_SET_KEY"] == "explicit_new_value"
                    assert "USER_SET_KEY" in secrets_manager._managed_env_keys

    def test_set_secret_warns_when_overriding_os_set_env_var(self, caplog: pytest.LogCaptureFixture) -> None:
        """Operator-set values are silently clobbered by ``set_secret``; warn loudly.

        ``set_secret`` honors the user's stated intent (override and claim
        ownership), but the asymmetry with ``refresh`` and the delete
        handler -- both of which preserve OS-set values -- is easy to miss
        without a warning. A noisy log line is the price of admission so
        the operator notices and can decide whether to clear the OS-set
        value or revert the file write.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_path = Path(temp_dir)
            global_env = workspace_path / "global.env"

            with patch.dict(os.environ, {"WARN_KEY": "stale_operator_value"}, clear=False):
                config_manager = ConfigManager()
                config_manager.workspace_path = workspace_path

                with patch("griptape_nodes.retained_mode.managers.secrets_manager.ENV_VAR_PATH", global_env):
                    secrets_manager = SecretsManager(config_manager)
                    caplog.set_level(logging.WARNING, logger="griptape_nodes")
                    secrets_manager.set_secret("WARN_KEY", "explicit_new_value")

        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("WARN_KEY" in r.message and "OS-set" in r.message for r in warning_records), (
            "Expected a WARNING-level log mentioning the overridden OS-set key."
        )

    def test_set_secret_does_not_warn_for_brand_new_key(self, caplog: pytest.LogCaptureFixture) -> None:
        """No OS-set collision -> no warning.

        Counterpart to ``test_set_secret_warns_when_overriding_os_set_env_var``:
        the warning is gated on a real collision so a normal ``set_secret``
        on a fresh key stays quiet.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_path = Path(temp_dir)
            global_env = workspace_path / "global.env"

            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("FRESH_KEY", None)
                config_manager = ConfigManager()
                config_manager.workspace_path = workspace_path

                with patch("griptape_nodes.retained_mode.managers.secrets_manager.ENV_VAR_PATH", global_env):
                    secrets_manager = SecretsManager(config_manager)
                    caplog.set_level(logging.WARNING, logger="griptape_nodes")
                    secrets_manager.set_secret("FRESH_KEY", "fresh_value")

        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert not any("FRESH_KEY" in r.message for r in warning_records), (
            "set_secret on a non-OS-set key should not warn."
        )

    def test_refresh_merges_workspace_over_global(self) -> None:
        """Workspace .env wins over global .env on refresh, mirroring get_secret.

        The two-file merge code path was previously untested under refresh.
        This pins down that workspace beats global for keys present in
        both, that workspace-only keys install correctly, and that
        global-only keys install correctly.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_path = Path(temp_dir)
            workspace_env = workspace_path / ".env"
            global_env = workspace_path / "global.env"

            with patch.dict(os.environ, {}, clear=False):
                for k in ("DUAL_KEY", "WS_ONLY_KEY", "GLOBAL_ONLY_KEY"):
                    os.environ.pop(k, None)
                config_manager = ConfigManager()
                config_manager.workspace_path = workspace_path

                with patch("griptape_nodes.retained_mode.managers.secrets_manager.ENV_VAR_PATH", global_env):
                    workspace_env.write_text("DUAL_KEY=workspace_dual\nWS_ONLY_KEY=workspace_only\n")
                    global_env.write_text("DUAL_KEY=global_dual\nGLOBAL_ONLY_KEY=global_only\n")

                    secrets_manager = SecretsManager(config_manager)
                    # Boot already merged with workspace winning.
                    assert os.environ["DUAL_KEY"] == "workspace_dual"
                    assert os.environ["WS_ONLY_KEY"] == "workspace_only"
                    assert os.environ["GLOBAL_ONLY_KEY"] == "global_only"

                    # Rotate values in both files; workspace must still win
                    # for the dual-homed key after refresh.
                    workspace_env.write_text("DUAL_KEY=workspace_dual_v2\nWS_ONLY_KEY=workspace_only_v2\n")
                    global_env.write_text("DUAL_KEY=global_dual_v2\nGLOBAL_ONLY_KEY=global_only_v2\n")

                    secrets_manager.refresh_from_env_file()

                    assert os.environ["DUAL_KEY"] == "workspace_dual_v2"
                    assert os.environ["WS_ONLY_KEY"] == "workspace_only_v2"
                    assert os.environ["GLOBAL_ONLY_KEY"] == "global_only_v2"

    def test_refresh_dual_homed_key_drops_to_global_when_workspace_removes_it(self) -> None:
        """Removing a key from workspace falls back to its global value.

        The ``get_secret`` precedence is workspace > global > none. When
        workspace removes a key that global still has, refresh must drop
        the resolution down to the global value rather than popping the
        key entirely.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_path = Path(temp_dir)
            workspace_env = workspace_path / ".env"
            global_env = workspace_path / "global.env"

            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CASCADE_KEY", None)
                config_manager = ConfigManager()
                config_manager.workspace_path = workspace_path

                with patch("griptape_nodes.retained_mode.managers.secrets_manager.ENV_VAR_PATH", global_env):
                    workspace_env.write_text("CASCADE_KEY=workspace_cascade\n")
                    global_env.write_text("CASCADE_KEY=global_cascade\n")

                    secrets_manager = SecretsManager(config_manager)
                    assert os.environ["CASCADE_KEY"] == "workspace_cascade"
                    assert "CASCADE_KEY" in secrets_manager._managed_env_keys

                    # Workspace removes the key; global still has it.
                    workspace_env.write_text("")

                    secrets_manager.refresh_from_env_file()

                    # Falls back to the global value, still managed.
                    assert os.environ["CASCADE_KEY"] == "global_cascade"
                    assert "CASCADE_KEY" in secrets_manager._managed_env_keys

    def test_full_lifecycle_keeps_bookkeeping_invariant(self) -> None:  # noqa: PLR0915
        """Drive a full secret lifecycle and verify the bookkeeping invariant survives each step.

        Sequence: boot -> set_secret -> refresh -> delete -> refresh.
        The invariant: every key in ``_managed_env_keys`` is in
        ``os.environ`` with the value the manager last installed. Drift
        between the two would let ``get_secret`` return stale values or
        mask operator-set OS env vars. The invariant is held by routing
        every ``os.environ`` mutation through ``_install_managed`` /
        ``_uninstall_managed``; this test guards against a future change
        that adds a fifth bookkeeping site that forgets to call them.

        Stronger property each step also asserts: a key the manager owns
        (managed) and whose value is in the merged file view must equal
        that file value. A managed key not in the merged file view is a
        stale install -- the only legitimate case is between
        ``set_secret`` and the file write hitting disk (the test does not
        exercise that race).
        """

        def assert_invariant(secrets_manager: SecretsManager) -> None:
            merged = secrets_manager._read_merged_env_files()
            for key in secrets_manager._managed_env_keys:
                assert key in os.environ, f"managed key {key!r} missing from os.environ"
                if key in merged:
                    assert os.environ[key] == merged[key], (
                        f"managed key {key!r} has os.environ value {os.environ[key]!r} but file says {merged[key]!r}"
                    )

        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_path = Path(temp_dir)
            global_env = workspace_path / "global.env"

            with patch.dict(os.environ, {}, clear=False):
                # Strip every key the test will touch so previous tests
                # cannot bleed in via the process-global os.environ.
                for key in (
                    "LIFECYCLE_BOOT_KEY",
                    "LIFECYCLE_SET_KEY",
                    "LIFECYCLE_VANISHING_KEY",
                    "LIFECYCLE_OS_SET_KEY",
                    "UNRELATED",
                ):
                    os.environ.pop(key, None)

                config_manager = ConfigManager()
                config_manager.workspace_path = workspace_path

                with patch("griptape_nodes.retained_mode.managers.secrets_manager.ENV_VAR_PATH", global_env):
                    # Step 1: boot. Two file keys plus an OS-set key that
                    # collides with one of them (so we exercise the
                    # OS-precedence boot branch).
                    global_env.write_text(
                        "LIFECYCLE_BOOT_KEY=boot_value\nLIFECYCLE_OS_SET_KEY=file_loses\n",
                    )
                    os.environ["LIFECYCLE_OS_SET_KEY"] = "operator_wins"
                    secrets_manager = SecretsManager(config_manager)

                    assert "LIFECYCLE_BOOT_KEY" in secrets_manager._managed_env_keys
                    assert "LIFECYCLE_OS_SET_KEY" not in secrets_manager._managed_env_keys
                    assert os.environ["LIFECYCLE_OS_SET_KEY"] == "operator_wins"
                    assert_invariant(secrets_manager)

                    # Step 2: set_secret two fresh keys. Manager claims
                    # ownership of each and writes through to both the
                    # global file and os.environ.
                    secrets_manager.set_secret("LIFECYCLE_SET_KEY", "set_value")
                    secrets_manager.set_secret("LIFECYCLE_VANISHING_KEY", "vanishing_value")
                    assert "LIFECYCLE_SET_KEY" in secrets_manager._managed_env_keys
                    assert "LIFECYCLE_VANISHING_KEY" in secrets_manager._managed_env_keys
                    assert os.environ["LIFECYCLE_SET_KEY"] == "set_value"
                    assert os.environ["LIFECYCLE_VANISHING_KEY"] == "vanishing_value"
                    assert_invariant(secrets_manager)

                    # Step 3: refresh after the file (a) rotates one
                    # managed key's value and (b) drops another managed
                    # key entirely. Both branches of the refresh loop
                    # fire from a single broadcast:
                    #   * LIFECYCLE_BOOT_KEY is in previously_managed AND
                    #     present in the merged file -> overridden to
                    #     the new value.
                    #   * LIFECYCLE_SET_KEY is in previously_managed AND
                    #     present -> re-installed (no-op write of the
                    #     same value, but the survived path is exercised).
                    #   * LIFECYCLE_VANISHING_KEY is in previously_managed
                    #     and NOT in the merged file -> popped via
                    #     ``previously_managed - survived``.
                    #   * LIFECYCLE_OS_SET_KEY is not managed and is in
                    #     os.environ -> left untouched.
                    global_env.write_text(
                        "LIFECYCLE_BOOT_KEY=boot_value_v2\n"
                        "LIFECYCLE_OS_SET_KEY=file_still_loses\n"
                        "LIFECYCLE_SET_KEY=set_value\n",
                    )
                    secrets_manager.refresh_from_env_file()
                    assert os.environ["LIFECYCLE_BOOT_KEY"] == "boot_value_v2"
                    assert os.environ["LIFECYCLE_OS_SET_KEY"] == "operator_wins"
                    assert os.environ["LIFECYCLE_SET_KEY"] == "set_value"
                    # The pop branch: dropped from os.environ AND released
                    # from manager ownership. (Pre-fix this branch would
                    # also have wiped LIFECYCLE_OS_SET_KEY -- the
                    # invariant check above would have caught the regression.)
                    assert "LIFECYCLE_VANISHING_KEY" not in os.environ
                    assert "LIFECYCLE_VANISHING_KEY" not in secrets_manager._managed_env_keys
                    assert_invariant(secrets_manager)

                    # Step 4: delete the boot key via the request handler.
                    # Only the managed os.environ entry should drop;
                    # OS-set keys with the same shape are unaffected (a
                    # delete request never names them in this test).
                    from griptape_nodes.retained_mode.events.secrets_events import DeleteSecretValueRequest

                    secrets_manager.on_handle_delete_secret_value_request(
                        DeleteSecretValueRequest(key="LIFECYCLE_BOOT_KEY"),
                    )
                    assert "LIFECYCLE_BOOT_KEY" not in os.environ
                    assert "LIFECYCLE_BOOT_KEY" not in secrets_manager._managed_env_keys
                    assert_invariant(secrets_manager)

                    # Step 5: refresh after a transient file-vanish window.
                    # Managed state must survive a missing file, and the
                    # invariant must still hold.
                    global_env.unlink()
                    secrets_manager.refresh_from_env_file()
                    # LIFECYCLE_SET_KEY was managed; with no file present
                    # the refresh leaves it alone (early-return guard).
                    assert os.environ.get("LIFECYCLE_SET_KEY") == "set_value"
                    assert "LIFECYCLE_SET_KEY" in secrets_manager._managed_env_keys
                    # OS-set still untouched.
                    assert os.environ["LIFECYCLE_OS_SET_KEY"] == "operator_wins"
                    assert_invariant(secrets_manager)

                    # Step 6: the file comes back with the previously-managed
                    # key removed from it. Refresh pops the managed key.
                    global_env.write_text("UNRELATED=value\n")
                    secrets_manager.refresh_from_env_file()
                    assert "LIFECYCLE_SET_KEY" not in os.environ
                    assert "LIFECYCLE_SET_KEY" not in secrets_manager._managed_env_keys
                    assert os.environ["UNRELATED"] == "value"
                    assert "UNRELATED" in secrets_manager._managed_env_keys
                    assert_invariant(secrets_manager)
