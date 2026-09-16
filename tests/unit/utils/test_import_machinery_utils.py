"""Unit tests for import_machinery_utils module."""

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.utils.import_machinery_utils import (
    find_unfrozen_import_modules,
    restore_import_machinery,
    snapshot_import_machinery,
)

if TYPE_CHECKING:
    from collections.abc import Generator

_BOOTSTRAP = "importlib._bootstrap"


@pytest.fixture(autouse=True)
def _preserve_import_machinery() -> Generator[None, None, None]:
    """Put the real frozen modules back after each test.

    These tests deliberately corrupt process-wide interpreter state, so leaking a broken entry
    would break every test that runs afterwards, not just this module.
    """
    saved = {name: sys.modules[name] for name in (_BOOTSTRAP, "importlib._bootstrap_external")}
    try:
        yield
    finally:
        for name, module in saved.items():
            sys.modules[name] = module
            _, _, attribute_name = name.partition("importlib.")
            setattr(importlib, attribute_name, module)


def _poison_bootstrap() -> ModuleType:
    """Replace the frozen bootstrap with a stand-in, as a bad library hook does.

    A module object built here stands in for the from-source re-import that the real failure
    produces. Both share the property that matters: it is not the frozen module the interpreter
    started with, and it has no `sys` global.

    Returns:
        The stand-in module that was installed.
    """
    impostor = ModuleType(_BOOTSTRAP)
    impostor.__spec__ = importlib.machinery.ModuleSpec(_BOOTSTRAP, loader=None, origin="/not/frozen/_bootstrap.py")  # type: ignore[arg-type]
    sys.modules[_BOOTSTRAP] = impostor
    importlib._bootstrap = impostor  # type: ignore[attr-defined]
    return impostor


class TestRestoreImportMachinery:
    """Test snapshot_import_machinery / restore_import_machinery."""

    def test_reports_nothing_when_machinery_untouched(self) -> None:
        """A hook that leaves sys.modules alone produces no restored modules."""
        snapshot = snapshot_import_machinery()

        assert restore_import_machinery(snapshot) == []

    def test_reports_nothing_when_hook_clears_only_third_party_modules(self) -> None:
        """Clearing third-party modules is legitimate and must not be flagged."""
        sys.modules["griptape_nodes_fake_dependency"] = ModuleType("griptape_nodes_fake_dependency")
        snapshot = snapshot_import_machinery()

        del sys.modules["griptape_nodes_fake_dependency"]

        assert restore_import_machinery(snapshot) == []

    def test_restores_bootstrap_deleted_by_hook(self) -> None:
        """Deleting the frozen bootstrap is detected and undone."""
        frozen = sys.modules[_BOOTSTRAP]
        snapshot = snapshot_import_machinery()

        _poison_bootstrap()
        restored = restore_import_machinery(snapshot)

        assert restored == [_BOOTSTRAP]
        assert sys.modules[_BOOTSTRAP] is frozen

    def test_restores_parent_package_attribute(self) -> None:
        """The importlib._bootstrap attribute is restored, not just the sys.modules entry.

        Already-executed code reaches the bootstrap through the parent package attribute, so
        repairing only sys.modules would leave imports broken.
        """
        frozen = sys.modules[_BOOTSTRAP]
        snapshot = snapshot_import_machinery()

        _poison_bootstrap()
        restore_import_machinery(snapshot)

        assert importlib._bootstrap is frozen  # type: ignore[attr-defined]

    def test_imports_work_after_restore(self) -> None:
        """The repair leaves the interpreter able to import again."""
        snapshot = snapshot_import_machinery()

        _poison_bootstrap()
        restore_import_machinery(snapshot)

        assert importlib.import_module("json").__name__ == "json"

    def test_restore_is_idempotent(self) -> None:
        """A second restore of an already-repaired machinery reports no further damage."""
        snapshot = snapshot_import_machinery()
        _poison_bootstrap()
        restore_import_machinery(snapshot)

        assert restore_import_machinery(snapshot) == []


class TestFindUnfrozenImportModules:
    """Test find_unfrozen_import_modules."""

    def test_finds_nothing_in_healthy_interpreter(self) -> None:
        """A healthy interpreter has all frozen import modules intact."""
        assert find_unfrozen_import_modules() == []

    def test_finds_module_reexecuted_from_source(self) -> None:
        """A bootstrap whose spec origin is a filesystem path is reported."""
        _poison_bootstrap()

        assert find_unfrozen_import_modules() == [_BOOTSTRAP]
