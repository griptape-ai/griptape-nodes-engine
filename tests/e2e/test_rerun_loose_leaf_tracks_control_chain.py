"""A data-only leaf two hops below a control node tracks that node on every run.

``Head -> Counter -> Tail`` is the control path; ``Counter`` produces a new ``count`` each run.
``Counter.count -> First.value -> Second.value`` is a chain of data nodes with no control wires, and
``Second`` has no outgoing connections, so it is a loose leaf that gets its own DAG graph.

Seeded at run start, that graph saw ``First`` still RESOLVED from the previous run and stopped
there, so ``Second`` ran at once on ``First``'s old output while ``Counter`` re-ran beside it. The
leaf is now seeded after the control graphs finish, when ``First`` has been marked stale by
``Counter`` starting, so the leaf's graph adopts ``First`` and reads this run's value.

Runs use ``StartFlowRequest`` with no start node, the same request the editor's Run button sends,
in the default parallel mode.
"""

from __future__ import annotations

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

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.engine import Engine

pytestmark = pytest.mark.timeout(300, method="thread")

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "iterative_group_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "iterative_group_nodes.py"
LIBRARY_NAME = "Griptape Nodes Library"

RUNS = 3


@pytest.mark.asyncio
async def test_loose_leaf_two_hops_below_a_control_node_tracks_it_every_run(
    tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path]
) -> None:
    """Second must equal 4 x Counter's count on every run, not the previous run's value."""
    library_json = materialize_library(
        tmp_path / "library", template=FIXTURE_LIBRARY_JSON_TEMPLATE, node_file=FIXTURE_NODE_FILE, name=LIBRARY_NAME
    )
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result

    engine.context_manager.push_workflow(workflow_name="rerun_loose_leaf_wf")
    flow_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="LeafFlow", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    flow_name = flow_result.flow_name

    with engine.context_manager.flow(flow_name):
        for node_type, node_name in (
            ("LoopBodyControlNode", "Head"),
            ("CounterControlNode", "Counter"),
            ("LoopBodyControlNode", "Tail"),
            ("DoublerDataNode", "First"),
            ("DoublerDataNode", "Second"),
        ):
            node_result = engine.handle_request(
                CreateNodeRequest(node_type=node_type, specific_library_name=LIBRARY_NAME, node_name=node_name)
            )
            assert isinstance(node_result, CreateNodeResultSuccess), node_result

    for source_node, source_param, target_node, target_param in (
        ("Head", "exec_out", "Counter", "exec_in"),
        ("Counter", "exec_out", "Tail", "exec_in"),
        ("Counter", "count", "First", "value"),
        ("First", "doubled", "Second", "value"),
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

    observed = []
    for _ in range(RUNS):
        with engine.context_manager.flow(flow_name):
            run_result = await engine.ahandle_request(StartFlowRequest(flow_name=flow_name, flow_node_name=None))
        assert isinstance(run_result, StartFlowResultSuccess), run_result
        count = engine.node_manager.get_node_by_name("Counter").parameter_output_values.get("count")
        second = engine.node_manager.get_node_by_name("Second").parameter_output_values.get("doubled")
        observed.append((count, second))

    expected = [(count, count * 4) for count, _ in observed]
    assert observed == expected, f"Second lagged Counter on a later run (count, second): {observed}"
