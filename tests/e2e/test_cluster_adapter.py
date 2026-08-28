"""End-to-end tests for deriving execution clusters from real nodes and libraries.

The pure cluster computation takes primitive inputs; this adapter layer produces them from
live engine state: execution-dependency sets read from each node's registered library, and
edge serializability read from the producing node's real Parameter metadata. These tests
register the actual fixture libraries and instantiate actual nodes, so what is proven is
the full derivation path a dispatching executor will use.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from griptape_nodes.common.execution_clusters import (
    NodeGraphEdge,
    clusters_for_nodes,
    exec_dependencies_for_library,
)
from griptape_nodes.node_library.library_registry import LibraryRegistry, LibrarySchema
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.utils.version_utils import engine_version
from tests.e2e.offline_wheels import build_wheel, offline_install_flags

FIXTURES = Path(__file__).parent / "fixtures"
NONSERIALIZABLE_LIBRARY = "NonSerializable Library"
EXEC_DEP_LIBRARY = "Exec Dep Library"


def _register_fixture_library(tmp_path: Path, fixture_dir: str, node_files: list[str], **dep_overrides) -> None:
    """Register a copy of a fixture library patched to current versions."""
    source = FIXTURES / fixture_dir
    library_dir = tmp_path / fixture_dir
    library_dir.mkdir()
    schema = json.loads((source / "griptape_nodes_library.json").read_text())
    schema["library_schema_version"] = LibrarySchema.LATEST_SCHEMA_VERSION
    schema["metadata"]["engine_version"] = engine_version
    if dep_overrides:
        schema["metadata"]["dependencies"] = dep_overrides
    library_json = library_dir / "griptape_nodes_library.json"
    library_json.write_text(json.dumps(schema, indent=2))
    for node_file in node_files:
        shutil.copy(source / node_file, library_dir / node_file)
    result = GriptapeNodes.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(result, RegisterLibraryFromFileResultSuccess), getattr(result, "result_details", result)


@pytest.fixture(autouse=True)
def _register_libraries(tmp_path: Path) -> None:
    _register_fixture_library(tmp_path, "nonserializable_library", ["nonserializable_nodes.py"])
    wheel_dir = tmp_path / "wheels"
    build_wheel(wheel_dir, "fakeedit", "1.0.0")
    build_wheel(wheel_dir, "fakeexec", "2.0.0")
    _register_fixture_library(
        tmp_path,
        "exec_dep_library",
        ["exec_dep_node.py"],
        pip_dependencies=["fakeedit"],
        pip_dependencies_exec=["fakeexec"],
        pip_install_flags=offline_install_flags(wheel_dir),
    )


class TestExecDependencyDerivation:
    def test_library_with_exec_deps_reports_them(self) -> None:
        assert exec_dependencies_for_library(EXEC_DEP_LIBRARY) == frozenset({"fakeexec"})

    def test_library_without_exec_deps_reports_empty(self) -> None:
        assert exec_dependencies_for_library(NONSERIALIZABLE_LIBRARY) == frozenset()


class TestClustersFromLiveNodes:
    def test_live_edge_serializability_and_library_deps_drive_clustering(self) -> None:
        """Real parameter metadata and real library metadata drive the clustering.

        A real ProducerNode's serializable=False output pins it to its consumer, and a
        real ExecDepNode's cluster carries its library's execution dependencies.
        """
        producer = LibraryRegistry.create_node(
            node_type="ProducerNode", name="producer", specific_library_name=NONSERIALIZABLE_LIBRARY
        )
        consumer = LibraryRegistry.create_node(
            node_type="ConsumerNode", name="consumer", specific_library_name=NONSERIALIZABLE_LIBRARY
        )
        heavy = LibraryRegistry.create_node(
            node_type="ExecDepNode", name="heavy", specific_library_name=EXEC_DEP_LIBRARY
        )

        clusters = clusters_for_nodes(
            [producer, consumer, heavy],
            [
                # ProducerNode.session is serializable=False in the real Parameter metadata.
                NodeGraphEdge("producer", "session", "consumer", "session"),
                # ConsumerNode.marker is a plain string: a serializable boundary.
                NodeGraphEdge("consumer", "marker", "heavy", "edit_dep_version"),
            ],
        )

        by_membership = {cluster.node_names: cluster for cluster in clusters}
        assert frozenset({"producer", "consumer"}) in by_membership
        assert frozenset({"heavy"}) in by_membership
        assert by_membership[frozenset({"producer", "consumer"})].runs_in_orchestrator
        heavy_cluster = by_membership[frozenset({"heavy"})]
        assert heavy_cluster.exec_dependencies == frozenset({"fakeexec"})
        assert not heavy_cluster.runs_in_orchestrator

    def test_unknown_edge_source_raises(self) -> None:
        node = LibraryRegistry.create_node(
            node_type="ProducerNode", name="only", specific_library_name=NONSERIALIZABLE_LIBRARY
        )
        with pytest.raises(ValueError, match="is not among the given nodes"):
            clusters_for_nodes([node], [NodeGraphEdge("ghost", "x", "only", "session")])
