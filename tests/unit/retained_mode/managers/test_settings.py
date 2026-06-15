"""Unit tests for the Settings model validators."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from griptape_nodes.retained_mode.managers.settings import (
    AppInitializationComplete,
    LibraryRegistration,
    Settings,
)


class TestLibraryRegistrationSources:
    """A registration must name at least one source, and sourced entries need a name."""

    def test_bare_string_entry_is_valid_in_the_list(self) -> None:
        config = AppInitializationComplete.model_validate({"libraries_to_register": ["griptape_nodes_library.json"]})

        assert config.libraries_to_register == ["griptape_nodes_library.json"]

    def test_path_with_worker_mode_override_is_valid(self) -> None:
        registration = LibraryRegistration.model_validate({"path": "../shared/lib", "worker_mode_override": "WORKER"})

        assert registration.path == "../shared/lib"
        assert registration.worker_mode_override == "WORKER"

    def test_sourced_entry_without_path_is_valid(self) -> None:
        registration = LibraryRegistration.model_validate(
            {"name": "git-lib", "git_url": "griptape-ai/git-lib@v2.0", "version": ">=2.0,<3"}
        )

        assert registration.path is None
        assert registration.git_url == "griptape-ai/git-lib@v2.0"

    def test_no_source_and_no_path_raises(self) -> None:
        with pytest.raises(ValidationError, match="at least one of 'path' or 'git_url'"):
            LibraryRegistration.model_validate({"version": ">=1.0"})

    def test_sourced_entry_without_name_raises(self) -> None:
        with pytest.raises(ValidationError, match="requires 'name'"):
            LibraryRegistration.model_validate({"git_url": "griptape-ai/git-lib@v2.0"})


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
