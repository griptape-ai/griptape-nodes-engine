"""A run executes against the workflow it was started in, even when that is not the only one open.

Each test builds a flow in one workflow, then opens a second, empty one beside it. While the run
goes, a read of "the open workflow" made by the machines or the node executor answers with that
second workflow, and a request they send naming no workflow is handed none. So run machinery that
reads ambient state instead of the workflow it holds fails or acts on the empty workflow, and every
such read fails the test.

Node code (`exe_types/` and node libraries) still reads the open workflow in places; moving it onto
`node.workflow` comes next. Its reads answer with the run's workflow here, as `node.workflow` will.
"""

from __future__ import annotations

import asyncio
import contextlib
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self
from unittest.mock import AsyncMock, patch

import pytest

from griptape_nodes.exe_types.node_types import NodeResolutionState
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
)
from griptape_nodes.retained_mode.events.execution_events import (
    CancelFlowRequest,
    ContinueExecutionStepRequest,
    ExecuteNodeRequest,
    ExecuteNodeResultSuccess,
    ResolveNodeRequest,
    SingleExecutionStepRequest,
    SingleNodeStepRequest,
    StartFlowFromNodeRequest,
    StartFlowRequest,
)
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from griptape_nodes.retained_mode.managers import event_manager as event_manager_module
from griptape_nodes.retained_mode.runtime.open_workflow_set import OpenWorkflowSet
from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime
from tests.workflow_context import push_unsaved_workflow

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload

pytestmark = pytest.mark.timeout(120, method="thread")

FIXTURES_DIR = Path(__file__).parent / "fixtures"
CHAIN_LIBRARY_DIR = FIXTURES_DIR / "loose_sink_library"
CHAIN_LIBRARY_NAME = "Loose Sink Library"
GATED_LIBRARY_DIR = FIXTURES_DIR / "node_deletion_library"
GATED_LIBRARY_NAME = "Node Deletion Library"
FLOW_NAME = "MainFlow"

# Where run machinery lives. An ambient read made from one of these fails the test.
_RUN_MACHINERY = ("griptape_nodes/machines/", "griptape_nodes/common/node_executor.py")

# The frames a request passes through on its way to a handler.
_DISPATCH_FILES = (
    "retained_mode/engine.py",
    "retained_mode/griptape_nodes.py",
    "retained_mode/managers/event_manager.py",
    "retained_mode/runtime/workflow_runtime.py",
)

_POLL_ATTEMPTS = 500
_POLL_SECONDS = 0.01


def _is_run_machinery(reader: str) -> bool:
    return any(part in reader for part in _RUN_MACHINERY)


def _reader_of(stack: list[traceback.FrameSummary]) -> str:
    """The code that asked for the open workflow: the first frame outside the engine's managers.

    A read made by a handler while a request is dispatched is the handler's own, so a frame of
    dispatch ends the search there.
    """
    for frame in reversed(stack):
        path = frame.filename.replace("\\", "/")
        if path.endswith("open_workflow_set.py"):
            continue
        if path.endswith("retained_mode/managers/event_manager.py"):
            return "a request handler"
        if "griptape_nodes/retained_mode/" in path:
            continue
        return f"{path}:{frame.lineno} in {frame.name}"
    return "unknown"


class _AnotherWorkflowOpen:
    """Opens an empty second workflow beside the run's, and makes it what run machinery reads as open.

    A request the run machinery sends naming no workflow is handed none, as every one will be once
    the temporary fallback that hands it the only open workflow is deleted.
    """

    def __init__(self, engine: Engine, run_workflow: WorkflowRuntime) -> None:
        self._engine = engine
        self._run_workflow = run_workflow
        entry = engine.workflow_registry.register_unsaved("Another workflow", None)
        self.workflow = WorkflowRuntime(entry.workflow_id, engine)
        self.run_machinery_readers: list[str] = []

    def __enter__(self) -> Self:
        self._engine.open_workflows._by_id[self.workflow.workflow_id] = self.workflow
        self._patches = contextlib.ExitStack()
        self._patches.enter_context(patch.object(OpenWorkflowSet, "sole_workflow_or_none", self._ambient_workflow))
        self._patches.enter_context(
            patch.object(event_manager_module, "_workflow_for_unaddressed_request", self._unaddressed_workflow)
        )
        return self

    def __exit__(self, *_exc: object) -> None:
        self._patches.close()
        del self._engine.open_workflows._by_id[self.workflow.workflow_id]

    def assert_untouched(self) -> None:
        assert self.run_machinery_readers == []
        assert len(self.workflow.objects) == 0
        assert self.workflow.flows.global_control_flow_machine is None
        assert self.workflow.flows.global_dag_builder.node_to_reference == {}

    def _ambient_workflow(self) -> WorkflowRuntime:
        # Patched onto the class as a bound method, so it is not handed the set it replaces.
        reader = _reader_of(traceback.extract_stack()[:-1])
        if not _is_run_machinery(reader):
            return self._run_workflow
        self.run_machinery_readers.append(reader)
        return self.workflow

    def _unaddressed_workflow(self, _open_workflows: OpenWorkflowSet) -> WorkflowRuntime | None:
        # Drop the frames of the dispatch that asked, so the reader is whoever sent the request.
        stack = traceback.extract_stack()[:-1]
        while stack and stack[-1].filename.replace("\\", "/").endswith(_DISPATCH_FILES):
            stack.pop()
        reader = _reader_of(stack)
        if not _is_run_machinery(reader):
            return self._run_workflow
        self.run_machinery_readers.append(reader)
        return None


@pytest.fixture
def parallel_mode(engine: Engine) -> Iterator[None]:
    """Run under the parallel scheduler, so independent nodes run at the same time."""
    config_manager = engine.config_manager
    previous_mode = config_manager.get_config_value("workflow_execution_mode")
    previous_max = config_manager.get_config_value("max_nodes_in_parallel")
    config_manager.set_config_value("workflow_execution_mode", "parallel")
    config_manager.set_config_value("max_nodes_in_parallel", 4)
    try:
        yield
    finally:
        config_manager.set_config_value("workflow_execution_mode", previous_mode)
        config_manager.set_config_value("max_nodes_in_parallel", previous_max)


@pytest.fixture
def run_workflow(engine: Engine, tmp_path: Path, materialize_library: Callable[..., Path]) -> WorkflowRuntime:
    """The workflow the test builds its flow in and runs, with the fixture libraries registered."""
    for directory, node_file in (
        (CHAIN_LIBRARY_DIR, "loose_sink_nodes.py"),
        (GATED_LIBRARY_DIR, "gated_stream_nodes.py"),
    ):
        library_json = materialize_library(
            tmp_path / directory.name,
            template=directory / "griptape_nodes_library.json",
            node_file=directory / node_file,
        )
        result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
        assert isinstance(result, RegisterLibraryFromFileResultSuccess), result

    entry = push_unsaved_workflow(engine, "Run workflow")
    result = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name=FLOW_NAME))
    assert isinstance(result, CreateFlowResultSuccess), result
    return engine.open_workflows.get(entry.workflow_id)


@pytest.fixture
def chain(run_workflow: WorkflowRuntime) -> None:
    """A -> B -> C on control and data, and a data-only sink D fed by B, in the run's workflow."""
    for name, node_type in (("A", "ChainNode"), ("B", "ChainNode"), ("C", "ChainNode"), ("D", "SinkNode")):
        created = run_workflow.handle_request(
            CreateNodeRequest(
                node_type=node_type,
                specific_library_name=CHAIN_LIBRARY_NAME,
                node_name=name,
                override_parent_flow_name=FLOW_NAME,
            )
        )
        assert isinstance(created, CreateNodeResultSuccess), created
    for source, source_parameter, target, target_parameter in (
        ("A", "exec_out", "B", "exec_in"),
        ("B", "exec_out", "C", "exec_in"),
        ("A", "result", "B", "text"),
        ("B", "result", "C", "text"),
        ("B", "result", "D", "text"),
    ):
        connected = run_workflow.handle_request(
            CreateConnectionRequest(
                source_node_name=source,
                source_parameter_name=source_parameter,
                target_node_name=target,
                target_parameter_name=target_parameter,
            )
        )
        assert isinstance(connected, CreateConnectionResultSuccess), connected


async def _send(workflow: WorkflowRuntime, request: RequestPayload) -> ResultPayload:
    result = await workflow.ahandle_request(request)
    assert result.succeeded(), result.result_details
    return result


def _states(workflow: WorkflowRuntime, *names: str) -> dict[str, NodeResolutionState]:
    return {name: _node(workflow, name).state for name in names}


def _node(workflow: WorkflowRuntime, name: str) -> Any:
    node = workflow.objects.get(name)
    assert node is not None, name
    return node


def _all_resolved(*names: str) -> dict[str, NodeResolutionState]:
    return dict.fromkeys(names, NodeResolutionState.RESOLVED)


@pytest.mark.usefixtures("chain")
class TestARunInAWorkflowThatIsNotTheOnlyOneOpen:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["sequential", "parallel"])
    async def test_starting_the_flow_runs_it_in_its_workflow(
        self, engine: Engine, run_workflow: WorkflowRuntime, request: pytest.FixtureRequest, mode: str
    ) -> None:
        if mode == "parallel":
            request.getfixturevalue("parallel_mode")

        with _AnotherWorkflowOpen(engine, run_workflow) as another:
            await _send(run_workflow, StartFlowRequest(flow_name=FLOW_NAME))

        assert _states(run_workflow, "A", "B", "C", "D") == _all_resolved("A", "B", "C", "D")
        assert _node(run_workflow, "C").parameter_output_values["result"] == ">A>B>C"
        another.assert_untouched()

    @pytest.mark.asyncio
    async def test_starting_from_a_node_runs_it_in_its_workflow(
        self, engine: Engine, run_workflow: WorkflowRuntime
    ) -> None:
        with _AnotherWorkflowOpen(engine, run_workflow) as another:
            await _send(run_workflow, StartFlowFromNodeRequest(flow_name=FLOW_NAME, node_name="B"))

        assert _states(run_workflow, "B", "C") == _all_resolved("B", "C")
        another.assert_untouched()

    @pytest.mark.asyncio
    async def test_running_a_single_node_runs_it_and_its_inputs_in_its_workflow(
        self, engine: Engine, run_workflow: WorkflowRuntime
    ) -> None:
        with _AnotherWorkflowOpen(engine, run_workflow) as another:
            await _send(run_workflow, ResolveNodeRequest(node_name="C"))

        assert _states(run_workflow, "A", "B", "C") == _all_resolved("A", "B", "C")
        assert _node(run_workflow, "C").parameter_output_values["result"] == ">A>B>C"
        another.assert_untouched()

    @pytest.mark.asyncio
    async def test_stepping_a_paused_run_and_continuing_run_it_in_its_workflow(
        self, engine: Engine, run_workflow: WorkflowRuntime
    ) -> None:
        # A step request that carries a request id pauses the run rather than advancing it.
        step = SingleExecutionStepRequest(flow_name=FLOW_NAME)
        step.request_id = "step"

        with _AnotherWorkflowOpen(engine, run_workflow) as another:
            await _send(run_workflow, StartFlowRequest(flow_name=FLOW_NAME, debug_mode=True))
            await _send(run_workflow, step)
            assert engine.flow_manager.check_for_existing_running_flow_in(run_workflow)
            assert _states(run_workflow, "A") == {"A": NodeResolutionState.UNRESOLVED}
            for _ in range(_POLL_ATTEMPTS):
                if not engine.flow_manager.check_for_existing_running_flow_in(run_workflow):
                    break
                await _send(run_workflow, ContinueExecutionStepRequest(flow_name=FLOW_NAME))

        assert _states(run_workflow, "A", "B", "C", "D") == _all_resolved("A", "B", "C", "D")
        another.assert_untouched()

    @pytest.mark.asyncio
    async def test_stepping_over_a_node_runs_it_in_its_workflow(
        self, engine: Engine, run_workflow: WorkflowRuntime
    ) -> None:
        with _AnotherWorkflowOpen(engine, run_workflow) as another:
            await _send(run_workflow, StartFlowRequest(flow_name=FLOW_NAME, debug_mode=True))
            await _send(run_workflow, SingleNodeStepRequest(flow_name=FLOW_NAME))

        assert _states(run_workflow, "A", "B", "C", "D") == _all_resolved("A", "B", "C", "D")
        another.assert_untouched()


class TestCancellingARunInAWorkflowThatIsNotTheOnlyOneOpen:
    @pytest.mark.asyncio
    async def test_cancelling_stops_the_run_in_its_workflow(
        self, engine: Engine, run_workflow: WorkflowRuntime, tmp_path: Path
    ) -> None:
        created = run_workflow.handle_request(
            CreateNodeRequest(
                node_type="GatedStreamNode",
                specific_library_name=GATED_LIBRARY_NAME,
                node_name="Gated",
                override_parent_flow_name=FLOW_NAME,
            )
        )
        assert isinstance(created, CreateNodeResultSuccess), created
        gated = _node(run_workflow, "Gated")
        gated.set_parameter_value("gate_file", str(tmp_path / "never_opened"))

        with _AnotherWorkflowOpen(engine, run_workflow) as another:
            run = asyncio.create_task(run_workflow.ahandle_request(StartFlowRequest(flow_name=FLOW_NAME)))
            for _ in range(_POLL_ATTEMPTS):
                if gated.state is NodeResolutionState.RESOLVING:
                    break
                await asyncio.sleep(_POLL_SECONDS)
            assert gated.state is NodeResolutionState.RESOLVING

            await _send(run_workflow, CancelFlowRequest(flow_name=FLOW_NAME))
            await asyncio.wait_for(run, timeout=30)

        assert not engine.flow_manager.check_for_existing_running_flow_in(run_workflow)
        assert gated.state is not NodeResolutionState.RESOLVED
        another.assert_untouched()


@pytest.mark.usefixtures("chain")
class TestAWorkerRoutedNodeInAWorkflowThatIsNotTheOnlyOneOpen:
    @pytest.mark.asyncio
    async def test_its_dispatch_names_the_workflow_it_runs_in(
        self, engine: Engine, run_workflow: WorkflowRuntime
    ) -> None:
        sent: list[ExecuteNodeRequest] = []
        tracked_while_routed: list[bool] = []

        async def route_to_worker(event_request: Any, *_worker: str) -> dict:
            request = event_request.request
            sent.append(request)
            tracked_while_routed.append(request.node_name in run_workflow.nodes.orch_worker_requests)
            outputs = {}
            if request.node_name != "D":
                outputs = {"result": f">{request.node_name}"}
            return {
                "result_type": ExecuteNodeResultSuccess.__name__,
                "result": {"result_details": "ok", "parameter_output_values": outputs},
            }

        with (
            _AnotherWorkflowOpen(engine, run_workflow) as another,
            patch.object(engine.library_manager.workers, "get_worker_for_library", return_value=("worker", "topic")),
            patch.object(engine.worker_manager, "wait_until_executable", AsyncMock()),
            patch.object(engine.worker_manager, "route_to_worker", side_effect=route_to_worker),
        ):
            await _send(run_workflow, StartFlowRequest(flow_name=FLOW_NAME))

        entry = engine.workflow_registry.get(run_workflow.workflow_id)
        assert {request.node_name for request in sent} == {"A", "B", "C", "D"}
        for request in sent:
            assert request.orchestrator_workflow_id == run_workflow.workflow_id
            assert request.workflow_name == entry.name
        assert all(tracked_while_routed)
        assert run_workflow.nodes.orch_worker_requests == {}
        assert _states(run_workflow, "A", "B", "C", "D") == _all_resolved("A", "B", "C", "D")
        another.assert_untouched()


GROUP_LIBRARY_DIR = FIXTURES_DIR / "iterative_group_library"
# The loop packager asks for its flow endpoints from the standard library, by name.
GROUP_LIBRARY_NAME = "Griptape Nodes Library"


class TestAnIterativeGroupInAWorkflowThatIsNotTheOnlyOneOpen:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("execution_mode", ["Run Group Items One at a Time", "Run Group Items All at Once"])
    async def test_its_iterations_run_in_its_workflow(
        self,
        engine: Engine,
        run_workflow: WorkflowRuntime,
        tmp_path: Path,
        materialize_library: Callable[..., Path],
        execution_mode: str,
    ) -> None:
        library_json = materialize_library(
            tmp_path / "iterative_group_library",
            template=GROUP_LIBRARY_DIR / "griptape_nodes_library.json",
            node_file=GROUP_LIBRARY_DIR / "iterative_group_nodes.py",
            name=GROUP_LIBRARY_NAME,
        )
        registered = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
        assert isinstance(registered, RegisterLibraryFromFileResultSuccess), registered
        for name, node_type, parent_group_name in (
            ("For Each", "ProbeForEachGroupNode", None),
            ("Body", "LoopBodyControlNode", "For Each"),
        ):
            created = run_workflow.handle_request(
                CreateNodeRequest(
                    node_type=node_type,
                    specific_library_name=GROUP_LIBRARY_NAME,
                    node_name=name,
                    override_parent_flow_name=FLOW_NAME,
                    parent_group_name=parent_group_name,
                )
            )
            assert isinstance(created, CreateNodeResultSuccess), created
        for source, source_parameter, target, target_parameter in (
            ("For Each", "on_each", "Body", "exec_in"),
            ("Body", "exec_out", "For Each", "loop_complete"),
            ("For Each", "index", "Body", "text"),
            ("Body", "result", "For Each", "new_item_to_add"),
        ):
            connected = run_workflow.handle_request(
                CreateConnectionRequest(
                    source_node_name=source,
                    source_parameter_name=source_parameter,
                    target_node_name=target,
                    target_parameter_name=target_parameter,
                )
            )
            assert isinstance(connected, CreateConnectionResultSuccess), connected
        group = _node(run_workflow, "For Each")
        group.set_parameter_value("execution_mode", execution_mode)

        with _AnotherWorkflowOpen(engine, run_workflow) as another:
            await _send(run_workflow, StartFlowRequest(flow_name=FLOW_NAME, flow_node_name="For Each"))

        assert group.parameter_output_values["results"] == ["0", "1", "2"]
        # Every iteration's flow is gone again, and none was made in the other workflow.
        assert set(run_workflow.flows.name_to_parent_name) == {FLOW_NAME, group.metadata["subflow_name"]}
        another.assert_untouched()
