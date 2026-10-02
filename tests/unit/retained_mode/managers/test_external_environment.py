"""Tests for the externally managed environment hooks: env var parsing, worker prefixes, and settings."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from griptape_nodes.retained_mode.managers.config_manager import ConfigManager
from griptape_nodes.retained_mode.managers.external_environment import (
    LIBRARY_PATHS_ENV_VAR,
    LIBRARY_WORKER_REQUESTS_ENV_VAR,
    WorkerCommand,
    WorkerCommandRefusal,
    library_paths_from_environment,
    read_dependency_source,
    read_worker_command_prefix,
    resolve_worker_command,
    worker_requests_from_environment,
)
from griptape_nodes.retained_mode.managers.settings import (
    BETA_FEATURES_FROM_ENV_CONTEXT,
    LIBRARY_DEPENDENCY_SOURCE_KEY,
    WORKER_COMMAND_PREFIX_KEY,
    LibraryDependencySource,
    LibrarySettings,
    WorkerSettings,
)

# The context the env loader validates GTN_CONFIG_* overrides under.
_FROM_ENV = {BETA_FEATURES_FROM_ENV_CONTEXT: True}

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


class TestResolveWorkerCommand:
    def test_no_prefix_leaves_the_command_alone(self) -> None:
        result = resolve_worker_command(
            command=_COMMAND,
            prefix=[],
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
            prefix=["tool", "env", "python-{python_version}", "{library_request}", "--"],
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
            prefix=["tool", "env", "engine=={engine_version}", "{library_request}", "--name={library_name}", "--"],
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
            prefix=["tool", "{library_request}", "--"],
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
            prefix=["--packages={library_request}"],
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
            prefix=["--packages={library_request}"],
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
            prefix=["tool", "{library_request}", "--"],
            library_name="Foo Library",
            worker_requests={"Other Library": "lib_other"},
            engine_version="0.103.0",
            python_version="3.12",
            environment_mode=True,
        )

        assert isinstance(result, WorkerCommandRefusal)
        assert "'Foo Library'" in result.reason
        assert LIBRARY_WORKER_REQUESTS_ENV_VAR in result.reason

    def test_venv_mode_runs_a_library_with_no_request_unprefixed(self) -> None:
        result = resolve_worker_command(
            command=_COMMAND,
            prefix=["tool", "{library_request}", "--"],
            library_name="Foo Library",
            worker_requests={},
            engine_version="0.103.0",
            python_version="3.12",
            environment_mode=False,
        )

        assert result == WorkerCommand(args=_COMMAND)

    def test_a_prefix_that_needs_no_request_applies_without_one(self) -> None:
        result = resolve_worker_command(
            command=_COMMAND,
            prefix=["wrapper", "--engine={engine_version}"],
            library_name="Foo Library",
            worker_requests={},
            engine_version="0.103.0",
            python_version="3.12",
            environment_mode=True,
        )

        assert result == WorkerCommand(args=["wrapper", "--engine=0.103.0", *_COMMAND])


class TestReadingTheSettings:
    def test_dependency_source_defaults_to_venv(self) -> None:
        assert read_dependency_source(_config_returning({})) is LibraryDependencySource.VENV

    def test_dependency_source_accepts_any_letter_case(self) -> None:
        config = _config_returning({LIBRARY_DEPENDENCY_SOURCE_KEY: "Environment"})

        assert read_dependency_source(config) is LibraryDependencySource.ENVIRONMENT

    def test_an_unknown_dependency_source_reads_as_venv(self) -> None:
        config = _config_returning({LIBRARY_DEPENDENCY_SOURCE_KEY: "somewhere"})

        assert read_dependency_source(config) is LibraryDependencySource.VENV

    def test_a_prefix_that_is_not_a_list_is_not_used(self) -> None:
        config = _config_returning({WORKER_COMMAND_PREFIX_KEY: "tool env --"})

        assert read_worker_command_prefix(config) == []

    def test_a_prefix_with_a_non_text_entry_is_not_used(self) -> None:
        config = _config_returning({WORKER_COMMAND_PREFIX_KEY: ["tool", 3]})

        assert read_worker_command_prefix(config) == []


class TestEnvironmentOverrides:
    """The GTN_CONFIG_ variables a launcher sets must reach the settings typed, or be reported."""

    def test_command_prefix_takes_a_json_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GTN_CONFIG_WORKER__COMMAND_PREFIX", '["tool", "env", "{library_request}", "--"]')
        manager = ConfigManager()
        manager.load_configs()

        assert manager.get_config_value(WORKER_COMMAND_PREFIX_KEY) == ["tool", "env", "{library_request}", "--"]
        assert read_worker_command_prefix(manager) == ["tool", "env", "{library_request}", "--"]

    def test_a_command_prefix_that_is_not_json_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("GTN_CONFIG_WORKER__COMMAND_PREFIX", "tool env --")
        manager = ConfigManager()
        manager.load_configs()

        assert read_worker_command_prefix(manager) == []
        assert "GTN_CONFIG_WORKER__COMMAND_PREFIX" in caplog.text

    def test_dependency_source_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GTN_CONFIG_LIBRARY__DEPENDENCY_SOURCE", "environment")
        manager = ConfigManager()
        manager.load_configs()

        assert read_dependency_source(manager) is LibraryDependencySource.ENVIRONMENT

    def test_a_misspelled_dependency_source_is_reported_not_turned_into_venv_silently(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("GTN_CONFIG_LIBRARY__DEPENDENCY_SOURCE", "env-provided")
        manager = ConfigManager()
        manager.load_configs()

        assert read_dependency_source(manager) is LibraryDependencySource.VENV
        assert "GTN_CONFIG_LIBRARY__DEPENDENCY_SOURCE" in caplog.text


class TestSettingsValidation:
    """The validators, run the way the env loader (with the env context) and a config file run them."""

    def test_a_blank_command_prefix_variable_means_no_prefix(self) -> None:
        settings = WorkerSettings.model_validate({"command_prefix": "  "}, context=_FROM_ENV)

        assert settings.command_prefix == []

    def test_a_dependency_source_that_is_already_typed_is_kept(self) -> None:
        settings = LibrarySettings.model_validate({"dependency_source": LibraryDependencySource.ENVIRONMENT})

        assert settings.dependency_source is LibraryDependencySource.ENVIRONMENT

    def test_an_unknown_dependency_source_in_a_config_file_falls_back_to_venv(self) -> None:
        settings = LibrarySettings.model_validate({"dependency_source": "somewhere"})

        assert settings.dependency_source is LibraryDependencySource.VENV

    def test_an_unknown_dependency_source_from_the_environment_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must be one of"):
            LibrarySettings.model_validate({"dependency_source": "somewhere"}, context=_FROM_ENV)


class TestSuiteIsolation:
    def test_the_environment_the_suite_runs_in_is_not_read(self) -> None:
        """The unit-test conftest clears every variable these hooks read before each test.

        A developer running the suite from inside a prepared environment would otherwise have that
        environment's libraries discovered and its prefix put in front of test workers.
        """
        for name in (
            LIBRARY_PATHS_ENV_VAR,
            LIBRARY_WORKER_REQUESTS_ENV_VAR,
            "GTN_CONFIG_LIBRARY__DEPENDENCY_SOURCE",
            "GTN_CONFIG_WORKER__COMMAND_PREFIX",
        ):
            assert name not in os.environ
