from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, NamedTuple

from griptape_nodes.exe_types.node_types import NodeDependencies
from griptape_nodes.node_library.workflow_registry import LibraryNameAndNodeType, WorkflowShape
from griptape_nodes.retained_mode.events.base_events import (
    RequestPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    WorkflowAlteredMixin,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.node_events import SerializedNodeCommands, SetLockNodeStateRequest
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry
from griptape_nodes.retained_mode.events.variable_events import CreateVariableRequest

if TYPE_CHECKING:
    # Circular import: flow_events <-> workflow_events
    from griptape_nodes.retained_mode.events.workflow_events import ImportWorkflowAsReferencedSubFlowRequest


class FlowMetadataExtractionFailureReason(StrEnum):
    """Reasons why flow metadata extraction from image failed."""

    FILE_NOT_FOUND = "file_not_found"
    FILE_READ_ERROR = "file_read_error"
    INVALID_IMAGE_FORMAT = "invalid_image_format"
    NO_FLOW_METADATA = "no_flow_metadata"
    INVALID_BASE64 = "invalid_base64"
    INVALID_PICKLE = "invalid_pickle"
    DESERIALIZATION_FAILED = "deserialization_failed"


@dataclass(kw_only=True)
@PayloadRegistry.register
class CreateFlowRequest(RequestPayload):
    """Create a new flow (sub-workflow) within a parent flow.

    Use when: Creating sub-workflows, organizing complex workflows into components,
    implementing reusable workflow patterns, building hierarchical workflows.

    Args:
        parent_flow_name: Name of the parent flow to create the new flow within
        flow_name: Name for the new flow (None for auto-generated)
        set_as_new_context: Whether to set this flow as the new current context
        metadata: Initial metadata for the flow

    Results: CreateFlowResultSuccess (with flow name) | CreateFlowResultFailure (parent not found, name conflicts)
    """

    parent_flow_name: str | None
    flow_name: str | None = None
    # When True, this Flow will be pushed as the new Current Context.
    set_as_new_context: bool = True
    metadata: dict | None = None


@dataclass
@PayloadRegistry.register
class CreateFlowResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Flow created successfully.

    Args:
        flow_name: Name assigned to the new flow
    """

    flow_name: str


@dataclass
@PayloadRegistry.register
class CreateFlowResultFailure(ResultPayloadFailure):
    """Flow creation failed. Common causes: parent flow not found, name conflicts, invalid parameters."""


@dataclass
@PayloadRegistry.register
class DeleteFlowRequest(RequestPayload):
    """Delete a flow and all its contents.

    Use when: Removing unused sub-workflows, cleaning up complex workflows,
    implementing flow management features. Cascades to delete all nodes and sub-flows.

    Args:
        flow_name: Name of the flow to delete (None for current context flow)

    Results: DeleteFlowResultSuccess | DeleteFlowResultFailure (flow not found, deletion not allowed)
    """

    # If None is passed, assumes we're deleting the flow in the Current Context.
    flow_name: str | None = None


@dataclass
@PayloadRegistry.register
class DeleteFlowResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Flow deleted successfully. All nodes and sub-flows removed."""


@dataclass
@PayloadRegistry.register
class DeleteFlowResultFailure(ResultPayloadFailure):
    """Flow deletion failed. Common causes: flow not found, no current context, deletion not allowed."""


@dataclass
@PayloadRegistry.register
class ListNodesInFlowRequest(RequestPayload):
    """List all nodes in a specific flow.

    Use when: Inspecting flow contents, building flow visualizations,
    implementing flow management features, debugging workflow structure.

    Args:
        flow_name: Name of the flow to list nodes from (None for current context flow)
        node_types: Optional list of Python class names to filter by (e.g. ["StartFlow", "Agent"]).
            None returns all nodes.

    Results: ListNodesInFlowResultSuccess (with node names) | ListNodesInFlowResultFailure (flow not found)
    """

    # If None is passed, assumes we're using the flow in the Current Context.
    flow_name: str | None = None
    node_types: list[str] | None = None


@dataclass
@PayloadRegistry.register
class ListNodesInFlowResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Flow nodes listed successfully.

    Args:
        node_names: List of node names in the flow
    """

    node_names: list[str]


@dataclass
@PayloadRegistry.register
class ListNodesInFlowResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Flow nodes listing failed. Common causes: flow not found, no current context."""


# We have two different ways to list flows:
# 1. ListFlowsInFlowRequest - List flows in a specific flow, or if parent_flow_name=None, list canvas/top-level flows
# 2. ListFlowsInCurrentContext - List flows in whatever flow is at the top of the Current Context
# These are separate classes to avoid ambiguity and to catch incorrect usage at compile time.
# It was implemented this way to maintain backwards compatibility with the editor.
@dataclass
@PayloadRegistry.register
class ListFlowsInCurrentContextRequest(RequestPayload):
    pass


@dataclass
@PayloadRegistry.register
class ListFlowsInCurrentContextResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    flow_names: list[str]


@dataclass
@PayloadRegistry.register
class ListFlowsInCurrentContextResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    pass


@dataclass
class NodePosition:
    """Resulting editor position for a node in AutoLayoutFlowResultSuccess."""

    node_name: str
    x: float
    y: float


@dataclass
@PayloadRegistry.register
class AutoLayoutFlowRequest(RequestPayload):
    """Auto-place every node in a flow on a topological column-and-row grid.

    Use when: an MCP client has just finished building a graph (nodes + connections) and
    wants the editor to render it in a readable layout without having to compute pixel
    positions by hand. Runs a topological sort over the flow's data/control edges, assigns
    each node a layer (longest path from any source), and places layers as columns and
    siblings within a layer as rows.

    Writes the computed position into each node's `metadata["position"] = {"x", "y"}`,
    matching the convention the editor and other engine code already use. Existing metadata
    keys other than `position` are preserved.

    Nodes that participate in a cycle (should not happen in a well-formed flow) are placed
    at layer 0; a warning is logged.

    Args:
        flow_name: Flow to lay out. Defaults to the current-context flow, matching the
            convention used by other flow-scoped requests.
        origin_x: X coordinate of the first column. Defaults to 0.
        origin_y: Y coordinate of the first row. Defaults to 0.
        layer_spacing: Horizontal spacing between columns in editor pixels. 650 is a
            readable default that matches the pair-node spacing used elsewhere in the engine.
        row_spacing: Vertical spacing between sibling nodes within a layer.

    Results: AutoLayoutFlowResultSuccess (with per-node final positions) | AutoLayoutFlowResultFailure
    """

    flow_name: str | None = None
    origin_x: float = 0.0
    origin_y: float = 0.0
    layer_spacing: float = 650.0
    row_spacing: float = 300.0


@dataclass
@PayloadRegistry.register
class AutoLayoutFlowResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Auto-layout applied.

    Args:
        flow_name: Flow that was laid out.
        positioned_nodes: Final (node_name, x, y) for every node in the flow. Ordered by
            layer then by row within a layer so callers can reason about the visual shape.
    """

    flow_name: str
    positioned_nodes: list[NodePosition] = field(default_factory=list)


@dataclass
@PayloadRegistry.register
class AutoLayoutFlowResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Auto-layout failed. Common causes: no flow in current context, named flow not found."""


# Gives a list of the flows directly parented by the node specified.
@dataclass
@PayloadRegistry.register
class ListFlowsInFlowRequest(RequestPayload):
    # Pass in None to get the canvas.
    parent_flow_name: str | None = None


@dataclass
@PayloadRegistry.register
class ListFlowsInFlowResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    flow_names: list[str]


@dataclass
@PayloadRegistry.register
class ListFlowsInFlowResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    pass


@dataclass
@PayloadRegistry.register
class GetTopLevelFlowRequest(RequestPayload):
    pass


@dataclass
@PayloadRegistry.register
class GetTopLevelFlowResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    flow_name: str | None


# A Flow's state can be serialized into a sequence of commands that the engine then runs.
@dataclass
class SerializedFlowCommands:
    """Represents the serialized commands for a flow, including the nodes and their connections.

    Useful for save/load, copy/paste, etc.

    Attributes:
        flow_initialization_command (CreateFlowRequest | ImportWorkflowAsReferencedSubFlowRequest | None): Command to initialize the flow that contains all of this.
            Can be CreateFlowRequest for standalone flows, ImportWorkflowAsReferencedSubFlowRequest for referenced workflows,
            or None to deserialize into whatever Flow is in the Current Context.
        serialized_node_commands (list[SerializedNodeCommands]): List of serialized commands for nodes.
            Handles creating all of the nodes themselves, along with configuring them. Does NOT set Parameter values,
            which is done as a separate step.
        serialized_connections (list[SerializedFlowCommands.IndirectConnectionSerialization]): List of serialized connections.
            Creates the connections between Nodes.
        unique_parameter_uuid_to_values (dict[SerializedNodeCommands.UniqueParameterValueUUID, Any]): Records the unique Parameter values used by the Flow.
        set_parameter_value_commands (dict[SerializedNodeCommands.NodeUUID, list[SerializedNodeCommands.IndirectSetParameterValueCommand]]): List of commands
            to set parameter values, keyed by node UUID, during deserialization.
        sub_flows_commands (list["SerializedFlowCommands"]): List of sub-flow commands. Cascades into sub-flows within this serialization.
        node_dependencies (NodeDependencies): Aggregated dependencies from all nodes in this flow and its sub-flows.
            Includes referenced workflows, static files, Python imports, and libraries. Used for workflow packaging,
            dependency resolution, and deployment planning.
        node_types_used (set[LibraryNameAndNodeType]): Set of all node types used in this flow and its sub-flows.
            Each entry contains the library name and node type name pair, used for tracking which node types are utilized.
    """

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

    @dataclass
    class SerializedVariableCommand:
        """Companion class to recreate a Variable during deserialization, pulling its value from the unique-values pool.

        Attributes:
            create_variable_command (CreateVariableRequest): The base create-variable command.
                The ``value`` field on this command is ignored during AST generation — the generated script
                substitutes the value from ``top_level_unique_values_dict[<unique_value_uuid>]`` instead.
            unique_value_uuid (SerializedNodeCommands.UniqueParameterValueUUID): The UUID into the
                unique values dictionary for this variable's value. Shares the pool with parameter values.
        """

        create_variable_command: CreateVariableRequest
        unique_value_uuid: SerializedNodeCommands.UniqueParameterValueUUID

    flow_initialization_command: CreateFlowRequest | ImportWorkflowAsReferencedSubFlowRequest | None
    serialized_node_commands: list[SerializedNodeCommands]
    serialized_connections: list[IndirectConnectionSerialization]
    unique_parameter_uuid_to_values: dict[SerializedNodeCommands.UniqueParameterValueUUID, Any]
    set_parameter_value_commands: dict[
        SerializedNodeCommands.NodeUUID, list[SerializedNodeCommands.IndirectSetParameterValueCommand]
    ]
    set_lock_commands_per_node: dict[SerializedNodeCommands.NodeUUID, SetLockNodeStateRequest]
    sub_flows_commands: list[SerializedFlowCommands]
    node_dependencies: NodeDependencies
    node_types_used: set[LibraryNameAndNodeType]
    flow_name: str | None = None
    serialized_variable_commands: list[SerializedVariableCommand] = field(default_factory=list)


@dataclass
@PayloadRegistry.register
class SerializeFlowToCommandsRequest(RequestPayload):
    """Request payload to serialize a flow into a sequence of commands.

    Attributes:
        flow_name (str | None): The name of the flow to serialize. If None is passed, assumes we're serializing the flow in the Current Context.
        include_create_flow_command (bool): If set to False, this will omit the CreateFlow call from the serialized flow object.
            This can be useful so that the contents of a flow can be deserialized into an existing flow instead of creating a new one and deserializing the nodes into that.
            Copy/paste can make use of this.
    """

    broadcast_result: bool = False
    flow_name: str | None = None
    include_create_flow_command: bool = True


@dataclass
@PayloadRegistry.register
class SerializeFlowToCommandsResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    serialized_flow_commands: SerializedFlowCommands


@dataclass
@PayloadRegistry.register
class SerializeFlowToCommandsResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    pass


@dataclass
@PayloadRegistry.register
class DeserializeFlowFromCommandsRequest(RequestPayload):
    serialized_flow_commands: SerializedFlowCommands
    pop_flow_context_after: bool = (
        True  # Whether to pop the flow context after deserialization (default: True for cleanup)
    )


@dataclass
@PayloadRegistry.register
class DeserializeFlowFromCommandsResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    flow_name: str
    node_name_mappings: dict[str, str] = field(default_factory=dict)  # original_name -> deserialized_name


@dataclass
@PayloadRegistry.register
class DeserializeFlowFromCommandsResultFailure(ResultPayloadFailure):
    pass


@dataclass
@PayloadRegistry.register
class ExtractFlowCommandsFromImageMetadataRequest(RequestPayload):
    """Extract flow commands from PNG image metadata.

    This request reads a PNG image file, extracts the embedded workflow metadata
    (specifically the gtn_flow_commands field), decodes it from base64, unpickles it,
    and returns the SerializedFlowCommands object. Optionally, it can automatically
    deserialize the flow by calling DeserializeFlowFromCommandsRequest.

    Use when: Loading flow commands from an exported image, inspecting workflow
    structure before deserialization, extracting flows shared as PNG files.

    Args:
        file_url_or_path: Path to the PNG image file (absolute or relative) or URL
        deserialize: If True, automatically deserialize the flow after extraction (default: False)

    Results:
        ExtractFlowCommandsFromImageMetadataResultSuccess - Commands extracted (and optionally deserialized)
        ExtractFlowCommandsFromImageMetadataResultFailure - Extraction or deserialization failed
    """

    file_url_or_path: str
    deserialize: bool = False


@dataclass(kw_only=True)
@PayloadRegistry.register
class ExtractFlowCommandsFromImageMetadataResultSuccess(ResultPayloadSuccess):
    """Flow commands extracted successfully from image metadata.

    An image without embedded flow commands is still a success: the image simply
    carries no workflow payload, which is a valid state. In that case
    serialized_flow_commands is None.

    Args:
        serialized_flow_commands: The extracted and decoded SerializedFlowCommands object, or None if the image has no embedded flow commands
        flow_name: Name of the deserialized flow (only present if deserialize=True)
        node_name_mappings: Mapping from original to deserialized node names (only present if deserialize=True)
    """

    serialized_flow_commands: SerializedFlowCommands | None = None
    flow_name: str | None = None
    node_name_mappings: dict[str, str] = field(default_factory=dict)


@dataclass
@PayloadRegistry.register
class ExtractFlowCommandsFromImageMetadataResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Flow commands extraction failed.

    Args:
        file_path: The file path that was attempted (for error context)
    """

    file_path: str


@dataclass
@PayloadRegistry.register
class GetFlowDetailsRequest(RequestPayload):
    """Request payload to get detailed information about a flow.

    This provides metadata about a flow including its reference status and parent hierarchy,
    useful for editor integration to display flows appropriately.

    Attributes:
        flow_name (str | None): The name of the flow to get details for. If None is passed,
            assumes we're getting details for the flow in the Current Context.
    """

    # If None is passed, assumes we're getting details for the flow in the Current Context.
    flow_name: str | None = None


@dataclass
@PayloadRegistry.register
class GetFlowDetailsResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Success result containing flow details.

    Attributes:
        referenced_workflow_name (str | None): The name of the workflow that was
            imported to create this flow. None if this flow was created standalone.
        parent_flow_name (str | None): The name of the parent flow, or None if this is a
            top-level flow.
    """

    referenced_workflow_name: str | None
    parent_flow_name: str | None
    flow_type: str | None


@dataclass
@PayloadRegistry.register
class GetFlowDetailsResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Failure result when flow details cannot be retrieved.

    This occurs when the specified flow doesn't exist, the current context is empty
    (when flow_name is None), or there are issues with the flow's parent mapping.
    """


@dataclass
@PayloadRegistry.register
class GetFlowMetadataRequest(RequestPayload):
    """Get metadata associated with a flow.

    Use when: Retrieving flow layout information, getting custom flow properties,
    implementing flow management features, debugging flow state.

    Results: GetFlowMetadataResultSuccess (with metadata dict) | GetFlowMetadataResultFailure (flow not found)
    """

    # If None is passed, assumes we're using the Flow in the Current Context
    flow_name: str | None = None


@dataclass
@PayloadRegistry.register
class GetFlowMetadataResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Flow metadata retrieved successfully.

    Args:
        metadata: Dictionary containing flow metadata
    """

    metadata: dict


@dataclass
@PayloadRegistry.register
class GetFlowMetadataResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Flow metadata retrieval failed. Common causes: flow not found, no current context."""


@dataclass
@PayloadRegistry.register
class SetFlowMetadataRequest(RequestPayload):
    """Set metadata associated with a flow.

    Use when: Updating flow layout information, storing custom flow properties,
    implementing flow management features, saving flow state.

    Results: SetFlowMetadataResultSuccess | SetFlowMetadataResultFailure (flow not found, metadata error)
    """

    metadata: dict
    # If None is passed, assumes we're using the Flow in the Current Context
    flow_name: str | None = None


@dataclass
@PayloadRegistry.register
class SetFlowMetadataResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Flow metadata updated successfully."""


@dataclass
@PayloadRegistry.register
class SetFlowMetadataResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Flow metadata update failed. Common causes: flow not found, no current context, invalid metadata."""


# Type aliases for parameter mapping clarity
SanitizedParameterName = str  # What appears in the serialized flow
OriginalNodeName = str  # Original node name (can have spaces, dots, etc.)
OriginalParameterName = str  # Original parameter name
PackagedNodeName = str  # Name of Start/End node in packaged flow


class OriginalNodeParameter(NamedTuple):
    """Represents the original source of a parameter before sanitization."""

    node_name: OriginalNodeName
    parameter_name: OriginalParameterName


class PackagedNodeParameterMapping(NamedTuple):
    """Parameter mappings for a packaged node (Start or End)."""

    node_name: PackagedNodeName  # Name of the packaged node (e.g., "Start_Package_MultiNode")
    parameter_mappings: dict[SanitizedParameterName, OriginalNodeParameter]  # Parameter name -> original node/param


class ParameterNameMapping(NamedTuple):
    """Maps a sanitized parameter name back to its original node and parameter."""

    output_sanitized_parameter_name: SanitizedParameterName
    original: OriginalNodeParameter


@dataclass
@PayloadRegistry.register
class PackageNodesAsSerializedFlowRequest(RequestPayload):
    """Package multiple nodes as a complete flow with artificial start and end nodes.

    Creates a serialized flow where:
    - Start node has output parameters matching all selected nodes' incoming connections
    - All selected nodes maintain their existing connections between each other
    - End node has input parameters matching all selected nodes' outgoing connections
    - Flow structure: Start → [Selected Nodes with internal connections] → End

    Use when: Creating complex reusable components, exporting node groups for templates,
    building multi-step sub-workflows, packaging interconnected functionality.

    Args:
        node_names: List of node names to package as a flow (empty list will create StartFlow→EndFlow only with warning)
        start_node_type: Node type name for the artificial start node (None or omitted defaults to "StartFlow")
        end_node_type: Node type name for the artificial end node (None or omitted defaults to "EndFlow")
        start_node_library_name: Library name containing the start node (defaults to "Griptape Nodes Library")
        end_node_library_name: Library name containing the end node (defaults to "Griptape Nodes Library")
        entry_control_node_name: Name of the node that should receive the control flow entry (required if entry_control_parameter_name specified)
        entry_control_parameter_name: Name of the control parameter on the entry node (None for auto-detection of first available control parameter)
        output_parameter_prefix: Prefix for parameter names on the generated end node to avoid collisions (defaults to "packaged_node_")

    Results: PackageNodesAsSerializedFlowResultSuccess (with serialized flow and node name mapping) | PackageNodesAsSerializedFlowResultFailure
    """

    # List of node names to package (empty list creates StartFlow→EndFlow only with warning)
    node_names: list[str] = field(default_factory=list)
    start_node_type: str | None = None
    end_node_type: str | None = None
    start_node_library_name: str = "Griptape Nodes Library"
    end_node_library_name: str = "Griptape Nodes Library"
    entry_control_node_name: str | None = None
    entry_control_parameter_name: str | None = None
    output_parameter_prefix: str = "packaged_node_"
    node_group_name: str | None = None  # Name of the SubflowNodeGroup if packaging a group


@dataclass
@PayloadRegistry.register
class PackageNodesAsSerializedFlowResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Multiple nodes successfully packaged as serialized flow.

    Args:
        serialized_flow_commands: The complete serialized flow with StartFlow, selected nodes with preserved connections, and EndFlow
        workflow_shape: The workflow shape defining inputs and outputs for external callers
        packaged_node_names: List of node names that were included in the package
        parameter_name_mappings: List of parameter mappings for packaged nodes.
            Index 0 = Start node mappings, Index 1 = End node mappings.
            Each entry contains the node name and its parameter mappings.
    """

    serialized_flow_commands: SerializedFlowCommands
    workflow_shape: WorkflowShape
    packaged_node_names: list[str]
    parameter_name_mappings: list[PackagedNodeParameterMapping]


@dataclass
@PayloadRegistry.register
class PackageNodesAsSerializedFlowResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Multiple nodes packaging failed.

    Common causes: one or more nodes not found, no current context, serialization error,
    entry control node/parameter not found, connection analysis failed.
    """
