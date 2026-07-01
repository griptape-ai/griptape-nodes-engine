from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, NamedTuple, NewType
from uuid import uuid4

from griptape_nodes.exe_types.core_types import NodeMessagePayload
from griptape_nodes.exe_types.node_types import NodeDependencies, NodeResolutionState
from griptape_nodes.retained_mode.events.base_events import (
    RequestPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    WorkflowAlteredMixin,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.connection_events import (
    IncomingConnection,
    ListConnectionsForNodeResultSuccess,
    OutgoingConnection,
)
from griptape_nodes.retained_mode.events.parameter_events import (
    GetParameterDetailsResultSuccess,
    GetParameterValueResultSuccess,
    SetParameterValueRequest,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry


class NewPosition(NamedTuple):
    """The X and Y position for the node to be copied to. Updates in the node metadata."""

    x: float
    y: float

    @classmethod
    def _cattrs_structure(cls, data: Any, _type: type) -> NewPosition:
        """Custom cattrs structuring hook.

        The editor sends positions as plain arrays (e.g. [976.66, 760]) with
        values that may be ints or floats. The default cattrs NamedTuple
        structuring cannot resolve the float type annotation because this
        module uses `from __future__ import annotations`, which turns
        annotations into ForwardRef strings. This hook handles both list and
        dict inputs and explicitly coerces values to float.
        """
        if isinstance(data, dict):
            return cls(x=float(data["x"]), y=float(data["y"]))
        return cls(x=float(data[0]), y=float(data[1]))


@dataclass
@PayloadRegistry.register
class CreateNodeRequest(RequestPayload):
    """Create a new node in a workflow.

    Use when: Building workflows programmatically, responding to user requests ("add a CSV reader"),
    loading saved workflows. Validates node type exists, generates unique name if needed.

    Args:
        node_type: Class name of the node to create
        specific_library_name: Library to search for the node type (None for any library)
        node_name: Desired name for the node (None for auto-generated)
        override_parent_flow_name: Flow to create the node in (None for current context)
        metadata: Initial metadata for the node (position, display properties)
        resolution: Initial resolution state (defaults to UNRESOLVED)
        initial_setup: Skip setup work when loading from file (defaults to False)
        set_as_new_context: Set this node as current context after creation (defaults to False)
        create_error_proxy_on_failure: Create Error Proxy node if creation fails (defaults to True)
        node_names_to_add: List of existing node names to add to this node after creation (used by SubflowNodeGroup, defaults to None)
        subflow_name: Subflow name for node groups (if None, a fresh one will be created; used by SubflowNodeGroup, defaults to None)
        parent_group_name: Name of the group node to add this node to after creation (defaults to None)

    Results: CreateNodeResultSuccess (with assigned name) | CreateNodeResultFailure (invalid type, missing library, flow not found)
    """

    node_type: str
    specific_library_name: str | None = None
    node_name: str | None = None
    # If None is passed, assumes we're using the flow in the Current Context
    override_parent_flow_name: str | None = None
    metadata: dict | None = None
    resolution: str = NodeResolutionState.UNRESOLVED.value
    # initial_setup prevents unnecessary work when we are loading a workflow from a file.
    initial_setup: bool = False
    # When True, this Node will be pushed as the current Node within the Current Context.
    set_as_new_context: bool = False
    # When True, create an Error Proxy node if the requested node type fails to create
    create_error_proxy_on_failure: bool = True
    # List of node names to add to this node after creation (used by SubflowNodeGroup)
    node_names_to_add: list[str] | None = None
    # Subflow name for node groups (if None, a fresh one will be created; used by SubflowNodeGroup)
    subflow_name: str | None = None
    # Name of the group node to add this node to after creation
    parent_group_name: str | None = None


@dataclass
@PayloadRegistry.register
class CreateNodeResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Node created successfully. Node is now available for parameter setting, connections, and execution.

    Args:
        node_name: Final assigned name (may differ from requested)
        node_type: Class name of created node
        specific_library_name: Library that provided this node type
        parent_flow_name: Name of the flow the node was created in
        parent_group_name: Name of the group this node belongs to (None if not in a group)
    """

    node_name: str
    node_type: str
    specific_library_name: str | None = None
    parent_flow_name: str | None = None
    parent_group_name: str | None = None


@dataclass
@PayloadRegistry.register
class CreateNodeResultFailure(ResultPayloadFailure):
    """Node creation failed.

    Common causes: invalid node_type, missing library, flow not found,
    no current context, or instantiation errors. Workflow unchanged.
    """


# Backwards compatibility for workflows that use the deprecated CreateNodeGroupRequest
@dataclass
class CreateNodeGroupRequest:
    pass


@dataclass
@PayloadRegistry.register
class DeleteNodeRequest(RequestPayload):
    """Delete a node from a workflow.

    Use when: Removing obsolete nodes, cleaning up failed nodes, restructuring workflows,
    implementing undo. Handles cascading cleanup of connections and execution cancellation.

    Args:
        node_name: Name of the node to delete (None for current context node)

    Results: DeleteNodeResultSuccess | DeleteNodeResultFailure (node not found, cleanup failed)
    """

    # If None is passed, assumes we're using the Node in the Current Context.
    node_name: str | None = None


@dataclass
@PayloadRegistry.register
class DeleteNodeResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Node deleted successfully. Node and all connections removed, no longer available for use."""


@dataclass
@PayloadRegistry.register
class DeleteNodeResultFailure(ResultPayloadFailure):
    """Node deletion failed.

    Common causes: node not found, no current context,
    execution cancellation failed, or connection cleanup failed. Workflow unchanged.
    """


@dataclass
@PayloadRegistry.register
class GetNodeResolutionStateRequest(RequestPayload):
    """Get the current resolution state of a node.

    Use when: Checking if node is ready to execute, monitoring execution progress,
    workflow orchestration, debugging. States: UNRESOLVED -> RESOLVED -> EXECUTING -> COMPLETED/FAILED

    Args:
        node_name: Name of the node to check (None for current context node)

    Results: GetNodeResolutionStateResultSuccess (with state) | GetNodeResolutionStateResultFailure (node not found)
    """

    # If None is passed, assumes we're using the Node in the Current Context
    node_name: str | None = None


@dataclass
@PayloadRegistry.register
class GetNodeResolutionStateResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Node resolution state retrieved successfully.

    Args:
        state: Current state (UNRESOLVED, RESOLVED, EXECUTING, COMPLETED, FAILED)
    """

    state: str


@dataclass
@PayloadRegistry.register
class GetNodeResolutionStateResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Resolution state retrieval failed. Common causes: node not found, no current context."""


@dataclass
@PayloadRegistry.register
class ListParametersOnNodeRequest(RequestPayload):
    """List all parameter names available on a node.

    Use when: Parameter discovery, validation before setting values, generating UIs,
    implementing completion features. Names can be used with GetParameterValue, SetParameterValue, connections.

    Args:
        node_name: Name of the node to list parameters for (None for current context node)

    Results: ListParametersOnNodeResultSuccess (with parameter names) | ListParametersOnNodeResultFailure (node not found)
    """

    # If None is passed, assumes we're using the Node in the Current Context
    node_name: str | None = None


@dataclass
@PayloadRegistry.register
class ListParametersOnNodeResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Parameter names retrieved successfully.

    Args:
        parameter_names: List of parameter names available on the node
    """

    parameter_names: list[str]


@dataclass
@PayloadRegistry.register
class ListParametersOnNodeResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Parameter listing failed. Common causes: node not found, no current context."""


@dataclass
@PayloadRegistry.register
class GetNodeMetadataRequest(RequestPayload):
    """Retrieve metadata associated with a node.

    Use when: Getting node position for layout, retrieving custom properties, implementing selection,
    saving/loading workflow layout. Metadata doesn't affect execution but provides workflow context.

    Args:
        node_name: Name of the node to get metadata for (None for current context node)

    Results: GetNodeMetadataResultSuccess (with metadata dict) | GetNodeMetadataResultFailure (node not found)
    """

    # If None is passed, assumes we're using the Node in the Current Context
    node_name: str | None = None


@dataclass
@PayloadRegistry.register
class GetNodeMetadataResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Node metadata retrieved successfully.

    Args:
        metadata: Dictionary containing position, display properties, custom user data
    """

    metadata: dict


@dataclass
@PayloadRegistry.register
class GetNodeMetadataResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Metadata retrieval failed. Common causes: node not found, no current context."""


@dataclass
@PayloadRegistry.register
class SetNodeMetadataRequest(RequestPayload):
    """Update metadata associated with a node.

    Use when: Updating node position, storing custom properties/annotations, implementing styling,
    saving user preferences. Metadata doesn't affect execution but provides workflow context.

    Args:
        metadata: Dictionary of metadata to set (position, display properties, custom data)
        node_name: Name of the node to update metadata for (None for current context node)

    Results: SetNodeMetadataResultSuccess | SetNodeMetadataResultFailure (node not found, update error)
    """

    metadata: dict
    # If None is passed, assumes we're using the Node in the Current Context
    node_name: str | None = None


@dataclass
@PayloadRegistry.register
class SetNodeMetadataResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Node metadata updated successfully. New metadata stored and available for future requests."""


@dataclass
@PayloadRegistry.register
class SetNodeMetadataResultFailure(ResultPayloadFailure):
    """Metadata update failed. Common causes: node not found, no current context, invalid metadata format."""


@dataclass
@PayloadRegistry.register
class BatchSetNodeMetadataRequest(RequestPayload):
    """Update metadata for multiple nodes in a single request.

    Use when: Updating positions for multiple nodes at once, applying bulk styling changes,
    implementing multi-node selection operations, optimizing performance for UI updates.
    Supports partial updates - only specified metadata fields are updated for each node.

    Args:
        node_metadata_updates: Dictionary mapping node names to their metadata updates.
                              Each node's metadata is merged with existing metadata (partial update).
                              If a node name is None, uses the current context node.

    Results: BatchSetNodeMetadataResultSuccess | BatchSetNodeMetadataResultFailure (some nodes not found, update errors)
    """

    node_metadata_updates: dict[str | None, dict[str, Any]]


@dataclass
@PayloadRegistry.register
class BatchSetNodeMetadataResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Batch node metadata update completed successfully.

    Args:
        updated_nodes: List of node names that were successfully updated
        failed_nodes: Dictionary mapping failed node names to error descriptions (if any)
    """

    updated_nodes: list[str]
    failed_nodes: dict[str, str] = field(default_factory=dict)


@dataclass
@PayloadRegistry.register
class BatchSetNodeMetadataResultFailure(ResultPayloadFailure):
    """Batch metadata update failed.

    Common causes: all nodes not found, no current context, invalid metadata format,
    or other systemic errors preventing the batch operation.
    """


# Get all info via a "jumbo" node event. Batches multiple info requests for, say, a GUI.
# ...jumbode?
@dataclass
@PayloadRegistry.register
class GetAllNodeInfoRequest(RequestPayload):
    """Retrieve comprehensive information about a node in a single call.

    Use when: Populating UIs, implementing node inspection/debugging, gathering complete state
    for serialization, optimizing performance. Batches metadata, resolution state, connections, parameters.

    Args:
        node_name: Name of the node to get information for (None for current context node)

    Results: GetAllNodeInfoResultSuccess (with comprehensive info) | GetAllNodeInfoResultFailure (node not found)
    """

    # If None is passed, assumes we're using the Node in the Current Context
    node_name: str | None = None


@dataclass
class ParameterInfoValue:
    details: GetParameterDetailsResultSuccess
    value: GetParameterValueResultSuccess


@dataclass
@PayloadRegistry.register
class GetAllNodeInfoResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Comprehensive node information retrieved successfully.

    Args:
        metadata: Node metadata (position, display properties, etc.)
        node_resolution_state: Current execution state
        connections: All incoming and outgoing connections
        element_id_to_value: Parameter details and values by element ID
        root_node_element: Root element information
    """

    metadata: dict
    node_resolution_state: str
    locked: bool
    connections: ListConnectionsForNodeResultSuccess
    element_id_to_value: dict[str, ParameterInfoValue]
    root_node_element: dict[str, Any]


@dataclass
@PayloadRegistry.register
class GetAllNodeInfoResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Comprehensive node information retrieval failed.

    Common causes: node not found, no current context, partial failure in gathering information components.
    """


@dataclass
@PayloadRegistry.register
class SetLockNodeStateRequest(WorkflowNotAlteredMixin, RequestPayload):
    """Lock a node.

    Args:
        node_name: Name of the node to lock
        lock: Whether to lock or unlock the node. If true, the node will be locked, otherwise it will be unlocked.

    Results: SetLockNodeStateResultSuccess (node locked) | SetLockNodeStateResultFailure (node not found)
    """

    node_name: str | None
    lock: bool


@dataclass
@PayloadRegistry.register
class SetLockNodeStateResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Node locked successfully."""

    node_name: str
    locked: bool


@dataclass
@PayloadRegistry.register
class SetLockNodeStateResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Node failed to lock."""


@dataclass
@PayloadRegistry.register
class BatchSetNodeLockStateRequest(WorkflowNotAlteredMixin, RequestPayload):
    """Set lock state for multiple nodes in a single request.

    Use when: Locking or unlocking multiple nodes at once, consistent with BatchSetNodeMetadataRequest.

    Args:
        node_names: Names of nodes to lock/unlock.
        lock: Whether to lock (True) or unlock (False) the nodes.

    Results: BatchSetNodeLockStateResultSuccess | BatchSetNodeLockStateResultFailure
    """

    node_names: list[str]
    lock: bool


@dataclass
@PayloadRegistry.register
class BatchSetNodeLockStateResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Batch node lock state update completed successfully."""

    updated_nodes: list[str]
    failed_nodes: dict[str, str] = field(default_factory=dict)


@dataclass
@PayloadRegistry.register
class BatchSetNodeLockStateResultFailure(ResultPayloadFailure):
    """Batch node lock state update failed. Common causes: all nodes not found."""


# A Node's state can be serialized to a sequence of commands that the engine runs.
@dataclass
class SerializedNodeCommands:
    """Represents a set of serialized commands for a node, including its creation and modifications.

    This is useful for encapsulating a Node, either for saving a workflow, copy/paste, etc.

    Attributes:
        create_node_command (CreateNodeRequest): The command to create the node.
        element_modification_commands (list[RequestPayload]): A list of commands to create or modify the elements (including Parameters) of the node.
        node_dependencies (NodeDependencies): Dependencies that this node has on external resources (workflows, files, imports, libraries).
        node_uuid (NodeUUID): The UUID of this particular node. During deserialization, this UUID will be used to correlate this node's instance
            with the connections and parameter values necessary. We cannot use node name because Griptape Nodes enforces unique names, and we cannot
            predict the name that will be selected upon instantiation. Similarly, the same serialized node may be deserialized multiple times, such
            as during copy/paste or duplicate.
    """

    # Have to use str instead of the UUID class because it's not JSON serializable >:-/
    NodeUUID = NewType("NodeUUID", str)
    UniqueParameterValueUUID = NewType("UniqueParameterValueUUID", str)

    @dataclass
    class IndirectSetParameterValueCommand:
        """Companion class to assign parameter values from our unique values collection, since we can't predict the names.

        Attributes:
            set_parameter_value_command (SetParameterValueRequest): The base set parameter command.
            unique_value_uuid (SerializedNodeCommands.UniqueParameterValue.UniqueParameterValueUUID): The UUID into the
                unique values dictionary that must be provided when serializing/deserializing, used to assign values upon deserialization.
        """

        set_parameter_value_command: SetParameterValueRequest
        unique_value_uuid: SerializedNodeCommands.UniqueParameterValueUUID

    create_node_command: CreateNodeRequest
    element_modification_commands: list[RequestPayload]
    node_dependencies: NodeDependencies
    lock_node_command: SetLockNodeStateRequest | None = None
    is_node_group: bool = False
    node_uuid: NodeUUID = field(default_factory=lambda: SerializedNodeCommands.NodeUUID(str(uuid4())))


@dataclass
class SerializedParameterValueTracker:
    """Tracks the serialization state of parameter value hashes.

    This class manages the relationship between value hashes and their unique UUIDs,
    indicating whether a value is serializable or not. It allows the addition of both
    serializable and non-serializable value hashes and provides methods to retrieve
    the serialization state and unique UUIDs for given value hashes.

    Attributes:
        _value_hash_to_unique_value_uuid (dict[Any, SerializedNodeCommands.UniqueParameterValueUUID]):
            A dictionary mapping value hashes to their unique UUIDs when they are serializable.
        _non_serializable_value_hashes (set[Any]):
            A set of value hashes that are not serializable.
    """

    class TrackerState(Enum):
        """State of a value hash in the tracker."""

        NOT_IN_TRACKER = auto()
        SERIALIZABLE = auto()
        NOT_SERIALIZABLE = auto()

    _value_hash_to_unique_value_uuid: dict[Any, SerializedNodeCommands.UniqueParameterValueUUID] = field(
        default_factory=dict
    )
    _non_serializable_value_hashes: set[Any] = field(default_factory=set)

    def get_tracker_state(self, value_hash: Any) -> TrackerState:
        if value_hash in self._non_serializable_value_hashes:
            return SerializedParameterValueTracker.TrackerState.NOT_SERIALIZABLE
        if value_hash in self._value_hash_to_unique_value_uuid:
            return SerializedParameterValueTracker.TrackerState.SERIALIZABLE
        return SerializedParameterValueTracker.TrackerState.NOT_IN_TRACKER

    def add_as_serializable(
        self, value_hash: Any, unique_value_uuid: SerializedNodeCommands.UniqueParameterValueUUID
    ) -> None:
        self._value_hash_to_unique_value_uuid[value_hash] = unique_value_uuid

    def add_as_not_serializable(self, value_hash: Any) -> None:
        self._non_serializable_value_hashes.add(value_hash)

    def get_uuid_for_value_hash(self, value_hash: Any) -> SerializedNodeCommands.UniqueParameterValueUUID:
        return self._value_hash_to_unique_value_uuid[value_hash]

    def get_serializable_count(self) -> int:
        return len(self._value_hash_to_unique_value_uuid)


@dataclass
@PayloadRegistry.register
class SerializeNodeToCommandsRequest(RequestPayload):
    """Serialize a node into a sequence of commands.

    Use when: Implementing copy/paste, exporting nodes, creating templates, backing up nodes.
    Captures complete node state including parameters and connections.

    Args:
        node_name: Name of the node to serialize (None for current context node)
        unique_parameter_uuid_to_values: Mapping of UUIDs to unique parameter values (modified in-place)
        serialized_parameter_value_tracker: Tracks serialization state of parameter values

    Results: SerializeNodeToCommandsResultSuccess (with commands) | SerializeNodeToCommandsResultFailure (serialization error)
    """

    broadcast_result: bool = False
    node_name: str | None = None
    unique_parameter_uuid_to_values: dict[SerializedNodeCommands.UniqueParameterValueUUID, Any] = field(
        default_factory=dict
    )
    serialized_parameter_value_tracker: SerializedParameterValueTracker = field(
        default_factory=SerializedParameterValueTracker
    )
    use_pickling: bool = False
    serialize_all_parameter_values: bool = False
    # If True, keep subflow_name in group metadata for workflow file generation (defaults to False for copy/paste)
    include_existing_subflow_in_group: bool = False


@dataclass
@PayloadRegistry.register
class SerializeNodeToCommandsResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Represents a successful result for serializing a node into a sequence of commands.

    Attributes:
        serialized_node_commands (SerializedNodeCommands): The serialized commands representing the node.
        set_parameter_value_commands (list[SerializedNodeCommands.IndirectSetParameterValueCommand]): A list of
            commands to set parameter values, keyed into the unique values dictionary.
    """

    serialized_node_commands: SerializedNodeCommands
    set_parameter_value_commands: list[SerializedNodeCommands.IndirectSetParameterValueCommand]


@dataclass
@PayloadRegistry.register
class SerializeNodeToCommandsResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    pass


@dataclass
class SerializedSelectedNodesCommands:
    @dataclass
    class IndirectConnectionSerialization:
        """Companion class to create connections from node IDs in a serialization, since we can't predict the names.

        These are UUIDs referencing into the serialized_node_commands we maintain.

        Attributes:
            source_node_uuid (SerializedNodeCommands.NodeUUID): UUID of the source node, as stored within the serialization.
            source_parameter_name (str): Name of the source parameter.
            target_node_uuid (SerializedNodeCommands.NodeUUID): UUID of the target node.
            target_parameter_name (str): Name of the target parameter.
        """

        source_node_uuid: SerializedNodeCommands.NodeUUID
        source_parameter_name: str
        target_node_uuid: SerializedNodeCommands.NodeUUID
        target_parameter_name: str

    serialized_node_commands: list[SerializedNodeCommands]
    set_parameter_value_commands: dict[
        SerializedNodeCommands.NodeUUID, list[SerializedNodeCommands.IndirectSetParameterValueCommand]
    ]
    set_lock_commands_per_node: dict[SerializedNodeCommands.NodeUUID, SetLockNodeStateRequest]
    serialized_connection_commands: list[IndirectConnectionSerialization]


@dataclass
@PayloadRegistry.register
class SerializeSelectedNodesToCommandsRequest(WorkflowNotAlteredMixin, RequestPayload):
    """Serialize multiple selected nodes into commands.

    Use when: Implementing copy/paste, exporting workflow sections, creating templates,
    backing up workflows, transferring configurations. Preserves nodes and interconnections.

    Args:
        nodes_to_serialize: List of node identifiers (each containing [node_name, timestamp])
        copy_to_clipboard: Whether to copy the result to clipboard (defaults to True for backward compatibility)

    Results: SerializeSelectedNodesToCommandsResultSuccess (with commands) | SerializeSelectedNodesToCommandsResultFailure (node not found, serialization error)
    """

    # They will be passed with node_name, timestamp
    nodes_to_serialize: list[list[str]]
    copy_to_clipboard: bool = True


@dataclass
@PayloadRegistry.register
class SerializeSelectedNodesToCommandsResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Selected nodes serialized successfully.

    Preserves complete structure including node configurations, parameter values, and connection relationships.

    Args:
        serialized_selected_node_commands: Complete serialized representation
    """

    # They will be passed with node_name, timestamp
    # Could be a flow command if it's all nodes in a flow.
    serialized_selected_node_commands: str
    pickled_values: dict[str, str]
    node_names_serialized: list[str]


@dataclass
@PayloadRegistry.register
class SerializeSelectedNodesToCommandsResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Selected nodes serialization failed.

    Common causes: nodes not found, non-serializable parameter values, connection resolution failures.
    """


@dataclass
@PayloadRegistry.register
class DeserializeSelectedNodesFromCommandsRequest(WorkflowNotAlteredMixin, RequestPayload):
    """Recreate nodes from serialized commands.

    Use when: Implementing paste functionality, importing configurations, restoring from backups,
    duplicating complex structures. Creates new nodes with unique names and restores parameters/connections.

    Args:
        positions: List of positions for the recreated nodes (None for default positions)

    Results: DeserializeSelectedNodesFromCommandsResultSuccess (with node names) | DeserializeSelectedNodesFromCommandsResultFailure (deserialization error)
    """

    deserialize_commands: str
    pickled_values: dict[str, str]
    positions: list[NewPosition] | None = None


@dataclass
@PayloadRegistry.register
class DeserializeSelectedNodesFromCommandsResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Nodes recreated successfully from serialized commands. Parameter values and connections restored.

    Args:
        node_names: List of all node names created (including children)
        non_children_names: List of node names that are not children, which should have connections remapped in a duplicate operation.
    """

    node_names: list[str]
    non_children_names: list[str] = field(default_factory=list)


@dataclass
@PayloadRegistry.register
class DeserializeSelectedNodesFromCommandsResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Nodes recreation failed.

    Common causes: invalid/corrupted commands, missing node types/libraries,
    parameter deserialization failures, connection creation errors.
    """


@dataclass
@PayloadRegistry.register
class DeserializeNodeFromCommandsRequest(RequestPayload):
    """Recreate a single node from serialized commands.

    Use when: Restoring individual nodes from backups/templates, implementing node-level copy/paste,
    loading configurations, creating from templates. Creates new node with unique name and restores parameters.

    Args:
        serialized_node_commands: Serialized node commands containing complete node state

    Results: DeserializeNodeFromCommandsResultSuccess (with node name) | DeserializeNodeFromCommandsResultFailure (deserialization error)
    """

    serialized_node_commands: SerializedNodeCommands


@dataclass
@PayloadRegistry.register
class DeserializeNodeFromCommandsResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Node recreated successfully from serialized commands. Parameter values restored.

    Args:
        node_name: Name assigned to newly created node
    """

    node_name: str


@dataclass
@PayloadRegistry.register
class DeserializeNodeFromCommandsResultFailure(ResultPayloadFailure):
    """Node recreation failed.

    Common causes: invalid/corrupted commands, missing node type/library,
    parameter deserialization failures, creation errors or constraints.
    """


@dataclass
@PayloadRegistry.register
class DuplicateSelectedNodesRequest(WorkflowNotAlteredMixin, RequestPayload):
    """Duplicate selected nodes with new positions.

    Use when: Implementing duplicate functionality, creating multiple instances of same configuration,
    expanding workflows by replicating patterns, quick copying without serialization overhead.
    Preserves connections between duplicated nodes.

    Args:
        nodes_to_duplicate: List of node identifiers to duplicate (each containing [node_name, timestamp])
        positions: List of positions for the duplicated nodes (None for default positions)

    Results: DuplicateSelectedNodesResultSuccess (with node names) | DuplicateSelectedNodesResultFailure (duplication error)
    """

    nodes_to_duplicate: list[list[str]]
    positions: list[NewPosition] | None = None


@dataclass
@PayloadRegistry.register
class DuplicateSelectedNodesResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Nodes duplicated successfully. Configuration and connections preserved.

    Args:
        node_names: List of names assigned to newly duplicated nodes
    """

    node_names: list[str]


@dataclass
@PayloadRegistry.register
class DuplicateSelectedNodesResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Node duplication failed.

    Common causes: nodes not found, constraints/conflicts,
    insufficient resources, connection duplication failures.
    """


@dataclass
@PayloadRegistry.register
class SendNodeMessageRequest(RequestPayload):
    """Send a message to a specific node.

    Use when: External systems need to signal or send data directly to individual nodes,
    implementing custom communication patterns, triggering node-specific behaviors.

    Args:
        node_name: Name of the target node (None for current context node)
        optional_element_name: Optional element name this message relates to
        message_type: String indicating message type for receiver parsing
        message: Message payload of any type

    Results: SendNodeMessageResultSuccess (with response) | SendNodeMessageResultFailure (node not found, handler error)
    """

    message_type: str
    message: NodeMessagePayload | None
    node_name: str | None = None
    optional_element_name: str | None = None


@dataclass
@PayloadRegistry.register
class SendNodeMessageResultSuccess(ResultPayloadSuccess):
    """Node message sent and processed successfully.

    Args:
        response: Optional response data from the node's message handler
    """

    response: NodeMessagePayload | None = None


@dataclass
@PayloadRegistry.register
class SendNodeMessageResultFailure(ResultPayloadFailure):
    """Node message sending failed.

    Common causes: node not found, no current context, message handler error,
    unsupported message type.

    Args:
        response: Optional response data from the node's message handler (even on failure)
    """

    response: NodeMessagePayload | None = None


@dataclass
@PayloadRegistry.register
class GetFlowForNodeRequest(RequestPayload):
    """Get the flow name that contains a specific node.

    Use when: Need to determine which flow a node belongs to for variable scoping,
    flow-specific operations, or hierarchical lookups.

    Args:
        node_name: Name of the node to get the flow for

    Results: GetFlowForNodeResultSuccess (with flow name) | GetFlowForNodeResultFailure (node not found)
    """

    node_name: str


@dataclass
@PayloadRegistry.register
class GetFlowForNodeResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Flow for node retrieved successfully.

    Args:
        flow_name: Name of the flow that contains the node
    """

    flow_name: str


@dataclass
@PayloadRegistry.register
class GetFlowForNodeResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Flow for node retrieval failed.

    Common causes: node not found, node not assigned to any flow.
    """


@dataclass
@PayloadRegistry.register
class CanResetNodeToDefaultsRequest(RequestPayload):
    """Check if a node can be reset to its default state.

    Use when: Need to validate whether a node reset operation is allowed before attempting it,
    implementing UI state (enabled/disabled reset button), or providing user feedback.
    Checks for conditions that would prevent reset (locked state, missing metadata, etc.).

    Args:
        node_name: Name of the node to check (None for current context node)

    Results: CanResetNodeToDefaultsResultSuccess (with can_reset flag and reason) | CanResetNodeToDefaultsResultFailure (validation failed)
    """

    node_name: str | None = None


@dataclass
@PayloadRegistry.register
class CanResetNodeToDefaultsResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Node reset check completed successfully.

    Args:
        can_reset: True if the node can be reset to defaults, False otherwise
        editor_tooltip_reason: Optional explanation if node cannot be reset (e.g., "Cannot reset locked node")
    """

    can_reset: bool
    editor_tooltip_reason: str | None = None


@dataclass
@PayloadRegistry.register
class CanResetNodeToDefaultsResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Node reset check failed.

    Common causes: node not found, no current context, no library metadata.
    """


@dataclass
@PayloadRegistry.register
class ResetNodeToDefaultsRequest(RequestPayload):
    """Reset a node to its default state while preserving connections where possible.

    Use when: Need to reset a node's configuration back to defaults, clear customizations,
    fix broken node state, or restore a node to its initial state. Creates a fresh instance
    of the same node type and reconnects it to the workflow.

    Args:
        node_name: Name of the node to reset (None for current context node)

    Results: ResetNodeToDefaultsResultSuccess (with reconnection status) | ResetNodeToDefaultsResultFailure (reset failed)
    """

    node_name: str | None = None


@dataclass
@PayloadRegistry.register
class ResetNodeToDefaultsResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Node reset to defaults successfully.

    Args:
        node_name: Name of the reset node
        failed_incoming_connections: List of incoming connections that failed to reconnect
        failed_outgoing_connections: List of outgoing connections that failed to reconnect
    """

    node_name: str
    failed_incoming_connections: list[IncomingConnection]
    failed_outgoing_connections: list[OutgoingConnection]


@dataclass
@PayloadRegistry.register
class ResetNodeToDefaultsResultFailure(ResultPayloadFailure):
    """Node reset to defaults failed.

    Common causes: node not found, no current context, failed to create new node,
    failed to delete old node, failed to rename new node.
    """


@dataclass
@PayloadRegistry.register
class AddNodesToNodeGroupRequest(RequestPayload):
    """Adds nodes to a NodeGroup.

    Use when: Need to add nodes to an existing NodeGroup, building node groups programmatically,
    organizing nodes into logical groups.

    Args:
        node_name: Name of the node to add to the group
        node_group_name: Name of the NodeGroup to add the node to
        flow_name: Optional flow name to search in (None for current context flow)

    Results: AddNodesToNodeGroupResultSuccess | AddNodeToNodeGroupResultFailure (node not found, group not found, add failed)
    """

    node_names: list[str]
    node_group_name: str
    flow_name: str | None = None


@dataclass
@PayloadRegistry.register
class AddNodesToNodeGroupResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Node added to NodeGroup successfully."""


@dataclass
@PayloadRegistry.register
class AddNodesToNodeGroupResultFailure(ResultPayloadFailure):
    """Adding node to NodeGroup failed.

    Common causes: node not found, NodeGroup not found, node already in group,
    flow not found, no current context.
    """


@dataclass
@PayloadRegistry.register
class RemoveNodeFromNodeGroupRequest(RequestPayload):
    """Remove nodes from a NodeGroup.

    Use when: Need to remove nodes from a NodeGroup, reorganizing workflow structure,
    implementing undo operations.

    Args:
        node_names: Names of the nodes to remove from the group
        node_group_name: Name of the NodeGroup to remove the nodes from
        flow_name: Optional flow name to search in (None for current context flow)

    Results: RemoveNodeFromNodeGroupResultSuccess | RemoveNodeFromNodeGroupResultFailure (node not found, group not found, node not in group)
    """

    node_names: list[str]
    node_group_name: str
    flow_name: str | None = None


@dataclass
@PayloadRegistry.register
class RemoveNodeFromNodeGroupResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Nodes removed from NodeGroup successfully."""


@dataclass
@PayloadRegistry.register
class RemoveNodeFromNodeGroupResultFailure(ResultPayloadFailure):
    """Removing node from NodeGroup failed.

    Common causes: node not found, NodeGroup not found, node not in group,
    flow not found, no current context.
    """


@dataclass
@PayloadRegistry.register
class MoveNodeToNewFlowRequest(RequestPayload):
    """Move a node from one flow to another flow.

    Use when: Reorganizing nodes between flows, adding nodes to NodeGroup subflows,
    removing nodes from NodeGroup subflows. Node connections are preserved since
    connections are global and work across flows.

    Args:
        node_name: Name of the node to move (None for current context)
        target_flow_name: Name of the destination flow
        source_flow_name: Name of the source flow (None to use node's current flow)

    Results: MoveNodeToNewFlowResultSuccess | MoveNodeToNewFlowResultFailure (node not found, flow not found, node not in source flow)
    """

    target_flow_name: str
    node_name: str | None = None
    source_flow_name: str | None = None


@dataclass
@PayloadRegistry.register
class MoveNodeToNewFlowResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Node moved successfully between flows. Connections preserved.

    Args:
        node_name: Name of the node that was moved
        source_flow_name: Name of the flow the node was moved from
        target_flow_name: Name of the flow the node was moved to
    """

    node_name: str
    source_flow_name: str
    target_flow_name: str


@dataclass
@PayloadRegistry.register
class MoveNodeToNewFlowResultFailure(ResultPayloadFailure):
    """Node move failed.

    Common causes: node not found, source flow not found, target flow not found,
    node not in source flow, node is a NodeGroup with subflow conflicts.
    """


@dataclass
@PayloadRegistry.register
class UnresolveNodeRequest(RequestPayload):
    """Mark a node UNRESOLVED without resetting its parameter values.

    Use when: Forcing a node to re-execute without resetting its parameter values.
    Downstream nodes are also marked UNRESOLVED. Useful for developer tooling.

    If the node is currently RESOLVING the request fails immediately. Retry
    after the node has finished resolving to unresolve it.

    Args:
        node_name: Name of the node to unresolve.

    Results: UnresolveNodeResultSuccess | UnresolveNodeResultFailure
    """

    node_name: str


@dataclass
@PayloadRegistry.register
class UnresolveNodeResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Node marked UNRESOLVED. Downstream nodes also invalidated."""


@dataclass
@PayloadRegistry.register
class UnresolveNodeResultFailure(ResultPayloadFailure):
    """Unresolve failed. Common causes: node not found, node is currently RESOLVING."""
