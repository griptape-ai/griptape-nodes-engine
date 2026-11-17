"""A library's artifact providers leave with the library, so reloading it registers them again."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from griptape_nodes.node_library.advanced_node_library import AdvancedNodeLibrary
from griptape_nodes.node_library.library_registry import Library, LibraryRegistry, LibrarySchema
from griptape_nodes.retained_mode.events.app_events import AppInitializationComplete
from griptape_nodes.retained_mode.events.artifact_events import RegisterArtifactProviderRequest
from griptape_nodes.retained_mode.events.library_events import (
    UnloadLibraryFromRegistryRequest,
    UnloadLibraryFromRegistryResultSuccess,
)
from griptape_nodes.retained_mode.managers.artifact_providers import BaseArtifactProvider
from griptape_nodes.retained_mode.managers.fitness_problems.libraries import AfterLibraryCallbackProblem
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine

LIBRARY_NAME = "Provider Test Library"


@pytest.fixture(autouse=True)
def forget_test_library(engine: Engine) -> Iterator[None]:
    """Drop the test library and its providers, even when a test fails mid-load."""
    yield
    LibraryRegistry._libraries.pop(LIBRARY_NAME, None)
    engine.artifact_manager.unregister_providers_for_library(LIBRARY_NAME)


def _make_provider_class(friendly_name: str = "ReloadTest") -> type[BaseArtifactProvider]:
    class ReloadTestProvider(BaseArtifactProvider):
        @classmethod
        def get_friendly_name(cls) -> str:
            return friendly_name

        @classmethod
        def get_supported_formats(cls) -> set[str]:
            return {"reloadtest"}

        @classmethod
        def get_preview_formats(cls) -> set[str]:
            return set()

        @classmethod
        def get_artifact_metadata(cls, _source_path: str) -> None:
            return None

    return ReloadTestProvider


def _load(
    engine: Engine,
    provider_class: type[BaseArtifactProvider],
    tmp_path: Path,
    *,
    register_before_nodes_load: bool = False,
) -> LibraryManager.LibraryInfo:
    """Load a library that registers `provider_class` from a load callback and raises if that fails."""

    def register_provider() -> None:
        result = engine.handle_request(RegisterArtifactProviderRequest(provider_class=provider_class))
        if not result.succeeded():
            raise RuntimeError(result.result_details)

    class ProviderLib(AdvancedNodeLibrary):
        def before_library_nodes_loaded(self, _library_data: LibrarySchema, _library: Library) -> None:
            if register_before_nodes_load:
                register_provider()

        def after_library_nodes_loaded(self, _library_data: LibrarySchema, _library: Library) -> None:
            if not register_before_nodes_load:
                register_provider()

    schema = MagicMock(spec=LibrarySchema)
    schema.is_default_library = False
    schema.name = LIBRARY_NAME
    schema.nodes = []
    schema.workflow_nodes = []
    schema.widgets = []
    schema.config_categories = []
    library = LibraryRegistry.generate_new_library(library_data=schema, advanced_library=ProviderLib())
    library_info = LibraryManager.LibraryInfo(
        lifecycle_state=LibraryManager.LibraryLifecycleState.EVALUATED,
        fitness=LibraryManager.LibraryFitness.NOT_EVALUATED,
        library_path=str(tmp_path / "library.json"),
        is_sandbox=False,
        library_name=LIBRARY_NAME,
        library_version="1.0.0",
    )
    engine.library_manager.module_loading.attempt_load_nodes_from_library(
        library_data=schema,
        library=library,
        base_dir=tmp_path,
        library_info=library_info,
    )
    return library_info


def _unload(engine: Engine) -> None:
    result = engine.handle_request(UnloadLibraryFromRegistryRequest(library_name=LIBRARY_NAME))
    assert isinstance(result, UnloadLibraryFromRegistryResultSuccess)


def _callback_problems(library_info: LibraryManager.LibraryInfo) -> list:
    return [problem for problem in library_info.problems if isinstance(problem, AfterLibraryCallbackProblem)]


class TestLibraryArtifactProvidersOnReload:
    def test_provider_registered_before_nodes_load_unloads_with_the_library(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        registry = engine.artifact_manager._registry
        provider_class = _make_provider_class()
        _load(engine, provider_class, tmp_path, register_before_nodes_load=True)
        _unload(engine)

        assert registry.get_provider_class_by_friendly_name("ReloadTest") is None

    def test_provider_whose_settings_fail_still_unloads_with_the_library(self, engine: Engine, tmp_path: Path) -> None:
        registry = engine.artifact_manager._registry

        class BrokenSettingsProvider(_make_provider_class()):
            @classmethod
            def get_preview_formats(cls) -> set[str]:
                return {"png"}

            @classmethod
            def get_default_preview_format(cls) -> str:
                msg = "no default preview format"
                raise RuntimeError(msg)

        first_load = _load(engine, BrokenSettingsProvider, tmp_path)
        assert _callback_problems(first_load) != []
        _unload(engine)

        assert registry.get_provider_class_by_friendly_name("ReloadTest") is None

    def test_reloading_a_library_registers_its_provider_again(self, engine: Engine, tmp_path: Path) -> None:
        provider_class = _make_provider_class()
        first_load = _load(engine, provider_class, tmp_path)
        _unload(engine)
        second_load = _load(engine, provider_class, tmp_path)

        assert _callback_problems(first_load) == []
        assert _callback_problems(second_load) == []

    def test_reload_registers_the_current_provider_class(self, engine: Engine, tmp_path: Path) -> None:
        registry = engine.artifact_manager._registry
        old_provider_class = _make_provider_class()
        _load(engine, old_provider_class, tmp_path)
        _unload(engine)
        new_provider_class = _make_provider_class()
        _load(engine, new_provider_class, tmp_path)

        assert registry.get_provider_class_by_friendly_name("ReloadTest") is new_provider_class
        assert registry.get_provider_classes_by_format("reloadtest") == [new_provider_class]
        assert old_provider_class not in registry.get_all_provider_classes()

    def test_unloading_a_library_removes_its_provider(self, engine: Engine, tmp_path: Path) -> None:
        registry = engine.artifact_manager._registry
        provider_class = _make_provider_class()
        _load(engine, provider_class, tmp_path)
        registry.get_or_create_provider_instance(provider_class)
        _unload(engine)

        assert registry.get_provider_class_by_friendly_name("ReloadTest") is None
        assert registry.get_provider_classes_by_format("reloadtest") == []
        assert provider_class not in registry._provider_instances

    @pytest.mark.asyncio
    async def test_unloading_a_library_keeps_built_in_providers(self, engine: Engine, tmp_path: Path) -> None:
        registry = engine.artifact_manager._registry
        await engine.artifact_manager.on_app_initialization_complete(AppInitializationComplete())
        built_in_provider_classes = list(registry.get_all_provider_classes())
        assert built_in_provider_classes
        _load(engine, _make_provider_class(), tmp_path)
        _unload(engine)

        assert registry.get_all_provider_classes() == built_in_provider_classes
