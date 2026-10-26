"""Every node a run executes is reported to execution-event listeners as the artist placed it (#5779).

Each loop body run is reported under the canvas node's name, nested under the group's run, and the
Start and End nodes the engine adds send nothing. Covers both execution modes and nested loops.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.node_library.library_registry import LibraryRegistry
from griptape_nodes.retained_mode.events.base_events import ExecutionEvent, ExecutionGriptapeNodeEvent
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
)
from griptape_nodes.retained_mode.events.execution_events import (
    NodeFinishProcessEvent,
    NodeProcessOutcome,
    NodeStartProcessEvent,
    StartFlowRequest,
    StartFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
    UnloadLibraryFromRegistryRequest,
    UnloadLibraryFromRegistryResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from griptape_nodes.retained_mode.engine import Engine

# Timeout with thread dump.
pytestmark = pytest.mark.timeout(300, method="thread")

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "iterative_group_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "iterative_group_nodes.py"
WATCHER_LIBRARY_FILE = Path(__file__).parent / "fixtures" / "node_watcher_library" / "node_watcher_library.py"
# `flow_manager._validate_and_get_multi_node_library_info` resolves the packaged flow's
# StartFlow/EndFlow endpoints from a library with exactly this name.
LIBRARY_NAME = "Griptape Nodes Library"

OUTER_GROUP_NAME = "ForEachGroup"
INNER_GROUP_NAME = "InnerGroup"
BODY_NODE_NAME = "Body"
# `ProbeForEachGroupNode` iterates a fixed three-item list.
ITEMS_PER_LOOP = 3
EXECUTION_MODES = ["Run Group Items One at a Time", "Run Group Items All at Once"]


class RecordedNodeEvents:
    """Collects the node events a run sends, the way a library's listeners would hear them."""

    def __init__(self) -> None:
        self.events: list[NodeStartProcessEvent | NodeFinishProcessEvent] = []

    def record(self, payload: NodeStartProcessEvent | NodeFinishProcessEvent) -> None:
        self.events.append(payload)

    @property
    def starts(self) -> list[NodeStartProcessEvent]:
        return [event for event in self.events if isinstance(event, NodeStartProcessEvent)]

    @property
    def finishes(self) -> list[NodeFinishProcessEvent]:
        return [event for event in self.events if isinstance(event, NodeFinishProcessEvent)]

    def starts_of(self, node_name: str) -> list[NodeStartProcessEvent]:
        return [event for event in self.starts if event.node_name == node_name]


@pytest.fixture
def recorded_node_events(engine: Engine) -> Iterator[RecordedNodeEvents]:
    """Listen for node events on the test's engine for the length of the test."""
    recorded = RecordedNodeEvents()
    # Listeners only hear events that reach a queue, as they do in a running engine.
    engine.event_manager.initialize_queue(asyncio.Queue())
    engine.event_manager.add_listener_to_execution_event(NodeStartProcessEvent, recorded.record)
    engine.event_manager.add_listener_to_execution_event(NodeFinishProcessEvent, recorded.record)
    try:
        yield recorded
    finally:
        engine.event_manager.remove_listener_for_execution_event(NodeStartProcessEvent, recorded.record)
        engine.event_manager.remove_listener_for_execution_event(NodeFinishProcessEvent, recorded.record)


def _register_fixture_library(engine: Engine, tmp_path: Path, materialize_library: Callable[..., Path]) -> None:
    library_json = materialize_library(
        tmp_path / "library", template=FIXTURE_LIBRARY_JSON_TEMPLATE, node_file=FIXTURE_NODE_FILE, name=LIBRARY_NAME
    )
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result


def _create_node(engine: Engine, node_type: str, node_name: str, parent_group_name: str | None = None) -> str:
    result = engine.handle_request(
        CreateNodeRequest(
            node_type=node_type,
            specific_library_name=LIBRARY_NAME,
            node_name=node_name,
            parent_group_name=parent_group_name,
        )
    )
    assert isinstance(result, CreateNodeResultSuccess), result
    return result.node_name


def _connect(engine: Engine, source_node: str, source_param: str, target_node: str, target_param: str) -> None:
    result = engine.handle_request(
        CreateConnectionRequest(
            source_node_name=source_node,
            source_parameter_name=source_param,
            target_node_name=target_node,
            target_parameter_name=target_param,
        )
    )
    assert isinstance(result, CreateConnectionResultSuccess), (
        f"{source_node}.{source_param} -> {target_node}.{target_param}: {result}"
    )


def _wire_loop_body(engine: Engine, group_name: str, body_name: str) -> None:
    """Wire the body so every iteration re-executes it, as a real graph does."""
    _connect(engine, group_name, "on_each", body_name, "exec_in")
    _connect(engine, body_name, "exec_out", group_name, "loop_complete")


def _set_execution_mode(engine: Engine, group_name: str, execution_mode: str) -> None:
    engine.handle_request(
        SetParameterValueRequest(parameter_name="execution_mode", node_name=group_name, value=execution_mode)
    )


async def _run(engine: Engine, flow_name: str, start_node_name: str) -> None:
    with engine.context_manager.flow(flow_name):
        run_result = await engine.ahandle_request(StartFlowRequest(flow_name=flow_name, flow_node_name=start_node_name))
    assert isinstance(run_result, StartFlowResultSuccess), run_result


def _create_parent_flow(engine: Engine, workflow_name: str) -> str:
    engine.context_manager.push_workflow(workflow_name=workflow_name)
    parent_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ParentFlow", set_as_new_context=False)
    )
    assert isinstance(parent_result, CreateFlowResultSuccess), parent_result
    return parent_result.flow_name


def _assert_every_run_paired_and_succeeded(recorded: RecordedNodeEvents) -> None:
    starts_by_id = {event.execution_id: event for event in recorded.starts}
    finishes_by_id = {event.execution_id: event for event in recorded.finishes}
    assert len(starts_by_id) == len(recorded.starts), "two runs shared an execution_id"
    assert starts_by_id.keys() == finishes_by_id.keys(), "a run started without finishing, or the reverse"
    for execution_id, finish in finishes_by_id.items():
        start = starts_by_id[execution_id]
        assert finish.node_name == start.node_name
        assert finish.parent_execution_id == start.parent_execution_id
        assert finish.outcome == NodeProcessOutcome.SUCCEEDED, finish


@pytest.mark.parametrize("execution_mode", EXECUTION_MODES)
@pytest.mark.asyncio
async def test_loop_body_runs_are_reported_as_the_canvas_node(
    tmp_path: Path,
    engine: Engine,
    materialize_library: Callable[..., Path],
    recorded_node_events: RecordedNodeEvents,
    execution_mode: str,
) -> None:
    """Each iteration's body run names the canvas node and nests under the group's run."""
    _register_fixture_library(engine, tmp_path, materialize_library)
    parent_flow = _create_parent_flow(engine, "node_process_events_wf")
    with engine.context_manager.flow(parent_flow):
        group_name = _create_node(engine, "ProbeForEachGroupNode", OUTER_GROUP_NAME)
        body_name = _create_node(engine, "LoopBodyControlNode", BODY_NODE_NAME, parent_group_name=group_name)
    _wire_loop_body(engine, group_name, body_name)
    _set_execution_mode(engine, group_name, execution_mode)

    await _run(engine, parent_flow, group_name)

    assert Counter(event.node_name for event in recorded_node_events.starts) == {
        group_name: 1,
        body_name: ITEMS_PER_LOOP,
    }, f"a node other than the canvas's ran under its own name ({execution_mode})"
    _assert_every_run_paired_and_succeeded(recorded_node_events)

    [group_start] = recorded_node_events.starts_of(group_name)
    assert group_start.parent_execution_id is None
    assert group_start.flow_name == parent_flow
    for body_start in recorded_node_events.starts_of(body_name):
        assert body_start.parent_execution_id == group_start.execution_id
        assert body_start.node_type == "LoopBodyControlNode"
        assert body_start.specific_library_name == LIBRARY_NAME

    last_finish = recorded_node_events.finishes[-1]
    assert last_finish.execution_id == group_start.execution_id, "the group finished before its body"


@pytest.mark.parametrize("execution_mode", EXECUTION_MODES)
@pytest.mark.asyncio
async def test_nested_loop_body_runs_resolve_to_the_canvas_node(
    tmp_path: Path,
    engine: Engine,
    materialize_library: Callable[..., Path],
    recorded_node_events: RecordedNodeEvents,
    execution_mode: str,
) -> None:
    """A loop inside a loop: the innermost body is still named as on the canvas, nested two deep."""
    _register_fixture_library(engine, tmp_path, materialize_library)
    parent_flow = _create_parent_flow(engine, "nested_node_process_events_wf")
    with engine.context_manager.flow(parent_flow):
        outer_name = _create_node(engine, "ProbeForEachGroupNode", OUTER_GROUP_NAME)
        inner_name = _create_node(engine, "ProbeForEachGroupNode", INNER_GROUP_NAME, parent_group_name=outer_name)
        body_name = _create_node(engine, "LoopBodyControlNode", BODY_NODE_NAME, parent_group_name=inner_name)
    _wire_loop_body(engine, inner_name, body_name)
    _wire_loop_body(engine, outer_name, inner_name)
    _set_execution_mode(engine, outer_name, execution_mode)
    _set_execution_mode(engine, inner_name, execution_mode)

    await _run(engine, parent_flow, outer_name)

    assert Counter(event.node_name for event in recorded_node_events.starts) == {
        outer_name: 1,
        inner_name: ITEMS_PER_LOOP,
        body_name: ITEMS_PER_LOOP * ITEMS_PER_LOOP,
    }, f"a node other than the canvas's ran under its own name ({execution_mode})"
    _assert_every_run_paired_and_succeeded(recorded_node_events)

    [outer_start] = recorded_node_events.starts_of(outer_name)
    inner_execution_ids = {event.execution_id for event in recorded_node_events.starts_of(inner_name)}
    assert {event.parent_execution_id for event in recorded_node_events.starts_of(inner_name)} == {
        outer_start.execution_id
    }
    body_parents = Counter(event.parent_execution_id for event in recorded_node_events.starts_of(body_name))
    assert set(body_parents) == inner_execution_ids
    assert set(body_parents.values()) == {ITEMS_PER_LOOP}, "each inner run should parent one body run per item"


@pytest.mark.asyncio
async def test_library_listeners_watch_every_node_and_stop_when_it_unloads(
    tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path]
) -> None:
    """The path a library author takes: declare listeners, run a workflow, unload the library."""
    template = tmp_path / "template.json"
    schema = json.loads(FIXTURE_LIBRARY_JSON_TEMPLATE.read_text())
    schema["advanced_library_path"] = WATCHER_LIBRARY_FILE.name
    template.write_text(json.dumps(schema))
    library_json = materialize_library(
        tmp_path / "library",
        template=template,
        node_file=FIXTURE_NODE_FILE,
        name=LIBRARY_NAME,
        extra_files=[WATCHER_LIBRARY_FILE],
    )
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result
    watcher = LibraryRegistry.get_library(LIBRARY_NAME).get_advanced_library()
    assert watcher is not None
    # Listeners only hear events that reach a queue, as they do in a running engine.
    engine.event_manager.initialize_queue(asyncio.Queue())

    parent_flow = _create_parent_flow(engine, "library_listener_wf")
    with engine.context_manager.flow(parent_flow):
        group_name = _create_node(engine, "ProbeForEachGroupNode", OUTER_GROUP_NAME)
        body_name = _create_node(engine, "LoopBodyControlNode", BODY_NODE_NAME, parent_group_name=group_name)
    _wire_loop_body(engine, group_name, body_name)

    await _run(engine, parent_flow, group_name)

    heard = watcher.heard  # pyright: ignore[reportAttributeAccessIssue]
    assert Counter((type(event).__name__, event.node_name) for event in heard) == {
        ("NodeStartProcessEvent", group_name): 1,
        ("NodeFinishProcessEvent", group_name): 1,
        ("NodeStartProcessEvent", body_name): ITEMS_PER_LOOP,
        ("NodeFinishProcessEvent", body_name): ITEMS_PER_LOOP,
    }

    unload_result = engine.handle_request(UnloadLibraryFromRegistryRequest(library_name=LIBRARY_NAME))
    assert isinstance(unload_result, UnloadLibraryFromRegistryResultSuccess), unload_result
    heard_before_unload = len(heard)
    engine.event_manager.put_event(
        ExecutionGriptapeNodeEvent(wrapped_event=ExecutionEvent(payload=NodeStartProcessEvent(node_name="After")))
    )
    assert len(heard) == heard_before_unload, "a listener kept firing after its library unloaded"
