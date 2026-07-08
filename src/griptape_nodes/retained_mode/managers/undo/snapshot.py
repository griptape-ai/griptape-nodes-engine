"""Prototype: full-workflow snapshot strategy for undo/redo.

An experimental alternative to the inverse-command recorders. Instead of computing a per-request
inverse, this snapshots the whole top-level flow around each user action and restores by replacing
the flow's contents. Selected at startup via ``GRIPTAPE_NODES_UNDO_STRATEGY=snapshot``; the default
remains the inverse-command ``RecordingSession``.

It exists so the two approaches can be compared head to head in the editor behind the same
keybindings and stacks. It deliberately makes no attempt to be efficient.

Scope and known limitations (prototype):

- Single top-level flow (the common editor case). Multiple top-level flows or subflow-only state
  are not handled.
- Restore rebuilds the entire flow: it deletes every node (cascading connections) and re-creates
  everything from the snapshot. That is O(workflow size) per undo and emits a full teardown/rebuild
  event stream -- the canvas "blink" and the loss of selection/viewport/execution state that are
  exactly the tradeoffs this prototype exists to expose.
- A snapshot is taken on every candidate edit, so serialization cost is paid per edit.

Capture and restore timings are logged at INFO so the cost can be observed against inverse commands.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from griptape_nodes.exe_types.flow import ControlFlow
from griptape_nodes.retained_mode.events.context_events import SetWorkflowContextRequest
from griptape_nodes.retained_mode.events.flow_events import (
    DeserializeFlowFromCommandsRequest,
    GetTopLevelFlowRequest,
    GetTopLevelFlowResultSuccess,
    ListNodesInFlowRequest,
    ListNodesInFlowResultSuccess,
    SerializeFlowToCommandsRequest,
    SerializeFlowToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import DeleteNodeRequest
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
from griptape_nodes.retained_mode.managers.undo.core import UndoBatch, UndoEntry, UndoEntryReplayError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload
    from griptape_nodes.retained_mode.events.flow_events import SerializedFlowCommands
    from griptape_nodes.retained_mode.managers.undo.core import UndoRecorder

logger = logging.getLogger("griptape_nodes")


@dataclass
class FlowSnapshot:
    """A serialized point-in-time image of one top-level flow's contents.

    ``serialized_flow_commands`` is captured with ``include_create_flow_command=False`` so it
    deserializes into the existing flow rather than creating a new one, keeping the flow's identity
    (and name) stable across undo/redo.
    """

    flow_name: str
    serialized_flow_commands: SerializedFlowCommands


def capture_workflow_snapshot() -> FlowSnapshot | None:
    """Serialize the current top-level flow, or None when there is nothing to snapshot."""
    top_level_result = GriptapeNodes.handle_request(GetTopLevelFlowRequest())
    if not isinstance(top_level_result, GetTopLevelFlowResultSuccess) or top_level_result.flow_name is None:
        return None
    flow_name = top_level_result.flow_name

    started = time.perf_counter()
    serialize_result = GriptapeNodes.handle_request(
        SerializeFlowToCommandsRequest(flow_name=flow_name, include_create_flow_command=False)
    )
    if not isinstance(serialize_result, SerializeFlowToCommandsResultSuccess):
        logger.warning("Snapshot undo: failed to serialize flow '%s'; not snapshotting.", flow_name)
        return None
    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.info("Snapshot undo: captured flow '%s' in %.1f ms.", flow_name, elapsed_ms)
    return FlowSnapshot(flow_name=flow_name, serialized_flow_commands=serialize_result.serialized_flow_commands)


def restore_workflow_snapshot(snapshot: FlowSnapshot) -> None:
    """Replace the flow's contents with the snapshot: delete every node, then deserialize.

    Raises UndoEntryReplayError on any failure so the manager can clear history and surface a typed
    failure rather than leaving the workflow half-restored.
    """
    flow = GriptapeNodes.ObjectManager().attempt_get_object_by_name_as_type(snapshot.flow_name, ControlFlow)
    if flow is None:
        msg = f"snapshot restore could not find flow '{snapshot.flow_name}'"
        raise UndoEntryReplayError(msg)

    started = time.perf_counter()

    list_result = GriptapeNodes.handle_request(ListNodesInFlowRequest(flow_name=snapshot.flow_name))
    if not isinstance(list_result, ListNodesInFlowResultSuccess):
        msg = f"snapshot restore could not list nodes in flow '{snapshot.flow_name}'"
        raise UndoEntryReplayError(msg)

    for node_name in list_result.node_names:
        # A node may already be gone if it was a child cleaned up by an earlier delete's cascade.
        if not GriptapeNodes.ObjectManager().has_object_with_name(node_name):
            continue
        delete_result = GriptapeNodes.handle_request(DeleteNodeRequest(node_name=node_name))
        if delete_result.failed():
            msg = f"snapshot restore failed deleting node '{node_name}': {delete_result.result_details}"
            raise UndoEntryReplayError(msg)

    with GriptapeNodes.ContextManager().flow(flow):
        deserialize_result = GriptapeNodes.handle_request(
            DeserializeFlowFromCommandsRequest(
                serialized_flow_commands=snapshot.serialized_flow_commands,
                pop_flow_context_after=False,
            )
        )
    if deserialize_result.failed():
        msg = f"snapshot restore failed rebuilding flow '{snapshot.flow_name}': {deserialize_result.result_details}"
        raise UndoEntryReplayError(msg)

    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "Snapshot undo: restored flow '%s' (%d nodes) in %.1f ms.",
        snapshot.flow_name,
        len(list_result.node_names),
        elapsed_ms,
    )


@dataclass
class FlowSnapshotEntry(UndoEntry):
    """Reverses a user action by restoring the whole-flow snapshot taken before it (undo) or after it (redo)."""

    before: FlowSnapshot
    after: FlowSnapshot

    def undo(self) -> None:
        restore_workflow_snapshot(self.before)

    def redo(self) -> None:
        restore_workflow_snapshot(self.after)


@dataclass
class _SnapshotDispatch:
    """Marker returned by begin_request_dispatch so end_request_dispatch knows whether it opened the frame."""

    opened: bool


class SnapshotRecordingSession:
    """Drop-in alternative to RecordingSession that records whole-flow snapshots instead of inverses.

    Implements the same surface UndoManager and the EventManager dispatch path use. Recorders and
    record_inverse are ignored (snapshots need no per-request knowledge); register_non_undoable is
    honored so execution requests are not snapshotted.
    """

    _CLEAR_HISTORY_REQUEST_TYPES: tuple[type[RequestPayload], ...] = (
        ClearAllObjectStateRequest,
        SetWorkflowContextRequest,
        RunWorkflowFromScratchRequest,
        RunWorkflowFromRegistryRequest,
        RunWorkflowWithCurrentStateRequest,
        ImportWorkflowRequest,
    )
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
        self._non_undoable_types: set[type[RequestPayload]] = set()
        self._before: FlowSnapshot | None = None
        self._label: str | None = None
        self._depth = 0

    def register_recorder(self, request_type: type[RequestPayload], recorder: UndoRecorder) -> None:
        """No-op: the snapshot strategy needs no per-request reversal knowledge."""

    def register_non_undoable(self, *request_types: type[RequestPayload]) -> None:
        """Honor non-undoable declarations so execution requests are not snapshotted."""
        self._non_undoable_types.update(request_types)

    def record_inverse(
        self,
        inverse: RequestPayload | Sequence[RequestPayload],
        label: str,
        *,
        forward: RequestPayload | Sequence[RequestPayload] | None = None,
    ) -> None:
        """No-op: inverses are irrelevant to the snapshot strategy."""

    @contextmanager
    def transaction(self, label: str) -> Iterator[None]:
        """Group every mutation in the block into one snapshot pair."""
        if self._is_replaying() or self._depth > 0:
            yield
            return
        self._before = capture_workflow_snapshot()
        self._label = label
        self._depth += 1
        try:
            yield
        except Exception:
            self._reset()
            self._invalidate_history()
            raise
        self._depth -= 1
        self._finalize(committed=True)

    def begin_request_dispatch(self, request: RequestPayload, request_id: str | None) -> _SnapshotDispatch | None:
        request_type = type(request)
        if request_type in self._OWN_EVENT_TYPES:
            return None
        if self._is_replaying():
            return None
        if isinstance(request, self._CLEAR_HISTORY_REQUEST_TYPES):
            self._invalidate_history()
            return None

        # A frame is already open (an outer action or transaction): this dispatch is folded in.
        if self._depth > 0:
            self._depth += 1
            return _SnapshotDispatch(opened=False)

        # Only a user-initiated request (has request_id) that is not declared non-undoable opens a
        # frame. Skipping non-undoable types here avoids serializing before, say, running a flow.
        if request_id is None or request_type in self._non_undoable_types:
            return None

        self._before = capture_workflow_snapshot()
        self._label = "Edit"
        self._depth += 1
        return _SnapshotDispatch(opened=True)

    def end_request_dispatch(
        self,
        capture: _SnapshotDispatch | None,
        request: RequestPayload,  # noqa: ARG002
        result: ResultPayload | None,
    ) -> None:
        if capture is None:
            return
        self._depth -= 1
        if not capture.opened:
            return
        committed = result is not None and result.succeeded() and result.altered_workflow_state
        self._finalize(committed=committed)

    def clear_history(self) -> None:
        """Reset in-flight snapshot state (the manager owns the stacks)."""
        self._reset()

    def _finalize(self, *, committed: bool) -> None:
        before = self._before
        label = self._label or "Edit"
        self._reset()
        if not committed or before is None:
            return
        after = capture_workflow_snapshot()
        if after is None:
            return
        self._commit_batch(UndoBatch(label=label, entries=[FlowSnapshotEntry(before=before, after=after)]))

    def _reset(self) -> None:
        self._before = None
        self._label = None
        self._depth = 0
