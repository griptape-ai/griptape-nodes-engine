from dataclasses import dataclass

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
    """Take back the last undoable change to a workflow.

    Use when: A user presses Undo, or an agent reverses an edit it made.

    A change is undoable when the request that made it carries an `undo_group`. Undo puts the
    nodes the change touched back the way they were before it, as reopening a save made then would.
    Nodes the change did not touch are left as they are, with their results and anything they hold
    in memory. Workflow variables are left as they are. Unavailable while the workflow is running.

    Results: UndoResultSuccess (with the change's label) | UndoResultFailure (nothing to undo, a run
        or another change is in progress, or the workflow could not be put back)
    """


@dataclass
@PayloadRegistry.register
class UndoResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """The change was taken back.

    Args:
        label: What the change was called, such as "Set Parameter Value".
    """

    label: str


@dataclass
@PayloadRegistry.register
class UndoResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Nothing was undone.

    Args:
        workflow_rebuilt: Whether the undo started and could not finish, so the workflow was rebuilt
            as it was before it, without its results. A client showing the workflow reads it again.
    """

    workflow_rebuilt: bool = False


@dataclass
@PayloadRegistry.register
class RedoRequest(RequestPayload):
    """Put back the change the last undo took back.

    Use when: A user presses Redo.

    Changing the workflow drops what could be redone, except by running it. Unavailable while the
    workflow is running.

    Results: RedoResultSuccess (with the change's label) | RedoResultFailure (nothing to redo, a run
        or another change is in progress, or the workflow could not be put back)
    """


@dataclass
@PayloadRegistry.register
class RedoResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """The change was put back.

    Args:
        label: What the change was called, such as "Set Parameter Value".
    """

    label: str


@dataclass
@PayloadRegistry.register
class RedoResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Nothing was redone.

    Args:
        workflow_rebuilt: Whether the redo started and could not finish, so the workflow was rebuilt
            as it was before it, without its results. A client showing the workflow reads it again.
    """

    workflow_rebuilt: bool = False
