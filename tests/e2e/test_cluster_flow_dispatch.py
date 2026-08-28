"""End-to-end test: a real flow run dispatches a heavy cluster as one request.

This is the keystone proof for venue execution. A library that declares execution
dependencies has two nodes joined by a serializable=False value (a live Session). Running
the flow through the REAL machinery (CreateFlow/CreateNode/CreateConnection/StartFlow, the
DAG builder, the parallel resolution machine) must:

- compute the two nodes into one heavy cluster,
- gate the cluster until its external inputs are resolved (trivially true here),
- dispatch it as exactly ONE ExecuteClusterRequest,
- hand the live Session across the intra-cluster edge without serialization,
- apply outputs back onto the orchestrator's real nodes, and
- complete the second member as a no-op so both end RESOLVED.

A light library running the same shape must dispatch ZERO cluster requests: the gate is
inert for everything that has no execution dependencies.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import griptape_nodes.retained_mode.managers.node_manager as node_manager_module
from griptape_nodes.exe_types.node_types import NodeResolutionState
from griptape_nodes.node_library.library_registry import LibrarySchema
from griptape_nodes.retained_mode.events.execution_events import (
    ExecuteClusterRequest,
    StartFlowRequest,
    StartFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.utils.version_utils import engine_version
from tests.e2e.offline_wheels import build_wheel, offline_install_flags

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "nonserializable_library"
EXPECTED_MARKER = "live-session-marker"


@pytest.fixture
def cluster_dispatches(monkeypatch: pytest.MonkeyPatch) -> list[ExecuteClusterRequest]:
    """Count ExecuteClusterRequest dispatches by wrapping the handler's core."""
    dispatched: list[ExecuteClusterRequest] = []
    original = node_manager_module.execute_cluster

    async def counting(request: ExecuteClusterRequest, event_manager: EventManager) -> ResultPayload:
        dispatched.append(request)
        return await original(request, event_manager)

    monkeypatch.setattr(node_manager_module, "execute_cluster", counting)
    return dispatched


def _register_library(tmp_path: Path, *, name: str, heavy: bool) -> None:
    """Register the producer/consumer fixture, optionally declaring execution deps."""
    library_dir = tmp_path / name.replace(" ", "_")
    library_dir.mkdir()
    schema = json.loads((FIXTURE_DIR / "griptape_nodes_library.json").read_text())
    schema["name"] = name
    schema["library_schema_version"] = LibrarySchema.LATEST_SCHEMA_VERSION
    schema["metadata"]["engine_version"] = engine_version
    if heavy:
        wheel_dir = tmp_path / "wheels"
        build_wheel(wheel_dir, "fakeexec", "2.0.0")
        schema["metadata"]["dependencies"] = {
            "pip_dependencies": [],
            "pip_dependencies_exec": ["fakeexec"],
            "pip_install_flags": offline_install_flags(wheel_dir),
        }
    library_json = library_dir / "griptape_nodes_library.json"
    library_json.write_text(json.dumps(schema, indent=2))
    shutil.copy(FIXTURE_DIR / "nonserializable_nodes.py", library_dir / "nonserializable_nodes.py")
    result = GriptapeNodes.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(result, RegisterLibraryFromFileResultSuccess), getattr(result, "result_details", result)


async def _run_producer_consumer_flow(
    library_name: str,
    create_node: Callable[..., str],
    connect: Callable[..., None],
) -> None:
    GriptapeNodes.ContextManager().push_workflow(workflow_name=f"wf_{library_name}")
    flow_result = GriptapeNodes.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ClusterFlow", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result

    create_node("ProducerNode", "Producer", flow_result.flow_name, library_name=library_name)
    create_node("ConsumerNode", "Consumer", flow_result.flow_name, library_name=library_name)
    connect("Producer", "session", "Consumer", "session")

    run_result = await GriptapeNodes.ahandle_request(
        StartFlowRequest(
            flow_name=flow_result.flow_name,
            flow_node_name="Consumer",
            wait_for_completion=True,
            completion_timeout_ms=30_000,
        )
    )
    assert isinstance(run_result, StartFlowResultSuccess), getattr(run_result, "result_details", run_result)


class TestHeavyClusterFlowDispatch:
    @pytest.mark.asyncio
    async def test_flow_run_dispatches_the_cluster_once_and_resolves_all_members(
        self,
        tmp_path: Path,
        create_node: Callable[..., str],
        connect: Callable[..., None],
        cluster_dispatches: list[ExecuteClusterRequest],
    ) -> None:
        _register_library(tmp_path, name="Heavy Cluster Library", heavy=True)

        await _run_producer_consumer_flow("Heavy Cluster Library", create_node, connect)

        assert len(cluster_dispatches) == 1, "the producer/consumer pair must dispatch as ONE cluster"
        node_manager = GriptapeNodes.NodeManager()
        producer = node_manager.get_node_by_name("Producer")
        consumer = node_manager.get_node_by_name("Consumer")
        assert producer.state == NodeResolutionState.RESOLVED
        assert consumer.state == NodeResolutionState.RESOLVED
        # The live value crossed inside the dispatch; the derived string landed back on
        # the orchestrator's real node.
        assert consumer.parameter_output_values.get("marker") == EXPECTED_MARKER

    @pytest.mark.asyncio
    async def test_light_library_never_dispatches_a_cluster(
        self,
        tmp_path: Path,
        create_node: Callable[..., str],
        connect: Callable[..., None],
        cluster_dispatches: list[ExecuteClusterRequest],
    ) -> None:
        """The same graph shape without execution deps runs exactly as it always has."""
        _register_library(tmp_path, name="Light Cluster Library", heavy=False)

        await _run_producer_consumer_flow("Light Cluster Library", create_node, connect)

        assert len(cluster_dispatches) == 0
        consumer = GriptapeNodes.NodeManager().get_node_by_name("Consumer")
        assert consumer.parameter_output_values.get("marker") == EXPECTED_MARKER
