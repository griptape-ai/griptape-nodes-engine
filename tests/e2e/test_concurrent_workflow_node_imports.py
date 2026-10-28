"""Workflow nodes that import their workflow at the same time each get a flow of their own.

Two Workflow nodes on parallel branches, or one inside a loop set to "Run Group Items
All at Once", import their backing workflow as concurrent tasks. Each import must land
in its own node's flow and hand its own subflow back, however the tasks interleave.

The interleaving is forced rather than left to chance: every import waits at a barrier
just before the workflow file runs, so both are open at the same time and neither has
created its flow yet.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import pytest

from griptape_nodes.exe_types.node_types import NodeResolutionState
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
)
from griptape_nodes.retained_mode.events.execution_events import StartFlowRequest, StartFlowResultSuccess
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from griptape_nodes.retained_mode.events.parameter_events import (
    SetParameterValueRequest,
    SetParameterValueResultSuccess,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    ImportWorkflowAsReferencedSubFlowRequest,
    ImportWorkflowAsReferencedSubFlowResultSuccess,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.managers.workflow.running import WorkflowExecutionResult

# Timeout with thread dump.
pytestmark = pytest.mark.timeout(60, method="thread")

WORKFLOW_NODE_DIR = Path(__file__).parent / "fixtures" / "workflow_node_library"
WORKFLOW_NODE_JSON = WORKFLOW_NODE_DIR / "griptape_nodes_library.json"
WORKFLOW_NODE_FILE = WORKFLOW_NODE_DIR / "workflow_node_nodes.py"
WORKFLOW_FILE = WORKFLOW_NODE_DIR / "shout_workflow.py"
WORKFLOW_NODE_LIBRARY = "Workflow Node Library"
WORKFLOW_NODE_TYPE = "ShoutWorkflow"

LOOP_DIR = Path(__file__).parent / "fixtures" / "loop_copy_back_library"
LOOP_JSON = LOOP_DIR / "griptape_nodes_library.json"
LOOP_FILE = LOOP_DIR / "loop_copy_back_nodes.py"
# The loop packager resolves its StartFlow/EndFlow endpoints from a library with exactly this name.
LOOP_LIBRARY = "Griptape Nodes Library"
LOOP_GROUP_TYPE = "ItemsForEachGroupNode"

BARRIER_TIMEOUT = 20.0
PARALLEL_EXECUTION_MODE = "Run Group Items All at Once"
LOOP_ITERATIONS = 2


class ImportRecord(NamedTuple):
    """Where one import was asked to put its flow and where it put it."""

    target_flow: str
    created_flow: str
    created_flow_parent: str | None


class ImportRecorder:
    """Holds the imports that finished during a test."""

    def __init__(self) -> None:
        self.records: list[ImportRecord] = []


@pytest.fixture
def registered_libraries(tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path]) -> None:
    """Register the Workflow node library and the loop library the loop test needs."""
    workflow_library_json = materialize_library(
        tmp_path / "workflow_library",
        template=WORKFLOW_NODE_JSON,
        node_file=WORKFLOW_NODE_FILE,
        extra_files=[WORKFLOW_FILE],
    )
    loop_library_json = materialize_library(
        tmp_path / "loop_library", template=LOOP_JSON, node_file=LOOP_FILE, name=LOOP_LIBRARY
    )
    for library_json in (workflow_library_json, loop_library_json):
        result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
        assert isinstance(result, RegisterLibraryFromFileResultSuccess), result


@pytest.fixture
def parallel_mode(engine: Engine) -> Iterator[None]:
    """Run independent nodes of a flow at the same time."""
    config_manager = engine.config_manager
    prior_mode = config_manager.get_config_value("workflow_execution_mode")
    config_manager.set_config_value("workflow_execution_mode", "parallel")
    try:
        yield
    finally:
        config_manager.set_config_value("workflow_execution_mode", prior_mode)


@pytest.fixture
def import_recorder(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> ImportRecorder:
    """Record every finished import's target and result, and force the imports to overlap.

    The barrier sits in `run_workflow`, which `_execute_workflow_import` calls with its
    flow context already entered. Both imports are open before either runs its file, and
    the first to arrive builds its flow while the second's context is still open, which
    is the order that mixed them up.
    """
    recorder = ImportRecorder()
    runner = engine.workflow_manager.runner
    referenced_import = engine.workflow_manager.referenced_import
    original_run_workflow = runner.run_workflow
    original_execute_import = referenced_import._execute_workflow_import
    expected_imports = 2
    arrived = 0
    all_arrived = asyncio.Event()
    first_finished = asyncio.Event()

    async def run_workflow_after_barrier(relative_file_path: str) -> WorkflowExecutionResult:
        nonlocal arrived
        arrived += 1
        is_first = arrived == 1
        if arrived >= expected_imports:
            all_arrived.set()
        await asyncio.wait_for(all_arrived.wait(), timeout=BARRIER_TIMEOUT)
        if not is_first:
            # The last to arrive holds its flow context open until the first has built
            # its flow.
            await asyncio.wait_for(first_finished.wait(), timeout=BARRIER_TIMEOUT)
        try:
            return await original_run_workflow(relative_file_path)
        finally:
            if is_first:
                first_finished.set()

    async def execute_import_and_record(
        request: ImportWorkflowAsReferencedSubFlowRequest, *args: object, **kwargs: object
    ) -> object:
        result = await original_execute_import(request, *args, **kwargs)  # type: ignore[arg-type]
        assert isinstance(result, ImportWorkflowAsReferencedSubFlowResultSuccess), result
        assert request.flow_name is not None
        parent = engine.flow_manager.get_parent_flow(result.created_flow_name)
        recorder.records.append(ImportRecord(request.flow_name, result.created_flow_name, parent))
        return result

    monkeypatch.setattr(runner, "run_workflow", run_workflow_after_barrier)
    monkeypatch.setattr(referenced_import, "_execute_workflow_import", execute_import_and_record)
    return recorder


def _set_value(engine: Engine, node_name: str, parameter_name: str, value: object) -> None:
    result = engine.handle_request(
        SetParameterValueRequest(parameter_name=parameter_name, node_name=node_name, value=value)
    )
    assert isinstance(result, SetParameterValueResultSuccess), result


def _connect(engine: Engine, source: tuple[str, str], target: tuple[str, str]) -> None:
    result = engine.handle_request(
        CreateConnectionRequest(
            source_node_name=source[0],
            source_parameter_name=source[1],
            target_node_name=target[0],
            target_parameter_name=target[1],
        )
    )
    assert isinstance(result, CreateConnectionResultSuccess), (source, target, result)


@pytest.mark.usefixtures("registered_libraries", "parallel_mode")
@pytest.mark.asyncio
async def test_workflow_nodes_on_parallel_branches_each_get_their_own_subflow(
    engine: Engine, create_node: Callable[..., str], import_recorder: ImportRecorder
) -> None:
    """Two Workflow nodes with no connection between them run together and keep separate subflows."""
    engine.context_manager.push_workflow(workflow_name="concurrent_workflow_nodes_branches")
    flow_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ParentFlow", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    parent_flow = flow_result.flow_name

    inputs = {"Left": "left", "Right": "right"}
    for node_name, text in inputs.items():
        create_node(WORKFLOW_NODE_TYPE, node_name, parent_flow, library_name=WORKFLOW_NODE_LIBRARY)
        _set_value(engine, node_name, "text", text)

    run_result = await engine.ahandle_request(StartFlowRequest(flow_name=parent_flow))
    assert isinstance(run_result, StartFlowResultSuccess), run_result

    node_manager = engine.node_manager
    subflows = {}
    for node_name, text in inputs.items():
        node = node_manager.get_node_by_name(node_name)
        assert node.state == NodeResolutionState.RESOLVED, node_name
        assert node.parameter_output_values.get("result") == f"{text.upper()}!"
        subflows[node_name] = node.metadata["subflow_name"]
        assert engine.flow_manager.get_parent_flow(subflows[node_name]) == parent_flow

    assert subflows["Left"] != subflows["Right"]
    assert len(import_recorder.records) == len(inputs)
    assert {record.created_flow for record in import_recorder.records} == set(subflows.values())
    for record in import_recorder.records:
        assert record.created_flow_parent == record.target_flow == parent_flow


@pytest.mark.usefixtures("registered_libraries")
@pytest.mark.asyncio
async def test_workflow_node_in_a_loop_run_all_at_once_gets_a_subflow_per_iteration(
    engine: Engine, import_recorder: ImportRecorder
) -> None:
    """Each iteration's Workflow node imports into its own iteration flow and returns its own result."""
    engine.context_manager.push_workflow(workflow_name="concurrent_workflow_nodes_loop")
    flow_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="Parent", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    group_name = "Loop"
    node_name = "Shout It"
    echo_name = "Echo"
    with engine.context_manager.flow(flow_result.flow_name):
        group = engine.handle_request(
            CreateNodeRequest(node_type=LOOP_GROUP_TYPE, specific_library_name=LOOP_LIBRARY, node_name=group_name)
        )
        assert isinstance(group, CreateNodeResultSuccess), group
        for node_type, library, name in (
            ("EchoNode", LOOP_LIBRARY, echo_name),
            (WORKFLOW_NODE_TYPE, WORKFLOW_NODE_LIBRARY, node_name),
        ):
            child = engine.handle_request(
                CreateNodeRequest(
                    node_type=node_type, specific_library_name=library, node_name=name, parent_group_name=group_name
                )
            )
            assert isinstance(child, CreateNodeResultSuccess), child

    # The loop hands out ints and the workflow wants text, so the echo node converts each one.
    _connect(engine, (group_name, "on_each"), (echo_name, "exec_in"))
    _connect(engine, (echo_name, "exec_out"), (node_name, "exec_in"))
    _connect(engine, (node_name, "exec_out"), (group_name, "loop_complete"))
    _connect(engine, (group_name, "current_item"), (echo_name, "value"))
    _connect(engine, (echo_name, "result"), (node_name, "text"))
    _connect(engine, (node_name, "result"), (group_name, "new_item_to_add"))
    _set_value(engine, group_name, "execution_mode", PARALLEL_EXECUTION_MODE)

    with engine.context_manager.flow(flow_result.flow_name):
        run_result = await engine.ahandle_request(
            StartFlowRequest(flow_name=flow_result.flow_name, flow_node_name=group_name)
        )
    assert isinstance(run_result, StartFlowResultSuccess), run_result

    group_node = engine.node_manager.get_node_by_name(group_name)
    assert group_node.parameter_output_values.get("results") == ["10!", "20!"]

    # The iteration flows are gone by now, so the proof is what each import did while they lived.
    assert len(import_recorder.records) == LOOP_ITERATIONS
    assert len({record.target_flow for record in import_recorder.records}) == LOOP_ITERATIONS
    assert len({record.created_flow for record in import_recorder.records}) == LOOP_ITERATIONS
    for record in import_recorder.records:
        assert record.created_flow_parent == record.target_flow
