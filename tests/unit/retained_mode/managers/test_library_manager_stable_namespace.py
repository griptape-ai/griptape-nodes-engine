"""Tests for loading library node modules under a stable, deterministic namespace.

Library node files are executed under a stable module name
(``griptape_nodes.node_libraries.<lib>.<file>``) rather than a volatile per-process name
derived from ``hash(str(path))``. This is what makes the ``__module__`` pickle records
reproducible across engine restarts, so pickled parameter values (including objects
embedded in saved image metadata) unpickle reliably.

Regression: dragging an image onto the canvas failed with
``No module named 'gtn_dynamic_module_..._<hash>'`` because the volatile module name in the
embedded pickle no longer existed in a fresh process (Python randomizes ``hash()`` of
strings per process).
"""

from __future__ import annotations

import gc
import importlib
import pickle
import sys
import weakref
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.retained_mode.managers.library_manager import (
    AmbiguousLegacyModuleError,
    LibraryManager,
    StableNamespaceCollisionError,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from types import ModuleType

    from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

_MODULE_SOURCE = """
from enum import StrEnum


class Behavior(StrEnum):
    OVERWRITE = "Overwrite existing"
    PRESERVE = "Preserve existing"


class Widget:
    def __init__(self, count: int) -> None:
        self.count = count
"""

_NESTED_MODULE_SOURCE = """
from enum import StrEnum


class Node:
    class Behavior(StrEnum):
        OVERWRITE = "Overwrite existing"
        PRESERVE = "Preserve existing"
"""


@pytest.fixture
def restore_sys_modules() -> object:
    """Remove any modules registered under the stable prefix during a test."""
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name.startswith(LibraryManager.STABLE_NAMESPACE_PREFIX):
            sys.modules.pop(name, None)


def _write_module(path: Path, name: str) -> Path:
    file_path = path / name
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(_MODULE_SOURCE)
    return file_path


@pytest.mark.usefixtures("restore_sys_modules")
class TestStableNamespaceLoading:
    def test_module_loads_under_stable_namespace(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        """The module and every class it defines carries the stable namespace as __module__."""
        manager = griptape_nodes.LibraryManager()
        file_path = _write_module(tmp_path, "collision_behavior.py")

        module = manager._load_module_from_file(file_path, "My Test Library")

        expected = "griptape_nodes.node_libraries.my_test_library.collision_behavior"
        assert module.__name__ == expected
        assert module.Behavior.__module__ == expected
        assert module.Widget.__module__ == expected
        assert sys.modules[expected] is module

    def test_stable_namespace_is_hash_independent(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        """The module name is a pure function of library + file, with no volatile hash suffix."""
        manager = griptape_nodes.LibraryManager()
        file_path = _write_module(tmp_path, "collision_behavior.py")

        module = manager._load_module_from_file(file_path, "My Test Library")

        # The registered name must equal the deterministic stable namespace exactly, so it
        # cannot embed a process-randomized hash the way the old dynamic name did.
        assert module.__name__ == manager._create_stable_namespace("My Test Library", file_path)
        assert "gtn_dynamic_module" not in module.__name__

    def test_parent_packages_are_registered(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        """The synthetic parent packages exist so the dotted stable name is importable."""
        manager = griptape_nodes.LibraryManager()
        file_path = _write_module(tmp_path, "collision_behavior.py")

        manager._load_module_from_file(file_path, "My Test Library")

        assert "griptape_nodes.node_libraries" in sys.modules
        assert "griptape_nodes.node_libraries.my_test_library" in sys.modules
        # The leaf is wired onto its parent so attribute-based import navigation resolves.
        parent = sys.modules["griptape_nodes.node_libraries.my_test_library"]
        assert hasattr(parent, "collision_behavior")

    def test_pickled_value_embeds_stable_namespace(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        """Pickled instances reference the stable namespace, not a volatile dynamic name."""
        manager = griptape_nodes.LibraryManager()
        file_path = _write_module(tmp_path, "collision_behavior.py")
        module = manager._load_module_from_file(file_path, "My Test Library")

        data = pickle.dumps(module.Behavior.OVERWRITE)

        assert b"griptape_nodes.node_libraries.my_test_library.collision_behavior" in data
        assert b"gtn_dynamic_module" not in data

    def test_pickle_survives_module_reload(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        """A value pickled before a reload unpickles after it: the drag-image-after-restart case.

        Reloading the file produces a fresh module object under the same stable name, standing
        in for a fresh engine process. Because the name is stable, pickle resolves the class.
        """
        manager = griptape_nodes.LibraryManager()
        file_path = _write_module(tmp_path, "collision_behavior.py")
        module = manager._load_module_from_file(file_path, "My Test Library")

        enum_bytes = pickle.dumps(module.Behavior.OVERWRITE)
        widget_count = 7
        widget_bytes = pickle.dumps(module.Widget(widget_count))

        # Simulate a fresh process losing the in-memory module, then the library reloading.
        manager._unregister_all_stable_module_aliases_for_library("My Test Library")
        assert "griptape_nodes.node_libraries.my_test_library.collision_behavior" not in sys.modules
        reloaded = manager._load_module_from_file(file_path, "My Test Library")

        restored_enum = pickle.loads(enum_bytes)  # noqa: S301
        restored_widget = pickle.loads(widget_bytes)  # noqa: S301

        assert restored_enum == "Overwrite existing"
        assert restored_enum is reloaded.Behavior.OVERWRITE
        assert restored_widget.count == widget_count

    def test_same_stem_collision_raises(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        """A second, different file sharing a stem with an already-loaded file raises."""
        manager = griptape_nodes.LibraryManager()
        first = _write_module(tmp_path / "video", "compare.py")
        second = _write_module(tmp_path / "traits", "compare.py")

        module_a = manager._load_module_from_file(first, "My Test Library")

        with pytest.raises(StableNamespaceCollisionError) as exc_info:
            manager._load_module_from_file(second, "My Test Library")

        assert sys.modules[module_a.__name__] is module_a
        assert str(first) in str(exc_info.value)
        assert str(second) in str(exc_info.value)

    def test_rejected_claim_leaves_no_partial_state(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        """A rejected claim registers nothing: no leaf module, no tracking entry, no reservation."""
        manager = griptape_nodes.LibraryManager()
        first = _write_module(tmp_path / "video", "compare.py")
        second = _write_module(tmp_path / "traits", "compare.py")

        module_a = manager._load_module_from_file(first, "My Test Library")
        namespace = module_a.__name__

        with pytest.raises(StableNamespaceCollisionError):
            manager._load_module_from_file(second, "My Test Library")

        leaf_prefix = "griptape_nodes.node_libraries.my_test_library."
        assert [name for name in sys.modules if name.startswith(leaf_prefix)] == [namespace]
        assert list(manager._stable_module_to_file) == [namespace]
        assert manager._library_to_stable_modules["My Test Library"] == {namespace}
        assert manager._pending_stable_modules == {}

    def test_rejected_reservation_keeps_the_first_file_importable(
        self, griptape_nodes: GriptapeNodes, tmp_path: Path
    ) -> None:
        """A rejected lazy reservation leaves the winner's reservation intact and still importable."""
        manager = griptape_nodes.LibraryManager()
        first = _write_module(tmp_path / "video", "compare.py")
        second = _write_module(tmp_path / "traits", "compare.py")
        module_loaders: dict[Path, Callable[[], ModuleType]] = {}

        first_loader = manager._get_or_create_module_loader(first, "My Test Library", module_loaders)
        manager._register_pending_stable_module_loader("My Test Library", first, first_loader)
        reserved_namespace = next(iter(manager._pending_stable_modules))

        second_loader = manager._get_or_create_module_loader(second, "My Test Library", module_loaders)
        with pytest.raises(StableNamespaceCollisionError):
            manager._register_pending_stable_module_loader("My Test Library", second, second_loader)

        assert list(manager._pending_stable_modules) == [reserved_namespace]
        assert list(manager._pending_stable_modules[reserved_namespace].loaders_by_library) == ["My Test Library"]
        assert manager._stable_module_to_file == {}

        # The surviving reservation still resolves, and to the file that claimed it.
        imported = importlib.import_module(reserved_namespace)
        assert imported.__file__ is not None
        assert first.samefile(imported.__file__)

    def test_hot_reload_keeps_same_namespace(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        """Reloading the same file reuses its namespace and replaces the module object."""
        manager = griptape_nodes.LibraryManager()
        file_path = _write_module(tmp_path, "collision_behavior.py")

        first = manager._load_module_from_file(file_path, "My Test Library")
        second = manager._load_module_from_file(file_path, "My Test Library")

        assert first.__name__ == second.__name__
        assert first is not second
        assert sys.modules[second.__name__] is second

    def test_hot_reload_preserves_live_instances_and_releases_old_generation(
        self, griptape_nodes: GriptapeNodes, tmp_path: Path
    ) -> None:
        """Old classes remain alive only while their instances still reference them."""
        manager = griptape_nodes.LibraryManager()
        file_path = _write_module(tmp_path, "collision_behavior.py")

        first = manager._load_module_from_file(file_path, "My Test Library")
        old_module_ref = weakref.ref(first)
        old_class = first.Widget
        old_class_ref = weakref.ref(old_class)
        widget_count = 7
        old_instance = old_class(widget_count)

        second = manager._load_module_from_file(file_path, "My Test Library")
        del first
        del old_class
        gc.collect()

        # Replacing sys.modules and the parent-package attribute releases the old module.
        # A live instance intentionally keeps its old class generation alive and usable.
        assert old_module_ref() is None
        assert old_class_ref() is not None
        assert old_instance.count == widget_count
        assert not isinstance(old_instance, second.Widget)

        del old_instance
        gc.collect()

        assert old_class_ref() is None

    def test_unload_removes_stable_modules(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        """Unloading a library tears its stable modules out of sys.modules and its parent."""
        manager = griptape_nodes.LibraryManager()
        file_path = _write_module(tmp_path, "collision_behavior.py")
        module = manager._load_module_from_file(file_path, "My Test Library")
        name = module.__name__

        manager._unregister_all_stable_module_aliases_for_library("My Test Library")

        assert name not in sys.modules
        parent = sys.modules.get("griptape_nodes.node_libraries.my_test_library")
        assert parent is None or not hasattr(parent, "collision_behavior")

    def test_is_dynamic_module_and_stable_namespace_lookup(self, griptape_nodes: GriptapeNodes) -> None:
        """The stable-namespace helpers key off the namespace prefix."""
        manager = griptape_nodes.LibraryManager()
        stable = "griptape_nodes.node_libraries.my_test_library.collision_behavior"

        assert manager.is_dynamic_module(stable) is True
        assert manager.is_dynamic_module("griptape.artifacts") is False
        assert manager.get_stable_namespace_for_dynamic_module(stable) == stable
        assert manager.get_stable_namespace_for_dynamic_module("griptape.artifacts") is None

    @pytest.mark.skipif(sys.platform == "win32", reason="symlink creation requires privileges on Windows")
    def test_symlinked_file_keeps_its_logical_name(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        """A node file loaded through a differently named symlink keeps the symlink's name.

        The namespace is part of every pickled reference, so it must follow the name the
        file was loaded by (what earlier engine versions recorded), not the symlink target.
        """
        manager = griptape_nodes.LibraryManager()
        target = _write_module(tmp_path, "actual_behavior.py")
        alias = tmp_path / "alias_behavior.py"
        alias.symlink_to(target)

        module = manager._load_module_from_file(alias, "My Test Library")

        expected = "griptape_nodes.node_libraries.my_test_library.alias_behavior"
        assert module.__name__ == expected
        # Legacy volatile tokens were built from the loaded (symlink) name too.
        resolved = manager.resolve_volatile_dynamic_class("gtn_dynamic_module_alias_behavior_py_42", "Behavior")
        assert resolved is module.Behavior

    def test_shared_namespace_survives_until_last_owner_unloads(
        self, griptape_nodes: GriptapeNodes, tmp_path: Path
    ) -> None:
        """Two libraries resolving to the same module must not unload it out from under each other.

        'My Test Library' and 'My-Test Library' sanitize to the same namespace segment; when
        both point at the same file, they share one stable module. The second library must
        reuse the first's module object (re-executing would strand the first library's
        registry on a stale class generation), unloading the first library must keep the
        module importable for the second, and only the final unload removes it.
        """
        manager = griptape_nodes.LibraryManager()
        file_path = _write_module(tmp_path, "collision_behavior.py")

        module_first = manager._load_module_from_file(file_path, "My Test Library")
        module_second = manager._load_module_from_file(file_path, "My-Test Library")
        assert module_second is module_first, "a co-owning library must reuse the loaded module, not re-execute it"
        namespace = module_first.__name__
        pickled = pickle.dumps(module_first.Behavior.OVERWRITE)

        manager._unregister_all_stable_module_aliases_for_library("My-Test Library")

        assert namespace in sys.modules, "the module must survive while another library still owns it"
        assert sys.modules[namespace] is module_first, "the surviving owner must keep its exact module generation"
        assert pickle.loads(pickled) is module_first.Behavior.OVERWRITE  # noqa: S301
        assert manager.resolve_volatile_dynamic_class("gtn_dynamic_module_collision_behavior_py_42", "Behavior") is (
            module_first.Behavior
        )

        manager._unregister_all_stable_module_aliases_for_library("My Test Library")

        assert namespace not in sys.modules, "the final owner's unload must remove the module"


@pytest.mark.usefixtures("restore_sys_modules")
class TestVolatileDynamicModuleResolution:
    """Mapping old volatile pickle module names back to classes in loaded stable modules."""

    def test_resolves_to_class_in_loaded_stable_module(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        manager = griptape_nodes.LibraryManager()
        file_path = _write_module(tmp_path, "collision_behavior.py")
        module = manager._load_module_from_file(file_path, "My Test Library")

        resolved = manager.resolve_volatile_dynamic_class(
            "gtn_dynamic_module_collision_behavior_py_-8859640815979518826", "Behavior"
        )

        assert resolved is module.Behavior

    def test_returns_none_for_non_volatile_name(self, griptape_nodes: GriptapeNodes) -> None:
        manager = griptape_nodes.LibraryManager()

        assert manager.resolve_volatile_dynamic_class("griptape.artifacts", "TextArtifact") is None

    def test_returns_none_when_class_missing(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        manager = griptape_nodes.LibraryManager()
        file_path = _write_module(tmp_path, "collision_behavior.py")
        manager._load_module_from_file(file_path, "My Test Library")

        resolved = manager.resolve_volatile_dynamic_class("gtn_dynamic_module_collision_behavior_py_123", "NoSuchClass")

        assert resolved is None

    def test_returns_none_when_no_module_loaded(self, griptape_nodes: GriptapeNodes) -> None:
        manager = griptape_nodes.LibraryManager()

        resolved = manager.resolve_volatile_dynamic_class("gtn_dynamic_module_never_loaded_py_42", "Behavior")

        assert resolved is None

    def test_raises_when_multiple_libraries_define_the_legacy_class(
        self, griptape_nodes: GriptapeNodes, tmp_path: Path
    ) -> None:
        manager = griptape_nodes.LibraryManager()
        first_file = _write_module(tmp_path / "first", "collision_behavior.py")
        second_file = _write_module(tmp_path / "second", "collision_behavior.py")
        first = manager._load_module_from_file(first_file, "First Library")
        second = manager._load_module_from_file(second_file, "Second Library")

        with pytest.raises(AmbiguousLegacyModuleError) as exc_info:
            manager.resolve_volatile_dynamic_class("gtn_dynamic_module_collision_behavior_py_42", "Behavior")

        assert exc_info.value.candidate_modules == (first.__name__, second.__name__)

    def test_resolves_nested_class_via_dotted_qualified_name(
        self, griptape_nodes: GriptapeNodes, tmp_path: Path
    ) -> None:
        """Pickle protocol 4+ records nested classes as dotted names like 'Node.Behavior'."""
        manager = griptape_nodes.LibraryManager()
        file_path = tmp_path / "nested_behavior.py"
        file_path.write_text(_NESTED_MODULE_SOURCE)
        module = manager._load_module_from_file(file_path, "My Test Library")

        resolved = manager.resolve_volatile_dynamic_class("gtn_dynamic_module_nested_behavior_py_42", "Node.Behavior")

        assert resolved is module.Node.Behavior

    def test_resolves_hyphenated_file_name(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        """Volatile names kept hyphens ('.'->'_' only); the reconstructed token must too."""
        manager = griptape_nodes.LibraryManager()
        file_path = _write_module(tmp_path, "collision-behavior.py")
        module = manager._load_module_from_file(file_path, "My Test Library")

        resolved = manager.resolve_volatile_dynamic_class("gtn_dynamic_module_collision-behavior_py_99", "Behavior")

        assert resolved is module.Behavior

    def test_resolves_dotted_file_name(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        """Volatile names replaced every '.' in the file name, not just the '.py' suffix."""
        manager = griptape_nodes.LibraryManager()
        file_path = _write_module(tmp_path, "collision.behavior.py")
        module = manager._load_module_from_file(file_path, "My Test Library")

        resolved = manager.resolve_volatile_dynamic_class("gtn_dynamic_module_collision_behavior_py_99", "Behavior")

        assert resolved is module.Behavior

    def test_hyphen_and_underscore_files_stay_distinct(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        """'foo-bar.py' and 'foo_bar.py' had distinct volatile tokens; both must resolve exactly.

        Both files sanitize to the same stable stem ('collision_behavior'), so each is loaded
        into a library whose name does not sanitize the same as the other's, keeping their
        stable namespaces distinct. Their legacy tokens differ by the hyphen, so neither lookup
        may be ambiguous.
        """
        manager = griptape_nodes.LibraryManager()
        hyphen_file = _write_module(tmp_path / "first", "collision-behavior.py")
        underscore_file = _write_module(tmp_path / "second", "collision_behavior.py")
        hyphen_module = manager._load_module_from_file(hyphen_file, "Hyphen Library")
        underscore_module = manager._load_module_from_file(underscore_file, "Underscore Library")

        resolved_hyphen = manager.resolve_volatile_dynamic_class(
            "gtn_dynamic_module_collision-behavior_py_1", "Behavior"
        )
        resolved_underscore = manager.resolve_volatile_dynamic_class(
            "gtn_dynamic_module_collision_behavior_py_2", "Behavior"
        )

        assert resolved_hyphen is hyphen_module.Behavior
        assert resolved_underscore is underscore_module.Behavior


class TestModuleDisplayName:
    @pytest.mark.parametrize(
        ("module_name", "expected"),
        [
            ("griptape_nodes.node_libraries", "a node library"),
            ("griptape_nodes.node_libraries.my_library.node_file", "my_library.node_file"),
            ("gtn_dynamic_module_node_file_py_-123", "node_file"),
            ("griptape.artifacts", "griptape.artifacts"),
        ],
    )
    def test_returns_artist_readable_name(self, griptape_nodes: GriptapeNodes, module_name: str, expected: str) -> None:
        manager = griptape_nodes.LibraryManager()

        assert manager.get_module_display_name(module_name) == expected
