"""The vocabulary domains use to describe reversible operations.

This module is domain-agnostic: it defines what an undo entry and an undo batch are, the shared
lifecycle triage every recording strategy applies, and the `RecordingStrategy` contract a strategy
implements. `UndoManager` (the mechanism) and the concrete strategies (currently the whole-flow
`SnapshotRecordingSession`) both build on these types.

The `RecordingStrategy` protocol is the extension seam: a future finer-grained strategy (e.g. one
that captures per-touched-entity state deltas instead of whole-flow snapshots) implements this same
contract and drops in behind `UndoManager` without changing the dispatch path or the lifecycle
policy.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Any, Protocol

from griptape_nodes.retained_mode.events.context_events import SetWorkflowContextRequest
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.undo_events import (
    ClearUndoStateRequest,
    GetUndoStateRequest,
    RedoRequest,
    UndoRequest,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    ImportWorkflowRequest,
    RunWorkflowFromRegistryRequest,
    RunWorkflowFromScratchRequest,
    RunWorkflowWithCurrentStateRequest,
)

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload


# Request types that invalidate all undo history whenever they are dispatched, regardless of origin,
# because they replace or restructure the whole object graph. This is global lifecycle policy shared
# by every recording strategy, so it lives here rather than being duplicated in each session.
CLEAR_HISTORY_REQUEST_TYPES: tuple[type[RequestPayload], ...] = (
    ClearAllObjectStateRequest,
    SetWorkflowContextRequest,
    RunWorkflowFromScratchRequest,
    RunWorkflowFromRegistryRequest,
    RunWorkflowWithCurrentStateRequest,
    ImportWorkflowRequest,
)

# The undo manager's own request types. They alter workflow state by design (undo/redo) or not at all
# (state/clear) and must never be recorded or clear history.
OWN_EVENT_TYPES: tuple[type[RequestPayload], ...] = (
    UndoRequest,
    RedoRequest,
    GetUndoStateRequest,
    ClearUndoStateRequest,
)


class DispatchTriage(Enum):
    """The shared lifecycle verdict for a dispatch, applied before any strategy-specific framing.

    IGNORE: the undo system does not track this dispatch at all (its own events, or a replay in
        progress). CLEAR_HISTORY: a lifecycle request that replaces or restructures the whole object
        graph; the strategy clears history and flags any open frame. PROCEED: an ordinary dispatch
        the strategy frames per its own logic.
    """

    IGNORE = auto()
    CLEAR_HISTORY = auto()
    PROCEED = auto()


def triage_dispatch(request: RequestPayload, *, is_replaying: bool) -> DispatchTriage:
    """Classify a dispatch by the shared lifecycle policy, before any strategy-specific framing.

    Replay is isolated before the history-clearing check: a mutation dispatched during undo/redo
    must never clear history, even if it is (or cascades into) a history-clearing lifecycle type.
    Every recording strategy calls this so the policy sequence lives in one place and cannot drift.
    """
    if type(request) in OWN_EVENT_TYPES:
        return DispatchTriage.IGNORE
    if is_replaying:
        return DispatchTriage.IGNORE
    if isinstance(request, CLEAR_HISTORY_REQUEST_TYPES):
        return DispatchTriage.CLEAR_HISTORY
    return DispatchTriage.PROCEED


class UndoEntryReplayError(RuntimeError):
    """Raised when replaying an undo/redo entry fails and the undo history can no longer be trusted."""


class UndoEntry(ABC):
    """A single reversible operation within an undo batch.

    Implementations issue ordinary engine requests to revert (undo) or re-apply (redo)
    the operation, raising UndoEntryReplayError when replay fails.
    """

    @abstractmethod
    def undo(self) -> None:
        """Revert the recorded operation. Raises UndoEntryReplayError on failure."""

    @abstractmethod
    def redo(self) -> None:
        """Re-apply the recorded operation. Raises UndoEntryReplayError on failure."""


@dataclass
class UndoBatch:
    """One undoable user action, made up of one or more entries replayed together.

    Attributes:
        label: Human-readable description of the action (e.g. "Create node 'Agent_1'").
        entries: Entries recorded in application order. Undo replays them in reverse.
    """

    label: str
    entries: list[UndoEntry]


class RecordingStrategy(Protocol):
    """The surface UndoManager and the EventManager dispatch path drive, independent of strategy.

    One implementation exists today (the whole-flow SnapshotRecordingSession); this protocol is the
    seam a future finer-grained strategy plugs into. UndoManager holds a strategy without knowing
    which, so strategies can diverge in mechanism but not in the surface (or the shared lifecycle
    policy) they honor.
    """

    def register_non_undoable(self, *request_types: type[RequestPayload]) -> None:
        """Declare request types that mutate workflow state but are intentionally not undoable."""
        ...

    def transaction(self, label: str) -> AbstractContextManager[None]:
        """Group every recordable mutation issued within the block into a single undo batch."""
        ...

    def begin_request_dispatch(self, request: RequestPayload, request_id: str | None) -> Any:
        """Observe a request before its handler runs; return an opaque capture for end_request_dispatch."""
        ...

    def end_request_dispatch(self, capture: Any, request: RequestPayload, result: ResultPayload | None) -> None:
        """Finish observing a dispatch: contribute to the active frame and finalize if it opened one."""
        ...
