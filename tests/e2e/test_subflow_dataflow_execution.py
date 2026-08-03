"""Regression test for issue #4993: data-only referenced subflows must run end to end.

A referenced workflow wired purely with data connections (Start -> ... -> End with no
control edges) runs fine on its own, because the top-level scheduler resolves terminal
data nodes and walks their dependencies backward. When the same workflow is executed as
an isolated subflow (the path SubflowWorkflowNode uses via StartLocalSubflowRequest), the
engine used to seed only the start node and walk forward *control* edges. With no control
edges to follow it resolved just the start node and silently skipped the downstream/leaf
nodes, so the End node never ran and no outputs came back, while the run still reported
success.

This test builds that exact data-only shape inside an isolated subflow and asserts the
end node resolves and its output propagates. It runs in-process (no subprocess) and drives
the real engine execution path, so a regression in the isolated-subflow DAG seeding fails
here.
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
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

if TYPE_CHECKING:
    from collections.abc import Callable

# Timeout with thread dump.
pytestmark = pytest.mark.timeout(300, method="thread")

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "subflow_dataflow_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "subflow_dataflow_nodes.py"
LIBRARY_NAME = "Subflow Dataflow Library"
_EXPECTED = "value-through-data-only-subflow"


@pytest.mark.skipif(
    not FIXTURE_LIBRARY_JSON_TEMPLATE.exists(),
    reason=f"Subflow Dataflow Library fixture missing at {FIXTURE_LIBRARY_JSON_TEMPLATE}",
)
@pytest.mark.asyncio
async def test_isolated_subflow_runs_data_only_graph(
    tmp_path: Path,
    materialize_library: Callable[..., Path],
    create_node: Callable[..., str],
    connect: Callable[..., None],
) -> None:
    """A data-only referenced subflow must resolve its downstream/leaf nodes and return outputs."""
    library_json = materialize_library(
        tmp_path / "library", template=FIXTURE_LIBRARY_JSON_TEMPLATE, node_file=FIXTURE_NODE_FILE
    )
    register_result = GriptapeNodes.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result

    GriptapeNodes.ContextManager().push_workflow(workflow_name="subflow_dataflow_wf")

    # Parent flow holds the driver; the subflow is a child flow, like an imported referenced workflow.
    parent_result = GriptapeNodes.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ParentFlow", set_as_new_context=False)
    )
    assert isinstance(parent_result, CreateFlowResultSuccess), parent_result
    parent_flow = parent_result.flow_name

    subflow_result = GriptapeNodes.handle_request(
        CreateFlowRequest(parent_flow_name=parent_flow, flow_name="SubFlow", set_as_new_context=False)
    )
    assert isinstance(subflow_result, CreateFlowResultSuccess), subflow_result
    subflow = subflow_result.flow_name

    # Subflow: Start -> Pass -> End wired with DATA connections ONLY (no exec_out -> exec_in).
    create_node("DataStartNode", "Start", subflow, library_name=LIBRARY_NAME)
    create_node("DataPassNode", "Pass", subflow, library_name=LIBRARY_NAME)
    create_node("DataEndNode", "End", subflow, library_name=LIBRARY_NAME)
    connect("Start", "value", "Pass", "value")
    connect("Pass", "value", "End", "value")

    GriptapeNodes.handle_request(SetParameterValueRequest(parameter_name="value", node_name="Start", value=_EXPECTED))

    # Driver runs the subflow the way SubflowWorkflowNode does.
    create_node("RunSubflowNode", "Driver", parent_flow, library_name=LIBRARY_NAME)
    driver = GriptapeNodes.NodeManager().get_node_by_name("Driver")
    driver.metadata["subflow_name"] = subflow
    driver.metadata["end_node_name"] = "End"

    run_result = await GriptapeNodes.ahandle_request(
        StartFlowRequest(
            flow_name=parent_flow,
            flow_node_name="Driver",
            wait_for_completion=True,
            completion_timeout_ms=30000,
        )
    )
    assert isinstance(run_result, StartFlowResultSuccess), run_result

    node_manager = GriptapeNodes.NodeManager()
    end_node = node_manager.get_node_by_name("End")
    pass_node = node_manager.get_node_by_name("Pass")
    driver = node_manager.get_node_by_name("Driver")

    # The downstream/leaf nodes must actually run, not get skipped.
    assert pass_node.state == NodeResolutionState.RESOLVED, "Pass node did not resolve in the isolated subflow"
    assert end_node.state == NodeResolutionState.RESOLVED, "End node did not resolve in the isolated subflow"

    # The End node's output must be produced and collectable by the parent.
    assert end_node.parameter_output_values.get("value") == _EXPECTED
    assert driver.parameter_output_values.get("collected_value") == _EXPECTED
