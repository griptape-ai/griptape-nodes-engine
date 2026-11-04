from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass, field, replace
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx2
from PIL import Image

from griptape_nodes.exe_types.flow import ControlFlow
from griptape_nodes.exe_types.node_groups import BaseNodeGroup
from griptape_nodes.exe_types.node_types import (
    BaseNode,
    NodeDependencies,
    VariableReference,
)
from griptape_nodes.node_library.workflow_registry import LibraryNameAndNodeType
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
)
from griptape_nodes.retained_mode.events.flow_events import (
    TRANSIENT_KEY,
    CreateFlowRequest,
    CreateFlowResultSuccess,
    DeserializeFlowFromCommandsRequest,
    DeserializeFlowFromCommandsResultFailure,
    DeserializeFlowFromCommandsResultSuccess,
    ExtractFlowCommandsFromImageMetadataRequest,
    ExtractFlowCommandsFromImageMetadataResultFailure,
    ExtractFlowCommandsFromImageMetadataResultSuccess,
    ImportWorkflowAsReferencedSubFlowRequest,
    ListFlowsInFlowRequest,
    ListFlowsInFlowResultSuccess,
    ListNodesInFlowRequest,
    ListNodesInFlowResultSuccess,
    SerializedConnectionKey,
    SerializedFlowCommands,
    SerializeFlowToCommandsRequest,
    SerializeFlowToCommandsResultFailure,
    SerializeFlowToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    DeserializeNodeFromCommandsRequest,
    DeserializeNodeFromCommandsResultSuccess,
    SerializedNodeCommands,
    SerializedParameterValueTracker,
    SerializeNodeToCommandsRequest,
    SerializeNodeToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.variable_events import (
    CreateVariableRequest,
    GetVariableRequest,
    GetVariableResultSuccess,
    ListVariablesRequest,
    ListVariablesResultSuccess,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    ImportWorkflowAsReferencedSubFlowResultSuccess,
)
from griptape_nodes.retained_mode.file_metadata.workflow_metadata import FLOW_COMMANDS_KEY
from griptape_nodes.retained_mode.request_handlers import handles
from griptape_nodes.retained_mode.variable_types import VariableScope
from griptape_nodes.serialization.commands import CommandsFormatError, decode_commands
from griptape_nodes.serialization.legacy_pickle import LegacyPickleError, read_legacy_image_flow_commands
from griptape_nodes.serialization.values import Unencodable, decode_value, try_encode, value_key

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager
    from griptape_nodes.retained_mode.variable_types import FlowVariable

logger = logging.getLogger("griptape_nodes")


class ImageFlowCommandsError(Exception):
    """An image's metadata holds flow commands this engine cannot read.

    The message completes "Failed because ...".
    """


class FlowDeserializationError(Exception):
    """A saved Flow could not be rebuilt.

    Rebuilding a Flow runs in three passes — nodes, then connections, then values — each of which
    recurses through every subflow. Any of them can fail deep in the recursion, and all of them fail
    the whole load. Raising this specific type lets on_deserialize_flow_from_commands catch exactly
    the failures its own passes report, rather than every RuntimeError from the request handlers
    those passes call into. The message is user-facing: it is appended to "Attempted to deserialize
    a Flow 'X'. ", so phrase it as a sentence completing that thought.
    """


@dataclass
class DeserializedFlowNodes:
    """Nodes rebuilt from one Flow and everything below it, keyed the two ways callers need.

    Connections are serialized against a Flow's whole subtree, not just its own level, so the
    connection pass has to resolve UUIDs belonging to nodes that were rebuilt inside a subflow.
    Collecting the whole subtree first is what lets a single pass wire them all up.

    Flow names are collected for the same reason node names are: a group records the name of its
    subflow, and both names are deduped on the way back in, so a group rebuilt into a session that
    already holds one of the same name has to be pointed at the subflow this load actually created.
    """

    node_uuid_to_node_name: dict[SerializedNodeCommands.NodeUUID, str] = field(default_factory=dict)
    original_name_to_node_name: dict[str, str] = field(default_factory=dict)
    original_flow_name_to_flow_name: dict[str, str] = field(default_factory=dict)

    def merge(self, other: DeserializedFlowNodes) -> None:
        """Fold a subflow's rebuilt nodes into this one.

        Args:
            other: The nodes rebuilt from a subflow of this Flow
        """
        self.node_uuid_to_node_name.update(other.node_uuid_to_node_name)
        self.original_name_to_node_name.update(other.original_name_to_node_name)
        self.original_flow_name_to_flow_name.update(other.original_flow_name_to_flow_name)


class FlowSerialization(EngineScoped):
    """Serialize flows to commands and rebuild them, including commands saved in image metadata."""

    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        event_manager.register_request_handlers(self)

    def _aggregate_flow_dependencies(
        self, serialized_node_commands: list[SerializedNodeCommands], sub_flows_commands: list[SerializedFlowCommands]
    ) -> NodeDependencies:
        """Aggregate dependencies from nodes and sub-flows into a single NodeDependencies object.

        Args:
            serialized_node_commands: List of serialized node commands to aggregate from
            sub_flows_commands: List of sub-flow commands to aggregate from

        Returns:
            NodeDependencies object with all dependencies merged
        """
        # Start with empty dependencies and aggregate into it
        aggregated_deps = NodeDependencies()

        # Aggregate dependencies from all nodes
        for node_cmd in serialized_node_commands:
            aggregated_deps.aggregate_from(node_cmd.node_dependencies)

        # Aggregate dependencies from all sub-flows
        for sub_flow_cmd in sub_flows_commands:
            aggregated_deps.aggregate_from(sub_flow_cmd.node_dependencies)

        return aggregated_deps

    def _aggregate_node_types_used(
        self, serialized_node_commands: list[SerializedNodeCommands], sub_flows_commands: list[SerializedFlowCommands]
    ) -> set[LibraryNameAndNodeType]:
        """Aggregate node types used from nodes and sub-flows.

        Args:
            serialized_node_commands: List of serialized node commands to aggregate from
            sub_flows_commands: List of sub-flow commands to aggregate from

        Returns:
            Set of LibraryNameAndNodeType with all node types used

        Raises:
            ValueError: If a node command has no library name specified
        """
        node_types_used: set[LibraryNameAndNodeType] = set()

        # Collect node types from all nodes in this flow
        for node_cmd in serialized_node_commands:
            create_cmd = node_cmd.create_node_command
            node_type = create_cmd.node_type
            library_name = create_cmd.specific_library_name
            if library_name is None:
                msg = f"Node type '{node_type}' has no library name specified during serialization"
                raise ValueError(msg)
            node_types_used.add(LibraryNameAndNodeType(library_name=library_name, node_type=node_type))

        # Aggregate node types from all sub-flows
        for sub_flow_cmd in sub_flows_commands:
            node_types_used.update(sub_flow_cmd.node_types_used)

        return node_types_used

    def _aggregate_connections(
        self,
        flow_connections: list[SerializedFlowCommands.IndirectConnectionSerialization],
        sub_flows_commands: list[SerializedFlowCommands],
    ) -> list[SerializedFlowCommands.IndirectConnectionSerialization]:
        """Aggregate connections from this flow and all sub-flows into a single list.

        Args:
            flow_connections: List of connections from the current flow
            sub_flows_commands: List of sub-flow commands to aggregate from

        Returns:
            List of all connections from this flow and all sub-flows combined
        """
        aggregated_connections = list(flow_connections)

        # Aggregate connections from all sub-flows
        for sub_flow_cmd in sub_flows_commands:
            aggregated_connections.extend(sub_flow_cmd.serialized_connections)

        return aggregated_connections

    def _aggregate_unique_parameter_values(
        self,
        unique_parameter_uuid_to_values: dict[SerializedNodeCommands.UniqueParameterValueUUID, Any],
        sub_flows_commands: list[SerializedFlowCommands],
    ) -> dict[SerializedNodeCommands.UniqueParameterValueUUID, Any]:
        """Aggregate unique parameter values from this flow and all sub-flows.

        Args:
            unique_parameter_uuid_to_values: Unique parameter values from current flow
            sub_flows_commands: List of sub-flow commands to aggregate from

        Returns:
            Dictionary with all unique parameter values merged
        """
        aggregated_values = dict(unique_parameter_uuid_to_values)

        # Merge unique values from all sub-flows
        for sub_flow_cmd in sub_flows_commands:
            aggregated_values.update(sub_flow_cmd.unique_parameter_uuid_to_values)

        return aggregated_values

    def _serialize_variables_for_flow(
        self,
        flow_name: str,
        unique_parameter_uuid_to_values: dict[SerializedNodeCommands.UniqueParameterValueUUID, Any],
        variable_references: set[VariableReference],
    ) -> list[SerializedFlowCommands.SerializedVariableCommand]:
        """Build SerializedVariableCommands for flow-scoped variables owned by this flow and declared by a node.

        Only variables that are:
          (a) owned by ``flow_name``, AND
          (b) named by at least one declared ``VariableReference`` after scope resolution
        get serialized. Variables present in engine state but not claimed by any node
        (orphans from deleted nodes) are silently dropped.

        Variable values share the parameter value pool in ``unique_parameter_uuid_to_values`` — the
        generated workflow script references both kinds through the same ``top_level_unique_values_dict``.

        Args:
            flow_name: The flow whose flow-scoped variables are being serialized.
            unique_parameter_uuid_to_values: Shared unique-value pool; variable values are added in place.
            variable_references: Aggregated set of references declared by nodes in this flow and its subtree.

        Returns:
            A list of ``SerializedVariableCommand``s for this flow's declared flow-scoped variables.
        """
        declared_names_for_this_flow = self._resolve_declared_variable_names_for_flow(
            flow_name=flow_name, variable_references=variable_references
        )
        if not declared_names_for_this_flow:
            return []

        flow_list_result = self.engine.handle_request(
            ListVariablesRequest(starting_flow=flow_name, lookup_scope=VariableScope.CURRENT_FLOW_ONLY)
        )
        if not isinstance(flow_list_result, ListVariablesResultSuccess):
            logger.warning(
                "Attempted to list flow-scoped variables for flow '%s' during serialization. Failed with: %s. Skipping.",
                flow_name,
                flow_list_result.result_details,
            )
            return []

        return [
            self._build_serialized_variable_command(
                variable=variable,
                unique_parameter_uuid_to_values=unique_parameter_uuid_to_values,
            )
            for variable in flow_list_result.variables
            if variable.name in declared_names_for_this_flow
        ]

    def _resolve_declared_variable_names_for_flow(
        self,
        flow_name: str,
        variable_references: set[VariableReference],
    ) -> set[str]:
        """Return the names of variables owned by ``flow_name`` that are claimed by a declared reference.

        This pass serializes *flow-owned* variables into the workflow script. References with
        other scopes persist through separate stories (globals are a deferred follow-up;
        project layer entries live in the project template, not the workflow) and are skipped
        here with a debug log.

        Resolution rules per reference scope:
          - CURRENT_FLOW_ONLY: claims ``ref.name`` only if a variable with that name exists in ``flow_name``.
          - HIERARCHICAL: uses ``GetVariableRequest`` to resolve; claims ``ref.name`` only if the resolved
            variable is owned by ``flow_name`` (ancestor-owned variables get claimed by the ancestor's
            own serialization pass).
          - PROJECT_ONLY / HIERARCHICAL_FROM_PROJECT / GLOBAL_ONLY / ALL: not this pass's responsibility;
            skipped with a debug log.
        """
        claimed_names: set[str] = set()

        for ref in variable_references:
            match ref.scope:
                case VariableScope.CURRENT_FLOW_ONLY:
                    if self._variable_is_owned_by(name=ref.name, flow_name=flow_name):
                        claimed_names.add(ref.name)
                case VariableScope.HIERARCHICAL:
                    owning_flow = self._hierarchical_owning_flow_for(name=ref.name, starting_flow=flow_name)
                    if owning_flow == flow_name:
                        claimed_names.add(ref.name)
                case (
                    VariableScope.PROJECT_ONLY
                    | VariableScope.HIERARCHICAL_FROM_PROJECT
                    | VariableScope.GLOBAL_ONLY
                    | VariableScope.ALL
                ):
                    logger.debug(
                        "Skipping variable reference '%s' with scope '%s' during flow serialization.",
                        ref.name,
                        ref.scope,
                    )
                case _:
                    msg = (
                        f"Attempted to serialize variable reference '{ref.name}' during flow "
                        f"serialization. Failed due to unknown scope '{ref.scope}'."
                    )
                    raise ValueError(msg)

        return claimed_names

    def _variable_is_owned_by(self, name: str, flow_name: str) -> bool:
        """Return True if a flow-scoped variable with ``name`` exists directly in ``flow_name``."""
        list_result = self.engine.handle_request(
            ListVariablesRequest(starting_flow=flow_name, lookup_scope=VariableScope.CURRENT_FLOW_ONLY)
        )
        if not isinstance(list_result, ListVariablesResultSuccess):
            logger.debug(
                "Attempted variable ownership check for '%s' in flow '%s'. Failed to list variables: %s",
                name,
                flow_name,
                list_result.result_details,
            )
            return False
        return any(variable.name == name for variable in list_result.variables)

    def _hierarchical_owning_flow_for(self, name: str, starting_flow: str) -> str | None:
        """Return the owning flow name of a variable found by hierarchical lookup, or None if not found."""
        result = self.engine.handle_request(
            GetVariableRequest(name=name, lookup_scope=VariableScope.HIERARCHICAL, starting_flow=starting_flow)
        )
        if not isinstance(result, GetVariableResultSuccess):
            # A failure here is the expected signal for "variable does not resolve hierarchically" —
            # e.g. a declared reference whose target was deleted. Logged at debug so it is available
            # for diagnosis without generating noise during normal saves.
            logger.debug(
                "Hierarchical lookup for variable '%s' starting at flow '%s' did not resolve: %s",
                name,
                starting_flow,
                result.result_details,
            )
            return None
        return result.variable.owning_flow_name

    def _build_serialized_variable_command(
        self,
        variable: FlowVariable,
        unique_parameter_uuid_to_values: dict[SerializedNodeCommands.UniqueParameterValueUUID, Any],
    ) -> SerializedFlowCommands.SerializedVariableCommand:
        """Pool the variable's encoded value under a hash of its content and build the indirect command."""
        encoded = try_encode(variable.value)
        if isinstance(encoded, Unencodable):
            logger.warning(
                "Attempted to save variable '%s'. Failed because %s It will reopen with no value.",
                variable.name,
                encoded.reason,
            )
            encoded = None
        unique_value_uuid = SerializedNodeCommands.UniqueParameterValueUUID(value_key(encoded))
        unique_parameter_uuid_to_values[unique_value_uuid] = encoded

        create_variable_command = CreateVariableRequest(
            name=variable.name,
            type=variable.type,
            is_global=False,
            value=None,  # Overridden at deserialization via top_level_unique_values_dict lookup.
            owning_flow=variable.owning_flow_name,
            initial_setup=True,
        )
        return SerializedFlowCommands.SerializedVariableCommand(
            create_variable_command=create_variable_command,
            unique_value_uuid=unique_value_uuid,
        )

    def _aggregate_set_parameter_value_commands(
        self,
        set_parameter_value_commands: dict[
            SerializedNodeCommands.NodeUUID, list[SerializedNodeCommands.IndirectSetParameterValueCommand]
        ],
        sub_flows_commands: list[SerializedFlowCommands],
    ) -> dict[SerializedNodeCommands.NodeUUID, list[SerializedNodeCommands.IndirectSetParameterValueCommand]]:
        """Aggregate set parameter value commands from this flow and all sub-flows.

        Args:
            set_parameter_value_commands: Set parameter value commands from current flow
            sub_flows_commands: List of sub-flow commands to aggregate from

        Returns:
            Dictionary with all set parameter value commands merged
        """
        aggregated_commands = dict(set_parameter_value_commands)

        # Merge commands from all sub-flows
        for sub_flow_cmd in sub_flows_commands:
            for node_uuid, commands in sub_flow_cmd.set_parameter_value_commands.items():
                if node_uuid in aggregated_commands:
                    aggregated_commands[node_uuid].extend(commands)
                else:
                    aggregated_commands[node_uuid] = list(commands)

        return aggregated_commands

    # TODO: https://github.com/griptape-ai/griptape-nodes/issues/861
    # similar manager refactors: https://github.com/griptape-ai/griptape-nodes/issues/806
    @handles(SerializeFlowToCommandsRequest)
    def on_serialize_flow_to_commands(self, request: SerializeFlowToCommandsRequest) -> ResultPayload:  # noqa: C901, PLR0911, PLR0912, PLR0915
        flow_name = request.flow_name
        flow = None
        if flow_name is None:
            if self.engine.context_manager.has_current_flow():
                flow = self.engine.context_manager.get_current_flow()
                flow_name = flow.name
            else:
                details = "Attempted to serialize a Flow to commands from the Current Context. Failed because the Current Context was empty."
                return SerializeFlowToCommandsResultFailure(result_details=details)
        if flow is None:
            # Does this flow exist?
            flow = self.engine.object_manager.attempt_get_object_by_name_as_type(flow_name, ControlFlow)
            if flow is None:
                details = (
                    f"Attempted to serialize Flow '{flow_name}' to commands, but no Flow with that name could be found."
                )
                return SerializeFlowToCommandsResultFailure(result_details=details)

        # Track all parameter values that were in use by these Nodes (maps UUID to Parameter value)
        unique_parameter_uuid_to_values = {}
        # And track how values map into that map.
        serialized_parameter_value_tracker = SerializedParameterValueTracker()

        with self.engine.context_manager.flow(flow):
            # The base flow creation, if desired.
            if request.include_create_flow_command:
                # Check if this flow is a referenced workflow
                if self.engine.flow_manager.is_referenced_workflow(flow):
                    referenced_workflow_name = self.engine.flow_manager.get_referenced_workflow_name(flow)
                    create_flow_request = ImportWorkflowAsReferencedSubFlowRequest(
                        workflow_name=referenced_workflow_name,  # type: ignore[arg-type] # is_referenced_workflow() guarantees this is not None
                        imported_flow_metadata=flow.metadata,
                    )
                else:
                    # Always set set_as_new_context=False during serialization - let the workflow manager
                    # that loads this serialized flow decide whether to push it to context or not
                    # Get parent flow name from the flow manager's tracking
                    parent_name = self.engine.flow_manager.get_parent_flow(flow_name)
                    create_flow_request = CreateFlowRequest(
                        flow_name=flow_name,
                        parent_flow_name=parent_name,
                        set_as_new_context=False,
                        metadata=flow.metadata,
                    )
            else:
                create_flow_request = None

            serialized_node_commands = []
            serialized_node_group_commands = []  # SubflowNodeGroups must be added LAST
            set_parameter_value_commands_per_node = {}  # Maps a node UUID to a list of set parameter value commands
            set_lock_commands_per_node = {}  # Maps a node UUID to a set Lock command, if it exists.

            # Now each of the child nodes in the flow.
            node_name_to_uuid = {}
            nodes_in_flow_request = ListNodesInFlowRequest()
            nodes_in_flow_result = self.engine.handle_request(nodes_in_flow_request)
            if not isinstance(nodes_in_flow_result, ListNodesInFlowResultSuccess):
                details = (
                    f"Attempted to serialize Flow '{flow_name}'. Failed while attempting to list Nodes in the Flow."
                )
                return SerializeFlowToCommandsResultFailure(result_details=details)

            # Serialize each node
            for node_name in nodes_in_flow_result.node_names:
                node = self.engine.object_manager.attempt_get_object_by_name_as_type(node_name, BaseNode)
                if node is None:
                    details = f"Attempted to serialize Flow '{flow_name}'. Failed while attempting to serialize Node '{node_name}' within the Flow."
                    return SerializeFlowToCommandsResultFailure(result_details=details)

                with self.engine.context_manager.node(node):
                    # Note: the parameter value stuff is pass-by-reference, and we expect the values to be modified in place.
                    # This might be dangerous if done over the wire.

                    # We set include_existing_subflow_in_group to true here, because we're serializing a whole flow, which means the subflows are being serialized
                    serialize_node_request = SerializeNodeToCommandsRequest(
                        unique_parameter_uuid_to_values=unique_parameter_uuid_to_values,  # Unique values
                        serialized_parameter_value_tracker=serialized_parameter_value_tracker,  # Mapping values to UUIDs,
                        include_existing_subflow_in_group=True,
                    )
                    serialize_node_result = self.engine.handle_request(serialize_node_request)
                    if not isinstance(serialize_node_result, SerializeNodeToCommandsResultSuccess):
                        details = f"Attempted to serialize Flow '{flow_name}'. Failed while attempting to serialize Node '{node_name}' within the Flow."
                        return SerializeFlowToCommandsResultFailure(result_details=details)

                    serialized_node = serialize_node_result.serialized_node_commands

                    # Store the serialized node's UUID for correlation to connections and setting parameter values later.
                    node_name_to_uuid[node_name] = serialized_node.node_uuid

                    # BaseNodeGroups must be serialized LAST because CreateNodeGroupRequest references child node names
                    # If we deserialize a NodeGroup before its children, the child nodes won't exist yet
                    if isinstance(node, BaseNodeGroup):
                        serialized_node_group_commands.append(serialized_node)
                    else:
                        serialized_node_commands.append(serialized_node)

                    # Get the list of set value commands for THIS node.
                    set_value_commands_list = serialize_node_result.set_parameter_value_commands
                    if serialize_node_result.serialized_node_commands.lock_node_command is not None:
                        set_lock_commands_per_node[serialized_node.node_uuid] = (
                            serialize_node_result.serialized_node_commands.lock_node_command
                        )
                    set_parameter_value_commands_per_node[serialized_node.node_uuid] = set_value_commands_list

            # Serialize sub-flows first, before creating connections.
            # We need the complete UUID map from all flows to handle cross-flow connections.
            parent_flow = self.engine.context_manager.get_current_flow()
            parent_flow_name = parent_flow.name
            flows_in_flow_request = ListFlowsInFlowRequest(parent_flow_name=parent_flow_name)
            flows_in_flow_result = self.engine.handle_request(flows_in_flow_request)
            if not isinstance(flows_in_flow_result, ListFlowsInFlowResultSuccess):
                details = f"Attempted to serialize Flow '{flow_name}'. Failed while attempting to list child Flows in the Flow."
                return SerializeFlowToCommandsResultFailure(result_details=details)

            sub_flow_commands = []
            for child_flow in flows_in_flow_result.flow_names:
                child_flow_obj = self.engine.object_manager.attempt_get_object_by_name_as_type(child_flow, ControlFlow)
                if child_flow_obj is None:
                    details = f"Attempted to serialize Flow '{flow_name}', but no Flow with that name could be found."
                    return SerializeFlowToCommandsResultFailure(result_details=details)

                # Skip transient child flows: they are runtime-only artifacts (e.g. a subflow a node
                # imports at execution time, or a per-iteration loop-body flow) and must not be
                # baked into the saved workflow.
                if child_flow_obj.metadata.get(TRANSIENT_KEY):
                    continue

                # Check if this is a referenced workflow
                if self.engine.flow_manager.is_referenced_workflow(child_flow_obj):
                    # For referenced workflows, create a minimal SerializedFlowCommands with just the import command
                    referenced_workflow_name = self.engine.flow_manager.get_referenced_workflow_name(child_flow_obj)
                    import_command = ImportWorkflowAsReferencedSubFlowRequest(
                        workflow_name=referenced_workflow_name,  # type: ignore[arg-type] # is_referenced_workflow() guarantees this is not None
                        imported_flow_metadata=child_flow_obj.metadata,
                    )

                    # Create NodeDependencies with just the referenced workflow
                    sub_flow_dependencies = NodeDependencies(
                        referenced_workflows={referenced_workflow_name}  # type: ignore[arg-type] # is_referenced_workflow() guarantees this is not None
                    )

                    serialized_flow = SerializedFlowCommands(
                        flow_initialization_command=import_command,
                        serialized_node_commands=[],
                        serialized_connections=[],
                        unique_parameter_uuid_to_values={},
                        set_parameter_value_commands={},
                        set_lock_commands_per_node={},
                        sub_flows_commands=[],
                        node_dependencies=sub_flow_dependencies,
                        node_types_used=set(),
                    )
                    sub_flow_commands.append(serialized_flow)
                else:
                    # For standalone sub-flows, use the existing recursive serialization.
                    with self.engine.context_manager.flow(flow=child_flow_obj):
                        child_flow_request = SerializeFlowToCommandsRequest()
                        child_flow_result = self.engine.handle_request(child_flow_request)
                        if not isinstance(child_flow_result, SerializeFlowToCommandsResultSuccess):
                            details = f"Attempted to serialize parent flow '{flow_name}'. Failed while serializing child flow '{child_flow}'."
                            return SerializeFlowToCommandsResultFailure(result_details=details)
                        serialized_flow = child_flow_result.serialized_flow_commands
                        sub_flow_commands.append(serialized_flow)

        # Append NodeGroup commands AFTER regular node commands
        # This ensures child nodes exist before their parent NodeGroups are created during deserialization
        serialized_node_commands.extend(serialized_node_group_commands)

        # Update BaseNodeGroup commands to use UUIDs instead of names in node_names_to_add
        # This allows workflow generation to directly look up variable names from UUIDs
        # Build a complete node name to UUID map including nodes from all subflows
        complete_node_name_to_uuid = dict(node_name_to_uuid)  # Start with current flow's nodes

        def collect_subflow_node_uuids(subflow_commands_list: list[SerializedFlowCommands]) -> None:
            """Recursively collect node name-to-UUID mappings from subflows."""
            for subflow_cmd in subflow_commands_list:
                for node_cmd in subflow_cmd.serialized_node_commands:
                    # Extract node name from the create command
                    create_cmd = node_cmd.create_node_command
                    if create_cmd.node_name:
                        complete_node_name_to_uuid[create_cmd.node_name] = node_cmd.node_uuid
                # Recursively process nested subflows
                if subflow_cmd.sub_flows_commands:
                    collect_subflow_node_uuids(subflow_cmd.sub_flows_commands)

        collect_subflow_node_uuids(sub_flow_commands)

        for node_group_command in serialized_node_group_commands:
            create_cmd = node_group_command.create_node_command

            if create_cmd.node_names_to_add:
                # Convert node names to UUIDs using the complete map (including subflows)
                node_uuids = []
                for child_node_name in create_cmd.node_names_to_add:
                    if child_node_name in complete_node_name_to_uuid:
                        uuid = complete_node_name_to_uuid[child_node_name]
                        node_uuids.append(uuid)
                # Replace the list with UUIDs (as strings since that's what the field expects)
                create_cmd.node_names_to_add = node_uuids

        # Now create the connections using the complete UUID map (includes all flows).
        # This must happen after subflows are serialized so we have all UUIDs available.
        create_connection_commands = []
        for connection in self.engine.flow_manager._get_connections_for_flow(flow):
            source_node_name = connection.source_node.name
            target_node_name = connection.target_node.name

            # Use the complete UUID map that includes nodes from all subflows
            if source_node_name not in complete_node_name_to_uuid:
                details = f"Attempted to serialize Flow '{flow_name}'. Connection source node '{source_node_name}' not found in UUID map."
                return SerializeFlowToCommandsResultFailure(result_details=details)
            if target_node_name not in complete_node_name_to_uuid:
                details = f"Attempted to serialize Flow '{flow_name}'. Connection target node '{target_node_name}' not found in UUID map."
                return SerializeFlowToCommandsResultFailure(result_details=details)

            source_node_uuid = complete_node_name_to_uuid[source_node_name]
            target_node_uuid = complete_node_name_to_uuid[target_node_name]
            create_connection_command = SerializedFlowCommands.IndirectConnectionSerialization(
                source_node_uuid=source_node_uuid,
                source_parameter_name=connection.source_parameter.name,
                target_node_uuid=target_node_uuid,
                target_parameter_name=connection.target_parameter.name,
            )
            create_connection_commands.append(create_connection_command)

        # Aggregate all dependencies from nodes and sub-flows
        aggregated_dependencies = self._aggregate_flow_dependencies(serialized_node_commands, sub_flow_commands)

        # Aggregate all node types used from nodes and sub-flows
        try:
            aggregated_node_types_used = self._aggregate_node_types_used(serialized_node_commands, sub_flow_commands)
        except ValueError as e:
            details = f"Attempted to serialize Flow '{flow_name}' to commands. Failed while aggregating node types: {e}"
            return SerializeFlowToCommandsResultFailure(result_details=details)

        # Aggregate unique parameter values from this flow and all sub-flows
        aggregated_unique_values = self._aggregate_unique_parameter_values(
            unique_parameter_uuid_to_values, sub_flow_commands
        )

        # Aggregate all connections from this flow and all sub-flows
        aggregated_connections = self._aggregate_connections(create_connection_commands, sub_flow_commands)

        # Serialize flow-scoped variables owned by this flow that are declared by a node.
        serialized_variable_commands = self._serialize_variables_for_flow(
            flow_name=flow_name,
            unique_parameter_uuid_to_values=aggregated_unique_values,
            variable_references=aggregated_dependencies.variable_references,
        )

        # Extract flow name from initialization command if available
        extracted_flow_name = None
        if create_flow_request is not None and hasattr(create_flow_request, "flow_name"):
            extracted_flow_name = create_flow_request.flow_name

        serialized_flow = SerializedFlowCommands(
            flow_initialization_command=create_flow_request,
            serialized_node_commands=serialized_node_commands,
            serialized_connections=aggregated_connections,
            unique_parameter_uuid_to_values=aggregated_unique_values,
            set_parameter_value_commands=set_parameter_value_commands_per_node,
            set_lock_commands_per_node=set_lock_commands_per_node,
            sub_flows_commands=sub_flow_commands,
            node_dependencies=aggregated_dependencies,
            node_types_used=aggregated_node_types_used,
            flow_name=extracted_flow_name,
            serialized_variable_commands=serialized_variable_commands,
        )
        details = f"Successfully serialized Flow '{flow_name}' into commands."
        result = SerializeFlowToCommandsResultSuccess(serialized_flow_commands=serialized_flow, result_details=details)
        return result

    @handles(DeserializeFlowFromCommandsRequest)
    def on_deserialize_flow_from_commands(self, request: DeserializeFlowFromCommandsRequest) -> ResultPayload:  # noqa: C901, PLR0911, PLR0912 (I am big and complicated and have a lot of negative edge-cases)
        # Do we want to create a NEW Flow to deserialize into, or use the one in the Current Context?
        if request.serialized_flow_commands.flow_initialization_command is None:
            if self.engine.context_manager.has_current_flow():
                flow = self.engine.context_manager.get_current_flow()
                flow_name = flow.name
                pushed_flow_context = False
                created_new_flow = False
            else:
                details = "Attempted to deserialize a set of Flow Creation commands into the Current Context. Failed because the Current Context was empty."
                return DeserializeFlowFromCommandsResultFailure(result_details=details)
        else:
            # Issue the creation command first.
            flow_initialization_command = request.serialized_flow_commands.flow_initialization_command
            flow_initialization_result = self.engine.handle_request(flow_initialization_command)

            # Handle different types of creation commands
            match flow_initialization_command:
                case CreateFlowRequest():
                    if not isinstance(flow_initialization_result, CreateFlowResultSuccess):
                        details = f"Attempted to deserialize a serialized set of Flow Creation commands. Failed to create flow '{flow_initialization_command.flow_name}'."
                        return DeserializeFlowFromCommandsResultFailure(result_details=details)
                    flow_name = flow_initialization_result.flow_name
                case ImportWorkflowAsReferencedSubFlowRequest():
                    if not isinstance(flow_initialization_result, ImportWorkflowAsReferencedSubFlowResultSuccess):
                        details = f"Attempted to deserialize a serialized set of Flow Creation commands. Failed to import workflow '{flow_initialization_command.workflow_name}'."
                        return DeserializeFlowFromCommandsResultFailure(result_details=details)
                    flow_name = flow_initialization_result.created_flow_name
                case _:
                    details = f"Attempted to deserialize Flow Creation commands with unknown command type: {type(flow_initialization_command).__name__}."
                    return DeserializeFlowFromCommandsResultFailure(result_details=details)

            # Adopt the newly-created flow as our current context.
            flow = self.engine.object_manager.attempt_get_object_by_name_as_type(flow_name, ControlFlow)
            if flow is None:
                details = f"Attempted to deserialize a serialized set of Flow Creation commands. Failed to find created flow '{flow_name}'."
                return DeserializeFlowFromCommandsResultFailure(result_details=details)
            self.engine.context_manager.push_flow(flow=flow)
            pushed_flow_context = True  # Mark that we pushed this flow
            created_new_flow = True  # Mark that we created this flow

        # Deserializing a flow goes in a specific order: every node in this Flow AND its subflows
        # first, then connections, then values. Connections are serialized against a Flow's whole
        # subtree (see _aggregate_connections), so an edge crossing a Flow boundary names a node
        # that lives one or more levels down. Connections are wired only after every node in the
        # subtree exists, subflows included, because any earlier and such an edge fails with "node
        # did not exist within the flow" and fails the whole load.
        try:
            deserialized_nodes = self._deserialize_nodes_for_flow_and_subflows(
                serialized_flow_commands=request.serialized_flow_commands
            )

            # Now apply the connections, for this Flow and every Flow beneath it.
            # We didn't know the exact name that would be used for the nodes, but we knew the node's
            # creation UUID. Tie the UUID back to the node names.
            #
            # The outermost Flow claims every edge, deliberately, and the recursion below it finds
            # nothing left to do: this Flow's serialized_connections is the fully aggregated list, and
            # each edge is created the first time it is seen. Codegen resolves the same duplication
            # the other way round -- it recurses first, so the innermost Flow wins -- because there
            # the edge has to be written where its endpoints are already in scope as variables. Here
            # every node in the subtree exists before any edge is made, so there is no such
            # constraint and claiming at the top is the simpler of the two.
            self._deserialize_connections_for_flow_and_subflows(
                serialized_flow_commands=request.serialized_flow_commands,
                deserialized_nodes=deserialized_nodes,
                created_connection_keys=set(),
            )

            # Now assign the values, again across the whole subtree.
            # This is the same issue that we handle for Connections:
            # we don't know the exact node name that would be used, but we do know the UUIDs.
            self._deserialize_values_for_flow_and_subflows(
                serialized_flow_commands=request.serialized_flow_commands, deserialized_nodes=deserialized_nodes
            )
        except FlowDeserializationError as err:
            details = f"Attempted to deserialize a Flow '{flow_name}'. {err}"
            if created_new_flow:
                self.engine.flow_manager._cleanup_flow_on_failed_deserialization(
                    flow_name, pushed_flow_context=pushed_flow_context
                )
            return DeserializeFlowFromCommandsResultFailure(result_details=details)

        node_name_mappings = deserialized_nodes.original_name_to_node_name

        # Pop flow context if requested and we pushed it
        if request.pop_flow_context_after and pushed_flow_context:
            self.engine.context_manager.pop_flow()

        details = f"Successfully deserialized Flow '{flow_name}'."
        return DeserializeFlowFromCommandsResultSuccess(
            flow_name=flow_name, result_details=details, node_name_mappings=node_name_mappings
        )

    def _deserialize_nodes_for_flow_and_subflows(
        self, serialized_flow_commands: SerializedFlowCommands
    ) -> DeserializedFlowNodes:
        """Rebuild every node in a Flow and, recursively, in each of its subflows.

        Each subflow is created and entered so its nodes land inside it, then left again, so the
        rebuilt Flow hierarchy mirrors the serialized one.

        Node groups are rebuilt last, after this Flow's ordinary nodes and after every subflow, for
        the same reason the workflow file generator emits them last: a group claims its members by
        name at creation time, and a member can live in a subflow below the group. Rebuilding the
        group first silently dropped members that did not exist yet, leaving an empty group.

        Args:
            serialized_flow_commands: The commands for the Flow being rebuilt

        Returns:
            The rebuilt nodes of this Flow and its whole subtree

        Raises:
            FlowDeserializationError: If a node, or a subflow holding nodes, could not be rebuilt
        """
        deserialized_nodes = DeserializedFlowNodes()

        regular_node_commands = [
            serialized_node
            for serialized_node in serialized_flow_commands.serialized_node_commands
            if not serialized_node.is_node_group
        ]
        node_group_commands = [
            serialized_node
            for serialized_node in serialized_flow_commands.serialized_node_commands
            if serialized_node.is_node_group
        ]

        for serialized_node in regular_node_commands:
            self._deserialize_and_record_node(serialized_node=serialized_node, deserialized_nodes=deserialized_nodes)

        for sub_flow_commands in serialized_flow_commands.sub_flows_commands:
            sub_flow_nodes = self._deserialize_subflow_nodes(sub_flow_commands=sub_flow_commands)
            deserialized_nodes.merge(sub_flow_nodes)

        for serialized_node in node_group_commands:
            self._deserialize_and_record_node(serialized_node=serialized_node, deserialized_nodes=deserialized_nodes)

        return deserialized_nodes

    def _deserialize_and_record_node(
        self, serialized_node: SerializedNodeCommands, deserialized_nodes: DeserializedFlowNodes
    ) -> None:
        """Rebuild one node and record the name it was given under both keys callers look it up by.

        Args:
            serialized_node: The commands for the node being rebuilt
            deserialized_nodes: The collection to record the rebuilt node in

        Raises:
            FlowDeserializationError: If the node could not be rebuilt
        """
        node_name = self._deserialize_one_node(serialized_node=serialized_node, deserialized_nodes=deserialized_nodes)
        deserialized_nodes.node_uuid_to_node_name[serialized_node.node_uuid] = node_name
        original_node_name = serialized_node.create_node_command.node_name
        if original_node_name is not None:
            # Callers map old names to new ones; a node saved without a name has nothing to map.
            deserialized_nodes.original_name_to_node_name[original_node_name] = node_name

    def _deserialize_one_node(
        self, serialized_node: SerializedNodeCommands, deserialized_nodes: DeserializedFlowNodes
    ) -> str:
        """Rebuild a single node, resolving any group membership it declares.

        Args:
            serialized_node: The commands for the node being rebuilt
            deserialized_nodes: Nodes rebuilt so far, used to resolve group membership UUIDs

        Returns:
            The name the rebuilt node was given

        Raises:
            FlowDeserializationError: If the node could not be rebuilt
        """
        create_cmd = serialized_node.create_node_command
        serialized_node_for_deserialization = serialized_node

        # A saved group names things by what they were called at save time, and every one of those
        # names is deduped on the way back in. Rewrite them to what this load actually created,
        # copying rather than mutating so the caller's serialized data is left as it was found.
        if serialized_node.is_node_group:
            create_cmd_copy = replace(
                create_cmd,
                node_names_to_add=self._remapped_group_member_names(
                    create_cmd=create_cmd, deserialized_nodes=deserialized_nodes
                ),
                metadata=self._remapped_group_metadata(create_cmd=create_cmd, deserialized_nodes=deserialized_nodes),
            )
            serialized_node_for_deserialization = replace(serialized_node, create_node_command=create_cmd_copy)

        deserialize_node_request = DeserializeNodeFromCommandsRequest(
            serialized_node_commands=serialized_node_for_deserialization
        )
        deserialized_node_result = self.engine.handle_request(deserialize_node_request)
        if not isinstance(deserialized_node_result, DeserializeNodeFromCommandsResultSuccess):
            msg = f"Failed while rebuilding the node '{create_cmd.node_name}'."
            raise FlowDeserializationError(msg)

        return deserialized_node_result.node_name

    def _remapped_group_member_names(
        self, create_cmd: CreateNodeRequest, deserialized_nodes: DeserializedFlowNodes
    ) -> list[str] | None:
        """Turn a saved group's member UUIDs into the names its members were rebuilt under.

        The field is declared as plain names, but a serialized group stores its members as UUIDs,
        because the names they will be rebuilt under are not known until they are rebuilt.

        Args:
            create_cmd: The group's creation command as it was saved
            deserialized_nodes: Nodes rebuilt so far, holding the UUID-to-name mapping

        Returns:
            The members' rebuilt names, or None if the group was saved holding nothing

        Raises:
            FlowDeserializationError: If any member was not rebuilt
        """
        if not create_cmd.node_names_to_add:
            return None

        member_uuids = [SerializedNodeCommands.NodeUUID(name) for name in create_cmd.node_names_to_add]
        unresolved_count = sum(
            1 for member_uuid in member_uuids if member_uuid not in deserialized_nodes.node_uuid_to_node_name
        )
        # A group IS its membership, so a group that comes back holding three of its five nodes is
        # worse than a load that stops and says so. Every member is rebuildable by the time we get
        # here because _deserialize_nodes_for_flow_and_subflows rebuilds groups last; if that
        # ordering ever regresses, fail loudly instead of quietly returning a hollowed-out group.
        if unresolved_count:
            group_name = create_cmd.node_name or create_cmd.node_type
            msg = f"Failed while rebuilding the group '{group_name}' because {unresolved_count} of its {len(member_uuids)} nodes were not rebuilt."
            raise FlowDeserializationError(msg)

        return [deserialized_nodes.node_uuid_to_node_name[member_uuid] for member_uuid in member_uuids]

    def _remapped_group_metadata(
        self, create_cmd: CreateNodeRequest, deserialized_nodes: DeserializedFlowNodes
    ) -> dict | None:
        """Point a saved group's `subflow_name` at the subflow this load created for it.

        A group records its subflow by name, and that name is deduped like any other on the way in:
        loading a graph into a session that already holds a same-named subflow creates the group's
        one under a new name. Left unremapped, the rebuilt group reaches for the name it was saved
        with, finds a flow belonging to something else, and moves the nodes it just loaded into it --
        with no error, because the flow it names does exist.

        Args:
            create_cmd: The group's creation command as it was saved
            deserialized_nodes: Rebuilt nodes, holding the saved-to-created flow name mapping

        Returns:
            Metadata with the subflow name rewritten, or the original if there is nothing to rewrite
        """
        saved_metadata = create_cmd.metadata
        if saved_metadata is None:
            return None

        saved_subflow_name = saved_metadata.get("subflow_name")
        if saved_subflow_name is None:
            # Copy/paste strips subflow_name so the pasted group builds a fresh subflow of its own.
            return saved_metadata

        created_subflow_name = deserialized_nodes.original_flow_name_to_flow_name.get(saved_subflow_name)
        if created_subflow_name is None:
            # The group's subflow was not part of this load. Its own on-demand creation path handles
            # that, and keeping a stale name would point it at whatever now answers to it.
            remapped_metadata = copy.deepcopy(saved_metadata)
            remapped_metadata.pop("subflow_name", None)
            return remapped_metadata

        if created_subflow_name == saved_subflow_name:
            return saved_metadata

        remapped_metadata = copy.deepcopy(saved_metadata)
        remapped_metadata["subflow_name"] = created_subflow_name
        return remapped_metadata

    def _deserialize_subflow_nodes(self, sub_flow_commands: SerializedFlowCommands) -> DeserializedFlowNodes:
        """Create one subflow and rebuild the nodes inside it, and inside its own subflows.

        Args:
            sub_flow_commands: The commands for the subflow being rebuilt

        Returns:
            The rebuilt nodes of the subflow and its whole subtree

        Raises:
            FlowDeserializationError: If the subflow could not be created or entered
        """
        flow_initialization_command = sub_flow_commands.flow_initialization_command
        if flow_initialization_command is None:
            # No creation command means "deserialize into the Flow already in context", which is
            # this Flow: the nodes belong here rather than in a child.
            return self._deserialize_nodes_for_flow_and_subflows(serialized_flow_commands=sub_flow_commands)

        sub_flow_name = self._create_flow_for_deserialization(flow_initialization_command=flow_initialization_command)
        sub_flow = self.engine.object_manager.attempt_get_object_by_name_as_type(sub_flow_name, ControlFlow)
        if sub_flow is None:
            msg = f"Failed to find the sub-flow '{sub_flow_name}' just created for it."
            raise FlowDeserializationError(msg)

        # Nodes are created into the Flow at the top of the context stack, so enter the subflow
        # while rebuilding its contents and leave it afterwards.
        with self.engine.context_manager.flow(flow=sub_flow):
            sub_flow_nodes = self._deserialize_nodes_for_flow_and_subflows(serialized_flow_commands=sub_flow_commands)

        # Record what this flow was saved as against what it came back as. A group node saved a
        # reference to its subflow by name, and the name is deduped on the way in, so the group has
        # to be pointed at the flow this load created rather than a same-named one already here.
        original_flow_name = self._original_flow_name_of(flow_initialization_command=flow_initialization_command)
        if original_flow_name is not None:
            sub_flow_nodes.original_flow_name_to_flow_name[original_flow_name] = sub_flow_name

        return sub_flow_nodes

    def _original_flow_name_of(
        self, flow_initialization_command: CreateFlowRequest | ImportWorkflowAsReferencedSubFlowRequest
    ) -> str | None:
        """The name a Flow was saved under, which is what a group's stored reference names it by.

        Args:
            flow_initialization_command: The command that brought the Flow into existence

        Returns:
            The saved name, or None if the command carries no name to map from
        """
        if isinstance(flow_initialization_command, CreateFlowRequest):
            return flow_initialization_command.flow_name
        if isinstance(flow_initialization_command, ImportWorkflowAsReferencedSubFlowRequest):
            # An imported workflow is referenced by workflow name, not by a flow name a group
            # could have recorded, so there is nothing to map.
            return None
        return None

    def _create_flow_for_deserialization(
        self, flow_initialization_command: CreateFlowRequest | ImportWorkflowAsReferencedSubFlowRequest
    ) -> str:
        """Issue a Flow's initialization command and return the name it was created under.

        Args:
            flow_initialization_command: The command that brings the Flow into existence

        Returns:
            The name of the created Flow

        Raises:
            FlowDeserializationError: If the Flow could not be created
        """
        flow_initialization_result = self.engine.handle_request(flow_initialization_command)

        match flow_initialization_command:
            case CreateFlowRequest():
                if not isinstance(flow_initialization_result, CreateFlowResultSuccess):
                    msg = f"Failed to create the sub-flow '{flow_initialization_command.flow_name}'."
                    raise FlowDeserializationError(msg)
                return flow_initialization_result.flow_name
            case ImportWorkflowAsReferencedSubFlowRequest():
                if not isinstance(flow_initialization_result, ImportWorkflowAsReferencedSubFlowResultSuccess):
                    msg = f"Failed to import the workflow '{flow_initialization_command.workflow_name}' as a sub-flow."
                    raise FlowDeserializationError(msg)
                return flow_initialization_result.created_flow_name
            case _:
                msg = f"Failed because the sub-flow used an unknown creation command: {type(flow_initialization_command).__name__}."
                raise FlowDeserializationError(msg)

        msg = "Failed to create flow during deserialization due to an unexpected control flow path."
        raise FlowDeserializationError(msg)

    def _deserialize_connections_for_flow_and_subflows(
        self,
        serialized_flow_commands: SerializedFlowCommands,
        deserialized_nodes: DeserializedFlowNodes,
        created_connection_keys: set[SerializedConnectionKey],
    ) -> None:
        """Recreate the connections of a Flow and every Flow beneath it, each exactly once.

        Serialization aggregates every subflow's connections up into each of its ancestors (see
        _aggregate_connections), so an edge crossing N group boundaries is reported N+1 times. It
        has to be created only once: re-issuing an existing edge is not a no-op, because
        on_create_connection_request treats a duplicate as a restricted scenario and tears the
        existing edge down before rebuilding it, which churns the group proxy parameters that the
        edge was routed through. Claiming each edge the first time it is seen keeps the load path
        from undoing the wiring it just restored.

        Args:
            serialized_flow_commands: The commands for the Flow being rebuilt
            deserialized_nodes: Every node rebuilt for this Flow's whole subtree
            created_connection_keys: Edges already created for this Flow's subtree, added to here

        Raises:
            FlowDeserializationError: If a connection names a node that was not rebuilt, or could not be made
        """
        for indirect_connection in serialized_flow_commands.serialized_connections:
            source_node_name = deserialized_nodes.node_uuid_to_node_name.get(indirect_connection.source_node_uuid)
            if source_node_name is None:
                msg = "Failed while reconnecting the flow because the node a connection starts from was not rebuilt."
                raise FlowDeserializationError(msg)
            target_node_name = deserialized_nodes.node_uuid_to_node_name.get(indirect_connection.target_node_uuid)
            if target_node_name is None:
                msg = "Failed while reconnecting the flow because the node a connection leads to was not rebuilt."
                raise FlowDeserializationError(msg)

            connection_key = indirect_connection.key()
            if connection_key in created_connection_keys:
                continue
            created_connection_keys.add(connection_key)

            create_connection_request = CreateConnectionRequest(
                source_node_name=source_node_name,
                source_parameter_name=indirect_connection.source_parameter_name,
                target_node_name=target_node_name,
                target_parameter_name=indirect_connection.target_parameter_name,
            )
            create_connection_result = self.engine.handle_request(create_connection_request)
            if create_connection_result.failed():
                msg = f"Failed while reconnecting '{source_node_name}.{indirect_connection.source_parameter_name}' to '{target_node_name}.{indirect_connection.target_parameter_name}'."
                raise FlowDeserializationError(msg)

        for sub_flow_commands in serialized_flow_commands.sub_flows_commands:
            self._deserialize_connections_for_flow_and_subflows(
                serialized_flow_commands=sub_flow_commands,
                deserialized_nodes=deserialized_nodes,
                created_connection_keys=created_connection_keys,
            )

    def _deserialize_values_for_flow_and_subflows(
        self, serialized_flow_commands: SerializedFlowCommands, deserialized_nodes: DeserializedFlowNodes
    ) -> None:
        """Reapply the parameter values of a Flow and every Flow beneath it.

        Args:
            serialized_flow_commands: The commands for the Flow being rebuilt
            deserialized_nodes: Every node rebuilt for this Flow's whole subtree

        Raises:
            FlowDeserializationError: If a value belongs to a node that was not rebuilt, or could not be set
        """
        for node_uuid, set_value_command_list in serialized_flow_commands.set_parameter_value_commands.items():
            node_name = deserialized_nodes.node_uuid_to_node_name.get(node_uuid)
            if node_name is None:
                msg = "Failed while restoring saved values because the node they belong to was not rebuilt."
                raise FlowDeserializationError(msg)
            node = self.engine.object_manager.attempt_get_object_by_name_as_type(node_name, BaseNode)
            if node is None:
                msg = f"Failed while restoring the saved values of '{node_name}'."
                raise FlowDeserializationError(msg)

            with self.engine.context_manager.node(node=node):
                for indirect_set_value_command in set_value_command_list:
                    self._apply_deserialized_parameter_value(
                        indirect_set_value_command=indirect_set_value_command,
                        node=node,
                        unique_parameter_uuid_to_values=serialized_flow_commands.unique_parameter_uuid_to_values,
                    )

        for sub_flow_commands in serialized_flow_commands.sub_flows_commands:
            self._deserialize_values_for_flow_and_subflows(
                serialized_flow_commands=sub_flow_commands, deserialized_nodes=deserialized_nodes
            )

    def _apply_deserialized_parameter_value(
        self,
        indirect_set_value_command: SerializedNodeCommands.IndirectSetParameterValueCommand,
        node: BaseNode,
        unique_parameter_uuid_to_values: dict[SerializedNodeCommands.UniqueParameterValueUUID, Any],
    ) -> None:
        """Set one saved parameter value on a rebuilt node.

        Args:
            indirect_set_value_command: The saved assignment, referencing its value by UUID
            node: The node the value belongs to
            unique_parameter_uuid_to_values: The pool the value UUID resolves against

        Raises:
            FlowDeserializationError: If the value is missing from the pool, or could not be set
        """
        parameter_name = indirect_set_value_command.set_parameter_value_command.parameter_name
        unique_value_uuid = indirect_set_value_command.unique_value_uuid
        if unique_value_uuid not in unique_parameter_uuid_to_values:
            msg = f"Failed while restoring the saved value of '{node.name}.{parameter_name}' because the value was missing."
            raise FlowDeserializationError(msg)

        # Call the SetParameterValueRequest, subbing in a freshly decoded copy of the pooled value.
        indirect_set_value_command.set_parameter_value_command.value = decode_value(
            unique_parameter_uuid_to_values[unique_value_uuid]
        )
        # Update the parameter value command to have the correct name.
        indirect_set_value_command.set_parameter_value_command.node_name = node.name
        set_parameter_value_result = self.engine.handle_request(indirect_set_value_command.set_parameter_value_command)
        if set_parameter_value_result.failed():
            msg = (
                f"Failed while restoring the saved value of '{node.name}.{parameter_name}'. "
                f"{set_parameter_value_result.result_details}"
            )
            raise FlowDeserializationError(msg)

    @handles(ExtractFlowCommandsFromImageMetadataRequest)
    def on_extract_flow_commands_from_image_metadata(  # noqa: PLR0911, C901
        self, request: ExtractFlowCommandsFromImageMetadataRequest
    ) -> ResultPayload:
        """Extract flow commands from PNG image metadata.

        Args:
            request: ExtractFlowCommandsFromImageMetadataRequest with file path or URL

        Returns:
            ExtractFlowCommandsFromImageMetadataResultSuccess if successful
            ExtractFlowCommandsFromImageMetadataResultFailure if any step fails
        """
        file_url_or_path = request.file_url_or_path

        # Check if input is a URL or file path
        is_url = file_url_or_path.startswith(("http://", "https://"))

        if is_url:
            # Handle URL: download the image
            try:
                response = httpx2.get(file_url_or_path, timeout=30.0)
                response.raise_for_status()
                pil_image = Image.open(BytesIO(response.content))
            except Exception as e:
                return ExtractFlowCommandsFromImageMetadataResultFailure(
                    result_details=f"Failed to download or open image from URL: {e}",
                    file_path=file_url_or_path,
                )
        else:
            # Handle file path: validate and open
            path_obj = Path(file_url_or_path)
            if not path_obj.exists():
                return ExtractFlowCommandsFromImageMetadataResultFailure(
                    result_details=f"File not found: {file_url_or_path}",
                    file_path=file_url_or_path,
                )

            if not path_obj.is_file():
                return ExtractFlowCommandsFromImageMetadataResultFailure(
                    result_details=f"Path is not a file: {file_url_or_path}",
                    file_path=file_url_or_path,
                )

            # Read PNG image and extract metadata
            try:
                pil_image = Image.open(file_url_or_path)
            except Exception as e:
                return ExtractFlowCommandsFromImageMetadataResultFailure(
                    result_details=f"Failed to open image file: {e}",
                    file_path=file_url_or_path,
                )

        # An image without embedded flow commands is a valid state, not an error.
        metadata = pil_image.info if hasattr(pil_image, "info") else {}
        # Closed now: a failure reading the commands can keep this frame, and the open file, alive
        # until garbage collection, and Windows won't delete or replace an open file.
        pil_image.close()
        if not metadata:
            return ExtractFlowCommandsFromImageMetadataResultSuccess(
                result_details=f"Image has no metadata: {file_url_or_path}",
                serialized_flow_commands=None,
                altered_workflow_state=False,
            )

        metadata_keys = [str(key) for key in metadata]

        if FLOW_COMMANDS_KEY not in metadata:
            return ExtractFlowCommandsFromImageMetadataResultSuccess(
                result_details=f"No flow commands metadata found in image. Available keys: {metadata_keys}",
                serialized_flow_commands=None,
                altered_workflow_state=False,
            )

        try:
            serialized_flow_commands = self._read_image_flow_commands(metadata[FLOW_COMMANDS_KEY])
        except ImageFlowCommandsError as error:
            return ExtractFlowCommandsFromImageMetadataResultFailure(
                result_details=f"Attempted to read the workflow saved in image '{file_url_or_path}'. Failed because {error}.",
                file_path=file_url_or_path,
            )

        # Check if we should also deserialize the flow
        if request.deserialize:
            # Deserialize the flow
            deserialize_request = DeserializeFlowFromCommandsRequest(serialized_flow_commands=serialized_flow_commands)
            deserialize_result = self.engine.handle_request(deserialize_request)

            # Validation: Check if deserialization succeeded
            if not isinstance(deserialize_result, DeserializeFlowFromCommandsResultSuccess):
                return ExtractFlowCommandsFromImageMetadataResultFailure(
                    result_details=f"Failed to deserialize flow from image metadata: {deserialize_result.result_details}",
                    file_path=file_url_or_path,
                )

            # Success path: Return with deserialization info
            return ExtractFlowCommandsFromImageMetadataResultSuccess(
                result_details=f"Successfully extracted and deserialized flow '{deserialize_result.flow_name}' from image metadata",
                serialized_flow_commands=serialized_flow_commands,
                flow_name=deserialize_result.flow_name,
                node_name_mappings=deserialize_result.node_name_mappings,
                altered_workflow_state=True,
            )

        # Success path: Return the extracted commands for user inspection (no deserialization)
        return ExtractFlowCommandsFromImageMetadataResultSuccess(
            result_details="Successfully extracted flow commands from image metadata",
            serialized_flow_commands=serialized_flow_commands,
            altered_workflow_state=False,
        )

    def _read_image_flow_commands(self, text: str) -> SerializedFlowCommands:
        """Read the flow commands an image's metadata holds as JSON, or as pickle from earlier engines.

        Raises:
            ImageFlowCommandsError: The text holds no readable flow commands.
        """
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            try:
                return read_legacy_image_flow_commands(
                    text, self.engine.library_manager.module_loading.stable_module_names()
                )
            except LegacyPickleError as error:
                raise ImageFlowCommandsError(str(error)) from error
        try:
            return decode_commands(data, SerializedFlowCommands)
        except CommandsFormatError as error:
            raise ImageFlowCommandsError(str(error)) from error
