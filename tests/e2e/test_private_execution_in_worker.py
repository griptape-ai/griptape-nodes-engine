"""A Private Execution group runs through the local path and sends its nodes to the worker.

With the `private_execution_in_worker` beta feature on, `NodeExecutor` no longer packages the group
and starts a process for it. It runs the group like Local Execution, inside a scope naming the
general-purpose worker, and `NodeManager` sends every node the group runs to that worker.

The worker here is a stand-in: it records each node it is sent, with the scope the node arrived
in, then runs the node in this process. Everything up to that hand-off is the real engine,
including PARALLEL resolution, whose concurrent node tasks are what has to carry the scope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest

from griptape_nodes.common.node_executor import NodeExecutor
from griptape_nodes.common.private_execution_scope import PrivateExecutionScope, current_private_execution_scope
from griptape_nodes.exe_types.node_types import PRIVATE_EXECUTION, NodeResolutionState
from griptape_nodes.retained_mode.beta_features import PRIVATE_EXECUTION_IN_WORKER
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
)
from griptape_nodes.retained_mode.events.execution_events import (
    ExecuteNodeRequest,
    StartFlowRequest,
    StartFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest
from griptape_nodes.retained_mode.managers.worker_manager import WorkerRegistration

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from griptape_nodes.retained_mode.engine import Engine

# Timeout with thread dump.
pytestmark = pytest.mark.timeout(300, method="thread")

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "subflow_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "subflow_echo_node.py"
# Distinct from the other tests registering this fixture in-process; see
# test_subflow_group_parallel_execution.py for why a shared name fails.
LIBRARY_NAME = "Private Execution Worker Library"
_NODE_COUNT = 3
_DELAY_SECONDS = 0.3
_GENERAL_PURPOSE_WORKER = "general-purpose-worker"


@pytest.fixture
def parallel_mode(engine: Engine) -> Iterator[None]:
    """Run flows in PARALLEL mode, whose concurrent node tasks are what has to carry the scope."""
    config_manager = engine.config_manager
    prior_mode = config_manager.get_config_value("workflow_execution_mode")
    prior_max = config_manager.get_config_value("max_nodes_in_parallel")
    config_manager.set_config_value("workflow_execution_mode", "parallel")
    config_manager.set_config_value("max_nodes_in_parallel", 5)
    yield
    config_manager.set_config_value("workflow_execution_mode", prior_mode)
    config_manager.set_config_value("max_nodes_in_parallel", prior_max)


@dataclass
class _StandInWorker:
    """A general-purpose worker that records what it is sent and runs it in this process."""

    engine: Engine
    received: list[tuple[str, PrivateExecutionScope | None]] = field(default_factory=list)

    def start(self) -> None:
        """Register it as up, at the library count it would have loaded.

        Called once the test's libraries are registered: a real worker starts after the libraries
        load, and one registered before a library change is stale and gets restarted.
        """
        self.engine.worker_manager._workers[_GENERAL_PURPOSE_WORKER] = WorkerRegistration(
            request_topic=f"sessions/test/workers/{_GENERAL_PURPOSE_WORKER}/request", worker_key=None
        )
        workers = self.engine.library_manager.workers
        workers._general_purpose_worker_generation = workers._library_set_generation


@pytest.fixture
def general_purpose_worker(engine: Engine) -> Iterator[_StandInWorker]:
    """Stand in for the general-purpose worker at the hand-off, so everything before it is the real engine."""
    worker = _StandInWorker(engine=engine)
    node_manager = engine.node_manager

    async def run_on_worker(request: ExecuteNodeRequest, *_args: Any) -> Any:
        worker.received.append((request.node_name, current_private_execution_scope.get()))
        # The worker has no scope of its own: run the node here, as it would run there.
        token = current_private_execution_scope.set(None)
        try:
            return await node_manager.on_execute_node_request(request)
        finally:
            current_private_execution_scope.reset(token)

    with patch.object(node_manager, "_execute_node_via_worker", side_effect=run_on_worker):
        yield worker
    engine.worker_manager._workers.pop(_GENERAL_PURPOSE_WORKER, None)


def _build_group(engine: Engine, library_json: Path, *, execution_environment: str) -> tuple[str, str, list[str]]:
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result
    engine.context_manager.push_workflow(workflow_name="private_execution_in_worker_wf")

    parent_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ParentFlow", set_as_new_context=False)
    )
    assert isinstance(parent_result, CreateFlowResultSuccess), parent_result
    parent_flow = parent_result.flow_name

    with engine.context_manager.flow(parent_flow):
        group_result = engine.handle_request(
            CreateNodeRequest(node_type="SubflowGroupNode", specific_library_name=LIBRARY_NAME, node_name="Group_1")
        )
        assert isinstance(group_result, CreateNodeResultSuccess), group_result
        node_names = []
        for i in range(_NODE_COUNT):
            node_result = engine.handle_request(
                CreateNodeRequest(
                    node_type="TimedEchoNode",
                    specific_library_name=LIBRARY_NAME,
                    node_name=f"Timed_{i}",
                    parent_group_name=group_result.node_name,
                )
            )
            assert isinstance(node_result, CreateNodeResultSuccess), node_result
            node_names.append(node_result.node_name)

    for i, name in enumerate(node_names):
        engine.handle_request(SetParameterValueRequest(parameter_name="text", node_name=name, value=f"n-{i}"))
        engine.handle_request(SetParameterValueRequest(parameter_name="delay", node_name=name, value=_DELAY_SECONDS))
    engine.handle_request(
        SetParameterValueRequest(
            parameter_name="execution_environment", node_name=group_result.node_name, value=execution_environment
        )
    )
    return parent_flow, group_result.node_name, node_names


@pytest.mark.skipif(
    not FIXTURE_LIBRARY_JSON_TEMPLATE.exists(),
    reason=f"Subflow Library fixture missing at {FIXTURE_LIBRARY_JSON_TEMPLATE}",
)
@pytest.mark.asyncio
@pytest.mark.usefixtures("parallel_mode")
async def test_every_node_of_the_group_runs_on_the_worker(
    tmp_path: Path,
    engine: Engine,
    materialize_library: Callable[..., Path],
    general_purpose_worker: _StandInWorker,
) -> None:
    """Each node the group runs reaches the worker with the group's scope, and no process is started."""
    library_json = materialize_library(
        tmp_path / "library", template=FIXTURE_LIBRARY_JSON_TEMPLATE, node_file=FIXTURE_NODE_FILE, name=LIBRARY_NAME
    )
    config_manager = engine.config_manager
    config_manager.set_config_value(PRIVATE_EXECUTION_IN_WORKER.config_key, True)
    try:
        parent_flow, group_name, node_names = _build_group(
            engine, library_json, execution_environment=PRIVATE_EXECUTION
        )
        general_purpose_worker.start()
        with patch.object(NodeExecutor, "_execute_private_workflow", new_callable=AsyncMock) as private_workflow:
            run_result = await engine.ahandle_request(
                StartFlowRequest(flow_name=parent_flow, flow_node_name=group_name)
            )
    finally:
        config_manager.set_config_value(PRIVATE_EXECUTION_IN_WORKER.config_key, False)

    assert isinstance(run_result, StartFlowResultSuccess), run_result
    private_workflow.assert_not_awaited()
    # Every node the group ran reached the worker, each still carrying the group's scope, including
    # the ones PARALLEL resolution ran as concurrent tasks.
    assert sorted(general_purpose_worker.received) == sorted(
        (name, PrivateExecutionScope(owner_name=group_name)) for name in node_names
    )
    for i, name in enumerate(node_names):
        node = engine.node_manager.get_node_by_name(name)
        assert node.state == NodeResolutionState.RESOLVED, f"{name} did not resolve (state={node.state})"
        assert node.parameter_output_values.get("text") == f"n-{i}"


@pytest.mark.skipif(
    not FIXTURE_LIBRARY_JSON_TEMPLATE.exists(),
    reason=f"Subflow Library fixture missing at {FIXTURE_LIBRARY_JSON_TEMPLATE}",
)
@pytest.mark.asyncio
@pytest.mark.usefixtures("parallel_mode")
async def test_with_the_feature_off_the_group_still_starts_its_own_process(
    tmp_path: Path,
    engine: Engine,
    materialize_library: Callable[..., Path],
    general_purpose_worker: _StandInWorker,
) -> None:
    """Off, Private Execution packages the group and starts a process for it, as before."""
    library_json = materialize_library(
        tmp_path / "library",
        template=FIXTURE_LIBRARY_JSON_TEMPLATE,
        node_file=FIXTURE_NODE_FILE,
        name=f"{LIBRARY_NAME} Off",
    )
    register_name = f"{LIBRARY_NAME} Off"
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result
    engine.context_manager.push_workflow(workflow_name="private_execution_off_wf")
    parent_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ParentFlow", set_as_new_context=False)
    )
    assert isinstance(parent_result, CreateFlowResultSuccess), parent_result
    with engine.context_manager.flow(parent_result.flow_name):
        group_result = engine.handle_request(
            CreateNodeRequest(node_type="SubflowGroupNode", specific_library_name=register_name, node_name="Group_1")
        )
        assert isinstance(group_result, CreateNodeResultSuccess), group_result
        node_result = engine.handle_request(
            CreateNodeRequest(
                node_type="TimedEchoNode",
                specific_library_name=register_name,
                node_name="Timed_0",
                parent_group_name=group_result.node_name,
            )
        )
        assert isinstance(node_result, CreateNodeResultSuccess), node_result
    engine.handle_request(
        SetParameterValueRequest(
            parameter_name="execution_environment", node_name=group_result.node_name, value=PRIVATE_EXECUTION
        )
    )

    general_purpose_worker.start()
    with patch.object(NodeExecutor, "_execute_private_workflow", new_callable=AsyncMock) as private_workflow:
        await engine.ahandle_request(
            StartFlowRequest(flow_name=parent_result.flow_name, flow_node_name=group_result.node_name)
        )

    private_workflow.assert_awaited_once()
    assert general_purpose_worker.received == []


LOOP_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "iterative_group_library"
# The packaged iteration flows take their StartFlow/EndFlow nodes from a library with exactly this name.
LOOP_LIBRARY_NAME = "Griptape Nodes Library"


@pytest.mark.parametrize("execution_mode", ["Run Group Items One at a Time", "Run Group Items All at Once"])
@pytest.mark.asyncio
async def test_a_private_loop_sends_its_iterations_to_the_worker(
    tmp_path: Path,
    engine: Engine,
    materialize_library: Callable[..., Path],
    general_purpose_worker: _StandInWorker,
    execution_mode: str,
) -> None:
    """A loop needs nothing special: its iterations take the local loop path, inside the scope."""
    library_json = materialize_library(
        tmp_path / "library",
        template=LOOP_FIXTURE_DIR / "griptape_nodes_library.json",
        node_file=LOOP_FIXTURE_DIR / "iterative_group_nodes.py",
        name=LOOP_LIBRARY_NAME,
    )
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result
    engine.context_manager.push_workflow(workflow_name="private_loop_in_worker_wf")
    parent_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ParentFlow", set_as_new_context=False)
    )
    assert isinstance(parent_result, CreateFlowResultSuccess), parent_result
    parent_flow = parent_result.flow_name

    with engine.context_manager.flow(parent_flow):
        group_result = engine.handle_request(
            CreateNodeRequest(
                node_type="ProbeForEachGroupNode", specific_library_name=LOOP_LIBRARY_NAME, node_name="ForEachGroup"
            )
        )
        assert isinstance(group_result, CreateNodeResultSuccess), group_result
        body_result = engine.handle_request(
            CreateNodeRequest(
                node_type="LoopBodyControlNode",
                specific_library_name=LOOP_LIBRARY_NAME,
                node_name="Body",
                parent_group_name=group_result.node_name,
            )
        )
        assert isinstance(body_result, CreateNodeResultSuccess), body_result
    group_name = group_result.node_name
    for source_node, source_param, target_node, target_param in (
        (group_name, "on_each", "Body", "exec_in"),
        ("Body", "exec_out", group_name, "loop_complete"),
        (group_name, "index", "Body", "text"),
        ("Body", "result", group_name, "new_item_to_add"),
    ):
        connection_result = engine.handle_request(
            CreateConnectionRequest(
                source_node_name=source_node,
                source_parameter_name=source_param,
                target_node_name=target_node,
                target_parameter_name=target_param,
            )
        )
        assert isinstance(connection_result, CreateConnectionResultSuccess), connection_result
    for parameter_name, value in (("execution_mode", execution_mode), ("execution_environment", PRIVATE_EXECUTION)):
        engine.handle_request(
            SetParameterValueRequest(parameter_name=parameter_name, node_name=group_name, value=value)
        )

    config_manager = engine.config_manager
    config_manager.set_config_value(PRIVATE_EXECUTION_IN_WORKER.config_key, True)
    general_purpose_worker.start()
    try:
        with (
            patch.object(NodeExecutor, "_execute_loop_iterations_privately", new_callable=AsyncMock) as parallel,
            patch.object(NodeExecutor, "_execute_loop_iterations_sequentially_private", new_callable=AsyncMock) as one,
            engine.context_manager.flow(parent_flow),
        ):
            run_result = await engine.ahandle_request(
                StartFlowRequest(flow_name=parent_flow, flow_node_name=group_name)
            )
    finally:
        config_manager.set_config_value(PRIVATE_EXECUTION_IN_WORKER.config_key, False)

    assert isinstance(run_result, StartFlowResultSuccess), run_result
    parallel.assert_not_awaited()
    one.assert_not_awaited()
    # The body runs as a packaged copy per iteration, so count what reached the worker rather than
    # naming it: at least one node per iteration, every one carrying the loop's scope.
    scopes = {scope for _name, scope in general_purpose_worker.received}
    assert scopes == {PrivateExecutionScope(owner_name=group_name)}, general_purpose_worker.received
    body_runs = [name for name, _scope in general_purpose_worker.received if name.startswith("Body")]
    assert len(body_runs) >= len(("first", "second", "third")), general_purpose_worker.received
