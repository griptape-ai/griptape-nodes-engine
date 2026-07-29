from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from griptape_nodes.exe_types.core_types import (
    ControlParameter,
    Parameter,
    ParameterMode,
    ParameterTypeBuiltin,
    Trait,
)
from griptape_nodes.exe_types.flow import ControlFlow
from griptape_nodes.exe_types.node_groups.base_node_group import BaseNodeGroup
from griptape_nodes.exe_types.node_types import (
    LOCAL_EXECUTION,
    get_library_names_with_publish_handlers,
)
from griptape_nodes.exe_types.param_components.subflow_execution_component import SubflowExecutionComponent
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    DeleteConnectionRequest,
    DeleteConnectionResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import DeleteFlowRequest, DeleteFlowResultFailure
from griptape_nodes.retained_mode.events.parameter_events import (
    AddParameterToNodeRequest,
    AddParameterToNodeResultSuccess,
    RemoveParameterFromNodeRequest,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.traits.options import Options

if TYPE_CHECKING:
    from griptape_nodes.exe_types.connections import Connections
    from griptape_nodes.exe_types.node_types import BaseNode, Connection

logger = logging.getLogger("griptape_nodes")

NODE_GROUP_FLOW = "NodeGroupFlow"
LEFT_PARAMETERS_KEY = "left_parameters"
RIGHT_PARAMETERS_KEY = "right_parameters"


class SubflowNodeGroup(BaseNodeGroup, ABC):
    """Abstract base class for subflow node groups.

    Proxy node that represents a group of nodes during DAG execution.

    This node acts as a single execution unit for a group of nodes that should
    be executed in parallel. When the DAG executor encounters this proxy node,
    it passes the entire NodeGroup to the NodeExecutor which handles parallel
    execution of all grouped nodes.

    The proxy node has parameters that mirror the external connections to/from
    the group, allowing it to seamlessly integrate into the DAG structure.
    """

    _proxy_param_to_connections: dict[str, int]

    def __init__(
        self,
        name: str,
        metadata: dict[Any, Any] | None = None,
    ) -> None:
        super().__init__(name, metadata)
        self.execution_environment = Parameter(
            name="execution_environment",
            tooltip="Environment that the group should execute in",
            type=ParameterTypeBuiltin.STR,
            allowed_modes={ParameterMode.PROPERTY},
            default_value=LOCAL_EXECUTION,
            traits={Options(choices=get_library_names_with_publish_handlers())},
        )
        self.add_parameter(self.execution_environment)
        # Track mapping from proxy parameter name to (original_node, original_param_name)
        self._proxy_param_to_connections = {}
        if "execution_environment" not in self.metadata:
            self.metadata["execution_environment"] = {}
        self.metadata["execution_environment"]["Griptape Nodes Library"] = {
            "start_flow_node": "StartFlow",
            "parameter_names": {},
        }
        self.metadata["executable"] = True

        # Don't create subflow in __init__ - it will be created on-demand when nodes are added
        # or restored during deserialization

        # Add parameters from registered StartFlow nodes for each publishing library
        self._add_start_flow_parameters()

        # Add subprocess execution status component for real-time GUI updates
        self._add_subflow_execution_parameters()

    def _create_subflow(self) -> None:
        """Create a dedicated subflow for this NodeGroup's nodes.

        Note: This is called during __init__, so the node may not yet be added to a flow.
        The subflow will be created without a parent initially, and can be reparented later.
        """
        from griptape_nodes.retained_mode.events.flow_events import (
            CreateFlowRequest,
            CreateFlowResultSuccess,
        )
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        subflow_name = f"{self.name}_subflow"
        self.metadata["subflow_name"] = subflow_name

        # Get current flow to set as parent so subflow will be serialized with parent
        current_flow = GriptapeNodes.ContextManager().get_current_flow()
        parent_flow_name = current_flow.name if current_flow else None

        # Create metadata with flow_type
        subflow_metadata = {"flow_type": NODE_GROUP_FLOW}

        request = CreateFlowRequest(
            flow_name=subflow_name,
            parent_flow_name=parent_flow_name,
            set_as_new_context=False,
            metadata=subflow_metadata,
        )
        result = GriptapeNodes.handle_request(request)
        if isinstance(result, CreateFlowResultSuccess):
            # Final name may be different that initial name due to de-dupe.
            self.metadata["subflow_name"] = result.flow_name

        if not isinstance(result, CreateFlowResultSuccess):
            logger.warning("%s failed to create subflow '%s': %s", self.name, subflow_name, result.result_details)

    def _add_start_flow_parameters(self) -> None:
        """Add parameters from all registered StartFlow nodes to this SubflowNodeGroup.

        For each library that has registered a PublishWorkflowRequest handler with
        a StartFlow node, this method:
        1. Creates a temporary instance of that StartFlow node
        2. Extracts all its parameters
        3. Adds them to this SubflowNodeGroup with a prefix based on the class name
        4. Stores metadata mapping execution environments to their parameters
        """
        from griptape_nodes.retained_mode.events.workflow_events import PublishWorkflowRequest
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        # Initialize metadata structure for execution environment mappings
        if self.metadata is None:
            self.metadata = {}
        if "execution_environment" not in self.metadata:
            self.metadata["execution_environment"] = {}

        # Get all libraries that have registered PublishWorkflowRequest handlers
        library_manager = GriptapeNodes.LibraryManager()
        event_handlers = library_manager.get_registered_event_handlers(PublishWorkflowRequest)

        # Process each registered library
        for library_name, handler in event_handlers.items():
            self._process_library_start_flow_parameters(library_name, handler)

    def _add_subflow_execution_parameters(self) -> None:
        """Add parameters for subflow execution tracking."""
        self._subflow_execution_component = SubflowExecutionComponent(self)
        self._subflow_execution_component.add_output_parameters()

    def _process_library_start_flow_parameters(self, library_name: str, handler: Any) -> None:
        """Process and add StartFlow parameters from a single library.

        Args:
            library_name: Name of the library
            handler: The registered event handler containing event data
        """
        import logging

        from griptape_nodes.node_library.library_registry import LibraryRegistry
        from griptape_nodes.retained_mode.events.workflow_events import PublishWorkflowRegisteredEventData

        logger = logging.getLogger(__name__)

        registered_event_data = handler.event_data

        if registered_event_data is None:
            return
        if not isinstance(registered_event_data, PublishWorkflowRegisteredEventData):
            return

        # Get the StartFlow node information
        start_flow_node_type = registered_event_data.start_flow_node_type
        start_flow_library_name = registered_event_data.start_flow_node_library_name

        try:
            # Get the library that contains the StartFlow node
            library = LibraryRegistry.get_library(name=start_flow_library_name)
        except KeyError:
            logger.debug(
                "Library '%s' not found when adding StartFlow parameters for '%s'",
                start_flow_library_name,
                library_name,
            )
            return

        try:
            # Create a temporary instance of the StartFlow node to inspect its parameters
            temp_start_flow_node = library.create_node(
                node_type=start_flow_node_type,
                name=f"temp_{start_flow_node_type}",
            )
        except Exception as e:
            logger.debug(
                "Failed to create temporary StartFlow node '%s' from library '%s': %s",
                start_flow_node_type,
                start_flow_library_name,
                e,
            )
            return

        # Get the class name for prefixing (convert to lowercase for parameter naming)
        class_name_prefix = start_flow_node_type.lower()

        # Store metadata for this execution environment
        parameter_names = []

        # Add each parameter from the StartFlow node to this SubflowNodeGroup
        for param in temp_start_flow_node.parameters:
            if isinstance(param, ControlParameter):
                continue

            # Create prefixed parameter name
            prefixed_param_name = f"{class_name_prefix}_{param.name}"
            parameter_names.append(prefixed_param_name)

            # Clone and add the parameter
            self._clone_and_add_parameter(param, prefixed_param_name)

        # Store the mapping in metadata
        self.metadata["execution_environment"][library_name] = {
            "start_flow_node": start_flow_node_type,
            "parameter_names": parameter_names,
        }

    def _clone_and_add_parameter(self, param: Parameter, new_name: str) -> None:
        """Clone a parameter with a new name and add it to this node.

        Args:
            param: The parameter to clone
            new_name: The new name for the cloned parameter
        """
        # Extract traits from parameter children (traits are stored as children of type Trait)
        traits_set: set[type[Trait] | Trait] | None = {child for child in param.children if isinstance(child, Trait)}
        if not traits_set:
            traits_set = None

        # Clone the parameter with the new name
        cloned_param = Parameter(
            name=new_name,
            tooltip=param.tooltip,
            type=param.type,
            allowed_modes=param.allowed_modes,
            default_value=param.default_value,
            traits=traits_set,
            parent_container_name=param.parent_container_name,
            parent_element_name=param.parent_element_name,
        )

        # Add the parameter to this node
        self.add_parameter(cloned_param)

    def _create_proxy_parameter_for_connection(self, original_param: Parameter, *, is_incoming: bool) -> Parameter:
        """Create a proxy parameter on this SubflowNodeGroup for an external connection.

        Args:
            original_param: The parameter from the grouped node
            grouped_node: The node within the group that has the original parameter
            conn_id: The connection ID for uniqueness
            is_incoming: True if this is an incoming connection to the group

        Returns:
            The newly created proxy parameter
        """
        # Clone the parameter with the new name
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        input_types = None
        output_type = None
        if is_incoming:
            input_types = original_param.input_types
        else:
            output_type = original_param.output_type

        request = AddParameterToNodeRequest(
            node_name=self.name,
            parameter_name=original_param.name,
            input_types=input_types,
            output_type=output_type,
            tooltip="",
            mode_allowed_input=True,
            mode_allowed_output=True,
        )
        # Add with a request, because this will handle naming for us.
        result = GriptapeNodes.handle_request(request)
        if not isinstance(result, AddParameterToNodeResultSuccess):
            msg = "Failed to add parameter to node."
            raise TypeError(msg)
        # Retrieve and return the newly created parameter
        proxy_param = self.get_parameter_by_name(result.parameter_name)
        if proxy_param is None:
            msg = f"{self.name} failed to create proxy parameter '{result.parameter_name}'"
            raise RuntimeError(msg)
        if is_incoming:
            if LEFT_PARAMETERS_KEY in self.metadata:
                self.metadata[LEFT_PARAMETERS_KEY].append(proxy_param.name)
            else:
                self.metadata[LEFT_PARAMETERS_KEY] = [proxy_param.name]
        elif RIGHT_PARAMETERS_KEY in self.metadata:
            self.metadata[RIGHT_PARAMETERS_KEY].append(proxy_param.name)
        else:
            self.metadata[RIGHT_PARAMETERS_KEY] = [proxy_param.name]

        return proxy_param

    def get_all_nodes(self) -> dict[str, BaseNode]:
        all_nodes = {}
        for node_name, node in self.nodes.items():
            all_nodes[node_name] = node
            if isinstance(node, SubflowNodeGroup):
                all_nodes.update(node.nodes)
        return all_nodes

    def map_external_connection(self, conn: Connection, *, is_incoming: bool) -> bool:
        """Track a connection to/from a node in the group and rewire it through a proxy parameter.

        Args:
            conn: The external connection to track
            conn_id: ID of the connection
            is_incoming: True if connection is coming INTO the group
        """
        if is_incoming:
            grouped_parameter = conn.target_parameter
            # Store the existing connection so it can be recreated if needed.
        else:
            grouped_parameter = conn.source_parameter
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        request = DeleteConnectionRequest(
            conn.source_parameter.name,
            conn.target_parameter.name,
            conn.source_node.name,
            conn.target_node.name,
        )
        result = GriptapeNodes.handle_request(request)
        if not isinstance(result, DeleteConnectionResultSuccess):
            return False
        proxy_parameter = self._create_proxy_parameter_for_connection(grouped_parameter, is_incoming=is_incoming)
        # Create connections for proxy parameter
        self.create_connections_for_proxy(proxy_parameter, conn, is_incoming=is_incoming)
        return True

    def create_connections_for_proxy(
        self, proxy_parameter: Parameter, old_connection: Connection, *, is_incoming: bool
    ) -> None:
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        create_first_connection = CreateConnectionRequest(
            source_parameter_name=old_connection.source_parameter.name,
            target_parameter_name=proxy_parameter.name,
            source_node_name=old_connection.source_node.name,
            target_node_name=self.name,
            is_node_group_internal=not is_incoming,
        )
        create_second_connection = CreateConnectionRequest(
            source_parameter_name=proxy_parameter.name,
            target_parameter_name=old_connection.target_parameter.name,
            source_node_name=self.name,
            target_node_name=old_connection.target_node.name,
            is_node_group_internal=is_incoming,
        )
        # Store the mapping from proxy parameter to original node/parameter
        # only increment by 1, even though we're making two connections.
        if proxy_parameter.name not in self._proxy_param_to_connections:
            self._proxy_param_to_connections[proxy_parameter.name] = 2
        else:
            self._proxy_param_to_connections[proxy_parameter.name] += 2
        GriptapeNodes.handle_request(create_first_connection)
        GriptapeNodes.handle_request(create_second_connection)

    def unmap_node_connections(self, node: BaseNode, connections: Connections) -> None:  # noqa: C901
        """Remove tracking of an external connection, restore original connection, and clean up proxy parameter.

        Args:
            node: The node to unmap
            connections: The connections object
        """
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        # For the node being removed - We need to figure out all of it's connections TO the node group. These connections need to be remapped.
        # If we delete connections from a proxy parameter, and it has no more connections, then the proxy parameter should be deleted unless it's user defined.
        # It will 1. not be in the proxy map. and 2. it will have a value of > 0
        # Get all outgoing connections
        outgoing_connections = connections.get_outgoing_connections_to_node(node, to_node=self)
        # Delete outgoing connections
        for parameter_name, outgoing_connection_list in outgoing_connections.items():
            for outgoing_connection in outgoing_connection_list:
                proxy_parameter = outgoing_connection.target_parameter
                # get old connections first, since this will delete the proxy
                remap_connections = connections.get_outgoing_connections_from_parameter(self, proxy_parameter)
                # Delete the internal connection
                delete_result = GriptapeNodes.FlowManager().on_delete_connection_request(
                    DeleteConnectionRequest(
                        source_parameter_name=parameter_name,
                        target_parameter_name=proxy_parameter.name,
                        source_node_name=node.name,
                        target_node_name=self.name,
                    )
                )
                if delete_result.failed():
                    msg = f"{self.name}: Failed to delete internal outgoing connection from {node.name}.{parameter_name} to proxy {proxy_parameter.name}: {delete_result.result_details}"
                    raise RuntimeError(msg)

                # Now create the new connection! We need to get the connections from the proxy parameter
                for connection in remap_connections:
                    create_result = GriptapeNodes.FlowManager().on_create_connection_request(
                        CreateConnectionRequest(
                            source_parameter_name=parameter_name,
                            target_parameter_name=connection.target_parameter.name,
                            source_node_name=node.name,
                            target_node_name=connection.target_node.name,
                        )
                    )
                    if create_result.failed():
                        msg = f"{self.name}: Failed to create direct outgoing connection from {node.name}.{parameter_name} to {connection.target_node.name}.{connection.target_parameter.name}: {create_result.result_details}"
                        raise RuntimeError(msg)

        # Get all incoming connections
        incoming_connections = connections.get_incoming_connections_from_node(node, from_node=self)
        # Delete incoming connections
        for parameter_name, incoming_connection_list in incoming_connections.items():
            for incoming_connection in incoming_connection_list:
                proxy_parameter = incoming_connection.source_parameter
                # Get the incoming connections to the proxy parameter
                remap_connections = connections.get_incoming_connections_to_parameter(self, proxy_parameter)
                # Delete the internal connection
                delete_result = GriptapeNodes.FlowManager().on_delete_connection_request(
                    DeleteConnectionRequest(
                        source_parameter_name=proxy_parameter.name,
                        target_parameter_name=parameter_name,
                        source_node_name=self.name,
                        target_node_name=node.name,
                    )
                )
                if delete_result.failed():
                    msg = f"{self.name}: Failed to delete internal incoming connection from proxy {proxy_parameter.name} to {node.name}.{parameter_name}: {delete_result.result_details}"
                    raise RuntimeError(msg)

                # Now create the new connection! We need to get the connections to the proxy parameter
                for connection in remap_connections:
                    create_result = GriptapeNodes.FlowManager().on_create_connection_request(
                        CreateConnectionRequest(
                            source_parameter_name=connection.source_parameter.name,
                            target_parameter_name=parameter_name,
                            source_node_name=connection.source_node.name,
                            target_node_name=node.name,
                        )
                    )
                    if create_result.failed():
                        msg = f"{self.name}: Failed to create direct incoming connection from {connection.source_node.name}.{connection.source_parameter.name} to {node.name}.{parameter_name}: {create_result.result_details}"
                        raise RuntimeError(msg)

    def _remove_nodes_from_existing_parents(self, nodes: list[BaseNode]) -> None:
        """Remove nodes from their existing parent groups."""
        child_nodes = {}
        for node in nodes:
            if node.parent_group is not None:
                existing_parent_group = node.parent_group
                if isinstance(existing_parent_group, SubflowNodeGroup):
                    child_nodes.setdefault(existing_parent_group, []).append(node)
        for parent_group, node_list in child_nodes.items():
            parent_group.remove_nodes_from_group(node_list)

    def _cleanup_proxy_parameter(self, proxy_parameter: Parameter, metadata_key: str) -> None:
        """Clean up proxy parameter if it has no more connections.

        Args:
            proxy_parameter: The proxy parameter to potentially clean up
            metadata_key: The metadata key ('left_parameters' or 'right_parameters')
        """
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        if proxy_parameter.name not in self._proxy_param_to_connections:
            return

        self._proxy_param_to_connections[proxy_parameter.name] -= 1
        if self._proxy_param_to_connections[proxy_parameter.name] == 0:
            GriptapeNodes.NodeManager().on_remove_parameter_from_node_request(
                request=RemoveParameterFromNodeRequest(node_name=self.name, parameter_name=proxy_parameter.name)
            )
            del self._proxy_param_to_connections[proxy_parameter.name]
            if metadata_key in self.metadata and proxy_parameter.name in self.metadata[metadata_key]:
                self.metadata[metadata_key].remove(proxy_parameter.name)

    def _remap_outgoing_connections(self, node: BaseNode, connections: Connections) -> None:
        """Remap outgoing connections that go through proxy parameters.

        Args:
            node: The node being added to the group
            connections: Connections object from FlowManager
        """
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        outgoing_connections = connections.get_outgoing_connections_to_node(node, to_node=self)
        for parameter_name, outgoing_connection_list in outgoing_connections.items():
            for outgoing_connection in outgoing_connection_list:
                proxy_parameter = outgoing_connection.target_parameter
                remap_connections = connections.get_outgoing_connections_from_parameter(self, proxy_parameter)

                # Check if proxy has other incoming connections besides this one
                # If so, we should keep the proxy and its outgoing connections
                incoming_to_proxy = connections.get_incoming_connections_to_parameter(self, proxy_parameter)
                other_incoming_exists = any(
                    conn.source_node.name != node.name or conn.source_parameter.name != parameter_name
                    for conn in incoming_to_proxy
                )

                # Delete the connection from this node to proxy
                delete_result = GriptapeNodes.FlowManager().on_delete_connection_request(
                    DeleteConnectionRequest(
                        source_parameter_name=parameter_name,
                        target_parameter_name=proxy_parameter.name,
                        source_node_name=node.name,
                        target_node_name=self.name,
                    )
                )
                if delete_result.failed():
                    msg = f"{self.name}: Failed to delete internal outgoing connection from {node.name}.{parameter_name} to proxy {proxy_parameter.name}: {delete_result.result_details}"
                    raise RuntimeError(msg)

                # Create direct connections from this node to target nodes
                for connection in remap_connections:
                    create_result = GriptapeNodes.FlowManager().on_create_connection_request(
                        CreateConnectionRequest(
                            source_parameter_name=parameter_name,
                            target_parameter_name=connection.target_parameter.name,
                            source_node_name=node.name,
                            target_node_name=connection.target_node.name,
                        )
                    )
                    if create_result.failed():
                        msg = f"{self.name}: Failed to create direct outgoing connection from {node.name}.{parameter_name} to {connection.target_node.name}.{connection.target_parameter.name}: {create_result.result_details}"
                        raise RuntimeError(msg)

                # Only delete outgoing connections from proxy and clean up if no other incoming connections exist
                if not other_incoming_exists:
                    for connection in remap_connections:
                        delete_result = GriptapeNodes.FlowManager().on_delete_connection_request(
                            DeleteConnectionRequest(
                                source_parameter_name=connection.source_parameter.name,
                                target_parameter_name=connection.target_parameter.name,
                                source_node_name=connection.source_node.name,
                                target_node_name=connection.target_node.name,
                            )
                        )
                        if delete_result.failed():
                            msg = f"{self.name}: Failed to delete external connection from proxy {proxy_parameter.name} to {connection.target_node.name}.{connection.target_parameter.name}: {delete_result.result_details}"
                            raise RuntimeError(msg)

                    self._cleanup_proxy_parameter(proxy_parameter, RIGHT_PARAMETERS_KEY)

    def _remap_incoming_connections(self, node: BaseNode, connections: Connections) -> None:
        """Remap incoming connections that go through proxy parameters.

        Args:
            node: The node being added to the group
            connections: Connections object from FlowManager
        """
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        incoming_connections = connections.get_incoming_connections_from_node(node, from_node=self)
        for parameter_name, incoming_connection_list in incoming_connections.items():
            for incoming_connection in incoming_connection_list:
                proxy_parameter = incoming_connection.source_parameter
                remap_connections = connections.get_incoming_connections_to_parameter(self, proxy_parameter)

                # Check if proxy has other outgoing connections besides this one
                # If so, we should keep the proxy and its incoming connections
                outgoing_from_proxy = connections.get_outgoing_connections_from_parameter(self, proxy_parameter)
                other_outgoing_exists = any(
                    conn.target_node.name != node.name or conn.target_parameter.name != parameter_name
                    for conn in outgoing_from_proxy
                )

                # Delete the connection from proxy to this node
                delete_result = GriptapeNodes.FlowManager().on_delete_connection_request(
                    DeleteConnectionRequest(
                        source_parameter_name=proxy_parameter.name,
                        target_parameter_name=parameter_name,
                        source_node_name=self.name,
                        target_node_name=node.name,
                    )
                )
                if delete_result.failed():
                    msg = f"{self.name}: Failed to delete internal incoming connection from proxy {proxy_parameter.name} to {node.name}.{parameter_name}: {delete_result.result_details}"
                    raise RuntimeError(msg)

                # Create direct connections from source nodes to this node
                for connection in remap_connections:
                    create_result = GriptapeNodes.FlowManager().on_create_connection_request(
                        CreateConnectionRequest(
                            source_parameter_name=connection.source_parameter.name,
                            target_parameter_name=parameter_name,
                            source_node_name=connection.source_node.name,
                            target_node_name=node.name,
                        )
                    )
                    if create_result.failed():
                        msg = f"{self.name}: Failed to create direct incoming connection from {connection.source_node.name}.{connection.source_parameter.name} to {node.name}.{parameter_name}: {create_result.result_details}"
                        raise RuntimeError(msg)

                # Only delete incoming connections to proxy and clean up if no other outgoing connections exist
                if not other_outgoing_exists:
                    for connection in remap_connections:
                        delete_result = GriptapeNodes.FlowManager().on_delete_connection_request(
                            DeleteConnectionRequest(
                                source_parameter_name=connection.source_parameter.name,
                                target_parameter_name=proxy_parameter.name,
                                source_node_name=connection.source_node.name,
                                target_node_name=self.name,
                            )
                        )
                        if delete_result.failed():
                            msg = f"{self.name}: Failed to delete external connection from {connection.source_node.name}.{connection.source_parameter.name} to proxy {proxy_parameter.name}: {delete_result.result_details}"
                            raise RuntimeError(msg)

                    self._cleanup_proxy_parameter(proxy_parameter, LEFT_PARAMETERS_KEY)

    def remap_to_internal(self, nodes: list[BaseNode], connections: Connections) -> None:
        """Remap connections that are now internal after adding nodes to the group.

        When nodes are added to a group, some connections that previously went through
        proxy parameters may now be internal. This method identifies such connections
        and restores direct connections between the nodes.

        Args:
            nodes: List of nodes being added to the group
            connections: Connections object from FlowManager
        """
        for node in nodes:
            self._remap_outgoing_connections(node, connections)
            self._remap_incoming_connections(node, connections)

    def after_outgoing_connection_removed(
        self, source_parameter: Parameter, target_node: BaseNode, target_parameter: Parameter
    ) -> None:
        # Instead of right_parameters, we should check the internal connections
        if target_node.parent_group == self:
            metadata_key = LEFT_PARAMETERS_KEY
        else:
            metadata_key = RIGHT_PARAMETERS_KEY
        self._cleanup_proxy_parameter(source_parameter, metadata_key)
        return super().after_outgoing_connection_removed(source_parameter, target_node, target_parameter)

    def after_incoming_connection_removed(
        self, source_node: BaseNode, source_parameter: Parameter, target_parameter: Parameter
    ) -> None:
        # Instead of left_parameters, we should check the internal connections.
        if source_node.parent_group == self:
            metadata_key = RIGHT_PARAMETERS_KEY
        else:
            metadata_key = LEFT_PARAMETERS_KEY
        self._cleanup_proxy_parameter(target_parameter, metadata_key)
        return super().after_incoming_connection_removed(source_node, source_parameter, target_parameter)

    def after_value_set(self, parameter: Parameter, value: Any) -> None:
        super().after_value_set(parameter, value)
        self.subflow_execution_component.after_value_set(parameter, value)

    def add_nodes_to_group(self, nodes: list[BaseNode]) -> None:
        """Add nodes to the group and track their connections.

        Args:
            nodes: List of nodes to add to the group
        """
        from griptape_nodes.retained_mode.events.node_events import (
            MoveNodeToNewFlowRequest,
            MoveNodeToNewFlowResultSuccess,
        )
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        self._remove_nodes_from_existing_parents(nodes)
        self._add_nodes_to_group_dict(nodes)

        # Create subflow on-demand if it doesn't exist
        subflow_name = self.metadata.get("subflow_name")
        if subflow_name is None:
            self._create_subflow()
            subflow_name = self.metadata.get("subflow_name")

        if subflow_name is not None:
            for node in nodes:
                move_request = MoveNodeToNewFlowRequest(node_name=node.name, target_flow_name=subflow_name)
                move_result = GriptapeNodes.handle_request(move_request)
                if not isinstance(move_result, MoveNodeToNewFlowResultSuccess):
                    msg = "%s failed to move node '%s' to subflow: %s", self.name, node.name, move_result.result_details
                    logger.error(msg)
                    raise RuntimeError(msg)  # noqa: TRY004

        connections = GriptapeNodes.FlowManager().get_connections()
        node_names_in_group = set(self.nodes.keys())
        self.metadata["node_names_in_group"] = list(node_names_in_group)
        self.remap_to_internal(nodes, connections)
        self._map_external_connections_for_nodes(nodes, connections, node_names_in_group)

    def _map_external_connections_for_nodes(
        self, nodes: list[BaseNode], connections: Connections, node_names_in_group: set[str]
    ) -> None:
        """Map external connections for nodes being added to the group.

        Args:
            nodes: List of nodes being added
            connections: Connections object from FlowManager
            node_names_in_group: Set of all node names currently in the group
        """
        # Group outgoing connections by (source_node, source_parameter) to reuse proxy parameters
        # Skip connections that already go to the NodeGroup itself (existing proxy parameters)
        outgoing_by_source: dict[tuple[str, str], list[Connection]] = {}
        for node in nodes:
            outgoing_connections = connections.get_all_outgoing_connections(node)
            for conn in outgoing_connections:
                if conn.target_node.name not in node_names_in_group and conn.target_node.name != self.name:
                    key = (conn.source_node.name, conn.source_parameter.name)
                    outgoing_by_source.setdefault(key, []).append(conn)

        # Group incoming connections by (source_node, source_parameter) to reuse proxy parameters
        # This ensures that when an external node connects to multiple internal nodes,
        # they share a single proxy parameter
        # Skip connections that already come from the NodeGroup itself (existing proxy parameters)
        incoming_by_source: dict[tuple[str, str], list[Connection]] = {}
        for node in nodes:
            incoming_connections = connections.get_all_incoming_connections(node)
            for conn in incoming_connections:
                if conn.source_node.name not in node_names_in_group and conn.source_node.name != self.name:
                    key = (conn.source_node.name, conn.source_parameter.name)
                    incoming_by_source.setdefault(key, []).append(conn)

        # Map outgoing connections - one proxy parameter per source parameter
        for conn_list in outgoing_by_source.values():
            self._map_external_connections_group(conn_list, is_incoming=False)

        # Map incoming connections - one proxy parameter per source parameter
        for conn_list in incoming_by_source.values():
            self._map_external_connections_group(conn_list, is_incoming=True)

    def _map_external_connections_group(self, conn_list: list[Connection], *, is_incoming: bool) -> None:
        """Map a group of external connections that share the same external parameter.

        Creates a single proxy parameter and connects all nodes through it.
        If an existing proxy parameter already handles the same internal source,
        it will be reused instead of creating a new one.

        Args:
            conn_list: List of connections sharing the same external parameter
            is_incoming: True if these are incoming connections to the group
        """
        if not conn_list:
            return

        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        # All connections share the same external parameter
        # For outgoing: the internal (source) parameter is shared
        # For incoming: the external (source) parameter is shared
        first_conn = conn_list[0]
        # Use source_parameter in both cases since we group by source
        grouped_parameter = first_conn.source_parameter

        # Check if there's an existing proxy parameter we can reuse
        existing_proxy = self._find_existing_proxy_for_source(
            first_conn.source_node, first_conn.source_parameter, is_incoming=is_incoming
        )

        # Delete all original connections first
        for conn in conn_list:
            request = DeleteConnectionRequest(
                conn.source_parameter.name,
                conn.target_parameter.name,
                conn.source_node.name,
                conn.target_node.name,
            )
            result = GriptapeNodes.handle_request(request)
            if not isinstance(result, DeleteConnectionResultSuccess):
                logger.warning(
                    "%s failed to delete connection from %s.%s to %s.%s",
                    self.name,
                    conn.source_node.name,
                    conn.source_parameter.name,
                    conn.target_node.name,
                    conn.target_parameter.name,
                )

        # Use existing proxy or create a new one
        if existing_proxy is not None:
            proxy_parameter = existing_proxy
        else:
            proxy_parameter = self._create_proxy_parameter_for_connection(grouped_parameter, is_incoming=is_incoming)

        # Create connections for all external nodes through the single proxy
        for conn in conn_list:
            self._create_connections_for_proxy_single(proxy_parameter, conn, is_incoming=is_incoming)

    def _find_existing_proxy_for_source(
        self, source_node: BaseNode, source_parameter: Parameter, *, is_incoming: bool
    ) -> Parameter | None:
        """Find an existing proxy parameter that already handles the given source.

        For outgoing connections (is_incoming=False):
            Looks for a right-side proxy that has an incoming connection from the
            same internal source node/parameter.

        For incoming connections (is_incoming=True):
            Looks for a left-side proxy that has an incoming connection from the
            same external source node/parameter.

        Args:
            source_node: The source node of the connection
            source_parameter: The source parameter of the connection
            is_incoming: True if looking for incoming connection proxies

        Returns:
            The existing proxy parameter if found, None otherwise
        """
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        connections = GriptapeNodes.FlowManager().get_connections()

        # Determine which proxy parameters to check based on direction
        if is_incoming:
            proxy_param_names = self.metadata.get(LEFT_PARAMETERS_KEY, [])
        else:
            proxy_param_names = self.metadata.get(RIGHT_PARAMETERS_KEY, [])

        for proxy_name in proxy_param_names:
            proxy_param = self.get_parameter_by_name(proxy_name)
            if proxy_param is None:
                continue

            # Check incoming connections to this proxy parameter
            incoming_to_proxy = connections.get_incoming_connections_to_parameter(self, proxy_param)
            for conn in incoming_to_proxy:
                if conn.source_node.name == source_node.name and conn.source_parameter.name == source_parameter.name:
                    return proxy_param

        return None

    def _create_connections_for_proxy_single(
        self, proxy_parameter: Parameter, old_connection: Connection, *, is_incoming: bool
    ) -> None:
        """Create connections for a single external connection through a proxy parameter.

        Unlike create_connections_for_proxy, this assumes the proxy parameter already exists
        and is being shared by multiple connections.

        Args:
            proxy_parameter: The proxy parameter to connect through
            old_connection: The original connection being remapped
            is_incoming: True if this is an incoming connection to the group
        """
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        create_first_connection = CreateConnectionRequest(
            source_parameter_name=old_connection.source_parameter.name,
            target_parameter_name=proxy_parameter.name,
            source_node_name=old_connection.source_node.name,
            target_node_name=self.name,
            is_node_group_internal=not is_incoming,
        )
        create_second_connection = CreateConnectionRequest(
            source_parameter_name=proxy_parameter.name,
            target_parameter_name=old_connection.target_parameter.name,
            source_node_name=self.name,
            target_node_name=old_connection.target_node.name,
            is_node_group_internal=is_incoming,
        )

        # Track connections for cleanup
        if proxy_parameter.name not in self._proxy_param_to_connections:
            self._proxy_param_to_connections[proxy_parameter.name] = 2
        else:
            self._proxy_param_to_connections[proxy_parameter.name] += 2

        GriptapeNodes.handle_request(create_first_connection)
        GriptapeNodes.handle_request(create_second_connection)

    def delete_nodes_from_group(self, nodes: list[BaseNode]) -> None:
        """Delete nodes from the group and untrack their connections.

        Args:
            nodes: List of nodes to delete from the group
        """
        for node in nodes:
            self.nodes.pop(node.name)
        self.metadata["node_names_in_group"] = list(self.nodes.keys())

    def remove_nodes_from_group(self, nodes: list[BaseNode]) -> None:
        """Remove nodes from the group and untrack their connections.

        Args:
            nodes: List of nodes to remove from the group
        """
        from griptape_nodes.retained_mode.events.node_events import (
            MoveNodeToNewFlowRequest,
            MoveNodeToNewFlowResultSuccess,
        )
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        self._validate_nodes_in_group(nodes)

        parent_flow_name = None
        try:
            parent_flow_name = GriptapeNodes.NodeManager().get_node_parent_flow_by_name(self.name)
        except KeyError:
            logger.warning("%s has no parent flow, cannot move nodes back", self.name)

        connections = GriptapeNodes.FlowManager().get_connections()
        for node in nodes:
            node.parent_group = None
            self.nodes.pop(node.name)

            if parent_flow_name is not None:
                move_request = MoveNodeToNewFlowRequest(node_name=node.name, target_flow_name=parent_flow_name)
                move_result = GriptapeNodes.handle_request(move_request)
                if not isinstance(move_result, MoveNodeToNewFlowResultSuccess):
                    msg = (
                        "%s failed to move node '%s' back to parent flow: %s",
                        self.name,
                        node.name,
                        move_result.result_details,
                    )
                    logger.error(msg)
                    raise RuntimeError(msg)

        for node in nodes:
            self.unmap_node_connections(node, connections)

        self.metadata["node_names_in_group"] = list(self.nodes.keys())

        remaining_nodes = list(self.nodes.values())
        if remaining_nodes:
            node_names_in_group = set(self.nodes.keys())
            self._map_external_connections_for_nodes(remaining_nodes, connections, node_names_in_group)

    async def execute_subflow(self) -> None:
        """Execute the subflow and propagate output values.

        This helper method:
        1. Starts the local subflow execution
        2. Collects output values from internal nodes
        3. Sets them on the NodeGroup's output (right) proxy parameters

        Can be called by concrete subclasses in their aprocess() implementation.
        """
        from griptape_nodes.retained_mode.events.execution_events import (
            StartLocalSubflowRequest,
            StartLocalSubflowResultFailure,
        )
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        subflow = self.metadata.get("subflow_name")
        if subflow is not None and isinstance(subflow, str):
            result = await GriptapeNodes.FlowManager().on_start_local_subflow_request(
                StartLocalSubflowRequest(flow_name=subflow)
            )

            if isinstance(result, StartLocalSubflowResultFailure):
                logger.error("%s: %s", self.name, result.result_details)
                # Clear partial outputs to prevent inconsistent state
                self.parameter_output_values.clear()
                # Re-raise the error message directly without wrapping
                msg = result.result_details
                raise RuntimeError(msg)

        self._propagate_output_values_from_internal_nodes()

    def _propagate_output_values_from_internal_nodes(self) -> None:
        """Collect output values from internal nodes and set them on proxy output parameters.

        For each right (output) proxy parameter, finds the internal node connected
        to it and copies the value to this group's parameter_output_values.
        """
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        connections = GriptapeNodes.FlowManager().get_connections()

        right_params = self.metadata.get(RIGHT_PARAMETERS_KEY, [])
        for proxy_param_name in right_params:
            proxy_param = self.get_parameter_by_name(proxy_param_name)
            if proxy_param is None:
                continue

            incoming_connections = connections.get_incoming_connections_to_parameter(self, proxy_param)
            if not incoming_connections:
                continue

            for connection in incoming_connections:
                if not connection.is_node_group_internal:
                    continue

                internal_node = connection.source_node
                internal_param = connection.source_parameter

                if internal_param.name in internal_node.parameter_output_values:
                    value = internal_node.parameter_output_values[internal_param.name]
                else:
                    value = internal_node.get_parameter_value(internal_param.name)

                if value is not None:
                    self.parameter_output_values[proxy_param_name] = value
                break

    @abstractmethod
    async def aprocess(self) -> None:
        """Execute all nodes in the group.

        Must be implemented by concrete subclasses to define execution behavior.
        """

    def process(self) -> Any:
        """Synchronous process method - not used for proxy nodes."""

    def after_node_deleted(self) -> None:
        nodes_to_remove = list(self.nodes.values())
        self.remove_nodes_from_group(nodes_to_remove)
        subflow_name = self.metadata.get("subflow_name")
        if subflow_name is not None:
            subflow = GriptapeNodes.ObjectManager().attempt_get_object_by_name_as_type(subflow_name, ControlFlow)
            if subflow is not None:
                delete_result = GriptapeNodes.handle_request(DeleteFlowRequest(flow_name=subflow_name))
                if isinstance(delete_result, DeleteFlowResultFailure):
                    # This will propagate up to DeleteNodeRequest, and prevent the node from deleting.
                    msg = f"Failed to delete subflow {subflow_name} when deleting node {self.name}"
                    raise ValueError(msg)
            else:
                msg = f"Node {self.name} has a subflow name of {subflow_name} but {subflow_name} doesn't exist. Removing from metadata."
                logger.warning(msg)
            # Delete the subflow name since now there is no subflow attached.
            self.metadata.pop("subflow_name")

    @property
    def subflow_execution_component(self) -> SubflowExecutionComponent:
        """Get the subflow execution component for real-time status updates."""
        return self._subflow_execution_component
