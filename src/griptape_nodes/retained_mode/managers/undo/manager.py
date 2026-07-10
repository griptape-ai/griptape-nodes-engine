"""The undo/redo mechanism entry point.

`UndoManager` owns the undo/redo stacks and the replay guard, wires the Undo/Redo/GetState/Clear
request handlers, and exposes the recording API. Recording itself is delegated to a
`RecordingStrategy` (currently the whole-flow `SnapshotRecordingSession`); domains declare which of
their requests are not undoable via `register_non_undoable`.

`RecordingStrategy` is the extension seam: a finer-grained strategy (e.g. per-touched-entity state
deltas) can be layered in later by implementing the same contract, without changing the manager or
the dispatch path.
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

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager
    from griptape_nodes.retained_mode.managers.undo.core import RecordingStrategy

logger = logging.getLogger("griptape_nodes")

# Maximum number of undoable user actions retained. Oldest entries are dropped first.
MAX_UNDO_BATCHES = 100


class UndoManager:
    """Owns the undo/redo stacks and replay; delegates recording to a RecordingStrategy."""

    def __init__(self, event_manager: EventManager) -> None:
        # Local import: the snapshot strategy pulls in flow/serialization events and the engine
        # singleton, which would create an import cycle if imported at module load.
        from griptape_nodes.retained_mode.managers.undo.snapshot import SnapshotRecordingSession

        self._undo_stack: deque[UndoBatch] = deque(maxlen=MAX_UNDO_BATCHES)
        self._redo_stack: list[UndoBatch] = []
        self._is_replaying = False
        self._recording: RecordingStrategy = SnapshotRecordingSession(
            is_replaying=lambda: self._is_replaying,
            commit_batch=self._commit_batch,
            invalidate_history=self.clear_history,
        )

        event_manager.assign_manager_to_request_type(UndoRequest, self.on_undo_request)
        event_manager.assign_manager_to_request_type(RedoRequest, self.on_redo_request)
        event_manager.assign_manager_to_request_type(GetUndoStateRequest, self.on_get_undo_state_request)
        event_manager.assign_manager_to_request_type(ClearUndoStateRequest, self.on_clear_undo_state_request)

    def register_non_undoable(self, *request_types: type[RequestPayload]) -> None:
        """Declare request types that mutate workflow state but are intentionally not undoable."""
        self._recording.register_non_undoable(*request_types)

    def transaction(self, label: str) -> AbstractContextManager[None]:
        """Group every recordable mutation issued within the block into a single undo batch."""
        return self._recording.transaction(label)

    def begin_request_dispatch(self, request: RequestPayload, request_id: str | None) -> object | None:
        """Observe a request before its handler runs; returns an opaque capture for end_request_dispatch.

        The capture is produced and consumed by the active recording session; callers treat it as
        opaque (the inverse and snapshot strategies use different capture shapes).
        """
        return self._recording.begin_request_dispatch(request, request_id)

    def end_request_dispatch(
        self, capture: object | None, request: RequestPayload, result: ResultPayload | None
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
        error_details = self._replay_batch(batch, undo=True)
        if error_details is not None:
            return UndoResultFailure(result_details=error_details)

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
        error_details = self._replay_batch(batch, undo=False)
        if error_details is not None:
            return RedoResultFailure(result_details=error_details)

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

    def _replay_batch(self, batch: UndoBatch, *, undo: bool) -> str | None:
        """Replay one batch under the replay guard; return a failure detail string, or None on success.

        Undo replays the batch's entries in reverse via entry.undo(); redo replays them in order via
        entry.redo(). Any replay failure leaves the workflow partially reverted or re-applied, so the
        whole history can no longer be trusted: it is cleared and a detail string is returned for the
        caller to wrap in its typed failure.
        """
        if undo:
            action_noun = "undo"
            entries = list(reversed(batch.entries))
        else:
            action_noun = "redo"
            entries = list(batch.entries)

        self._is_replaying = True
        try:
            for entry in entries:
                if undo:
                    entry.undo()
                else:
                    entry.redo()
        except UndoEntryReplayError as err:
            self.clear_history()
            return f"Attempted to {action_noun} '{batch.label}'. Failed because {err}. Undo history has been cleared."
        except Exception as err:
            # An entry raised something other than UndoEntryReplayError (e.g. an un-deep-copyable
            # request). The workflow is now partially changed, so history can no longer be trusted;
            # clear it and surface a typed failure rather than leaking the raw exception.
            logger.exception(
                "Unexpected error while replaying %s of '%s'; clearing undo history.", action_noun, batch.label
            )
            self.clear_history()
            return (
                f"Attempted to {action_noun} '{batch.label}'. Failed with unexpected error: {err}. "
                "Undo history has been cleared."
            )
        finally:
            self._is_replaying = False
        return None
