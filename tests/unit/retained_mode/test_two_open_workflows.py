"""Two workflows open in one engine at once, driven through dispatch with envelope workflow ids.

Not a feature test: the engine opens one workflow at a time. Each test drives the foundation with a
second workflow open next to the first (`OpenWorkflowSet(allow_several_open=True)`, a test seam), to
check that each workflow's state and teardown stay its own. A gap later work closes is a strict xfail
with the gap as its reason.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any, ClassVar, cast
from unittest.mock import AsyncMock, patch

import pytest

from griptape_nodes.app.worker_routing import DropAllLocalObjectsRequest, _handle_drop_all_local_objects
from griptape_nodes.common.macro_parser import ParsedMacro
from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import ControlNode, DataNode
from griptape_nodes.node_library.library_registry import LibraryMetadata, LibraryRegistry, LibrarySchema, NodeMetadata
from griptape_nodes.retained_mode.events.app_events import EngineHeartbeatRequest, GetEngineVersionRequest
from griptape_nodes.retained_mode.events.base_events import (
    ExecutionGriptapeNodeEvent,
    GriptapeNodeEvent,
    ProgressEvent,
)
from griptape_nodes.retained_mode.events.config_events import (
    GetConfigCategoryRequest,
    GetWorkspaceRequest,
    ListBetaFeaturesRequest,
)
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
    ListConnectionsForNodeRequest,
    ListConnectionsForNodeResultSuccess,
)
from griptape_nodes.retained_mode.events.context_events import (
    EnsureWorkflowAndFlowRequest,
    EnsureWorkflowAndFlowResultSuccess,
    GetWorkflowContextRequest,
    GetWorkflowContextSuccess,
    SetWorkflowContextRequest,
)
from griptape_nodes.retained_mode.events.execution_events import (
    CancelFlowRequest,
    ExecuteNodeRequest,
    ExecuteNodeResultSuccess,
    GetFlowStateRequest,
    GetIsFlowRunningRequest,
    GetIsFlowRunningResultSuccess,
    StartFlowRequest,
    StartFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    GetTopLevelFlowRequest,
    GetTopLevelFlowResultSuccess,
    ListFlowsInCurrentContextRequest,
    ListNodesInFlowRequest,
    ListNodesInFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.generic_events import WorkflowNotOpenResultFailure
from griptape_nodes.retained_mode.events.library_events import ListRegisteredLibrariesRequest, ReloadAllLibrariesRequest
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    CreateNodeResultSuccess,
    DeleteNodeRequest,
    GetAllNodeInfoRequest,
    GetNodeResolutionStateRequest,
    ListParametersOnNodeRequest,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest, RenameObjectRequest
from griptape_nodes.retained_mode.events.os_events import ResolveMacroPathRequest
from griptape_nodes.retained_mode.events.parameter_events import (
    GetParameterValueRequest,
    GetParameterValueResultSuccess,
    SetParameterValueRequest,
)
from griptape_nodes.retained_mode.events.project_events import (
    GetCurrentProjectRequest,
    ListProjectTemplatesRequest,
    LoadProjectTemplateRequest,
    LoadProjectTemplateResultSuccess,
    MacroPath,
    SetCurrentProjectRequest,
)
from griptape_nodes.retained_mode.events.variable_events import (
    CreateVariableRequest,
    CreateVariableResultSuccess,
    GetVariableValueRequest,
    GetVariableValueResultSuccess,
    ListVariablesRequest,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    DeleteWorkflowRequest,
    GetVariableSubstitutionEnabledRequest,
    GetWorkflowRunCommandRequest,
    ListAllWorkflowInfoRequest,
    ListAllWorkflowsRequest,
    ListCallableWorkflowsRequest,
    RunWorkflowFromRegistryRequest,
    SaveWorkflowRequest,
    SaveWorkflowResultSuccess,
)
from griptape_nodes.retained_mode.managers.event_manager import ResultContext
from griptape_nodes.retained_mode.runtime.binding import NO_WORKFLOW, WorkflowNotNamedError
from griptape_nodes.retained_mode.runtime.open_workflow_set import OpenWorkflowSet
from griptape_nodes.retained_mode.variable_types import VariableScope

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload
    from griptape_nodes.retained_mode.events.execution_events import NodeMetadata as ExecutionNodeMetadata
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

_LIBRARY_NAME = "Two Open Workflows Spike Library"


def _resolve(macro: str) -> ResolveMacroPathRequest:
    return ResolveMacroPathRequest(macro_path=MacroPath(ParsedMacro(macro), {}))


class _ValueNode(DataNode):
    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="value",
                tooltip="Value",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY, ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        self.parameter_output_values["value"] = self.get_parameter_value("value")


class _ParkNode(ControlNode):
    """Records the workflow it runs in, waits on its gate (keyed by node name), then echoes its input."""

    gates: ClassVar[dict[str, asyncio.Event]] = {}
    started: ClassVar[dict[str, asyncio.Event]] = {}
    ran_in: ClassVar[dict[str, Any]] = {}

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="value",
                tooltip="Value",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(Parameter(name="result", tooltip="Result", type="str", allowed_modes={ParameterMode.OUTPUT}))

    def process(self) -> None:
        raise NotImplementedError

    async def aprocess(self) -> None:
        _ParkNode.ran_in[self.name] = self.engine.current_workflow_or_none()
        started = _ParkNode.started.get(self.name)
        if started is not None:
            started.set()
        gate = _ParkNode.gates.get(self.name)
        if gate is not None:
            await gate.wait()
        self.parameter_output_values["result"] = f"done:{self.get_parameter_value('value')}"


class _RawThreadNode(ControlNode):
    """Node code that starts a plain `threading.Thread` (no copied context) and asks for its workflow there."""

    seen: ClassVar[dict[str, Any]] = {}

    def process(self) -> None:
        raise NotImplementedError

    async def aprocess(self) -> None:
        engine = self.engine

        def read_in_raw_thread() -> None:
            try:
                _RawThreadNode.seen["workflow"] = engine.current_workflow_or_none()
            except WorkflowNotNamedError as err:
                _RawThreadNode.seen["workflow"] = err
            result = engine.handle_request(_resolve("{workflow_dir}/out.png"))
            _RawThreadNode.seen["workflow_dir"] = result

        thread = threading.Thread(target=read_in_raw_thread)
        thread.start()
        await asyncio.to_thread(thread.join)
        # The same read through asyncio.to_thread, which copies the context.
        _RawThreadNode.seen["to_thread_workflow"] = await asyncio.to_thread(engine.current_workflow_or_none)


@pytest.fixture
def spike_library() -> Iterator[None]:
    """Register this module's nodes as a library. LibraryRegistry is process-global, so it is cleared around the test."""
    LibraryRegistry._clear()
    schema = LibrarySchema(
        name=_LIBRARY_NAME,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="test", description="test", library_version="0.1.0", engine_version="0.0.0", tags=[]
        ),
        categories=[],
        nodes=[],
    )
    library = LibraryRegistry.generate_new_library(schema)
    for node_class in (_ValueNode, _ParkNode, _RawThreadNode):
        library.register_new_node_type(
            node_class, NodeMetadata(category="test", description="d", display_name=node_class.__name__)
        )
    _ParkNode.gates = {}
    _ParkNode.started = {}
    _ParkNode.ran_in = {}
    _RawThreadNode.seen = {}
    try:
        yield
    finally:
        LibraryRegistry._clear()


def _allow_several_open(engine: Engine) -> None:
    """Swap in an open set that lets a second workflow open next to the first (the spike's one switch)."""
    engine._open_workflows = OpenWorkflowSet(allow_several_open=True)


def _open(engine: Engine, display_name: str) -> WorkflowRuntime:
    """Open a new unsaved workflow next to whatever is open, as an editor tab would."""
    workflow = engine.workflow_registry.register_unsaved(display_name, listed=False)
    return engine.open_workflow(workflow.workflow_id)


def _send(engine: Engine, request: RequestPayload, workflow: WorkflowRuntime | None) -> ResultPayload:
    """Dispatch `request` as a client would, with the envelope naming `workflow` (or nothing)."""
    workflow_id = None if workflow is None else workflow.workflow_id
    return engine.event_manager.handle_request(
        request, result_context=ResultContext(open_workflow_id=workflow_id)
    ).result


async def _send_async(engine: Engine, request: RequestPayload, workflow: WorkflowRuntime | None) -> ResultPayload:
    workflow_id = None if workflow is None else workflow.workflow_id
    return (
        await engine.event_manager.ahandle_request(request, result_context=ResultContext(open_workflow_id=workflow_id))
    ).result


def _make_canvas(engine: Engine, workflow: WorkflowRuntime, flow_name: str = "canvas") -> None:
    result = _send(engine, CreateFlowRequest(parent_flow_name=None, flow_name=flow_name), workflow)
    assert isinstance(result, CreateFlowResultSuccess), result.result_details


def _make_node(engine: Engine, workflow: WorkflowRuntime, node_type: str, name: str, flow: str = "canvas") -> None:
    result = _send(
        engine, CreateNodeRequest(node_type=node_type, node_name=name, override_parent_flow_name=flow), workflow
    )
    assert isinstance(result, CreateNodeResultSuccess), result.result_details
    assert result.node_name == name


def _set(engine: Engine, workflow: WorkflowRuntime, node: str, value: str, parameter: str = "value") -> None:
    result = _send(engine, SetParameterValueRequest(node_name=node, parameter_name=parameter, value=value), workflow)
    assert result.succeeded(), result.result_details


def _get(engine: Engine, workflow: WorkflowRuntime, node: str, parameter: str = "value") -> Any:
    result = _send(engine, GetParameterValueRequest(node_name=node, parameter_name=parameter), workflow)
    assert isinstance(result, GetParameterValueResultSuccess), result.result_details
    return result.value


def _nodes_in(engine: Engine, workflow: WorkflowRuntime, flow: str = "canvas") -> list[str]:
    result = _send(engine, ListNodesInFlowRequest(flow_name=flow), workflow)
    assert isinstance(result, ListNodesInFlowResultSuccess), result.result_details
    return sorted(result.node_names)


def _failed_for_no_workflow_named(result: ResultPayload) -> bool:
    return isinstance(getattr(result, "exception", None), WorkflowNotNamedError) or "did not name one" in str(
        result.result_details
    )


def _park(name: str) -> asyncio.Event:
    """Make the `_ParkNode` named `name` wait when it runs, and return the event that releases it."""
    _ParkNode.gates[name] = asyncio.Event()
    _ParkNode.started[name] = asyncio.Event()
    return _ParkNode.gates[name]


async def _start_parked_run(engine: Engine, workflow: WorkflowRuntime, node: str) -> asyncio.Task[ResultPayload]:
    """Start a run of `node` in `workflow` through dispatch, and wait until the node is parked in it."""
    run = asyncio.create_task(_send_async(engine, StartFlowRequest(flow_name="canvas", flow_node_name=node), workflow))
    await asyncio.wait_for(_ParkNode.started[node].wait(), 5)
    return run


@pytest.fixture
def two_open(engine: Engine, spike_library: None) -> tuple[WorkflowRuntime, WorkflowRuntime]:  # noqa: ARG001
    """Workflows A and B open at once, each with a top-level flow named "canvas"."""
    _allow_several_open(engine)
    a = _open(engine, "A")
    b = _open(engine, "B")
    _make_canvas(engine, a)
    _make_canvas(engine, b)
    return a, b


class TestEachWorkflowSeesOnlyItsOwn:
    """Scenario (a)."""

    def test_same_names_in_both_workflows_stay_apart(
        self, engine: Engine, two_open: tuple[WorkflowRuntime, WorkflowRuntime]
    ) -> None:
        a, b = two_open
        for workflow in (a, b):
            _make_node(engine, workflow, "_ValueNode", "n")
        _make_node(engine, a, "_ValueNode", "only_in_a")
        _set(engine, a, "n", "from A")
        _set(engine, b, "n", "from B")

        assert _get(engine, a, "n") == "from A"
        assert _get(engine, b, "n") == "from B"
        assert _nodes_in(engine, a) == ["n", "only_in_a"]
        assert _nodes_in(engine, b) == ["n"]
        assert not _send(engine, GetAllNodeInfoRequest(node_name="only_in_a"), b).succeeded()
        assert a.objects.has("only_in_a")
        assert not b.objects.has("only_in_a")
        assert a.nodes.name_to_parent_flow_name == {"n": "canvas", "only_in_a": "canvas"}
        assert b.nodes.name_to_parent_flow_name == {"n": "canvas"}

    def test_connections_stay_in_their_workflow(
        self, engine: Engine, two_open: tuple[WorkflowRuntime, WorkflowRuntime]
    ) -> None:
        a, b = two_open
        for workflow in (a, b):
            _make_node(engine, workflow, "_ValueNode", "source")
            _make_node(engine, workflow, "_ValueNode", "target")
        connected = _send(
            engine,
            CreateConnectionRequest(
                source_node_name="source",
                source_parameter_name="value",
                target_node_name="target",
                target_parameter_name="value",
            ),
            a,
        )
        assert isinstance(connected, CreateConnectionResultSuccess), connected.result_details

        in_a = _send(engine, ListConnectionsForNodeRequest(node_name="target"), a)
        in_b = _send(engine, ListConnectionsForNodeRequest(node_name="target"), b)

        assert isinstance(in_a, ListConnectionsForNodeResultSuccess)
        assert isinstance(in_b, ListConnectionsForNodeResultSuccess)
        assert [c.source_node_name for c in in_a.incoming_connections] == ["source"]
        assert in_b.incoming_connections == []
        assert a.flows.connections is not b.flows.connections

    def test_variables_stay_in_their_workflow(
        self, engine: Engine, two_open: tuple[WorkflowRuntime, WorkflowRuntime]
    ) -> None:
        a, b = two_open
        for workflow, value in ((a, "a"), (b, "b")):
            created = _send(
                engine, CreateVariableRequest(name="greeting", type="str", is_global=True, value=value), workflow
            )
            assert isinstance(created, CreateVariableResultSuccess), created.result_details
        flow_var = _send(engine, CreateVariableRequest(name="local", type="str", owning_flow="canvas", value="x"), a)
        assert flow_var.succeeded(), flow_var.result_details

        for workflow, value in ((a, "a"), (b, "b")):
            got = _send(
                engine, GetVariableValueRequest(name="greeting", lookup_scope=VariableScope.GLOBAL_ONLY), workflow
            )
            assert isinstance(got, GetVariableValueResultSuccess), got.result_details
            assert got.value == value
        assert "canvas" in a.variables.flow_layers
        assert "canvas" not in b.variables.flow_layers

    def test_context_is_per_workflow(self, engine: Engine, two_open: tuple[WorkflowRuntime, WorkflowRuntime]) -> None:
        a, b = two_open
        extra = _send(engine, CreateFlowRequest(parent_flow_name=None, flow_name="second", set_as_new_context=True), a)
        assert extra.succeeded(), extra.result_details

        context_a = _send(engine, GetWorkflowContextRequest(), a)
        context_b = _send(engine, GetWorkflowContextRequest(), b)
        top_b = _send(engine, GetTopLevelFlowRequest(), b)

        assert isinstance(context_a, GetWorkflowContextSuccess)
        assert isinstance(context_b, GetWorkflowContextSuccess)
        assert context_a.workflow_id == a.workflow_id
        assert context_b.workflow_id == b.workflow_id
        assert a.context.get_current_flow().name == "second"
        assert b.context.get_current_flow().name == "canvas"
        assert isinstance(top_b, GetTopLevelFlowResultSuccess)
        assert top_b.flow_name == "canvas"


class TestRunInOneEditTheOther:
    """Scenario (b)."""

    @pytest.mark.asyncio
    async def test_editing_b_while_a_is_parked_mid_run_leaves_each_with_its_own_results(
        self, engine: Engine, two_open: tuple[WorkflowRuntime, WorkflowRuntime]
    ) -> None:
        a, b = two_open
        _make_node(engine, a, "_ParkNode", "park")
        _set(engine, a, "park", "a")
        _make_node(engine, b, "_ValueNode", "existing")
        _set(engine, b, "existing", "untouched")
        gate = _park("park")
        run = await _start_parked_run(engine, a, "park")

        created = await _send_async(engine, CreateNodeRequest(node_type="_ValueNode", node_name="added"), b)
        set_while_running = await _send_async(
            engine, SetParameterValueRequest(node_name="added", parameter_name="value", value="b edit"), b
        )
        b_running = await _send_async(engine, GetIsFlowRunningRequest(flow_name="canvas"), b)
        a_running = await _send_async(engine, GetIsFlowRunningRequest(flow_name="canvas"), a)
        gate.set()
        result = await asyncio.wait_for(run, 5)

        assert isinstance(created, CreateNodeResultSuccess), created.result_details
        assert set_while_running.succeeded(), set_while_running.result_details
        assert isinstance(b_running, GetIsFlowRunningResultSuccess)
        assert isinstance(a_running, GetIsFlowRunningResultSuccess)
        assert b_running.is_running is False
        assert a_running.is_running is True
        assert isinstance(result, StartFlowResultSuccess), result.result_details
        assert _ParkNode.ran_in["park"] is a
        assert _get(engine, a, "park", "result") == "done:a"
        assert _nodes_in(engine, a) == ["park"]
        assert _nodes_in(engine, b) == ["added", "existing"]
        assert _get(engine, b, "added") == "b edit"
        assert _get(engine, b, "existing") == "untouched"
        assert b.flows.global_control_flow_machine is None

    @pytest.mark.asyncio
    async def test_both_workflows_can_run_at_once(
        self, engine: Engine, two_open: tuple[WorkflowRuntime, WorkflowRuntime]
    ) -> None:
        a, b = two_open
        for workflow, value in ((a, "a"), (b, "b")):
            _make_node(engine, workflow, "_ParkNode", f"park_{value}")
            _set(engine, workflow, f"park_{value}", value)
        gate_a = _park("park_a")
        gate_b = _park("park_b")
        run_a = await _start_parked_run(engine, a, "park_a")
        run_b = await _start_parked_run(engine, b, "park_b")

        gate_b.set()
        result_b = await asyncio.wait_for(run_b, 5)
        gate_a.set()
        result_a = await asyncio.wait_for(run_a, 5)

        assert isinstance(result_a, StartFlowResultSuccess), result_a.result_details
        assert isinstance(result_b, StartFlowResultSuccess), result_b.result_details
        assert _ParkNode.ran_in == {"park_a": a, "park_b": b}
        assert _get(engine, a, "park_a", "result") == "done:a"
        assert _get(engine, b, "park_b", "result") == "done:b"


class TestCloseOneWhileTheOtherRuns:
    """Scenario (c)."""

    @pytest.mark.asyncio
    async def test_closing_b_while_a_runs_leaves_a_running_with_its_state(
        self, engine: Engine, two_open: tuple[WorkflowRuntime, WorkflowRuntime]
    ) -> None:
        a, b = two_open
        _make_node(engine, a, "_ParkNode", "park")
        _set(engine, a, "park", "a")
        _make_node(engine, b, "_ValueNode", "doomed")
        gate = _park("park")
        run = await _start_parked_run(engine, a, "park")

        engine.close_workflow(b)
        still_running = await _send_async(engine, GetIsFlowRunningRequest(flow_name="canvas"), a)
        gate.set()
        result = await asyncio.wait_for(run, 5)

        assert engine.open_workflows.all_open() == [a]
        assert isinstance(still_running, GetIsFlowRunningResultSuccess)
        assert still_running.is_running is True
        assert isinstance(result, StartFlowResultSuccess), result.result_details
        assert _get(engine, a, "park", "result") == "done:a"
        assert len(b.objects) == 0

    @pytest.mark.asyncio
    async def test_closing_a_while_b_is_open_and_running_leaves_b(
        self, engine: Engine, two_open: tuple[WorkflowRuntime, WorkflowRuntime]
    ) -> None:
        a, b = two_open
        _make_node(engine, a, "_ValueNode", "doomed")
        _make_node(engine, b, "_ParkNode", "park")
        _set(engine, b, "park", "b")
        gate = _park("park")
        run = await _start_parked_run(engine, b, "park")

        engine.close_workflow(a)
        gate.set()
        result = await asyncio.wait_for(run, 5)

        assert engine.open_workflows.all_open() == [b]
        assert isinstance(result, StartFlowResultSuccess), result.result_details
        assert _get(engine, b, "park", "result") == "done:b"

    def test_clear_all_addressed_to_b_closes_only_b(
        self, engine: Engine, two_open: tuple[WorkflowRuntime, WorkflowRuntime]
    ) -> None:
        a, b = two_open
        _make_node(engine, a, "_ValueNode", "survivor")

        result = _send(engine, ClearAllObjectStateRequest(i_know_what_im_doing=True), b)

        assert result.succeeded(), result.result_details
        assert engine.open_workflows.all_open() == [a]
        assert a.objects.has("survivor")


# Requests the editor plausibly sends with no envelope id, each paired with whether it still works with
# two workflows open. Ordered so the destructive ones come last; every one either fails or acts on
# host state, so none of them changes A or B.
_NO_ID_REQUESTS: list[tuple[str, Callable[[], RequestPayload]]] = [
    ("EngineHeartbeatRequest", lambda: EngineHeartbeatRequest(heartbeat_id="hb")),
    ("GetEngineVersionRequest", GetEngineVersionRequest),
    ("ListRegisteredLibrariesRequest", ListRegisteredLibrariesRequest),
    ("ListAllWorkflowsRequest", ListAllWorkflowsRequest),
    ("ListAllWorkflowInfoRequest", ListAllWorkflowInfoRequest),
    ("ListCallableWorkflowsRequest", ListCallableWorkflowsRequest),
    ("GetConfigCategoryRequest", GetConfigCategoryRequest),
    ("GetWorkspaceRequest", GetWorkspaceRequest),
    ("ListBetaFeaturesRequest", ListBetaFeaturesRequest),
    ("GetCurrentProjectRequest", GetCurrentProjectRequest),
    ("ListProjectTemplatesRequest", ListProjectTemplatesRequest),
    ("ResolveMacroPathRequest {workspace_dir}", lambda: _resolve("{workspace_dir}/x.png")),
    ("ResolveMacroPathRequest {workflow_dir}", lambda: _resolve("{workflow_dir}/x.png")),
    ("GetWorkflowContextRequest", GetWorkflowContextRequest),
    ("ListFlowsInCurrentContextRequest", ListFlowsInCurrentContextRequest),
    ("GetTopLevelFlowRequest", GetTopLevelFlowRequest),
    ("ListNodesInFlowRequest", lambda: ListNodesInFlowRequest(flow_name="canvas")),
    ("GetAllNodeInfoRequest", lambda: GetAllNodeInfoRequest(node_name="n")),
    ("ListParametersOnNodeRequest", lambda: ListParametersOnNodeRequest(node_name="n")),
    ("GetNodeResolutionStateRequest", lambda: GetNodeResolutionStateRequest(node_name="n")),
    ("GetParameterValueRequest", lambda: GetParameterValueRequest(node_name="n", parameter_name="value")),
    ("ListConnectionsForNodeRequest", lambda: ListConnectionsForNodeRequest(node_name="n")),
    ("GetIsFlowRunningRequest", lambda: GetIsFlowRunningRequest(flow_name="canvas")),
    ("GetFlowStateRequest", lambda: GetFlowStateRequest(flow_name="canvas")),
    ("ListVariablesRequest", ListVariablesRequest),
    ("GetVariableSubstitutionEnabledRequest", GetVariableSubstitutionEnabledRequest),
    ("GetWorkflowRunCommandRequest", GetWorkflowRunCommandRequest),
    (
        "SetParameterValueRequest",
        lambda: SetParameterValueRequest(node_name="n", parameter_name="value", value="unnamed"),
    ),
    ("CreateNodeRequest", lambda: CreateNodeRequest(node_type="_ValueNode")),
    ("CreateFlowRequest", lambda: CreateFlowRequest(parent_flow_name=None)),
    ("CreateVariableRequest", lambda: CreateVariableRequest(name="v", type="str", is_global=True, value="x")),
    ("RenameObjectRequest", lambda: RenameObjectRequest(object_name="n", requested_name="m")),
    ("StartFlowRequest", lambda: StartFlowRequest(flow_name="canvas")),
    ("CancelFlowRequest", lambda: CancelFlowRequest(flow_name="canvas")),
    ("SaveWorkflowRequest", SaveWorkflowRequest),
    ("DeleteNodeRequest", lambda: DeleteNodeRequest(node_name="n")),
    ("EnsureWorkflowAndFlowRequest", lambda: EnsureWorkflowAndFlowRequest(display_name="C")),
    ("SetWorkflowContextRequest", lambda: SetWorkflowContextRequest(display_name="C")),
]

# What the sweep found: the requests that fail with WorkflowNotNamedError when no id is sent.
_FAIL_WITHOUT_AN_ID = {
    "ResolveMacroPathRequest {workflow_dir}",
    "GetWorkflowContextRequest",
    "ListFlowsInCurrentContextRequest",
    "GetTopLevelFlowRequest",
    "ListNodesInFlowRequest",
    "GetAllNodeInfoRequest",
    "ListParametersOnNodeRequest",
    "GetNodeResolutionStateRequest",
    "GetParameterValueRequest",
    "ListConnectionsForNodeRequest",
    "GetIsFlowRunningRequest",
    "GetFlowStateRequest",
    "ListVariablesRequest",
    "GetVariableSubstitutionEnabledRequest",
    "GetWorkflowRunCommandRequest",
    "SetParameterValueRequest",
    "CreateNodeRequest",
    "CreateFlowRequest",
    "CreateVariableRequest",
    "RenameObjectRequest",
    "StartFlowRequest",
    "CancelFlowRequest",
    "SaveWorkflowRequest",
    "DeleteNodeRequest",
    "EnsureWorkflowAndFlowRequest",
    "SetWorkflowContextRequest",
}


class TestRequestsWithNoEnvelopeId:
    """Scenario (d)."""

    def test_host_requests_with_no_id_work_while_two_are_open(
        self,
        engine: Engine,
        two_open: tuple[WorkflowRuntime, WorkflowRuntime],  # noqa: ARG002
    ) -> None:
        heartbeat = _send(engine, EngineHeartbeatRequest(heartbeat_id="hb"), None)
        version = _send(engine, GetEngineVersionRequest(), None)

        assert heartbeat.succeeded(), heartbeat.result_details
        assert version.succeeded(), version.result_details

    def test_which_requests_fail_without_an_id_while_two_are_open(
        self,
        engine: Engine,
        two_open: tuple[WorkflowRuntime, WorkflowRuntime],
        tmp_path: Path,
    ) -> None:
        """Which handlers need a named workflow. Dispatch's queued-changes flush needs none."""
        a, b = two_open
        engine.config_manager.workspace_path = tmp_path
        for workflow in (a, b):
            _make_node(engine, workflow, "_ValueNode", "n")
        outcomes: dict[str, str] = {}

        for label, make_request in _NO_ID_REQUESTS:
            result = _send(engine, make_request(), None)
            if _failed_for_no_workflow_named(result):
                outcomes[label] = "not named"
            elif result.succeeded():
                outcomes[label] = "ok"
            else:
                outcomes[label] = f"other failure: {result.result_details}"

        failed = {label for label, outcome in outcomes.items() if outcome == "not named"}
        other = {label: outcome for label, outcome in outcomes.items() if outcome.startswith("other")}
        assert other == {}
        assert failed == _FAIL_WITHOUT_AN_ID
        # Nothing an unnamed request did reached either workflow.
        assert engine.open_workflows.all_open() == [a, b]
        assert _get(engine, a, "n") == ""
        assert _get(engine, b, "n") == ""

    def test_clear_all_with_no_id_closes_every_open_workflow(
        self, engine: Engine, two_open: tuple[WorkflowRuntime, WorkflowRuntime]
    ) -> None:
        a, b = two_open
        _make_node(engine, a, "_ValueNode", "n")
        _make_node(engine, b, "_ValueNode", "n")

        result = _send(engine, ClearAllObjectStateRequest(i_know_what_im_doing=True), None)

        assert result.succeeded(), result.result_details
        assert engine.open_workflows.all_open() == []
        assert len(a.objects) == 0
        assert len(b.objects) == 0

    def test_internal_requests_inherit_the_binding_of_the_request_that_sent_them(
        self, engine: Engine, two_open: tuple[WorkflowRuntime, WorkflowRuntime]
    ) -> None:
        """Engine.handle_request (what handlers and nodes call) sends no envelope id, but keeps the caller's binding."""
        _a, b = two_open
        _make_node(engine, b, "_ValueNode", "n")

        with engine.bind_workflow(b):
            result = engine.handle_request(GetParameterValueRequest(node_name="n", parameter_name="value"))

        assert result.succeeded(), result.result_details


class TestEventsNameTheirWorkflow:
    """Scenario (e)."""

    @pytest.mark.asyncio
    async def test_results_of_addressed_requests_name_their_workflow(
        self, engine: Engine, two_open: tuple[WorkflowRuntime, WorkflowRuntime]
    ) -> None:
        a, _ = two_open
        _make_node(engine, a, "_ValueNode", "n")

        event = await engine.event_manager.ahandle_request(
            SetParameterValueRequest(node_name="n", parameter_name="value", value="x"),
            result_context=ResultContext(open_workflow_id=a.workflow_id),
        )

        assert event.open_workflow_id == a.workflow_id

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "FINDING e1: execution events (ExecutionEvent) and ProgressEvent "
            "carry no workflow_id, only node names, and node names repeat across "
            "workflows. Results of requests a run makes internally (Engine.handle_request) echo no "
            "open_workflow_id either. The editor cannot route a run's events to its tab."
        ),
    )
    @pytest.mark.asyncio
    async def test_events_emitted_during_a_run_name_their_workflow(
        self, engine: Engine, two_open: tuple[WorkflowRuntime, WorkflowRuntime]
    ) -> None:
        a, _ = two_open
        _make_node(engine, a, "_ParkNode", "park")
        queue: asyncio.Queue = asyncio.Queue()
        engine.event_manager.initialize_queue(queue)

        result = await _send_async(engine, StartFlowRequest(flow_name="canvas", flow_node_name="park"), a)
        assert isinstance(result, StartFlowResultSuccess), result.result_details
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())

        execution_events = [e for e in events if isinstance(e, ExecutionGriptapeNodeEvent)]
        result_events = [e for e in events if isinstance(e, GriptapeNodeEvent)]
        assert execution_events, "the run emitted no execution events"
        assert "workflow_id" in type(execution_events[0].wrapped_event).model_fields
        assert "workflow_id" in ProgressEvent.__dataclass_fields__
        assert all(e.wrapped_event.open_workflow_id == a.workflow_id for e in result_events)


class TestHeldObjects:
    """Scenario (f)."""

    def test_closing_b_keeps_the_objects_held_for_a(
        self, engine: Engine, two_open: tuple[WorkflowRuntime, WorkflowRuntime]
    ) -> None:
        a, b = two_open
        released: list[str] = []
        resources = engine.resource_manager
        # Held while each workflow's node runs, which is bound to it.
        with engine.bind_workflow(a):
            key_a = resources.put_local_object(
                "model for A", owner="Lib", source="node_in_A", key="a", on_drop=lambda _v: released.append("A")
            )
        with engine.bind_workflow(b):
            resources.put_local_object(
                "model for B", owner="Lib", source="node_in_B", key="b", on_drop=lambda _v: released.append("B")
            )

        engine.close_workflow(b)

        assert engine.open_workflows.all_open() == [a]
        assert released == ["B"]
        assert resources.get_local_object(key_a, owner="Lib") == "model for A"

    def test_clear_all_addressed_to_b_tells_the_workers_only_b_closed(
        self, engine: Engine, two_open: tuple[WorkflowRuntime, WorkflowRuntime]
    ) -> None:
        _a, b = two_open
        worker_manager = engine.worker_manager

        with patch.object(worker_manager, "broadcast_drop_all_local_objects", AsyncMock()) as drop_all:
            result = _send(engine, ClearAllObjectStateRequest(i_know_what_im_doing=True), b)

        assert result.succeeded(), result.result_details
        assert drop_all.await_args is not None
        assert drop_all.await_args.kwargs == {"workflow_id": b.workflow_id}

    def test_clear_all_addressed_to_the_only_open_workflow_keeps_the_objects_held_for_no_workflow(
        self, engine: Engine, two_open: tuple[WorkflowRuntime, WorkflowRuntime]
    ) -> None:
        a, b = two_open
        engine.close_workflow(a)
        released: list[str] = []
        resources = engine.resource_manager
        with engine.bind_workflow(NO_WORKFLOW):
            resources.put_local_object(
                "shared model", owner="Lib", source="host", key="h", on_drop=lambda _v: released.append("host")
            )
        with engine.bind_workflow(b):
            resources.put_local_object(
                "model for B", owner="Lib", source="node_in_B", key="b", on_drop=lambda _v: released.append("B")
            )

        with patch.object(engine.worker_manager, "broadcast_drop_all_local_objects", AsyncMock()) as drop_all:
            result = _send(engine, ClearAllObjectStateRequest(i_know_what_im_doing=True), b)

        assert result.succeeded(), result.result_details
        assert engine.open_workflows.all_open() == []
        assert released == ["B"]
        assert drop_all.await_args is not None
        assert drop_all.await_args.kwargs == {"workflow_id": b.workflow_id}

    def test_clear_all_with_no_id_releases_every_held_object_and_tells_the_workers_to(
        self, engine: Engine, two_open: tuple[WorkflowRuntime, WorkflowRuntime]
    ) -> None:
        a, b = two_open
        released: list[str] = []
        resources = engine.resource_manager
        for workflow, label in ((a, "A"), (b, "B"), (NO_WORKFLOW, "host")):
            with engine.bind_workflow(workflow):
                resources.put_local_object(label, owner="Lib", source=label, key=label, on_drop=released.append)

        with patch.object(engine.worker_manager, "broadcast_drop_all_local_objects", AsyncMock()) as drop_all:
            result = _send(engine, ClearAllObjectStateRequest(i_know_what_im_doing=True), None)

        assert result.succeeded(), result.result_details
        assert engine.open_workflows.all_open() == []
        assert sorted(released) == ["A", "B", "host"]
        assert drop_all.await_args is not None
        assert drop_all.await_args.kwargs == {"workflow_id": None}


def _worker_request(node_name: str, orchestrator_workflow_id: str, value: str) -> ExecuteNodeRequest:
    return ExecuteNodeRequest(
        node_name=node_name,
        node_metadata=cast("ExecutionNodeMetadata", {"node_type": "_ParkNode", "library": _LIBRARY_NAME}),
        parameter_values={"value": value},
        orchestrator_workflow_id=orchestrator_workflow_id,
    )


_ORCHESTRATOR_A = "0000000000000000000000000000000a"
_ORCHESTRATOR_B = "0000000000000000000000000000000b"


@pytest.fixture
def worker_engine(engine: Engine, spike_library: None) -> Engine:  # noqa: ARG001
    """The test's engine, in the role of a worker."""
    engine.library_manager._is_worker = True
    return engine


class TestWorkerForTwoOrchestratorWorkflows:
    """Scenario (g)."""

    @pytest.mark.asyncio
    async def test_a_worker_answers_nodes_for_a_second_workflow(self, worker_engine: Engine) -> None:
        first = await worker_engine.ahandle_request(_worker_request("p", _ORCHESTRATOR_A, "a"))
        second = await worker_engine.ahandle_request(_worker_request("p", _ORCHESTRATOR_B, "b"))

        assert isinstance(first, ExecuteNodeResultSuccess), first.result_details
        assert isinstance(second, ExecuteNodeResultSuccess), second.result_details

    @pytest.mark.asyncio
    async def test_nodes_for_two_workflows_one_after_the_other_run_in_their_own_runtimes(
        self, worker_engine: Engine
    ) -> None:
        first = await worker_engine.ahandle_request(_worker_request("p", _ORCHESTRATOR_A, "a"))
        ran_first = _ParkNode.ran_in["p"]
        second = await worker_engine.ahandle_request(_worker_request("p", _ORCHESTRATOR_B, "b"))
        ran_second = _ParkNode.ran_in["p"]

        assert isinstance(first, ExecuteNodeResultSuccess), first.result_details
        assert isinstance(second, ExecuteNodeResultSuccess), second.result_details
        assert first.parameter_output_values["result"] == "done:a"
        assert second.parameter_output_values["result"] == "done:b"
        assert ran_first.workflow_id == _ORCHESTRATOR_A
        assert ran_second.workflow_id == _ORCHESTRATOR_B
        assert ran_first is not ran_second

    @pytest.mark.asyncio
    async def test_interleaved_nodes_for_two_workflows_each_run_in_their_own_runtime(
        self, worker_engine: Engine
    ) -> None:
        gate_a = _park("pa")
        gate_b = _park("pb")
        run_a = asyncio.create_task(worker_engine.ahandle_request(_worker_request("pa", _ORCHESTRATOR_A, "a")))
        run_b = asyncio.create_task(worker_engine.ahandle_request(_worker_request("pb", _ORCHESTRATOR_B, "b")))
        await asyncio.wait_for(asyncio.gather(_ParkNode.started["pa"].wait(), _ParkNode.started["pb"].wait()), 5)

        kept_while_both_run = {w.workflow_id for w in worker_engine.worker_workflows.all_kept()}
        gate_b.set()
        result_b = await asyncio.wait_for(run_b, 5)
        gate_a.set()
        result_a = await asyncio.wait_for(run_a, 5)

        assert kept_while_both_run == {_ORCHESTRATOR_A, _ORCHESTRATOR_B}
        assert isinstance(result_a, ExecuteNodeResultSuccess), result_a.result_details
        assert isinstance(result_b, ExecuteNodeResultSuccess), result_b.result_details
        assert result_a.parameter_output_values["result"] == "done:a"
        assert result_b.parameter_output_values["result"] == "done:b"
        assert _ParkNode.ran_in["pa"].workflow_id == _ORCHESTRATOR_A
        assert _ParkNode.ran_in["pb"].workflow_id == _ORCHESTRATOR_B

    @pytest.mark.asyncio
    async def test_the_teardown_for_closing_a_leaves_bs_runtime_and_objects(self, worker_engine: Engine) -> None:
        await worker_engine.ahandle_request(_worker_request("pa", _ORCHESTRATOR_A, "a"))
        await worker_engine.ahandle_request(_worker_request("pb", _ORCHESTRATOR_B, "b"))
        runtime_a = worker_engine.worker_workflows.find(_ORCHESTRATOR_A)
        runtime_b = worker_engine.worker_workflows.find(_ORCHESTRATOR_B)
        assert runtime_a is not None
        assert runtime_b is not None
        resources = worker_engine.resource_manager
        # Held while each workflow's node runs, which is bound to its runtime.
        with worker_engine.run_in_worker_workflow(runtime_a):
            key_a = resources.put_local_object("A's model", owner="Lib", source="pa", key="a")
        with worker_engine.run_in_worker_workflow(runtime_b):
            key_b = resources.put_local_object("B's model", owner="Lib", source="pb", key="b")

        # The orchestrator closes A and tells its workers which workflow closed.
        await _handle_drop_all_local_objects(
            DropAllLocalObjectsRequest(workflow_id=_ORCHESTRATOR_A), event_manager=worker_engine.event_manager
        )

        assert worker_engine.worker_workflows.find(_ORCHESTRATOR_A) is None
        assert worker_engine.worker_workflows.find(_ORCHESTRATOR_B) is runtime_b
        assert resources.get_local_object(key_a, owner="Lib") is None
        assert resources.get_local_object(key_b, owner="Lib") == "B's model"

    def test_unbound_code_on_a_worker_holding_two_runtimes_finds_no_workflow(self, worker_engine: Engine) -> None:
        """A worker's node work is bound to its runtime; unbound work finds none, however many it keeps."""
        for workflow_id in (_ORCHESTRATOR_A, _ORCHESTRATOR_B):
            worker_engine.context_manager.adopt_orchestrator_workflow(workflow_id, None, None, None)

        assert worker_engine.current_workflow_or_none() is None
        assert not worker_engine.context_manager.has_current_workflow()


class TestRawThreadInNodeCode:
    """Scenario (h)."""

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "FINDING h1: a plain threading.Thread does not copy the ContextVar binding, "
            "so node code in one is unbound; with two workflows open "
            "Engine.current_workflow_or_none raises WorkflowNotNamedError and "
            "{workflow_dir} fails to resolve. asyncio.to_thread copies the context and works."
        ),
    )
    @pytest.mark.asyncio
    async def test_node_code_in_a_raw_thread_finds_its_workflow(
        self, engine: Engine, two_open: tuple[WorkflowRuntime, WorkflowRuntime]
    ) -> None:
        a, _ = two_open
        _make_node(engine, a, "_RawThreadNode", "threaded")

        result = await _send_async(engine, StartFlowRequest(flow_name="canvas", flow_node_name="threaded"), a)

        assert isinstance(result, StartFlowResultSuccess), result.result_details
        assert _RawThreadNode.seen["to_thread_workflow"] is a
        assert _RawThreadNode.seen["workflow"] is a
        assert _RawThreadNode.seen["workflow_dir"].succeeded()

    @pytest.mark.asyncio
    async def test_with_one_workflow_open_a_raw_thread_falls_back_to_it(
        self,
        engine: Engine,
        spike_library: None,  # noqa: ARG002
    ) -> None:
        only = _open(engine, "only")
        _make_canvas(engine, only)
        _make_node(engine, only, "_RawThreadNode", "threaded")

        result = await _send_async(engine, StartFlowRequest(flow_name="canvas", flow_node_name="threaded"), only)

        assert isinstance(result, StartFlowResultSuccess), result.result_details
        assert _RawThreadNode.seen["workflow"] is only
        assert _RawThreadNode.seen["workflow_dir"].succeeded()


def _use_default_project(engine: Engine, workspace: Path) -> None:
    project_yml = workspace / "project_template.yml"
    project_yml.write_text(DEFAULT_PROJECT_TEMPLATE.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE))
    load_result = engine.handle_request(LoadProjectTemplateRequest(project_path=project_yml))
    assert isinstance(load_result, LoadProjectTemplateResultSuccess)
    engine.handle_request(SetCurrentProjectRequest(project_id=load_result.project_id))
    engine.config_manager.workspace_path = workspace


@pytest.fixture
def saved_workflow_name(engine: Engine, spike_library: None, tmp_path: Path) -> Iterator[str]:  # noqa: ARG001
    """A saved workflow file in a fresh workspace, saved and closed while only one workflow could be open."""
    workspace = tmp_path.resolve() / "workspace"
    workspace.mkdir()
    _use_default_project(engine, workspace)
    opened = engine.handle_request(EnsureWorkflowAndFlowRequest(display_name="saved"))
    assert isinstance(opened, EnsureWorkflowAndFlowResultSuccess), opened.result_details
    saved = asyncio.run(engine.ahandle_request(SaveWorkflowRequest()))
    assert isinstance(saved, SaveWorkflowResultSuccess), saved.result_details
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    assert engine.open_workflows.all_open() == []
    yield saved.workflow_name
    engine.handle_request(SetCurrentProjectRequest(project_id=None))


class TestLoadingASavedFileWhileAnotherIsOpen:
    """Scenario (i)."""

    def test_a_clean_slate_load_into_b_leaves_a_open(self, engine: Engine, saved_workflow_name: str) -> None:
        _allow_several_open(engine)
        a = _open(engine, "A")
        b = _open(engine, "B")
        _make_canvas(engine, a)
        _make_node(engine, a, "_ValueNode", "survivor")

        result = asyncio.run(
            _send_async(
                engine, RunWorkflowFromRegistryRequest(workflow_name=saved_workflow_name, run_with_clean_slate=True), b
            )
        )

        assert result.succeeded(), result.result_details
        open_now = engine.open_workflows.all_open()
        assert open_now[0] is a
        assert b not in open_now
        assert len(open_now) == 2  # noqa: PLR2004
        assert a.objects.has("survivor")

    def test_a_clean_slate_load_with_no_id_closes_every_open_workflow_and_loads(
        self, engine: Engine, saved_workflow_name: str
    ) -> None:
        _allow_several_open(engine)
        a = _open(engine, "A")
        b = _open(engine, "B")

        result = asyncio.run(
            _send_async(
                engine,
                RunWorkflowFromRegistryRequest(workflow_name=saved_workflow_name, run_with_clean_slate=True),
                None,
            )
        )

        assert result.succeeded(), result.result_details
        open_now = engine.open_workflows.all_open()
        assert a not in open_now
        assert b not in open_now
        assert len(open_now) == 1

    def test_loading_a_saved_file_into_a_new_tab_leaves_a_alone(self, engine: Engine, saved_workflow_name: str) -> None:
        _allow_several_open(engine)
        a = _open(engine, "A")
        _make_canvas(engine, a)

        async def load_unbound() -> ResultPayload:
            with engine.bind_workflow(NO_WORKFLOW):
                return await engine.ahandle_request(
                    RunWorkflowFromRegistryRequest(workflow_name=saved_workflow_name, run_with_clean_slate=False)
                )

        result = asyncio.run(load_unbound())

        assert result.succeeded(), result.result_details
        open_now = engine.open_workflows.all_open()
        assert open_now[0] is a
        loaded = open_now[-1]
        assert loaded is not a
        assert loaded.objects.get_filtered_subset() != {}
        assert a.objects.has("canvas")


class TestOtherSharedState:
    """Scenario (j): state shared across workflows that a run or request reads."""

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "FINDING j1: the heartbeat reports one current workflow (Engine._get_current_workflow_info), "
            "read from the engine's one open workflow. With two open it reports none."
        ),
    )
    def test_the_heartbeat_reports_an_open_workflow_while_two_are_open(
        self,
        engine: Engine,
        two_open: tuple[WorkflowRuntime, WorkflowRuntime],  # noqa: ARG002
    ) -> None:

        info = engine._get_current_workflow_info()

        assert info["current_workflow_id"] is not None

    @pytest.mark.asyncio
    async def test_a_post_dispatch_hook_sees_the_workflow_its_request_was_addressed_to(
        self, engine: Engine, two_open: tuple[WorkflowRuntime, WorkflowRuntime]
    ) -> None:
        _, b = two_open
        seen: list[Any] = []
        hook_ran = asyncio.Event()

        async def hook(_request: RequestPayload, _result: ResultPayload) -> None:
            try:
                seen.append(engine.current_workflow_or_none())
            except WorkflowNotNamedError as err:
                seen.append(err)
            hook_ran.set()

        engine.event_manager.initialize_queue(asyncio.Queue())
        engine.event_manager.add_post_dispatch_hook(GetTopLevelFlowRequest, hook)

        await _send_async(engine, GetTopLevelFlowRequest(), b)
        await asyncio.wait_for(hook_ran.wait(), 5)

        assert seen == [b]

    @pytest.mark.asyncio
    async def test_a_request_a_worker_forwards_names_the_orchestrator_workflow(self, worker_engine: Engine) -> None:
        runtime = worker_engine.context_manager.adopt_orchestrator_workflow(_ORCHESTRATOR_A, None, None, None)
        sent: list[Any] = []

        class _CapturingClient:
            async def request_to_orchestrator(self, *, event_request: Any, **_: Any) -> dict:
                sent.append(event_request)
                msg = "captured"
                raise RuntimeError(msg)

        transport_loop = asyncio.new_event_loop()
        transport_thread = threading.Thread(target=transport_loop.run_forever, daemon=True)
        transport_thread.start()
        event_manager = worker_engine.event_manager
        event_manager._worker_request_client = cast("Any", _CapturingClient())
        event_manager._orchestrator_request_topic = "orchestrator"
        event_manager._worker_response_topic = "worker"
        event_manager._websocket_event_loop = transport_loop
        try:
            with worker_engine.run_in_worker_workflow(runtime), pytest.raises(RuntimeError, match="captured"):
                await event_manager.forward_to_orchestrator(
                    GetParameterValueRequest(node_name="n", parameter_name="value"), {}
                )
        finally:
            transport_loop.call_soon_threadsafe(transport_loop.stop)
            transport_thread.join()
            transport_loop.close()

        assert sent[0].open_workflow_id == _ORCHESTRATOR_A

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "FINDING j4: DeleteWorkflowRequest closes the deleted workflow only when it is the current one. "
            "Deleting B from A's tab is refused safely instead: the "
            "registry will not remove an open workflow's entry ('Failed because it is open. Close it first')."
        ),
    )
    def test_deleting_a_workflow_open_in_another_tab_closes_it(
        self, engine: Engine, two_open: tuple[WorkflowRuntime, WorkflowRuntime]
    ) -> None:
        a, b = two_open
        b_name = engine.workflow_registry.get(b.workflow_id).name

        deleted = _send(engine, DeleteWorkflowRequest(name=b_name), a)

        assert deleted.succeeded(), deleted.result_details
        assert engine.open_workflows.all_open() == [a]

    def test_reloading_libraries_gets_past_closing_both_workflows(
        self,
        engine: Engine,
        two_open: tuple[WorkflowRuntime, WorkflowRuntime],  # noqa: ARG002
    ) -> None:

        result = _send(engine, ReloadAllLibrariesRequest(), None)

        assert "Failed to clear the existing object state" not in str(result.result_details)
        assert engine.open_workflows.all_open() == []

    def test_reloading_libraries_with_an_envelope_id_closes_every_workflow(
        self, engine: Engine, two_open: tuple[WorkflowRuntime, WorkflowRuntime]
    ) -> None:
        """A host request: the clear-all it sends names no workflow, whatever the client named."""
        a, _b = two_open

        result = _send(engine, ReloadAllLibrariesRequest(), a)

        assert "Failed to clear the existing object state" not in str(result.result_details)
        assert engine.open_workflows.all_open() == []

    def test_a_host_request_with_the_id_of_a_closed_workflow_succeeds(
        self, engine: Engine, two_open: tuple[WorkflowRuntime, WorkflowRuntime]
    ) -> None:
        a, b = two_open
        engine.close_workflow(b)

        heartbeat = _send(engine, EngineHeartbeatRequest(heartbeat_id="hb"), b)
        stale_read = _send(engine, GetParameterValueRequest(node_name="n", parameter_name="value"), b)

        assert heartbeat.succeeded(), heartbeat.result_details
        assert isinstance(stale_read, WorkflowNotOpenResultFailure)
        assert engine.open_workflows.all_open() == [a]
