from dataclasses import dataclass, field

from griptape_nodes.retained_mode.events.base_events import (
    RequestPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    WorkflowAlteredMixin,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry


@dataclass
@PayloadRegistry.register
class UndoRequest(RequestPayload):
    """Undo the most recent undoable user action.

    Use when: Reverting the last workflow edit (e.g. node creation or deletion) in response
    to a user-initiated undo (Ctrl/Cmd+Z).

    Results: UndoResultSuccess (with the label of the undone action) | UndoResultFailure
        (nothing to undo, flow currently running, or replay failed)
    """


@dataclass
@PayloadRegistry.register
class UndoResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Undo applied successfully.

    Args:
        undone_label: Human-readable label of the action that was undone.
    """

    undone_label: str


@dataclass
@PayloadRegistry.register
class UndoResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Undo failed.

    Common causes: undo stack empty, a flow is currently executing, or replaying the
    inverse operations failed (in which case the undo history is cleared).
    """


@dataclass
@PayloadRegistry.register
class RedoRequest(RequestPayload):
    """Re-apply the most recently undone user action.

    Use when: Restoring an action after an undo (Ctrl/Cmd+Shift+Z).

    Results: RedoResultSuccess (with the label of the redone action) | RedoResultFailure
        (nothing to redo, flow currently running, or replay failed)
    """


@dataclass
@PayloadRegistry.register
class RedoResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Redo applied successfully.

    Args:
        redone_label: Human-readable label of the action that was re-applied.
    """

    redone_label: str


@dataclass
@PayloadRegistry.register
class RedoResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Redo failed.

    Common causes: redo stack empty, a flow is currently executing, or replaying the
    operations failed (in which case the undo history is cleared).
    """


@dataclass
@PayloadRegistry.register
class GetUndoStateRequest(RequestPayload):
    """Get the current undo/redo stack state.

    Use when: Enabling/disabling undo and redo UI affordances, displaying the labels of
    the next undoable/redoable actions in menus.

    Results: GetUndoStateResultSuccess (with stack labels)
    """


@dataclass
@PayloadRegistry.register
class GetUndoStateResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Undo/redo state retrieved successfully.

    Args:
        undo_labels: Labels of undoable actions, oldest first (the last entry is next to be undone).
        redo_labels: Labels of redoable actions, oldest first (the last entry is next to be redone).
    """

    undo_labels: list[str] = field(default_factory=list)
    redo_labels: list[str] = field(default_factory=list)


@dataclass
@PayloadRegistry.register
class GetUndoStateResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Undo/redo state retrieval failed."""


@dataclass
@PayloadRegistry.register
class ClearUndoStateRequest(RequestPayload):
    """Clear all undo/redo history.

    Use when: Discarding stale history after operations that invalidate it, resetting state
    in tests, or explicit user request.

    Results: ClearUndoStateResultSuccess
    """


@dataclass
@PayloadRegistry.register
class ClearUndoStateResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Undo/redo history cleared."""


@dataclass
@PayloadRegistry.register
class ClearUndoStateResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Undo/redo history clearing failed."""
