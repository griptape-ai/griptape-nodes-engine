from __future__ import annotations

import copy
import logging
from abc import ABC, abstractmethod
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from griptape_nodes.retained_mode.events.context_events import SetWorkflowContextRequest
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.undo_events import (
    ClearUndoStateRequest,
    ClearUndoStateResultSuccess,
    GetUndoStateRequest,
    GetUndoStateResultSuccess,
    RedoRequest,
    RedoResultFailure,
    RedoResultSuccess,
    UndoRequest,
    UndoResultFailure,
    UndoResultSuccess,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    ImportWorkflowRequest,
    RunWorkflowFromRegistryRequest,
    RunWorkflowFromScratchRequest,
    RunWorkflowWithCurrentStateRequest,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload, ResultPayloadSuccess
    from griptape_nodes.retained_mode.managers.event_manager import EventManager

logger = logging.getLogger("griptape_nodes")

# Maximum number of undoable user actions retained. Oldest entries are dropped first.
MAX_UNDO_BATCHES = 100


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


def _as_sequence(value: RequestPayload | Sequence[RequestPayload]) -> list[RequestPayload]:
    if isinstance(value, (list, tuple)):
        return list(cast("Sequence[RequestPayload]", value))
    return [cast("RequestPayload", value)]


def _prepare_replay_request(request: RequestPayload) -> RequestPayload:
    """Snapshot a request for later replay: deep-copy it and strip its request_id.

    Deep-copy so later mutation of the live request cannot change the stored inverse. Strip
    request_id so the replay is treated as internal (never re-recorded).
    """
    clone = copy.deepcopy(request)
    clone.request_id = None
    return clone


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
            dispatch_expecting_success(request, f"undo via replaying {type(request).__name__}")

    def redo(self) -> None:
        for request in self.redo_requests:
            dispatch_expecting_success(request, f"redo via replaying {type(request).__name__}")


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

    Recorders live in the domain that owns the request they reverse (e.g. node recorders live
    beside NodeManager) and are registered via UndoManager.register_recorder. Use a recorder
    (rather than record_inverse) when reversing an operation requires state captured *before* the
    handler runs (e.g. serializing a node before it is deleted) or *after* it (e.g. the assigned
    name of a freshly created node).
    """

    @abstractmethod
    def capture_before(self, request: RequestPayload) -> RecorderCapture:
        """Capture any 'before' state required to reverse the request. Runs before the handler."""

    @abstractmethod
    def create_batch(self, request: RequestPayload, result: ResultPayload, state: Any) -> UndoBatch | None:
        """Build the undo batch after the handler succeeded. Returning None invalidates history."""


@dataclass
class DispatchCapture:
    """Per-dispatch recording state handed between begin_request_dispatch and end_request_dispatch.

    Attributes:
        request: The request being dispatched (used as the default redo target for record_inverse).
        opened_frame: True when this dispatch opened the active recording frame and must finalize it.
        recorder: The recorder covering this request type, or None when the type has none.
        before_state: Recorder-specific state captured before the handler ran.
        declined: True when the recorder could not faithfully capture this request.
        recorded_inverse: Set by record_inverse when the handler declared its own inverse.
    """

    request: RequestPayload
    opened_frame: bool
    recorder: UndoRecorder | None
    before_state: Any
    declined: bool
    recorded_inverse: bool = False


@dataclass
class _RecordingFrame:
    """A single user action being recorded, accumulating entries until it is finalized.

    Attributes:
        label: Batch label; the first recorder/record_inverse to supply one wins.
        entries: Reversible entries collected during the frame, in application order.
        invalidate: When True, the frame commits nothing and clears history (an unrecordable
            or partially-failed mutation occurred, so recorded inverses can no longer be trusted).
        is_transaction: When True, nested dispatches contribute to this frame (grouping). When
            False, the frame owner captures its own nested effects and nested dispatches are ignored.
    """

    label: str | None = None
    entries: list[UndoEntry] = field(default_factory=list)
    invalidate: bool = False
    is_transaction: bool = False


class UndoManager:
    """Owns the undo/redo mechanism and policy; domains own the knowledge of how to reverse requests.

    Mechanism (here): the undo/redo stacks, the replay guard, grouping a dispatch (and its
    cascade) into one batch, the user-initiated gate (request_id), transaction grouping, the
    global history-clearing lifecycle events, and the Undo/Redo/GetState/Clear request handlers.

    Knowledge (elsewhere): how to reverse a specific request. Domains register a recorder via
    register_recorder (for reversals needing pre/post-dispatch state) or call record_inverse from
    their handler's success path (for reversals computed inline). Domains that intentionally do not
    support undo for a mutating request declare it via register_non_undoable.

    Safety invariant: a user-initiated mutation either (a) records an inverse, (b) is declared
    non-undoable, or (c) invalidates history (the safe default). This lets coverage grow one domain
    at a time without ever replaying against state the manager does not understand.
    """

    # Request types that invalidate all undo history whenever they are dispatched, regardless of
    # origin, because they replace or restructure the whole object graph. This is global lifecycle
    # policy (not per-feature domain knowledge), so it stays centralized here.
    _CLEAR_HISTORY_REQUEST_TYPES: tuple[type[RequestPayload], ...] = (
        ClearAllObjectStateRequest,
        SetWorkflowContextRequest,
        RunWorkflowFromScratchRequest,
        RunWorkflowFromRegistryRequest,
        RunWorkflowWithCurrentStateRequest,
        ImportWorkflowRequest,
    )

    # The manager's own request types. They alter workflow state by design (undo/redo) or not at
    # all (state/clear) and must never be recorded or clear history.
    _OWN_EVENT_TYPES: tuple[type[RequestPayload], ...] = (
        UndoRequest,
        RedoRequest,
        GetUndoStateRequest,
        ClearUndoStateRequest,
    )

    def __init__(self, event_manager: EventManager) -> None:
        self._undo_stack: deque[UndoBatch] = deque(maxlen=MAX_UNDO_BATCHES)
        self._redo_stack: list[UndoBatch] = []
        self._is_replaying = False
        self._recorders: dict[type[RequestPayload], UndoRecorder] = {}
        self._non_undoable_types: set[type[RequestPayload]] = set()
        self._active_frame: _RecordingFrame | None = None
        self._dispatch_stack: list[DispatchCapture] = []

        event_manager.assign_manager_to_request_type(UndoRequest, self.on_undo_request)
        event_manager.assign_manager_to_request_type(RedoRequest, self.on_redo_request)
        event_manager.assign_manager_to_request_type(GetUndoStateRequest, self.on_get_undo_state_request)
        event_manager.assign_manager_to_request_type(ClearUndoStateRequest, self.on_clear_undo_state_request)

    def register_recorder(self, request_type: type[RequestPayload], recorder: UndoRecorder) -> None:
        """Register the recorder a domain uses to reverse one of its request types.

        Called from the owning manager's __init__, mirroring assign_manager_to_request_type.
        """
        existing = self._recorders.get(request_type)
        if existing is not None:
            msg = (
                f"Attempted to register an undo recorder for '{request_type.__name__}', but one is "
                f"already registered ({type(existing).__name__})."
            )
            raise ValueError(msg)
        self._recorders[request_type] = recorder

    def register_non_undoable(self, *request_types: type[RequestPayload]) -> None:
        """Declare request types that mutate workflow state but are intentionally not undoable.

        Such a mutation neither records an entry nor invalidates history. Called from the owning
        manager's __init__. As recorders are added for these types, they are removed from here.
        """
        self._non_undoable_types.update(request_types)

    def record_inverse(
        self,
        inverse: RequestPayload | Sequence[RequestPayload],
        label: str,
        *,
        forward: RequestPayload | Sequence[RequestPayload] | None = None,
    ) -> None:
        """Declare how to reverse the operation being handled in the current dispatch.

        Domain handlers call this on their success path with the inverse request(s). Redo replays
        the forward request(s), defaulting to the request currently being handled. A no-op when
        nothing is being recorded (internal origin, replay in progress, or ineligible dispatch), so
        handlers can call it unconditionally.
        """
        if self._active_frame is None or not self._dispatch_stack:
            return
        capture = self._dispatch_stack[-1]
        forward_source = forward if forward is not None else capture.request
        undo_requests = [_prepare_replay_request(request) for request in _as_sequence(inverse)]
        redo_requests = (
            [_prepare_replay_request(request) for request in _as_sequence(forward_source)]
            if forward_source is not None
            else []
        )
        self._active_frame.entries.append(
            RequestReplayUndoEntry(undo_requests=undo_requests, redo_requests=redo_requests)
        )
        if self._active_frame.label is None:
            self._active_frame.label = label
        capture.recorded_inverse = True

    @contextmanager
    def transaction(self, label: str) -> Iterator[None]:
        """Group every recordable mutation issued within the block into a single undo batch.

        Use for engine-side operations that issue multiple requests (e.g. paste) so undo reverts
        them as one action. When a recording frame is already active, the block joins it and, for
        the duration, promotes it to collect nested contributions under this label. During replay
        the block is a no-op passthrough.
        """
        if self._is_replaying:
            yield
            return

        existing_frame = self._active_frame
        if existing_frame is not None:
            previous_is_transaction = existing_frame.is_transaction
            existing_frame.is_transaction = True
            if existing_frame.label is None:
                existing_frame.label = label
            try:
                yield
            except Exception:
                existing_frame.invalidate = True
                raise
            finally:
                existing_frame.is_transaction = previous_is_transaction
            return

        frame = _RecordingFrame(label=label, is_transaction=True)
        self._active_frame = frame
        try:
            yield
        except Exception:
            self._active_frame = None
            self.clear_history()
            raise
        self._active_frame = None
        self._finalize_frame(frame)

    def begin_request_dispatch(self, request: RequestPayload, request_id: str | None) -> DispatchCapture | None:
        """Observe a request before its handler runs; returns capture state for end_request_dispatch.

        Called by the EventManager on every dispatch. Returns None when the dispatch does not
        contribute to recording: the manager's own events, replay in progress, or a nested dispatch
        under a non-transaction frame. History-clearing lifecycle types clear immediately and return
        None. A recording frame is opened for a user-initiated top-level request (has request_id).
        """
        request_type = type(request)
        if request_type in self._OWN_EVENT_TYPES:
            return None
        if isinstance(request, self._CLEAR_HISTORY_REQUEST_TYPES):
            self.clear_history()
            return None
        if self._is_replaying:
            return None

        opened_frame = False
        if self._active_frame is None:
            if request_id is None:
                return None
            self._active_frame = _RecordingFrame()
            opened_frame = True
        elif not self._active_frame.is_transaction:
            # Nested dispatch under a single-request frame: the frame owner already captures its
            # own cascade (e.g. a delete's connection removals), so nested dispatches are ignored.
            return None
        # Otherwise a transaction frame is active and this dispatch contributes to it.

        recorder = self._recorders.get(request_type)
        before_state = None
        declined = False
        if recorder is not None:
            try:
                capture = recorder.capture_before(request)
                before_state = capture.state
                declined = capture.declined
            except Exception:
                logger.exception(
                    "Undo recorder for request type '%s' raised during capture; treating as unrecordable.",
                    request_type.__name__,
                )
                declined = True

        dispatch_capture = DispatchCapture(
            request=request,
            opened_frame=opened_frame,
            recorder=recorder,
            before_state=before_state,
            declined=declined,
        )
        self._dispatch_stack.append(dispatch_capture)
        return dispatch_capture

    def end_request_dispatch(
        self, capture: DispatchCapture | None, request: RequestPayload, result: ResultPayload | None
    ) -> None:
        """Finish observing a dispatch: contribute to the active frame and finalize if it was opened.

        A None result means the handler raised. Only the dispatch that opened the frame finalizes it
        (commit a batch or clear history).
        """
        if capture is None:
            return
        if self._dispatch_stack and self._dispatch_stack[-1] is capture:
            self._dispatch_stack.pop()
        frame = self._active_frame
        try:
            self._contribute_to_frame(frame, capture, request, result)
        finally:
            if capture.opened_frame:
                self._active_frame = None
                self._finalize_frame(frame)

    def clear_history(self) -> None:
        """Drop all undo and redo history."""
        if self._undo_stack or self._redo_stack:
            logger.debug("Cleared undo history (%d undo, %d redo).", len(self._undo_stack), len(self._redo_stack))
        self._undo_stack.clear()
        self._redo_stack.clear()

    def on_undo_request(self, request: UndoRequest) -> ResultPayload:  # noqa: ARG002
        if self._is_replaying:
            details = "Attempted to undo. Failed because an undo or redo is already in progress."
            return UndoResultFailure(result_details=details)
        if GriptapeNodes.FlowManager().check_for_existing_running_flow():
            details = "Attempted to undo. Failed because a flow is currently running."
            return UndoResultFailure(result_details=details)
        if not self._undo_stack:
            details = "Attempted to undo. Failed because there is nothing to undo."
            return UndoResultFailure(result_details=details)

        batch = self._undo_stack.pop()
        self._is_replaying = True
        try:
            for entry in reversed(batch.entries):
                entry.undo()
        except UndoEntryReplayError as err:
            self.clear_history()
            details = f"Attempted to undo '{batch.label}'. Failed because {err}. Undo history has been cleared."
            return UndoResultFailure(result_details=details)
        finally:
            self._is_replaying = False

        self._redo_stack.append(batch)
        return UndoResultSuccess(undone_label=batch.label, result_details=f"Undid '{batch.label}'.")

    def on_redo_request(self, request: RedoRequest) -> ResultPayload:  # noqa: ARG002
        if self._is_replaying:
            details = "Attempted to redo. Failed because an undo or redo is already in progress."
            return RedoResultFailure(result_details=details)
        if GriptapeNodes.FlowManager().check_for_existing_running_flow():
            details = "Attempted to redo. Failed because a flow is currently running."
            return RedoResultFailure(result_details=details)
        if not self._redo_stack:
            details = "Attempted to redo. Failed because there is nothing to redo."
            return RedoResultFailure(result_details=details)

        batch = self._redo_stack.pop()
        self._is_replaying = True
        try:
            for entry in batch.entries:
                entry.redo()
        except UndoEntryReplayError as err:
            self.clear_history()
            details = f"Attempted to redo '{batch.label}'. Failed because {err}. Undo history has been cleared."
            return RedoResultFailure(result_details=details)
        finally:
            self._is_replaying = False

        self._undo_stack.append(batch)
        return RedoResultSuccess(redone_label=batch.label, result_details=f"Redid '{batch.label}'.")

    def on_get_undo_state_request(self, request: GetUndoStateRequest) -> ResultPayload:  # noqa: ARG002
        undo_labels = [batch.label for batch in self._undo_stack]
        redo_labels = [batch.label for batch in self._redo_stack]
        return GetUndoStateResultSuccess(
            undo_labels=undo_labels,
            redo_labels=redo_labels,
            result_details=f"Undo state retrieved ({len(undo_labels)} undo, {len(redo_labels)} redo).",
        )

    def on_clear_undo_state_request(self, request: ClearUndoStateRequest) -> ResultPayload:  # noqa: ARG002
        self.clear_history()
        return ClearUndoStateResultSuccess(result_details="Undo history cleared.")

    def _contribute_to_frame(
        self,
        frame: _RecordingFrame | None,
        capture: DispatchCapture,
        request: RequestPayload,
        result: ResultPayload | None,
    ) -> None:
        """Fold one dispatch's outcome into the active recording frame."""
        if frame is None:
            return
        if result is None:
            # Handler raised mid-dispatch. If this dispatch intended to record, the workflow state
            # is now partially mutated and any recorded inverse can no longer be trusted.
            if capture.recorder is not None or capture.recorded_inverse:
                frame.invalidate = True
            return
        if result.failed():
            return
        if capture.recorder is not None:
            self._contribute_recorder(frame, capture, request, result)
            return
        self._contribute_without_recorder(frame, capture, request, result)

    def _contribute_recorder(
        self,
        frame: _RecordingFrame,
        capture: DispatchCapture,
        request: RequestPayload,
        result: ResultPayload,
    ) -> None:
        """Fold a recorder-backed dispatch into the frame, invalidating history if it cannot record."""
        recorder = capture.recorder
        if recorder is None or capture.declined:
            frame.invalidate = True
            return
        try:
            batch = recorder.create_batch(request, result, capture.before_state)
        except Exception:
            logger.exception(
                "Undo recorder for request type '%s' raised while building the batch; invalidating history.",
                type(request).__name__,
            )
            frame.invalidate = True
            return
        if batch is None:
            frame.invalidate = True
            return
        frame.entries.extend(batch.entries)
        if frame.label is None:
            frame.label = batch.label

    def _contribute_without_recorder(
        self,
        frame: _RecordingFrame,
        capture: DispatchCapture,
        request: RequestPayload,
        result: ResultPayload,
    ) -> None:
        """Fold a dispatch with no recorder into the frame: track via record_inverse, ignore, or invalidate."""
        if capture.recorded_inverse:
            return
        if not result.altered_workflow_state:
            return
        if type(request) in self._non_undoable_types:
            return
        logger.debug(
            "Invalidating undo history: request type '%s' mutated the workflow but declared no inverse.",
            type(request).__name__,
        )
        frame.invalidate = True

    def _finalize_frame(self, frame: _RecordingFrame | None) -> None:
        """Commit a completed frame as a batch, or clear history if it was invalidated."""
        if frame is None:
            return
        if frame.invalidate:
            self.clear_history()
            return
        if frame.entries:
            self._undo_stack.append(UndoBatch(label=frame.label or "Edit", entries=list(frame.entries)))
            self._redo_stack.clear()
