"""The recording state machine: turning a stream of dispatches into undo batches.

`RecordingSession` owns the per-action framing -- opening a frame for a user-initiated request,
folding that request's cascade into it, and finalizing it into an UndoBatch (or invalidating
history). It is deliberately decoupled from the undo/redo stacks: it reads whether a replay is in
progress and reports completed batches / invalidation through callbacks supplied by UndoManager.
"""

from __future__ import annotations

import copy
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

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
from griptape_nodes.retained_mode.managers.undo.core import (
    RequestReplayUndoEntry,
    UndoBatch,
    UndoEntry,
    UndoRecorder,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload

logger = logging.getLogger("griptape_nodes")


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


@dataclass
class DispatchCapture:
    """Per-dispatch recording state handed between begin_request_dispatch and end_request_dispatch.

    Attributes:
        request: The request being dispatched (used as the default redo target for record_inverse).
        opened_frame: True when this dispatch opened the active recording frame and must finalize it.
        records: True when this dispatch contributes to the active frame. False for a dispatch nested
            inside another recording dispatch (e.g. a delete's connection cascade), whose reversal is
            owned by that ancestor; such a dispatch is tracked only so record_inverse can tell it apart.
        recorder: The recorder covering this request type, or None when the type has none.
        before_state: Recorder-specific state captured before the handler ran.
        declined: True when the recorder could not faithfully capture this request.
        recorded_inverse: Set by record_inverse when the handler declared its own inverse.
    """

    request: RequestPayload
    opened_frame: bool
    records: bool
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
    """

    label: str | None = None
    entries: list[UndoEntry] = field(default_factory=list)
    invalidate: bool = False


class RecordingSession:
    """Groups each user-initiated dispatch (and its cascade) into an UndoBatch.

    Owns the recorder registry and the framing state (active frame, dispatch stack, nesting depth).
    A dispatch records when it is the outermost recordable dispatch in a frame -- opened by a
    user-initiated request (has request_id) or, inside a transaction, by an engine-issued request --
    but not when it is nested inside another recording dispatch (that ancestor owns the reversal).

    The session never touches the undo/redo stacks. It reports a completed batch via commit_batch,
    a lost-trust condition via invalidate_history, and reads replay state via is_replaying.
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

    def __init__(
        self,
        *,
        is_replaying: Callable[[], bool],
        commit_batch: Callable[[UndoBatch], None],
        invalidate_history: Callable[[], None],
    ) -> None:
        self._is_replaying = is_replaying
        self._commit_batch = commit_batch
        self._invalidate_history = invalidate_history
        self._recorders: dict[type[RequestPayload], UndoRecorder] = {}
        self._non_undoable_types: set[type[RequestPayload]] = set()
        self._active_frame: _RecordingFrame | None = None
        self._dispatch_stack: list[DispatchCapture] = []
        # Count of currently-open recording dispatches. A dispatch records only when this is 0 on
        # entry, so a nested cascade (depth > 0) is owned by its recording ancestor rather than
        # recorded separately.
        self._recording_depth = 0

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
        if not capture.records:
            # Nested inside another recording dispatch; that ancestor owns the reversal.
            return
        forward_source = forward if forward is not None else capture.request
        try:
            undo_requests = [_prepare_replay_request(request) for request in _as_sequence(inverse)]
            redo_requests = (
                [_prepare_replay_request(request) for request in _as_sequence(forward_source)]
                if forward_source is not None
                else []
            )
        except Exception:
            # Snapshotting must never break the operation being handled. If a value cannot be
            # deep-copied, this action simply will not be undoable; history is left untouched
            # (types that record inline are declared non-undoable, so they will not invalidate it).
            logger.exception("Failed to snapshot requests for undo of '%s'; this action will not be undoable.", label)
            return
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
        them as one action. Opens a recording frame that the block's requests contribute to; each
        top-level request within the block becomes one entry (its own cascade stays folded in).
        A no-op passthrough during replay, or when a recording frame is already active (the active
        frame already groups that dispatch's work).
        """
        if self._is_replaying() or self._active_frame is not None:
            yield
            return

        frame = _RecordingFrame(label=label)
        self._active_frame = frame
        try:
            yield
        except Exception:
            self._active_frame = None
            self._invalidate_history()
            raise
        self._active_frame = None
        self._finalize_frame(frame)

    def begin_request_dispatch(self, request: RequestPayload, request_id: str | None) -> DispatchCapture | None:
        """Observe a request before its handler runs; returns capture state for end_request_dispatch.

        Called by the EventManager on every dispatch. Returns None for dispatches the undo system
        ignores entirely (its own events, history-clearing lifecycle types, replay in progress).
        Otherwise a DispatchCapture is always returned so the dispatch is tracked; capture.records
        says whether it contributes to a recording frame. A dispatch records when it is the
        outermost recordable dispatch in a frame -- opened by a user-initiated request (has
        request_id) or, inside a transaction, by an engine-issued request -- but not when it is
        nested inside another recording dispatch (that ancestor owns the reversal).
        """
        request_type = type(request)
        if request_type in self._OWN_EVENT_TYPES:
            return None
        # Isolate replay first: an inverse dispatched during undo/redo must never clear history or
        # be recorded, even if it is (or cascades into) a history-clearing lifecycle type.
        if self._is_replaying():
            return None
        if isinstance(request, self._CLEAR_HISTORY_REQUEST_TYPES):
            self._invalidate_history()
            return None

        # Nothing to track: no recording frame is open and this dispatch cannot open one (only a
        # user-initiated request, which carries a request_id, opens a frame). Returning None keeps
        # internal dispatches (e.g. execution-time events) off the shared dispatch stack, so
        # concurrently-interleaved begin/end calls cannot orphan captures on it.
        if self._active_frame is None and request_id is None:
            return None

        opened_frame = False
        records = False
        if self._active_frame is None:
            if request_id is not None:
                self._active_frame = _RecordingFrame()
                opened_frame = True
                records = True
        else:
            # A frame is active (opened by an outer request or a transaction). Record only when this
            # is the outermost dispatch within it; a nested cascade is owned by its ancestor.
            records = self._recording_depth == 0

        recorder = None
        before_state = None
        declined = False
        if records:
            self._recording_depth += 1
            recorder = self._recorders.get(request_type)
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
            records=records,
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

        A None result means the handler raised. Only a recording dispatch contributes; only the
        dispatch that opened the frame finalizes it (commit a batch or clear history).
        """
        if capture is None:
            return
        if self._dispatch_stack and self._dispatch_stack[-1] is capture:
            self._dispatch_stack.pop()
        if not capture.records:
            return
        self._recording_depth -= 1
        frame = self._active_frame
        try:
            self._contribute_to_frame(frame, capture, request, result)
        finally:
            if capture.opened_frame:
                self._active_frame = None
                self._finalize_frame(frame)

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
        # An empty batch is a deliberate no-op (e.g. an unchanged value); it neither records nor
        # names the frame.
        if batch.entries and frame.label is None:
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
            self._invalidate_history()
            return
        if frame.entries:
            self._commit_batch(UndoBatch(label=frame.label or "Edit", entries=list(frame.entries)))
