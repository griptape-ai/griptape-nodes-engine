"""The end-of-loop copy-back leaves a list with connected rows alone instead of failing the loop.

After the last iteration, a loop copies the End node's values back onto the canvas body nodes so they
show what the loop produced. The End node maps every PROPERTY parameter, including a list container,
so a node whose list rows are each wired from another body node (Merge Images is the shipped example)
was handed the whole last-iteration list. A whole list written to a node becomes its rows, and
replacing a connected row would remove its connection, which the list refuses. The write had no
guard, so the loop failed after every iteration had already run.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

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
    AddParameterToNodeRequest,
    AddParameterToNodeResultSuccess,
    SetParameterValueRequest,
    SetParameterValueResultSuccess,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.engine import Engine

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "loop_copy_back_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "loop_copy_back_nodes.py"
# The packager resolves its StartFlow/EndFlow endpoints from a library with exactly this name.
LIBRARY_NAME = "Griptape Nodes Library"

GROUP = "Loop"
ECHO = "Echo"
SINK = "Sink"

SEQUENTIAL = "Run Group Items One at a Time"
PARALLEL = "Run Group Items All at Once"


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


@pytest.mark.parametrize("execution_mode", [SEQUENTIAL, PARALLEL])
@pytest.mark.asyncio
async def test_loop_finishes_when_a_body_list_has_connected_rows(
    tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path], execution_mode: str
) -> None:
    """Body: Echo -> one row of Sink.items. The loop completes and the canvas Sink shows the last list."""
    library_json = materialize_library(
        tmp_path / "library", template=FIXTURE_LIBRARY_JSON_TEMPLATE, node_file=FIXTURE_NODE_FILE, name=LIBRARY_NAME
    )
    registered = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(registered, RegisterLibraryFromFileResultSuccess), registered

    engine.context_manager.push_workflow(workflow_name="loop_copy_back")
    flow = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="Parent", set_as_new_context=False))
    assert isinstance(flow, CreateFlowResultSuccess), flow
    with engine.context_manager.flow(flow.flow_name):
        group = engine.handle_request(
            CreateNodeRequest(node_type="ItemsForEachGroupNode", specific_library_name=LIBRARY_NAME, node_name=GROUP)
        )
        assert isinstance(group, CreateNodeResultSuccess), group
        for node_type, node_name in (("EchoNode", ECHO), ("ListSinkNode", SINK)):
            child = engine.handle_request(
                CreateNodeRequest(
                    node_type=node_type,
                    specific_library_name=LIBRARY_NAME,
                    node_name=node_name,
                    parent_group_name=GROUP,
                )
            )
            assert isinstance(child, CreateNodeResultSuccess), child
    row = engine.handle_request(AddParameterToNodeRequest(node_name=SINK, parent_container_name="items"))
    assert isinstance(row, AddParameterToNodeResultSuccess), row

    _connect(engine, GROUP, "on_each", ECHO, "exec_in")
    _connect(engine, ECHO, "exec_out", SINK, "exec_in")
    _connect(engine, SINK, "exec_out", GROUP, "loop_complete")
    _connect(engine, GROUP, "current_item", ECHO, "value")
    _connect(engine, ECHO, "result", SINK, row.parameter_name)
    _connect(engine, SINK, "result", GROUP, "new_item_to_add")
    mode_set = engine.handle_request(
        SetParameterValueRequest(node_name=GROUP, parameter_name="execution_mode", value=execution_mode)
    )
    assert isinstance(mode_set, SetParameterValueResultSuccess), mode_set
    queue: asyncio.Queue = asyncio.Queue()
    engine.event_manager.initialize_queue(queue)

    with engine.context_manager.flow(flow.flow_name):
        run_result = await engine.ahandle_request(StartFlowRequest(flow_name=flow.flow_name, flow_node_name=GROUP))
    assert isinstance(run_result, StartFlowResultSuccess), run_result

    group_node = engine.node_manager.get_node_by_name(GROUP)
    assert group_node.parameter_output_values.get("results") == ["10", "20"]
    sink = engine.node_manager.get_node_by_name(SINK)
    assert sink.parameter_output_values.get("items") == ["20"], "the canvas node still shows the last iteration's list"
