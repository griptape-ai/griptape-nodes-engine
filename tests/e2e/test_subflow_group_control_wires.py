"""A member's control wire that crosses a group boundary rides the group's own control ports.

Issue #6210: grouping a node in the middle of a control chain used to mint a proxy parameter for
its outgoing control wire, so the downstream node hung off the proxy while the executor followed
the group's own, unwired, exit port and ended the run. Downstream nodes stayed UNRESOLVED and the
run still reported success. Issue #5487 is the same thing seen on a loop group, where the minted
proxies drew as data ports.

Now the wire keeps two legs on dedicated ports, one direction each. Outside, it meets the group's
entry or exit port. Inside, it runs from the group's inner entry (On Start, or On Each on a loop)
to the member, or from the member to the inner exit (Group Complete, or Loop Complete on a loop).
Data wires are untouched: they still get a proxy parameter each.
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

FIXTURES = Path(__file__).parent / "fixtures"
SUBFLOW_LIBRARY_JSON = FIXTURES / "subflow_library" / "griptape_nodes_library.json"
SUBFLOW_NODE_FILE = FIXTURES / "subflow_library" / "subflow_echo_node.py"
LOOP_LIBRARY_JSON = FIXTURES / "iterative_group_library" / "griptape_nodes_library.json"
LOOP_NODE_FILE = FIXTURES / "iterative_group_library" / "iterative_group_nodes.py"
SUBFLOW_LIBRARY = "Subflow Group Control Wires Library"
# A loop packages its body with the Start Flow node of the library its execution environment names,
# which is "Griptape Nodes Library", so the loop fixture has to be registered under that name.
LOOP_LIBRARY = "Griptape Nodes Library"
LIBRARIES = {
    SUBFLOW_LIBRARY: (SUBFLOW_LIBRARY_JSON, SUBFLOW_NODE_FILE),
    LOOP_LIBRARY: (LOOP_LIBRARY_JSON, LOOP_NODE_FILE),
}
CONTROL = ParameterTypeBuiltin.CONTROL_TYPE.value


def _start(engine: Engine, materialize_library: Callable[..., Path], tmp_path: Path, library: str) -> str:
    json_path, node_file = LIBRARIES[library]
    library_json = materialize_library(tmp_path / "library", template=json_path, node_file=node_file, name=library)
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result
    engine.context_manager.push_workflow(workflow_name="control_wires_wf")
    flow_result = engine.handle_request(CreateFlowRequest(parent_flow_name=None, set_as_new_context=True))
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    return flow_result.flow_name


@pytest.fixture
def flow(tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path]) -> str:
    """A clean engine with the subflow fixture library registered and an empty top-level flow."""
    return _start(engine, materialize_library, tmp_path, SUBFLOW_LIBRARY)


def _node(engine: Engine, node_type: str, name: str, library: str = SUBFLOW_LIBRARY, **kwargs: object) -> None:
    result = engine.handle_request(
        CreateNodeRequest(node_type=node_type, specific_library_name=library, node_name=name, **kwargs)  # type: ignore[arg-type]
    )
    assert isinstance(result, CreateNodeResultSuccess), result


def _connect(engine: Engine, source: str, source_param: str, target: str, target_param: str) -> None:
    result = engine.handle_request(
        CreateConnectionRequest(
            source_node_name=source,
            source_parameter_name=source_param,
            target_node_name=target,
            target_parameter_name=target_param,
        )
    )
    assert isinstance(result, CreateConnectionResultSuccess), result.result_details


def _edges(engine: Engine, node_name: str, *, control_only: bool = False) -> set[str]:
    """Every edge touching a node, as 'Source.param->Target.param' with ' (internal)' on inner legs."""
    node = engine.node_manager.get_node_by_name(node_name)
    connections = engine.flow_manager.get_connections()
    edges = connections.get_all_outgoing_connections(node) + connections.get_all_incoming_connections(node)
    return {
        f"{e.source_node.name}.{e.source_parameter.name}->{e.target_node.name}.{e.target_parameter.name}"
        + (" (internal)" if e.is_node_group_internal else "")
        for e in edges
        if not control_only or e.source_parameter.output_type == CONTROL
    }


def _grown(engine: Engine, node_name: str) -> list[str]:
    """Parameters a group grew to carry a member's wire (proxies are user-defined)."""
    return [p.name for p in engine.node_manager.get_node_by_name(node_name).parameters if p.user_defined]


def _state(engine: Engine, name: str) -> NodeResolutionState:
    return engine.node_manager.get_node_by_name(name).state


def _group(
    engine: Engine, members: list[str], node_type: str = "SubflowGroupNode", library: str = SUBFLOW_LIBRARY
) -> None:
    _node(engine, node_type, "G", library, node_names_to_add=members)


def _chain(engine: Engine) -> None:
    """Up -> Child -> Down wired for control, with Child also feeding Down's text."""
    for name in ("Up", "Child", "Down"):
        _node(engine, "EchoNode", name)
        assert engine.handle_request(
            SetParameterValueRequest(parameter_name="text", node_name=name, value=name)
        ).succeeded()
    _connect(engine, "Up", "exec_out", "Child", "exec_in")
    _connect(engine, "Child", "exec_out", "Down", "exec_in")


GROUPED_CHAIN = {
    "Up.exec_out->G.group_exec_in",
    "G.on_start->Child.exec_in (internal)",
    "Child.exec_out->G.group_complete (internal)",
    "G.group_exec_out->Down.exec_in",
}


@pytest.mark.skipif(not SUBFLOW_LIBRARY_JSON.exists(), reason="subflow fixture library missing")
class TestGroupingAControlChain:
    def test_the_wire_gets_an_outer_and_an_inner_leg(self, engine: Engine, flow: str) -> None:
        """No proxy is minted; the entry/exit ports carry the outer legs and On Start / Group Complete the inner."""
        _ = flow
        _chain(engine)
        _group(engine, ["Child"])

        assert _edges(engine, "G") == GROUPED_CHAIN
        assert _grown(engine, "G") == []

    @pytest.mark.asyncio
    async def test_the_node_after_the_group_runs(self, engine: Engine, flow: str) -> None:
        """The run used to end at the group's unwired exit port and still report success."""
        _chain(engine)
        _group(engine, ["Child"])

        run_result = await engine.ahandle_request(StartFlowRequest(flow_name=flow, flow_node_name="Up"))
        assert isinstance(run_result, StartFlowResultSuccess), run_result

        assert _state(engine, "Child") == NodeResolutionState.RESOLVED
        assert _state(engine, "Down") == NodeResolutionState.RESOLVED

    def test_re_adding_a_member_changes_nothing(self, engine: Engine, flow: str) -> None:
        """The editor re-sends every member after creating a group; that confirms membership, not re-wires it."""
        _ = flow
        _chain(engine)
        _group(engine, ["Child"])
        before = (_edges(engine, "G"), _edges(engine, "Child"))

        result = engine.handle_request(AddNodesToNodeGroupRequest(node_names=["Child"], node_group_name="G"))

        assert isinstance(result, AddNodesToNodeGroupResultSuccess), result
        assert result.node_names_added == ["Child"]
        assert (_edges(engine, "G"), _edges(engine, "Child")) == before

    def test_grouping_the_successor_wires_it_straight_to_the_member(self, engine: Engine, flow: str) -> None:
        _ = flow
        _chain(engine)
        _group(engine, ["Child"])

        result = engine.handle_request(AddNodesToNodeGroupRequest(node_names=["Down"], node_group_name="G"))
        assert isinstance(result, AddNodesToNodeGroupResultSuccess), result

        assert _edges(engine, "G", control_only=True) == {
            "Up.exec_out->G.group_exec_in",
            "G.on_start->Child.exec_in (internal)",
        }
        assert "Child.exec_out->Down.exec_in" in _edges(engine, "Child")

    def test_grouping_the_caller_wires_it_straight_to_the_member(self, engine: Engine, flow: str) -> None:
        _ = flow
        _chain(engine)
        _group(engine, ["Child"])

        result = engine.handle_request(AddNodesToNodeGroupRequest(node_names=["Up"], node_group_name="G"))
        assert isinstance(result, AddNodesToNodeGroupResultSuccess), result

        assert _edges(engine, "G", control_only=True) == {
            "Child.exec_out->G.group_complete (internal)",
            "G.group_exec_out->Down.exec_in",
        }
        assert "Up.exec_out->Child.exec_in" in _edges(engine, "Child")


@pytest.mark.skipif(not SUBFLOW_LIBRARY_JSON.exists(), reason="subflow fixture library missing")
class TestUngrouping:
    def test_ungrouping_the_only_member_restores_the_chain(self, engine: Engine, flow: str) -> None:
        """One caller, one member, one successor: the pairing is unambiguous, so the chain comes back."""
        _ = flow
        _chain(engine)
        _group(engine, ["Child"])

        result = engine.handle_request(RemoveNodeFromNodeGroupRequest(node_names=["Child"], node_group_name="G"))
        assert isinstance(result, RemoveNodeFromNodeGroupResultSuccess), result

        assert _edges(engine, "Child", control_only=True) == {
            "Up.exec_out->Child.exec_in",
            "Child.exec_out->Down.exec_in",
        }
        assert _edges(engine, "G", control_only=True) == set()
        group = engine.node_manager.get_node_by_name("G")
        assert [
            p.name
            for p in group.parameters
            if p.name in {"group_exec_in", "group_exec_out", "on_start", "group_complete"}
        ] == [
            "group_exec_in",
            "group_exec_out",
            "on_start",
            "group_complete",
        ]

    def test_ungrouping_one_of_two_exiting_members_leaves_the_exit_with_the_group(
        self, engine: Engine, flow: str
    ) -> None:
        """Two members lead to the exit port, so its successor can't be said to be either one's."""
        _ = flow
        for name in ("A", "B", "Down"):
            _node(engine, "EchoNode", name)
        _connect(engine, "A", "exec_out", "Down", "exec_in")
        _connect(engine, "B", "exec_out", "Down", "exec_in")
        _group(engine, ["A", "B"])
        assert _edges(engine, "G", control_only=True) == {
            "A.exec_out->G.group_complete (internal)",
            "B.exec_out->G.group_complete (internal)",
            "G.group_exec_out->Down.exec_in",
        }

        result = engine.handle_request(RemoveNodeFromNodeGroupRequest(node_names=["A"], node_group_name="G"))
        assert isinstance(result, RemoveNodeFromNodeGroupResultSuccess), result

        assert _edges(engine, "A", control_only=True) == set()
        assert _edges(engine, "G", control_only=True) == {
            "B.exec_out->G.group_complete (internal)",
            "G.group_exec_out->Down.exec_in",
        }


@pytest.mark.skipif(not LOOP_LIBRARY_JSON.exists(), reason="loop fixture library missing")
class TestLoopGroup:
    @pytest.mark.asyncio
    async def test_a_loop_carries_the_wire_on_on_each_and_loop_complete(
        self, tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path]
    ) -> None:
        flow_name = _start(engine, materialize_library, tmp_path, LOOP_LIBRARY)
        _node(engine, "StartFlow", "Up", LOOP_LIBRARY)
        _node(engine, "CounterControlNode", "Child", LOOP_LIBRARY)
        _node(engine, "EndFlow", "Down", LOOP_LIBRARY)
        _connect(engine, "Up", "exec_out", "Child", "exec_in")
        _connect(engine, "Child", "exec_out", "Down", "exec_in")
        _group(engine, ["Child"], node_type="ProbeForEachGroupNode", library=LOOP_LIBRARY)

        assert _edges(engine, "G", control_only=True) == {
            "Up.exec_out->G.exec_in",
            "G.on_each->Child.exec_in (internal)",
            "Child.exec_out->G.loop_complete (internal)",
            "G.exec_out->Down.exec_in",
        }
        assert _grown(engine, "G") == []
        assert engine.node_manager.get_node_by_name("G").get_parameter_by_name("on_start") is None

        run_result = await engine.ahandle_request(StartFlowRequest(flow_name=flow_name, flow_node_name="Up"))
        assert isinstance(run_result, StartFlowResultSuccess), run_result
        assert _state(engine, "Down") == NodeResolutionState.RESOLVED


@pytest.mark.skipif(not SUBFLOW_LIBRARY_JSON.exists(), reason="subflow fixture library missing")
class TestDataWiresAreUnchanged:
    """Only control wires changed; data wires still get one proxy parameter each."""

    def test_a_data_wire_crossing_the_boundary_gets_a_proxy(self, engine: Engine, flow: str) -> None:
        _ = flow
        _chain(engine)
        _connect(engine, "Child", "text", "Down", "text")
        _group(engine, ["Child"])

        assert _grown(engine, "G") == ["text"]
        assert {"Child.text->G.text (internal)", "G.text->Down.text"} <= _edges(engine, "G")

    def test_the_editors_group_then_re_add_keeps_the_data_wire(self, engine: Engine, flow: str) -> None:
        """The editor creates the group with the node, then re-sends it; the data wire must survive both."""
        _ = flow
        _chain(engine)
        _connect(engine, "Child", "text", "Down", "text")
        _group(engine, ["Child"])

        result = engine.handle_request(AddNodesToNodeGroupRequest(node_names=["Child"], node_group_name="G"))

        assert isinstance(result, AddNodesToNodeGroupResultSuccess), result
        assert _grown(engine, "G") == ["text"]
        assert {"Child.text->G.text (internal)", "G.text->Down.text"} <= _edges(engine, "G")

    def test_one_output_feeding_two_outside_nodes_keeps_both_wires(self, engine: Engine, flow: str) -> None:
        _ = flow
        for name in ("Child", "Down1", "Down2"):
            _node(engine, "EchoNode", name)
        _connect(engine, "Child", "text", "Down1", "text")
        _connect(engine, "Child", "text", "Down2", "text")
        _group(engine, ["Child"])

        assert _grown(engine, "G") == ["text"]
        assert _edges(engine, "G") == {"Child.text->G.text (internal)", "G.text->Down1.text", "G.text->Down2.text"}
