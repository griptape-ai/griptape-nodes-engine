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
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest

from griptape_nodes.exe_types.core_types import ParameterTypeBuiltin
from griptape_nodes.exe_types.node_types import NodeResolutionState
from griptape_nodes.retained_mode.events.connection_events import CreateConnectionRequest, CreateConnectionResultSuccess
from griptape_nodes.retained_mode.events.execution_events import (
    StartFlowRequest,
    StartFlowResultSuccess,
    StartLocalSubflowRequest,
)
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

    from griptape_nodes.exe_types.node_groups.subflow_node_group import SubflowNodeGroup
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

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

    def test_ungrouping_a_body_member_leaves_the_loops_own_wires_untouched(
        self, tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path]
    ) -> None:
        """Start Loop / On Complete are wired directly by the artist here, not drawn by grouping.

        Ungrouping a body member must never touch them on that basis alone, however many wires
        happen to sit on the port at the time: On Each and Loop Complete are a loop's own
        user-facing controls, not minted proxies.
        """
        _start(engine, materialize_library, tmp_path, LOOP_LIBRARY)
        _node(engine, "StartFlow", "Up", LOOP_LIBRARY)
        _node(engine, "EndFlow", "Down", LOOP_LIBRARY)
        _node(engine, "ProbeForEachGroupNode", "G", LOOP_LIBRARY)
        _connect(engine, "Up", "exec_out", "G", "exec_in")
        _connect(engine, "G", "exec_out", "Down", "exec_in")
        _node(engine, "CounterControlNode", "Body1", LOOP_LIBRARY)
        _node(engine, "CounterControlNode", "Body2", LOOP_LIBRARY)
        add_result = engine.handle_request(
            AddNodesToNodeGroupRequest(node_names=["Body1", "Body2"], node_group_name="G")
        )
        assert isinstance(add_result, AddNodesToNodeGroupResultSuccess), add_result
        _connect(engine, "G", "on_each", "Body1", "exec_in")
        _connect(engine, "Body2", "exec_out", "G", "loop_complete")

        remove_body1 = engine.handle_request(RemoveNodeFromNodeGroupRequest(node_names=["Body1"], node_group_name="G"))
        assert isinstance(remove_body1, RemoveNodeFromNodeGroupResultSuccess), remove_body1
        assert _edges(engine, "Up", control_only=True) == {"Up.exec_out->G.exec_in"}
        assert "Up.exec_out->Body1.exec_in" not in _edges(engine, "Body1", control_only=True)

        remove_body2 = engine.handle_request(RemoveNodeFromNodeGroupRequest(node_names=["Body2"], node_group_name="G"))
        assert isinstance(remove_body2, RemoveNodeFromNodeGroupResultSuccess), remove_body2
        assert _edges(engine, "Down", control_only=True) == {"G.exec_out->Down.exec_in"}
        assert "Body2.exec_out->Down.exec_in" not in _edges(engine, "Body2", control_only=True)


@pytest.mark.skipif(not SUBFLOW_LIBRARY_JSON.exists(), reason="subflow fixture library missing")
class TestOnStartEntryResolution:
    """On Start now actually controls a plain group's entry, for every execution path that packages one.

    NodeExecutor.resolve_inner_entry_node reads the inner entry's wiring on any SubflowNodeGroup,
    not just a loop's On Each; it used to answer (None, None) for anything that was not a
    BaseIterativeNodeGroup. SubflowNodeGroup.execute_subflow (local execution) and
    NodeExecutor._publish_local_workflow (isolated/library execution) both now call it and act on
    the result, instead of always leaving the group's entry at its implicit default.
    """

    def test_a_plain_groups_on_start_resolves_to_its_wired_target(self, engine: Engine, flow: str) -> None:
        """On Start wired to a non-default (not first-in-chain) member resolves to that member."""
        _ = flow
        for name in ("Child1", "Child2", "Child3"):
            _node(engine, "EchoNode", name)
            assert engine.handle_request(
                SetParameterValueRequest(parameter_name="text", node_name=name, value=name)
            ).succeeded()
        _connect(engine, "Child1", "exec_out", "Child2", "exec_in")
        _connect(engine, "Child2", "exec_out", "Child3", "exec_in")
        _group(engine, ["Child1", "Child2", "Child3"])
        # Child1 is first in the internal chain and has no incoming control wire, so implicit
        # child-discovery would pick it; On Start explicitly names Child2 instead.
        _connect(engine, "G", "on_start", "Child2", "exec_in")

        group = cast("SubflowNodeGroup", engine.node_manager.get_node_by_name("G"))
        entry = engine.flow_manager.node_executor.resolve_inner_entry_node(group)

        assert entry == ("Child2", "exec_in")

    def test_an_unwired_on_start_still_falls_back_to_none(self, engine: Engine, flow: str) -> None:
        """Leaving On Start unwired keeps today's fallback to implicit child-discovery."""
        _ = flow
        _node(engine, "EchoNode", "Child")
        assert engine.handle_request(
            SetParameterValueRequest(parameter_name="text", node_name="Child", value="Child")
        ).succeeded()
        _group(engine, ["Child"])

        group = cast("SubflowNodeGroup", engine.node_manager.get_node_by_name("G"))
        entry = engine.flow_manager.node_executor.resolve_inner_entry_node(group)

        assert entry == (None, None)

    @pytest.mark.asyncio
    async def test_on_start_is_passed_as_the_local_subflows_explicit_start_node(
        self, engine: Engine, flow: str
    ) -> None:
        """End to end on the local execution path: On Start reaches StartLocalSubflowRequest itself.

        An isolated subflow run resolves every independently-reachable node regardless of which one
        is named `start_node` -- "a data-only subflow (or a group of independent nodes) resolves
        all its leaf nodes in parallel instead of stopping at the start node" is the documented
        contract of `ControlFlowMachine._process_nodes_for_dag`, confirmed empirically here: Child1
        and Child2 both resolve whether or not this fix is in place, and their execution order is
        set by control-vs-data classification, not by `start_node`. So resolution outcome isn't the
        signal that distinguishes before/after; the request this fix constructs is. Before this fix,
        `start_node` was always None (auto-detect); this asserts it now carries On Start's target.
        """
        _ = flow
        for name in ("Child1", "Child2"):
            _node(engine, "EchoNode", name)
            assert engine.handle_request(
                SetParameterValueRequest(parameter_name="text", node_name=name, value=name)
            ).succeeded()
        _group(engine, ["Child1", "Child2"])
        _connect(engine, "G", "on_start", "Child2", "exec_in")

        original = engine.flow_manager.on_start_local_subflow_request_in_workflow
        captured_start_nodes: list[str | None] = []

        async def spy(request: StartLocalSubflowRequest, workflow: WorkflowRuntime) -> object:
            captured_start_nodes.append(request.start_node)
            return await original(request, workflow)

        with patch.object(engine.flow_manager, "on_start_local_subflow_request_in_workflow", side_effect=spy):
            run_result = await engine.ahandle_request(StartFlowRequest(flow_name=flow, flow_node_name="G"))
        assert isinstance(run_result, StartFlowResultSuccess), run_result

        assert captured_start_nodes == ["Child2"]

    @pytest.mark.asyncio
    async def test_on_start_controls_the_packaged_entry_for_isolated_execution(
        self, tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path]
    ) -> None:
        """End to end on the packaging side: the published flow's Start node wires to On Start's target.

        _publish_local_workflow packages and saves the flow without running it in a subprocess, so
        this is testable without a real worker or GT_CLOUD_API_KEY -- only _execute_subprocess /
        _publish_library_workflow need those, and this stops short of calling them, the same way
        test_isolated_execution_rename.py never runs a group to check its execution_environment.
        """
        flow_name = _start(engine, materialize_library, tmp_path, SUBFLOW_LIBRARY)
        loop_library_json = materialize_library(
            tmp_path / "loop_library", template=LOOP_LIBRARY_JSON, node_file=LOOP_NODE_FILE, name=LOOP_LIBRARY
        )
        register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(loop_library_json)))
        assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result
        _ = flow_name

        _node(engine, "EchoNode", "Child1")
        assert engine.handle_request(
            SetParameterValueRequest(parameter_name="text", node_name="Child1", value="Child1")
        ).succeeded()
        _node(engine, "EchoNode", "Child2")
        assert engine.handle_request(
            SetParameterValueRequest(parameter_name="text", node_name="Child2", value="Child2")
        ).succeeded()
        _group(engine, ["Child1", "Child2"])
        _connect(engine, "G", "on_start", "Child2", "exec_in")

        group = cast("SubflowNodeGroup", engine.node_manager.get_node_by_name("G"))
        publish_result = await engine.flow_manager.node_executor._publish_local_workflow(group)
        assert publish_result is not None

        serialized = publish_result.package_result.serialized_flow_commands
        uuid_to_name = {
            command.node_uuid: command.create_node_command.node_name for command in serialized.serialized_node_commands
        }
        start_uuids = {
            command.node_uuid
            for command in serialized.serialized_node_commands
            if command.create_node_command.node_type == "StartFlow"
        }
        entry_targets = {
            uuid_to_name[connection.target_node_uuid]
            for connection in serialized.serialized_connections
            if connection.source_node_uuid in start_uuids
        }

        assert entry_targets == {"Child2"}


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
