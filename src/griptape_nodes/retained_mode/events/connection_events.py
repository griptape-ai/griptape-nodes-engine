from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from griptape_nodes.retained_mode.events.base_events import (
    RequestPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    WorkflowAlteredMixin,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry

if TYPE_CHECKING:
    from griptape_nodes.exe_types.implicit_conversion import ImplicitConversion


@dataclass
@PayloadRegistry.register
class CreateConnectionRequest(RequestPayload):
    """Create a connection between two node parameters.

    Use when: Connecting node outputs to inputs, building data flow between nodes,
    loading saved workflows. Validates type compatibility and connection rules.

    Args:
        source_parameter_name: Name of the parameter providing the data
        target_parameter_name: Name of the parameter receiving the data
        source_node_name: Name of the source node (None for current context)
        target_node_name: Name of the target node (None for current context)
        initial_setup: Skip setup work when loading from file
        is_node_group_internal: Mark this connection as internal to a node group (for DAG building)

    Results: CreateConnectionResultSuccess | CreateConnectionResultFailure (incompatible types, invalid nodes/parameters)
    """

    source_parameter_name: str
    target_parameter_name: str
    # If node name is None, use the Current Context
    source_node_name: str | None = None
    target_node_name: str | None = None
    # initial_setup prevents unnecessary work when we are loading a workflow from a file.
    initial_setup: bool = False
    # Mark this connection as internal to a node group proxy parameter
    is_node_group_internal: bool = False


@dataclass
class ConnectionConversion:
    """How a connection converts its value to the type its target takes.

    Present only on connections into a parameter that opted in to implicit conversion and whose
    source type it doesn't accept as-is. The editor draws these connections differently.

    Args:
        source_type: The type the source sends
        target_type: The type the value is converted to
        lossy: The conversion can discard information (a float becoming a whole number)
        may_fail: The conversion depends on the value and can fail when the flow runs (text that
            isn't a number or isn't JSON)
    """

    source_type: str
    target_type: str
    lossy: bool = False
    may_fail: bool = False

    @classmethod
    def describe(cls, conversion: ImplicitConversion | None) -> ConnectionConversion | None:
        """Describe a connection's conversion for the editor, or None for a connection without one."""
        if conversion is None:
            return None
        return cls(
            source_type=conversion.source_type,
            target_type=conversion.target_type,
            lossy=conversion.lossy,
            may_fail=conversion.may_fail,
        )


@dataclass
class ReplacedConnection:
    """A connection that was deleted to make room for a new one."""

    source_node_name: str
    source_parameter_name: str
    target_node_name: str
    target_parameter_name: str


@dataclass
@PayloadRegistry.register
class CreateConnectionResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Connection created successfully between parameters.

    Args:
        replaced_connection: The existing connection that was deleted to make room for this one,
            because the target input or source output allows only one connection (None if nothing
            was replaced)
        conversion: How the connection converts its value to the target's type (None if the
            target takes the source's type as-is)
    """

    replaced_connection: ReplacedConnection | None = None
    conversion: ConnectionConversion | None = None


@dataclass
@PayloadRegistry.register
class CreateConnectionResultFailure(ResultPayloadFailure):
    """Connection creation failed.

    Common causes: incompatible types, nodes/parameters not found,
    connection already exists, circular dependency.
    """


@dataclass
@PayloadRegistry.register
class DeleteConnectionRequest(RequestPayload):
    """Delete a connection between two node parameters.

    Use when: Removing unwanted connections, restructuring workflows, disconnecting nodes,
    updating data flow. Cleans up connection state and updates node resolution.

    Args:
        source_parameter_name: Name of the parameter providing the data
        target_parameter_name: Name of the parameter receiving the data
        source_node_name: Name of the source node (None for current context)
        target_node_name: Name of the target node (None for current context)

    Results: DeleteConnectionResultSuccess | DeleteConnectionResultFailure (connection not found, nodes/parameters not found)
    """

    source_parameter_name: str
    target_parameter_name: str
    # If node name is None, use the Current Context
    source_node_name: str | None = None
    target_node_name: str | None = None


@dataclass
@PayloadRegistry.register
class DeleteConnectionResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Connection deleted successfully. Connection state cleaned up."""


@dataclass
@PayloadRegistry.register
class DeleteConnectionResultFailure(ResultPayloadFailure):
    """Connection deletion failed. Common causes: connection not found, nodes/parameters not found."""


@dataclass
@PayloadRegistry.register
class ListConnectionsForNodeRequest(RequestPayload):
    """List all connections for a specific node.

    Use when: Inspecting node connectivity, building connection visualizations,
    debugging data flow, validating workflow structure.

    Args:
        node_name: Name of the node to list connections for (None for current context)
        include_internal: Whether to include internal connections (connections where both nodes are in the same group). Defaults to True.

    Results: ListConnectionsForNodeResultSuccess (with connection lists) | ListConnectionsForNodeResultFailure (node not found)
    """

    # If node name is None, use the Current Context
    node_name: str | None = None
    # Whether to include internal connections to groups (defaults to True for backward compatibility)
    include_internal: bool = True


@dataclass
class IncomingConnection:
    source_node_name: str
    source_parameter_name: str
    target_parameter_name: str
    # How the connection converts its value to the target's type; None if it doesn't.
    conversion: ConnectionConversion | None = None


@dataclass
class OutgoingConnection:
    source_parameter_name: str
    target_node_name: str
    target_parameter_name: str
    # How the connection converts its value to the target's type; None if it doesn't.
    conversion: ConnectionConversion | None = None


@dataclass
@PayloadRegistry.register
class ListConnectionsForNodeResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Node connections retrieved successfully.

    Args:
        incoming_connections: List of connections feeding into this node
        outgoing_connections: List of connections from this node to others
    """

    incoming_connections: list[IncomingConnection]
    outgoing_connections: list[OutgoingConnection]


@dataclass
@PayloadRegistry.register
class ListConnectionsForNodeResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Node connections listing failed. Common causes: node not found, no current context."""
