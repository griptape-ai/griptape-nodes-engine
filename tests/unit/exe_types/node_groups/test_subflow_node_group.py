"""Unit tests for SubflowNodeGroup."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, create_autospec

import pytest

from griptape_nodes.exe_types.core_types import ControlParameterInput, ControlParameterOutput, Parameter, ParameterMode
from griptape_nodes.exe_types.node_groups.subflow_node_group import (
    LEFT_PARAMETERS_KEY,
    RIGHT_PARAMETERS_KEY,
    SubflowNodeGroup,
)
from griptape_nodes.exe_types.node_types import BaseNode, Connection
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    DeleteConnectionRequest,
    DeleteConnectionResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.parameter_events import (
    AddParameterToNodeResultFailure,
    AddParameterToNodeResultSuccess,
    RemoveParameterFromNodeRequest,
    RemoveParameterFromNodeResultFailure,
    RemoveParameterFromNodeResultSuccess,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine


_UNMAP_RECREATED_EDGE_COUNT = 2
_UNMAP_DELETED_EDGE_COUNT = 4
_PROXY_CONNECTION_COUNT_AFTER_TWO_REMAPPED_EDGES = 4


class TestSubflowNodeGroupCreateSubflow:
    """_create_subflow must persist the deduplicated flow name it actually created."""

    def test_records_deduplicated_flow_name_on_collision(
        self,
        engine: Engine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The group records the flow name it got back, not the (colliding) name it requested."""
        group = _MiniSubflowGroup(name="G")

        # Simulate the engine deduplicating the requested "G_subflow" (already taken) to "G_subflow_1".
        deduped_result = CreateFlowResultSuccess(flow_name="G_subflow_1", result_details="created")
        mock_handle = create_autospec(engine.handle_request, return_value=deduped_result)
        monkeypatch.setattr(engine, "handle_request", mock_handle)

        # _create_subflow reads the current flow only to parent the request; keep it off engine state.
        context_manager = engine.context_manager
        monkeypatch.setattr(
            context_manager,
            "get_current_flow",
            create_autospec(context_manager.get_current_flow, return_value=None),
        )

        group._create_subflow()

        # The request is derived from the group's own name...
        mock_handle.assert_called_once_with(
            CreateFlowRequest(
                flow_name="G_subflow",
                parent_flow_name=None,
                set_as_new_context=False,
                metadata={"flow_type": "NodeGroupFlow"},
            )
        )
        # ...but the group must record the flow it ACTUALLY got back, not the requested name.
        assert group.metadata["subflow_name"] == "G_subflow_1"

    def test_preserves_saved_proxy_side_metadata(self, engine: Engine) -> None:  # noqa: ARG002
        group = _MiniSubflowGroup(
            name="G",
            metadata={"left_parameters": ["exec_in"], "right_parameters": ["exec_out"]},
        )

        assert group.metadata["left_parameters"] == ["group_exec_in", "exec_in"]
        assert group.metadata["right_parameters"] == ["group_exec_out", "exec_out"]

    def test_deduplicates_saved_proxy_side_metadata(self, engine: Engine) -> None:
        group = _MiniSubflowGroup(
            name="G",
            metadata={
                "left_parameters": ["exec_in", "exec_in", "other"],
                "right_parameters": ["exec_out", "other", "other"],
            },
        )
        engine.object_manager.add_object_by_name(group.name, group)

        assert group.metadata["left_parameters"] == ["group_exec_in", "exec_in", "other"]
        assert group.metadata["right_parameters"] == ["group_exec_out", "exec_out", "other"]


class TestGetAllNodes:
    """get_all_nodes has to reach the whole body, not just the first level down.

    Callers use it to package a group for execution (remote, private, iterative), so a node it
    misses is a node that silently does not run.
    """

    def test_collects_members_nested_more_than_one_level_deep(
        self,
        engine: Engine,  # noqa: ARG002 - initialises the engine singleton for construction
    ) -> None:
        outer = _MiniSubflowGroup(name="outer")
        middle = _MiniSubflowGroup(name="middle")
        inner = _MiniSubflowGroup(name="inner")
        leaf = _MiniSubflowGroup(name="leaf")

        # Wire membership directly: this covers the traversal, not the add-to-group machinery.
        outer.nodes = {"middle": middle}
        middle.nodes = {"inner": inner}
        inner.nodes = {"leaf": leaf}

        # "leaf" is three levels down; walking a single level would stop at "middle".
        assert set(outer.get_all_nodes()) == {"middle", "inner", "leaf"}

    def test_returns_direct_members_when_nothing_is_nested(
        self,
        engine: Engine,  # noqa: ARG002 - initialises the engine singleton for construction
    ) -> None:
        group = _MiniSubflowGroup(name="group")
        group.nodes = {"only": _MiniSubflowGroup(name="only")}

        assert set(group.get_all_nodes()) == {"only"}


class TestSubflowNodeGroupProxyParameters:
    """Boundary proxies must remain control ports after request-handler reconstruction."""

    def test_control_proxy_preserves_port_shape_and_bridge_modes(self, engine: Engine) -> None:
        group = _MiniSubflowGroup(name="group")
        engine.object_manager.add_object_by_name(group.name, group)

        incoming_proxy = group._create_proxy_parameter_for_connection(
            ControlParameterInput(name="upstream_exec"), is_incoming=True
        )
        outgoing_proxy = group._create_proxy_parameter_for_connection(
            ControlParameterOutput(name="downstream_exec"), is_incoming=False
        )

        assert isinstance(incoming_proxy, ControlParameterInput)
        assert isinstance(outgoing_proxy, ControlParameterOutput)
        assert incoming_proxy.name == "upstream_exec"
        assert outgoing_proxy.name == "downstream_exec"
        assert incoming_proxy.display_name == "Flow In"
        assert outgoing_proxy.display_name == "Flow Out"
        assert incoming_proxy.allowed_modes == {ParameterMode.INPUT, ParameterMode.OUTPUT}
        assert outgoing_proxy.allowed_modes == {ParameterMode.INPUT, ParameterMode.OUTPUT}
        assert ParameterMode.PROPERTY not in incoming_proxy.allowed_modes
        assert ParameterMode.PROPERTY not in outgoing_proxy.allowed_modes

    def test_control_port_serialization_preserves_directional_shape(self) -> None:
        incoming = ControlParameterInput(name="exec_in")
        outgoing = ControlParameterOutput(name="exec_out")

        incoming_dict = incoming.to_dict()
        outgoing_dict = outgoing.to_dict()

        assert incoming_dict["input_types"] == ["parametercontroltype"]
        assert incoming_dict["output_type"] is None
        assert outgoing_dict["input_types"] is None
        assert outgoing_dict["output_type"] == "parametercontroltype"

    def test_proxy_accepts_control_parameters_without_a_tooltip(self, engine: Engine) -> None:
        group = _MiniSubflowGroup(name="group")
        engine.object_manager.add_object_by_name(group.name, group)

        incoming = ControlParameterInput(name="exec_in", tooltip="")
        proxy = group._create_proxy_parameter_for_connection(incoming, is_incoming=True)

        assert isinstance(proxy, ControlParameterInput)
        assert proxy.tooltip


class TestSubflowNodeGroupProxyLifecycle:
    """Proxy cleanup and remapping keep the graph and rail metadata in sync."""

    def test_registers_execution_rail_before_existing_members_and_ignores_duplicates(
        self,
        engine: Engine,  # noqa: ARG002
    ) -> None:
        group = _MiniSubflowGroup(name="rails")
        group.metadata[LEFT_PARAMETERS_KEY] = ["saved_proxy"]

        group._register_side_parameter(LEFT_PARAMETERS_KEY, "group_exec_in")
        group._register_side_parameter(LEFT_PARAMETERS_KEY, "group_exec_in")
        group._register_side_parameter(LEFT_PARAMETERS_KEY, "new_proxy")

        assert group.metadata[LEFT_PARAMETERS_KEY] == ["group_exec_in", "saved_proxy", "new_proxy"]

    def test_proxy_creation_reports_an_unsuccessful_add(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        group = _MiniSubflowGroup(name="failed_proxy")
        result = AddParameterToNodeResultFailure(result_details="parameter rejected")
        monkeypatch.setattr(engine, "handle_request", create_autospec(engine.handle_request, return_value=result))

        with pytest.raises(TypeError, match="Failed to add parameter"):
            group._create_proxy_parameter_for_connection(Parameter(name="value", tooltip=""), is_incoming=True)

    def test_proxy_creation_reports_a_missing_created_parameter(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        group = _MiniSubflowGroup(name="missing_proxy")
        result = AddParameterToNodeResultSuccess(
            parameter_name="missing",
            type="any",
            node_name=group.name,
            result_details="added",
        )
        monkeypatch.setattr(engine, "handle_request", create_autospec(engine.handle_request, return_value=result))

        with pytest.raises(RuntimeError, match="failed to create proxy"):
            group._create_proxy_parameter_for_connection(Parameter(name="value", tooltip=""), is_incoming=True)

    def test_cleanup_waits_for_the_second_bridge_edge(self, engine: Engine) -> None:
        group = _group_with_proxy(engine, "counted_proxy")
        proxy = group.get_parameter_by_name("proxy")
        assert proxy is not None
        group._proxy_param_to_connections[proxy.name] = 2

        group._cleanup_proxy_parameter(proxy, RIGHT_PARAMETERS_KEY)

        assert group._proxy_param_to_connections[proxy.name] == 1
        assert group.get_parameter_by_name(proxy.name) is proxy

    def test_cleanup_uses_graph_state_after_deserialization(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        group = _group_with_proxy(engine, "graph_proxy")
        proxy = group.get_parameter_by_name("proxy")
        assert proxy is not None
        graph = MagicMock()
        graph.get_incoming_connections_to_parameter.return_value = [object()]
        graph.get_outgoing_connections_from_parameter.return_value = []
        monkeypatch.setattr(engine.flow_manager, "get_connections", MagicMock(return_value=graph))

        group._cleanup_proxy_parameter(proxy, LEFT_PARAMETERS_KEY)

        assert group.get_parameter_by_name(proxy.name) is proxy

    def test_cleanup_keeps_proxy_with_an_outgoing_bridge_after_deserialization(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        group = _group_with_proxy(engine, "outgoing_graph_proxy")
        proxy = group.get_parameter_by_name("proxy")
        assert proxy is not None
        graph = MagicMock()
        graph.get_incoming_connections_to_parameter.return_value = []
        graph.get_outgoing_connections_from_parameter.return_value = [object()]
        monkeypatch.setattr(engine.flow_manager, "get_connections", MagicMock(return_value=graph))

        group._cleanup_proxy_parameter(proxy, RIGHT_PARAMETERS_KEY)

        assert group.get_parameter_by_name(proxy.name) is proxy
        assert proxy.name in group.metadata[RIGHT_PARAMETERS_KEY]

    def test_cleanup_keeps_metadata_when_parameter_removal_fails(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        group = _group_with_proxy(engine, "failed_cleanup")
        proxy = group.get_parameter_by_name("proxy")
        assert proxy is not None
        group._proxy_param_to_connections[proxy.name] = 1
        failure = RemoveParameterFromNodeResultFailure(result_details="parameter is locked")
        monkeypatch.setattr(
            engine.node_manager,
            "on_remove_parameter_from_node_request",
            MagicMock(return_value=failure),
        )

        group._cleanup_proxy_parameter(proxy, RIGHT_PARAMETERS_KEY)

        assert proxy.name not in group._proxy_param_to_connections
        assert proxy.name in group.metadata[RIGHT_PARAMETERS_KEY]

    def test_cleanup_removes_proxy_and_rail_metadata_after_final_edge(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        group = _group_with_proxy(engine, "successful_cleanup")
        proxy = group.get_parameter_by_name("proxy")
        assert proxy is not None
        group._proxy_param_to_connections[proxy.name] = 1
        graph = MagicMock()
        graph.get_incoming_connections_to_parameter.return_value = []
        graph.get_outgoing_connections_from_parameter.return_value = []
        monkeypatch.setattr(engine.flow_manager, "get_connections", MagicMock(return_value=graph))

        def remove_parameter(*, request: RemoveParameterFromNodeRequest) -> RemoveParameterFromNodeResultSuccess:
            assert request.parameter_name == proxy.name
            group.remove_node_element(proxy)
            return RemoveParameterFromNodeResultSuccess(result_details="removed")

        remove = MagicMock(side_effect=remove_parameter)
        monkeypatch.setattr(engine.node_manager, "on_remove_parameter_from_node_request", remove)

        group._cleanup_proxy_parameter(proxy, RIGHT_PARAMETERS_KEY)

        assert group.get_parameter_by_name(proxy.name) is None
        assert proxy.name not in group.metadata[RIGHT_PARAMETERS_KEY]
        assert proxy.name not in group._proxy_param_to_connections

    def test_unmap_restores_both_directions_and_restores_parent_group(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        group = _MiniSubflowGroup(name="unmap_group")
        node = BaseNode(name="inside")
        node.parent_group = group
        downstream = BaseNode(name="downstream")
        upstream = BaseNode(name="upstream")

        node_output = Parameter(name="out", tooltip="")
        node_input = Parameter(name="in", tooltip="")
        proxy = Parameter(name="proxy", tooltip="")
        downstream_input = Parameter(name="downstream_in", tooltip="")
        upstream_output = Parameter(name="upstream_out", tooltip="")

        outgoing_internal = Connection(node, node_output, group, proxy)
        outgoing_wall = Connection(group, proxy, downstream, downstream_input)
        incoming_internal = Connection(group, proxy, node, node_input)
        incoming_wall = Connection(upstream, upstream_output, group, proxy)
        connections = MagicMock()
        connections.get_outgoing_connections_to_node.return_value = {"out": [outgoing_internal]}
        connections.get_outgoing_connections_from_parameter.return_value = [outgoing_wall]
        connections.get_incoming_connections_from_node.return_value = {"in": [incoming_internal]}
        connections.get_incoming_connections_to_parameter.return_value = [incoming_wall]
        connections.connections = {1: outgoing_wall, 2: incoming_wall}

        success = MagicMock()
        success.failed.return_value = False
        delete_connection = MagicMock(return_value=success)
        create_connection = MagicMock(return_value=success)
        monkeypatch.setattr(engine.flow_manager, "on_delete_connection_request", delete_connection)
        monkeypatch.setattr(engine.flow_manager, "on_create_connection_request", create_connection)

        group.unmap_node_connections(node, connections)

        assert node.parent_group is group
        assert create_connection.call_count == _UNMAP_RECREATED_EDGE_COUNT
        assert delete_connection.call_count == _UNMAP_DELETED_EDGE_COUNT

    @pytest.mark.parametrize("is_incoming", [True, False])
    def test_unmap_does_not_delete_a_wall_edge_replaced_by_direct_connection(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, *, is_incoming: bool
    ) -> None:
        group = _MiniSubflowGroup(name="replaced_wall_group")
        node = BaseNode(name="inside")
        node.parent_group = group
        endpoint = BaseNode(name="endpoint")
        proxy = Parameter(name="proxy", tooltip="")
        endpoint_parameter = Parameter(name="endpoint_param", tooltip="")
        internal_parameter = Parameter(name="internal_in" if is_incoming else "internal_out", tooltip="")
        internal_connection = (
            Connection(group, proxy, node, internal_parameter)
            if is_incoming
            else Connection(node, internal_parameter, group, proxy)
        )
        wall_connection = (
            Connection(endpoint, endpoint_parameter, group, proxy)
            if is_incoming
            else Connection(group, proxy, endpoint, endpoint_parameter)
        )
        direct_connection = (
            Connection(endpoint, endpoint_parameter, node, internal_parameter)
            if is_incoming
            else Connection(node, internal_parameter, endpoint, endpoint_parameter)
        )
        connections = MagicMock()
        connections.get_outgoing_connections_to_node.return_value = (
            {} if is_incoming else {"internal_out": [internal_connection]}
        )
        connections.get_outgoing_connections_from_parameter.return_value = [wall_connection]
        connections.get_incoming_connections_from_node.return_value = (
            {"internal_in": [internal_connection]} if is_incoming else {}
        )
        connections.get_incoming_connections_to_parameter.return_value = [wall_connection]
        connections.connections = {1: wall_connection}

        success = MagicMock()
        success.failed.return_value = False
        delete_connection = MagicMock(return_value=success)

        def create_direct_connection(*_args: Any, **_kwargs: Any) -> MagicMock:
            # The flow manager replaces the wall edge when the destination accepts one input.
            assert node.parent_group is None
            connections.connections.clear()
            connections.connections[2] = direct_connection
            return success

        create_connection = MagicMock(side_effect=create_direct_connection)
        monkeypatch.setattr(engine.flow_manager, "on_delete_connection_request", delete_connection)
        monkeypatch.setattr(engine.flow_manager, "on_create_connection_request", create_connection)

        group.unmap_node_connections(node, connections)

        assert create_connection.call_count == 1
        delete_connection.assert_called_once()
        assert connections.connections == {2: direct_connection}
        assert node.parent_group is group

    @pytest.mark.parametrize("is_incoming", [True, False])
    def test_unmap_reports_a_failed_direct_connection_and_restores_parent_group(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, *, is_incoming: bool
    ) -> None:
        group = _MiniSubflowGroup(name="failed_unmap_group")
        node = BaseNode(name="inside")
        node.parent_group = group
        proxy = Parameter(name="proxy", tooltip="")
        endpoint = BaseNode(name="endpoint")
        endpoint_parameter = Parameter(name="endpoint_param", tooltip="")
        connection = (
            Connection(group, proxy, node, Parameter(name="internal_in", tooltip=""))
            if is_incoming
            else Connection(node, Parameter(name="internal_out", tooltip=""), group, proxy)
        )
        wall_connection = (
            Connection(endpoint, endpoint_parameter, group, proxy)
            if is_incoming
            else Connection(group, proxy, endpoint, endpoint_parameter)
        )
        connections = MagicMock()
        connections.get_outgoing_connections_to_node.return_value = (
            {} if is_incoming else {"internal_out": [connection]}
        )
        connections.get_outgoing_connections_from_parameter.return_value = [wall_connection]
        connections.get_incoming_connections_from_node.return_value = (
            {"internal_in": [connection]} if is_incoming else {}
        )
        connections.get_incoming_connections_to_parameter.return_value = [wall_connection]
        success = MagicMock()
        success.failed.return_value = False
        failure = MagicMock()
        failure.failed.return_value = True
        failure.result_details = "edge rejected"
        monkeypatch.setattr(engine.flow_manager, "on_delete_connection_request", MagicMock(return_value=success))
        monkeypatch.setattr(engine.flow_manager, "on_create_connection_request", MagicMock(return_value=failure))

        direction = "incoming" if is_incoming else "outgoing"
        with pytest.raises(RuntimeError, match=f"Failed to create direct {direction} connection"):
            group.unmap_node_connections(node, connections)

        assert node.parent_group is group

    @pytest.mark.parametrize("is_incoming", [True, False])
    def test_unmap_restores_parent_group_when_direct_creation_raises(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, *, is_incoming: bool
    ) -> None:
        group = _MiniSubflowGroup(name="raising_unmap_group")
        node = BaseNode(name="inside")
        node.parent_group = group
        endpoint = BaseNode(name="endpoint")
        proxy = Parameter(name="proxy", tooltip="")
        endpoint_parameter = Parameter(name="endpoint_param", tooltip="")
        internal_connection = (
            Connection(group, proxy, node, Parameter(name="internal_in", tooltip=""))
            if is_incoming
            else Connection(node, Parameter(name="internal_out", tooltip=""), group, proxy)
        )
        wall_connection = (
            Connection(endpoint, endpoint_parameter, group, proxy)
            if is_incoming
            else Connection(group, proxy, endpoint, endpoint_parameter)
        )
        connections = MagicMock()
        connections.get_outgoing_connections_to_node.return_value = (
            {} if is_incoming else {"internal_out": [internal_connection]}
        )
        connections.get_outgoing_connections_from_parameter.return_value = [wall_connection]
        connections.get_incoming_connections_from_node.return_value = (
            {"internal_in": [internal_connection]} if is_incoming else {}
        )
        connections.get_incoming_connections_to_parameter.return_value = [wall_connection]
        success = MagicMock()
        success.failed.return_value = False

        def raise_during_creation(_request: CreateConnectionRequest) -> None:
            assert node.parent_group is None
            msg = "creation raised"
            raise ValueError(msg)

        monkeypatch.setattr(engine.flow_manager, "on_delete_connection_request", MagicMock(return_value=success))
        monkeypatch.setattr(
            engine.flow_manager, "on_create_connection_request", MagicMock(side_effect=raise_during_creation)
        )

        with pytest.raises(ValueError, match="creation raised"):
            group.unmap_node_connections(node, connections)

        assert node.parent_group is group

    @pytest.mark.parametrize("is_incoming", [True, False])
    def test_unmap_reports_a_failed_wall_connection_removal(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, *, is_incoming: bool
    ) -> None:
        group = _MiniSubflowGroup(name="failed_delete_group")
        node = BaseNode(name="inside")
        node.parent_group = group
        proxy = Parameter(name="proxy", tooltip="")
        endpoint = BaseNode(name="endpoint")
        endpoint_parameter = Parameter(name="endpoint_param", tooltip="")
        internal_connection = (
            Connection(group, proxy, node, Parameter(name="internal_in", tooltip=""))
            if is_incoming
            else Connection(node, Parameter(name="internal_out", tooltip=""), group, proxy)
        )
        wall_connection = (
            Connection(endpoint, endpoint_parameter, group, proxy)
            if is_incoming
            else Connection(group, proxy, endpoint, endpoint_parameter)
        )
        connections = MagicMock()
        connections.get_outgoing_connections_to_node.return_value = (
            {} if is_incoming else {"internal_out": [internal_connection]}
        )
        connections.get_outgoing_connections_from_parameter.return_value = [wall_connection]
        connections.get_incoming_connections_from_node.return_value = (
            {"internal_in": [internal_connection]} if is_incoming else {}
        )
        connections.get_incoming_connections_to_parameter.return_value = [wall_connection]
        connections.connections = {1: wall_connection}
        success = MagicMock()
        success.failed.return_value = False
        failure = MagicMock()
        failure.failed.return_value = True
        failure.result_details = "edge already removed"
        monkeypatch.setattr(engine.flow_manager, "on_create_connection_request", MagicMock(return_value=success))
        monkeypatch.setattr(
            engine.flow_manager, "on_delete_connection_request", MagicMock(side_effect=[success, failure])
        )

        direction = "incoming" if is_incoming else "outgoing"
        with pytest.raises(RuntimeError, match=f"Failed to delete {direction} wall connection"):
            group.unmap_node_connections(node, connections)

        assert node.parent_group is group

    @pytest.mark.parametrize("is_incoming", [True, False])
    def test_grouped_proxy_uses_the_internal_parameter_shape(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, *, is_incoming: bool
    ) -> None:
        group = _MiniSubflowGroup(name="grouped_proxy")
        source_node = BaseNode(name="source")
        target_node = BaseNode(name="target")
        source_parameter = Parameter(name="source_value", tooltip="")
        target_parameter = Parameter(name="target_value", tooltip="")
        connection = Connection(source_node, source_parameter, target_node, target_parameter)
        proxy = Parameter(name="proxy", tooltip="")
        create_proxy = MagicMock(return_value=proxy)
        create_connections = MagicMock()
        monkeypatch.setattr(group, "_find_existing_proxy_for_source", MagicMock(return_value=None))
        monkeypatch.setattr(group, "_create_proxy_parameter_for_connection", create_proxy)
        monkeypatch.setattr(group, "_create_connections_for_proxy_single", create_connections)
        monkeypatch.setattr(
            engine,
            "handle_request",
            create_autospec(
                engine.handle_request,
                return_value=DeleteConnectionResultSuccess(result_details="deleted"),
            ),
        )

        group._map_external_connections_group([connection], is_incoming=is_incoming)

        expected_parameter = target_parameter if is_incoming else source_parameter
        create_proxy.assert_called_once_with(expected_parameter, is_incoming=is_incoming)
        create_connections.assert_called_once_with(proxy, connection, is_incoming=is_incoming)

    def test_grouped_proxy_reuses_existing_proxy(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        group = _MiniSubflowGroup(name="reused_proxy_group")
        source_node = BaseNode(name="source")
        target_node = BaseNode(name="target")
        source_parameter = Parameter(name="source_value", tooltip="")
        target_parameter = Parameter(name="target_value", tooltip="")
        connection = Connection(source_node, source_parameter, target_node, target_parameter)
        proxy = Parameter(name="existing_proxy", tooltip="")
        create_proxy = MagicMock()
        create_connections = MagicMock()
        handle_request = MagicMock(return_value=DeleteConnectionResultSuccess(result_details="deleted"))
        monkeypatch.setattr(group, "_find_existing_proxy_for_source", MagicMock(return_value=proxy))
        monkeypatch.setattr(group, "_create_proxy_parameter_for_connection", create_proxy)
        monkeypatch.setattr(group, "_create_connections_for_proxy_single", create_connections)
        monkeypatch.setattr(engine, "handle_request", handle_request)

        group._map_external_connections_group([connection], is_incoming=True)

        handle_request.assert_called_once_with(
            DeleteConnectionRequest(
                source_parameter_name=source_parameter.name,
                target_parameter_name=target_parameter.name,
                source_node_name=source_node.name,
                target_node_name=target_node.name,
            )
        )
        create_proxy.assert_not_called()
        create_connections.assert_called_once_with(proxy, connection, is_incoming=True)

    def test_grouped_proxy_ignores_an_empty_connection_list(
        self,
        engine: Engine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        group = _MiniSubflowGroup(name="empty_proxy_group")
        find_proxy = MagicMock()
        handle_request = MagicMock()
        monkeypatch.setattr(group, "_find_existing_proxy_for_source", find_proxy)
        monkeypatch.setattr(engine, "handle_request", handle_request)

        group._map_external_connections_group([], is_incoming=True)

        find_proxy.assert_not_called()
        handle_request.assert_not_called()

    @pytest.mark.parametrize("is_incoming", [True, False])
    def test_find_existing_proxy_matches_source_on_the_correct_side(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, *, is_incoming: bool
    ) -> None:
        group = _MiniSubflowGroup(name="existing_proxy_group")
        proxy = Parameter(name="proxy", tooltip="")
        group.add_parameter(proxy)
        side_key = LEFT_PARAMETERS_KEY if is_incoming else RIGHT_PARAMETERS_KEY
        group.metadata[side_key] = ["removed_proxy", proxy.name]
        source_node = BaseNode(name="source")
        source_parameter = Parameter(name="source_value", tooltip="")
        matching_connection = Connection(source_node, source_parameter, group, proxy)
        connections = MagicMock()
        connections.get_incoming_connections_to_parameter.return_value = [matching_connection]
        monkeypatch.setattr(engine.flow_manager, "get_connections", MagicMock(return_value=connections))

        result = group._find_existing_proxy_for_source(source_node, source_parameter, is_incoming=is_incoming)

        assert result is proxy

    def test_find_existing_proxy_returns_none_when_source_does_not_match(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        group = _MiniSubflowGroup(name="unmatched_proxy_group")
        proxy = Parameter(name="proxy", tooltip="")
        group.add_parameter(proxy)
        group.metadata[LEFT_PARAMETERS_KEY].append(proxy.name)
        source_node = BaseNode(name="source")
        source_parameter = Parameter(name="source_value", tooltip="")
        different_source = Connection(
            BaseNode(name="different_source"),
            Parameter(name="different_value", tooltip=""),
            group,
            proxy,
        )
        connections = MagicMock()
        connections.get_incoming_connections_to_parameter.return_value = [different_source]
        monkeypatch.setattr(engine.flow_manager, "get_connections", MagicMock(return_value=connections))

        result = group._find_existing_proxy_for_source(source_node, source_parameter, is_incoming=True)

        assert result is None

    @pytest.mark.parametrize(
        ("is_incoming", "first_is_internal", "second_is_internal"),
        [(True, False, True), (False, True, False)],
    )
    def test_proxy_connections_preserve_direction_and_increment_cleanup_count(
        self,
        engine: Engine,
        monkeypatch: pytest.MonkeyPatch,
        *,
        is_incoming: bool,
        first_is_internal: bool,
        second_is_internal: bool,
    ) -> None:
        group = _MiniSubflowGroup(name="proxy_edges")
        source_node = BaseNode(name="source")
        target_node = BaseNode(name="target")
        source_parameter = Parameter(name="source_value", tooltip="")
        target_parameter = Parameter(name="target_value", tooltip="")
        proxy = Parameter(name="proxy", tooltip="")
        connection = Connection(source_node, source_parameter, target_node, target_parameter)
        handle_request = MagicMock()
        monkeypatch.setattr(engine, "handle_request", handle_request)

        group._create_connections_for_proxy_single(proxy, connection, is_incoming=is_incoming)
        group._create_connections_for_proxy_single(proxy, connection, is_incoming=is_incoming)

        assert [request.args[0] for request in handle_request.call_args_list] == [
            CreateConnectionRequest(
                source_parameter_name=source_parameter.name,
                target_parameter_name=proxy.name,
                source_node_name=source_node.name,
                target_node_name=group.name,
                is_node_group_internal=first_is_internal,
            ),
            CreateConnectionRequest(
                source_parameter_name=proxy.name,
                target_parameter_name=target_parameter.name,
                source_node_name=group.name,
                target_node_name=target_node.name,
                is_node_group_internal=second_is_internal,
            ),
        ] * 2
        assert group._proxy_param_to_connections[proxy.name] == _PROXY_CONNECTION_COUNT_AFTER_TWO_REMAPPED_EDGES

    def test_remove_nodes_unmaps_each_node_and_remaps_remaining_nodes(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        group = _MiniSubflowGroup(name="remove_group")
        removed = BaseNode(name="removed")
        remaining = BaseNode(name="remaining")
        group.nodes = {removed.name: removed, remaining.name: remaining}
        removed.parent_group = group
        remaining.parent_group = group
        connections = MagicMock()
        unmap = MagicMock()
        remap = MagicMock()
        monkeypatch.setattr(group, "unmap_node_connections", unmap)
        monkeypatch.setattr(group, "_map_external_connections_for_nodes", remap)
        monkeypatch.setattr(engine.flow_manager, "get_connections", MagicMock(return_value=connections))

        removed_nodes = group.remove_nodes_from_group([removed])

        assert removed_nodes == [removed]
        unmap.assert_called_once_with(removed, connections)
        remap.assert_called_once_with([remaining], connections, {"remaining"})
        assert remaining.parent_group is group


def _group_with_proxy(engine: Engine, name: str) -> _MiniSubflowGroup:
    """Register a group and add one metadata-tracked proxy for cleanup tests."""
    group = _MiniSubflowGroup(name=name)
    engine.object_manager.add_object_by_name(group.name, group)
    proxy = Parameter(name="proxy", tooltip="", user_defined=True)
    group.add_parameter(proxy)
    group.metadata[RIGHT_PARAMETERS_KEY].append(proxy.name)
    return group


class _MiniSubflowGroup(SubflowNodeGroup):
    """Minimal concrete SubflowNodeGroup exercising only _create_subflow."""

    async def aprocess(self) -> None:  # pragma: no cover - execution not exercised here
        await self.execute_subflow()

    def process(self) -> Any:  # pragma: no cover - execution not exercised here
        return None
