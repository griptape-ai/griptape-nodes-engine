import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import NamedTuple

from griptape_nodes.exe_types.base_iterative_nodes import BaseIterativeEndNode, BaseIterativeStartNode
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode, ParameterTypeBuiltin
from griptape_nodes.exe_types.node_types import BaseNode, Connection, NodeResolutionState

logger = logging.getLogger("griptape_nodes")


class Direction(StrEnum):
    UPSTREAM = "upstream"
    DOWNSTREAM = "downstream"


class ConnectionData(NamedTuple):
    node: BaseNode
    parameter: Parameter


@dataclass
class Connections:
    # store connections as IDs
    connections: dict[int, Connection]
    # Store in node.name:parameter.name to connection id
    outgoing_index: dict[str, dict[str, list[int]]]
    incoming_index: dict[str, dict[str, list[int]]]

    # In order to get those nodes that are dirty and resolve them
    def __init__(self) -> None:
        self.connections = {}
        self.outgoing_index = {}
        self.incoming_index = {}

    def has_connection(
        self,
        source_node_name: str,
        source_parameter_name: str,
        target_node_name: str,
        target_parameter_name: str,
    ) -> bool:
        """Return whether an exact connection between the named endpoints exists."""
        return (
            self._get_connection_by_endpoints(
                source_node_name,
                source_parameter_name,
                target_node_name,
                target_parameter_name,
            )
            is not None
        )

    def add_connection(
        self,
        source_node: BaseNode,
        source_parameter: Parameter,
        target_node: BaseNode,
        target_parameter: Parameter,
        *,
        is_node_group_internal: bool = False,
    ) -> Connection:
        if ParameterMode.OUTPUT not in source_parameter.get_mode():
            errormsg = f"Output Connection not allowed on Parameter '{source_parameter.name}'."
            raise ValueError(errormsg)
        if ParameterMode.INPUT not in target_parameter.get_mode():
            errormsg = f"Input Connection not allowed on Parameter '{target_parameter.name}'."
            raise ValueError(errormsg)

        existing_connection = self._get_connection_by_endpoints(
            source_node.name,
            source_parameter.name,
            target_node.name,
            target_parameter.name,
        )
        if existing_connection is not None:
            return existing_connection

        # Handle multiple inputs on parameters and multiple outputs on controls
        if self.connection_allowed(source_node, source_parameter, is_source=True) and self.connection_allowed(
            target_node, target_parameter, is_source=False
        ):
            connection = Connection(
                source_node,
                source_parameter,
                target_node,
                target_parameter,
                is_node_group_internal=is_node_group_internal,
            )
            # New index management.
            connection_id = id(connection)
            # Add connection to our dict here
            self.connections[connection_id] = connection
            # Outgoing connection
            self.outgoing_index.setdefault(source_node.name, {}).setdefault(source_parameter.name, []).append(
                connection_id
            )
            # Incoming connection
            self.incoming_index.setdefault(target_node.name, {}).setdefault(target_parameter.name, []).append(
                connection_id
            )
            return connection
        msg = "Connection not allowed because of multiple connections on the same parameter input or control output parameter"
        raise ValueError(msg)

    def _get_connection_by_endpoints(
        self,
        source_node_name: str,
        source_parameter_name: str,
        target_node_name: str,
        target_parameter_name: str,
    ) -> Connection | None:
        connection_ids = self.outgoing_index.get(source_node_name, {}).get(source_parameter_name, [])
        for connection_id in connection_ids:
            connection = self.connections.get(connection_id)
            if connection is None:
                continue
            if (
                connection.target_node.name == target_node_name
                and connection.target_parameter.name == target_parameter_name
            ):
                return connection
        return None

    def get_existing_connection_for_restricted_scenario(
        self, node: BaseNode, parameter: Parameter, *, is_source: bool
    ) -> Connection | None:
        """Returns connections that may exist if we are in a restricted connection scenario (see below), or None if not in such as scenario.

        Here are the rules as enforced by the engine:
          * A Control Parameter can have multiple connections to an input, but only one connection on an output.
          * A Data Parameter can have one connection on an input, but multiple outputs.

        Args:
            node: the Node we are querying
            parameter: the parameter on the Node
            is_source: are we assessing this node/parameter combo as a SOURCE for a connection or as a TARGET?

        Returns:
            None: if the source node/parameter isn't in a restricted scenario OR in a restricted scenario but no connection in place.
            Connection: if in a restricted scenario and has a Connection.
        """
        connection_list = None
        if is_source and ParameterTypeBuiltin.CONTROL_TYPE.value == parameter.output_type:
            connections = self.outgoing_index
            connections_from_node = connections.get(node.name, {})
            connection_list = connections_from_node.get(parameter.name, None)
        if not is_source and ParameterTypeBuiltin.CONTROL_TYPE.value not in parameter.input_types:
            connections = self.incoming_index
            connections_from_node = connections.get(node.name, {})
            connection_list = connections_from_node.get(parameter.name, None)

        if connection_list:
            connection_id = connection_list[0]
            connection = self.connections[connection_id]
            return connection

        # Not in a restricted scenario and/or no connection in place.
        return None

    def connection_allowed(self, node: BaseNode, parameter: Parameter, *, is_source: bool) -> bool:
        # True if allowed, false if not
        # See if we're in a scenario where we can only have one such connection which would prevent us from establishing an additional connection.
        connection = self.get_existing_connection_for_restricted_scenario(
            node=node, parameter=parameter, is_source=is_source
        )
        return connection is None

    def _get_connected_node_for_end_loop_control(
        self, end_loop_node: BaseIterativeEndNode, control_parameter: Parameter
    ) -> ConnectionData | None:
        """For a BaseIterativeEndNode and its control parameter, finds the connected node and parameter.

        It checks both outgoing connections (where BaseIterativeEndNode's parameter is a source)
        and incoming connections (where BaseIterativeEndNode's parameter is a target).
        """
        # Check if the BaseIterativeEndNode's control parameter is a source for an outgoing connection
        if ParameterMode.OUTPUT in control_parameter.allowed_modes:
            outgoing_connections_for_node = self.outgoing_index.get(end_loop_node.name, {})
            connection_ids_as_source = outgoing_connections_for_node.get(control_parameter.name, [])
            if connection_ids_as_source:
                connection_id = connection_ids_as_source[0]
                connection = self.connections.get(connection_id)
                if connection:
                    return ConnectionData(connection.target_node, connection.target_parameter)
        elif ParameterMode.INPUT in control_parameter.allowed_modes:
            # Check if the BaseIterativeEndNode's control parameter is a target for an incoming connection
            incoming_connections_for_node = self.incoming_index.get(end_loop_node.name, {})
            connection_ids_as_target = incoming_connections_for_node.get(control_parameter.name, [])
            if connection_ids_as_target:
                for connection_id in connection_ids_as_target:
                    connection = self.connections.get(connection_id)
                    if connection and isinstance(connection.source_node, BaseIterativeStartNode):
                        return ConnectionData(connection.source_node, connection.source_parameter)
        return None  # No connection found for this control parameter

    def get_connected_node(  # noqa: C901, Need if checks.
        self, node: BaseNode, parameter: Parameter, direction: Direction | None = None, *, include_internal: bool = True
    ) -> ConnectionData | None:
        # Check to see if we should be getting the next connection or the previous connection based on the parameter.
        # Override this method for BaseIterativeEndNodes - these might have to go backwards or forwards.
        if direction is not None:
            # We've added direction as an override, since we sometimes need to get connections in a certain direction regardless of parameter types.
            if direction == Direction.UPSTREAM:
                connections = self.incoming_index
            elif direction == Direction.DOWNSTREAM:
                connections = self.outgoing_index
        else:
            if (
                isinstance(node, BaseIterativeEndNode)
                and ParameterTypeBuiltin.CONTROL_TYPE.value == parameter.output_type
            ):
                return self._get_connected_node_for_end_loop_control(node, parameter)
            if ParameterTypeBuiltin.CONTROL_TYPE.value == parameter.output_type:
                connections = self.outgoing_index
                # We still default to downstream (forwards) connections for control parameters
                direction = Direction.DOWNSTREAM
            else:
                connections = self.incoming_index
                # And upstream (backwards) connections for data parameters.
                direction = Direction.UPSTREAM
        connections_from_node = connections.get(node.name, {})

        connection_id = connections_from_node.get(parameter.name, [])
        # TODO: https://github.com/griptape-ai/griptape-nodes/issues/859
        if not len(connection_id):
            return None
        # Right now, our special case is that it is ok to have multiple inputs to a CONTROL_TYPE parameter, so if we're going upstream, it's ok. And it's ok to have multiple downstream outputs from a data type parameter.
        if (
            len(connection_id) > 1
            and not (
                direction == Direction.UPSTREAM and parameter.output_type == ParameterTypeBuiltin.CONTROL_TYPE.value
            )
            and not (
                direction == Direction.DOWNSTREAM and parameter.output_type != ParameterTypeBuiltin.CONTROL_TYPE.value
            )
        ):
            msg = f"There should not be more than one {direction} connection here to/from {node.name}.{parameter.name}"
            raise ValueError(msg)
        connection_id = connection_id[0]
        if connection_id in self.connections:
            connection = self.connections[connection_id]
            # We don't traverse internal NodeGroup connections when include_internal is False.
            if connection.is_node_group_internal and not include_internal:
                return None
            if direction == Direction.DOWNSTREAM:
                # Return the target (next place to go)
                return ConnectionData(connection.target_node, connection.target_parameter)
            # Return the source (next place to chain back to)
            return ConnectionData(connection.source_node, connection.source_parameter)
        return None

    def remove_connection_by_object(self, conn: Connection) -> bool:
        """Remove a connection from the manager by Connection object.

        Args:
            conn: The Connection object to remove

        Returns:
            True if connection was removed, False if not found
        """
        conn_id = id(conn)

        if conn_id not in self.connections:
            return False

        self._remove_connection(
            conn_id,
            conn.source_node.name,
            conn.source_parameter.name,
            conn.target_node.name,
            conn.target_parameter.name,
        )
        return True

    def remove_connection(
        self, source_node: str, source_parameter: str, target_node: str, target_parameter: str
    ) -> bool:
        # Remove from outgoing
        try:
            # use copy to prevent modifying the list while it's iterating
            outgoing_parameter_connections = self.outgoing_index[source_node][source_parameter].copy()
        except Exception:
            logger.exception("Cannot remove connection that does not exist")
            return False
        for connection_id in outgoing_parameter_connections:
            if connection_id not in self.connections:
                logger.error("Cannot remove connection does not exist")
                return False
            connection = self.connections[connection_id]
            test_target_node = connection.target_node.name
            test_target_parameter = connection.target_parameter.name
            if test_target_node == target_node and test_target_parameter == target_parameter:
                self._remove_connection(
                    connection_id, source_node, source_parameter, test_target_node, test_target_parameter
                )
                return True
        return False

    def _remove_connection(
        self, connection_id: int, source_node: str, source_param: str, target_node: str, target_param: str
    ) -> None:
        # Now delete from EVERYWHERE!
        # delete the parameter from the node name dictionary
        self.outgoing_index[source_node][source_param].remove(connection_id)
        if not self.outgoing_index[source_node][source_param]:
            del self.outgoing_index[source_node][source_param]
            # if the node name dictionary is empty, delete it!
            if not self.outgoing_index[source_node]:
                del self.outgoing_index[source_node]
        # delete the parameter from the node name dictionary
        self.incoming_index[target_node][target_param].remove(connection_id)
        if not self.incoming_index[target_node][target_param]:
            del self.incoming_index[target_node][target_param]
            # if the node name dictionary is empty, delete it!
            if not self.incoming_index[target_node]:
                del self.incoming_index[target_node]
        # delete from the connections dictionary
        del self.connections[connection_id]

    def get_connections_between_nodes(self, node_names: set[str]) -> list[Connection]:
        """Get all connections where both source and target are in the provided set.

        Args:
            node_names: Set of node names to check for internal connections

        Returns:
            List of connections that have both endpoints in the node_names set
        """
        internal_connections = []

        for node_name in node_names:
            if node_name not in self.outgoing_index:
                continue

            for connection_ids in self.outgoing_index[node_name].values():
                for conn_id in connection_ids:
                    if conn_id not in self.connections:
                        continue
                    conn = self.connections[conn_id]

                    if conn.target_node.name in node_names:
                        internal_connections.append(conn)

        return internal_connections

    # Used to check data connections for all future nodes to be BAD!
    def unresolve_future_nodes(self, node: BaseNode, _visited: set[str] | None = None) -> None:
        # Recursively traverse downstream nodes, tracking visited nodes to avoid cycles.
        if _visited is None:
            _visited = set()
        if node.name in _visited:
            return
        _visited.add(node.name)

        # There are no outgoing connections from this node.
        if node.name not in self.outgoing_index:
            return
        for parameter in node.parameters:
            # If it is a data connection and has an OUTPUT type
            # and an outgoing connection exists from this parameter
            if (
                ParameterMode.OUTPUT in parameter.allowed_modes
                and ParameterTypeBuiltin.CONTROL_TYPE.value != parameter.output_type
                and parameter.name in self.outgoing_index[node.name]
            ):
                # A connection or connections exist
                connections = self.outgoing_index[node.name][parameter.name]
                # For each connection, check the next node and do all the same.
                for connection_id in connections:
                    if connection_id in self.connections:
                        connection = self.connections[connection_id]
                        target_node = connection.target_node
                        # If that node is resolved, mark it unresolved.
                        # Emit an event so clients know this node has changed resolution state.
                        if target_node.state == NodeResolutionState.RESOLVED:
                            target_node.make_node_unresolved(
                                current_states_to_trigger_change_event=set(
                                    {NodeResolutionState.RESOLVED, NodeResolutionState.RESOLVING}
                                )
                            )
                        # Always continue traversal through downstream nodes so that
                        # resolved nodes beyond an already-unresolved intermediate are
                        # still reached and unresolved.
                        self.unresolve_future_nodes(target_node, _visited)

    def get_outgoing_connections_to_node(self, node: BaseNode, to_node: BaseNode) -> dict[str, list[Connection]]:
        connections = {}
        if node.name in self.outgoing_index:
            parameters = self.outgoing_index[node.name]
            for parameter, connection_ids in parameters.items():
                for conn_id in connection_ids:
                    connection = self.connections[conn_id]
                    if connection.target_node.name == to_node.name:
                        if parameter not in connections:
                            connections[parameter] = [connection]
                        else:
                            connections[parameter].append(connection)
        return connections

    def get_incoming_connections_from_node(self, node: BaseNode, from_node: BaseNode) -> dict[str, list[Connection]]:
        connections = {}
        if node.name in self.incoming_index:
            parameters = self.incoming_index[node.name]
            for parameter, connection_ids in parameters.items():
                for conn_id in connection_ids:
                    connection = self.connections[conn_id]
                    if connection.source_node.name == from_node.name:
                        if parameter not in connections:
                            connections[parameter] = [connection]
                        else:
                            connections[parameter].append(connection)
        return connections

    def get_outgoing_connections_from_parameter(self, node: BaseNode, parameter: Parameter) -> list[Connection]:
        connections = []
        if node.name in self.outgoing_index and parameter.name in self.outgoing_index[node.name]:
            for conn_id in self.outgoing_index[node.name][parameter.name]:
                connections.append(self.connections[conn_id])  # noqa: PERF401
        return connections

    def get_incoming_connections_to_parameter(self, node: BaseNode, parameter: Parameter) -> list[Connection]:
        connections = []
        if node.name in self.incoming_index and parameter.name in self.incoming_index[node.name]:
            for conn_id in self.incoming_index[node.name][parameter.name]:
                connections.append(self.connections[conn_id])  # noqa: PERF401
        return connections

    def get_all_outgoing_connections(self, node: BaseNode) -> list[Connection]:
        """Get all outgoing connections from a node.

        Args:
            node: The node to get outgoing connections for

        Returns:
            List of all outgoing connections from the node
        """
        connections = []
        if node.name in self.outgoing_index:
            for connection_ids in self.outgoing_index[node.name].values():
                for conn_id in connection_ids:
                    connections.append(self.connections[conn_id])  # noqa: PERF401, Keeping loop for understanding.
        return connections

    def get_all_incoming_connections(self, node: BaseNode) -> list[Connection]:
        """Get all incoming connections to a node.

        Args:
            node: The node to get incoming connections for

        Returns:
            List of all incoming connections to the node
        """
        connections = []
        if node.name in self.incoming_index:
            for connection_ids in self.incoming_index[node.name].values():
                for conn_id in connection_ids:
                    connections.append(self.connections[conn_id])  # noqa: PERF401, Keeping loop for understanding.
        return connections

    def is_node_in_forward_control_path(
        self, start_node: BaseNode, target_node: BaseNode, visited: set[str] | None = None
    ) -> bool:
        """Check if target_node is reachable from start_node through control flow connections.

        Args:
            start_node: The node to start traversal from
            target_node: The node to check if reachable
            visited: Set of already visited node names (used internally for recursion)

        Returns:
            True if target_node is in the forward control path from start_node, False otherwise
        """
        if visited is None:
            visited = set()

        if start_node.name in visited:
            return False
        visited.add(start_node.name)

        # Check ALL outgoing control connections
        # This handles IfElse nodes that have multiple possible control outputs
        if start_node.name in self.outgoing_index:
            for param_name, connection_ids in self.outgoing_index[start_node.name].items():
                # Find the parameter to check if it's a control type
                param = start_node.get_parameter_by_name(param_name)
                if param and param.output_type == ParameterTypeBuiltin.CONTROL_TYPE.value:
                    # This is a control parameter - check all its connections
                    for connection_id in connection_ids:
                        if connection_id in self.connections:
                            connection = self.connections[connection_id]
                            next_node = connection.target_node

                            if next_node.name == target_node.name:
                                return True

                            # Recursively check the forward path
                            if self.is_node_in_forward_control_path(next_node, target_node, visited):
                                return True

        return False
