"""End-to-end coverage for resolving a node downstream of a loop while the loop is still running.

The second resolve joins the live run. Its upstream walk adopts the loop's end node, which already
belongs to the first resolve's graph, so the end node ends up in both graphs. Finishing the loop
empties the first graph, and emptying a graph drops its nodes from ``node_to_reference``. The end
node has to be released from the second graph too, or the downstream node waits on it forever and
the run never completes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.base_iterative_nodes import BaseIterativeStartNode
from griptape_nodes.exe_types.node_types import BaseNode, NodeResolutionState
from griptape_nodes.retained_mode.events.execution_events import ResolveNodeRequest, ResolveNodeResultSuccess
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.engine import Engine

pytestmark = pytest.mark.timeout(120, method="thread")

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SINK_LIBRARY_NAME = "Loose Sink Library"
# The loop packager resolves its own start/end node types out of the standard library by name,
# so the loop fixture has to register under that name rather than one of its own.
LOOP_LIBRARY_NAME = "Griptape Nodes Library"
# The fixture's LoopStartNode always iterates its fixed three-item list.
LOOP_ITERATIONS = 3
# Long enough for a healthy run to finish, short enough that a wedged one fails the test quickly.
RUN_TIMEOUT_SECONDS = 10


def _origin() -> dict:
    """Return the minimal metadata an iterative node needs, fresh each call."""
    return {"position": {"x": 0, "y": 0}}


@pytest.fixture
def loop_flow(tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path]) -> str:
    """Register the loop and sink fixture libraries and return the name of an empty flow."""
    for directory, library_name, node_file in (
        ("loose_sink_library", SINK_LIBRARY_NAME, "loose_sink_nodes.py"),
        ("loop_library", LOOP_LIBRARY_NAME, "loop_nodes.py"),
    ):
        library_json = materialize_library(
            tmp_path / directory,
            template=FIXTURES_DIR / directory / "griptape_nodes_library.json",
            node_file=FIXTURES_DIR / directory / node_file,
            name=library_name,
        )
        library_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
        assert isinstance(library_result, RegisterLibraryFromFileResultSuccess), library_result

    engine.context_manager.push_workflow(workflow_name="resolve_during_loop_wf")
    flow_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="MainFlow", set_as_new_context=True)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    return flow_result.flow_name


@pytest.mark.asyncio
async def test_resolving_downstream_of_a_running_loop_completes(
    engine: Engine,
    create_node: Callable[..., str],
    connect: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    loop_flow: str,
) -> None:
    """A node resolved mid-loop runs on the loop's results, and the run then finishes cleanly."""
    start = create_node("LoopStartNode", "LoopStart", loop_flow, library_name=LOOP_LIBRARY_NAME, metadata=_origin())
    create_node("LoopBodyNode", "Body", loop_flow, library_name=LOOP_LIBRARY_NAME, metadata=_origin())
    create_node("ListSinkNode", "Sink", loop_flow, library_name=SINK_LIBRARY_NAME, metadata=_origin())

    # Creating an iterative start node tethers its paired end node, so take that one by reference.
    start_node = engine.node_manager.get_node_by_name(start)
    assert isinstance(start_node, BaseIterativeStartNode), start_node
    end_node = start_node.end_node
    assert end_node is not None, "The start node did not tether an end node."

    connect(start, "exec_out", "Body", "exec_in")
    connect("Body", "exec_out", end_node.name, "add_item")
    connect("Body", "result", end_node.name, "new_item_to_add")
    connect(end_node.name, "results", "Sink", "items")

    # Hold every loop iteration until the second resolve has joined the run.
    loop_may_finish = asyncio.Event()
    body_class = type(engine.node_manager.get_node_by_name("Body"))
    original_aprocess = body_class.aprocess

    async def _held_aprocess(self: BaseNode) -> None:
        await loop_may_finish.wait()
        await original_aprocess(self)

    monkeypatch.setattr(body_class, "aprocess", _held_aprocess)

    loop_run = asyncio.create_task(engine.ahandle_request(ResolveNodeRequest(node_name=end_node.name)))
    for _ in range(500):
        if end_node.state == NodeResolutionState.RESOLVING:
            break
        await asyncio.sleep(0.01)
    assert end_node.state == NodeResolutionState.RESOLVING, "the loop never started"

    sink_result = await engine.ahandle_request(ResolveNodeRequest(node_name="Sink"))
    assert isinstance(sink_result, ResolveNodeResultSuccess), sink_result

    loop_may_finish.set()
    loop_result = await asyncio.wait_for(loop_run, timeout=RUN_TIMEOUT_SECONDS)
    assert isinstance(loop_result, ResolveNodeResultSuccess), loop_result

    sink = engine.node_manager.get_node_by_name("Sink")
    assert sink.state == NodeResolutionState.RESOLVED, "the node resolved during the loop never ran"
    loop_results = end_node.parameter_output_values["results"]
    assert len(loop_results) == LOOP_ITERATIONS, "the loop did not collect every iteration"
    assert sink.get_parameter_value("items") == loop_results, "the sink ran before the loop's results existed"
    assert not engine.flow_manager.global_dag_builder.node_to_reference, "the run left nodes in the DAG"
    assert not engine.flow_manager.check_for_existing_running_flow(), "the run never finished"
