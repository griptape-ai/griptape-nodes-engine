"""Unit tests for the Settings model validators."""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from griptape_nodes.retained_mode.managers import settings as settings_module
from griptape_nodes.retained_mode.managers.settings import (
    FROM_ENV_CONTEXT,
    AppInitializationComplete,
    LibraryDependencyInstallBehavior,
    LibraryDownload,
    LibraryRegistration,
    Settings,
)


class TestLibraryRegistration:
    """libraries_to_register entries name a local library by path; git fields moved to LibraryDownload."""

    def test_bare_string_entry_is_valid_in_the_list(self) -> None:
        config = AppInitializationComplete.model_validate({"libraries_to_register": ["griptape_nodes_library.json"]})

        assert config.libraries_to_register == ["griptape_nodes_library.json"]

    def test_path_with_worker_mode_override_is_valid(self) -> None:
        """A config in the wild still carries this key, and `extra="forbid"` would reject an unknown one.

        It no longer decides anything; the field exists so the entry parses.
        """
        registration = LibraryRegistration.model_validate({"path": "../shared/lib", "worker_mode_override": "WORKER"})

        assert registration.path == "../shared/lib"
        assert registration.worker_mode_override == "WORKER"

    def test_git_fields_are_rejected(self) -> None:
        # git_url/version/name moved to LibraryDownload; extra="forbid" now rejects them here.
        with pytest.raises(ValidationError):
            LibraryRegistration.model_validate({"git_url": "griptape-ai/git-lib@v2.0", "name": "git-lib"})

    def test_missing_path_raises(self) -> None:
        # A register entry's only identity is its path; the object form requires it.
        with pytest.raises(ValidationError):
            LibraryRegistration.model_validate({"enabled": True})


class TestLibraryDownload:
    """libraries_to_download entries carry git_url plus an optional version pin and name."""

    def test_object_form_is_valid(self) -> None:
        download = LibraryDownload.model_validate(
            {"git_url": "griptape-ai/git-lib@v2.0", "version": ">=2.0,<3", "name": "git-lib"}
        )

        assert download.git_url == "griptape-ai/git-lib@v2.0"
        assert download.version == ">=2.0,<3"
        assert download.name == "git-lib"

    def test_version_and_name_default_to_none(self) -> None:
        download = LibraryDownload.model_validate({"git_url": "griptape-ai/git-lib@v2.0"})

        assert download.version is None
        assert download.name is None

    def test_bare_string_entry_is_valid_in_the_list(self) -> None:
        config = AppInitializationComplete.model_validate({"libraries_to_download": ["griptape-ai/git-lib@v2.0"]})

        assert config.libraries_to_download == ["griptape-ai/git-lib@v2.0"]

    def test_object_entry_is_valid_in_the_list(self) -> None:
        config = AppInitializationComplete.model_validate(
            {"libraries_to_download": [{"git_url": "griptape-ai/git-lib@v2.0", "version": ">=2.0"}]}
        )

        entry = config.libraries_to_download[0]
        assert isinstance(entry, LibraryDownload)
        assert entry.git_url == "griptape-ai/git-lib@v2.0"
        assert entry.version == ">=2.0"

    def test_missing_git_url_raises(self) -> None:
        with pytest.raises(ValidationError):
            LibraryDownload.model_validate({"version": ">=1.0"})

    def test_unknown_field_raises(self) -> None:
        with pytest.raises(ValidationError):
            LibraryDownload.model_validate({"git_url": "griptape-ai/git-lib@v2.0", "path": "../shared/lib"})


class TestThreadStorageBackend:
    """The backend field migrates legacy values instead of failing validation.

    A config persisted before Griptape Cloud thread storage was removed carries
    ``thread_storage_backend: "gtc"``. Validation must coerce it to ``"local"``
    rather than raise, otherwise the whole merged config is discarded and every
    other user setting reverts to defaults.
    """

    @pytest.mark.parametrize("value", ["gtc", "whatever", "", None, 123])
    def test_legacy_or_unknown_values_coerce_to_local(self, value: object) -> None:
        assert Settings.model_validate({"thread_storage_backend": value}).thread_storage_backend == "local"

    def test_local_is_preserved(self) -> None:
        assert Settings.model_validate({"thread_storage_backend": "local"}).thread_storage_backend == "local"

    def test_default_is_local(self) -> None:
        assert Settings().thread_storage_backend == "local"


class TestMaxWorkflowRecursionDepth:
    """The recursion limit defaults to 10 and cannot be set below 1."""

    def test_default_is_ten(self) -> None:
        assert Settings().max_workflow_recursion_depth == 10  # noqa: PLR2004

    @pytest.mark.parametrize("value", [0, -1])
    def test_values_below_one_from_a_file_become_one(self, value: int, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger=settings_module.logger.name):
            result = Settings.model_validate({"max_workflow_recursion_depth": value})

        assert result.max_workflow_recursion_depth == 1
        assert "below its minimum of 1" in caplog.text

    @pytest.mark.parametrize("value", [0, -1, "0", "-1"])
    def test_values_below_one_from_env_are_rejected(self, value: int | str) -> None:
        with pytest.raises(ValidationError):
            Settings.model_validate({"max_workflow_recursion_depth": value}, context={FROM_ENV_CONTEXT: True})

    @pytest.mark.parametrize("value", [1, 500])
    def test_valid_values_pass_unchanged(self, value: int) -> None:
        assert Settings.model_validate({"max_workflow_recursion_depth": value}).max_workflow_recursion_depth == value


class TestLibraryDependencyInstallBehavior:
    """The library.dependency_install_behavior field coerces bad persisted values to ALWAYS.

    A typo or stale value in a persisted config must not fail whole-config
    validation and reset every other user setting to defaults.
    """

    @pytest.mark.parametrize("value", ["typo", "", None, 123, "ALWAYS"])
    def test_unknown_values_coerce_to_always(self, value: object) -> None:
        result = Settings.model_validate({"library": {"dependency_install_behavior": value}})
        assert result.library.dependency_install_behavior == LibraryDependencyInstallBehavior.ALWAYS

    @pytest.mark.parametrize("value", ["always", "never"])
    def test_valid_string_values_are_preserved(self, value: str) -> None:
        result = Settings.model_validate({"library": {"dependency_install_behavior": value}})
        assert result.library.dependency_install_behavior == LibraryDependencyInstallBehavior(value)

    def test_default_is_always(self) -> None:
        assert Settings().library.dependency_install_behavior == LibraryDependencyInstallBehavior.ALWAYS


class TestRefreshPreviewsOnFileChange:
    """Previews refresh on file changes unless the user turns it off."""

    def test_default_is_on(self) -> None:
        assert Settings().refresh_previews_on_file_change is True

    def test_watches_at_most_1000_files_by_default(self) -> None:
        assert Settings().refresh_previews_max_files == 1000  # noqa: PLR2004


class TestRefreshPreviewsMaxFiles:
    """refresh_previews_max_files falls back to its default instead of failing the whole config."""

    def test_a_whole_number_is_kept(self) -> None:
        assert Settings.model_validate({"refresh_previews_max_files": 250}).refresh_previews_max_files == 250  # noqa: PLR2004

    @pytest.mark.parametrize("value", ["abc", 0, -5, True, 12.5, None])
    def test_an_unusable_value_falls_back_to_the_default_with_a_warning(
        self, value: object, caplog: pytest.LogCaptureFixture
    ) -> None:
        from griptape_nodes.retained_mode.managers import settings as settings_module

        settings_module._reported_invalid_settings.discard(("refresh_previews_max_files", repr(value)))
        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            result = Settings.model_validate({"refresh_previews_max_files": value})

        assert result.refresh_previews_max_files == settings_module.DEFAULT_REFRESH_PREVIEWS_MAX_FILES
        assert "Ignoring refresh_previews_max_files" in caplog.text

    def test_an_environment_value_is_converted(self) -> None:
        from griptape_nodes.retained_mode.managers.settings import FROM_ENV_CONTEXT

        result = Settings.model_validate({"refresh_previews_max_files": "250"}, context={FROM_ENV_CONTEXT: True})

        assert result.refresh_previews_max_files == 250  # noqa: PLR2004

    @pytest.mark.parametrize("value", ["abc", "0", "12.5"])
    def test_an_unusable_environment_value_is_reported(self, value: str) -> None:
        from griptape_nodes.retained_mode.managers.settings import FROM_ENV_CONTEXT

        with pytest.raises(ValidationError, match="refresh_previews_max_files must be a whole number"):
            Settings.model_validate({"refresh_previews_max_files": value}, context={FROM_ENV_CONTEXT: True})
