"""Running a workflow again recomputes data nodes fed by a node that produced a new output.

``Counter`` is a control node whose output changes every run. ``Doubler`` is a data node between it
and ``Sink``. Nothing is edited between runs, so the only thing that can make ``Doubler`` recompute
is ``Counter`` running again. If that doesn't mark ``Doubler`` stale, it stays RESOLVED and ``Sink``
receives the first run's value on every later run.

Runs use ``StartFlowRequest`` with no start node, the same request the editor's Run button sends.
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
async def test_rerun_recomputes_a_data_node_whose_input_changed(
    tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path]
) -> None:
    """Sink must see Doubler track Counter on every run, not keep the first run's value."""
    library_json = materialize_library(
        tmp_path / "library", template=FIXTURE_LIBRARY_JSON_TEMPLATE, node_file=FIXTURE_NODE_FILE, name=LIBRARY_NAME
    )
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result

    engine.context_manager.push_workflow(workflow_name="rerun_marks_downstream_stale_wf")
    flow_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="RerunFlow", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    flow_name = flow_result.flow_name

    with engine.context_manager.flow(flow_name):
        for node_type, node_name in (
            ("CounterControlNode", "Counter"),
            ("DoublerDataNode", "Doubler"),
            ("LoopBodyControlNode", "Sink"),
        ):
            node_result = engine.handle_request(
                CreateNodeRequest(node_type=node_type, specific_library_name=LIBRARY_NAME, node_name=node_name)
            )
            assert isinstance(node_result, CreateNodeResultSuccess), node_result

    for source_node, source_param, target_node, target_param in (
        ("Counter", "exec_out", "Sink", "exec_in"),
        ("Counter", "count", "Doubler", "value"),
        ("Doubler", "doubled", "Sink", "text"),
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

    sink_results = []
    for _ in range(RUNS):
        with engine.context_manager.flow(flow_name):
            run_result = await engine.ahandle_request(StartFlowRequest(flow_name=flow_name, flow_node_name=None))
        assert isinstance(run_result, StartFlowResultSuccess), run_result
        sink_results.append(engine.node_manager.get_node_by_name("Sink").parameter_output_values.get("result"))

    assert sink_results == ["0", "2", "4"], f"Sink saw a stale Doubler output on a later run: {sink_results}"
