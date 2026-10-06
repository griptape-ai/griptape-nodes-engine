"""Tests for the externally managed environment hooks: env var parsing and settings."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from griptape_nodes.retained_mode.managers.config_manager import ConfigManager
from griptape_nodes.retained_mode.managers.external_environment import (
    LIBRARY_PATHS_ENV_VAR,
    library_paths_from_environment,
    read_provisioned_by,
)
from griptape_nodes.retained_mode.managers.settings import (
    FROM_ENV_CONTEXT,
    LIBRARY_PROVISIONED_BY_KEY,
    LibraryProvisioner,
    LibrarySettings,
)

# The context the env loader validates GTN_CONFIG_* overrides under.
_FROM_ENV = {FROM_ENV_CONTEXT: True}


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


class TestReadingTheSettings:
    def test_provisioned_by_defaults_to_engine(self) -> None:
        assert read_provisioned_by(_config_returning({})) is LibraryProvisioner.ENGINE

    def test_provisioned_by_accepts_any_letter_case(self) -> None:
        config = _config_returning({LIBRARY_PROVISIONED_BY_KEY: "Environment"})

        assert read_provisioned_by(config) is LibraryProvisioner.ENVIRONMENT

    def test_an_unknown_provisioner_reads_as_engine(self) -> None:
        config = _config_returning({LIBRARY_PROVISIONED_BY_KEY: "somewhere"})

        assert read_provisioned_by(config) is LibraryProvisioner.ENGINE


class TestEnvironmentOverrides:
    """The GTN_CONFIG_ variables a launcher sets must reach the settings typed, or be reported."""

    def test_provisioned_by_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GTN_CONFIG_LIBRARY__PROVISIONED_BY", "environment")
        manager = ConfigManager()
        manager.load_configs()

        assert read_provisioned_by(manager) is LibraryProvisioner.ENVIRONMENT

    def test_a_misspelled_provisioner_is_reported_not_turned_into_engine_silently(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("GTN_CONFIG_LIBRARY__PROVISIONED_BY", "env-provided")
        manager = ConfigManager()
        manager.load_configs()

        assert read_provisioned_by(manager) is LibraryProvisioner.ENGINE
        assert "GTN_CONFIG_LIBRARY__PROVISIONED_BY" in caplog.text


class TestSettingsValidation:
    """The validators, run the way the env loader (with the env context) and a config file run them."""

    def test_a_provisioner_that_is_already_typed_is_kept(self) -> None:
        settings = LibrarySettings.model_validate({"provisioned_by": LibraryProvisioner.ENVIRONMENT})

        assert settings.provisioned_by is LibraryProvisioner.ENVIRONMENT

    def test_an_unknown_provisioner_in_a_config_file_falls_back_to_engine(self) -> None:
        settings = LibrarySettings.model_validate({"provisioned_by": "somewhere"})

        assert settings.provisioned_by is LibraryProvisioner.ENGINE

    def test_an_unknown_provisioner_from_the_environment_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must be one of"):
            LibrarySettings.model_validate({"provisioned_by": "somewhere"}, context=_FROM_ENV)


class TestSuiteIsolation:
    def test_the_environment_the_suite_runs_in_is_not_read(self) -> None:
        """The unit-test conftest clears every variable these hooks read before each test.

        A developer running the suite from inside a prepared environment would otherwise have that
        environment's libraries discovered.
        """
        for name in (
            LIBRARY_PATHS_ENV_VAR,
            "GTN_CONFIG_LIBRARY__PROVISIONED_BY",
        ):
            assert name not in os.environ
