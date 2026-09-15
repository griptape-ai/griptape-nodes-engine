"""End-to-end coverage for how the DAG scheduler seeds graphs around data-only nodes.

Two shapes are exercised:

- A straight control chain with a *loose* data sink hanging off the middle node. The sink
  has no control connections, so it is seeded into its own graph, and that graph's upstream
  data recursion can adopt a control node that has not run yet. The adopted node still holds
  the control token, so it must advance control even though its graph has work left in it.
- A node with two control outputs where *both* wire into one downstream control input.
  Only one output is taken at runtime, so the downstream node must still run exactly once.
- A loose sink reading a loop's collected results, which adopts the loop's end node before
  the loop hands that same end node the control token on its way out.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.base_iterative_nodes import BaseIterativeStartNode
from griptape_nodes.exe_types.node_types import NodeResolutionState
from griptape_nodes.retained_mode.events.execution_events import StartFlowRequest
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.engine import Engine

pytestmark = pytest.mark.timeout(120, method="thread")

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "loose_sink_library"
LIBRARY_NAME = "Loose Sink Library"

LOOP_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "loop_library"
# The loop packager resolves its own start/end node types out of the standard library by name,
# so the loop fixture has to register under that name rather than one of its own.
LOOP_LIBRARY_NAME = "Griptape Nodes Library"


def _origin() -> dict:
    """Return the minimal metadata an iterative node needs, fresh each call.

    Node creation stamps the library and node type into the metadata dict it is handed, so a
    shared constant would leave every node claiming the library of whichever node was made last.
    """
    return {"position": {"x": 0, "y": 0}}


@pytest.fixture
def loose_sink_flow(
    tmp_path: Path,
    engine: Engine,
    materialize_library: Callable[..., Path],
) -> str:
    """Register the fixture library and return the name of an empty flow to build in."""
    library_json = materialize_library(
        tmp_path / "loose_sink_library",
        template=FIXTURE_DIR / "griptape_nodes_library.json",
        node_file=FIXTURE_DIR / "loose_sink_nodes.py",
    )
    library_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(library_result, RegisterLibraryFromFileResultSuccess), library_result

    engine.context_manager.push_workflow(workflow_name="loose_sink_wf")
    flow_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="MainFlow", set_as_new_context=True)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    return flow_result.flow_name


@pytest.fixture
def loop_and_sink_flow(
    tmp_path: Path,
    engine: Engine,
    materialize_library: Callable[..., Path],
    loose_sink_flow: str,
) -> str:
    """Add the loop fixture library alongside the sink library, in the same flow."""
    loop_json = materialize_library(
        tmp_path / "loop_library",
        template=LOOP_FIXTURE_DIR / "griptape_nodes_library.json",
        node_file=LOOP_FIXTURE_DIR / "loop_nodes.py",
        name=LOOP_LIBRARY_NAME,
    )
    loop_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(loop_json)))
    assert isinstance(loop_result, RegisterLibraryFromFileResultSuccess), loop_result
    return loose_sink_flow


async def _run(engine: Engine, flow_name: str) -> dict[str, NodeResolutionState]:
    """Run the flow to completion and return each node's final resolution state."""
    await engine.ahandle_request(
        StartFlowRequest(flow_name=flow_name, wait_for_completion=True, completion_timeout_ms=30000)
    )
    flow = engine.flow_manager.get_flow_by_name(flow_name)
    return {name: engine.node_manager.get_node_by_name(name).state for name in flow.nodes}


@pytest.mark.asyncio
@pytest.mark.parametrize("with_loose_sink", [False, True])
async def test_loose_sink(
    engine: Engine,
    create_node: Callable[..., str],
    connect: Callable[..., None],
    loose_sink_flow: str,
    with_loose_sink: bool,  # noqa: FBT001
) -> None:
    """A loose data sink hanging off the middle of a control chain must not truncate the chain."""
    flow = loose_sink_flow
    for name in ("A", "B", "C"):
        create_node("ChainNode", name, flow, library_name=LIBRARY_NAME)
    connect("A", "exec_out", "B", "exec_in")
    connect("B", "exec_out", "C", "exec_in")
    connect("A", "result", "B", "text")
    connect("B", "result", "C", "text")

    if with_loose_sink:
        create_node("SinkNode", "D", flow, library_name=LIBRARY_NAME)
        connect("B", "result", "D", "text")

    states = await _run(engine, flow)

    assert states["A"] == NodeResolutionState.RESOLVED
    assert states["B"] == NodeResolutionState.RESOLVED
    assert states["C"] == NodeResolutionState.RESOLVED, "control advance past the sink's graph was dropped"
    if with_loose_sink:
        assert states["D"] == NodeResolutionState.RESOLVED


@pytest.mark.asyncio
@pytest.mark.parametrize("evaluate", [False, True])
async def test_branch_both_outputs_into_one_control_input(
    engine: Engine,
    create_node: Callable[..., str],
    connect: Callable[..., None],
    loose_sink_flow: str,
    evaluate: bool,  # noqa: FBT001
) -> None:
    """Both control outputs of a branch wired to the same ``exec_in``: the target still runs."""
    flow = loose_sink_flow
    create_node("BranchNode", "Branch", flow, library_name=LIBRARY_NAME)
    create_node("ChainNode", "After", flow, library_name=LIBRARY_NAME)
    connect("Branch", "Then", "After", "exec_in")
    connect("Branch", "Else", "After", "exec_in")
    engine.node_manager.get_node_by_name("Branch").set_parameter_value("evaluate", evaluate)

    states = await _run(engine, flow)

    assert states["Branch"] == NodeResolutionState.RESOLVED
    assert states["After"] == NodeResolutionState.RESOLVED


@pytest.mark.asyncio
@pytest.mark.parametrize("evaluate", [False, True])
async def test_branch_with_loose_sink_on_one_branch(
    engine: Engine,
    create_node: Callable[..., str],
    connect: Callable[..., None],
    loose_sink_flow: str,
    evaluate: bool,  # noqa: FBT001
) -> None:
    """A loose sink fed from a node on one branch must not disturb the branch that is taken.

    ``Branch`` picks ``Then`` -> ``X`` or ``Else`` -> ``Y``; both rejoin at ``End``. ``Sink``
    takes data from ``X`` only, so seeding it drags ``X`` into a second graph regardless of
    which branch runs. ``X`` must not advance control from there, or ``End`` gets re-dirtied
    after it has already run.
    """
    flow = loose_sink_flow
    create_node("BranchNode", "Branch", flow, library_name=LIBRARY_NAME)
    for name in ("X", "Y", "End"):
        create_node("ChainNode", name, flow, library_name=LIBRARY_NAME)
    create_node("SinkNode", "Sink", flow, library_name=LIBRARY_NAME)
    connect("Branch", "Then", "X", "exec_in")
    connect("Branch", "Else", "Y", "exec_in")
    connect("X", "exec_out", "End", "exec_in")
    connect("Y", "exec_out", "End", "exec_in")
    connect("X", "result", "Sink", "text")
    engine.node_manager.get_node_by_name("Branch").set_parameter_value("evaluate", evaluate)

    states = await _run(engine, flow)

    assert states["Branch"] == NodeResolutionState.RESOLVED
    taken = "X" if evaluate else "Y"
    assert states[taken] == NodeResolutionState.RESOLVED
    assert states["End"] == NodeResolutionState.RESOLVED


@pytest.mark.asyncio
@pytest.mark.parametrize("with_loose_sink", [False, True])
async def test_loose_sink_on_loop_results(
    engine: Engine,
    create_node: Callable[..., str],
    connect: Callable[..., None],
    loop_and_sink_flow: str,
    with_loose_sink: bool,  # noqa: FBT001
) -> None:
    """A loose sink reading a loop's results must not strand the node after the loop.

    The sink's upstream data walk adopts the loop's end node, so the end node enters the DAG as
    somebody's data dependency. The loop then hands that same end node the control token on its
    way out, which has to override how the node originally got there.
    """
    flow = loop_and_sink_flow
    create_node("ChainNode", "Before", flow, library_name=LIBRARY_NAME, metadata=_origin())
    start = create_node("LoopStartNode", "LoopStart", flow, library_name=LOOP_LIBRARY_NAME, metadata=_origin())
    create_node("LoopBodyNode", "Body", flow, library_name=LOOP_LIBRARY_NAME, metadata=_origin())
    create_node("ChainNode", "After", flow, library_name=LIBRARY_NAME, metadata=_origin())

    # Creating an iterative start node tethers its paired end node, so take that one by reference.
    start_node = engine.node_manager.get_node_by_name(start)
    assert isinstance(start_node, BaseIterativeStartNode), start_node
    end_node = start_node.end_node
    assert end_node is not None, "The start node did not tether an end node."

    connect("Before", "exec_out", start, "exec_in")
    connect(start, "exec_out", "Body", "exec_in")
    connect("Body", "exec_out", end_node.name, "add_item")
    connect(end_node.name, "exec_out", "After", "exec_in")

    if with_loose_sink:
        create_node("ListSinkNode", "Sink", flow, library_name=LIBRARY_NAME, metadata=_origin())
        connect(end_node.name, "results", "Sink", "items")

    states = await _run(engine, flow)

    assert states["Before"] == NodeResolutionState.RESOLVED
    assert states[end_node.name] == NodeResolutionState.RESOLVED
    assert states["After"] == NodeResolutionState.RESOLVED, "control advance out of the loop was dropped"
    if with_loose_sink:
        assert states["Sink"] == NodeResolutionState.RESOLVED
