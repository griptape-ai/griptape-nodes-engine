"""Running a node that sits after a loop, when the loop body reads a node from outside the loop.

Resolving the node after the loop builds its DAG back through the loop body, so the outside node
is queued in the outer run. When the loop end runs, the packager pulls that outside node into the
loop body and drops it from the outer DAG. It must leave the outer run's queue too: a name left
there names a node the DAG no longer has, and the next pull from the queue raised ``KeyError``
after every iteration had already finished.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.base_iterative_nodes import BaseIterativeStartNode
from griptape_nodes.retained_mode.events.execution_events import ResolveNodeRequest, ResolveNodeResultSuccess
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.engine import Engine

# Timeout with thread dump.
pytestmark = pytest.mark.timeout(300, method="thread")

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "loop_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "loop_nodes.py"
# The packager asks for its StartFlow/EndFlow endpoints from this library by name.
LIBRARY_NAME = "Griptape Nodes Library"


def _at(x: int, y: int) -> dict:
    return {"position": {"x": x, "y": y}}


@pytest.mark.asyncio
async def test_resolving_a_node_after_a_loop_whose_body_reads_an_outside_node(
    engine: Engine,
    materialize_library: Callable[..., Path],
    create_node: Callable[..., str],
    connect: Callable[..., None],
    tmp_path: Path,
) -> None:
    """The node after the loop resolves with every iteration's result, and the run ends."""
    library_json = materialize_library(
        tmp_path / "loop_library",
        template=FIXTURE_LIBRARY_JSON_TEMPLATE,
        node_file=FIXTURE_NODE_FILE,
        name=LIBRARY_NAME,
    )
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result

    engine.context_manager.push_workflow(workflow_name="resolve_after_loop_wf")
    flow_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="LoopFlow", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    flow_name = flow_result.flow_name

    # Positions matter: with nodes queued together, the top-left one runs first. The loop start is
    # placed ahead of the outside node so the outside node is still queued when the loop packages it.
    start_name = create_node("LoopStartNode", "Loop Start", flow_name, library_name=LIBRARY_NAME, metadata=_at(0, 0))
    # Off the control chain and outside the loop: the shape that gets pulled into the body.
    outside_name = create_node(
        "LoopBodyNode", "Outside Text", flow_name, library_name=LIBRARY_NAME, metadata=_at(400, 400)
    )
    first_name = create_node("LoopJoinNode", "Body A", flow_name, library_name=LIBRARY_NAME, metadata=_at(200, 0))
    second_name = create_node("LoopBodyNode", "Body B", flow_name, library_name=LIBRARY_NAME, metadata=_at(400, 0))
    after_name = create_node(
        "LoopResultsNode", "After Loop", flow_name, library_name=LIBRARY_NAME, metadata=_at(800, 0)
    )

    start_node = engine.node_manager.get_node_by_name(start_name)
    assert isinstance(start_node, BaseIterativeStartNode), start_node
    end_node = start_node.end_node
    assert end_node is not None, "The start node did not tether an end node."
    end_name = end_node.name

    engine.handle_request(SetParameterValueRequest(parameter_name="text", node_name=outside_name, value="item-"))

    connect(start_name, "exec_out", first_name, "exec_in")
    connect(first_name, "exec_out", second_name, "exec_in")
    connect(second_name, "exec_out", end_name, "add_item")
    connect(outside_name, "result", first_name, "prefix")
    connect(start_name, "index", first_name, "item")
    connect(first_name, "result", second_name, "text")
    connect(second_name, "result", end_name, "new_item_to_add")
    connect(end_name, "exec_out", after_name, "exec_in")
    connect(end_name, "results", after_name, "items")

    with engine.context_manager.flow(flow_name):
        resolve_result = await engine.ahandle_request(ResolveNodeRequest(node_name=after_name))

    assert isinstance(resolve_result, ResolveNodeResultSuccess), resolve_result
    assert not engine.flow_manager.check_for_existing_running_flow(), "The flow is still marked as running."
    after_node = engine.node_manager.get_node_by_name(after_name)
    assert after_node.parameter_output_values["result"] == ["item-0", "item-1", "item-2"]
