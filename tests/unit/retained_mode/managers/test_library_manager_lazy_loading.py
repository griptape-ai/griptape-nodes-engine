"""Tests for the opt-in lazy node-loading flag in LibraryManager.

Eager loading (the default) imports each node's module at load time, so a broken node
surfaces as a library problem immediately. Lazy loading registers node types with a
deferred loader and imports on first use, so a broken node is not reported until used.
"""

from __future__ import annotations

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
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager
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
