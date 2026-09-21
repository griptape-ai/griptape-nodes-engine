"""Regression test for issue #4920: independent nodes in a SubflowNodeGroup must run in parallel.

Running a Subflow Node Group executes the group's own subflow through the isolated-subflow DAG
path. That path used to reuse the top-level `exclude_subflow_group_children` scope filter, which
drops every node whose `parent_group` is set. Inside a group's own subflow *all* members carry
that parent_group, so the filter stranded them: only the single start node got seeded and the
other independent nodes never resolved (let alone ran concurrently).

This test builds a group of independent nodes, runs it under PARALLEL mode, and asserts every
child resolves and their processing intervals overlap, matching the parallel behavior of a
top-level workflow run.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.node_types import NodeResolutionState
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

# Timeout with thread dump.
pytestmark = pytest.mark.timeout(300, method="thread")

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "subflow_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "subflow_echo_node.py"
# Distinct from the "Subflow Library" name used by test_subflow_execution.py: both register the
# fixture in-process, and the LibraryManager's LOADED bookkeeping leaks across tests sharing a
# worker, so a shared name makes whichever test runs second fail to re-register.
LIBRARY_NAME = "Subflow Group Parallel Library"
_NODE_COUNT = 3
_DELAY_SECONDS = 0.4


@pytest.mark.skipif(
    not FIXTURE_LIBRARY_JSON_TEMPLATE.exists(),
    reason=f"Subflow Library fixture missing at {FIXTURE_LIBRARY_JSON_TEMPLATE}",
)
@pytest.mark.asyncio
async def test_group_runs_independent_nodes_in_parallel(
    tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path]
) -> None:
    """Every independent child of a group resolves, and they overlap in time under PARALLEL mode."""
    library_json = materialize_library(
        tmp_path / "library", template=FIXTURE_LIBRARY_JSON_TEMPLATE, node_file=FIXTURE_NODE_FILE, name=LIBRARY_NAME
    )
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result

    config_manager = engine.config_manager
    prior_mode = config_manager.get_config_value("workflow_execution_mode")
    prior_max = config_manager.get_config_value("max_nodes_in_parallel")
    config_manager.set_config_value("workflow_execution_mode", "parallel")
    config_manager.set_config_value("max_nodes_in_parallel", 5)

    try:
        engine.context_manager.push_workflow(workflow_name="repro_4920_wf")

        parent_result = engine.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="ParentFlow", set_as_new_context=False)
        )
        assert isinstance(parent_result, CreateFlowResultSuccess), parent_result
        parent_flow = parent_result.flow_name

        with engine.context_manager.flow(parent_flow):
            group_result = engine.handle_request(
                CreateNodeRequest(
                    node_type="SubflowGroupNode",
                    specific_library_name=LIBRARY_NAME,
                    node_name="Group_1",
                )
            )
            assert isinstance(group_result, CreateNodeResultSuccess), group_result

            node_names = []
            for i in range(_NODE_COUNT):
                node_result = engine.handle_request(
                    CreateNodeRequest(
                        node_type="TimedEchoNode",
                        specific_library_name=LIBRARY_NAME,
                        node_name=f"Timed_{i}",
                        parent_group_name=group_result.node_name,
                    )
                )
                assert isinstance(node_result, CreateNodeResultSuccess), node_result
                node_names.append(node_result.node_name)

        for i, name in enumerate(node_names):
            engine.handle_request(SetParameterValueRequest(parameter_name="text", node_name=name, value=f"n-{i}"))
            engine.handle_request(
                SetParameterValueRequest(parameter_name="delay", node_name=name, value=_DELAY_SECONDS)
            )

        run_result = await engine.ahandle_request(
            StartFlowRequest(
                flow_name=parent_flow,
                flow_node_name=group_result.node_name,
                wait_for_completion=True,
                completion_timeout_ms=30000,
            )
        )
        assert isinstance(run_result, StartFlowResultSuccess), run_result
    finally:
        config_manager.set_config_value("workflow_execution_mode", prior_mode)
        config_manager.set_config_value("max_nodes_in_parallel", prior_max)

    node_manager = engine.node_manager
    intervals = []
    for i, name in enumerate(node_names):
        node = node_manager.get_node_by_name(name)
        # Before the fix only the first child was seeded; the rest stayed unresolved.
        assert node.state == NodeResolutionState.RESOLVED, f"{name} did not resolve (state={node.state})"
        assert node.parameter_output_values.get("text") == f"n-{i}"
        start = node.parameter_output_values.get("start_ts")
        end = node.parameter_output_values.get("end_ts")
        assert start, f"{name} missing start_ts output"
        assert end, f"{name} missing end_ts output"
        intervals.append((start, end))

    # Concurrent execution means the latest start happens before the earliest end.
    latest_start = max(start for start, _ in intervals)
    earliest_end = min(end for _, end in intervals)
    assert latest_start < earliest_end, (
        f"Independent group nodes did not overlap in time (serial execution): intervals={intervals}"
    )
