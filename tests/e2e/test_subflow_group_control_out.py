"""Nodes wired after a Subflow Node Group run.

See https://github.com/griptape-ai/griptape-nodes-engine/issues/6210.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.node_types import NodeResolutionState
from griptape_nodes.retained_mode.events.connection_events import CreateConnectionRequest
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

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "subflow_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "subflow_echo_node.py"
# Distinct name so the LibraryManager's LOADED bookkeeping doesn't collide with the
# other in-process subflow tests sharing a worker.
LIBRARY_NAME = "Subflow Group Control Out Library"


@pytest.mark.asyncio
@pytest.mark.parametrize("tail_control_source", ["Middle.exec_out", "Group.group_exec_out"])
async def test_node_after_grouped_node_runs(
    tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path], tail_control_source: str
) -> None:
    """Tail runs whether its Flow In came from Middle before grouping or from the group's Flow Out."""
    library_json = materialize_library(
        tmp_path / "library", template=FIXTURE_LIBRARY_JSON_TEMPLATE, node_file=FIXTURE_NODE_FILE, name=LIBRARY_NAME
    )
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result

    engine.context_manager.push_workflow(workflow_name="subflow_control_out_wf")
    flow_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ParentFlow", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    flow_name = flow_result.flow_name

    with engine.context_manager.flow(flow_name):
        for node_name in ("Head", "Middle", "Tail"):
            created = engine.handle_request(
                CreateNodeRequest(node_type="EchoNode", specific_library_name=LIBRARY_NAME, node_name=node_name)
            )
            assert isinstance(created, CreateNodeResultSuccess), created

        set_result = engine.handle_request(
            SetParameterValueRequest(node_name="Head", parameter_name="text", value="hello")
        )
        assert set_result.succeeded(), set_result

        wires = [("Head.exec_out", "Middle.exec_in"), ("Head.text", "Middle.text"), ("Middle.text", "Tail.text")]
        if tail_control_source == "Middle.exec_out":
            wires.append(("Middle.exec_out", "Tail.exec_in"))
        for source, target in wires:
            _connect(engine, source, target)

        group = engine.handle_request(
            CreateNodeRequest(
                node_type="SubflowGroupNode",
                specific_library_name=LIBRARY_NAME,
                node_name="Group",
                node_names_to_add=["Middle"],
            )
        )
        assert isinstance(group, CreateNodeResultSuccess), group
        if tail_control_source == "Group.group_exec_out":
            _connect(engine, "Group.group_exec_out", "Tail.exec_in")

    run_result = await engine.ahandle_request(StartFlowRequest(flow_name=flow_name, flow_node_name="Head"))
    assert isinstance(run_result, StartFlowResultSuccess), run_result

    tail = engine.node_manager.get_node_by_name("Tail")
    assert tail.state == NodeResolutionState.RESOLVED, tail.state
    assert tail.parameter_output_values.get("text") == "hello"


def _connect(engine: Engine, source: str, target: str) -> None:
    source_node, source_parameter = source.split(".")
    target_node, target_parameter = target.split(".")
    connected = engine.handle_request(
        CreateConnectionRequest(
            source_node_name=source_node,
            source_parameter_name=source_parameter,
            target_node_name=target_node,
            target_parameter_name=target_parameter,
        )
    )
    assert connected.succeeded(), connected
