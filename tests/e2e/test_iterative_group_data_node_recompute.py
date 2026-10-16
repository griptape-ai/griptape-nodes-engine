"""A data node inside an iterative group body recomputes every pass.

Sequential passes reuse one deserialized body flow, so a node stays RESOLVED from the pass before.
When a control node in the body re-runs and produces a new output, the data node it feeds has to be
marked stale, or the upstream walk that gathers the next node's inputs skips it and hands on the
previous pass's value.

The interior node's input must come from an in-body control node that recomputes from its own
state (``CounterControlNode``, standing in for Get Variable or an Agent), not from the group's own
``index``/``current_item``. The executor sets those with ``SetParameterValueRequest`` every pass,
which already marks their consumers stale and would hide this bug.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

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
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.engine import Engine

pytestmark = pytest.mark.timeout(300, method="thread")

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "iterative_group_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "iterative_group_nodes.py"
# `flow_manager._validate_and_get_multi_node_library_info` resolves the packaged flow's
# StartFlow/EndFlow endpoints from a library with exactly this name.
LIBRARY_NAME = "Griptape Nodes Library"

GROUP_NODE_NAME = "ForEachGroup"
COUNTER_NODE_NAME = "Counter"
DATA_NODE_NAME = "Doubler"
SINK_NODE_NAME = "Sink"


async def _run_doubler_loop(
    tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path], execution_mode: str
) -> list[Any]:
    """Build a loop body: Counter -> Doubler -> Sink, with Doubler as the only pure data node.

    Control chain: on_each -> Counter.exec_in -> Sink.exec_in -> loop_complete (Doubler is not on
    this chain at all -- it is pure data). Data chain: Counter.count -> Doubler.value,
    Doubler.doubled -> Sink.value, Sink.result -> Group.new_item_to_add.

    Counter recomputes from its own instance state every time it runs (0, 1, 2, ...) -- nothing
    external re-sets a parameter on it, so the only thing that makes Doubler see a new input is
    Counter actually running and resolving fresh each iteration.

    Returns the group's `results` list after a full run.
    """
    library_json = materialize_library(
        tmp_path / "library", template=FIXTURE_LIBRARY_JSON_TEMPLATE, node_file=FIXTURE_NODE_FILE, name=LIBRARY_NAME
    )
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result

    engine.context_manager.push_workflow(workflow_name="data_node_recompute_wf")

    parent_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ParentFlow", set_as_new_context=False)
    )
    assert isinstance(parent_result, CreateFlowResultSuccess), parent_result
    parent_flow = parent_result.flow_name

    with engine.context_manager.flow(parent_flow):
        group_result = engine.handle_request(
            CreateNodeRequest(
                node_type="ProbeForEachGroupNode", specific_library_name=LIBRARY_NAME, node_name=GROUP_NODE_NAME
            )
        )
        assert isinstance(group_result, CreateNodeResultSuccess), group_result
        counter_result = engine.handle_request(
            CreateNodeRequest(
                node_type="CounterControlNode",
                specific_library_name=LIBRARY_NAME,
                node_name=COUNTER_NODE_NAME,
                parent_group_name=group_result.node_name,
            )
        )
        assert isinstance(counter_result, CreateNodeResultSuccess), counter_result
        data_result = engine.handle_request(
            CreateNodeRequest(
                node_type="DoublerDataNode",
                specific_library_name=LIBRARY_NAME,
                node_name=DATA_NODE_NAME,
                parent_group_name=group_result.node_name,
            )
        )
        assert isinstance(data_result, CreateNodeResultSuccess), data_result
        sink_result = engine.handle_request(
            CreateNodeRequest(
                node_type="LoopBodyControlNode",
                specific_library_name=LIBRARY_NAME,
                node_name=SINK_NODE_NAME,
                parent_group_name=group_result.node_name,
            )
        )
        assert isinstance(sink_result, CreateNodeResultSuccess), sink_result

    for source_node, source_param, target_node, target_param in (
        (GROUP_NODE_NAME, "on_each", COUNTER_NODE_NAME, "exec_in"),
        (COUNTER_NODE_NAME, "exec_out", SINK_NODE_NAME, "exec_in"),
        (SINK_NODE_NAME, "exec_out", GROUP_NODE_NAME, "loop_complete"),
        (COUNTER_NODE_NAME, "count", DATA_NODE_NAME, "value"),
        (DATA_NODE_NAME, "doubled", SINK_NODE_NAME, "text"),
        (SINK_NODE_NAME, "result", GROUP_NODE_NAME, "new_item_to_add"),
    ):
        connection_result = engine.handle_request(
            CreateConnectionRequest(
                source_node_name=source_node,
                source_parameter_name=source_param,
                target_node_name=target_node,
                target_parameter_name=target_param,
            )
        )
        assert isinstance(connection_result, CreateConnectionResultSuccess), (
            f"{source_node}.{source_param} -> {target_node}.{target_param}: {connection_result}"
        )

    engine.handle_request(
        SetParameterValueRequest(
            parameter_name="execution_mode", node_name=group_result.node_name, value=execution_mode
        )
    )

    with engine.context_manager.flow(parent_flow):
        run_result = await engine.ahandle_request(
            StartFlowRequest(
                flow_name=parent_flow,
                flow_node_name=group_result.node_name,
            )
        )
    assert isinstance(run_result, StartFlowResultSuccess), run_result

    node = engine.node_manager.get_node_by_name(group_result.node_name)
    # "results" is an OUTPUT-only parameter: get_parameter_value reads parameter_values, not
    # parameter_output_values, so it never sees a value process() only ever writes to the
    # output side.
    return list(node.parameter_output_values.get("results") or [])


# Expected results differ by execution mode, and that difference is itself the evidence this test
# exists to pin down. Sequential mode reuses one deserialized flow instance across all three
# iterations, so Counter's `_count` genuinely accumulates (0, 1, 2) -- a correct Doubler must
# track it ("0", "2", "4"); a stale one freezes at iteration 1's value ("0", "0", "0"). Parallel
# mode deserializes a fresh flow instance per iteration, so every Counter starts its own `_count`
# at 0 by construction -- ("0", "0", "0") is correct there, not a symptom of staleness. There is
# no shared mutable node state across parallel iterations for this bug to act on.
EXPECTED_RESULTS_BY_MODE = {
    "Run Group Items One at a Time": ["0", "2", "4"],
    "Run Group Items All at Once": ["0", "0", "0"],
}


@pytest.mark.parametrize("execution_mode", list(EXPECTED_RESULTS_BY_MODE))
@pytest.mark.asyncio
async def test_interior_data_node_recomputes_every_iteration(
    tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path], execution_mode: str
) -> None:
    """Doubler.doubled must track Counter.count each iteration, not freeze at iteration 1's value."""
    results = await _run_doubler_loop(tmp_path, engine, materialize_library, execution_mode)

    assert results == EXPECTED_RESULTS_BY_MODE[execution_mode], (
        f"interior data node did not recompute as expected ({execution_mode}): {results}"
    )
