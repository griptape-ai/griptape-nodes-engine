"""The undo/redo mechanism entry point.

`UndoManager` owns the undo/redo stacks and the replay guard, wires the Undo/Redo/GetState/Clear
request handlers, and exposes the recording API. Recording itself is delegated to a
`RecordingSession`; the per-domain knowledge of how to reverse a request lives in recorders that
domains register via `register_recorder` (or inverses they declare inline via `record_inverse`).

Safety invariant: a user-initiated mutation either (a) records an inverse, (b) is declared
non-undoable, or (c) invalidates history (the safe default). This lets coverage grow one domain at
a time without ever replaying against state the manager does not understand.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import TYPE_CHECKING

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
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.undo.core import UndoBatch, UndoEntryReplayError
from griptape_nodes.retained_mode.managers.undo.recording import RecordingSession

if TYPE_CHECKING:
    from collections.abc import Sequence
    from contextlib import AbstractContextManager

    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager
    from griptape_nodes.retained_mode.managers.undo.core import UndoRecorder
    from griptape_nodes.retained_mode.managers.undo.recording import DispatchCapture

logger = logging.getLogger("griptape_nodes")

# Maximum number of undoable user actions retained. Oldest entries are dropped first.
MAX_UNDO_BATCHES = 100


class UndoManager:
    """Owns the undo/redo stacks and replay; delegates recording to a RecordingSession."""

    def __init__(self, event_manager: EventManager) -> None:
        self._undo_stack: deque[UndoBatch] = deque(maxlen=MAX_UNDO_BATCHES)
        self._redo_stack: list[UndoBatch] = []
        self._is_replaying = False
        self._recording = RecordingSession(
            is_replaying=lambda: self._is_replaying,
            commit_batch=self._commit_batch,
            invalidate_history=self.clear_history,
        )

        event_manager.assign_manager_to_request_type(UndoRequest, self.on_undo_request)
        event_manager.assign_manager_to_request_type(RedoRequest, self.on_redo_request)
        event_manager.assign_manager_to_request_type(GetUndoStateRequest, self.on_get_undo_state_request)
        event_manager.assign_manager_to_request_type(ClearUndoStateRequest, self.on_clear_undo_state_request)

    def register_recorder(self, request_type: type[RequestPayload], recorder: UndoRecorder) -> None:
        """Register the recorder a domain uses to reverse one of its request types."""
        self._recording.register_recorder(request_type, recorder)

    def register_non_undoable(self, *request_types: type[RequestPayload]) -> None:
        """Declare request types that mutate workflow state but are intentionally not undoable."""
        self._recording.register_non_undoable(*request_types)

    def record_inverse(
        self,
        inverse: RequestPayload | Sequence[RequestPayload],
        label: str,
        *,
        forward: RequestPayload | Sequence[RequestPayload] | None = None,
    ) -> None:
        """Declare how to reverse the operation being handled in the current dispatch."""
        self._recording.record_inverse(inverse, label, forward=forward)

    def transaction(self, label: str) -> AbstractContextManager[None]:
        """Group every recordable mutation issued within the block into a single undo batch."""
        return self._recording.transaction(label)

    def begin_request_dispatch(self, request: RequestPayload, request_id: str | None) -> DispatchCapture | None:
        """Observe a request before its handler runs; returns capture state for end_request_dispatch."""
        return self._recording.begin_request_dispatch(request, request_id)

    def end_request_dispatch(
        self, capture: DispatchCapture | None, request: RequestPayload, result: ResultPayload | None
    ) -> None:
        """Finish observing a dispatch: contribute to the active frame and finalize if it was opened."""
        self._recording.end_request_dispatch(capture, request, result)

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
        except Exception as err:
            # An entry raised something other than UndoEntryReplayError (e.g. an un-deep-copyable
            # redo request). The workflow is now partially reverted, so history can no longer be
            # trusted; clear it and surface a typed failure rather than leaking the raw exception.
            logger.exception("Unexpected error while undoing '%s'; clearing undo history.", batch.label)
            self.clear_history()
            details = f"Attempted to undo '{batch.label}'. Failed with unexpected error: {err}. Undo history has been cleared."
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
        except Exception as err:
            # An entry raised something other than UndoEntryReplayError. The workflow is now
            # partially re-applied, so history can no longer be trusted; clear it and surface a
            # typed failure rather than leaking the raw exception.
            logger.exception("Unexpected error while redoing '%s'; clearing undo history.", batch.label)
            self.clear_history()
            details = f"Attempted to redo '{batch.label}'. Failed with unexpected error: {err}. Undo history has been cleared."
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

    def _commit_batch(self, batch: UndoBatch) -> None:
        """Push a completed batch onto the undo stack, invalidating the redo stack."""
        self._undo_stack.append(batch)
        self._redo_stack.clear()
