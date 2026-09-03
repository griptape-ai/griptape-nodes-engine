"""Tests for FlowManager's serialize/deserialize-to-commands round trip.

Covers ``on_serialize_flow_to_commands`` / ``on_deserialize_flow_from_commands``, connection
aggregation and dedup across nested Flows (``_aggregate_connections`` /
``SerializedConnectionKey``), and the node/value/connection ordering the deserializer relies on
(``_deserialize_nodes_for_flow_and_subflows`` and friends).

The node types named below (e.g. ``"TestNodeA"``) are never resolvable through a real library in
this test environment, so ``CreateNodeRequest`` always falls back to an ``ErrorProxyNode``
placeholder. That placeholder round-trips through the exact same serialize/deserialize commands a
real node would (it records its original type/library and dynamically grows whatever parameters a
connection or an ``initial_setup`` value touches), which is what the rest of the unit suite already
relies on for the same reason (see ``TestSerializeFlowSkipsTransientChildFlows`` in
``test_flow_manager.py``).
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.node_types import NodeDependencies
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    DeserializeFlowFromCommandsRequest,
    DeserializeFlowFromCommandsResultFailure,
    DeserializeFlowFromCommandsResultSuccess,
    SerializedConnectionKey,
    SerializedFlowCommands,
    SerializeFlowToCommandsRequest,
    SerializeFlowToCommandsResultFailure,
    SerializeFlowToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    CreateNodeResultSuccess,
    SerializedNodeCommands,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.parameter_events import (
    SetParameterValueRequest,
    SetParameterValueResultSuccess,
)

if TYPE_CHECKING:
    from collections.abc import Generator

    from griptape_nodes.retained_mode.engine import Engine


@pytest.fixture
def clean_object_state(engine: Engine) -> Generator[None, None, None]:
    """Clear all object state around a test so leftover flows never bleed across tests."""
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    try:
        yield
    finally:
        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))


def _create_flow(
    engine: Engine, flow_name: str, *, parent_flow_name: str | None = None, set_as_new_context: bool = True
) -> str:
    result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=parent_flow_name, flow_name=flow_name, set_as_new_context=set_as_new_context)
    )
    assert isinstance(result, CreateFlowResultSuccess), result
    return result.flow_name


def _create_node(engine: Engine, node_type: str, node_name: str, *, library: str | None = None) -> str:
    result = engine.handle_request(
        CreateNodeRequest(node_type=node_type, node_name=node_name, specific_library_name=library)
    )
    assert isinstance(result, CreateNodeResultSuccess), result
    return result.node_name


def _connect(engine: Engine, source_node: str, source_parameter: str, target_node: str, target_parameter: str) -> None:
    result = engine.handle_request(
        CreateConnectionRequest(
            source_node_name=source_node,
            source_parameter_name=source_parameter,
            target_node_name=target_node,
            target_parameter_name=target_parameter,
        )
    )
    assert isinstance(result, CreateConnectionResultSuccess), result


def _set_value(
    engine: Engine, node_name: str, parameter_name: str, *, value: str | float | bool | dict | list | None
) -> None:
    """Set a value on a placeholder node's parameter, growing it the way workflow load does."""
    result = engine.handle_request(
        SetParameterValueRequest(node_name=node_name, parameter_name=parameter_name, value=value, initial_setup=True)
    )
    assert isinstance(result, SetParameterValueResultSuccess), result


def _serialize(engine: Engine, flow_name: str, *, include_create_flow_command: bool = True) -> SerializedFlowCommands:
    result = engine.handle_request(
        SerializeFlowToCommandsRequest(flow_name=flow_name, include_create_flow_command=include_create_flow_command)
    )
    assert isinstance(result, SerializeFlowToCommandsResultSuccess), result
    return result.serialized_flow_commands


def _deserialize_into_fresh_context(
    engine: Engine,
    serialized_flow_commands: SerializedFlowCommands,
    workflow_name: str,
    *,
    pop_flow_context_after: bool = True,
) -> DeserializeFlowFromCommandsResultSuccess:
    """Replay commands the way an image-metadata restore does, and require success.

    Clears all object state first: a Flow's serialization can carry a ``CreateFlowRequest`` with
    ``parent_flow_name=None`` (the Canvas), and the engine allows only one Canvas at a time, so the
    original graph has to be gone before the replay can create its own. A fresh workflow context
    keeps the restored graph from colliding on names with whatever the test built to serialize.
    """
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    engine.context_manager.push_workflow(workflow_name=workflow_name)
    result = engine.handle_request(
        DeserializeFlowFromCommandsRequest(
            serialized_flow_commands=serialized_flow_commands, pop_flow_context_after=pop_flow_context_after
        )
    )
    assert isinstance(result, DeserializeFlowFromCommandsResultSuccess), result
    return result


class TestIncludeCreateFlowCommand:
    """``include_create_flow_command`` toggles whether the payload can create its own Flow."""

    @pytest.mark.usefixtures("clean_object_state")
    def test_true_includes_create_flow_request(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="wf_include_true")
        flow_name = _create_flow(engine, "parent")

        serialized = _serialize(engine, flow_name, include_create_flow_command=True)

        assert isinstance(serialized.flow_initialization_command, CreateFlowRequest)
        assert serialized.flow_initialization_command.flow_name == flow_name

    @pytest.mark.usefixtures("clean_object_state")
    def test_false_omits_initialization_command(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="wf_include_false")
        flow_name = _create_flow(engine, "parent")

        serialized = _serialize(engine, flow_name, include_create_flow_command=False)

        assert serialized.flow_initialization_command is None


class TestSerializeUnknownFlow:
    """Serializing a Flow name that does not exist fails cleanly instead of raising."""

    @pytest.mark.usefixtures("clean_object_state")
    def test_unknown_flow_name_returns_failure(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="wf_unknown")

        result = engine.handle_request(SerializeFlowToCommandsRequest(flow_name="does_not_exist"))

        assert isinstance(result, SerializeFlowToCommandsResultFailure)


class TestSubflowAggregation:
    """A parent Flow's serialization reports its whole subtree, not just its direct nodes."""

    @pytest.mark.usefixtures("clean_object_state")
    def test_subflow_appears_in_sub_flows_commands_with_its_nodes(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="wf_subflow_shape")
        parent_name = _create_flow(engine, "parent")
        child_name = _create_flow(engine, "child", parent_flow_name=parent_name, set_as_new_context=True)
        _create_node(engine, "ChildNode", "child_node")

        serialized = _serialize(engine, parent_name)

        assert len(serialized.sub_flows_commands) == 1
        child_commands = serialized.sub_flows_commands[0]
        assert child_commands.flow_name == child_name
        child_node_names = {cmd.create_node_command.node_name for cmd in child_commands.serialized_node_commands}
        assert child_node_names == {"child_node"}

    @pytest.mark.usefixtures("clean_object_state")
    def test_node_types_used_aggregates_and_dedupes_across_subflows(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="wf_node_types_used")
        parent_name = _create_flow(engine, "parent")
        _create_node(engine, "SharedTypeNode", "parent_node_1", library="lib_shared")
        _create_flow(engine, "child", parent_flow_name=parent_name, set_as_new_context=True)
        _create_node(engine, "SharedTypeNode", "child_node_1", library="lib_shared")

        serialized = _serialize(engine, parent_name)

        # Two nodes of the same (library, type) pair across a Flow boundary collapse to one entry.
        matching = [entry for entry in serialized.node_types_used if entry.node_type == "SharedTypeNode"]
        assert len(matching) == 1
        assert matching[0].library_name == "lib_shared"

    @pytest.mark.usefixtures("clean_object_state")
    def test_node_dependencies_aggregates_from_nodes_and_subflows(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="wf_node_dependencies")
        parent_name = _create_flow(engine, "parent")
        _create_node(engine, "ParentDepNode", "parent_node", library="lib_parent_only")
        _create_flow(engine, "child", parent_flow_name=parent_name, set_as_new_context=True)
        _create_node(engine, "ChildDepNode", "child_node", library="lib_child_only")

        serialized = _serialize(engine, parent_name)

        aggregated_library_names = {entry.library_name for entry in serialized.node_dependencies.libraries}
        assert {"lib_parent_only", "lib_child_only"} <= aggregated_library_names


class TestConnectionAggregationAndDedup:
    """Connections crossing a Flow boundary are reported by every ancestor but wired exactly once."""

    @pytest.mark.usefixtures("clean_object_state")
    def test_cross_boundary_edge_is_wired_exactly_once_on_deserialize(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="wf_cross_boundary")
        parent_name = _create_flow(engine, "parent")
        source_name = _create_node(engine, "SourceNode", "source_node")
        _create_flow(engine, "child", parent_flow_name=parent_name, set_as_new_context=True)
        target_name = _create_node(engine, "TargetNode", "target_node")
        engine.context_manager.pop_flow()
        _connect(engine, source_name, "exec_out", target_name, "exec_in")

        serialized = _serialize(engine, parent_name)

        # The edge crosses the parent/child boundary, so both this Flow's own connection list AND
        # the child's aggregated list would name it; the parent-level aggregate must not duplicate it.
        assert len(serialized.serialized_connections) == 1

        deserialize_result = _deserialize_into_fresh_context(engine, serialized, "wf_cross_boundary_restored")
        rebuilt_source = deserialize_result.node_name_mappings[source_name]
        rebuilt_target = deserialize_result.node_name_mappings[target_name]

        connections = engine.flow_manager.get_connections()
        outgoing = connections.outgoing_index.get(rebuilt_source, {}).get("exec_out", [])
        matching_targets = [
            connections.connections[connection_id].target_node.name
            for connection_id in outgoing
            if connections.connections[connection_id].target_node.name == rebuilt_target
        ]
        assert matching_targets == [rebuilt_target]

    def test_serialized_connection_key_identifies_same_edge_regardless_of_reporting_flow(self) -> None:
        """Two IndirectConnectionSerialization instances with equal endpoints share one key.

        This is the mechanism ``_deserialize_connections_for_flow_and_subflows`` relies on to skip
        an edge the second (and third, ...) time an ancestor Flow reports it.
        """
        node_uuid_a = SerializedNodeCommands.NodeUUID("node-a")
        node_uuid_b = SerializedNodeCommands.NodeUUID("node-b")
        connection_from_parent = SerializedFlowCommands.IndirectConnectionSerialization(
            source_node_uuid=node_uuid_a,
            source_parameter_name="exec_out",
            target_node_uuid=node_uuid_b,
            target_parameter_name="exec_in",
        )
        connection_from_child = SerializedFlowCommands.IndirectConnectionSerialization(
            source_node_uuid=node_uuid_a,
            source_parameter_name="exec_out",
            target_node_uuid=node_uuid_b,
            target_parameter_name="exec_in",
        )

        assert connection_from_parent.key() == connection_from_child.key()
        assert isinstance(connection_from_parent.key(), SerializedConnectionKey)


class TestNodeCreationBeforeConnectionWiring:
    """Every node in the subtree exists before any connection is wired.

    An aggregated edge can name a node one or more levels down; wiring it before that node's
    subflow has been created and populated fails with "node did not exist within the flow".
    """

    @pytest.mark.usefixtures("clean_object_state")
    def test_deserializing_a_deeply_nested_cross_boundary_edge_succeeds(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="wf_deep_nesting")
        parent_name = _create_flow(engine, "parent")
        source_name = _create_node(engine, "SourceNode", "source_node")
        _create_flow(engine, "child", parent_flow_name=parent_name, set_as_new_context=True)
        _create_flow(engine, "grandchild", parent_flow_name="child", set_as_new_context=True)
        target_name = _create_node(engine, "TargetNode", "target_node")
        engine.context_manager.pop_flow()
        engine.context_manager.pop_flow()
        _connect(engine, source_name, "exec_out", target_name, "exec_in")

        serialized = _serialize(engine, parent_name)

        # Success alone is the assertion: an ordering regression raises FlowDeserializationError
        # (surfaced here as a failure result) instead of building the flow.
        deserialize_result = _deserialize_into_fresh_context(engine, serialized, "wf_deep_nesting_restored")
        assert isinstance(deserialize_result, DeserializeFlowFromCommandsResultSuccess)


class TestSetParameterValueCommandsKeyedByNodeUuid:
    """``set_parameter_value_commands`` is keyed by node UUID and resolves on the way back in."""

    @pytest.mark.usefixtures("clean_object_state")
    def test_value_set_on_a_node_is_keyed_by_that_nodes_uuid_and_restored(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="wf_set_value_keying")
        parent_name = _create_flow(engine, "parent")
        node_name = _create_node(engine, "ValueNode", "value_node")
        _set_value(engine, node_name, "payload", value="distinctive-value-123")

        serialized = _serialize(engine, parent_name)

        node_uuid = next(
            cmd.node_uuid
            for cmd in serialized.serialized_node_commands
            if cmd.create_node_command.node_name == node_name
        )
        assert node_uuid in serialized.set_parameter_value_commands
        assert len(serialized.set_parameter_value_commands[node_uuid]) >= 1

        deserialize_result = _deserialize_into_fresh_context(engine, serialized, "wf_set_value_keying_restored")
        rebuilt_name = deserialize_result.node_name_mappings[node_name]
        rebuilt_node = engine.node_manager.get_node_by_name(rebuilt_name)
        assert rebuilt_node.get_parameter_value("payload") == "distinctive-value-123"


class TestNodeNameMappings:
    """``node_name_mappings`` names every rebuilt node, including ones inside sub-flows."""

    @pytest.mark.usefixtures("clean_object_state")
    def test_mappings_cover_parent_and_subflow_nodes(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="wf_name_mappings")
        parent_name = _create_flow(engine, "parent")
        parent_node_name = _create_node(engine, "ParentNode", "parent_node")
        _create_flow(engine, "child", parent_flow_name=parent_name, set_as_new_context=True)
        child_node_name = _create_node(engine, "ChildNode", "child_node")
        engine.context_manager.pop_flow()

        serialized = _serialize(engine, parent_name)

        deserialize_result = _deserialize_into_fresh_context(engine, serialized, "wf_name_mappings_restored")

        assert parent_node_name in deserialize_result.node_name_mappings
        assert child_node_name in deserialize_result.node_name_mappings


class TestControlAndDataConnectionsRoundTrip:
    """A graph with both a control edge and a data edge preserves both, with direction intact."""

    @pytest.mark.usefixtures("clean_object_state")
    def test_control_and_data_edges_both_survive_with_direction(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="wf_control_and_data")
        parent_name = _create_flow(engine, "parent")
        upstream_name = _create_node(engine, "UpstreamNode", "upstream_node")
        downstream_name = _create_node(engine, "DownstreamNode", "downstream_node")
        _connect(engine, upstream_name, "exec_out", downstream_name, "exec_in")
        _connect(engine, upstream_name, "payload", downstream_name, "payload")

        serialized = _serialize(engine, parent_name)
        deserialize_result = _deserialize_into_fresh_context(engine, serialized, "wf_control_and_data_restored")

        rebuilt_upstream = deserialize_result.node_name_mappings[upstream_name]
        rebuilt_downstream = deserialize_result.node_name_mappings[downstream_name]
        connections = engine.flow_manager.get_connections()

        control_targets = {
            connections.connections[connection_id].target_node.name
            for connection_id in connections.outgoing_index.get(rebuilt_upstream, {}).get("exec_out", [])
        }
        data_targets = {
            connections.connections[connection_id].target_node.name
            for connection_id in connections.outgoing_index.get(rebuilt_upstream, {}).get("payload", [])
        }
        assert control_targets == {rebuilt_downstream}
        assert data_targets == {rebuilt_downstream}


class TestDeserializeFailureCleanup:
    """A failed deserialization does not leave a half-built Flow behind."""

    @pytest.mark.usefixtures("clean_object_state")
    def test_unknown_node_type_leaves_no_flow_when_error_proxy_disabled(self, engine: Engine) -> None:
        """A node that cannot even become a placeholder must roll back the whole Flow it was in.

        ``create_error_proxy_on_failure=False`` is the one path that actually fails node creation
        (a real missing node type still succeeds as an ErrorProxyNode), so it is the way to force
        ``_deserialize_one_node`` into its failure branch deterministically.
        """
        engine.context_manager.push_workflow(workflow_name="wf_deserialize_cleanup")
        parent_name = _create_flow(engine, "parent")
        serialized = _serialize(engine, parent_name)
        assert serialized.serialized_node_commands == []

        failing_create_command = CreateNodeRequest(
            node_type="DoesNotExist",
            node_name="doomed_node",
            create_error_proxy_on_failure=False,
        )
        failing_node_command = SerializedNodeCommands(
            create_node_command=failing_create_command,
            element_modification_commands=[],
            node_dependencies=NodeDependencies(),
        )
        broken_serialized = replace(serialized, serialized_node_commands=[failing_node_command])

        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        engine.context_manager.push_workflow(workflow_name="wf_deserialize_cleanup_restored")
        result = engine.handle_request(DeserializeFlowFromCommandsRequest(serialized_flow_commands=broken_serialized))

        assert isinstance(result, DeserializeFlowFromCommandsResultFailure)
        assert not engine.object_manager.has_object_with_name(parent_name)


class TestPopFlowContextAfter:
    """``pop_flow_context_after`` leaves the context stack the way the caller found it."""

    @pytest.mark.usefixtures("clean_object_state")
    def test_true_pops_the_pushed_flow_context(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="wf_pop_true")
        parent_name = _create_flow(engine, "parent")
        serialized = _serialize(engine, parent_name)

        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        engine.context_manager.push_workflow(workflow_name="wf_pop_true_restored")
        assert not engine.context_manager.has_current_flow()

        result = engine.handle_request(
            DeserializeFlowFromCommandsRequest(serialized_flow_commands=serialized, pop_flow_context_after=True)
        )
        assert isinstance(result, DeserializeFlowFromCommandsResultSuccess), result

        assert not engine.context_manager.has_current_flow()

    @pytest.mark.usefixtures("clean_object_state")
    def test_false_leaves_the_new_flow_on_the_context_stack(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="wf_pop_false")
        parent_name = _create_flow(engine, "parent")
        serialized = _serialize(engine, parent_name)

        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        engine.context_manager.push_workflow(workflow_name="wf_pop_false_restored")
        assert not engine.context_manager.has_current_flow()

        result = engine.handle_request(
            DeserializeFlowFromCommandsRequest(serialized_flow_commands=serialized, pop_flow_context_after=False)
        )
        assert isinstance(result, DeserializeFlowFromCommandsResultSuccess), result

        assert engine.context_manager.has_current_flow()
        assert engine.context_manager.get_current_flow().name == result.flow_name
        engine.context_manager.pop_flow()


class TestUniqueValuePoolSharedAcrossSubtree:
    """A value shared by nodes in different Flows of the same subtree should pool to one entry."""

    @pytest.mark.usefixtures("clean_object_state")
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "EFFICIENCY: on_serialize_flow_to_commands creates a fresh unique_parameter_uuid_to_values "
            "dict and a fresh SerializedParameterValueTracker for every recursive "
            "SerializeFlowToCommandsRequest() call it issues for a child Flow (SerializeFlowToCommandsRequest "
            "carries no tracker fields to pass one through), so a value shared by a node in the "
            "parent Flow and a node in a child Flow is pickled/stored twice under two different "
            "UUIDs instead of being pooled once. Intended contract: the unique-value pool is shared "
            "across a Flow's whole subtree, matching the dedup a value gets when both nodes are in "
            "the same Flow. - see #5436"
        ),
    )
    def test_shared_value_across_flow_boundary_appears_once_in_pool(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="wf_shared_value_pool")
        parent_name = _create_flow(engine, "parent")
        parent_node_name = _create_node(engine, "ParentValueNode", "parent_value_node")
        _set_value(engine, parent_node_name, "payload", value="shared-across-boundary")
        _create_flow(engine, "child", parent_flow_name=parent_name, set_as_new_context=True)
        child_node_name = _create_node(engine, "ChildValueNode", "child_value_node")
        _set_value(engine, child_node_name, "payload", value="shared-across-boundary")
        engine.context_manager.pop_flow()

        serialized = _serialize(engine, parent_name)

        distinct_values = list(serialized.unique_parameter_uuid_to_values.values())
        matching_entries = [value for value in distinct_values if value == "shared-across-boundary"]
        assert len(matching_entries) == 1
