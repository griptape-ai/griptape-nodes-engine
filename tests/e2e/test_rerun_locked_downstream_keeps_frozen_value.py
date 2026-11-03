"""A locked node downstream of a re-running node keeps its frozen value, never recomputes.

``unresolve_future_nodes`` (called at the start of every node's execution, see
``parallel_resolution.ExecuteDagState``) marks RESOLVED downstream nodes UNRESOLVED without
checking ``lock`` -- the same as it has always done for a user edit via
``SetParameterValueRequest``. The DAG builder and queueing logic key off ``node.lock`` directly
(not resolution state) to skip execution and hand on the frozen value, so a locked node must
never actually recompute, however many times something upstream of it re-runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

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
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    CreateNodeResultSuccess,
    SetLockNodeStateRequest,
    SetLockNodeStateResultSuccess,
)

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
async def test_locked_doubler_keeps_frozen_value_across_reruns(
    tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path]
) -> None:
    """A locked Doubler must keep the value it had when locked across repeated top-level runs."""
    library_json = materialize_library(
        tmp_path / "library", template=FIXTURE_LIBRARY_JSON_TEMPLATE, node_file=FIXTURE_NODE_FILE, name=LIBRARY_NAME
    )
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result

    engine.context_manager.push_workflow(workflow_name="probe_locked_wf")
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
        assert isinstance(connection_result, CreateConnectionResultSuccess), connection_result

    # Run once unlocked so Doubler has a real computed value, then lock it.
    with engine.context_manager.flow(flow_name):
        run_result = await engine.ahandle_request(StartFlowRequest(flow_name=flow_name, flow_node_name=None))
    assert isinstance(run_result, StartFlowResultSuccess), run_result

    doubler = engine.node_manager.get_node_by_name("Doubler")
    value_before_lock = dict(doubler.parameter_output_values)

    lock_result = engine.handle_request(SetLockNodeStateRequest(node_name="Doubler", lock=True))
    assert isinstance(lock_result, SetLockNodeStateResultSuccess), lock_result
    assert doubler.lock is True

    for i in range(RUNS):
        with engine.context_manager.flow(flow_name):
            run_result = await engine.ahandle_request(StartFlowRequest(flow_name=flow_name, flow_node_name=None))
        assert isinstance(run_result, StartFlowResultSuccess), f"run {i} failed: {run_result}"

        # Counter advances every run, so a Doubler that actually recomputed would produce a new
        # value. It must not: the lock freezes it, and Sink must keep receiving that frozen value.
        assert dict(doubler.parameter_output_values) == value_before_lock, (
            f"run {i}: locked Doubler's value changed from {value_before_lock}"
        )
        assert doubler.state == NodeResolutionState.RESOLVED, f"run {i}: locked Doubler left in state {doubler.state}"
