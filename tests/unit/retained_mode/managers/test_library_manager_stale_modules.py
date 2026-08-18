"""Tests for explaining an import failure caused by reloading a library mid-session.

Python caches imported modules for the life of the process, so re-registering a library whose node
modules are already in memory cannot replace the code they imported, and only a restart can. The
engine has to say so, because nothing about the raw ImportError suggests it.
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
from griptape_nodes.retained_mode.events.base_events import ResultDetails
from griptape_nodes.retained_mode.events.library_events import UnloadLibraryFromRegistryRequest
from griptape_nodes.retained_mode.managers.fitness_problems.libraries.node_module_import_problem import (
    NodeModuleImportProblem,
)
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from griptape_nodes.retained_mode.events.base_events import ResultPayload
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


def _import_problem() -> NodeModuleImportProblem:
    return NodeModuleImportProblem(
        class_name="SomeNode",
        file_path="nodes/some_node.py",
        error_message="cannot import name 'added_by_update'",
        root_cause="cannot import name 'added_by_update'",
    )


def _first_detail_message(result: ResultPayload) -> str:
    assert isinstance(result.result_details, ResultDetails)
    return result.result_details.result_details[0].message


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
        assert manager._was_reloaded_after_its_modules_were_imported(LIBRARY_NAME) is False
        assert manager.explain_stale_module_failure(LIBRARY_NAME) is None

    def test_reloading_after_a_module_loaded_is_remembered(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        manager = griptape_nodes.LibraryManager()
        _register_library(manager, tmp_path)
        _pretend_a_node_module_loaded(manager)

        _unload(manager)

        assert manager._was_reloaded_after_its_modules_were_imported(LIBRARY_NAME) is True
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
        assert manager._was_reloaded_after_its_modules_were_imported(LIBRARY_NAME) is True

    def test_an_untouched_library_is_never_blamed(self, griptape_nodes: GriptapeNodes) -> None:
        manager = griptape_nodes.LibraryManager()

        assert manager._was_reloaded_after_its_modules_were_imported("Some Other Library") is False
        assert manager.explain_stale_module_failure("Some Other Library") is None


class TestNodeImportProblemReporting:
    def test_no_problems_recorded(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        manager = griptape_nodes.LibraryManager()
        _register_library(manager, tmp_path)

        assert manager._library_has_node_import_problems(LIBRARY_NAME) is False
        assert manager.get_library_name_for_node_type("SomeNode") is None

    def test_import_problems_are_found_and_attributed(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        manager = griptape_nodes.LibraryManager()
        _register_library(
            manager,
            tmp_path,
            problems=[_import_problem()],
        )

        assert manager._library_has_node_import_problems(LIBRARY_NAME) is True
        # A node type whose module failed to import registers nowhere, so the recorded failure is
        # the only thing that can name its library.
        assert manager.get_library_name_for_node_type("SomeNode") == LIBRARY_NAME
        assert manager.get_library_name_for_node_type("UnrelatedNode") is None


class TestReloadResultsReportRestart:
    """Every path that reloads a library in place has to report the restart, not just updates."""

    def test_a_clean_reload_reports_no_restart(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        manager = griptape_nodes.LibraryManager()
        _register_library(manager, tmp_path)
        _pretend_a_node_module_loaded(manager)
        _unload(manager)
        _register_library(manager, tmp_path)

        # The library was reloaded after importing, but its nodes took the new code, so the artist
        # has nothing to do.
        assert manager._explain_restart_after_reload(LIBRARY_NAME) is None

        update_result = manager._build_library_update_result(
            library_name=LIBRARY_NAME, old_version="1.0.0", new_version="2.0.0"
        )
        assert update_result.restart_required is False

        switch_result = manager._build_library_ref_switch_result(
            library_name=LIBRARY_NAME, old_ref="main", new_ref="dev", old_version="1.0.0", new_version="2.0.0"
        )
        assert switch_result.restart_required is False

    def test_a_stale_reload_reports_a_restart_for_updates_and_ref_switches(
        self, griptape_nodes: GriptapeNodes, tmp_path: Path
    ) -> None:
        manager = griptape_nodes.LibraryManager()
        _register_library(manager, tmp_path)
        _pretend_a_node_module_loaded(manager)
        _unload(manager)
        _register_library(manager, tmp_path, problems=[_import_problem()])

        update_result = manager._build_library_update_result(
            library_name=LIBRARY_NAME, old_version="1.0.0", new_version="2.0.0"
        )
        assert update_result.restart_required is True
        assert "Restart the engine" in _first_detail_message(update_result)

        # Switching a branch or tag reloads through the same path, so it owes the artist the same
        # explanation.
        switch_result = manager._build_library_ref_switch_result(
            library_name=LIBRARY_NAME, old_ref="main", new_ref="dev", old_version="1.0.0", new_version="2.0.0"
        )
        assert switch_result.restart_required is True
        assert "Restart the engine" in _first_detail_message(switch_result)
        assert switch_result.new_ref == "dev"

    def test_an_import_failure_without_a_reload_asks_for_no_restart(
        self, griptape_nodes: GriptapeNodes, tmp_path: Path
    ) -> None:
        manager = griptape_nodes.LibraryManager()
        _register_library(manager, tmp_path, problems=[_import_problem()])

        # The library is simply broken: it was never reloaded on top of imported modules, so a
        # restart would change nothing.
        assert manager._explain_restart_after_reload(LIBRARY_NAME) is None
        assert (
            manager._build_library_update_result(
                library_name=LIBRARY_NAME, old_version="1.0.0", new_version="2.0.0"
            ).restart_required
            is False
        )
