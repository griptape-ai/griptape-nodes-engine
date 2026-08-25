"""The whole-flow snapshot recording strategy for undo/redo.

Frames each user action: capture the top-level flow before the action, capture it again after, and
commit the pair as one undo batch. Reversal is delegated to `flow_snapshot`, which reconciles the
live flow against the captured image, so this module owns only the framing question -- when does an
action start, when does it end, and did it change anything worth recording.

Because reversal is state-centric, every way of producing an effect (create a node directly,
duplicate, paste, import) undoes identically. `RecordingStrategy` (in `undo.core`) is the seam for
layering in a finer-grained strategy later (e.g. one that captures per-touched-entity deltas instead
of the whole flow) without changing the manager or the dispatch path.

Framing notes:

- Only what a flow snapshot models is undoable. Requests that mutate state outside the flow are
  never snapshot points -- domains opt in explicitly via `register_undoable`. See `flow_snapshot`
  for what the snapshot covers.
- A batch commits on the dispatch reporting that it altered workflow state, not on a snapshot diff:
  capture mints fresh UUIDs, so two captures of identical state do not compare equal. A request that
  reports an edit without changing anything -- setting a parameter to the value it already holds --
  therefore still consumes an undo slot and replays as a no-op.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from griptape_nodes.retained_mode.managers.undo.core import (
    DispatchTriage,
    UndoBatch,
    UndoEntry,
    triage_dispatch,
)
from griptape_nodes.retained_mode.managers.undo.flow_snapshot import (
    capture_workflow_snapshot,
    restore_workflow_snapshot,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload
    from griptape_nodes.retained_mode.managers.undo.flow_snapshot import FlowSnapshot

logger = logging.getLogger("griptape_nodes")


@dataclass
class FlowSnapshotEntry(UndoEntry):
    """Reverses a user action by restoring the whole-flow snapshot taken before it (undo) or after it (redo)."""

    engine: Engine
    before: FlowSnapshot
    after: FlowSnapshot

    def undo(self) -> None:
        restore_workflow_snapshot(self.engine, self.before)

    def redo(self) -> None:
        restore_workflow_snapshot(self.engine, self.after)


@dataclass
class _SnapshotDispatch:
    """Marker returned by begin_request_dispatch so end_request_dispatch knows whether it opened the frame."""

    opened: bool


class SnapshotRecordingSession:
    """Implements RecordingStrategy by recording whole-flow snapshots around each user action.

    Only the requests a domain declares via register_undoable are snapshot points, and the shared
    lifecycle policy (CLEAR_HISTORY_REQUEST_TYPES, OWN_EVENT_TYPES) applies via triage_dispatch.
    Declaring is opt-in because a flow snapshot models only flow contents: an undeclared request
    that mutates something outside the flow would otherwise commit a batch whose before and after
    are identical, so undoing it would consume a stack slot and revert nothing. It needs no
    per-request reversal knowledge beyond that: a snapshot captures whole-flow state, so it is
    agnostic to which declared request produced a change.

    Frame invariant: the open-frame state below (`_before`, `_label`, `_depth`, `_altered`,
    `_invalidated`) describes one frame at a time and assumes begin/end pairs nest as a single call
    stack -- an outer dispatch driving inner ones. Overlapping gestures are detected rather than
    folded together: an externally-initiated request carries a request_id, which a nested dispatch
    never does, so one arriving while a frame is open means two independent gestures are in flight
    at once (an async handler yielded and the next request ran as its own task). Both the frame and
    the history are dropped in that case, costing undoability rather than recording one gesture's
    edit as part of another's. `_close_frame` fails the same way if begin/end accounting ever goes
    out of sync.
    """

    def __init__(
        self,
        *,
        engine: Engine,
        is_replaying: Callable[[], bool],
        commit_batch: Callable[[UndoBatch], None],
        invalidate_history: Callable[[], None],
    ) -> None:
        self._engine = engine
        self._is_replaying = is_replaying
        self._commit_batch = commit_batch
        self._invalidate_history = invalidate_history
        self._undoable_labels: dict[type[RequestPayload], str] = {}
        self._before: FlowSnapshot | None = None
        self._label: str | None = None
        self._depth = 0
        # Set when a dispatch inside the open frame reported that it altered workflow state. The
        # frame commits on this union rather than on the framing request's own result, so a handler
        # that under-reports its own alteration while cascading into mutating requests still yields
        # an undoable batch instead of a silently unrecorded edit.
        self._altered = False
        # Set when a history-clearing lifecycle request is seen while a frame is open, so the frame
        # finalizes without committing a batch onto the just-cleared stacks.
        self._invalidated = False

    def register_undoable(self, labels: Mapping[type[RequestPayload], str]) -> None:
        """Declare request types a flow snapshot can capture, each with the name of its operation.

        The name is keyed by request type rather than by call path, so the same operation reads the
        same in the undo stack however it was triggered.
        """
        self._undoable_labels.update(labels)

    @contextmanager
    def transaction(self, label: str) -> Iterator[None]:
        """Group every mutation in the block into one snapshot pair.

        Commits only if some declared-undoable dispatch inside the block reported that it altered
        workflow state, mirroring the per-dispatch gate. A block that takes a no-op or early-exit path
        therefore pushes no undo entry, rather than one the user would watch do nothing.

        A raise inside the block still commits: the after-snapshot is taken from live state, so it
        records whatever the block managed to change, and undoing it backs that partial edit out.
        """
        if self._is_replaying() or self._depth > 0:
            yield
            return
        # Unlike begin_request_dispatch, the frame opens even when the capture failed: it suppresses
        # framing by nested dispatches, and _finalize declines to commit without a before-snapshot.
        self._before = capture_workflow_snapshot(self._engine)
        self._label = label
        self._depth += 1
        try:
            yield
        except Exception:
            self._close_and_finalize()
            raise
        self._close_and_finalize()

    def begin_request_dispatch(self, request: RequestPayload, request_id: str | None) -> _SnapshotDispatch | None:
        request_type = type(request)
        triage = triage_dispatch(request, is_replaying=self._is_replaying())
        if triage is DispatchTriage.IGNORE:
            return None
        if triage is DispatchTriage.CLEAR_HISTORY:
            self._invalidate_history()
            # If a frame is open, make it finalize without committing onto the just-cleared stacks.
            if self._depth > 0:
                self._invalidated = True
            return None

        if self._depth > 0:
            return self._join_open_frame(request_type, request_id)

        # Only a user-initiated request (has request_id) whose effects a snapshot can capture opens a
        # frame. Everything else is inert: it is never snapshotted, so it costs nothing and cannot
        # commit a batch that would revert nothing.
        if request_id is None or request_type not in self._undoable_labels:
            return None

        before = capture_workflow_snapshot(self._engine)
        if before is None:
            # Could not snapshot (nothing to capture, or serialization failed): do not open a frame,
            # so this action is simply not undoable rather than committing an unusable batch.
            return None
        self._before = before
        self._label = self._undoable_labels[request_type]
        self._depth += 1
        logger.debug("Snapshot undo: opened frame '%s' (%s).", self._label, request_type.__name__)
        return _SnapshotDispatch(opened=True)

    def end_request_dispatch(
        self,
        capture: _SnapshotDispatch | None,
        request: RequestPayload,
        result: ResultPayload | None,
    ) -> None:
        if capture is None:
            return
        if not self._close_frame():
            return

        altered = result is not None and result.succeeded() and result.altered_workflow_state
        # Only declared-undoable types contribute: a snapshot cannot represent anything else, so an
        # undeclared mutation inside the frame must not make it look like a capturable edit.
        if altered and type(request) in self._undoable_labels:
            self._altered = True

        if not capture.opened:
            return
        self._finalize(committed=self._altered)

    def _join_open_frame(self, request_type: type[RequestPayload], request_id: str | None) -> _SnapshotDispatch | None:
        """Fold an inner dispatch into the open frame, or fail closed on an overlapping gesture.

        An externally-initiated request carries a request_id, which an inner dispatch never does, so
        one arriving mid-frame is a separate gesture in flight at the same time (see the frame
        invariant on the class). Folding it in would record its edit as part of the open action, so
        the frame and the history are dropped instead.
        """
        if request_id is not None:
            logger.warning(
                "Snapshot undo: %s arrived while '%s' was still in progress; clearing undo history to "
                "avoid recording one edit as part of another.",
                request_type.__name__,
                self._label,
            )
            self._invalidate_history()
            self._invalidated = True
            return None
        self._depth += 1
        return _SnapshotDispatch(opened=False)

    def _close_and_finalize(self) -> None:
        """Close the frame this call opened and commit it if anything inside it altered the flow."""
        if not self._close_frame():
            return
        self._finalize(committed=self._altered)

    def _close_frame(self) -> bool:
        """Close one nesting level of the open frame; False when frame accounting was out of sync.

        A frame closing that was never open means begin/end pairs did not nest as a single call stack
        (see the frame invariant on the class). Any batch built from there would be attributed to the
        wrong action, so the frame and the history are dropped instead.
        """
        if self._depth <= 0:
            logger.warning(
                "Snapshot undo: dispatch frame tracking went out of sync; clearing undo history to "
                "avoid recording a misattributed edit."
            )
            self._reset()
            self._invalidate_history()
            return False
        self._depth -= 1
        return True

    def _finalize(self, *, committed: bool) -> None:
        before = self._before
        label = self._label or "Edit"
        invalidated = self._invalidated
        self._reset()
        if invalidated or not committed or before is None:
            logger.debug(
                "Snapshot undo: discarded frame '%s' (invalidated=%s, altered=%s, captured=%s).",
                label,
                invalidated,
                committed,
                before is not None,
            )
            return
        after = capture_workflow_snapshot(self._engine)
        if after is None:
            logger.debug("Snapshot undo: discarded frame '%s'; could not capture the after state.", label)
            return
        logger.debug("Snapshot undo: committed '%s'.", label)
        self._commit_batch(
            UndoBatch(label=label, entries=[FlowSnapshotEntry(engine=self._engine, before=before, after=after)])
        )

    def _reset(self) -> None:
        self._before = None
        self._label = None
        self._depth = 0
        self._altered = False
        self._invalidated = False
