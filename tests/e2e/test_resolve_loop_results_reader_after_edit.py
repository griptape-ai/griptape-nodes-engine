"""Resolving a node that reads a loop's results re-runs the loop, even when the loop End arrives as a dependency.

Shape: a legacy For Each Start/End pair whose body reads an outside data node, and a loose data
node after the loop that reads End's ``results``. Run the workflow once, edit the outside node, then
Resolve the reader.

The edit marks the outside node, the body, End and the reader stale, but not Start. Resolving the
reader builds a DAG with the reader as root and End as a data dependency. The End node only runs
when its Start hands it the control token, so Start has to be a dependency of End however End got
into the DAG. It used to be added only when End was the root of the build: here End is not, so
Start was never added, the body nodes were adopted as plain dependencies and ran once in the outer
flow with a stale ``current_item``, End was popped from the queue and skipped (loop Ends are only
fired by their Start), and the reader waited forever. Same on `main`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.base_iterative_nodes import BaseIterativeStartNode
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
)
from griptape_nodes.retained_mode.events.execution_events import (
    ResolveNodeRequest,
    ResolveNodeResultSuccess,
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

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.engine import Engine

pytestmark = pytest.mark.timeout(300, method="thread")

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "loop_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "loop_nodes.py"
LIBRARY_NAME = "Griptape Nodes Library"

RESOLVE_TIMEOUT_SECONDS = 30


def _create(engine: Engine, node_type: str, node_name: str) -> str:
    result = engine.handle_request(
        CreateNodeRequest(
            node_type=node_type,
            specific_library_name=LIBRARY_NAME,
            node_name=node_name,
            metadata={"position": {"x": 0, "y": 0}},
        )
    )
    assert isinstance(result, CreateNodeResultSuccess), result
    return result.node_name


def _connect(engine: Engine, source: str, source_param: str, target: str, target_param: str) -> None:
    result = engine.handle_request(
        CreateConnectionRequest(
            source_node_name=source,
            source_parameter_name=source_param,
            target_node_name=target,
            target_parameter_name=target_param,
        )
    )
    assert isinstance(result, CreateConnectionResultSuccess), (
        f"{source}.{source_param} -> {target}.{target_param}: {result}"
    )


@pytest.mark.asyncio
async def test_resolving_results_reader_after_editing_an_outside_input_reruns_the_loop(
    tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path]
) -> None:
    """After editing Outside, resolving After must finish and show the loop's new results."""
    library_json = materialize_library(
        tmp_path / "loop_library",
        template=FIXTURE_LIBRARY_JSON_TEMPLATE,
        node_file=FIXTURE_NODE_FILE,
        name=LIBRARY_NAME,
    )
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result

    engine.context_manager.push_workflow(workflow_name="resolve_results_reader_wf")
    flow_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="LoopFlow", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    flow_name = flow_result.flow_name

    with engine.context_manager.flow(flow_name):
        outside_name = _create(engine, "LoopBodyNode", "Outside")
        start_name = _create(engine, "LoopStartNode", "Loop Start")
        body_name = _create(engine, "LoopJoinNode", "Body")
        after_name = _create(engine, "LoopResultsNode", "After")

        start_node = engine.node_manager.get_node_by_name(start_name)
        assert isinstance(start_node, BaseIterativeStartNode), start_node
        end_node = start_node.end_node
        assert end_node is not None, "The start node did not tether an end node."

        _connect(engine, start_name, "exec_out", body_name, "exec_in")
        _connect(engine, body_name, "exec_out", end_node.name, "add_item")
        _connect(engine, outside_name, "result", body_name, "prefix")
        _connect(engine, start_name, "current_item", body_name, "item")
        _connect(engine, body_name, "result", end_node.name, "new_item_to_add")
        # Data only: After has no control wires, so Resolve on it is a single-node resolution.
        _connect(engine, end_node.name, "results", after_name, "items")

        engine.handle_request(SetParameterValueRequest(node_name=outside_name, parameter_name="text", value="one-"))
        run_result = await engine.ahandle_request(StartFlowRequest(flow_name=flow_name, flow_node_name=None))
        assert isinstance(run_result, StartFlowResultSuccess), run_result
        after_node = engine.node_manager.get_node_by_name(after_name)
        assert after_node.parameter_output_values["result"] == ["one-first", "one-second", "one-third"]

        # The edit marks Outside, Body, End and After stale. Start stays resolved.
        engine.handle_request(SetParameterValueRequest(node_name=outside_name, parameter_name="text", value="two-"))

        resolve_result = await asyncio.wait_for(
            engine.ahandle_request(ResolveNodeRequest(node_name=after_name)), timeout=RESOLVE_TIMEOUT_SECONDS
        )
        assert isinstance(resolve_result, ResolveNodeResultSuccess), resolve_result

    assert not engine.flow_manager.check_for_existing_running_flow(), "The flow is still marked as running."
    after_node = engine.node_manager.get_node_by_name(after_name)
    assert after_node.parameter_output_values["result"] == ["two-first", "two-second", "two-third"]
