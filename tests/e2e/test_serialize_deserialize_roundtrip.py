"""Round-trip coverage for SerializeFlowToCommands -> DeserializeFlowFromCommands.

These two halves disagreed about scope. The serializer aggregates each Flow's connections up
through every level of nesting, so an edge crossing a Flow boundary is reported by the parent as
well as the child. The deserializer used to rebuild only its own level's nodes before wiring
connections, and created subflows last, so any edge naming a node one level down failed with "node
did not exist within the flow" and took the whole load down with it.

That combination is what restoring a workflow embedded in image metadata does
(ExtractFlowCommandsFromImageMetadata with deserialize=True), so a graph containing a node group --
which always has boundary-crossing wall connections -- could not be restored at all. Nothing
covered the pairing: the only other test that deserializes uses a single flow with no subflow, so
there was nothing to aggregate and the disagreement stayed invisible.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.node_groups.base_node_group import BaseNodeGroup
from griptape_nodes.retained_mode.engine import current_engine
from griptape_nodes.retained_mode.events.connection_events import CreateConnectionRequest, CreateConnectionResultSuccess
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    DeserializeFlowFromCommandsRequest,
    DeserializeFlowFromCommandsResultSuccess,
    SerializeFlowToCommandsRequest,
    SerializeFlowToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import (
    AddNodesToNodeGroupRequest,
    AddNodesToNodeGroupResultSuccess,
    CreateNodeRequest,
    CreateNodeResultSuccess,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.events.flow_events import SerializedFlowCommands

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "subflow_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "subflow_echo_node.py"

_EXPECTED_TEXT = "value that has to survive the round trip"


@pytest.fixture
def library_name(tmp_path: Path, materialize_library: Callable[..., Path]) -> str:
    """Register the subflow fixture library into a clean engine and return its name."""
    library_json = materialize_library(
        tmp_path / "library", template=FIXTURE_LIBRARY_JSON_TEMPLATE, node_file=FIXTURE_NODE_FILE
    )
    GriptapeNodes.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    register_result = GriptapeNodes.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result
    GriptapeNodes.ContextManager().push_workflow(workflow_name="roundtrip_workflow")
    return register_result.library_name


def _serialize(flow_name: str) -> SerializedFlowCommands:
    serialize_result = GriptapeNodes.handle_request(SerializeFlowToCommandsRequest(flow_name=flow_name))
    assert isinstance(serialize_result, SerializeFlowToCommandsResultSuccess), serialize_result
    return serialize_result.serialized_flow_commands


def _deserialize_into_fresh_context(
    serialized_flow_commands: SerializedFlowCommands,
) -> DeserializeFlowFromCommandsResultSuccess:
    """Replay commands the way an image-metadata restore does, and require success."""
    GriptapeNodes.ContextManager().push_workflow(workflow_name="roundtrip_workflow_restored")
    result = GriptapeNodes.handle_request(
        DeserializeFlowFromCommandsRequest(serialized_flow_commands=serialized_flow_commands)
    )
    assert isinstance(result, DeserializeFlowFromCommandsResultSuccess), result
    return result


def _create_node(node_type: str, node_name: str, library: str, **kwargs: object) -> str:
    result = GriptapeNodes.handle_request(
        CreateNodeRequest(node_type=node_type, specific_library_name=library, node_name=node_name, **kwargs)  # type: ignore[arg-type]
    )
    assert isinstance(result, CreateNodeResultSuccess), result
    return result.node_name


def _get_group(node_name: str) -> BaseNodeGroup:
    """Fetch a rebuilt node that must be a group, so group membership can be inspected."""
    node = GriptapeNodes.NodeManager().get_node_by_name(node_name)
    assert isinstance(node, BaseNodeGroup), (
        f"expected '{node_name}' to be rebuilt as a group, got {type(node).__name__}"
    )
    return node


def _connect(source_node: str, target_node: str, parameter_name: str = "text") -> None:
    result = GriptapeNodes.handle_request(
        CreateConnectionRequest(
            source_node_name=source_node,
            source_parameter_name=parameter_name,
            target_node_name=target_node,
            target_parameter_name=parameter_name,
        )
    )
    assert isinstance(result, CreateConnectionResultSuccess), result


def _edges_from(node_name: str) -> set[str]:
    """Every outgoing edge of a node, as 'Source.param->Target.param' strings.

    Returned as a set of readable labels so a failure names the edges rather than dumping objects,
    and counted so a duplicated edge is as visible as a missing one.
    """
    node = GriptapeNodes.NodeManager().get_node_by_name(node_name)
    connections = GriptapeNodes.FlowManager().get_connections()
    return {
        f"{edge.source_node.name}.{edge.source_parameter.name}->{edge.target_node.name}.{edge.target_parameter.name}"
        for edge in connections.get_all_outgoing_connections(node)
    }


class TestPlainSubflowRoundTrip:
    def test_restores_a_connection_that_crosses_a_flow_boundary(self, library_name: str) -> None:
        """No node groups involved: two plain flows and one edge between them."""
        parent = GriptapeNodes.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="RoundTripParent", set_as_new_context=False)
        )
        assert isinstance(parent, CreateFlowResultSuccess), parent
        child = GriptapeNodes.handle_request(
            CreateFlowRequest(parent_flow_name=parent.flow_name, flow_name="RoundTripChild", set_as_new_context=False)
        )
        assert isinstance(child, CreateFlowResultSuccess), child

        with GriptapeNodes.ContextManager().flow(parent.flow_name):
            source = _create_node("EchoNode", "Source", library_name)
        with GriptapeNodes.ContextManager().flow(child.flow_name):
            target = _create_node("EchoNode", "Target", library_name)
        _connect(source, target)

        commands = _serialize(parent.flow_name)

        GriptapeNodes.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        result = _deserialize_into_fresh_context(commands)

        # The child flow, its node, and the cross-boundary edge all have to come back.
        restored_target = result.node_name_mappings["Target"]
        restored_source = result.node_name_mappings["Source"]
        node_manager = GriptapeNodes.NodeManager()
        assert node_manager.get_node_by_name(restored_target) is not None
        assert node_manager.get_node_by_name(restored_source) is not None
        assert _edges_from(restored_source) == {f"{restored_source}.text->{restored_target}.text"}


class TestNodeGroupRoundTrip:
    def test_restores_a_single_level_group(self, library_name: str) -> None:
        """A group's wall connections cross its boundary, so this failed for every group."""
        flow = GriptapeNodes.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="GroupRoundTrip", set_as_new_context=False)
        )
        assert isinstance(flow, CreateFlowResultSuccess), flow

        with GriptapeNodes.ContextManager().flow(flow.flow_name):
            group = _create_node("SubflowGroupNode", "Group", library_name)
            leaf = _create_node("EchoNode", "Leaf", library_name, parent_group_name=group)
            source = _create_node("EchoNode", "Source", library_name)
            GriptapeNodes.handle_request(
                SetParameterValueRequest(parameter_name="text", node_name=source, value=_EXPECTED_TEXT)
            )
            _connect(source, leaf)

        commands = _serialize(flow.flow_name)

        GriptapeNodes.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        result = _deserialize_into_fresh_context(commands)

        restored_group = _get_group(result.node_name_mappings["Group"])
        restored_leaf_name = result.node_name_mappings["Leaf"]
        assert restored_leaf_name in restored_group.nodes

    def test_restores_a_nested_group_and_its_deepest_member(self, library_name: str) -> None:
        """Nesting is the case with edges spanning more than one boundary."""
        flow = GriptapeNodes.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="NestedRoundTrip", set_as_new_context=False)
        )
        assert isinstance(flow, CreateFlowResultSuccess), flow

        with GriptapeNodes.ContextManager().flow(flow.flow_name):
            outer = _create_node("SubflowGroupNode", "OuterGroup", library_name)
            inner = _create_node("SubflowGroupNode", "InnerGroup", library_name)
            leaf = _create_node("EchoNode", "Leaf", library_name, parent_group_name=inner)
            source = _create_node("EchoNode", "Source", library_name)

            nest_result = GriptapeNodes.handle_request(
                AddNodesToNodeGroupRequest(node_names=[inner], node_group_name=outer)
            )
            assert isinstance(nest_result, AddNodesToNodeGroupResultSuccess), nest_result

            GriptapeNodes.handle_request(
                SetParameterValueRequest(parameter_name="text", node_name=source, value=_EXPECTED_TEXT)
            )
            _connect(source, leaf)

        commands = _serialize(flow.flow_name)

        GriptapeNodes.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        result = _deserialize_into_fresh_context(commands)

        restored_outer = _get_group(result.node_name_mappings["OuterGroup"])
        restored_inner = _get_group(result.node_name_mappings["InnerGroup"])
        restored_leaf = GriptapeNodes.NodeManager().get_node_by_name(result.node_name_mappings["Leaf"])

        # Membership has to survive at both levels, and transitively.
        assert restored_inner.name in restored_outer.nodes
        assert restored_leaf.name in restored_inner.nodes
        assert restored_outer.contains_node(restored_leaf)

        # The subflows have to be nested the same way the groups are.
        inner_subflow = restored_inner.metadata.get("subflow_name")
        outer_subflow = restored_outer.metadata.get("subflow_name")
        assert isinstance(inner_subflow, str), "inner group lost its subflow on load"
        assert GriptapeNodes.FlowManager().get_parent_flow(inner_subflow) == outer_subflow

    def test_creates_each_connection_once_even_though_every_level_reports_it(
        self, library_name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A nested graph must not re-issue an edge once per level of nesting.

        The serializer copies each subflow's connections up into every ancestor, so an edge crossing
        two group boundaries is offered to the deserializer three times. Re-issuing it is not
        harmless: CreateConnection treats a duplicate as a restricted scenario and deletes the
        existing edge before rebuilding it, and that delete decrements the refcount protecting the
        group's proxy parameter -- so the load path tears down the wiring it just restored.

        The final graph looks identical either way, which is why this counts requests instead of
        inspecting the result.
        """
        flow = GriptapeNodes.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="DedupeRoundTrip", set_as_new_context=False)
        )
        assert isinstance(flow, CreateFlowResultSuccess), flow

        with GriptapeNodes.ContextManager().flow(flow.flow_name):
            outer = _create_node("SubflowGroupNode", "OuterGroup", library_name)
            inner = _create_node("SubflowGroupNode", "InnerGroup", library_name)
            leaf = _create_node("EchoNode", "Leaf", library_name, parent_group_name=inner)
            source = _create_node("EchoNode", "Source", library_name)

            nest_result = GriptapeNodes.handle_request(
                AddNodesToNodeGroupRequest(node_names=[inner], node_group_name=outer)
            )
            assert isinstance(nest_result, AddNodesToNodeGroupResultSuccess), nest_result
            _connect(source, leaf)

        commands = _serialize(flow.flow_name)

        # Prove the premise: the same edge really is reported by more than one level.
        def count_serialized_edges(serialized: SerializedFlowCommands) -> int:
            total = len(serialized.serialized_connections)
            for sub_flow in serialized.sub_flows_commands:
                total += count_serialized_edges(sub_flow)
            return total

        def collect_edge_keys(serialized: SerializedFlowCommands, keys: set[object]) -> None:
            for connection in serialized.serialized_connections:
                keys.add(connection.key())
            for sub_flow in serialized.sub_flows_commands:
                collect_edge_keys(sub_flow, keys)

        distinct_edge_keys: set[object] = set()
        collect_edge_keys(commands, distinct_edge_keys)
        assert count_serialized_edges(commands) > len(distinct_edge_keys), (
            "expected the serializer to report at least one edge more than once, "
            "otherwise this test cannot observe the duplicate-application bug"
        )

        GriptapeNodes.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

        engine = current_engine()
        connection_requests: list[CreateConnectionRequest] = []
        original_handle_request = engine.handle_request

        def record_connection_requests(request: object, **kwargs: object) -> object:
            if isinstance(request, CreateConnectionRequest):
                connection_requests.append(request)
            return original_handle_request(request, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(engine, "handle_request", record_connection_requests)
        _deserialize_into_fresh_context(commands)
        monkeypatch.undo()

        issued_edges = [
            (
                request.source_node_name,
                request.source_parameter_name,
                request.target_node_name,
                request.target_parameter_name,
            )
            for request in connection_requests
        ]
        duplicated_edges = {edge for edge in issued_edges if issued_edges.count(edge) > 1}
        assert not duplicated_edges, f"these edges were created more than once on load: {sorted(duplicated_edges)}"
        # Every distinct edge still has to be created, so deduping cannot pass by wiring nothing.
        # Not compared to len(distinct_edge_keys): the load path may legitimately issue extra edges
        # of its own (a proxy remap, say), and only the duplicates above are the bug.
        assert len(issued_edges) >= len(distinct_edge_keys), (
            f"expected at least {len(distinct_edge_keys)} edges to be created, got {len(issued_edges)}"
        )

    def test_restores_an_edge_leaving_a_nested_group(self, library_name: str) -> None:
        """The other direction: a nested node is the source and the far end is outside both groups.

        Incoming and outgoing edges take separate branches when a group decides whether an edge
        crosses its boundary, and each hands off to a different side of the wall. Every other test
        here wires outside-in, so the outgoing branch was never taken.
        """
        flow = GriptapeNodes.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="OutgoingRoundTrip", set_as_new_context=False)
        )
        assert isinstance(flow, CreateFlowResultSuccess), flow

        with GriptapeNodes.ContextManager().flow(flow.flow_name):
            outer = _create_node("SubflowGroupNode", "OuterGroup", library_name)
            inner = _create_node("SubflowGroupNode", "InnerGroup", library_name)
            leaf = _create_node("EchoNode", "Leaf", library_name, parent_group_name=inner)
            sink = _create_node("EchoNode", "Sink", library_name)

            nest_result = GriptapeNodes.handle_request(
                AddNodesToNodeGroupRequest(node_names=[inner], node_group_name=outer)
            )
            assert isinstance(nest_result, AddNodesToNodeGroupResultSuccess), nest_result

            _connect(leaf, sink)

        commands = _serialize(flow.flow_name)

        GriptapeNodes.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        result = _deserialize_into_fresh_context(commands)

        restored_outer = _get_group(result.node_name_mappings["OuterGroup"])
        restored_leaf = GriptapeNodes.NodeManager().get_node_by_name(result.node_name_mappings["Leaf"])
        assert restored_outer.contains_node(restored_leaf)

        # Exactly one: the edge has to come back, and it must not be duplicated per boundary.
        assert len(_edges_from(restored_leaf.name)) == 1, (
            f"expected one outgoing edge from the nested leaf, got {_edges_from(restored_leaf.name)}"
        )

    def test_restores_saved_parameter_values_from_inside_a_subflow(self, library_name: str) -> None:
        """Values are stored per level too, so a value on a nested node must come back."""
        flow = GriptapeNodes.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="ValueRoundTrip", set_as_new_context=False)
        )
        assert isinstance(flow, CreateFlowResultSuccess), flow

        with GriptapeNodes.ContextManager().flow(flow.flow_name):
            group = _create_node("SubflowGroupNode", "Group", library_name)
            leaf = _create_node("EchoNode", "Leaf", library_name, parent_group_name=group)
            GriptapeNodes.handle_request(
                SetParameterValueRequest(parameter_name="text", node_name=leaf, value=_EXPECTED_TEXT)
            )

        commands = _serialize(flow.flow_name)

        GriptapeNodes.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        result = _deserialize_into_fresh_context(commands)

        restored_leaf = GriptapeNodes.NodeManager().get_node_by_name(result.node_name_mappings["Leaf"])
        assert restored_leaf.get_parameter_value("text") == _EXPECTED_TEXT
