"""Prototype: hybrid snapshot/inverse strategy for undo/redo.

Combines the two other strategies behind the same ``RecordingStrategy`` contract. Each user action
is routed, at frame-open time, to exactly one of two inner sessions:

- A declared *surgical* request type (one whose domain registered a recorder proven to reverse its
  whole cascade) is routed to an inverse ``RecordingSession``: O(change) capture, precise restore,
  execution state preserved on untouched nodes.
- Everything else that mutates (node moves, locks, dynamic-parameter structure changes, and any
  future request type nobody has written a recorder for) is routed to a ``SnapshotRecordingSession``,
  so it is still undoable via a whole-flow snapshot rather than clearing history the way the pure
  inverse strategy would.

The design constraint that forces the decision up front: a mutation can only be reversed if its
"before" image was captured *before* the handler ran (in ``begin_request_dispatch``). By then the
only thing known about the action is its outermost request type, so the surgical/snapshot election
is keyed on that type and cannot be deferred until an uncovered edit is observed.

Selected at startup via ``GRIPTAPE_NODES_UNDO_STRATEGY=hybrid``; the default remains the pure
inverse ``RecordingSession``.

Cost profile vs. the pure strategies:

- Covered edit: O(change) capture + surgical restore (like pure inverse), instead of the pure
  snapshot's O(workflow size) capture on every edit.
- Uncovered edit: O(workflow size) snapshot (like pure snapshot), instead of the pure inverse's
  "invalidate the entire undo history".

The one residual risk mirrors the pure inverse strategy: a surgical type is trusted to fold its
entire cascade into its recorded entries. If a surgical action cascades into a mutation its recorder
does not account for, that single action fails closed (the inverse session invalidates), because no
whole-flow snapshot was taken for it. Surgical types are therefore only the recorder-backed,
cascade-self-contained types, which the inverse suite already covers.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from griptape_nodes.retained_mode.managers.undo.core import DispatchTriage, triage_dispatch
from griptape_nodes.retained_mode.managers.undo.recording import RecordingSession
from griptape_nodes.retained_mode.managers.undo.snapshot import SnapshotRecordingSession

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload
    from griptape_nodes.retained_mode.managers.undo.core import RecordingStrategy, UndoBatch, UndoRecorder


@dataclass
class _HybridDispatch:
    """Capture returned by begin_request_dispatch: which inner session owns this dispatch, plus its capture.

    Attributes:
        session: The inner session this dispatch was routed to; end_request_dispatch routes back to it.
        inner: The opaque capture the inner session returned from its own begin_request_dispatch.
        opened: True when this dispatch opened the active frame and so must flush it on end.
    """

    session: RecordingStrategy
    inner: Any
    opened: bool


class HybridRecordingSession:
    """Implements RecordingStrategy by routing each action to an inverse or snapshot inner session.

    Interchangeable with RecordingSession and SnapshotRecordingSession behind the RecordingStrategy
    contract. It composes one of each inner session, intercepts their commit/invalidate callbacks so
    only the elected session's outcome reaches the real undo stacks, and holds a single active inner
    session for the currently-open frame so every nested cascade dispatch routes to the same place.

    A frame is homogeneous by construction: it is owned end to end by one inner session, so every
    committed batch is either all-surgical entries or a single whole-flow snapshot entry, and replay
    stays consistent with whichever session recorded it.
    """

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
        self._surgical_types: set[type[RequestPayload]] = set()
        # Inner sessions share the replay guard but route their outcomes through this session so it
        # decides what actually reaches the stacks.
        self._inverse = RecordingSession(
            is_replaying=is_replaying,
            commit_batch=self._on_inner_commit,
            invalidate_history=self._on_inner_invalidate,
        )
        self._snapshot = SnapshotRecordingSession(
            is_replaying=is_replaying,
            commit_batch=self._on_inner_commit,
            invalidate_history=self._on_inner_invalidate,
        )
        # The inner session that owns the frame currently open (None when no frame is open). Every
        # dispatch that arrives while this is set is a nested cascade dispatch routed to it.
        self._active_session: RecordingStrategy | None = None
        # Outcome of the active frame, captured from the intercepted callbacks and flushed to the
        # real stacks when the frame closes. At most one of these is set per frame.
        self._pending_batch: UndoBatch | None = None
        self._invalidated = False

    def register_recorder(self, request_type: type[RequestPayload], recorder: UndoRecorder) -> None:
        """Register a recorder on the inverse inner session (only surgical-routed types consult it)."""
        self._inverse.register_recorder(request_type, recorder)

    def register_non_undoable(self, *request_types: type[RequestPayload]) -> None:
        """Declare types that mutate but are never undoable (execution/runtime) on both inner sessions.

        Forwarded to both so neither opens a frame for them: the snapshot session skips them (no
        capture before running a flow), and the inverse session floors them.
        """
        self._inverse.register_non_undoable(*request_types)
        self._snapshot.register_non_undoable(*request_types)

    def register_inverse_floor(self, *request_types: type[RequestPayload]) -> None:
        """Declare editor mutations with no inverse recorder yet.

        These are deliberately NOT made surgical, so the hybrid routes them to the snapshot session
        and they remain undoable. Forwarded to both inner sessions for parity with their own contracts
        (a no-op on the snapshot side).
        """
        self._inverse.register_inverse_floor(*request_types)
        self._snapshot.register_inverse_floor(*request_types)

    def register_surgical(self, *request_types: type[RequestPayload]) -> None:
        """Route these request types to the inverse session instead of taking a whole-flow snapshot.

        A type declared surgical must also have a recorder registered (via register_recorder); the
        recorder supplies the reversal knowledge, and this declaration selects the surgical path over
        the snapshot path for actions opened by that type.
        """
        self._surgical_types.update(request_types)

    def record_inverse(
        self,
        inverse: RequestPayload | Sequence[RequestPayload],
        label: str,
        *,
        forward: RequestPayload | Sequence[RequestPayload] | None = None,
    ) -> None:
        """Forward an inline-declared inverse to the inverse session.

        A no-op unless the active frame is the inverse session's (the snapshot session ignores
        inverses, and the inverse session's record_inverse itself no-ops when it holds no open frame).
        """
        self._inverse.record_inverse(inverse, label, forward=forward)

    @contextmanager
    def transaction(self, label: str) -> Iterator[None]:
        """Group every mutation in the block into one whole-flow snapshot pair.

        Transactions always take the snapshot path: a transaction body can freely mix surgical and
        uncovered requests, so a snapshot is the only capture that safely reverses the whole block as
        one action. A no-op passthrough during replay or when a frame is already open.
        """
        if self._is_replaying() or self._active_session is not None:
            yield
            return
        self._active_session = self._snapshot
        try:
            with self._snapshot.transaction(label):
                yield
        finally:
            self._active_session = None
            self._flush_frame()

    def begin_request_dispatch(self, request: RequestPayload, request_id: str | None) -> _HybridDispatch | None:
        """Observe a dispatch: route a nested one to the active session, else elect a session for a new frame."""
        # A frame is already open: this is a nested cascade dispatch owned by the active session.
        if self._active_session is not None:
            inner = self._active_session.begin_request_dispatch(request, request_id)
            if inner is None:
                return None
            return _HybridDispatch(session=self._active_session, inner=inner, opened=False)

        # No frame open. Apply the shared lifecycle triage before electing a session.
        triage = triage_dispatch(request, is_replaying=self._is_replaying())
        if triage is DispatchTriage.IGNORE:
            return None
        if triage is DispatchTriage.CLEAR_HISTORY:
            self._invalidate_history()
            return None
        return self._open_frame(request, request_id)

    def _open_frame(self, request: RequestPayload, request_id: str | None) -> _HybridDispatch | None:
        """Elect a session for a new top-level frame and open it, or return None if none should open."""
        # Only a user-initiated request (carrying a request_id) opens a frame; an engine-issued
        # request outside a transaction contributes to nothing.
        if request_id is None:
            return None

        session: RecordingStrategy = self._inverse if type(request) in self._surgical_types else self._snapshot
        inner = session.begin_request_dispatch(request, request_id)
        if inner is None:
            # The elected session declined to open a frame (e.g. nothing to snapshot); this action is
            # simply not undoable.
            return None
        self._active_session = session
        return _HybridDispatch(session=session, inner=inner, opened=True)

    def end_request_dispatch(
        self, capture: _HybridDispatch | None, request: RequestPayload, result: ResultPayload | None
    ) -> None:
        """Route the dispatch end back to its owning session; flush the frame if this dispatch opened it."""
        if capture is None:
            return
        capture.session.end_request_dispatch(capture.inner, request, result)
        if capture.opened:
            self._active_session = None
            self._flush_frame()

    def _on_inner_commit(self, batch: UndoBatch) -> None:
        """Capture an inner session's committed batch; the real commit happens when the frame closes."""
        self._pending_batch = batch

    def _on_inner_invalidate(self) -> None:
        """Capture an inner session's invalidation.

        While a frame is open, defer it so the flush at frame close applies it as the frame's single
        outcome. Outside a frame (e.g. a lifecycle CLEAR_HISTORY the hybrid routed here), pass it
        straight through.
        """
        if self._active_session is not None:
            self._invalidated = True
            return
        self._invalidate_history()

    def _flush_frame(self) -> None:
        """Apply the closed frame's single outcome to the real stacks: invalidate, commit, or nothing."""
        invalidated = self._invalidated
        batch = self._pending_batch
        self._invalidated = False
        self._pending_batch = None
        if invalidated:
            self._invalidate_history()
            return
        if batch is not None:
            self._commit_batch(batch)
