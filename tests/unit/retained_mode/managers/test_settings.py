"""Unit tests for the Settings model validators."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from griptape_nodes.retained_mode.managers.settings import (
    AppInitializationComplete,
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
        registration = LibraryRegistration.model_validate({"path": "../shared/lib", "worker_mode_override": "WORKER"})

        assert registration.path == "../shared/lib"
        assert registration.worker_mode_override == "WORKER"

    def test_git_fields_are_rejected(self) -> None:
        # git_url/version/name moved to LibraryDownload; extra="forbid" now rejects them here.
        with pytest.raises(ValidationError):
            LibraryRegistration.model_validate({"git_url": "griptape-ai/git-lib@v2.0", "name": "git-lib"})


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
