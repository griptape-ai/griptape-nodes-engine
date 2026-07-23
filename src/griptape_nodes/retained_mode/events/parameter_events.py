from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NamedTuple

from griptape_nodes.exe_types.core_types import ParameterMode
from griptape_nodes.retained_mode.events.base_events import (
    BLOB_FIELD_METADATA_KEY,
    ExecutionPayload,
    RequestPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    WorkflowAlteredMixin,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.connection_events import IncomingConnection, OutgoingConnection
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass
@PayloadRegistry.register
class AddParameterToNodeRequest(RequestPayload):
    """Add a new parameter to a node.

    Use when: Dynamically adding inputs/outputs to nodes, customizing node interfaces,
    building configurable nodes. Supports type validation, tooltips, and mode restrictions.

    Args:
        node_name: Name of the node to add parameter to (None for current context)
        parameter_name: Name of the new parameter (None for auto-generated)
        default_value: Default value for the parameter
        tooltip: General tooltip text or structured tooltip
        tooltip_as_input: Tooltip when parameter is used as input
        tooltip_as_property: Tooltip when parameter is used as property
        tooltip_as_output: Tooltip when parameter is used as output
        type: Parameter type string
        input_types: List of allowed input types
        output_type: Output type for the parameter
        ui_options: UI configuration options
        mode_allowed_input: Whether parameter can be used as input
        mode_allowed_property: Whether parameter can be used as property
        mode_allowed_output: Whether parameter can be used as output
        is_user_defined: Whether this is a user-defined parameter (affects serialization)
        parent_container_name: Name of parent container if nested
        parent_element_name: Name of parent element if nested
        initial_setup: Skip setup work when loading from file
        settable: Whether parameter can be set directly by the user or not

    Results: AddParameterToNodeResultSuccess (with parameter name) | AddParameterToNodeResultFailure
    """

    # If node name is None, use the Current Context
    node_name: str | None = None
    parameter_name: str | None = None
    default_value: Any | None = None
    tooltip: str | list[dict] | None = None
    tooltip_as_input: str | list[dict] | None = None
    tooltip_as_property: str | list[dict] | None = None
    tooltip_as_output: str | list[dict] | None = None
    type: str | None = None
    input_types: list[str] | None = None
    output_type: str | None = None
    ui_options: dict | None = None
    mode_allowed_input: bool = field(default=True)
    mode_allowed_property: bool = field(default=True)
    mode_allowed_output: bool = field(default=True)
    is_user_defined: bool = field(default=True)
    settable: bool = field(default=True)
    parent_container_name: str | None = None
    parent_element_name: str | None = None
    # initial_setup prevents unnecessary work when we are loading a workflow from a file.
    initial_setup: bool = False

    @classmethod
    def create(cls, **kwargs) -> AddParameterToNodeRequest:
        if "name" in kwargs:
            name = kwargs.pop("name")
            kwargs["parameter_name"] = name
        known_attrs = {k: v for k, v in kwargs.items() if k in cls.__annotations__}
        # Create instance with known attributes and extra_attrs dict
        instance = cls(**known_attrs)
        return instance


@dataclass
@PayloadRegistry.register
class AddParameterToNodeResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Parameter added successfully to node.

    Args:
        parameter_name: Name of the new parameter
        type: Type of the parameter
        node_name: Name of the node parameter was added to
    """

    parameter_name: str
    type: str
    node_name: str


@dataclass
@PayloadRegistry.register
class AddParameterToNodeResultFailure(ResultPayloadFailure):
    """Parameter addition failed. Common causes: node not found, invalid parameter name, type conflicts."""


@dataclass
@PayloadRegistry.register
class RemoveParameterFromNodeRequest(RequestPayload):
    """Remove a parameter from a node.

    Use when: Cleaning up unused parameters, dynamically restructuring node interfaces,
    removing deprecated parameters. Handles cleanup of connections and values.

    Args:
        parameter_name: Name of the parameter to remove
        node_name: Name of the node to remove parameter from (None for current context)

    Results: RemoveParameterFromNodeResultSuccess | RemoveParameterFromNodeResultFailure
    """

    parameter_name: str
    # If node name is None, use the Current Context
    node_name: str | None = None


@dataclass
@PayloadRegistry.register
class RemoveParameterFromNodeResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Parameter removed successfully from node. Connections and values cleaned up."""


@dataclass
@PayloadRegistry.register
class RemoveParameterFromNodeResultFailure(ResultPayloadFailure):
    """Parameter removal failed. Common causes: node not found, parameter not found, removal not allowed."""


@dataclass
@PayloadRegistry.register
class AddParameterGroupToNodeRequest(RequestPayload):
    """Add a new ParameterGroup to a node.

    Use when: Dynamically organizing parameters into collapsible groups,
    structuring node interfaces, building nodes with categorized parameters.

    Args:
        node_name: Name of the node to add group to (None for current context)
        group_name: Name of the new parameter group
        parent_element_name: Name of parent element if nested (None for root level)
        ui_options: UI configuration options (e.g., {"collapsed": True})
        is_user_defined: Whether this is a user-defined group (affects serialization)
        initial_setup: Skip setup work when loading from file

    Results: AddParameterGroupToNodeResultSuccess | AddParameterGroupToNodeResultFailure
    """

    group_name: str
    node_name: str | None = None
    parent_element_name: str | None = None
    ui_options: dict = field(default_factory=dict)
    is_user_defined: bool = True
    initial_setup: bool = False


@dataclass
@PayloadRegistry.register
class AddParameterGroupToNodeResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """ParameterGroup added successfully to node.

    Args:
        group_name: Name of the new parameter group
        node_name: Name of the node group was added to
    """

    group_name: str
    node_name: str


@dataclass
@PayloadRegistry.register
class AddParameterGroupToNodeResultFailure(ResultPayloadFailure):
    """ParameterGroup addition failed. Common causes: node not found, invalid group name, group already exists."""


@dataclass
@PayloadRegistry.register
class AlterParameterGroupDetailsRequest(RequestPayload):
    """Alter the details and configuration of a ParameterGroup.

    Use when: Modifying ParameterGroup UI options (collapsed state, visibility, etc.),
    updating group configuration after creation, implementing save/load of group state changes.

    Args:
        group_name: Name of the ParameterGroup to alter
        node_name: Name of the node containing the group (None for current context)
        ui_options: New UI configuration options
        initial_setup: Skip setup work when loading from file

    Results: AlterParameterGroupDetailsResultSuccess | AlterParameterGroupDetailsResultFailure
    """

    group_name: str
    node_name: str | None = None
    ui_options: dict | None = None
    initial_setup: bool = False

    @classmethod
    def relevant_parameters(cls) -> list[str]:
        return ["group_name", "node_name", "ui_options"]


@dataclass
@PayloadRegistry.register
class AlterParameterGroupDetailsResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """ParameterGroup details altered successfully."""


@dataclass
@PayloadRegistry.register
class AlterParameterGroupDetailsResultFailure(ResultPayloadFailure):
    """ParameterGroup details alteration failed. Common causes: node not found, group not found."""


@dataclass
@PayloadRegistry.register
class SetParameterValueRequest(RequestPayload):
    """Set the value of a parameter on a node.

    Use when: Configuring node inputs, setting property values, loading saved workflows,
    updating parameter values programmatically. Handles type validation and conversion.

    Args:
        parameter_name: Name of the parameter to set
        value: Value to set for the parameter
        node_name: Name of the node containing the parameter (None for current context)
        data_type: Expected data type for validation (None for auto-detection)
        initial_setup: Skip setup work when loading from file
        is_output: Whether this is an output value (used when loading workflows)

    Results: SetParameterValueResultSuccess (with finalized value) | SetParameterValueResultFailure
    """

    parameter_name: str
    value: str | int | float | bool | dict | list | None = field(metadata={BLOB_FIELD_METADATA_KEY: True})
    # If node name is None, use the Current Context
    node_name: str | None = None
    data_type: str | None = None
    # initial_setup prevents unnecessary work when we are loading a workflow from a file.
    initial_setup: bool = False
    # is_output is true when the value being saved is from an output value. Used when loading a workflow from a file.
    is_output: bool = False
    # incoming_connection_source fields identify when this request comes from upstream node value passing during resolution
    # Both must be None (manual/user request) or both must be set (incoming connection source request)
    incoming_connection_source_node_name: str | None = None
    incoming_connection_source_parameter_name: str | None = None


@dataclass
@PayloadRegistry.register
class SetParameterValueResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Parameter value set successfully. Value may have been processed or converted.

    Args:
        finalized_value: The actual value stored after processing
        data_type: The determined data type of the value
    """

    finalized_value: Any = field(metadata={BLOB_FIELD_METADATA_KEY: True})
    data_type: str


@dataclass
@PayloadRegistry.register
class SetParameterValueResultFailure(ResultPayloadFailure):
    """Parameter value setting failed.

    Common causes: node not found, parameter not found,
    type validation error, value conversion error.
    """


@dataclass
@PayloadRegistry.register
class GetParameterDetailsRequest(RequestPayload):
    """Get detailed information about a parameter.

    Use when: Inspecting parameter configuration, validating parameter properties,
    building UIs that display parameter details, understanding parameter capabilities.

    Args:
        parameter_name: Name of the parameter to get details for
        node_name: Name of the node containing the parameter (None for current context)

    Results: GetParameterDetailsResultSuccess (with full details) | GetParameterDetailsResultFailure
    """

    parameter_name: str
    # If node name is None, use the Current Context
    node_name: str | None = None


@dataclass
@PayloadRegistry.register
class GetParameterDetailsResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Parameter details retrieved successfully.

    Args:
        element_id: Unique identifier for the parameter
        type: Parameter type
        input_types: Accepted input types
        output_type: Output type when used as output
        default_value: Default value if any
        tooltip: General tooltip text
        tooltip_as_input/property/output: Mode-specific tooltips
        mode_allowed_input/property/output: Which modes are allowed
        is_user_defined: Whether this is a user-defined parameter
        settable: Whether parameter can be set directly by the user or not (None for non-Parameters)
        private: Whether this parameter is private
        ui_options: UI configuration options
    """

    element_id: str
    type: str
    input_types: list[str]
    output_type: str
    default_value: Any | None
    tooltip: str | list[dict]
    tooltip_as_input: str | list[dict] | None
    tooltip_as_property: str | list[dict] | None
    tooltip_as_output: str | list[dict] | None
    mode_allowed_input: bool
    mode_allowed_property: bool
    mode_allowed_output: bool
    is_user_defined: bool
    settable: bool | None
    private: bool
    ui_options: dict | None


@dataclass
@PayloadRegistry.register
class GetParameterDetailsResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Parameter details retrieval failed. Common causes: node not found, parameter not found."""


@dataclass
@PayloadRegistry.register
class AlterParameterDetailsRequest(RequestPayload):
    """Alter the details and configuration of a parameter.

    Use when: Modifying parameter types, updating tooltips, changing allowed modes,
    configuring UI options, updating parameter constraints after creation.

    Args:
        parameter_name: Name of the parameter to alter
        node_name: Name of the node containing the parameter (None for current context)
        type: New parameter type
        input_types: New list of accepted input types
        output_type: New output type when used as output
        default_value: New default value
        clear_default_value: If True, clear the parameter default (set to None)
        tooltip: New general tooltip text
        tooltip_as_input: New tooltip when used as input
        tooltip_as_property: New tooltip when used as property
        tooltip_as_output: New tooltip when used as output
        mode_allowed_input: Whether parameter can be used as input
        mode_allowed_property: Whether parameter can be used as property
        mode_allowed_output: Whether parameter can be used as output
        settable: Whether parameter can be set directly by the user or not
        ui_options: New UI configuration options
        traits: Set of parameter traits
        initial_setup: Skip setup work when loading from file

    Results: AlterParameterDetailsResultSuccess | AlterParameterDetailsResultFailure
    """

    parameter_name: str
    # If node name is None, use the Current Context
    node_name: str | None = None
    type: str | None = None
    input_types: list[str] | None = None
    output_type: str | None = None
    default_value: Any | None = None
    clear_default_value: bool = False
    tooltip: str | list[dict] | None = None
    tooltip_as_input: str | list[dict] | None = None
    tooltip_as_property: str | list[dict] | None = None
    tooltip_as_output: str | list[dict] | None = None
    mode_allowed_input: bool | None = None
    mode_allowed_property: bool | None = None
    mode_allowed_output: bool | None = None
    settable: bool | None = None
    ui_options: dict | None = None
    traits: set[str] | None = None
    # initial_setup prevents unnecessary work when we are loading a workflow from a file.
    initial_setup: bool = False

    @classmethod
    def create(cls, **kwargs) -> AlterParameterDetailsRequest:
        if "allowed_modes" in kwargs:
            kwargs["mode_allowed_input"] = ParameterMode.INPUT in kwargs["allowed_modes"]
            kwargs["mode_allowed_output"] = ParameterMode.OUTPUT in kwargs["allowed_modes"]
            kwargs["mode_allowed_property"] = ParameterMode.PROPERTY in kwargs["allowed_modes"]
            kwargs.pop("allowed_modes")
        if "name" in kwargs:
            name = kwargs.pop("name")
            kwargs["parameter_name"] = name
        known_attrs = {k: v for k, v in kwargs.items() if k in cls.__annotations__}

        # Create instance with known attributes and extra_attrs dict
        instance = cls(**known_attrs)
        return instance

    @classmethod
    def relevant_parameters(cls) -> list[str]:
        return [
            "parameter_name",
            "node_name",
            "type",
            "input_types",
            "output_type",
            "default_value",
            "clear_default_value",
            "tooltip",
            "tooltip_as_input",
            "tooltip_as_property",
            "tooltip_as_output",
            "mode_allowed_input",
            "mode_allowed_property",
            "mode_allowed_output",
            "settable",
            "ui_options",
            "traits",
        ]


@dataclass
@PayloadRegistry.register
class AlterParameterDetailsResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    pass


@dataclass
@PayloadRegistry.register
class AlterParameterDetailsResultFailure(ResultPayloadFailure):
    pass


@dataclass
@PayloadRegistry.register
class GetParameterValueRequest(RequestPayload):
    """Get the current value of a parameter.

    Use when: Reading parameter values, debugging workflow state, displaying current values in UIs,
    validating parameter states before execution.

    Args:
        parameter_name: Name of the parameter to get value for
        node_name: Name of the node containing the parameter (None for current context)

    Results: GetParameterValueResultSuccess (with value and type info) | GetParameterValueResultFailure
    """

    parameter_name: str
    # If node name is None, use the Current Context
    node_name: str | None = None


@dataclass
@PayloadRegistry.register
class GetParameterValueResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Parameter value retrieved successfully.

    Args:
        input_types: Accepted input types
        type: Current parameter type
        output_type: Output type when used as output
        value: Current parameter value
    """

    input_types: list[str]
    type: str
    output_type: str
    value: Any = field(metadata={BLOB_FIELD_METADATA_KEY: True})


@dataclass
@PayloadRegistry.register
class GetParameterValueResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Parameter value retrieval failed. Common causes: node not found, parameter not found."""


@dataclass
@PayloadRegistry.register
class OnParameterValueChanged(WorkflowAlteredMixin, ResultPayloadSuccess):
    node_name: str
    parameter_name: str
    data_type: str
    value: Any = field(metadata={BLOB_FIELD_METADATA_KEY: True})


@dataclass
@PayloadRegistry.register
class GetCompatibleParametersRequest(RequestPayload):
    """Get parameters that are compatible for connections.

    Use when: Creating connections between nodes, validating connection compatibility,
    building connection UIs, discovering available connection targets.

    Args:
        parameter_name: Name of the parameter to find compatible parameters for
        is_output: Whether the parameter is an output parameter
        node_name: Name of the node containing the parameter (None for current context)

    Results: GetCompatibleParametersResultSuccess (with compatible parameters) | GetCompatibleParametersResultFailure
    """

    parameter_name: str
    is_output: bool
    # If node name is None, use the Current Context
    node_name: str | None = None


class ParameterAndMode(NamedTuple):
    parameter_name: str
    is_output: bool


@dataclass
@PayloadRegistry.register
class GetCompatibleParametersResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Compatible parameters retrieved successfully.

    Args:
        valid_parameters_by_node: Dictionary mapping node names to lists of compatible parameters
    """

    valid_parameters_by_node: dict[str, list[ParameterAndMode]]


@dataclass
@PayloadRegistry.register
class GetCompatibleParametersResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Compatible parameters retrieval failed. Common causes: node not found, parameter not found."""


@dataclass
@PayloadRegistry.register
class GetNodeElementDetailsRequest(RequestPayload):
    """Get detailed information about a node element.

    Use when: Inspecting node structure, debugging element configuration,
    building advanced UIs, understanding node composition.

    Args:
        node_name: Name of the node to get element details for (None for current context)
        specific_element_id: ID of specific element to get details for (None for root)

    Results: GetNodeElementDetailsResultSuccess (with element details) | GetNodeElementDetailsResultFailure
    """

    # If node name is None, use the Current Context
    node_name: str | None = None
    specific_element_id: str | None = None  # Pass None to use the root


@dataclass
@PayloadRegistry.register
class GetNodeElementDetailsResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    element_details: dict[str, Any] = field(metadata={BLOB_FIELD_METADATA_KEY: True})


@dataclass
@PayloadRegistry.register
class GetNodeElementDetailsResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    pass


# This is the same as getparameterelementdetailsrequest, might have to modify it a bit.
@dataclass
@PayloadRegistry.register
class AlterElementEvent(ExecutionPayload):
    element_details: dict[str, Any] = field(metadata={BLOB_FIELD_METADATA_KEY: True})


@dataclass
@PayloadRegistry.register
class RenameParameterRequest(RequestPayload):
    """Rename a parameter on a node.

    Use when: Refactoring parameter names, improving parameter clarity, updating parameter
    naming conventions. Handles updating connections and references.

    Args:
        parameter_name: Current name of the parameter
        new_parameter_name: New name for the parameter
        node_name: Name of the node containing the parameter (None for current context)
        initial_setup: Skip setup work when loading from file

    Results: RenameParameterResultSuccess (with old and new names) | RenameParameterResultFailure
    """

    parameter_name: str
    new_parameter_name: str
    # If node name is None, use the Current Context
    node_name: str | None = None
    # initial_setup prevents unnecessary work when we are loading a workflow from a file.
    initial_setup: bool = False


@dataclass
@PayloadRegistry.register
class RenameParameterResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Parameter renamed successfully. Connections and references updated.

    Args:
        old_parameter_name: Previous parameter name
        new_parameter_name: New parameter name
        node_name: Name of the node containing the parameter
    """

    old_parameter_name: str
    new_parameter_name: str
    node_name: str


@dataclass
@PayloadRegistry.register
class RenameParameterResultFailure(ResultPayloadFailure):
    """Parameter rename failed.

    Common causes: node not found, parameter not found,
    name already exists, invalid new name.
    """


@dataclass
@PayloadRegistry.register
class GetConnectionsForParameterRequest(RequestPayload):
    """Get connections for a specific parameter on a node.

    Use when: Checking if a parameter is connected, getting connection details for a parameter,
    validating parameter connection state, building connection-aware UIs.

    Args:
        parameter_name: Name of the parameter to get connections for
        node_name: Name of the node containing the parameter (None for current context)

    Results: GetConnectionsForParameterResultSuccess (with connection details) | GetConnectionsForParameterResultFailure
    """

    parameter_name: str
    # If node name is None, use the Current Context
    node_name: str | None = None


@dataclass
@PayloadRegistry.register
class GetConnectionsForParameterResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Parameter connections retrieved successfully.

    Args:
        parameter_name: Name of the parameter
        node_name: Name of the node containing the parameter
        incoming_connections: List of incoming connections to this parameter
        outgoing_connections: List of outgoing connections from this parameter
    """

    parameter_name: str
    node_name: str
    incoming_connections: list[IncomingConnection]
    outgoing_connections: list[OutgoingConnection]

    def has_incoming_connections(self) -> bool:
        """Check if the parameter has any incoming connections."""
        return len(self.incoming_connections) > 0

    def has_outgoing_connections(self) -> bool:
        """Check if the parameter has any outgoing connections."""
        return len(self.outgoing_connections) > 0


@dataclass
@PayloadRegistry.register
class GetConnectionsForParameterResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Parameter connections retrieval failed. Common causes: node not found, parameter not found."""


@dataclass
@PayloadRegistry.register
class RemoveElementEvent(ExecutionPayload):
    element_id: str


# Migration Events
@dataclass
class ConversionConfig:
    """Configuration for parameter conversion using intermediate nodes.

    Args:
        library: Library containing the conversion node type
        node_type: Type of node to create for conversion
        input_parameter: Parameter name on the conversion node to connect input to
        output_parameter: Parameter name on the conversion node to connect output from
        additional_parameters: Additional parameters to set on the conversion node
        offset_side: Reference side/position from target node (defaults to "left" for input, "right" for output)
        offset_x: X offset for positioning the conversion node relative to target node
        offset_y: Y offset for positioning the conversion node relative to target node
    """

    library: str
    node_type: str
    input_parameter: str
    output_parameter: str
    additional_parameters: dict[str, Any] | None = None
    offset_side: str | None = None
    offset_x: int = 0
    offset_y: int = 0


@dataclass
@PayloadRegistry.register
class ReorderParameterListItemRequest(RequestPayload):
    """Reorder an item within a ParameterList.

    Use when: User drags to reorder list items in the UI, implementing undo/redo for list reordering,
    programmatically organizing list items. Moves an item from one position to another within the same list.

    Args:
        parameter_list_name: Name of the ParameterList containing the items
        from_index: Current index of the item to move (0-based)
        to_index: Destination index where the item should be moved (0-based)
        node_name: Name of the node containing the ParameterList (None for current context)

    Results: ReorderParameterListItemResultSuccess | ReorderParameterListItemResultFailure (list not found, invalid indices)
    """

    parameter_list_name: str
    from_index: int
    to_index: int
    node_name: str | None = None


@dataclass
@PayloadRegistry.register
class ReorderParameterListItemResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """ParameterList item reordered successfully. List now reflects the new order."""


@dataclass
@PayloadRegistry.register
class ReorderParameterListItemResultFailure(ResultPayloadFailure):
    """ParameterList item reordering failed.

    Common causes: node not found, parameter list not found, invalid indices,
    from_index equals to_index, index out of range.
    """


@dataclass
@PayloadRegistry.register
class MigrateParameterRequest(RequestPayload):
    """Request to migrate a parameter from one node to another with optional conversions.

    Args:
        source_node_name: Name of the source node
        target_node_name: Name of the target node
        source_parameter_name: Name of the parameter to migrate from
        target_parameter_name: Name of the parameter to migrate to
        input_conversion: Configuration for converting incoming connections
        output_conversion: Configuration for converting outgoing connections
        value_transform: Function to transform values when no connections exist
        break_connections: If True, break any existing connections for the original parameter
    """

    source_node_name: str
    target_node_name: str
    source_parameter_name: str
    target_parameter_name: str
    input_conversion: ConversionConfig | None = None
    output_conversion: ConversionConfig | None = None
    value_transform: Callable | None = None
    break_connections: bool = True


@dataclass
@PayloadRegistry.register
class MigrateParameterResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Parameter migration completed successfully."""


@dataclass
@PayloadRegistry.register
class MigrateParameterResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Parameter migration failed."""
