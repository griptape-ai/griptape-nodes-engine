from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, NamedTuple
from uuid import uuid4

from griptape_nodes.exe_types.core_types import (
    Parameter,
    ParameterMode,
    ParameterTypeBuiltin,
)
from griptape_nodes.exe_types.node_groups import BaseNodeGroup, SubflowNodeGroup
from griptape_nodes.exe_types.node_types import (
    BaseNode,
    NodeDependencies,
)
from griptape_nodes.node_library.library_registry import LibraryNameAndVersion, LibraryRegistry
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.connection_events import (
    IncomingConnection,
    ListConnectionsForNodeRequest,
    ListConnectionsForNodeResultSuccess,
    OutgoingConnection,
)
from griptape_nodes.retained_mode.events.flow_events import (
    PACKAGED_FLOW_BOUNDARY_KEY,
    TRANSIENT_KEY,
    CreateFlowRequest,
    OriginalNodeParameter,
    PackageNodesAsSerializedFlowRequest,
    PackageNodesAsSerializedFlowResultFailure,
    PackageNodesAsSerializedFlowResultSuccess,
    SanitizedParameterName,
    SerializedFlowCommands,
)
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    SerializedNodeCommands,
    SerializedParameterValueTracker,
    SerializeNodeToCommandsRequest,
    SerializeNodeToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.parameter_events import (
    AddParameterToNodeRequest,
    AlterParameterDetailsRequest,
    SetParameterValueRequest,
)
from griptape_nodes.retained_mode.managers.workflow.shape import (
    build_workflow_shape_from_parameter_info,
    extract_parameter_shape_info,
)
from griptape_nodes.retained_mode.request_handlers import handles
from griptape_nodes.serialization.values import Unencodable, encodable_default, try_encode, value_key

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager
    from griptape_nodes.retained_mode.managers.workflow.shape import WorkflowShapeNodes

logger = logging.getLogger("griptape_nodes")


class ConnectionAnalysis(NamedTuple):
    """Analysis of connections separated by type (data vs control) when packaging nodes."""

    incoming_data_connections: list[IncomingConnection]
    incoming_control_connections: list[IncomingConnection]
    outgoing_data_connections: list[OutgoingConnection]
    outgoing_control_connections: list[OutgoingConnection]


class PackageNodeInfo(NamedTuple):
    """Information about the node being packaged."""

    package_node: BaseNode
    package_flow_name: str


class StartNodeIncomingDataResult(NamedTuple):
    """Result of processing incoming data connections for a start node."""

    parameter_commands: list[AddParameterToNodeRequest]
    data_connections: list[SerializedFlowCommands.IndirectConnectionSerialization]
    input_shape_data: WorkflowShapeNodes
    parameter_value_commands: list[SerializedNodeCommands.IndirectSetParameterValueCommand]


class PackagingStartNodeResult(NamedTuple):
    """Result of creating start node commands and connections for flow packaging."""

    start_node_commands: SerializedNodeCommands
    start_to_package_connections: list[SerializedFlowCommands.IndirectConnectionSerialization]
    input_shape_data: WorkflowShapeNodes
    start_node_parameter_value_commands: list[SerializedNodeCommands.IndirectSetParameterValueCommand]
    parameter_name_mappings: dict[SanitizedParameterName, OriginalNodeParameter]
    start_node_name: str


class PackagingEndNodeResult(NamedTuple):
    """Result of creating end node commands and data connections for flow packaging."""

    end_node_commands: SerializedNodeCommands
    package_to_end_connections: list[SerializedFlowCommands.IndirectConnectionSerialization]
    output_shape_data: WorkflowShapeNodes


class MultiNodeEndNodeResult(NamedTuple):
    """Result of creating end node commands and parameter mappings for multi-node packaging."""

    packaging_result: PackagingEndNodeResult
    parameter_name_mappings: dict[SanitizedParameterName, OriginalNodeParameter]
    alter_parameter_commands: list[AlterParameterDetailsRequest]
    end_node_name: str


class FlowPackaging(EngineScoped):
    """Package nodes into a serialized flow that runs them elsewhere, wrapped in Start and End nodes."""

    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        event_manager.register_request_handlers(self)

    @handles(PackageNodesAsSerializedFlowRequest)
    def on_package_nodes_as_serialized_flow_request(  # noqa: C901, PLR0911, PLR0912, PLR0915
        self, request: PackageNodesAsSerializedFlowRequest
    ) -> ResultPayload:
        """Handle request to package multiple nodes as a serialized flow.

        Creates a self-contained flow with Start → [Selected Nodes] → End structure,
        where artificial start/end nodes interface with external connections only.
        """
        # Step 0: Apply defaults for None values
        if request.start_node_type is None:
            request.start_node_type = "StartFlow"
        if request.end_node_type is None:
            request.end_node_type = "EndFlow"

        # Step 1: Reject empty node list
        if not request.node_names:
            return PackageNodesAsSerializedFlowResultFailure(
                result_details="Attempted to package nodes as serialized flow. Failed because no nodes were specified in the node_names list."
            )

        # Step 2: Validate library and get version
        library_version = self._validate_and_get_multi_node_library_info(request=request)
        if isinstance(library_version, PackageNodesAsSerializedFlowResultFailure):
            return library_version

        # Step 3: Validate all nodes exist
        validation_result = self._validate_multi_node_request(request)
        if validation_result is not None:
            return validation_result

        # Get the actual node objects for processing
        nodes_to_package = []
        for node_name in request.node_names:
            node = self.engine.node_manager.get_node_by_name(node_name)
            nodes_to_package.append(node)

        # Step 4: Initialize tracking variables and mappings (moved up so package node serialization can use them)
        unique_parameter_uuid_to_values = {}
        serialized_parameter_value_tracker = SerializedParameterValueTracker()
        node_name_to_uuid: dict[str, SerializedNodeCommands.NodeUUID] = {}
        packaged_nodes_set_parameter_value_commands: dict[
            SerializedNodeCommands.NodeUUID, list[SerializedNodeCommands.IndirectSetParameterValueCommand]
        ] = {}
        packaged_nodes_internal_connections: list[SerializedFlowCommands.IndirectConnectionSerialization] = []

        # Step 5: Serialize nodes with local execution environment to prevent recursive loops
        serialized_package_nodes = self._serialize_package_nodes_for_local_execution(
            nodes_to_package=nodes_to_package,
            unique_parameter_uuid_to_values=unique_parameter_uuid_to_values,
            serialized_parameter_value_tracker=serialized_parameter_value_tracker,
            node_name_to_uuid=node_name_to_uuid,
            set_parameter_value_commands=packaged_nodes_set_parameter_value_commands,
            internal_connections=packaged_nodes_internal_connections,
        )
        if isinstance(serialized_package_nodes, PackageNodesAsSerializedFlowResultFailure):
            return serialized_package_nodes

        # Step 6: Inject OUTPUT mode changes for PROPERTY-only parameters to enable value reconciliation after the
        # packaged workflow is run.
        # Example: Nodes A -> B -> C. If B has property-only parameters, those values may change during execution,
        # so we need to send the value back after the packaged flow has run. We do this by making connections from
        # B's property-only parameters to the End Node, ensuring they're reflected when the packaged flow returns.
        # Since connections require an OUTPUT parameter mode, we inject that here.
        self._inject_output_mode_for_property_parameters(nodes_to_package, serialized_package_nodes)

        # Step 7: Analyze external connections (connections from/to nodes outside our selection)
        node_connections_dict = self._analyze_multi_node_external_connections(package_nodes=nodes_to_package)
        if isinstance(node_connections_dict, PackageNodesAsSerializedFlowResultFailure):
            return node_connections_dict

        # Step 8: Retrieve SubflowNodeGroup if node_group_name was provided
        node_group_node: SubflowNodeGroup | None = None
        if request.node_group_name:
            try:
                node = self.engine.node_manager.get_node_by_name(request.node_group_name)
                if isinstance(node, SubflowNodeGroup):
                    node_group_node = node
            except Exception as e:
                logger.debug("Failed to retrieve SubflowNodeGroup '%s': %s", request.node_group_name, e)

        # Step 9: Create start node with parameters for external incoming connections
        start_node_result = self._create_multi_node_start_node_with_connections(
            request=request,
            library_version=library_version,
            unique_parameter_uuid_to_values=unique_parameter_uuid_to_values,
            serialized_parameter_value_tracker=serialized_parameter_value_tracker,
            node_name_to_uuid=node_name_to_uuid,
            external_connections_dict=node_connections_dict,
            node_group_node=node_group_node,
        )
        if isinstance(start_node_result, PackageNodesAsSerializedFlowResultFailure):
            return start_node_result

        # Step 10: Create end node with parameters for external outgoing connections and parameter mappings
        end_node_result = self._create_multi_node_end_node_with_connections(
            request=request,
            package_nodes=nodes_to_package,
            node_name_to_uuid=node_name_to_uuid,
            library_version=library_version,
            node_connections_dict=node_connections_dict,
        )
        if isinstance(end_node_result, PackageNodesAsSerializedFlowResultFailure):
            return end_node_result

        end_node_packaging_result = end_node_result.packaging_result

        # If no entry control node specified, connect start directly to end
        if not request.entry_control_node_name and not request.entry_control_parameter_name:
            start_to_end_control_connection = SerializedFlowCommands.IndirectConnectionSerialization(
                source_node_uuid=start_node_result.start_node_commands.node_uuid,
                source_parameter_name="exec_out",
                target_node_uuid=end_node_packaging_result.end_node_commands.node_uuid,
                target_parameter_name="exec_in",
            )
            start_node_result.start_to_package_connections.append(start_to_end_control_connection)

        # Combine parameter mappings as a list: [Start node (index 0), End node (index 1)]
        from griptape_nodes.retained_mode.events.flow_events import PackagedNodeParameterMapping

        parameter_name_mappings = [
            PackagedNodeParameterMapping(
                node_name=start_node_result.start_node_name,
                parameter_mappings=start_node_result.parameter_name_mappings,
            ),
            PackagedNodeParameterMapping(
                node_name=end_node_result.end_node_name,
                parameter_mappings=end_node_result.parameter_name_mappings,
            ),
        ]

        # Step 11: Assemble final SerializedFlowCommands
        # Collect all connections from start/end nodes and internal package connections
        all_connections = self._collect_all_connections_for_multi_node_package(
            start_node_result=start_node_result,
            end_node_packaging_result=end_node_packaging_result,
            packaged_nodes_internal_connections=packaged_nodes_internal_connections,
        )

        # Build WorkflowShape from collected parameter shape data
        workflow_shape = build_workflow_shape_from_parameter_info(
            input_node_params=start_node_result.input_shape_data,
            output_node_params=end_node_packaging_result.output_shape_data,
        )

        # Create set parameter value commands dict
        set_parameter_value_commands = {
            start_node_result.start_node_commands.node_uuid: start_node_result.start_node_parameter_value_commands,
            **packaged_nodes_set_parameter_value_commands,
        }

        # Collect all serialized nodes
        all_serialized_nodes = [
            start_node_result.start_node_commands,
            *serialized_package_nodes,
            end_node_packaging_result.end_node_commands,
        ]

        # Create comprehensive dependencies from all nodes
        combined_dependencies = NodeDependencies()
        for serialized_node in all_serialized_nodes:
            combined_dependencies.aggregate_from(serialized_node.node_dependencies)

        # Extract lock commands from serialized nodes (they're embedded in SerializedNodeCommands)
        set_lock_commands_per_node = {}
        for serialized_node in all_serialized_nodes:
            if serialized_node.lock_node_command:
                set_lock_commands_per_node[serialized_node.node_uuid] = serialized_node.lock_node_command

        # Create a CreateFlowRequest for the packaged flow so that it can
        # run as a standalone workflow.
        # Flag it transient: the iterative executor deserializes this packaged body into one child
        # flow per iteration, all parented to the running flow. They are recreated on every run and
        # torn down after, so they must never be serialized into a saved workflow (otherwise a save
        # bakes in a duplicate loop-body flow per iteration). The flow serializer skips transient
        # child flows; CreateFlowRequest.metadata is applied directly to the created ControlFlow.
        packaged_flow_metadata = {TRANSIENT_KEY: True}

        create_packaged_flow_request = CreateFlowRequest(
            parent_flow_name=None,  # Standalone flow
            set_as_new_context=False,  # Let deserializer decide
            metadata=packaged_flow_metadata,
        )

        # Aggregate node types used
        combined_node_types_used = self.engine.flow_manager.serialization._aggregate_node_types_used(
            serialized_node_commands=all_serialized_nodes, sub_flows_commands=[]
        )

        # Build the complete serialized flow
        final_serialized_flow = SerializedFlowCommands(
            flow_initialization_command=create_packaged_flow_request,
            serialized_node_commands=all_serialized_nodes,
            serialized_connections=all_connections,
            unique_parameter_uuid_to_values=unique_parameter_uuid_to_values,
            set_parameter_value_commands=set_parameter_value_commands,
            set_lock_commands_per_node=set_lock_commands_per_node,
            sub_flows_commands=[],
            node_dependencies=combined_dependencies,
            node_types_used=combined_node_types_used,
        )

        return PackageNodesAsSerializedFlowResultSuccess(
            serialized_flow_commands=final_serialized_flow,
            workflow_shape=workflow_shape,
            packaged_node_names=request.node_names,
            parameter_name_mappings=parameter_name_mappings,
            result_details=f"Successfully packaged {len(request.node_names)} nodes as serialized flow.",
        )

    def _validate_and_get_multi_node_library_info(
        self, request: PackageNodesAsSerializedFlowRequest
    ) -> str | PackageNodesAsSerializedFlowResultFailure:
        """Validate start/end node types exist in library and return library version."""
        # Early validation - ensure both start and end node types exist in the specified library
        try:
            start_end_library = LibraryRegistry.get_library_for_node_type(
                node_type=request.start_node_type,  # type: ignore[arg-type]  # Guaranteed non-None by handler
                specific_library_name=request.start_node_library_name,
            )
        except KeyError as err:
            details = f"Attempted to package nodes with start node type '{request.start_node_type}' from library '{request.start_node_library_name}'. Failed because start node type was not found in library. Error: {err}."
            return PackageNodesAsSerializedFlowResultFailure(result_details=details)

        try:
            LibraryRegistry.get_library_for_node_type(
                node_type=request.end_node_type,  # type: ignore[arg-type]  # Guaranteed non-None by handler
                specific_library_name=request.end_node_library_name,
            )
        except KeyError as err:
            details = f"Attempted to package nodes with end node type '{request.end_node_type}' from library '{request.end_node_library_name}'. Failed because end node type was not found in library. Error: {err}."
            return PackageNodesAsSerializedFlowResultFailure(result_details=details)

        # Get the actual library version
        start_end_library_metadata = start_end_library.get_metadata()
        return start_end_library_metadata.library_version

    def _validate_multi_node_request(
        self, request: PackageNodesAsSerializedFlowRequest
    ) -> PackageNodesAsSerializedFlowResultFailure | None:
        """Validate that all requested nodes exist and control flow configuration is valid."""
        # Validate all nodes exist
        missing_nodes = []
        for node_name in request.node_names:
            try:
                self.engine.node_manager.get_node_by_name(node_name)
            except Exception:
                missing_nodes.append(node_name)

        if missing_nodes:
            return PackageNodesAsSerializedFlowResultFailure(
                result_details=f"Attempted to package nodes as serialized flow. Failed because the following nodes were not found: {', '.join(missing_nodes)}."
            )

        # Validate control flow configuration for non-empty node lists
        if request.node_names and request.entry_control_parameter_name and not request.entry_control_node_name:
            return PackageNodesAsSerializedFlowResultFailure(
                result_details="Attempted to package nodes as serialized flow. Failed because entry_control_parameter_name was specified but entry_control_node_name was not. For multi-node packaging with a non-empty node list, both must be specified to avoid ambiguity about which node should receive the control connection."
            )

        # Validate entry_control_node_name exists and is in our package list
        if request.entry_control_node_name and request.entry_control_node_name not in request.node_names:
            return PackageNodesAsSerializedFlowResultFailure(
                result_details=f"Attempted to package nodes as serialized flow. Failed because entry_control_node_name '{request.entry_control_node_name}' is not in the list of nodes to package: {request.node_names}."
            )

        return None

    def _serialize_package_nodes_for_local_execution(  # noqa: C901, PLR0913, PLR0917
        self,
        nodes_to_package: list[BaseNode],
        unique_parameter_uuid_to_values: dict[SerializedNodeCommands.UniqueParameterValueUUID, Any],
        serialized_parameter_value_tracker: SerializedParameterValueTracker,
        node_name_to_uuid: dict[str, SerializedNodeCommands.NodeUUID],  # OUTPUT: will be populated
        set_parameter_value_commands: dict[  # OUTPUT: will be populated
            SerializedNodeCommands.NodeUUID, list[SerializedNodeCommands.IndirectSetParameterValueCommand]
        ],
        internal_connections: list[SerializedFlowCommands.IndirectConnectionSerialization],  # OUTPUT: will be populated
    ) -> list[SerializedNodeCommands] | PackageNodesAsSerializedFlowResultFailure:
        """Serialize package nodes while temporarily setting execution environment to local to prevent recursive loops.

        Args:
            nodes_to_package: List of nodes to serialize
            unique_parameter_uuid_to_values: Shared dictionary for deduplicating parameter values across all nodes
            serialized_parameter_value_tracker: Tracker for serialized parameter values
            node_name_to_uuid: OUTPUT - Dictionary mapping node names to UUIDs (populated by this method)
            set_parameter_value_commands: OUTPUT - Dict mapping node UUIDs to parameter value commands (populated by this method)
            internal_connections: OUTPUT - List of connections between package nodes (populated by this method)

        Returns:
            List of SerializedNodeCommands on success, or PackageNodesAsSerializedFlowResultFailure on failure
        """
        # Serialize each node using shared unique_parameter_uuid_to_values dictionary for deduplication
        serialized_node_commands = []
        serialized_node_group_commands = []  # SubflowNodeGroups must be added LAST

        for node in nodes_to_package:
            # Serialize this node using shared dictionaries for value deduplication
            serialize_request = SerializeNodeToCommandsRequest(
                node_name=node.name,
                unique_parameter_uuid_to_values=unique_parameter_uuid_to_values,
                serialized_parameter_value_tracker=serialized_parameter_value_tracker,
                include_existing_subflow_in_group=True,
                values_supplied_by_connections=self._package_parameters_fed_by_connections(node),
            )
            serialize_result = self.engine.node_manager.on_serialize_node_to_commands(serialize_request)

            if not isinstance(serialize_result, SerializeNodeToCommandsResultSuccess):
                return PackageNodesAsSerializedFlowResultFailure(
                    result_details=f"Attempted to package nodes as serialized flow. Failed to serialize node '{node.name}': {serialize_result.result_details}"
                )

            # Populate the shared node_name_to_uuid mapping
            create_cmd = serialize_result.serialized_node_commands.create_node_command
            # Get the node name from the CreateNodeRequest command.
            node_name = create_cmd.node_name
            if node_name is not None:
                node_name_to_uuid[node_name] = serialize_result.serialized_node_commands.node_uuid

            # BaseNodeGroups must be serialized LAST because they reference child node names via node_names_to_add
            # If we deserialize a NodeGroup before its children, the child nodes won't exist yet
            if isinstance(node, BaseNodeGroup):
                serialized_node_group_commands.append(serialize_result.serialized_node_commands)
            else:
                serialized_node_commands.append(serialize_result.serialized_node_commands)

            # Collect set parameter value commands (references to unique_parameter_uuid_to_values)
            if serialize_result.set_parameter_value_commands:
                set_parameter_value_commands[serialize_result.serialized_node_commands.node_uuid] = (
                    serialize_result.set_parameter_value_commands
                )

        # Update BaseNodeGroup commands to use UUIDs instead of names in node_names_to_add
        # This allows workflow generation to directly look up variable names from UUIDs

        for node_group_command in serialized_node_group_commands:
            create_cmd = node_group_command.create_node_command

            if create_cmd.node_names_to_add:
                node_uuids = []
                for child_node_name in create_cmd.node_names_to_add:
                    if child_node_name in node_name_to_uuid:
                        uuid = node_name_to_uuid[child_node_name]
                        node_uuids.append(uuid)
                # Replace the list with UUIDs (as strings since that's what the field expects)
                create_cmd.node_names_to_add = node_uuids

        # Build internal connections between package nodes
        package_node_names_set = {n.name for n in nodes_to_package}

        # Get connections from the connection manager
        connections_result = self._get_internal_connections_for_package(
            nodes_to_package=nodes_to_package,
            package_node_names_set=package_node_names_set,
            node_name_to_uuid=node_name_to_uuid,
        )

        if isinstance(connections_result, PackageNodesAsSerializedFlowResultFailure):
            return connections_result

        internal_connections.extend(connections_result)
        serialized_node_commands.extend(serialized_node_group_commands)
        return serialized_node_commands

    def _package_parameters_fed_by_connections(self, node: BaseNode) -> set[str]:
        """Names of the node's parameters whose values the packaged flow's connections will supply.

        A connected input gets its value inside the rebuilt flow before its node runs: from the Start
        node when the connection comes from outside the package, from the upstream node's output when
        it is internal. The value the canvas node holds is overwritten either way, so the package
        leaves it out rather than replaying it, where it would be validated against the parameter's
        current rules and could abort the whole rebuild (issue #5889).

        A locked node is the exception: its copy never runs (the paths that rebuild the package from
        a workflow file lock it too), so nothing would feed its inputs, and a downstream node reading
        a passthrough parameter would see the default instead of the frozen value. It keeps them all.
        """
        if node.lock:
            return set()
        return set(self.engine.flow_manager.get_connections().incoming_index.get(node.name, {}))

    def _get_internal_connections_for_package(
        self,
        nodes_to_package: list[BaseNode],
        package_node_names_set: set[str],
        node_name_to_uuid: dict[str, SerializedNodeCommands.NodeUUID],
    ) -> list[SerializedFlowCommands.IndirectConnectionSerialization] | PackageNodesAsSerializedFlowResultFailure:
        """Get internal connections between package nodes.

        Queries connections from the connection manager for all nodes being packaged.

        Args:
            nodes_to_package: List of nodes being packaged
            package_node_names_set: Set of node names in the package for O(1) lookup
            node_name_to_uuid: Mapping of node names to their UUIDs in the serialization

        Returns:
            List of serialized connections, or PackageNodesAsSerializedFlowResultFailure on error
        """
        internal_connections: list[SerializedFlowCommands.IndirectConnectionSerialization] = []
        # Query connections from connection manager
        for node in nodes_to_package:
            list_connections_request = ListConnectionsForNodeRequest(node_name=node.name)
            list_connections_result = self.engine.node_manager.on_list_connections_for_node_request(
                list_connections_request
            )

            if not isinstance(list_connections_result, ListConnectionsForNodeResultSuccess):
                return PackageNodesAsSerializedFlowResultFailure(
                    result_details=f"Attempted to package nodes as serialized flow. Failed to list connections for node '{node.name}': {list_connections_result.result_details}"
                )

            # Only include connections where BOTH source and target are in the package
            for outgoing_conn in list_connections_result.outgoing_connections:
                if outgoing_conn.target_node_name in package_node_names_set:
                    source_uuid = node_name_to_uuid[node.name]
                    target_uuid = node_name_to_uuid[outgoing_conn.target_node_name]
                    internal_connections.append(
                        SerializedFlowCommands.IndirectConnectionSerialization(
                            source_node_uuid=source_uuid,
                            source_parameter_name=outgoing_conn.source_parameter_name,
                            target_node_uuid=target_uuid,
                            target_parameter_name=outgoing_conn.target_parameter_name,
                        )
                    )

        return internal_connections

    def _inject_output_mode_for_property_parameters(
        self, nodes_to_package: list[BaseNode], serialized_package_nodes: list[SerializedNodeCommands]
    ) -> None:
        """Inject OUTPUT mode for PROPERTY-only parameters to enable value reconciliation.

        This method adds ALTER parameter commands to serialized nodes for parameters that have
        PROPERTY mode but not OUTPUT mode. This allows the orchestrator/caller to reconcile
        the packaged node's values after execution.

        Args:
            nodes_to_package: List of nodes being packaged
            serialized_package_nodes: The serialized node commands to modify
        """
        # Apply ALTER parameter commands for PROPERTY-only parameters to enable OUTPUT mode
        # We need these to emit their values back so that the orchestrator/caller
        # can reconcile the packaged node's values after it is executed.
        for package_node in nodes_to_package:
            # Find the corresponding serialized node
            serialized_node = None
            for serialized_node_command in serialized_package_nodes:
                # We need to get the create commoand.
                create_cmd = serialized_node_command.create_node_command
                # Get the node name from CreateNodeRequest
                cmd_node_name = create_cmd.node_name
                if cmd_node_name == package_node.name:
                    serialized_node = serialized_node_command
                    break

            if serialized_node is None:
                error_msg = f"Data integrity error: Could not find serialized node for package node '{package_node.name}'. This indicates a logic error in the serialization process."
                raise RuntimeError(error_msg)

            package_alter_parameter_commands = []
            for package_param in package_node.parameters:
                has_output_mode = ParameterMode.OUTPUT in package_param.allowed_modes
                has_property_mode = ParameterMode.PROPERTY in package_param.allowed_modes
                # If has PROPERTY but not OUTPUT, add ALTER command to enable OUTPUT
                if has_property_mode and not has_output_mode:
                    alter_param_request = AlterParameterDetailsRequest(
                        parameter_name=package_param.name,
                        node_name=package_node.name,
                        mode_allowed_output=True,
                    )
                    package_alter_parameter_commands.append(alter_param_request)

            # If we have alter parameter commands, append them to the existing element_modification_commands
            if package_alter_parameter_commands:
                serialized_node.element_modification_commands.extend(package_alter_parameter_commands)

    def _analyze_multi_node_external_connections(
        self, package_nodes: list[BaseNode]
    ) -> dict[str, ConnectionAnalysis] | PackageNodesAsSerializedFlowResultFailure:
        """Analyze external connections for each package node using filtered single-node analysis.

        This method reuses the existing single-node connection analysis method but applies filtering
        to only capture "external" connections - those that cross the package boundary.

        External connections are:
        - Incoming connections where the source node is NOT in the package
        - Outgoing connections where the target node is NOT in the package

        Internal connections (between nodes within the package) are filtered out by passing
        the set of package node names to the single-node analysis method.

        Args:
            package_nodes: List of nodes being packaged together

        Returns:
            Dictionary mapping node_name -> ConnectionAnalysis, where each ConnectionAnalysis
            contains only the external connections for that node, or failure result on error
        """
        package_node_names_set = {node.name for node in package_nodes}
        node_connections = {}

        for package_node in package_nodes:
            # Perform a single node analysis, filtering out internal connections
            connection_analysis = self._analyze_package_node_connections(
                package_node=package_node,
                node_name=package_node.name,
                package_node_names=package_node_names_set,
            )
            if isinstance(connection_analysis, PackageNodesAsSerializedFlowResultFailure):
                return PackageNodesAsSerializedFlowResultFailure(result_details=connection_analysis.result_details)

            node_connections[package_node.name] = connection_analysis

        return node_connections

    def _analyze_package_node_connections(
        self,
        package_node: BaseNode,
        node_name: str,
        package_node_names: set[str] | None = None,
    ) -> ConnectionAnalysis | PackageNodesAsSerializedFlowResultFailure:
        """Analyze package node connections and separate control from data connections.

        Args:
            package_node: The node being analyzed
            node_name: Name of the node
            package_node_names: Set of node names in the package for filtering internal connections
        """
        # Get incoming and outgoing connections for this node
        # Get connection details using the standard approach
        list_connections_request = ListConnectionsForNodeRequest(node_name=node_name)
        list_connections_result = self.engine.node_manager.on_list_connections_for_node_request(
            list_connections_request
        )

        if not isinstance(list_connections_result, ListConnectionsForNodeResultSuccess):
            details = f"Attempted to analyze connections for package node '{node_name}'. Failed because connection listing failed."
            return PackageNodesAsSerializedFlowResultFailure(result_details=details)

        incoming_connections = list_connections_result.incoming_connections
        outgoing_connections = list_connections_result.outgoing_connections

        # Separate control connections from data connections based on package node's parameter types
        incoming_data_connections = []
        incoming_control_connections = []
        for incoming_conn in incoming_connections:
            # Filter out internal connections if package_node_names is provided
            if package_node_names is not None and incoming_conn.source_node_name in package_node_names:
                continue

            # Get the package node's parameter to check if it's a control type
            package_param = package_node.get_parameter_by_name(incoming_conn.target_parameter_name)
            if package_param and ParameterTypeBuiltin.CONTROL_TYPE.value in package_param.input_types:
                incoming_control_connections.append(incoming_conn)
            else:
                incoming_data_connections.append(incoming_conn)

        outgoing_data_connections = []
        outgoing_control_connections = []
        for outgoing_conn in outgoing_connections:
            # Filter out internal connections if package_node_names is provided
            if package_node_names is not None and outgoing_conn.target_node_name in package_node_names:
                continue

            # Get the package node's parameter to check if it's a control type
            package_param = package_node.get_parameter_by_name(outgoing_conn.source_parameter_name)
            if package_param and ParameterTypeBuiltin.CONTROL_TYPE.value == package_param.output_type:
                outgoing_control_connections.append(outgoing_conn)
            else:
                outgoing_data_connections.append(outgoing_conn)

        return ConnectionAnalysis(
            incoming_data_connections=incoming_data_connections,
            incoming_control_connections=incoming_control_connections,
            outgoing_data_connections=outgoing_data_connections,
            outgoing_control_connections=outgoing_control_connections,
        )

    def _create_multi_node_end_node_with_connections(
        self,
        request: PackageNodesAsSerializedFlowRequest,
        package_nodes: list[BaseNode],
        node_name_to_uuid: dict[str, SerializedNodeCommands.NodeUUID],
        library_version: str,
        node_connections_dict: dict[str, ConnectionAnalysis],
    ) -> MultiNodeEndNodeResult | PackageNodesAsSerializedFlowResultFailure:
        """Create end node commands and connections for ALL package parameters that meet criteria (copied from single-node)."""
        # Generate UUID and name for end node
        end_node_uuid = SerializedNodeCommands.NodeUUID(str(uuid4()))
        end_node_name = "End_Multi_Package"

        # Build end node CreateNodeRequest
        end_create_node_command = CreateNodeRequest(
            node_type=request.end_node_type,  # type: ignore[arg-type]  # Guaranteed non-None by handler
            specific_library_name=request.end_node_library_name,
            node_name=end_node_name,
            metadata={PACKAGED_FLOW_BOUNDARY_KEY: True},
            initial_setup=True,
            create_error_proxy_on_failure=False,
        )

        # Create library details
        end_node_library_details = LibraryNameAndVersion(
            library_name=request.end_node_library_name,
            library_version=library_version,
        )

        # Initialize collections for building the end node
        end_node_parameter_commands = []
        package_to_end_connections = []
        output_shape_data: WorkflowShapeNodes = {}
        # Parameter name mappings (rosetta stone): maps mangled end node parameter names back to original (node_name, parameter_name)
        # This is essential for callers to understand which end node outputs correspond to which original node parameters
        parameter_name_mappings: dict[SanitizedParameterName, OriginalNodeParameter] = {}

        # Handle external control connections first
        self._create_end_node_control_connections(
            request=request,
            package_nodes=package_nodes,
            node_connections_dict=node_connections_dict,
            node_name_to_uuid=node_name_to_uuid,
            end_node_uuid=end_node_uuid,
            end_node_name=end_node_name,
            end_node_parameter_commands=end_node_parameter_commands,
            package_to_end_connections=package_to_end_connections,
            parameter_name_mappings=parameter_name_mappings,
            output_shape_data=output_shape_data,
        )

        # Process ALL parameters with OUTPUT or PROPERTY modes for comprehensive coverage
        self._create_end_node_data_parameters_and_connections(
            request=request,
            package_nodes=package_nodes,
            node_name_to_uuid=node_name_to_uuid,
            end_node_uuid=end_node_uuid,
            end_node_name=end_node_name,
            end_node_parameter_commands=end_node_parameter_commands,
            package_to_end_connections=package_to_end_connections,
            parameter_name_mappings=parameter_name_mappings,
            output_shape_data=output_shape_data,
        )

        # Build complete SerializedNodeCommands for end node
        end_node_dependencies = NodeDependencies()
        end_node_dependencies.libraries.add(end_node_library_details)

        end_node_commands = SerializedNodeCommands(
            create_node_command=end_create_node_command,
            element_modification_commands=end_node_parameter_commands,
            node_dependencies=end_node_dependencies,
            node_uuid=end_node_uuid,
        )

        end_node_result = PackagingEndNodeResult(
            end_node_commands=end_node_commands,
            package_to_end_connections=package_to_end_connections,
            output_shape_data=output_shape_data,
        )

        return MultiNodeEndNodeResult(
            packaging_result=end_node_result,
            parameter_name_mappings=parameter_name_mappings,
            alter_parameter_commands=[],
            end_node_name=end_node_name,
        )

    def _create_end_node_control_connections(  # noqa: PLR0913, PLR0917
        self,
        request: PackageNodesAsSerializedFlowRequest,
        package_nodes: list[BaseNode],
        node_connections_dict: dict[str, ConnectionAnalysis],  # Contains only EXTERNAL connections
        node_name_to_uuid: dict[
            str, SerializedNodeCommands.NodeUUID
        ],  # Map node names to UUIDs for connection creation
        end_node_uuid: SerializedNodeCommands.NodeUUID,
        end_node_name: str,
        end_node_parameter_commands: list[
            AddParameterToNodeRequest
        ],  # OUTPUT: Will populate with parameters to add to end node
        package_to_end_connections: list[
            SerializedFlowCommands.IndirectConnectionSerialization
        ],  # OUTPUT: Will populate with connections to add
        parameter_name_mappings: dict[
            SanitizedParameterName, OriginalNodeParameter
        ],  # OUTPUT: Will populate rosetta stone for parameter names so customer knows how to map mangled names back to original nodes.
        output_shape_data: WorkflowShapeNodes,  # OUTPUT: Will populate with workflow shape data
    ) -> None:
        """Create control connections and parameters on end node for EXTERNAL control flow connections."""
        for package_node in package_nodes:
            node_connection_analysis = node_connections_dict.get(package_node.name)
            if node_connection_analysis is None:
                # This node has no external connections (neither incoming nor outgoing), skip it
                continue

            # Handle external outgoing control connections
            for control_conn in node_connection_analysis.outgoing_control_connections:
                # Get the source parameter for validation and processing
                source_param = package_node.get_parameter_by_name(control_conn.source_parameter_name)
                if source_param is None:
                    msg = f"External control connection references parameter '{control_conn.source_parameter_name}' on node '{package_node.name}' which does not exist. This indicates a data consistency issue."
                    raise ValueError(msg)

                # Get the EndFlow node class for validation
                end_library = LibraryRegistry.get_library_for_node_type(
                    request.end_node_type,  # type: ignore[arg-type]  # Guaranteed non-None by handler
                    request.end_node_library_name,
                )
                end_node_class = end_library.get_node_class(request.end_node_type)  # type: ignore[arg-type]

                # Validate from package node's perspective (we know source param name, not target)
                valid_connection = package_node.__class__.allow_outgoing_connection_by_class(
                    end_node_class,
                    source_param.name,  # source parameter name - we KNOW this
                    None,  # target parameter name - we DON'T know this yet
                )

                # Only create the connection if validation passes
                if not valid_connection:
                    # Some nodes only allow connections to certain other nodes (for example ForEach Start and End Node). We shouldn't allow those connections to be serialized, because they'll fail on deserialization.
                    logger.warning(
                        "Skipping control connection from %s.%s to EndFlow - connection not allowed by node validation rules",
                        package_node.name,
                        source_param.name,
                    )
                    continue

                # Use comprehensive helper to process this control parameter
                self._process_parameter_for_end_node(
                    request=request,
                    parameter=source_param,
                    node_name=package_node.name,
                    node_uuid=node_name_to_uuid[package_node.name],
                    end_node_name=end_node_name,
                    end_node_uuid=end_node_uuid,
                    tooltip=f"Control output {control_conn.source_parameter_name} from packaged node {package_node.name}",
                    end_node_parameter_commands=end_node_parameter_commands,
                    package_to_end_connections=package_to_end_connections,
                    parameter_name_mappings=parameter_name_mappings,
                    output_shape_data=output_shape_data,
                )

    def _create_end_node_data_parameters_and_connections(  # noqa: PLR0913, PLR0917
        self,
        request: PackageNodesAsSerializedFlowRequest,
        package_nodes: list[BaseNode],
        node_name_to_uuid: dict[str, SerializedNodeCommands.NodeUUID],
        end_node_uuid: SerializedNodeCommands.NodeUUID,
        end_node_name: str,
        end_node_parameter_commands: list[
            AddParameterToNodeRequest
        ],  # OUTPUT: Will populate with parameters to add to end node
        package_to_end_connections: list[
            SerializedFlowCommands.IndirectConnectionSerialization
        ],  # OUTPUT: Will populate with connections to add
        parameter_name_mappings: dict[
            SanitizedParameterName, OriginalNodeParameter
        ],  # OUTPUT: Will populate rosetta stone for parameter names
        output_shape_data: WorkflowShapeNodes,  # OUTPUT: Will populate with workflow shape data
    ) -> None:
        """Create data parameters and connections on end node for all OUTPUT/PROPERTY parameters from packaged nodes."""
        for package_node in package_nodes:
            package_node_uuid = node_name_to_uuid[package_node.name]

            for package_param in package_node.parameters:
                # Only process parameters with OUTPUT or PROPERTY mode
                has_output_mode = ParameterMode.OUTPUT in package_param.allowed_modes
                has_property_mode = ParameterMode.PROPERTY in package_param.allowed_modes

                if not has_output_mode and not has_property_mode:
                    continue

                # Skip control parameters - those are handled by the control connections helper
                if package_param.output_type == ParameterTypeBuiltin.CONTROL_TYPE.value:
                    continue

                # Get the EndFlow node class for validation
                try:
                    end_library = LibraryRegistry.get_library_for_node_type(
                        request.end_node_type,  # type: ignore[arg-type]  # Guaranteed non-None by handler
                        request.end_node_library_name,
                    )
                    end_node_class = end_library.get_node_class(request.end_node_type)  # type: ignore[arg-type]

                    # Validate from package node's perspective (we know source param name, not target)
                    valid_connection = package_node.__class__.allow_outgoing_connection_by_class(
                        end_node_class,
                        package_param.name,  # source parameter name - we KNOW this
                        None,  # target parameter name - we DON'T know this yet
                    )
                except KeyError as err:
                    msg = f"Unable to find library for node type '{request.end_node_type}'"
                    raise KeyError(msg) from err

                # Use comprehensive helper to process this data parameter
                if valid_connection:
                    self._process_parameter_for_end_node(
                        request=request,
                        parameter=package_param,
                        node_name=package_node.name,
                        node_uuid=package_node_uuid,
                        end_node_name=end_node_name,
                        end_node_uuid=end_node_uuid,
                        tooltip=f"Output parameter {package_param.name} from packaged node {package_node.name}",
                        end_node_parameter_commands=end_node_parameter_commands,
                        package_to_end_connections=package_to_end_connections,
                        parameter_name_mappings=parameter_name_mappings,
                        output_shape_data=output_shape_data,
                    )

    def _process_parameter_for_end_node(  # noqa: PLR0913, PLR0917
        self,
        request: PackageNodesAsSerializedFlowRequest,
        parameter: Parameter,
        node_name: str,
        node_uuid: SerializedNodeCommands.NodeUUID,
        end_node_name: str,
        end_node_uuid: SerializedNodeCommands.NodeUUID,
        tooltip: str,
        end_node_parameter_commands: list[
            AddParameterToNodeRequest
        ],  # OUTPUT: Will populate with parameters to add to end node
        package_to_end_connections: list[
            SerializedFlowCommands.IndirectConnectionSerialization
        ],  # OUTPUT: Will populate with connections to add
        parameter_name_mappings: dict[
            SanitizedParameterName, OriginalNodeParameter
        ],  # OUTPUT: Will populate rosetta stone for parameter names
        output_shape_data: WorkflowShapeNodes,  # OUTPUT: Will populate with workflow shape data
    ) -> None:
        """Process a single parameter for inclusion in the end node, handling all aspects of parameter creation and connection."""
        # Create sanitized parameter name with collision avoidance
        sanitized_param_name = self._generate_sanitized_parameter_name(
            prefix=request.output_parameter_prefix,
            node_name=node_name,
            parameter_name=parameter.name,
        )

        # Build parameter name mapping for rosetta stone
        parameter_name_mappings[sanitized_param_name] = OriginalNodeParameter(
            node_name=node_name,
            parameter_name=parameter.name,
        )

        # Extract parameter shape info for workflow shape (outputs to external consumers)
        param_shape_info = extract_parameter_shape_info(parameter, include_control_params=True)
        if param_shape_info is not None:
            if end_node_name not in output_shape_data:
                output_shape_data[end_node_name] = {}
            output_shape_data[end_node_name][sanitized_param_name] = param_shape_info

        # Create parameter command for end node
        # Use flexible input types for data parameters to prevent type mismatch errors
        # Control parameters must keep their exact types (cannot use "any")
        is_control_param = parameter.output_type == ParameterTypeBuiltin.CONTROL_TYPE.value

        add_param_request = AddParameterToNodeRequest(
            node_name=end_node_name,
            parameter_name=sanitized_param_name,
            input_types=parameter.input_types if is_control_param else ["any"],  # Control: exact types; Data: any
            output_type=parameter.output_type,  # Preserve original output type
            default_value=None,
            tooltip=tooltip,
            initial_setup=True,
        )
        end_node_parameter_commands.append(add_param_request)

        # Create connection from package node to end node
        package_to_end_connection = SerializedFlowCommands.IndirectConnectionSerialization(
            source_node_uuid=node_uuid,
            source_parameter_name=parameter.name,
            target_node_uuid=end_node_uuid,
            target_parameter_name=sanitized_param_name,
        )
        package_to_end_connections.append(package_to_end_connection)

    def _create_multi_node_start_node_with_connections(  # noqa: PLR0913, PLR0917
        self,
        request: PackageNodesAsSerializedFlowRequest,
        library_version: str,
        unique_parameter_uuid_to_values: dict[SerializedNodeCommands.UniqueParameterValueUUID, Any],
        serialized_parameter_value_tracker: SerializedParameterValueTracker,
        node_name_to_uuid: dict[str, SerializedNodeCommands.NodeUUID],
        external_connections_dict: dict[
            str, ConnectionAnalysis
        ],  # Contains EXTERNAL connections only - used to determine which parameters need start node inputs
        node_group_node: SubflowNodeGroup | None = None,
    ) -> PackagingStartNodeResult | PackageNodesAsSerializedFlowResultFailure:
        """Create start node commands and connections for external incoming connections."""
        # Generate UUID and name for start node
        start_node_uuid = SerializedNodeCommands.NodeUUID(str(uuid4()))
        start_node_name = "Start_Package_MultiNode"
        # Parameter name mappings are essential to know which inputs are necessary on the start node given.
        parameter_name_mappings: dict[SanitizedParameterName, OriginalNodeParameter] = {}

        # Build start node CreateNodeRequest
        start_create_node_command = CreateNodeRequest(
            node_type=request.start_node_type,  # type: ignore[arg-type]  # Guaranteed non-None by handler
            specific_library_name=request.start_node_library_name,
            node_name=start_node_name,
            metadata={PACKAGED_FLOW_BOUNDARY_KEY: True},
            initial_setup=True,
            create_error_proxy_on_failure=False,
        )

        # Create library details
        start_node_library_details = LibraryNameAndVersion(
            library_name=request.start_node_library_name,
            library_version=library_version,
        )

        # Create parameter modification commands and connection mappings for the start node
        start_node_parameter_commands = []
        start_to_package_connections = []
        start_node_parameter_value_commands = []
        input_shape_data: WorkflowShapeNodes = {}

        # Iterate through all EXTERNAL, INCOMING, DATA connections.
        # We will then, for each connection:
        #  1. Generate a mangled name
        #  2. Add parameter with the mangled name for each connection source on the Start Node object
        #  3. Extract the value from the source node and have it assigned to the Start Node so that it can be propagated
        #  4. Create a connection from the Start Node to the package node
        for target_node_name, connection_analysis in external_connections_dict.items():
            result = self._create_start_node_parameters_and_connections_for_incoming_data(
                target_node_name=target_node_name,
                incoming_data_connections=connection_analysis.incoming_data_connections,
                output_parameter_prefix=request.output_parameter_prefix,
                start_node_name=start_node_name,
                start_node_uuid=start_node_uuid,
                start_create_node_command=start_create_node_command,
                node_name_to_uuid=node_name_to_uuid,
                unique_parameter_uuid_to_values=unique_parameter_uuid_to_values,
                serialized_parameter_value_tracker=serialized_parameter_value_tracker,
                parameter_name_mappings=parameter_name_mappings,
            )
            if isinstance(result, PackageNodesAsSerializedFlowResultFailure):
                return result

            # Accumulate results from helper
            start_node_parameter_commands.extend(result.parameter_commands)
            start_to_package_connections.extend(result.data_connections)
            start_node_parameter_value_commands.extend(result.parameter_value_commands)
            # Merge input shape data
            for node_name, params in result.input_shape_data.items():
                if node_name not in input_shape_data:
                    input_shape_data[node_name] = {}
                input_shape_data[node_name].update(params)

        # Create all control connections
        control_connections = self._create_start_node_control_connections(
            request=request,
            start_node_uuid=start_node_uuid,
            node_name_to_uuid=node_name_to_uuid,
        )
        if isinstance(control_connections, PackageNodesAsSerializedFlowResultFailure):
            return control_connections

        # Add control connections to the same list as data connections
        start_to_package_connections.extend(control_connections)

        # Set parameter values from SubflowNodeGroup if provided
        if node_group_node is not None:
            self._apply_node_group_parameters_to_start_node(
                node_group_node=node_group_node,
                start_node_library_name=request.start_node_library_name,
                start_node_type=request.start_node_type,  # type: ignore[arg-type]  # Guaranteed non-None
                start_node_parameter_value_commands=start_node_parameter_value_commands,
                unique_parameter_uuid_to_values=unique_parameter_uuid_to_values,
                serialized_parameter_value_tracker=serialized_parameter_value_tracker,
            )

        # Build complete SerializedNodeCommands for start node
        start_node_dependencies = NodeDependencies()
        start_node_dependencies.libraries.add(start_node_library_details)

        start_node_commands = SerializedNodeCommands(
            create_node_command=start_create_node_command,
            element_modification_commands=start_node_parameter_commands,
            node_dependencies=start_node_dependencies,
            node_uuid=start_node_uuid,
        )

        return PackagingStartNodeResult(
            start_node_commands=start_node_commands,
            start_to_package_connections=start_to_package_connections,
            input_shape_data=input_shape_data,
            start_node_parameter_value_commands=start_node_parameter_value_commands,
            parameter_name_mappings=parameter_name_mappings,
            start_node_name=start_node_name,
        )

    def _apply_node_group_parameters_to_start_node(  # noqa: PLR0913, PLR0917
        self,
        node_group_node: SubflowNodeGroup,
        start_node_library_name: str,
        start_node_type: str,
        start_node_parameter_value_commands: list[SerializedNodeCommands.IndirectSetParameterValueCommand],
        unique_parameter_uuid_to_values: dict[SerializedNodeCommands.UniqueParameterValueUUID, Any],
        serialized_parameter_value_tracker: SerializedParameterValueTracker,
    ) -> None:
        """Apply parameter values from SubflowNodeGroup to the StartFlow node.

        This method reads the execution environment metadata from the SubflowNodeGroup,
        extracts parameter values for the specified StartFlow node type, and creates
        set parameter value commands for those parameters.

        Args:
            node_group_node: The SubflowNodeGroup containing parameter values
            start_node_library_name: Name of the library containing the StartFlow node
            start_node_type: Type of the StartFlow node
            start_node_parameter_value_commands: List to append parameter value commands to
            unique_parameter_uuid_to_values: Dict to track unique parameter values
            serialized_parameter_value_tracker: Tracker for serialized parameter values

        Raises:
            ValueError: If required metadata is missing from SubflowNodeGroup
        """
        # Get execution environment metadata from SubflowNodeGroup
        if not node_group_node.metadata:
            msg = f"SubflowNodeGroup '{node_group_node.name}' is missing metadata. Cannot apply parameters to StartFlow node."
            raise ValueError(msg)

        execution_env_metadata = node_group_node.metadata.get("execution_environment")
        if not execution_env_metadata:
            msg = f"SubflowNodeGroup '{node_group_node.name}' metadata is missing 'execution_environment'. Cannot apply parameters to StartFlow node."
            raise ValueError(msg)

        # Find the metadata for the current library
        library_metadata = execution_env_metadata.get(start_node_library_name)
        if library_metadata is None:
            msg = f"SubflowNodeGroup '{node_group_node.name}' metadata does not contain library '{start_node_library_name}'. Available libraries: {list(execution_env_metadata.keys())}"
            raise ValueError(msg)

        # Verify this is the correct StartFlow node type
        registered_start_flow_node = library_metadata.get("start_flow_node")
        if registered_start_flow_node != start_node_type:
            msg = f"SubflowNodeGroup '{node_group_node.name}' has mismatched StartFlow node type. Expected '{start_node_type}', but metadata has '{registered_start_flow_node}'"
            raise ValueError(msg)

        # Get the list of parameter names that belong to this StartFlow node
        parameter_names = library_metadata.get("parameter_names", [])
        if not parameter_names:
            # This is not an error - it's valid for a StartFlow node to have no parameters
            logger.debug(
                "SubflowNodeGroup '%s' has no parameters registered for StartFlow node '%s'",
                node_group_node.name,
                start_node_type,
            )
            return

        # For each parameter, get its value from the SubflowNodeGroup and create a set value command
        class_name_prefix = start_node_type.lower()
        for prefixed_param_name in parameter_names:
            # Skip parameters that don't have the expected prefix for this StartFlow node.
            # These are group-level settings that control the group's behavior
            # but shouldn't be passed to the StartFlow node of each iteration.
            if not prefixed_param_name.startswith(f"{class_name_prefix}_"):
                logger.debug(
                    "Skipping group-level parameter '%s' - not a StartFlow parameter (expected prefix '%s_')",
                    prefixed_param_name,
                    class_name_prefix,
                )
                continue

            # Get the value from the SubflowNodeGroup parameter
            param_value = node_group_node._get_raw_parameter_value(param_name=prefixed_param_name)

            # Skip if no value is set
            if param_value is None:
                continue

            # Strip the prefix to get the original parameter name for the StartFlow node
            original_param_name = prefixed_param_name.removeprefix(f"{class_name_prefix}_")

            encoded = try_encode(param_value)
            if isinstance(encoded, Unencodable):
                logger.warning(
                    "Attempted to pass '%s' into the packaged flow. Failed because %s The flow runs without it.",
                    prefixed_param_name,
                    encoded.reason,
                )
                continue
            value_id = id(param_value)
            unique_param_uuid = SerializedNodeCommands.UniqueParameterValueUUID(value_key(encoded))
            unique_parameter_uuid_to_values[unique_param_uuid] = encoded
            serialized_parameter_value_tracker.add_as_serializable(value_id, unique_param_uuid)

            # Create set parameter value command
            set_value_request = SetParameterValueRequest(
                parameter_name=original_param_name,
                value=None,  # Will be overridden when instantiated
                is_output=False,
                initial_setup=True,
            )
            indirect_set_value_command = SerializedNodeCommands.IndirectSetParameterValueCommand(
                set_parameter_value_command=set_value_request,
                unique_value_uuid=unique_param_uuid,
            )
            start_node_parameter_value_commands.append(indirect_set_value_command)

    def _create_start_node_parameters_and_connections_for_incoming_data(  # noqa: PLR0913, PLR0917
        self,
        target_node_name: str,
        incoming_data_connections: list[IncomingConnection],
        output_parameter_prefix: str,
        start_node_name: str,
        start_node_uuid: SerializedNodeCommands.NodeUUID,
        start_create_node_command: CreateNodeRequest,
        node_name_to_uuid: dict[str, SerializedNodeCommands.NodeUUID],
        unique_parameter_uuid_to_values: dict[SerializedNodeCommands.UniqueParameterValueUUID, Any],
        serialized_parameter_value_tracker: SerializedParameterValueTracker,
        parameter_name_mappings: dict[SanitizedParameterName, OriginalNodeParameter],
    ) -> StartNodeIncomingDataResult | PackageNodesAsSerializedFlowResultFailure:
        """Create parameters and connections for incoming data connections to a specific target node."""
        start_node_parameter_commands = []
        start_to_package_connections = []
        start_node_parameter_value_commands = []
        input_shape_data: WorkflowShapeNodes = {}

        for connection in incoming_data_connections:
            target_parameter_name = connection.target_parameter_name

            # Create sanitized parameter name with prefix + node + parameter
            param_name = self._generate_sanitized_parameter_name(
                prefix=output_parameter_prefix,
                node_name=target_node_name,
                parameter_name=target_parameter_name,
            )

            parameter_name_mappings[param_name] = OriginalNodeParameter(
                node_name=target_node_name, parameter_name=target_parameter_name
            )

            # Get the source node to determine parameter type (from the external connection)
            try:
                source_node = self.engine.node_manager.get_node_by_name(connection.source_node_name)
            except ValueError as err:
                details = f"Attempted to package nodes as serialized flow. Failed because source node '{connection.source_node_name}' from incoming connection could not be found. Error: {err}."
                return PackageNodesAsSerializedFlowResultFailure(result_details=details)

            # Get the source parameter
            source_param = source_node.get_parameter_by_name(connection.source_parameter_name)
            if not source_param:
                details = f"Attempted to package nodes as serialized flow. Failed because source parameter '{connection.source_parameter_name}' on node '{connection.source_node_name}' from incoming connection could not be found."
                return PackageNodesAsSerializedFlowResultFailure(result_details=details)

            # Extract parameter shape info for workflow shape (inputs from external sources)
            param_shape_info = extract_parameter_shape_info(source_param, include_control_params=True)
            if param_shape_info is not None:
                if start_node_name not in input_shape_data:
                    input_shape_data[start_node_name] = {}
                input_shape_data[start_node_name][param_name] = param_shape_info

            # Extract parameter value from source node to set on start node
            param_value_commands = self.engine.node_manager.handle_parameter_value_saving(
                parameter=source_param,
                node=source_node,
                unique_parameter_uuid_to_values=unique_parameter_uuid_to_values,
                serialized_parameter_value_tracker=serialized_parameter_value_tracker,
                create_node_request=start_create_node_command,
            )
            if param_value_commands is not None:
                # Modify each command to target the start node parameter instead
                for param_value_command in param_value_commands:
                    param_value_command.set_parameter_value_command.node_name = start_node_name
                    param_value_command.set_parameter_value_command.parameter_name = param_name
                    start_node_parameter_value_commands.append(param_value_command)

            # Create parameter command for start node (following single-node pattern exactly)
            # Use source parameter's default value to ensure type-safe propagation during connection creation
            add_param_request = AddParameterToNodeRequest(
                node_name=start_node_name,
                parameter_name=param_name,
                type=source_param.output_type,
                default_value=encodable_default(source_param.default_value, source_node.name, source_param.name),
                tooltip=f"Parameter {target_parameter_name} from node {target_node_name} in packaged flow",
                initial_setup=True,
            )
            start_node_parameter_commands.append(add_param_request)

            # Get target node UUID from mapping
            target_node_uuid = node_name_to_uuid.get(target_node_name)
            if target_node_uuid is None:
                details = f"Attempted to package nodes as serialized flow. Failed because target node '{target_node_name}' UUID not found in mapping."
                return PackageNodesAsSerializedFlowResultFailure(result_details=details)

            # Create connection from start node to target node
            start_to_package_connections.append(
                SerializedFlowCommands.IndirectConnectionSerialization(
                    source_node_uuid=start_node_uuid,
                    source_parameter_name=param_name,
                    target_node_uuid=target_node_uuid,
                    target_parameter_name=target_parameter_name,
                )
            )

        return StartNodeIncomingDataResult(
            parameter_commands=start_node_parameter_commands,
            data_connections=start_to_package_connections,
            input_shape_data=input_shape_data,
            parameter_value_commands=start_node_parameter_value_commands,
        )

    def _create_start_node_control_connections(
        self,
        request: PackageNodesAsSerializedFlowRequest,
        start_node_uuid: SerializedNodeCommands.NodeUUID,
        node_name_to_uuid: dict[str, SerializedNodeCommands.NodeUUID],
    ) -> list[SerializedFlowCommands.IndirectConnectionSerialization] | PackageNodesAsSerializedFlowResultFailure:
        """Create control connection from start node to entry control node.

        Args:
            request: The packaging request with control flow configuration
            start_node_uuid: UUID of the start node
            node_name_to_uuid: Mapping of node names to UUIDs for lookup

        Returns:
            List of control connections or failure result
        """
        control_connections = []

        # Connect start node to specified entry control node (if specified)
        if request.entry_control_node_name:
            # Get the entry node (already validated to exist)
            entry_node = self.engine.node_manager.get_node_by_name(request.entry_control_node_name)
            entry_node_uuid = node_name_to_uuid[request.entry_control_node_name]

            # Connect start node to specified entry control node
            control_connection_result = self._create_start_node_control_connection(
                entry_control_parameter_name=request.entry_control_parameter_name,
                start_node_uuid=start_node_uuid,
                package_node_uuid=entry_node_uuid,
                package_node=entry_node,
            )
            if isinstance(control_connection_result, PackageNodesAsSerializedFlowResultFailure):
                return PackageNodesAsSerializedFlowResultFailure(
                    result_details=control_connection_result.result_details
                )

            control_connections.append(control_connection_result)

        return control_connections

    def _create_start_node_control_connection(
        self,
        entry_control_parameter_name: str | None,
        start_node_uuid: SerializedNodeCommands.NodeUUID,
        package_node_uuid: SerializedNodeCommands.NodeUUID,
        package_node: BaseNode,
    ) -> SerializedFlowCommands.IndirectConnectionSerialization | PackageNodesAsSerializedFlowResultFailure:
        """Create control flow connection from start node to package node.

        Connects the start node's first control output to the specified or first available package node control input.
        """
        if entry_control_parameter_name is not None:
            # Case 1: Specific entry parameter name provided
            package_control_input_name = entry_control_parameter_name
        else:
            # Case 2: Find the first available control input parameter
            package_control_input_name = None
            for param in package_node.parameters:
                if ParameterTypeBuiltin.CONTROL_TYPE.value in param.input_types:
                    package_control_input_name = param.name
                    logger.warning(
                        "No entry_control_parameter_name specified for packaging node '%s'. "
                        "Using first available control input parameter: '%s'",
                        package_node.name,
                        package_control_input_name,
                    )
                    break

            if package_control_input_name is None:
                details = f"Attempted to package node '{package_node.name}'. Failed because no control input parameters found on the node, so cannot create control flow connection."
                return PackageNodesAsSerializedFlowResultFailure(result_details=details)

        # StartNode always has a control output parameter with name "exec_out"
        source_control_parameter_name = "exec_out"

        # Create the connection
        control_connection = SerializedFlowCommands.IndirectConnectionSerialization(
            source_node_uuid=start_node_uuid,
            source_parameter_name=source_control_parameter_name,
            target_node_uuid=package_node_uuid,
            target_parameter_name=package_control_input_name,
        )
        return control_connection

    def _collect_all_connections_for_multi_node_package(
        self,
        start_node_result: PackagingStartNodeResult,
        end_node_packaging_result: PackagingEndNodeResult,
        packaged_nodes_internal_connections: list[SerializedFlowCommands.IndirectConnectionSerialization],
    ) -> list[SerializedFlowCommands.IndirectConnectionSerialization]:
        """Collect all connections for the multi-node packaged flow.

        Returns a list containing:
        1. Start node connections (data + control)
        2. End node connections (data)
        3. Internal package node connections

        Args:
            start_node_result: Result containing start node and its connections
            end_node_packaging_result: Result containing end node and its connections
            packaged_nodes_internal_connections: Internal connections between package nodes

        Returns:
            List of all connections for the packaged flow
        """
        all_connections = []

        # Add start and end node connections
        all_connections.extend(start_node_result.start_to_package_connections)
        all_connections.extend(end_node_packaging_result.package_to_end_connections)

        # Add internal package node connections
        all_connections.extend(packaged_nodes_internal_connections)

        return all_connections

    def _generate_sanitized_parameter_name(self, prefix: str, node_name: str, parameter_name: str) -> str:
        """Generate a sanitized parameter name for multi-node packaging.

        Creates a parameter name in the format: prefix + sanitized_node_name + _ + parameter_name
        Node names are sanitized by replacing spaces and dots with underscores.

        Args:
            prefix: Prefix for the parameter name (e.g., "packaged_node_")
            node_name: Original node name (may contain spaces, dots, etc.)
            parameter_name: Original parameter name

        Returns:
            Sanitized parameter name safe for use (e.g., "packaged_node_Merge_Texts_merge_string")
        """
        sanitized_node_name = node_name.replace(" ", "_").replace(".", "_")
        return f"{prefix}{sanitized_node_name}_{parameter_name}"
