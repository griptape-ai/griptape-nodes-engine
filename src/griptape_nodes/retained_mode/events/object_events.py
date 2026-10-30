from dataclasses import dataclass

from griptape_nodes.retained_mode.events.base_events import (
    RequestPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    WorkflowAlteredMixin,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry


@dataclass
@PayloadRegistry.register
class RenameObjectRequest(RequestPayload):
    """Rename an object (node, flow, etc.) in the system.

    Use when: Organizing workflows, fixing naming conflicts, implementing rename features,
    improving readability. Can automatically find alternative names if requested name is taken.

    Args:
        object_name: Current name of the object to rename
        requested_name: New name for the object
        allow_next_closest_name_available: Whether to use alternative name if requested name is taken

    Results: RenameObjectResultSuccess (with final name) | RenameObjectResultFailure (rename failed)
    """

    object_name: str
    requested_name: str
    allow_next_closest_name_available: bool = False


@dataclass
@PayloadRegistry.register
class RenameObjectResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Object renamed successfully.

    Args:
        final_name: The actual name assigned (may differ from requested if auto-naming was used)
    """

    final_name: str  # May not be the same as what was requested, if that bool was set


@dataclass
@PayloadRegistry.register
class RenameObjectResultFailure(ResultPayloadFailure):
    """Object rename failed.

    Args:
        next_available_name: Suggested alternative name (None if not available)
    """

    next_available_name: str | None = None


@dataclass
@PayloadRegistry.register
class ClearAllObjectStateRequest(RequestPayload):
    """WARNING: Clear all object state - wipes all flows, nodes, connections, everything!

    Use when: Resetting to clean state, recovering from corruption, starting fresh,
    implementing reset functionality. Requires explicit confirmation.

    Args:
        i_know_what_im_doing: Confirmation flag that must be set to True to proceed

    Results: ClearAllObjectStateResultSuccess | ClearAllObjectStateResultFailure (clear failed)
    """

    i_know_what_im_doing: bool = False


@dataclass
@PayloadRegistry.register
class ClearAllObjectStateResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """All object state cleared successfully. System is now in clean state."""


@dataclass
@PayloadRegistry.register
class ClearAllObjectStateResultFailure(ResultPayloadFailure):
    """Object state clearing failed. Common causes: confirmation not provided, clear operation failed."""
