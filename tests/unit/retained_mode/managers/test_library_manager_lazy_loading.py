"""Tests for the opt-in lazy node-loading flag in LibraryManager.

Eager loading (the default) imports each node's module at load time, so a broken node
surfaces as a library problem immediately. Lazy loading registers node types with a
deferred loader and imports on first use, so a broken node is not reported until used.
"""

from __future__ import annotations

import importlib
import pickle
import sys
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from griptape_nodes.node_library.library_registry import (
    CategoryDefinition,
    LibraryMetadata,
    LibraryRegistry,
    LibrarySchema,
    NodeDefinition,
    NodeMetadata,
)
from griptape_nodes.retained_mode.events.library_events import (
    DescribeNodeTypeRequest,
    DescribeNodeTypeResultSuccess,
)
from griptape_nodes.retained_mode.managers.fitness_problems.libraries.node_module_import_problem import (
    NodeModuleImportProblem,
)
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager, loads_with_library_recovery
from griptape_nodes.retained_mode.managers.settings import LibrarySettings

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

_GOOD_NODE_SOURCE = """
from griptape_nodes.exe_types.node_types import BaseNode


class GoodNode(BaseNode):
    def process(self):
        return None
"""

_BROKEN_NODE_SOURCE = """
import definitely_not_a_real_module_zzz  # noqa: F401

from griptape_nodes.exe_types.node_types import BaseNode


class BrokenNode(BaseNode):
    def process(self):
        return None
"""

# Two node classes in one file, plus a top-level side effect that appends to a marker file on
# every module execution. Used to prove the module is imported exactly once even when its two
# classes are resolved separately.
_SIBLINGS_SOURCE = """
from pathlib import Path

from griptape_nodes.exe_types.node_types import BaseNode

Path({marker!r}).open("a").write("x")


class SiblingA(BaseNode):
    def process(self):
        return None


class SiblingB(BaseNode):
    def process(self):
        return None
"""

_NAMED_NODE_SOURCE = """
from griptape_nodes.exe_types.node_types import BaseNode


class {class_name}(BaseNode):
    def process(self):
        return None
"""


@pytest.fixture(autouse=True)
def _clear_registry() -> Iterator[None]:
    # LibraryRegistry._libraries is a ClassVar, so it survives the conftest singleton reset.
    # Clear it around each test so re-registering the same library name does not collide.
    LibraryRegistry._clear()
    yield
    LibraryRegistry._clear()


def _node_metadata(display_name: str) -> NodeMetadata:
    return NodeMetadata(category="Test", description="test node", display_name=display_name)


def _write_library(tmp_path: Path) -> LibrarySchema:
    (tmp_path / "good_node.py").write_text(_GOOD_NODE_SOURCE)
    (tmp_path / "broken_node.py").write_text(_BROKEN_NODE_SOURCE)
    return LibrarySchema(
        name="Lazy Flag Test Library",
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="test",
            description="test",
            library_version="1.0.0",
            engine_version="1.0.0",
            tags=[],
        ),
        categories=[{"Test": CategoryDefinition(title="Test", description="test", color="#000", icon="Folder")}],
        nodes=[
            NodeDefinition(class_name="GoodNode", file_path="good_node.py", metadata=_node_metadata("Good")),
            NodeDefinition(class_name="BrokenNode", file_path="broken_node.py", metadata=_node_metadata("Broken")),
        ],
    )


def _library_info(schema: LibrarySchema, tmp_path: Path) -> LibraryManager.LibraryInfo:
    return LibraryManager.LibraryInfo(
        lifecycle_state=LibraryManager.LibraryLifecycleState.METADATA_LOADED,
        library_path=str(tmp_path / "griptape_nodes_library.json"),
        is_sandbox=False,
        library_name=schema.name,
        library_version="1.0.0",
        fitness=LibraryManager.LibraryFitness.NOT_EVALUATED,
        problems=[],
    )


def _schema(name: str, nodes: list[NodeDefinition]) -> LibrarySchema:
    return LibrarySchema(
        name=name,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="test", description="test", library_version="1.0.0", engine_version="1.0.0", tags=[]
        ),
        categories=[{"Test": CategoryDefinition(title="Test", description="test", color="#000", icon="Folder")}],
        nodes=nodes,
    )


class TestLazyNodeLoadingDefault:
    def test_setting_defaults_to_lazy(self) -> None:
        assert LibrarySettings().lazy_node_loading is True


class TestEagerLoading:
    def test_broken_node_is_reported_at_load(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        manager = griptape_nodes.LibraryManager()
        schema = _write_library(tmp_path)
        library = LibraryRegistry.generate_new_library(library_data=schema)
        info = _library_info(schema, tmp_path)

        manager._attempt_load_nodes_from_library(
            library_data=schema, library=library, base_dir=tmp_path, library_info=info, lazy_loading=False
        )

        # The importable node registers; the broken one does not, and its failure is a problem now.
        assert library.has_node_type("GoodNode")
        assert not library.has_node_type("BrokenNode")
        assert any(isinstance(p, NodeModuleImportProblem) for p in info.problems)
        # Some nodes loaded, but with problems -> FLAWED.
        assert info.fitness is LibraryManager.LibraryFitness.FLAWED


class TestLazyLoading:
    def test_broken_node_is_not_reported_until_used(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        manager = griptape_nodes.LibraryManager()
        schema = _write_library(tmp_path)
        library = LibraryRegistry.generate_new_library(library_data=schema)
        info = _library_info(schema, tmp_path)

        manager._attempt_load_nodes_from_library(
            library_data=schema, library=library, base_dir=tmp_path, library_info=info, lazy_loading=True
        )

        # Both types register without importing; no import problem is recorded at load.
        assert library.has_node_type("GoodNode")
        assert library.has_node_type("BrokenNode")
        assert not any(isinstance(p, NodeModuleImportProblem) for p in info.problems)
        assert info.fitness is LibraryManager.LibraryFitness.GOOD

        # The good node imports on first use; the broken one raises only when first used.
        assert library.create_node(node_type="GoodNode", name="g") is not None
        with pytest.raises(ImportError):
            library.get_node_class("BrokenNode")


class TestShouldLazyLoadNodes:
    def test_worker_always_eager(self, griptape_nodes: GriptapeNodes) -> None:
        manager = griptape_nodes.LibraryManager()
        config_mgr = griptape_nodes.ConfigManager()
        # Even with the setting on, a worker loads eagerly.
        with (
            patch.object(manager, "_is_worker", True),
            patch.object(config_mgr, "get_config_value", return_value=True),
        ):
            assert manager._should_lazy_load_nodes() is False

    def test_orchestrator_honors_setting_enabled(self, griptape_nodes: GriptapeNodes) -> None:
        manager = griptape_nodes.LibraryManager()
        config_mgr = griptape_nodes.ConfigManager()
        with (
            patch.object(manager, "_is_worker", False),
            patch.object(config_mgr, "get_config_value", return_value=True),
        ):
            assert manager._should_lazy_load_nodes() is True

    def test_orchestrator_honors_setting_disabled(self, griptape_nodes: GriptapeNodes) -> None:
        manager = griptape_nodes.LibraryManager()
        config_mgr = griptape_nodes.ConfigManager()
        with (
            patch.object(manager, "_is_worker", False),
            patch.object(config_mgr, "get_config_value", return_value=False),
        ):
            assert manager._should_lazy_load_nodes() is False


class TestMultipleNodesPerFile:
    def test_sibling_classes_share_a_single_module_import(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        manager = griptape_nodes.LibraryManager()
        marker = tmp_path / "import_count.txt"
        (tmp_path / "siblings.py").write_text(_SIBLINGS_SOURCE.format(marker=str(marker)))
        schema = _schema(
            "Siblings Library",
            [
                NodeDefinition(class_name="SiblingA", file_path="siblings.py", metadata=_node_metadata("A")),
                NodeDefinition(class_name="SiblingB", file_path="siblings.py", metadata=_node_metadata("B")),
            ],
        )
        library = LibraryRegistry.generate_new_library(library_data=schema)
        info = _library_info(schema, tmp_path)

        manager._attempt_load_nodes_from_library(
            library_data=schema, library=library, base_dir=tmp_path, library_info=info, lazy_loading=True
        )

        # Registration imports nothing.
        assert not marker.exists()

        node_a = library.get_node_class("SiblingA")
        node_b = library.get_node_class("SiblingB")

        # The shared file's module is imported exactly once (its top-level code ran once),
        # even though the two classes resolved separately.
        assert marker.read_text() == "x"
        # Both classes come from the same module object, so identity/side effects stay consistent.
        assert sys.modules[node_a.__module__] is sys.modules[node_b.__module__]
        assert sys.modules[node_a.__module__].SiblingA is node_a


class TestDescribeNodeTypeWithLazyImportFailure:
    def test_describe_returns_metadata_only_when_lazy_import_fails(
        self, griptape_nodes: GriptapeNodes, tmp_path: Path
    ) -> None:
        manager = griptape_nodes.LibraryManager()
        schema = _write_library(tmp_path)
        library = LibraryRegistry.generate_new_library(library_data=schema)
        info = _library_info(schema, tmp_path)
        manager._attempt_load_nodes_from_library(
            library_data=schema, library=library, base_dir=tmp_path, library_info=info, lazy_loading=True
        )

        result = manager.describe_node_type_request(
            DescribeNodeTypeRequest(node_type="BrokenNode", library=schema.name)
        )

        # A broken lazy import yields a usable (metadata-only) description rather than a failure.
        assert isinstance(result, DescribeNodeTypeResultSuccess)
        assert result.parameters == []


class TestStableNamespaceImportUnderLazyLoading:
    """Lazily registered node files must be importable via their stable namespace.

    Saved workflows reference library classes through
    ``griptape_nodes.node_libraries.<lib>.<file>`` imports and pickled values. With lazy
    loading nothing is in ``sys.modules`` at registration time, so these imports resolve
    through the StableNamespaceImportFinder meta-path hook instead.
    """

    GOOD_STABLE_NAMESPACE = "griptape_nodes.node_libraries.lazy_flag_test_library.good_node"
    BROKEN_STABLE_NAMESPACE = "griptape_nodes.node_libraries.lazy_flag_test_library.broken_node"

    @pytest.fixture(autouse=True)
    def _clean_stable_modules(self) -> Iterator[None]:
        # Purge before as well as after: earlier tests in this file load modules under the
        # same library name (and therefore the same stable namespace) without cleaning up.
        self._purge_stable_namespace_modules()
        yield
        self._purge_stable_namespace_modules()

    @staticmethod
    def _purge_stable_namespace_modules() -> None:
        for module_name in list(sys.modules):
            if module_name == "griptape_nodes.node_libraries" or module_name.startswith(
                "griptape_nodes.node_libraries."
            ):
                del sys.modules[module_name]

    def _register_lazy_library(
        self, griptape_nodes: GriptapeNodes, tmp_path: Path
    ) -> tuple[LibraryManager, LibrarySchema]:
        manager = griptape_nodes.LibraryManager()
        schema = _write_library(tmp_path)
        library = LibraryRegistry.generate_new_library(library_data=schema)
        info = _library_info(schema, tmp_path)
        manager._attempt_load_nodes_from_library(
            library_data=schema, library=library, base_dir=tmp_path, library_info=info, lazy_loading=True
        )
        return manager, schema

    def test_stable_namespace_imports_before_any_class_resolution(
        self, griptape_nodes: GriptapeNodes, tmp_path: Path
    ) -> None:
        self._register_lazy_library(griptape_nodes, tmp_path)
        assert self.GOOD_STABLE_NAMESPACE not in sys.modules

        module = importlib.import_module(self.GOOD_STABLE_NAMESPACE)

        assert module.GoodNode is not None
        # A later node-class resolution reuses the module the import loaded (shared memoized
        # loader), so class identity is consistent between imports and node creation.
        library = LibraryRegistry.get_library("Lazy Flag Test Library")
        assert library.get_node_class("GoodNode") is module.GoodNode

    def test_unpickle_resolves_stable_namespace_reference(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        self._register_lazy_library(griptape_nodes, tmp_path)
        assert self.GOOD_STABLE_NAMESPACE not in sys.modules

        # A GLOBAL-opcode pickle referencing the stable namespace, as found inside saved
        # workflow parameter values. Unpickling must import the module on demand.
        payload = f"c{self.GOOD_STABLE_NAMESPACE}\nGoodNode\n.".encode()
        node_class = pickle.loads(payload)  # noqa: S301 - crafted in-test payload

        assert node_class.__name__ == "GoodNode"

    def test_parent_packages_resolve_as_namespace_packages(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        self._register_lazy_library(griptape_nodes, tmp_path)

        root_package = importlib.import_module("griptape_nodes.node_libraries")
        library_package = importlib.import_module("griptape_nodes.node_libraries.lazy_flag_test_library")

        assert root_package.__path__ is not None
        assert library_package.__path__ is not None

    def test_legacy_volatile_reference_recovers_before_any_import(
        self, griptape_nodes: GriptapeNodes, tmp_path: Path
    ) -> None:
        """A legacy volatile module reference must recover with nothing imported yet.

        Older engines recorded per-process module names like
        ``gtn_dynamic_module_good_node_py_<hash>``. Volatile recovery matches those against
        modules that are loaded, so under lazy loading it has to import the pending file whose
        name matches the recorded token before it can find anything.
        """
        self._register_lazy_library(griptape_nodes, tmp_path)
        assert self.GOOD_STABLE_NAMESPACE not in sys.modules

        payload = b"cgtn_dynamic_module_good_node_py_123456789\nGoodNode\n."
        node_class = loads_with_library_recovery(payload)

        assert node_class is LibraryRegistry.get_library("Lazy Flag Test Library").get_node_class("GoodNode")
        # Only the file named by the token is imported; unrelated node files stay lazy.
        assert self.BROKEN_STABLE_NAMESPACE not in sys.modules

    def test_broken_module_import_raises_import_error(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        self._register_lazy_library(griptape_nodes, tmp_path)

        with pytest.raises(ImportError):
            importlib.import_module(self.BROKEN_STABLE_NAMESPACE)

    def test_unregistered_library_is_no_longer_importable(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        manager, schema = self._register_lazy_library(griptape_nodes, tmp_path)

        manager._unregister_all_stable_module_aliases_for_library(schema.name)

        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(self.GOOD_STABLE_NAMESPACE)

    def test_loaded_module_import_is_not_reexecuted_by_class_resolution(
        self, griptape_nodes: GriptapeNodes, tmp_path: Path
    ) -> None:
        manager = griptape_nodes.LibraryManager()
        marker = tmp_path / "import_count.txt"
        (tmp_path / "siblings.py").write_text(_SIBLINGS_SOURCE.format(marker=str(marker)))
        schema = _schema(
            "Siblings Import Library",
            [
                NodeDefinition(class_name="SiblingA", file_path="siblings.py", metadata=_node_metadata("A")),
                NodeDefinition(class_name="SiblingB", file_path="siblings.py", metadata=_node_metadata("B")),
            ],
        )
        library = LibraryRegistry.generate_new_library(library_data=schema)
        info = _library_info(schema, tmp_path)
        manager._attempt_load_nodes_from_library(
            library_data=schema, library=library, base_dir=tmp_path, library_info=info, lazy_loading=True
        )

        module = importlib.import_module("griptape_nodes.node_libraries.siblings_import_library.siblings")
        node_a = library.get_node_class("SiblingA")
        node_b = library.get_node_class("SiblingB")

        # The import and both class resolutions share one module execution.
        assert marker.read_text() == "x"
        assert module.SiblingA is node_a
        assert module.SiblingB is node_b

    def test_same_stem_files_stay_separately_importable(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        """Two files in one library sharing a stem must both stay importable.

        Both map to the same base namespace, so registering the second must disambiguate it
        rather than take over the first's entry: a lost entry would leave a saved workflow
        unable to import that file at all until one of its node classes happened to be used.
        """
        manager = griptape_nodes.LibraryManager()
        for subdir, class_name in (("video", "CompareVideo"), ("traits", "CompareTrait")):
            (tmp_path / subdir).mkdir()
            (tmp_path / subdir / "compare.py").write_text(_NAMED_NODE_SOURCE.format(class_name=class_name))
        schema = _schema(
            "Same Stem Library",
            [
                NodeDefinition(
                    class_name="CompareVideo", file_path="video/compare.py", metadata=_node_metadata("Video")
                ),
                NodeDefinition(
                    class_name="CompareTrait", file_path="traits/compare.py", metadata=_node_metadata("Trait")
                ),
            ],
        )
        library = LibraryRegistry.generate_new_library(library_data=schema)
        info = _library_info(schema, tmp_path)
        manager._attempt_load_nodes_from_library(
            library_data=schema, library=library, base_dir=tmp_path, library_info=info, lazy_loading=True
        )

        base_namespace = "griptape_nodes.node_libraries.same_stem_library.compare"
        pending_namespaces = sorted(manager._pending_stable_modules)
        suffixed_namespaces = [name for name in pending_namespaces if name.startswith(f"{base_namespace}_")]

        # The first registration keeps the plain namespace; the second is suffixed, not dropped.
        assert base_namespace in pending_namespaces
        assert len(suffixed_namespaces) == 1

        # Importing either namespace loads its own file, under the name it was reserved with.
        base_module = importlib.import_module(base_namespace)
        suffixed_module = importlib.import_module(suffixed_namespaces[0])
        assert base_module.__name__ == base_namespace
        assert suffixed_module.__name__ == suffixed_namespaces[0]
        assert {base_module.CompareVideo, suffixed_module.CompareTrait} == {
            library.get_node_class("CompareVideo"),
            library.get_node_class("CompareTrait"),
        }

    def test_shared_pending_namespace_survives_until_last_claimant_unloads(
        self, griptape_nodes: GriptapeNodes, tmp_path: Path
    ) -> None:
        """A pending namespace claimed by two libraries must outlive the first unload.

        Two library names that sanitize identically and point at the same node file share one
        stable namespace. Dropping the namespace when the first of them unloads would leave the
        survivor unable to import it, which is the cold-import failure lazy registration exists
        to prevent.
        """
        manager = griptape_nodes.LibraryManager()
        (tmp_path / "shared.py").write_text(_NAMED_NODE_SOURCE.format(class_name="SharedNode"))
        node = NodeDefinition(class_name="SharedNode", file_path="shared.py", metadata=_node_metadata("Shared"))
        for library_name in ("Shared Pending Library", "Shared-Pending Library"):
            schema = _schema(library_name, [node])
            library = LibraryRegistry.generate_new_library(library_data=schema)
            manager._attempt_load_nodes_from_library(
                library_data=schema,
                library=library,
                base_dir=tmp_path,
                library_info=_library_info(schema, tmp_path),
                lazy_loading=True,
            )

        stable_namespace = "griptape_nodes.node_libraries.shared_pending_library.shared"
        assert stable_namespace in manager._pending_stable_modules

        manager._unregister_all_stable_module_aliases_for_library("Shared Pending Library")

        module = importlib.import_module(stable_namespace)
        assert module.SharedNode is LibraryRegistry.get_library("Shared-Pending Library").get_node_class("SharedNode")

    def test_legacy_volatile_reference_reports_missing_library_when_file_is_broken(
        self, griptape_nodes: GriptapeNodes, tmp_path: Path
    ) -> None:
        """An unimportable candidate file must not mask why a legacy reference failed.

        Volatile recovery imports pending files whose name matches the recorded token. When such
        a file no longer imports cleanly it is simply not a match, so the caller must still see
        the original "nothing provides this saved reference" failure rather than an unrelated
        import error from a file it never asked about.
        """
        self._register_lazy_library(griptape_nodes, tmp_path)

        payload = b"cgtn_dynamic_module_broken_node_py_123456789\nBrokenNode\n."
        with pytest.raises(ModuleNotFoundError) as excinfo:
            loads_with_library_recovery(payload)

        assert "gtn_dynamic_module_broken_node_py_123456789" in str(excinfo.value)
