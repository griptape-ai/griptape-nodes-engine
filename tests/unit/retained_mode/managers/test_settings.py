"""Unit tests for the Settings model validators."""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from griptape_nodes.retained_mode.managers.settings import (
    FROM_ENV_CONTEXT,
    AppInitializationComplete,
    DistributedExecutionSettings,
    LibraryDependencyInstallBehavior,
    LibraryDownload,
    LibraryRegistration,
    OpenImageIOSettings,
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


class TestDistributedExecutionMaxWorkers:
    """`max_workers` is a limit of at least one worker, or empty for none."""

    @pytest.mark.parametrize("value", [0, -1])
    def test_a_limit_below_one_is_rejected(self, value: int) -> None:
        with pytest.raises(ValidationError):
            DistributedExecutionSettings.model_validate({"max_workers": value})

    @pytest.mark.parametrize("value", [None, 1, 4])
    def test_no_limit_or_a_positive_limit_is_accepted(self, value: int | None) -> None:
        settings = DistributedExecutionSettings.model_validate({"max_workers": value})

        assert settings.max_workers == value


class TestOpenImageIOSettings:
    """OpenImageIO cache, concurrency, and size-limit settings coerce bad persisted values to defaults."""

    def test_defaults(self) -> None:
        defaults = Settings().openimageio

        assert isinstance(defaults, OpenImageIOSettings)
        assert defaults.max_memory_mb == 1024  # noqa: PLR2004
        assert defaults.max_open_files == 100  # noqa: PLR2004
        assert defaults.autotile_size == 0
        assert defaults.deduplicate is True
        assert defaults.threads == 0
        assert defaults.max_image_size_mb == 4096  # noqa: PLR2004
        assert defaults.failure_retries == 0

    def test_valid_values_are_preserved(self) -> None:
        parsed = Settings.model_validate(
            {
                "openimageio": {
                    "max_memory_mb": 4096,
                    "max_open_files": 250,
                    "autotile_size": 128,
                    "deduplicate": False,
                    "threads": 8,
                    "max_image_size_mb": 8192,
                    "failure_retries": 3,
                }
            }
        ).openimageio

        assert parsed.max_memory_mb == 4096  # noqa: PLR2004
        assert parsed.max_open_files == 250  # noqa: PLR2004
        assert parsed.autotile_size == 128  # noqa: PLR2004
        assert parsed.deduplicate is False
        assert parsed.threads == 8  # noqa: PLR2004
        assert parsed.max_image_size_mb == 8192  # noqa: PLR2004
        assert parsed.failure_retries == 3  # noqa: PLR2004

    @pytest.mark.parametrize(
        ("field_name", "bad_value", "expected_default"),
        [
            ("max_memory_mb", 32, 1024),
            ("max_memory_mb", True, 1024),
            ("max_memory_mb", "abc", 1024),
            ("max_open_files", 5, 100),
            ("max_open_files", 5000, 100),
            ("autotile_size", 99, 0),
            ("autotile_size", -64, 0),
            ("deduplicate", "yes", True),
            ("threads", -1, 0),
            ("threads", 512, 0),
            ("max_image_size_mb", -10, 4096),
            ("failure_retries", -1, 0),
            ("failure_retries", 20, 0),
        ],
    )
    def test_invalid_persisted_field_falls_back_to_default_with_warning(
        self,
        field_name: str,
        bad_value: object,
        expected_default: object,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            result = Settings.model_validate({"openimageio": {field_name: bad_value}})

        assert getattr(result.openimageio, field_name) == expected_default
        assert f"openimageio.{field_name}" in caplog.text

    def test_non_mapping_openimageio_falls_back_to_defaults(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            result = Settings.model_validate({"openimageio": "invalid"})

        assert result.openimageio == OpenImageIOSettings()
        assert "openimageio" in caplog.text

    def test_environment_values_are_converted(self) -> None:
        result = Settings.model_validate(
            {
                "openimageio": {
                    "max_memory_mb": "2048",
                    "max_open_files": "150",
                    "autotile_size": "256",
                    "deduplicate": "false",
                    "threads": "4",
                    "max_image_size_mb": "0",
                    "failure_retries": "2",
                }
            },
            context={FROM_ENV_CONTEXT: True},
        ).openimageio

        assert result.max_memory_mb == 2048  # noqa: PLR2004
        assert result.max_open_files == 150  # noqa: PLR2004
        assert result.autotile_size == 256  # noqa: PLR2004
        assert result.deduplicate is False
        assert result.threads == 4  # noqa: PLR2004
        assert result.max_image_size_mb == 0
        assert result.failure_retries == 2  # noqa: PLR2004

    @pytest.mark.parametrize(
        ("field_name", "bad_env_value"),
        [
            ("max_memory_mb", "32"),
            ("max_open_files", "9"),
            ("autotile_size", "100"),
            ("deduplicate", "maybe"),
            ("threads", "-1"),
            ("max_image_size_mb", "-5"),
            ("failure_retries", "11"),
        ],
    )
    def test_invalid_environment_value_raises_validation_error(self, field_name: str, bad_env_value: str) -> None:
        with pytest.raises(ValidationError, match=f"openimageio.{field_name}"):
            Settings.model_validate(
                {"openimageio": {field_name: bad_env_value}},
                context={FROM_ENV_CONTEXT: True},
            )
