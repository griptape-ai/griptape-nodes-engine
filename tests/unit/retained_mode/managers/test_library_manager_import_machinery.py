"""Tests for import-machinery containment around advanced-library hooks in LibraryManager.

An advanced library's hook that removes importlib's frozen submodules from ``sys.modules``
breaks every later import in the engine process. The manager repairs the damage and records it
against the library whose hook caused it, so the failure is not attributed to whichever library
happens to load next.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from griptape_nodes.retained_mode.managers.fitness_problems.libraries import ImportMachineryDamagedProblem
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager
from griptape_nodes.utils.import_machinery_utils import snapshot_import_machinery

if TYPE_CHECKING:
    from collections.abc import Generator

_BOOTSTRAP = "importlib._bootstrap"


@pytest.fixture(autouse=True)
def _preserve_import_machinery() -> Generator[None, None, None]:
    """Put the real frozen modules back after each test.

    These tests corrupt process-wide interpreter state on purpose; leaking it would break every
    test that runs afterwards.
    """
    saved = {name: sys.modules[name] for name in (_BOOTSTRAP, "importlib._bootstrap_external")}
    try:
        yield
    finally:
        for name, module in saved.items():
            sys.modules[name] = module
            _, _, attribute_name = name.partition("importlib.")
            setattr(importlib, attribute_name, module)


@pytest.fixture
def library_info() -> LibraryManager.LibraryInfo:
    """A LibraryInfo to collect problems on."""
    return LibraryManager.LibraryInfo(
        lifecycle_state=LibraryManager.LibraryLifecycleState.EVALUATED,
        fitness=LibraryManager.LibraryFitness.NOT_EVALUATED,
        library_path="/libraries/some-library/griptape-nodes-library.json",
        is_sandbox=False,
        library_name="Badly Behaved Library",
    )


def _poison_bootstrap() -> None:
    """Replace the frozen bootstrap the way a bad library hook does."""
    impostor = ModuleType(_BOOTSTRAP)
    impostor.__spec__ = importlib.machinery.ModuleSpec(_BOOTSTRAP, loader=None, origin="/not/frozen/_bootstrap.py")  # type: ignore[arg-type]
    sys.modules[_BOOTSTRAP] = impostor
    importlib._bootstrap = impostor  # type: ignore[attr-defined]


class TestRepairImportMachineryAfterHook:
    """Test LibraryManager._repair_import_machinery_after_hook."""

    def test_well_behaved_hook_records_no_problem(self, library_info: LibraryManager.LibraryInfo) -> None:
        """A hook that leaves the import machinery alone is not flagged."""
        snapshot = snapshot_import_machinery()

        LibraryManager._repair_import_machinery_after_hook(
            MagicMock(),
            snapshot,
            hook_name="before_library_nodes_loaded",
            library_data=MagicMock(name="Well Behaved Library"),
            library_info=library_info,
        )

        assert library_info.problems == []

    def test_damaging_hook_records_problem_against_its_own_library(
        self, library_info: LibraryManager.LibraryInfo
    ) -> None:
        """The library whose hook broke the import machinery is the one blamed."""
        snapshot = snapshot_import_machinery()
        _poison_bootstrap()

        LibraryManager._repair_import_machinery_after_hook(
            MagicMock(),
            snapshot,
            hook_name="before_library_nodes_loaded",
            library_data=MagicMock(name="Badly Behaved Library"),
            library_info=library_info,
        )

        assert len(library_info.problems) == 1
        problem = library_info.problems[0]
        assert isinstance(problem, ImportMachineryDamagedProblem)
        assert problem.hook_name == "before_library_nodes_loaded"
        assert problem.module_names == [_BOOTSTRAP]

    def test_damaging_hook_leaves_imports_working(self, library_info: LibraryManager.LibraryInfo) -> None:
        """Later libraries can still import, so one bad hook cannot poison the session."""
        snapshot = snapshot_import_machinery()
        _poison_bootstrap()

        LibraryManager._repair_import_machinery_after_hook(
            MagicMock(),
            snapshot,
            hook_name="before_library_nodes_loaded",
            library_data=MagicMock(name="Badly Behaved Library"),
            library_info=library_info,
        )

        assert importlib.import_module("json").__name__ == "json"

    def test_problem_display_names_the_hook_and_module(self) -> None:
        """The displayed problem tells the user which hook and module, and what to do."""
        problem = ImportMachineryDamagedProblem(hook_name="before_library_nodes_loaded", module_names=[_BOOTSTRAP])

        display = ImportMachineryDamagedProblem.collate_problems_for_display([problem])

        assert "before_library_nodes_loaded" in display
        assert _BOOTSTRAP in display
        assert "restart" in display.lower()
