"""Tests for the externally managed environment hooks: env var parsing, worker prefixes, and settings."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from griptape_nodes.retained_mode.managers.config_manager import ConfigManager
from griptape_nodes.retained_mode.managers.external_environment import (
    LIBRARY_PATHS_ENV_VAR,
    LIBRARY_SECTION_KEY,
    LIBRARY_WORKER_REQUESTS_ENV_VAR,
    WorkerCommand,
    WorkerCommandPrefix,
    WorkerCommandRefusal,
    library_paths_from_environment,
    read_provisioned_by,
    read_worker_command_prefix,
    resolve_worker_command,
    sandbox_enabled,
    worker_requests_from_environment,
)
from griptape_nodes.retained_mode.managers.settings import (
    FROM_ENV_CONTEXT,
    LIBRARY_PROVISIONED_BY_KEY,
    WORKER_COMMAND_PREFIX_KEY,
    LibraryProvisioner,
    LibrarySettings,
    WorkerSettings,
)

# The context the env loader validates GTN_CONFIG_* overrides under.
_FROM_ENV = {FROM_ENV_CONTEXT: True}

_COMMAND = ["/engine/python", "-m", "griptape_nodes_app", "engine", "--library-name", "Foo Library"]


def _config_returning(values: dict[str, object]) -> MagicMock:
    config = MagicMock()
    config.get_config_value.side_effect = lambda key, default=None, **_: values.get(key, default)
    return config


class TestLibraryPathsFromEnvironment:
    def test_entries_keep_their_order(self) -> None:
        environ = {LIBRARY_PATHS_ENV_VAR: os.pathsep.join(["/b/lib.json", "/a/lib.json"])}

        assert library_paths_from_environment(environ) == ["/b/lib.json", "/a/lib.json"]

    def test_blanks_and_repeats_are_dropped(self) -> None:
        environ = {LIBRARY_PATHS_ENV_VAR: os.pathsep.join(["/a/lib.json", "", " ", "/a/lib.json", "/b/lib.json"])}

        assert library_paths_from_environment(environ) == ["/a/lib.json", "/b/lib.json"]

    def test_unset_means_no_libraries(self) -> None:
        assert library_paths_from_environment({}) == []


class TestWorkerRequestsFromEnvironment:
    def test_library_names_may_contain_spaces_and_requests_may_contain_the_separator(self) -> None:
        environ = {LIBRARY_WORKER_REQUESTS_ENV_VAR: os.pathsep.join(["Foo Library=lib_foo==1.4.2", "Bar=lib_bar"])}

        assert worker_requests_from_environment(environ) == {"Foo Library": "lib_foo==1.4.2", "Bar": "lib_bar"}

    def test_the_first_entry_for_a_library_wins(self) -> None:
        environ = {LIBRARY_WORKER_REQUESTS_ENV_VAR: os.pathsep.join(["Foo=first", "Foo=second"])}

        assert worker_requests_from_environment(environ) == {"Foo": "first"}

    def test_malformed_entries_are_skipped(self) -> None:
        environ = {LIBRARY_WORKER_REQUESTS_ENV_VAR: os.pathsep.join(["no-separator", "=no-name", "NoRequest=", "Ok=x"])}

        assert worker_requests_from_environment(environ) == {"Ok": "x"}

    def test_a_request_cannot_contain_the_separator(self, caplog: pytest.LogCaptureFixture) -> None:
        # Entries are separated like PATH entries, so a request containing the separator is split.
        environ = {LIBRARY_WORKER_REQUESTS_ENV_VAR: f"Foo Library=lib_foo{os.pathsep}tail{os.pathsep}Bar=lib_bar"}

        assert worker_requests_from_environment(environ) == {"Foo Library": "lib_foo", "Bar": "lib_bar"}
        assert "'tail'" in caplog.text
        assert f"separated by {os.pathsep!r}" in caplog.text


class TestResolveWorkerCommand:
    def test_no_prefix_leaves_the_command_alone(self) -> None:
        result = resolve_worker_command(
            command=_COMMAND,
            prefix=WorkerCommandPrefix(words=[]),
            library_name="Foo Library",
            worker_requests={},
            engine_version="0.103.0",
            python_version="3.12",
            environment_mode=True,
        )

        assert result == WorkerCommand(args=_COMMAND)

    def test_python_version_is_filled(self) -> None:
        result = resolve_worker_command(
            command=_COMMAND,
            prefix=WorkerCommandPrefix(words=["tool", "env", "python-{python_version}", "{library_request}", "--"]),
            library_name="Foo Library",
            worker_requests={"Foo Library": "lib_foo==1.4.2"},
            engine_version="0.103.0",
            python_version="3.13",
            environment_mode=True,
        )

        assert result == WorkerCommand(args=["tool", "env", "python-3.13", "lib_foo==1.4.2", "--", *_COMMAND])

    def test_placeholders_are_filled_and_the_prefix_goes_first(self) -> None:
        result = resolve_worker_command(
            command=_COMMAND,
            prefix=WorkerCommandPrefix(
                words=["tool", "env", "engine=={engine_version}", "{library_request}", "--name={library_name}", "--"]
            ),
            library_name="Foo Library",
            worker_requests={"Foo Library": "lib_foo==1.4.2"},
            engine_version="0.103.0",
            python_version="3.12",
            environment_mode=True,
        )

        assert result == WorkerCommand(
            args=["tool", "env", "engine==0.103.0", "lib_foo==1.4.2", "--name=Foo Library", "--", *_COMMAND]
        )

    def test_a_whole_word_request_becomes_one_word_per_part(self) -> None:
        result = resolve_worker_command(
            command=_COMMAND,
            prefix=WorkerCommandPrefix(words=["tool", "{library_request}", "--"]),
            library_name="Foo Library",
            worker_requests={"Foo Library": "lib_foo==1.4.2  extra_pkg"},
            engine_version="0.103.0",
            python_version="3.12",
            environment_mode=True,
        )

        assert result == WorkerCommand(args=["tool", "lib_foo==1.4.2", "extra_pkg", "--", *_COMMAND])

    def test_a_request_inside_a_word_is_replaced_as_text(self) -> None:
        result = resolve_worker_command(
            command=_COMMAND,
            prefix=WorkerCommandPrefix(words=["--packages={library_request}"]),
            library_name="Foo Library",
            worker_requests={"Foo Library": "a b"},
            engine_version="0.103.0",
            python_version="3.12",
            environment_mode=True,
        )

        assert result == WorkerCommand(args=["--packages=a b", *_COMMAND])

    def test_placeholder_text_inside_a_request_is_not_expanded(self) -> None:
        result = resolve_worker_command(
            command=_COMMAND,
            prefix=WorkerCommandPrefix(words=["--packages={library_request}"]),
            library_name="Foo Library",
            worker_requests={"Foo Library": "{library_name}"},
            engine_version="0.103.0",
            python_version="3.12",
            environment_mode=True,
        )

        assert result == WorkerCommand(args=["--packages={library_name}", *_COMMAND])

    def test_environment_mode_refuses_a_library_with_no_request(self) -> None:
        """Starting it unprefixed would run the library against whatever the engine's environment holds."""
        result = resolve_worker_command(
            command=_COMMAND,
            prefix=WorkerCommandPrefix(words=["tool", "{library_request}", "--"]),
            library_name="Foo Library",
            worker_requests={"Other Library": "lib_other"},
            engine_version="0.103.0",
            python_version="3.12",
            environment_mode=True,
        )

        assert isinstance(result, WorkerCommandRefusal)
        assert "'Foo Library'" in result.reason
        assert LIBRARY_WORKER_REQUESTS_ENV_VAR in result.reason

    def test_engine_mode_runs_a_library_with_no_request_unprefixed(self, caplog: pytest.LogCaptureFixture) -> None:
        result = resolve_worker_command(
            command=_COMMAND,
            prefix=WorkerCommandPrefix(words=["tool", "{library_request}", "--"]),
            library_name="Foo Library",
            worker_requests={},
            engine_version="0.103.0",
            python_version="3.12",
            environment_mode=False,
        )

        assert result == WorkerCommand(args=_COMMAND)
        # The only clue that the prefix was dropped, so it is a warning.
        assert any(record.levelname == "WARNING" and "Foo Library" in record.getMessage() for record in caplog.records)

    def test_a_prefix_that_needs_no_request_applies_without_one(self) -> None:
        result = resolve_worker_command(
            command=_COMMAND,
            prefix=WorkerCommandPrefix(words=["wrapper", "--engine={engine_version}"]),
            library_name="Foo Library",
            worker_requests={},
            engine_version="0.103.0",
            python_version="3.12",
            environment_mode=True,
        )

        assert result == WorkerCommand(args=["wrapper", "--engine=0.103.0", *_COMMAND])


class TestUnusablePrefix:
    def test_environment_mode_refuses_a_worker_when_the_prefix_cannot_be_used(self) -> None:
        result = resolve_worker_command(
            command=_COMMAND,
            prefix=WorkerCommandPrefix(words=[], problem="GTN_CONFIG_WORKER__COMMAND_PREFIX is set, but broken"),
            library_name="Foo Library",
            worker_requests={"Foo Library": "lib_foo==1.4.2"},
            engine_version="0.103.0",
            python_version="3.12",
            environment_mode=True,
        )

        assert isinstance(result, WorkerCommandRefusal)
        assert "GTN_CONFIG_WORKER__COMMAND_PREFIX" in result.reason

    def test_engine_mode_still_starts_the_worker_with_what_applies(self) -> None:
        result = resolve_worker_command(
            command=_COMMAND,
            prefix=WorkerCommandPrefix(words=[], problem="GTN_CONFIG_WORKER__COMMAND_PREFIX is set, but broken"),
            library_name="Foo Library",
            worker_requests={},
            engine_version="0.103.0",
            python_version="3.12",
            environment_mode=False,
        )

        assert result == WorkerCommand(args=_COMMAND)


class TestReadingTheSettings:
    def test_provisioned_by_defaults_to_engine(self) -> None:
        assert read_provisioned_by(_config_returning({})) is LibraryProvisioner.ENGINE

    def test_provisioned_by_accepts_any_letter_case(self) -> None:
        config = _config_returning({LIBRARY_SECTION_KEY: {"provisioned_by": "Environment"}})

        assert read_provisioned_by(config) is LibraryProvisioner.ENVIRONMENT

    def test_an_unknown_provisioner_fails_closed_to_environment(self, caplog: pytest.LogCaptureFixture) -> None:
        config = _config_returning({LIBRARY_SECTION_KEY: {"provisioned_by": "somewhere"}})

        assert read_provisioned_by(config) is LibraryProvisioner.ENVIRONMENT
        assert any(
            record.levelname == "ERROR" and LIBRARY_PROVISIONED_BY_KEY in record.message for record in caplog.records
        )

    def test_a_bad_value_in_another_library_setting_leaves_the_provisioner_alone(self) -> None:
        config = _config_returning({LIBRARY_SECTION_KEY: {"minimum_release_age": "not-a-number"}})

        assert read_provisioned_by(config) is LibraryProvisioner.ENGINE

    @pytest.mark.parametrize(
        ("provisioner", "raw", "expected"),
        [
            # Unset, or anything that is not true/false: the mode's default.
            ("engine", None, True),
            ("environment", None, False),
            ("engine", 1, True),
            ("environment", "maybe", False),
            # Set: wins in either mode, in any letter case.
            ("environment", " TRUE ", True),
            ("environment", True, True),
            ("engine", "false", False),
            ("engine", False, False),
        ],
    )
    def test_sandbox_enabled_defaults_by_mode_and_a_set_value_wins(
        self, provisioner: str, raw: object, *, expected: bool
    ) -> None:
        config = _config_returning({LIBRARY_SECTION_KEY: {"provisioned_by": provisioner, "sandbox_enabled": raw}})

        assert sandbox_enabled(config) is expected

    def test_a_prefix_that_is_not_a_list_is_reported_not_used(self) -> None:
        config = _config_returning({WORKER_COMMAND_PREFIX_KEY: "tool env --"})

        prefix = read_worker_command_prefix(config, {})

        assert prefix.words == []
        assert prefix.problem is not None
        assert WORKER_COMMAND_PREFIX_KEY in prefix.problem

    def test_a_prefix_with_a_non_text_entry_is_reported_not_used(self) -> None:
        config = _config_returning({WORKER_COMMAND_PREFIX_KEY: ["tool", 3]})

        prefix = read_worker_command_prefix(config, {})

        assert prefix.words == []
        assert prefix.problem is not None

    def test_an_unset_prefix_is_usable(self) -> None:
        assert read_worker_command_prefix(_config_returning({}), {}) == WorkerCommandPrefix(words=[])

    def test_an_explicitly_empty_prefix_variable_is_usable(self) -> None:
        prefix = read_worker_command_prefix(_config_returning({}), {"GTN_CONFIG_WORKER__COMMAND_PREFIX": "[]"})

        assert prefix == WorkerCommandPrefix(words=[])

    def test_a_variable_that_is_not_json_is_reported(self) -> None:
        prefix = read_worker_command_prefix(
            _config_returning({}), {"GTN_CONFIG_WORKER__COMMAND_PREFIX": "tool env {library_request} --"}
        )

        assert prefix.words == []
        assert prefix.problem is not None
        assert "GTN_CONFIG_WORKER__COMMAND_PREFIX" in prefix.problem


class TestEnvironmentOverrides:
    """The GTN_CONFIG_ variables a launcher sets must reach the settings typed, or be reported."""

    def test_command_prefix_takes_a_json_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GTN_CONFIG_WORKER__COMMAND_PREFIX", '["tool", "env", "{library_request}", "--"]')
        manager = ConfigManager()
        manager.load_configs()

        assert manager.get_config_value(WORKER_COMMAND_PREFIX_KEY) == ["tool", "env", "{library_request}", "--"]
        assert read_worker_command_prefix(manager, dict(os.environ)) == WorkerCommandPrefix(
            words=["tool", "env", "{library_request}", "--"]
        )

    def test_a_command_prefix_that_is_not_json_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("GTN_CONFIG_WORKER__COMMAND_PREFIX", "tool env --")
        manager = ConfigManager()
        manager.load_configs()

        prefix = read_worker_command_prefix(manager, dict(os.environ))

        assert prefix.words == []
        assert prefix.problem is not None
        assert "GTN_CONFIG_WORKER__COMMAND_PREFIX" in caplog.text

    def test_provisioned_by_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GTN_CONFIG_LIBRARY__PROVISIONED_BY", "environment")
        manager = ConfigManager()
        manager.load_configs()

        assert read_provisioned_by(manager) is LibraryProvisioner.ENVIRONMENT

    def test_a_misspelled_provisioner_fails_closed_to_environment_with_an_error(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("GTN_CONFIG_LIBRARY__PROVISIONED_BY", "env-provided")
        manager = ConfigManager()
        manager.load_configs()

        assert read_provisioned_by(manager) is LibraryProvisioner.ENVIRONMENT
        assert any(record.levelname == "ERROR" and "'env-provided'" in record.message for record in caplog.records)


class TestSettingsValidation:
    """The validators, run the way the env loader (with the env context) and a config file run them."""

    def test_a_blank_command_prefix_variable_means_no_prefix(self) -> None:
        settings = WorkerSettings.model_validate({"command_prefix": "  "}, context=_FROM_ENV)

        assert settings.command_prefix == []

    def test_a_provisioner_that_is_already_typed_is_kept(self) -> None:
        settings = LibrarySettings.model_validate({"provisioned_by": LibraryProvisioner.ENVIRONMENT})

        assert settings.provisioned_by is LibraryProvisioner.ENVIRONMENT

    def test_an_unknown_provisioner_in_a_config_file_fails_closed_to_environment(self) -> None:
        settings = LibrarySettings.model_validate({"provisioned_by": "somewhere"})

        assert settings.provisioned_by is LibraryProvisioner.ENVIRONMENT

    def test_an_unknown_provisioner_from_the_environment_fails_closed_to_environment(self) -> None:
        settings = LibrarySettings.model_validate({"provisioned_by": "somewhere"}, context=_FROM_ENV)

        assert settings.provisioned_by is LibraryProvisioner.ENVIRONMENT

    def test_the_sandbox_setting_is_unset_by_default(self) -> None:
        assert LibrarySettings().sandbox_enabled is None

    def test_a_typed_sandbox_setting_is_kept(self) -> None:
        settings = LibrarySettings.model_validate({"sandbox_enabled": True})

        assert settings.sandbox_enabled is True

    def test_a_bad_sandbox_setting_in_a_config_file_falls_back_to_the_default(self) -> None:
        settings = LibrarySettings.model_validate({"sandbox_enabled": "maybe"})

        assert settings.sandbox_enabled is None

    def test_a_bad_sandbox_setting_from_the_environment_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must be true or false"):
            LibrarySettings.model_validate({"sandbox_enabled": "maybe"}, context=_FROM_ENV)


class TestSandboxEnabledFromTheEnvironment:
    @pytest.mark.parametrize(("raw", "expected"), [("TRUE", True), ("true", True), ("False", False)])
    def test_true_or_false_in_any_letter_case(
        self, monkeypatch: pytest.MonkeyPatch, raw: str, *, expected: bool
    ) -> None:
        monkeypatch.setenv("GTN_CONFIG_LIBRARY__PROVISIONED_BY", "environment")
        monkeypatch.setenv("GTN_CONFIG_LIBRARY__SANDBOX_ENABLED", raw)
        manager = ConfigManager()
        manager.load_configs()

        assert sandbox_enabled(manager) is expected

    def test_a_bad_value_is_reported_and_the_mode_default_applies(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("GTN_CONFIG_LIBRARY__SANDBOX_ENABLED", "maybe")
        monkeypatch.setenv("GTN_CONFIG_LIBRARY__PROVISIONED_BY", "environment")
        manager = ConfigManager()
        manager.load_configs()

        assert sandbox_enabled(manager) is False
        assert "GTN_CONFIG_LIBRARY__SANDBOX_ENABLED" in caplog.text

    def test_engine_mode_can_turn_the_sandbox_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GTN_CONFIG_LIBRARY__SANDBOX_ENABLED", "false")
        manager = ConfigManager()
        manager.load_configs()

        assert sandbox_enabled(manager) is False


class TestSuiteIsolation:
    def test_the_environment_the_suite_runs_in_is_not_read(self) -> None:
        """The unit-test conftest clears every variable these hooks read before each test.

        A developer running the suite from inside a prepared environment would otherwise have that
        environment's libraries discovered and its prefix put in front of test workers.
        """
        for name in (
            LIBRARY_PATHS_ENV_VAR,
            LIBRARY_WORKER_REQUESTS_ENV_VAR,
            "GTN_CONFIG_LIBRARY__PROVISIONED_BY",
            "GTN_CONFIG_WORKER__COMMAND_PREFIX",
            "GTN_CONFIG_LIBRARY__SANDBOX_ENABLED",
        ):
            assert name not in os.environ
