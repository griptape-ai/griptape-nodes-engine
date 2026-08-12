"""End-to-end coverage for node types generated from workflow files.

A library can declare a node in ``workflow_nodes`` instead of ``nodes``, pointing at a saved
workflow file that has Start Flow and End Flow nodes. Registering the library must then produce a
usable node type whose parameters mirror the workflow's saved shape, and running that node must
execute the workflow and hand its End Flow values back as node outputs.

The fixture library ships ``shout_workflow.py`` (Start -> Shout -> End), regenerate it with
``uv run python scripts/generate_shout_workflow_fixture.py`` when the workflow file format changes.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.node_types import NodeResolutionState
from griptape_nodes.exe_types.workflow_node import WorkflowNode
from griptape_nodes.retained_mode.events.execution_events import StartFlowRequest, StartFlowResultSuccess
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    ListFlowsInFlowRequest,
    ListFlowsInFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.library_events import (
    ListNodeTypesInLibraryRequest,
    ListNodeTypesInLibraryResultSuccess,
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

if TYPE_CHECKING:
    from collections.abc import Callable

# Timeout with thread dump.
pytestmark = pytest.mark.timeout(300, method="thread")

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "workflow_node_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "workflow_node_nodes.py"
FIXTURE_WORKFLOW_FILE = FIXTURE_LIBRARY_DIR / "shout_workflow.py"
LIBRARY_NAME = "Workflow Node Library"
WORKFLOW_NODE_TYPE = "ShoutWorkflow"


@pytest.fixture
def registered_library(tmp_path: Path, materialize_library: Callable[..., Path]) -> Path:
    """Materialize and register the fixture library, returning its JSON path."""
    library_json = materialize_library(
        tmp_path / "library",
        template=FIXTURE_LIBRARY_JSON_TEMPLATE,
        node_file=FIXTURE_NODE_FILE,
        extra_files=[FIXTURE_WORKFLOW_FILE],
    )
    register_result = GriptapeNodes.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result
    return library_json


@pytest.mark.skipif(
    not FIXTURE_WORKFLOW_FILE.exists(),
    reason=f"Workflow Node Library fixture workflow missing at {FIXTURE_WORKFLOW_FILE}",
)
def test_workflow_node_registers_with_shape_derived_parameters(
    registered_library: Path,  # noqa: ARG001
    create_node: Callable[..., str],
) -> None:
    """A `workflow_nodes` entry becomes a real node type whose parameters mirror the shape."""
    list_result = GriptapeNodes.handle_request(ListNodeTypesInLibraryRequest(library=LIBRARY_NAME))
    assert isinstance(list_result, ListNodeTypesInLibraryResultSuccess), list_result
    assert WORKFLOW_NODE_TYPE in list_result.node_types

    GriptapeNodes.ContextManager().push_workflow(workflow_name="workflow_node_e2e_shape")

    flow_result = GriptapeNodes.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ParentFlow", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result

    create_node(WORKFLOW_NODE_TYPE, "Shout It", flow_result.flow_name, library_name=LIBRARY_NAME)
    node = GriptapeNodes.NodeManager().get_node_by_name("Shout It")

    assert isinstance(node, WorkflowNode), f"Expected a workflow-backed node, got {type(node).__name__}"
    # `text` comes from the workflow's Start Flow node, `result` from its End Flow node. Control
    # parameters in the shape are dropped in favor of the node's own control flow, and so is the End
    # Flow node's Status group, which reports on that node's run rather than on the workflow's output.
    assert [parameter.name for parameter in node.parameters] == ["exec_in", "exec_out", "text", "result"]
    assert node.get_parameter_by_name("exec_out") is node.control_parameter_out


@pytest.mark.skipif(
    not FIXTURE_WORKFLOW_FILE.exists(),
    reason=f"Workflow Node Library fixture workflow missing at {FIXTURE_WORKFLOW_FILE}",
)
@pytest.mark.asyncio
async def test_workflow_node_runs_its_workflow_and_returns_outputs(
    registered_library: Path,  # noqa: ARG001
    create_node: Callable[..., str],
) -> None:
    """Running the generated node executes the workflow and surfaces its End Flow values."""
    GriptapeNodes.ContextManager().push_workflow(workflow_name="workflow_node_e2e")

    flow_result = GriptapeNodes.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ParentFlow", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    parent_flow = flow_result.flow_name

    create_node(WORKFLOW_NODE_TYPE, "Shout It", parent_flow, library_name=LIBRARY_NAME)
    set_result = GriptapeNodes.handle_request(
        SetParameterValueRequest(parameter_name="text", node_name="Shout It", value="hello there")
    )
    assert set_result.succeeded(), set_result

    run_result = await GriptapeNodes.ahandle_request(
        StartFlowRequest(
            flow_name=parent_flow,
            flow_node_name="Shout It",
            wait_for_completion=True,
            completion_timeout_ms=60000,
        )
    )
    assert isinstance(run_result, StartFlowResultSuccess), run_result

    node = GriptapeNodes.NodeManager().get_node_by_name("Shout It")
    assert node.state == NodeResolutionState.RESOLVED
    assert node.parameter_output_values.get("result") == "HELLO THERE!"

    # The workflow was imported as a child flow of the node's own flow so it can be inspected
    # during the session, and it is tagged transient so a save never bakes it in.
    subflows_result = GriptapeNodes.handle_request(ListFlowsInFlowRequest(parent_flow_name=parent_flow))
    assert isinstance(subflows_result, ListFlowsInFlowResultSuccess), subflows_result
    assert node.metadata["subflow_name"] in subflows_result.flow_names


@pytest.mark.skipif(
    not FIXTURE_WORKFLOW_FILE.exists(),
    reason=f"Workflow Node Library fixture workflow missing at {FIXTURE_WORKFLOW_FILE}",
)
@pytest.mark.asyncio
async def test_two_workflow_nodes_run_independently(
    registered_library: Path,  # noqa: ARG001
    create_node: Callable[..., str],
    connect: Callable[..., None],
) -> None:
    """Two instances of the same generated node each get their own subflow.

    The second import renames the workflow's Start/End nodes (their names are already taken), so
    this covers the path where the saved shape's node names no longer match the live subflow.
    """
    GriptapeNodes.ContextManager().push_workflow(workflow_name="workflow_node_e2e_pair")

    flow_result = GriptapeNodes.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ParentFlow", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    parent_flow = flow_result.flow_name

    create_node(WORKFLOW_NODE_TYPE, "First", parent_flow, library_name=LIBRARY_NAME)
    create_node(WORKFLOW_NODE_TYPE, "Second", parent_flow, library_name=LIBRARY_NAME)
    connect("First", "exec_out", "Second", "exec_in")
    connect("First", "result", "Second", "text")

    set_result = GriptapeNodes.handle_request(
        SetParameterValueRequest(parameter_name="text", node_name="First", value="hey")
    )
    assert set_result.succeeded(), set_result

    run_result = await GriptapeNodes.ahandle_request(
        StartFlowRequest(
            flow_name=parent_flow,
            flow_node_name="First",
            wait_for_completion=True,
            completion_timeout_ms=60000,
        )
    )
    assert isinstance(run_result, StartFlowResultSuccess), run_result

    node_manager = GriptapeNodes.NodeManager()
    first = node_manager.get_node_by_name("First")
    second = node_manager.get_node_by_name("Second")

    assert first.parameter_output_values.get("result") == "HEY!"
    assert second.parameter_output_values.get("result") == "HEY!!"
    assert first.metadata["subflow_name"] != second.metadata["subflow_name"]
