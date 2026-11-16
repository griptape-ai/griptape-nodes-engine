"""A Workflow node whose library runs in a worker still runs in the orchestrator.

A Workflow node reads the flow it has just imported, and that flow only exists in the
orchestrator's object space. The nodes inside its subflow are routed to their own
libraries' workers as usual. The worker here is the simulated worker from
``tests.simulated_worker``, which has an engine of its own.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest

from griptape_nodes.exe_types.workflow_node import WorkflowNode
from griptape_nodes.node_library.workflow_registry import WorkflowMetadata, WorkflowShape
from griptape_nodes.retained_mode.events import converters
from griptape_nodes.retained_mode.events.connection_events import CreateConnectionRequest
from griptape_nodes.retained_mode.events.execution_events import (
    ExecuteNodeResultFailure,
    ExecuteNodeResultSuccess,
    StartFlowRequest,
    StartFlowResultFailure,
    StartFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    SerializeFlowToCommandsRequest,
    SerializeFlowToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from griptape_nodes.retained_mode.events.object_events import (
    ClearAllObjectStateRequest,
    ClearAllObjectStateResultSuccess,
)
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest
from griptape_nodes.utils.version_utils import engine_version
from tests.simulated_worker import SimulatedWorker, host_libraries_on_simulated_worker, simulated_worker_engine

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import EventRequest, ResultPayload

pytestmark = pytest.mark.timeout(300, method="thread")

FIXTURES_DIR = Path(__file__).parent / "fixtures"
WORKFLOW_LIBRARY_DIR = FIXTURES_DIR / "workflow_node_library"
WORKFLOW_LIBRARY_JSON_TEMPLATE = WORKFLOW_LIBRARY_DIR / "griptape_nodes_library.json"
WORKFLOW_NODE_FILE = WORKFLOW_LIBRARY_DIR / "workflow_node_nodes.py"
SHOUT_WORKFLOW_FILE = WORKFLOW_LIBRARY_DIR / "shout_workflow.py"
WORKFLOW_LIBRARY_NAME = "Workflow Node Library"
LEAF_LIBRARY_DIR = FIXTURES_DIR / "worker_leaf_library"
LEAF_LIBRARY_JSON_TEMPLATE = LEAF_LIBRARY_DIR / "griptape_nodes_library.json"
LEAF_NODE_FILE = LEAF_LIBRARY_DIR / "worker_leaf_nodes.py"
LEAF_LIBRARY_NAME = "Worker Leaf Library"
FAILING_HOST_LIBRARY_NAME = "Failing Workflow Host Library"
FAILING_WORKFLOW_NAME = "failing_workflow"
FAILING_NODE_TYPE = "FailingWorkflow"
LEAF_FAILURE_MESSAGE = "The fixture node refused to run"


class _WorkflowNodeInWorkerBase:
    """Fixtures that register the libraries and build the simulated worker."""

    @pytest.fixture
    def node_results(self) -> list[ResultPayload]:
        """The node results that came back from the worker, in order."""
        return []

    @pytest.fixture
    def worker(
        self,
        tmp_path: Path,
        node_results: list[ResultPayload],
        engine: Engine,
        materialize_library: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> Iterator[SimulatedWorker]:
        """Register the libraries and return a simulated worker that hosts nothing yet."""
        leaf_json = materialize_library(
            tmp_path / "leaf_library", template=LEAF_LIBRARY_JSON_TEMPLATE, node_file=LEAF_NODE_FILE
        )
        workflow_json = materialize_library(
            tmp_path / "workflow_library",
            template=WORKFLOW_LIBRARY_JSON_TEMPLATE,
            node_file=WORKFLOW_NODE_FILE,
            extra_files=[SHOUT_WORKFLOW_FILE],
        )
        for library_json in (leaf_json, workflow_json):
            result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
            assert isinstance(result, RegisterLibraryFromFileResultSuccess), result

        host_dir = tmp_path / "failing_host_library"
        self._write_failing_workflow(engine, host_dir)
        host_json = self._write_failing_host_library(host_dir)
        result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(host_json)))
        assert isinstance(result, RegisterLibraryFromFileResultSuccess), result

        with simulated_worker_engine(engine) as simulated:
            monkeypatch.setattr(
                engine.worker_manager,
                "wait_until_executable",
                AsyncMock(spec=engine.worker_manager.wait_until_executable),
            )
            monkeypatch.setattr(
                engine.worker_manager, "route_to_worker", self._recording_results(simulated, node_results)
            )
            yield simulated

    def failed_node_results(self, node_results: list[ResultPayload]) -> list[ExecuteNodeResultFailure]:
        """Return the node results that came back from the worker as failures."""
        return [result for result in node_results if isinstance(result, ExecuteNodeResultFailure)]

    def new_parent_flow(self, engine: Engine) -> str:
        """Create the flow the test's Workflow node lives in."""
        engine.context_manager.push_workflow(workflow_name="workflow_node_in_worker_parent")
        result = engine.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="ParentFlow", set_as_new_context=False)
        )
        assert isinstance(result, CreateFlowResultSuccess), result
        return result.flow_name

    def crossed_node_names(self, worker: SimulatedWorker) -> list[str]:
        """Return the names of every node that was sent to the worker."""
        return [request.node_name for request in worker.received]

    def assert_only_inner_nodes_crossed(self, engine: Engine, worker: SimulatedWorker, workflow_node_name: str) -> None:
        """Assert the Workflow node `workflow_node_name` did not cross, and that nodes from inside its subflow did."""
        workflow_node = engine.node_manager.get_node_by_name(workflow_node_name)
        assert isinstance(workflow_node, WorkflowNode), workflow_node
        workflow_node_type = workflow_node.metadata["node_type"]
        crossed_types = [(r.node_metadata or {}).get("node_type") for r in worker.received]
        assert all(crossed_types), worker.received
        assert workflow_node_type not in crossed_types, worker.received
        assert workflow_node_name not in self.crossed_node_names(worker), worker.received
        assert worker.received

    def _recording_results(
        self, simulated: SimulatedWorker, node_results: list[ResultPayload]
    ) -> Callable[..., Awaitable[dict[str, Any]]]:
        """Wrap `simulated.route_to_worker` so each node result it returns is noted in `node_results`."""

        async def route_to_worker(event_request: EventRequest, *worker_id: Any) -> dict[str, Any]:
            raw = await simulated.route_to_worker(event_request, *worker_id)
            result_type = (
                ExecuteNodeResultSuccess
                if raw["result_type"] == "ExecuteNodeResultSuccess"
                else ExecuteNodeResultFailure
            )
            node_results.append(converters.engine.structure(raw["result"], result_type))
            return raw

        return route_to_worker

    def _write_failing_workflow(self, engine: Engine, host_dir: Path) -> None:
        """Save a Start -> failing leaf node -> End workflow into `host_dir`.

        Built at runtime rather than committed, so this test does not depend on the generated fixture files.
        """
        host_dir.mkdir(parents=True)
        engine.context_manager.push_workflow(workflow_name=FAILING_WORKFLOW_NAME)
        flow_result = engine.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="ControlFlow_1", set_as_new_context=False)
        )
        assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
        flow_name = flow_result.flow_name

        nodes = [
            ("TextStartNode", "Start Flow", WORKFLOW_LIBRARY_NAME),
            ("WorkerFailingNode", "Failing", LEAF_LIBRARY_NAME),
            ("TextEndNode", "End Flow", WORKFLOW_LIBRARY_NAME),
        ]
        for node_type, node_name, library_name in nodes:
            node_result = engine.handle_request(
                CreateNodeRequest(
                    node_type=node_type,
                    specific_library_name=library_name,
                    node_name=node_name,
                    override_parent_flow_name=flow_name,
                    initial_setup=True,
                )
            )
            assert isinstance(node_result, CreateNodeResultSuccess), node_result
        for source, target in [("Start Flow", "Failing"), ("Failing", "End Flow")]:
            connection_result = engine.handle_request(
                CreateConnectionRequest(
                    source_node_name=source,
                    source_parameter_name="exec_out",
                    target_node_name=target,
                    target_parameter_name="exec_in",
                )
            )
            assert connection_result.succeeded(), connection_result

        workflow_manager = engine.workflow_manager
        shape = workflow_manager.extract_workflow_shape(FAILING_WORKFLOW_NAME, flow_name=flow_name)
        serialize_result = engine.handle_request(SerializeFlowToCommandsRequest(flow_name=flow_name))
        assert isinstance(serialize_result, SerializeFlowToCommandsResultSuccess), serialize_result
        commands = serialize_result.serialized_flow_commands
        metadata = WorkflowMetadata(
            name=FAILING_WORKFLOW_NAME,
            schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
            engine_version_created_with="0.0.0",
            node_libraries_referenced=list(commands.node_dependencies.libraries),
            workflow_shape=WorkflowShape(inputs=shape["input"], outputs=shape["output"]),
        )
        content = workflow_manager.codegen.generate_workflow_file_content(
            serialized_flow_commands=commands, workflow_metadata=metadata
        )
        (host_dir / f"{FAILING_WORKFLOW_NAME}.py").write_text(content)

        # Drop the in-process build so the test starts from a clean slate.
        clear_result = engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        assert isinstance(clear_result, ClearAllObjectStateResultSuccess), clear_result

    def _write_failing_host_library(self, host_dir: Path) -> Path:
        """Write a library whose only node is a Workflow node for the failing workflow."""
        schema = {
            "name": FAILING_HOST_LIBRARY_NAME,
            "library_schema_version": "0.11.0",
            "metadata": {
                "author": "Test Fixture",
                "description": "Library holding a Workflow node whose workflow fails in a leaf node",
                "library_version": "0.1.0",
                "engine_version": engine_version,
                "tags": ["test"],
                "dependencies": {"pip_dependencies": []},
            },
            "categories": [
                {"test": {"title": "test", "description": "Test nodes", "color": "border-gray-500", "icon": "Folder"}}
            ],
            "nodes": [],
            "workflow_nodes": [
                {
                    "node_type": FAILING_NODE_TYPE,
                    "workflow_path": f"{FAILING_WORKFLOW_NAME}.py",
                    "metadata": {
                        "category": "test",
                        "description": "Runs the failing workflow",
                        "display_name": "Failing Workflow",
                    },
                }
            ],
        }
        library_json = host_dir / "griptape_nodes_library.json"
        library_json.write_text(json.dumps(schema, indent=2))
        return library_json


class TestWorkflowNodeInWorkerLibrary(_WorkflowNodeInWorkerBase):
    """The Workflow node stays on the orchestrator and the nodes inside its subflow cross to the worker."""

    @pytest.mark.asyncio
    async def test_workflow_node_runs_here_and_its_inner_nodes_run_in_the_worker(
        self,
        engine: Engine,
        worker: SimulatedWorker,
        create_node: Callable[..., str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A Workflow node from a worker-hosted library succeeds, with only its inner nodes crossing."""
        host_libraries_on_simulated_worker(engine, monkeypatch, WORKFLOW_LIBRARY_NAME)
        parent_flow = self.new_parent_flow(engine)
        create_node("ShoutWorkflow", "Shouter", parent_flow, library_name=WORKFLOW_LIBRARY_NAME)
        set_result = engine.handle_request(
            SetParameterValueRequest(parameter_name="text", node_name="Shouter", value="hey")
        )
        assert set_result.succeeded(), set_result

        result = await engine.ahandle_request(StartFlowRequest(flow_name=parent_flow, flow_node_name="Shouter"))

        self.assert_only_inner_nodes_crossed(engine, worker, "Shouter")
        assert isinstance(result, StartFlowResultSuccess), result
        shouter = engine.node_manager.get_node_by_name("Shouter")
        assert shouter.parameter_output_values.get("result") == "HEY!", shouter.parameter_output_values

    @pytest.mark.asyncio
    async def test_inner_node_failure_is_reported_as_a_node_failure(
        self,
        engine: Engine,
        worker: SimulatedWorker,
        node_results: list[ResultPayload],
        create_node: Callable[..., str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failing worker-hosted node inside a worker-hosted Workflow node fails the flow with its own message."""
        host_libraries_on_simulated_worker(engine, monkeypatch, FAILING_HOST_LIBRARY_NAME, LEAF_LIBRARY_NAME)
        parent_flow = self.new_parent_flow(engine)
        create_node(FAILING_NODE_TYPE, "Outer", parent_flow, library_name=FAILING_HOST_LIBRARY_NAME)

        result = await engine.ahandle_request(StartFlowRequest(flow_name=parent_flow, flow_node_name="Outer"))

        self.assert_only_inner_nodes_crossed(engine, worker, "Outer")
        assert isinstance(result, StartFlowResultFailure), result
        details = str(result.result_details)
        assert "doesn't exist" not in details, details
        assert LEAF_FAILURE_MESSAGE in details, details
        assert self.crossed_node_names(worker) == ["Failing"], worker.received
        failures = self.failed_node_results(node_results)
        assert len(failures) == 1, node_results
        assert LEAF_FAILURE_MESSAGE in str(failures[0].result_details), failures
