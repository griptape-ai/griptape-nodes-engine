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
- Restore reconciles the live flow against the snapshot: it deletes only removed nodes, creates only
  added ones, and updates only changed values/positions/locks/connections on survivors. Nodes that
  did not change are left untouched, so the canvas updates surgically (no teardown/rebuild blink) and
  selection/viewport/execution state on unchanged nodes is preserved. Cost is O(changed) to apply,
  though a snapshot is still captured on every candidate edit (O(workflow size) to capture).
- Survivor parameter *structure* changes (a dynamic parameter added or removed by the undone action)
  are not reconciled; values, positions, locks, connections, and whole-node add/delete are.

Capture and restore timings are logged at INFO so the cost can be observed against inverse commands.
"""

from __future__ import annotations

import copy
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from griptape_nodes.exe_types.flow import ControlFlow
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    DeleteConnectionRequest,
    ListConnectionsForNodeRequest,
    ListConnectionsForNodeResultSuccess,
)
from griptape_nodes.retained_mode.events.context_events import SetWorkflowContextRequest
from griptape_nodes.retained_mode.events.flow_events import (
    GetTopLevelFlowRequest,
    GetTopLevelFlowResultSuccess,
    ListNodesInFlowRequest,
    ListNodesInFlowResultSuccess,
    SerializeFlowToCommandsRequest,
    SerializeFlowToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import (
    DeleteNodeRequest,
    DeserializeNodeFromCommandsRequest,
    SetLockNodeStateRequest,
    SetNodeMetadataRequest,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest
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
    from griptape_nodes.retained_mode.events.node_events import SerializedNodeCommands
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
    """Reconcile the live flow to match the snapshot, emitting only the minimal set of mutations.

    Nodes are matched by name (stable and unique). Removed nodes are deleted, added nodes are
    created, and survivors have only their changed values/position/lock/connections updated. Nodes
    that did not change are never touched, so the editor updates surgically instead of blinking
    through a full teardown/rebuild. Raises UndoEntryReplayError on any failure so the manager can
    clear history and surface a typed failure rather than leaving the workflow half-restored.
    """
    flow = GriptapeNodes.ObjectManager().attempt_get_object_by_name_as_type(snapshot.flow_name, ControlFlow)
    if flow is None:
        msg = f"snapshot restore could not find flow '{snapshot.flow_name}'"
        raise UndoEntryReplayError(msg)

    started = time.perf_counter()
    commands = snapshot.serialized_flow_commands

    # Match nodes by name (the create command carries the node's stable name).
    uuid_to_name: dict[str, str] = {}
    name_to_node_commands: dict[str, SerializedNodeCommands] = {}
    for node_commands in commands.serialized_node_commands:
        node_name = node_commands.create_node_command.node_name
        if node_name is None:
            continue
        uuid_to_name[node_commands.node_uuid] = node_name
        name_to_node_commands[node_name] = node_commands

    list_result = GriptapeNodes.handle_request(ListNodesInFlowRequest(flow_name=snapshot.flow_name))
    if not isinstance(list_result, ListNodesInFlowResultSuccess):
        msg = f"snapshot restore could not list nodes in flow '{snapshot.flow_name}'"
        raise UndoEntryReplayError(msg)

    current_names = set(list_result.node_names)
    target_names = set(name_to_node_commands)
    to_delete = current_names - target_names
    to_create = target_names - current_names

    # 1. Delete removed nodes (each cascades its own connections away).
    for node_name in to_delete:
        if not GriptapeNodes.ObjectManager().has_object_with_name(node_name):
            continue
        _require_success(
            DeleteNodeRequest(node_name=node_name),
            f"snapshot restore failed deleting node '{node_name}'",
        )

    # 2. Create added nodes (create command + element modifications, including position metadata).
    with GriptapeNodes.ContextManager().flow(flow):
        for node_name in to_create:
            _require_success(
                DeserializeNodeFromCommandsRequest(serialized_node_commands=name_to_node_commands[node_name]),
                f"snapshot restore failed creating node '{node_name}'",
            )

    # 3. Reconcile connections now that every endpoint exists.
    _reconcile_connections(commands, uuid_to_name, target_names)

    # 4. Reconcile per-node values / position / lock. Created nodes are forced (nothing to compare
    #    against); survivors are diffed so unchanged state emits no events.
    for node_name in target_names:
        _reconcile_node_state(
            node_name=node_name,
            node_commands=name_to_node_commands[node_name],
            commands=commands,
            force=node_name in to_create,
        )

    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "Snapshot undo: reconciled flow '%s' in %.1f ms (+%d nodes, -%d nodes, %d survivors).",
        snapshot.flow_name,
        elapsed_ms,
        len(to_create),
        len(to_delete),
        len(target_names & current_names),
    )


def _reconcile_connections(
    commands: SerializedFlowCommands,
    uuid_to_name: dict[str, str],
    target_names: set[str],
) -> None:
    """Delete connections not in the snapshot and create those missing, touching only what differs."""
    target_connections: set[tuple[str, str, str, str]] = set()
    for connection in commands.serialized_connections:
        source_name = uuid_to_name.get(connection.source_node_uuid)
        target_name = uuid_to_name.get(connection.target_node_uuid)
        if source_name is None or target_name is None:
            continue
        target_connections.add(
            (source_name, connection.source_parameter_name, target_name, connection.target_parameter_name)
        )

    # Enumerate current connections once, via each node's outgoing edges (source side is unique).
    current_connections: set[tuple[str, str, str, str]] = set()
    for node_name in target_names:
        list_result = GriptapeNodes.handle_request(ListConnectionsForNodeRequest(node_name=node_name))
        if not isinstance(list_result, ListConnectionsForNodeResultSuccess):
            continue
        for outgoing in list_result.outgoing_connections:
            current_connections.add(
                (node_name, outgoing.source_parameter_name, outgoing.target_node_name, outgoing.target_parameter_name)
            )

    for source_name, source_param, target_name, target_param in current_connections - target_connections:
        _require_success(
            DeleteConnectionRequest(
                source_node_name=source_name,
                source_parameter_name=source_param,
                target_node_name=target_name,
                target_parameter_name=target_param,
            ),
            f"snapshot restore failed removing connection '{source_name}.{source_param}' -> '{target_name}.{target_param}'",
        )
    for source_name, source_param, target_name, target_param in target_connections - current_connections:
        _require_success(
            CreateConnectionRequest(
                source_node_name=source_name,
                source_parameter_name=source_param,
                target_node_name=target_name,
                target_parameter_name=target_param,
            ),
            f"snapshot restore failed creating connection '{source_name}.{source_param}' -> '{target_name}.{target_param}'",
        )


def _reconcile_node_state(
    *,
    node_name: str,
    node_commands: SerializedNodeCommands,
    commands: SerializedFlowCommands,
    force: bool,
) -> None:
    """Restore a node's position, parameter values, and lock, setting only what differs (unless forced)."""
    node = GriptapeNodes.ObjectManager().attempt_get_object_by_name_as_type(node_name, BaseNode)
    if node is None:
        msg = f"snapshot restore could not find node '{node_name}' to reconcile"
        raise UndoEntryReplayError(msg)

    # Position / metadata. Created nodes already got it from their create command.
    target_metadata = node_commands.create_node_command.metadata
    if not force and target_metadata is not None and node.metadata != target_metadata:
        _require_success(
            SetNodeMetadataRequest(node_name=node_name, metadata=copy.deepcopy(target_metadata)),
            f"snapshot restore failed setting metadata on node '{node_name}'",
        )

    # Parameter values.
    for indirect_command in commands.set_parameter_value_commands.get(node_commands.node_uuid, []):
        parameter_name = indirect_command.set_parameter_value_command.parameter_name
        if indirect_command.unique_value_uuid not in commands.unique_parameter_uuid_to_values:
            continue
        target_value = commands.unique_parameter_uuid_to_values[indirect_command.unique_value_uuid]
        if not force and _values_equal(node.get_parameter_value(parameter_name), target_value):
            continue
        # Fresh request (do not mutate the snapshot's command; snapshots are reused across undo/redo).
        # initial_setup bypasses the input+property connection guard and avoids unresolving the node,
        # preserving execution state on nodes the restore did not otherwise change.
        _require_success(
            SetParameterValueRequest(
                node_name=node_name,
                parameter_name=parameter_name,
                value=copy.deepcopy(target_value),
                initial_setup=True,
            ),
            f"snapshot restore failed setting value '{node_name}.{parameter_name}'",
        )

    # Lock state.
    lock_command = commands.set_lock_commands_per_node.get(node_commands.node_uuid)
    target_lock = lock_command.lock if lock_command is not None else False
    if node.lock != target_lock:
        _require_success(
            SetLockNodeStateRequest(node_name=node_name, lock=target_lock),
            f"snapshot restore failed setting lock on node '{node_name}'",
        )


def _values_equal(current: Any, target: Any) -> bool:
    """Best-effort value equality; treats an unorderable/ambiguous comparison as 'changed'."""
    try:
        return bool(current == target)
    except Exception:
        return False


def _require_success(request: RequestPayload, failure_message: str) -> ResultPayload:
    result = GriptapeNodes.handle_request(request)
    if result.failed():
        msg = f"{failure_message}: {result.result_details}"
        raise UndoEntryReplayError(msg)
    return result


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
