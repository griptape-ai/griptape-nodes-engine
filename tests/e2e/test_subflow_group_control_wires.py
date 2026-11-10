"""A member's control wire that crosses a Subflow Node Group boundary rides the group's own ports.

Issue #6210: grouping a node in the middle of a control chain used to mint a proxy parameter for
its outgoing control wire, so the downstream node hung off the proxy while the executor followed
the group's own, unwired, exit port and ended the run. Downstream nodes stayed UNRESOLVED and the
run still reported success. Issue #5487 is the same thing seen on a loop group, where the minted
proxies drew as data ports.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.core_types import ParameterTypeBuiltin
from griptape_nodes.exe_types.node_types import NodeResolutionState
from griptape_nodes.retained_mode.events.connection_events import CreateConnectionRequest, CreateConnectionResultSuccess
from griptape_nodes.retained_mode.events.execution_events import StartFlowRequest, StartFlowResultSuccess
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import (
    AddNodesToNodeGroupRequest,
    AddNodesToNodeGroupResultSuccess,
    CreateNodeRequest,
    CreateNodeResultSuccess,
    RemoveNodeFromNodeGroupRequest,
    RemoveNodeFromNodeGroupResultSuccess,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.engine import Engine

# Timeout with thread dump.
pytestmark = pytest.mark.timeout(300, method="thread")

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "subflow_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "subflow_echo_node.py"
# Distinct from the other in-process subflow tests, whose LOADED bookkeeping leaks across a worker.
LIBRARY_NAME = "Subflow Group Control Wires Library"
CONTROL = ParameterTypeBuiltin.CONTROL_TYPE.value


@pytest.fixture
def flow(tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path]) -> str:
    """A clean engine with the fixture library registered and an empty top-level flow in context."""
    library_json = materialize_library(
        tmp_path / "library", template=FIXTURE_LIBRARY_JSON_TEMPLATE, node_file=FIXTURE_NODE_FILE, name=LIBRARY_NAME
    )
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result
    engine.context_manager.push_workflow(workflow_name="control_wires_wf")
    flow_result = engine.handle_request(CreateFlowRequest(parent_flow_name=None, set_as_new_context=True))
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    return flow_result.flow_name


def _edges(engine: Engine, node_name: str) -> set[str]:
    """Every edge touching a node, as 'Source.param->Target.param', so a failure names the wiring."""
    node = engine.node_manager.get_node_by_name(node_name)
    connections = engine.flow_manager.get_connections()
    edges = connections.get_all_outgoing_connections(node) + connections.get_all_incoming_connections(node)
    return {
        f"{e.source_node.name}.{e.source_parameter.name}->{e.target_node.name}.{e.target_parameter.name}" for e in edges
    }


def _control_ports(engine: Engine, node_name: str) -> list[str]:
    node = engine.node_manager.get_node_by_name(node_name)
    return [p.name for p in node.parameters if p.output_type == CONTROL or CONTROL in p.input_types]


def _build_grouped_chain(engine: Engine) -> None:
    """Up -> Child -> Down wired for control, then Child grouped into G."""
    for name in ("Up", "Child", "Down"):
        result = engine.handle_request(
            CreateNodeRequest(node_type="EchoNode", specific_library_name=LIBRARY_NAME, node_name=name)
        )
        assert isinstance(result, CreateNodeResultSuccess), result
        set_result = engine.handle_request(SetParameterValueRequest(parameter_name="text", node_name=name, value=name))
        assert set_result.succeeded(), set_result
    for source, target in (("Up", "Child"), ("Child", "Down")):
        result = engine.handle_request(
            CreateConnectionRequest(
                source_node_name=source,
                source_parameter_name="exec_out",
                target_node_name=target,
                target_parameter_name="exec_in",
            )
        )
        assert isinstance(result, CreateConnectionResultSuccess), result.result_details
    result = engine.handle_request(
        CreateNodeRequest(
            node_type="SubflowGroupNode", specific_library_name=LIBRARY_NAME, node_name="G", node_names_to_add=["Child"]
        )
    )
    assert isinstance(result, CreateNodeResultSuccess), result


@pytest.mark.skipif(not FIXTURE_LIBRARY_JSON_TEMPLATE.exists(), reason="subflow fixture library missing")
class TestGroupingAControlChain:
    def test_control_wires_move_onto_the_groups_own_ports(self, engine: Engine, flow: str) -> None:
        """No proxy is minted and no inner leg is kept: the wires now belong to the group."""
        _ = flow
        _build_grouped_chain(engine)

        assert _control_ports(engine, "G") == ["group_exec_in", "group_exec_out"]
        assert _edges(engine, "G") == {"Up.exec_out->G.group_exec_in", "G.group_exec_out->Down.exec_in"}
        assert not {e for e in _edges(engine, "Child") if "exec_" in e}

    @pytest.mark.asyncio
    async def test_the_node_after_the_group_runs(self, engine: Engine, flow: str) -> None:
        """The run used to end at the group's unwired exit port and still report success."""
        _build_grouped_chain(engine)

        run_result = await engine.ahandle_request(StartFlowRequest(flow_name=flow, flow_node_name="Up"))
        assert isinstance(run_result, StartFlowResultSuccess), run_result

        assert engine.node_manager.get_node_by_name("Child").state == NodeResolutionState.RESOLVED
        assert engine.node_manager.get_node_by_name("Down").state == NodeResolutionState.RESOLVED

    def test_ungrouping_leaves_the_control_wires_with_the_group(self, engine: Engine, flow: str) -> None:
        """A group is entered and left as a whole, so its wires are its own; the member leaves unwired."""
        _ = flow
        _build_grouped_chain(engine)

        result = engine.handle_request(RemoveNodeFromNodeGroupRequest(node_names=["Child"], node_group_name="G"))
        assert isinstance(result, RemoveNodeFromNodeGroupResultSuccess), result

        assert _edges(engine, "G") == {"Up.exec_out->G.group_exec_in", "G.group_exec_out->Down.exec_in"}
        assert not {e for e in _edges(engine, "Child") if "exec_" in e}
        assert _control_ports(engine, "G") == ["group_exec_in", "group_exec_out"]

    def test_grouping_the_node_the_exit_port_feeds_drops_that_wire(self, engine: Engine, flow: str) -> None:
        """A group's exit cannot point into its own member, so that wire goes when the node joins."""
        _ = flow
        _build_grouped_chain(engine)

        result = engine.handle_request(AddNodesToNodeGroupRequest(node_names=["Down"], node_group_name="G"))
        assert isinstance(result, AddNodesToNodeGroupResultSuccess), result

        assert _edges(engine, "G") == {"Up.exec_out->G.group_exec_in"}
        assert _control_ports(engine, "G") == ["group_exec_in", "group_exec_out"]
