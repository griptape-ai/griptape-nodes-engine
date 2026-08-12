"""Tests for explaining an import failure caused by reloading a library mid-session.

Python caches imported modules for the life of the process, so re-registering a library whose
node modules are already in memory cannot replace the code they imported. Node files get
re-executed; the helper packages they import do not. A node file that starts importing a symbol
its update introduced therefore fails against the previously imported helper, and no amount of
reloading fixes it. The engine has to say so, because the remedy is a restart and nothing about
the raw ImportError suggests that.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from griptape_nodes.node_library.library_registry import (
    CategoryDefinition,
    LibraryMetadata,
    LibraryRegistry,
    LibrarySchema,
)
from griptape_nodes.retained_mode.events.library_events import UnloadLibraryFromRegistryRequest
from griptape_nodes.retained_mode.managers.fitness_problems.libraries.node_module_import_problem import (
    NodeModuleImportProblem,
)
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

LIBRARY_NAME = "Stale Module Test Library"
STABLE_NAMESPACE = "griptape_nodes.node_libraries.stale_module_test_library.some_node"


@pytest.fixture(autouse=True)
def _clear_registry() -> Iterator[None]:
    # LibraryRegistry keeps its state in ClassVars, which the singleton reset does not touch.
    LibraryRegistry._clear()
    yield
    LibraryRegistry._clear()


def _register_library(manager: LibraryManager, tmp_path: Path, *, problems: list | None = None) -> None:
    schema = LibrarySchema(
        name=LIBRARY_NAME,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="test", description="test", library_version="1.0.0", engine_version="1.0.0", tags=[]
        ),
        categories=[{"Test": CategoryDefinition(title="Test", description="test", color="#000", icon="Folder")}],
        nodes=[],
    )
    LibraryRegistry.generate_new_library(library_data=schema)
    library_file_path = str(tmp_path / "griptape_nodes_library.json")
    manager._library_file_path_to_info[library_file_path] = LibraryManager.LibraryInfo(
        lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
        library_path=library_file_path,
        is_sandbox=False,
        library_name=LIBRARY_NAME,
        library_version="1.0.0",
        fitness=LibraryManager.LibraryFitness.GOOD,
        problems=problems if problems is not None else [],
    )


def _pretend_a_node_module_loaded(manager: LibraryManager) -> None:
    """Record what `_register_stable_module_alias` records when a node module actually imports."""
    manager._library_to_stable_modules.setdefault(LIBRARY_NAME, set()).add(STABLE_NAMESPACE)


def _unload(manager: LibraryManager) -> None:
    result = manager.unload_library_from_registry_request(UnloadLibraryFromRegistryRequest(library_name=LIBRARY_NAME))
    assert result.succeeded()


class TestStaleModuleDetection:
    def test_a_library_that_never_loaded_a_module_needs_no_restart(
        self, griptape_nodes: GriptapeNodes, tmp_path: Path
    ) -> None:
        manager = griptape_nodes.LibraryManager()
        _register_library(manager, tmp_path)

        _unload(manager)

        # Nothing was imported, so re-registering picks the library up cleanly.
        assert manager.was_reloaded_after_its_modules_were_imported(LIBRARY_NAME) is False
        assert manager.explain_stale_module_failure(LIBRARY_NAME) is None

    def test_reloading_after_a_module_loaded_is_remembered(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        manager = griptape_nodes.LibraryManager()
        _register_library(manager, tmp_path)
        _pretend_a_node_module_loaded(manager)

        _unload(manager)

        assert manager.was_reloaded_after_its_modules_were_imported(LIBRARY_NAME) is True
        explanation = manager.explain_stale_module_failure(LIBRARY_NAME)
        assert explanation is not None
        assert "Restart the engine" in explanation
        assert LIBRARY_NAME in explanation

    def test_the_marker_survives_a_successful_re_register(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        manager = griptape_nodes.LibraryManager()
        _register_library(manager, tmp_path)
        _pretend_a_node_module_loaded(manager)
        _unload(manager)
        _register_library(manager, tmp_path)

        # Only restarting the process clears cached modules, so re-registering must not clear it.
        assert manager.was_reloaded_after_its_modules_were_imported(LIBRARY_NAME) is True

    def test_an_untouched_library_is_never_blamed(self, griptape_nodes: GriptapeNodes) -> None:
        manager = griptape_nodes.LibraryManager()

        assert manager.was_reloaded_after_its_modules_were_imported("Some Other Library") is False
        assert manager.explain_stale_module_failure("Some Other Library") is None


class TestNodeImportProblemReporting:
    def test_no_problems_recorded(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        manager = griptape_nodes.LibraryManager()
        _register_library(manager, tmp_path)

        assert manager.library_has_node_import_problems(LIBRARY_NAME) is False
        assert manager.get_library_name_reporting_node_import_failure("SomeNode") is None

    def test_import_problems_are_found_and_attributed(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        manager = griptape_nodes.LibraryManager()
        _register_library(
            manager,
            tmp_path,
            problems=[
                NodeModuleImportProblem(
                    class_name="SomeNode",
                    file_path="nodes/some_node.py",
                    error_message="cannot import name 'added_by_update'",
                    root_cause="cannot import name 'added_by_update'",
                )
            ],
        )

        assert manager.library_has_node_import_problems(LIBRARY_NAME) is True
        # A node type whose module failed to import registers nowhere, so the recorded failure is
        # the only thing that can name its library.
        assert manager.get_library_name_reporting_node_import_failure("SomeNode") == LIBRARY_NAME
        assert manager.get_library_name_reporting_node_import_failure("UnrelatedNode") is None
