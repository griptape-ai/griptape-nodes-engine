"""Metadata reports whether each library is actually in the registry.

Metadata is derived from whatever `libraries_to_register` currently resolves to, while the
registry holds what was actually loaded. Those two disagree whenever config changes after
boot without a library reload: a settings write updates config and only marks a refresh as
pending, so until the user acts on it the engine advertises libraries it cannot answer any
other question about.

Nothing in the response used to expose that. A client would follow a metadata entry with a
name-keyed call and get `no Library with that name was registered` back, which is what
produced a wall of errors in a reported session. `is_registered` makes the difference
visible so those entries can be shown as pending a refresh instead of being queried.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.node_library.library_registry import (
    LibraryMetadata,
    LibraryRegistry,
    LibrarySchema,
)
from griptape_nodes.retained_mode.events.library_events import (
    CheckLibraryUpdateRequest,
    CheckLibraryUpdateResultFailure,
    LoadMetadataForAllLibrariesRequest,
    LoadMetadataForAllLibrariesResultSuccess,
)
from griptape_nodes.retained_mode.managers.settings import (
    LIBRARIES_TO_DOWNLOAD_KEY,
    LIBRARIES_TO_REGISTER_KEY,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine

LOADED_LIBRARY = "Nuke Nodes Library"
DECLARED_BUT_NOT_LOADED = "Griptape Nodes VOID Library"


def _library_json(name: str) -> str:
    schema = LibrarySchema(
        name=name,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="test",
            description=f"{name} manifest",
            library_version="0.1.0",
            engine_version="0.98.0",
            tags=[],
        ),
        categories=[],
        nodes=[],
    )
    return json.dumps(schema.model_dump(mode="json"))


def _write_library(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "griptape_nodes_library.json"
    manifest.write_text(_library_json(name), encoding="utf-8")
    return manifest


def _register_only_config(libraries: object) -> Callable[..., object]:
    """A `get_config_value` side effect serving only `libraries_to_register`."""

    def get_config_value(key: str, *, default: object = None, **_: object) -> object:
        if key == LIBRARIES_TO_REGISTER_KEY:
            return libraries
        if key == LIBRARIES_TO_DOWNLOAD_KEY:
            return []
        return default

    return get_config_value


def _register_in_registry(name: str) -> None:
    LibraryRegistry.generate_new_library(
        library_data=LibrarySchema(
            name=name,
            library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
            metadata=LibraryMetadata(
                author="test",
                description="loaded at boot",
                library_version="0.1.0",
                engine_version="0.98.0",
                tags=[],
            ),
            categories=[],
            nodes=[],
        )
    )


class TestMetadataReportsRegistryMembership:
    @pytest.fixture(autouse=True)
    def _clean_registry(self) -> Generator[None, None, None]:
        LibraryRegistry._clear()
        yield
        LibraryRegistry._clear()

    @pytest.fixture
    def two_declared_libraries(self, tmp_path: Path) -> list[str]:
        """Two libraries in config; only one of them will be registered."""
        return [
            str(_write_library(tmp_path / "loaded", LOADED_LIBRARY)),
            str(_write_library(tmp_path / "declared-only", DECLARED_BUT_NOT_LOADED)),
        ]

    @pytest.mark.asyncio
    async def test_declared_but_unloaded_libraries_are_flagged(
        self, engine: Engine, two_declared_libraries: list[str]
    ) -> None:
        """The exact divergence from the report: config has both, the registry has one."""
        library_manager = engine.library_manager
        _register_in_registry(LOADED_LIBRARY)

        with pytest.MonkeyPatch.context() as patcher:
            patcher.setattr(engine.config_manager, "get_config_value", _register_only_config(two_declared_libraries))
            result = await library_manager.load_metadata_for_all_libraries_request(LoadMetadataForAllLibrariesRequest())

        assert isinstance(result, LoadMetadataForAllLibrariesResultSuccess)
        by_name = {entry.library_schema.name: entry for entry in result.successful_libraries}

        assert by_name[LOADED_LIBRARY].is_registered is True
        assert by_name[DECLARED_BUT_NOT_LOADED].is_registered is False

    @pytest.mark.asyncio
    async def test_the_flag_predicts_whether_a_name_keyed_call_will_work(
        self, engine: Engine, two_declared_libraries: list[str]
    ) -> None:
        """The point of the flag: it tells a client which entries are safe to ask about.

        Every entry reporting is_registered False is exactly an entry whose follow-up call
        fails with "no Library with that name was registered", so a client that skips them
        never provokes that error.
        """
        library_manager = engine.library_manager
        _register_in_registry(LOADED_LIBRARY)

        with pytest.MonkeyPatch.context() as patcher:
            patcher.setattr(engine.config_manager, "get_config_value", _register_only_config(two_declared_libraries))
            metadata = await library_manager.load_metadata_for_all_libraries_request(
                LoadMetadataForAllLibrariesRequest()
            )
            assert isinstance(metadata, LoadMetadataForAllLibrariesResultSuccess)

            for entry in metadata.successful_libraries:
                name = entry.library_schema.name
                update_result = await library_manager.check_library_update_request(
                    CheckLibraryUpdateRequest(library_name=name)
                )
                disowned = isinstance(update_result, CheckLibraryUpdateResultFailure) and (
                    "no Library with that name was registered" in str(update_result.result_details)
                )
                assert disowned is not entry.is_registered

    @pytest.mark.asyncio
    async def test_a_second_copy_of_a_loaded_name_reports_registered(self, engine: Engine, tmp_path: Path) -> None:
        """Keyed on name, not path, because the follow-up requests are name-keyed too.

        Two manifests for one library name: only one copy loads, but a query for that name
        succeeds either way, so both entries report True.
        """
        library_manager = engine.library_manager
        _register_in_registry(LOADED_LIBRARY)

        copies = [
            str(_write_library(tmp_path / "copy-a", LOADED_LIBRARY)),
            str(_write_library(tmp_path / "copy-b", LOADED_LIBRARY)),
        ]

        with pytest.MonkeyPatch.context() as patcher:
            patcher.setattr(engine.config_manager, "get_config_value", _register_only_config(copies))
            result = await library_manager.load_metadata_for_all_libraries_request(LoadMetadataForAllLibrariesRequest())

        assert isinstance(result, LoadMetadataForAllLibrariesResultSuccess)
        assert [entry.is_registered for entry in result.successful_libraries] == [True, True]

    @pytest.mark.asyncio
    async def test_is_registered_is_independent_of_enabled(self, engine: Engine, tmp_path: Path) -> None:
        """`enabled` is the user's config flag; this is a registry answer. Different things."""
        library_manager = engine.library_manager
        manifest = str(_write_library(tmp_path / "loaded", LOADED_LIBRARY))
        _register_in_registry(LOADED_LIBRARY)

        with pytest.MonkeyPatch.context() as patcher:
            patcher.setattr(engine.config_manager, "get_config_value", _register_only_config([manifest]))
            result = await library_manager.load_metadata_for_all_libraries_request(LoadMetadataForAllLibrariesRequest())

        assert isinstance(result, LoadMetadataForAllLibrariesResultSuccess)
        entry = result.successful_libraries[0]
        # Nothing disabled it, and it is in the registry: the two flags are set from
        # unrelated sources and must not be conflated by callers.
        assert entry.enabled is True
        assert entry.is_registered is True
