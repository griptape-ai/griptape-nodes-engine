"""End-to-end tests for cluster execution (ExecuteClusterRequest).

A cluster is the dispatch unit for isolated execution: nodes joined by a serializable=False
value must run in one process, so they are sent as one request and hand that value off as a
live reference. These tests execute real clusters against a real engine using the
nonserializable_library fixture, whose ProducerNode emits a live ``Session`` object that
cannot cross a process boundary and whose ConsumerNode reads a string out of it.

The contract pinned here:

- An intra-cluster serializable=False edge delivers the live value to the consumer.
- Returned outputs are restricted to serializable parameters: the live Session never
  appears in the result, the string extracted from it does.
- Execution order follows edges, not request order.
- Malformed clusters (unknown edge targets, cycles, unknown node types) fail with a
  diagnostic naming the problem, and a node that raises fails the cluster naming the node.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from griptape_nodes.node_library.library_registry import LibrarySchema
from griptape_nodes.retained_mode.events.execution_events import (
    ClusterEdgeSpec,
    ClusterNodeSpec,
    ExecuteClusterRequest,
    ExecuteClusterResultFailure,
    ExecuteClusterResultSuccess,
)
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

FIXTURE_JSON = Path(__file__).parent / "fixtures" / "nonserializable_library" / "griptape_nodes_library.json"
LIBRARY = "NonSerializable Library"
EXPECTED_MARKER = "live-session-marker"


@pytest.fixture(autouse=True)
def _register_library(tmp_path: Path) -> None:
    """Register a copy of the fixture library patched to the current schema/engine versions."""
    from griptape_nodes.utils.version_utils import engine_version

    library_dir = tmp_path / "nonserializable_library"
    library_dir.mkdir()
    schema = json.loads(FIXTURE_JSON.read_text())
    schema["library_schema_version"] = LibrarySchema.LATEST_SCHEMA_VERSION
    schema["metadata"]["engine_version"] = engine_version
    library_json = library_dir / "griptape_nodes_library.json"
    library_json.write_text(json.dumps(schema, indent=2))
    shutil.copy(FIXTURE_JSON.parent / "nonserializable_nodes.py", library_dir / "nonserializable_nodes.py")
    result = GriptapeNodes.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(result, RegisterLibraryFromFileResultSuccess), getattr(result, "result_details", result)


def _producer(name: str = "producer") -> ClusterNodeSpec:
    return ClusterNodeSpec(node_name=name, node_type="ProducerNode", library_name=LIBRARY)


def _consumer(name: str = "consumer") -> ClusterNodeSpec:
    return ClusterNodeSpec(node_name=name, node_type="ConsumerNode", library_name=LIBRARY)


def _session_edge(source: str = "producer", target: str = "consumer") -> ClusterEdgeSpec:
    return ClusterEdgeSpec(
        source_node=source, source_parameter="session", target_node=target, target_parameter="session"
    )


class TestLiveValueHandoff:
    def test_live_value_crosses_the_intra_cluster_edge(self) -> None:
        """The consumer reads the producer's live Session and extracts its marker.

        This is the design's core claim: the serializable=False value flowed
        producer -> consumer without ever being serialized.
        """
        result = GriptapeNodes.handle_request(
            ExecuteClusterRequest(
                nodes=[_producer(), _consumer()],
                edges=[_session_edge()],
                output_nodes=["consumer"],
            )
        )

        assert isinstance(result, ExecuteClusterResultSuccess), getattr(result, "result_details", result)
        assert result.parameter_output_values["consumer"]["marker"] == EXPECTED_MARKER

    def test_unserializable_outputs_never_return_over_the_boundary(self) -> None:
        """The producer's live Session is not in the result even when requested."""
        result = GriptapeNodes.handle_request(
            ExecuteClusterRequest(
                nodes=[_producer(), _consumer()],
                edges=[_session_edge()],
                output_nodes=["producer", "consumer"],
            )
        )

        assert isinstance(result, ExecuteClusterResultSuccess)
        assert "session" not in result.parameter_output_values["producer"]
        assert result.parameter_output_values["consumer"] == {"marker": EXPECTED_MARKER}

    def test_execution_order_follows_edges_not_request_order(self) -> None:
        """Consumer listed first still runs after the producer that feeds it."""
        result = GriptapeNodes.handle_request(
            ExecuteClusterRequest(
                nodes=[_consumer(), _producer()],
                edges=[_session_edge()],
                output_nodes=["consumer"],
            )
        )

        assert isinstance(result, ExecuteClusterResultSuccess)
        assert result.parameter_output_values["consumer"]["marker"] == EXPECTED_MARKER

    def test_single_node_cluster_degenerates_to_per_node_execution(self) -> None:
        result = GriptapeNodes.handle_request(
            ExecuteClusterRequest(nodes=[_producer()], edges=[], output_nodes=["producer"])
        )

        assert isinstance(result, ExecuteClusterResultSuccess)
        # Its only output is unserializable, so nothing crosses back -- by design.
        assert result.parameter_output_values["producer"] == {}


class TestBoundedFailure:
    def test_node_that_raises_fails_the_cluster_naming_the_node(self) -> None:
        """A consumer with no incoming session raises inside process(); the failure says who."""
        result = GriptapeNodes.handle_request(
            ExecuteClusterRequest(nodes=[_consumer("lonely")], edges=[], output_nodes=["lonely"])
        )

        assert isinstance(result, ExecuteClusterResultFailure)
        assert result.failed_node == "lonely"

    def test_unknown_node_type_fails_with_diagnostic(self) -> None:
        result = GriptapeNodes.handle_request(
            ExecuteClusterRequest(
                nodes=[ClusterNodeSpec(node_name="ghost", node_type="NoSuchNode", library_name=LIBRARY)],
            )
        )

        assert isinstance(result, ExecuteClusterResultFailure)
        assert result.failed_node == "ghost"

    def test_edge_to_unknown_node_fails_validation(self) -> None:
        result = GriptapeNodes.handle_request(
            ExecuteClusterRequest(nodes=[_producer()], edges=[_session_edge("producer", "ghost")])
        )

        assert isinstance(result, ExecuteClusterResultFailure)

    def test_cycle_fails_validation(self) -> None:
        result = GriptapeNodes.handle_request(
            ExecuteClusterRequest(
                nodes=[_producer("a"), _consumer("b")],
                edges=[_session_edge("a", "b"), _session_edge("b", "a")],
            )
        )

        assert isinstance(result, ExecuteClusterResultFailure)

    def test_empty_cluster_fails_validation(self) -> None:
        result = GriptapeNodes.handle_request(ExecuteClusterRequest())

        assert isinstance(result, ExecuteClusterResultFailure)
