"""The vocabulary domains use to describe reversible operations.

This module is domain-agnostic: it defines what an undo entry and a recorder are, plus the
replay helpers domains call to issue their inverses. `UndoManager` (mechanism) and the per-domain
recorders (knowledge) both build on these types.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import dataclass
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
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

if TYPE_CHECKING:
    from collections.abc import Sequence
    from contextlib import AbstractContextManager

    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload, ResultPayloadSuccess


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


class UndoEntryReplayError(RuntimeError):
    """Raised when replaying an undo/redo entry fails and the undo history can no longer be trusted."""


def dispatch_expecting[T: "ResultPayloadSuccess"](
    request: RequestPayload, success_type: type[T], action_description: str
) -> T:
    """Dispatch a request during undo/redo replay, raising if it does not produce the expected success result.

    Domain-defined undo entries use this to replay a specific inverse and assert its result type.
    """
    result = GriptapeNodes.handle_request(request)
    if not isinstance(result, success_type):
        msg = f"Attempted to {action_description}. Failed with result details: {result.result_details}"
        raise UndoEntryReplayError(msg)
    return result


def dispatch_expecting_success(request: RequestPayload, action_description: str) -> ResultPayload:
    """Dispatch a request during undo/redo replay, raising if it fails.

    Used by the generic RequestReplayUndoEntry, which does not care about the concrete result type.
    """
    result = GriptapeNodes.handle_request(request)
    if result.failed():
        msg = f"Attempted to {action_description}. Failed with result details: {result.result_details}"
        raise UndoEntryReplayError(msg)
    return result


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


@dataclass
class RequestReplayUndoEntry(UndoEntry):
    """Generic entry that reverts/re-applies an operation by replaying stored requests.

    Produced by UndoManager.record_inverse: the undo direction replays the inverse request(s)
    the handler supplied; the redo direction replays the original forward request(s).
    """

    undo_requests: list[RequestPayload]
    redo_requests: list[RequestPayload]

    def undo(self) -> None:
        for request in self.undo_requests:
            # Deep-copy before dispatch so a handler that writes back to its request (e.g.
            # SetParameterValue normalizing value/data_type) cannot mutate the stored inverse and
            # let it drift across repeated undo/redo cycles.
            dispatch_expecting_success(deepcopy(request), f"undo via replaying {type(request).__name__}")

    def redo(self) -> None:
        for request in self.redo_requests:
            dispatch_expecting_success(deepcopy(request), f"redo via replaying {type(request).__name__}")


@dataclass
class RecorderCapture:
    """Outcome of a recorder's before-dispatch capture.

    Attributes:
        declined: True when the recorder cannot faithfully record this request (the manager
            treats the request as an unrecordable mutation and invalidates history if it succeeds).
        state: Opaque recorder-specific state handed back to create_batch after dispatch.
    """

    declined: bool = False
    state: Any = None


class UndoRecorder(ABC):
    """Records the information needed to build an UndoBatch for one request type.

    A recorder lives in the module that owns the reversal knowledge for its domain (e.g. node
    recorders live in undo.recorders.node) and is registered via UndoManager.register_recorder by
    the owning manager. Use a recorder (rather than record_inverse) when reversing an operation
    requires state captured *before* the handler runs (e.g. serializing a node before it is
    deleted) or *after* it (e.g. the assigned name of a freshly created node).
    """

    @abstractmethod
    def capture_before(self, request: RequestPayload) -> RecorderCapture:
        """Capture any 'before' state required to reverse the request. Runs before the handler."""

    @abstractmethod
    def create_batch(self, request: RequestPayload, result: ResultPayload, state: Any) -> UndoBatch | None:
        """Build the undo batch after the handler succeeded.

        Return a batch with one or more entries to record the reversal. Return an empty batch (no
        entries) to record nothing without invalidating history -- e.g. a no-op edit, or a variant
        of the request the recorder deliberately does not reverse. Return None to invalidate history
        (the recorder cannot faithfully reverse a mutation that did happen).
        """


class RecordingStrategy(Protocol):
    """The surface UndoManager and the EventManager dispatch path drive, independent of strategy.

    Two implementations exist and are interchangeable behind this contract: the default
    inverse-command RecordingSession and the experimental whole-flow SnapshotRecordingSession. The
    manager selects one at startup and holds it as a RecordingStrategy without knowing which, so the
    two can diverge in mechanism but not in the surface (or the shared lifecycle policy) they honor.
    """

    def register_recorder(self, request_type: type[RequestPayload], recorder: UndoRecorder) -> None:
        """Register the recorder a domain uses to reverse one of its request types."""
        ...

    def register_non_undoable(self, *request_types: type[RequestPayload]) -> None:
        """Declare request types that mutate workflow state but are intentionally not undoable."""
        ...

    def register_inverse_floor(self, *request_types: type[RequestPayload]) -> None:
        """Declare editor mutations that have no inverse recorder yet."""
        ...

    def record_inverse(
        self,
        inverse: RequestPayload | Sequence[RequestPayload],
        label: str,
        *,
        forward: RequestPayload | Sequence[RequestPayload] | None = None,
    ) -> None:
        """Declare how to reverse the operation being handled in the current dispatch."""
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
