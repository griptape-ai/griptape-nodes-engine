"""The whole-flow snapshot strategy for undo/redo.

The undo system's recording strategy: instead of computing a per-request inverse, it snapshots the
whole top-level flow around each user action and restores by reconciling the flow's contents. This
is state-centric -- it reasons about *what changed* (which nodes/connections/values exist now vs.
then), not about which request produced the change -- so every way of producing an effect (create a
node directly, duplicate, paste, import) undoes identically.

It is deliberately simple over efficient; it is the coarse baseline. `RecordingStrategy` (in
`undo.core`) is the seam for layering in a finer-grained strategy later (e.g. one that captures
per-touched-entity deltas instead of the whole flow) without changing the manager or dispatch path.

Scope and known limitations:

- Single top-level flow (the common editor case). Multiple top-level flows or subflow-only state
  are not handled.
- Only what a flow snapshot models is undoable: nodes, connections, parameter values, node metadata,
  and lock state. State outside the flow (variables, libraries, MCP servers, the workflow registry)
  is invisible to a snapshot, so requests that mutate it are not undoable and are simply never
  snapshot points -- domains opt in explicitly via `register_undoable`.
- Restore reconciles the live flow against the snapshot: it deletes only removed nodes, creates only
  added ones, and updates only changed values/positions/locks/connections on survivors. Nodes that
  did not change are left untouched, so the canvas updates surgically (no teardown/rebuild blink) and
  selection/viewport/execution state on unchanged nodes is preserved. Cost is O(changed) to apply,
  though a snapshot is still captured on every candidate edit (O(workflow size) to capture).
- Nodes are matched by name, so undoing a *rename* is the one case that does teardown/rebuild: the
  new-named node is deleted and the old-named one recreated. Non-serializable transient state on that
  node is lost, exactly as it would be for a freshly created node. Renaming the top-level *flow* is
  not reverted: restore resolves that flow by its top-level status, so an undo recorded before the
  rename still replays, and only the new flow name stays in place.
- Nodes living in a subflow (including a node group's private subflow) are outside the snapshot, so
  moves between flows are not undoable and are left undeclared by the node domain.
- Survivor parameter *structure* changes (a dynamic parameter added or removed by the undone action)
  are not reconciled; values, positions, locks, connections, and whole-node add/delete are. Requests
  that only change parameter structure are therefore left undeclared by the node domain.
- The top-level flow's own metadata is captured and restored, but `SetFlowMetadataRequest` merges
  keys rather than replacing the dict, so a key added since capture stays (the same caveat applies to
  per-node metadata).
- A batch commits on the dispatch reporting that it altered workflow state, not on a snapshot diff:
  serialization mints fresh UUIDs per capture, so two captures of identical state do not compare
  equal. A request that reports an edit without changing anything -- setting a parameter to the value
  it already holds -- therefore still consumes an undo slot and replays as a no-op.
"""

from __future__ import annotations

import copy
import logging
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
from griptape_nodes.retained_mode.events.flow_events import (
    GetFlowMetadataRequest,
    GetFlowMetadataResultSuccess,
    GetTopLevelFlowRequest,
    GetTopLevelFlowResultSuccess,
    ListNodesInFlowRequest,
    ListNodesInFlowResultSuccess,
    SerializeFlowToCommandsRequest,
    SerializeFlowToCommandsResultSuccess,
    SetFlowMetadataRequest,
)
from griptape_nodes.retained_mode.events.node_events import (
    DeleteNodeRequest,
    DeserializeNodeFromCommandsRequest,
    SetLockNodeStateRequest,
    SetNodeMetadataRequest,
)
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest
from griptape_nodes.retained_mode.managers.undo.core import (
    DispatchTriage,
    UndoBatch,
    UndoEntry,
    UndoEntryReplayError,
    triage_dispatch,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload
    from griptape_nodes.retained_mode.events.flow_events import SerializedFlowCommands
    from griptape_nodes.retained_mode.events.node_events import SerializedNodeCommands

logger = logging.getLogger("griptape_nodes")


@dataclass
class FlowSnapshot:
    """A serialized point-in-time image of one top-level flow's contents.

    ``serialized_flow_commands`` is captured with ``include_create_flow_command=False`` so it
    deserializes into the existing flow rather than creating a new one, keeping the flow's identity
    (and name) stable across undo/redo.

    ``explicit_value_keys`` records, per node name, the set of parameter names that had an explicit
    value at capture time (``node.parameter_values`` keys). Serialization drops values that are
    non-serializable or ``None``, so "absent from the serialized commands" does not mean "was unset";
    the reconcile clear-path uses this set instead, so it only clears values genuinely added since
    capture and never wipes a live non-serializable value on an unrelated undo.

    ``flow_name`` is the top-level flow's name at capture time, kept for error messages only. Restore
    resolves the top-level flow afresh, because a rename between capture and restore changes the name
    but not which flow the snapshot describes.

    ``flow_metadata`` is the flow's own metadata (canvas-level properties), which the serialized
    commands omit: they are captured with no create-flow command, and that command is what carries it.
    """

    flow_name: str
    serialized_flow_commands: SerializedFlowCommands
    explicit_value_keys: dict[str, set[str]]
    flow_metadata: dict[str, Any]


def capture_workflow_snapshot(engine: Engine) -> FlowSnapshot | None:
    """Serialize the current top-level flow, or None when there is nothing to snapshot.

    Never raises: capture runs inside begin_request_dispatch, which the EventManager calls before the
    user's handler. A serialization failure must degrade to "this action is not undoable" (return
    None) rather than propagate and break the edit the undo system is only meant to observe.
    """
    try:
        top_level_result = engine.handle_request(GetTopLevelFlowRequest())
        if not isinstance(top_level_result, GetTopLevelFlowResultSuccess) or top_level_result.flow_name is None:
            return None
        flow_name = top_level_result.flow_name

        serialize_result = engine.handle_request(
            SerializeFlowToCommandsRequest(flow_name=flow_name, include_create_flow_command=False)
        )
        if not isinstance(serialize_result, SerializeFlowToCommandsResultSuccess):
            logger.warning("Snapshot undo: failed to serialize flow '%s'; not snapshotting.", flow_name)
            return None
        explicit_value_keys = _capture_explicit_value_keys(engine, flow_name)
        if explicit_value_keys is None:
            # Could not enumerate every node's explicit values: proceeding with a partial map would
            # let the reconcile clear-path wipe values on nodes missing from it. Treat as "not
            # undoable" instead.
            logger.warning("Snapshot undo: incomplete value-key capture for flow '%s'; not snapshotting.", flow_name)
            return None
        flow_metadata = _capture_flow_metadata(engine, flow_name)
        if flow_metadata is None:
            logger.warning("Snapshot undo: could not read metadata for flow '%s'; not snapshotting.", flow_name)
            return None
        return FlowSnapshot(
            flow_name=flow_name,
            serialized_flow_commands=serialize_result.serialized_flow_commands,
            explicit_value_keys=explicit_value_keys,
            flow_metadata=flow_metadata,
        )
    except Exception:
        logger.exception("Snapshot undo: capturing the workflow snapshot raised; this action will not be undoable.")
        return None


def _capture_explicit_value_keys(engine: Engine, flow_name: str) -> dict[str, set[str]] | None:
    """Record, per node, the parameter names that hold an explicit value right now.

    Returns None if the node set cannot be fully enumerated, so the caller can decline to snapshot
    rather than proceed with a partial map (a missing node would default to "no explicit values" and
    have all its live values wiped by the reconcile clear-path). Used so the clear-path can tell
    "unset at capture" from "set but not serialized" (non-serializable or None values).
    """
    list_result = engine.handle_request(ListNodesInFlowRequest(flow_name=flow_name))
    if not isinstance(list_result, ListNodesInFlowResultSuccess):
        return None
    keys: dict[str, set[str]] = {}
    for node_name in list_result.node_names:
        node = engine.object_manager.attempt_get_object_by_name_as_type(node_name, BaseNode)
        if node is None:
            return None
        keys[node_name] = set(node.parameter_values.keys())
    return keys


def _capture_flow_metadata(engine: Engine, flow_name: str) -> dict[str, Any] | None:
    """The flow's own metadata, or None when it cannot be read (the caller then declines to snapshot)."""
    result = engine.handle_request(GetFlowMetadataRequest(flow_name=flow_name))
    if not isinstance(result, GetFlowMetadataResultSuccess):
        return None
    return copy.deepcopy(result.metadata)


def restore_workflow_snapshot(engine: Engine, snapshot: FlowSnapshot) -> None:
    """Reconcile the live flow to match the snapshot, emitting only the minimal set of mutations.

    Nodes are matched by name (stable and unique). Removed nodes are deleted, added nodes are
    created, and survivors have only their changed values/position/lock/connections updated. Nodes
    that did not change are never touched, so the editor updates surgically instead of blinking
    through a full teardown/rebuild. Raises UndoEntryReplayError on any failure so the manager can
    clear history and surface a typed failure rather than leaving the workflow half-restored.
    """
    # Resolve by top-level status, not by the captured name: renaming the flow between capture and
    # restore changes its name but not which flow this snapshot describes, and looking up the stale
    # name would fail the replay and take the whole undo history down with it.
    top_level_result = engine.handle_request(GetTopLevelFlowRequest())
    if not isinstance(top_level_result, GetTopLevelFlowResultSuccess) or top_level_result.flow_name is None:
        msg = f"snapshot restore could not find the flow it captured (named '{snapshot.flow_name}' at the time)"
        raise UndoEntryReplayError(msg)
    flow_name = top_level_result.flow_name

    flow = engine.object_manager.attempt_get_object_by_name_as_type(flow_name, ControlFlow)
    if flow is None:
        msg = f"snapshot restore could not find flow '{flow_name}'"
        raise UndoEntryReplayError(msg)

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

    list_result = engine.handle_request(ListNodesInFlowRequest(flow_name=flow_name))
    if not isinstance(list_result, ListNodesInFlowResultSuccess):
        msg = f"snapshot restore could not list nodes in flow '{flow_name}'"
        raise UndoEntryReplayError(msg)

    current_names = set(list_result.node_names)
    target_names = set(name_to_node_commands)
    to_delete = current_names - target_names
    to_create = target_names - current_names

    # 1. Delete removed nodes (each cascades its own connections away).
    for node_name in to_delete:
        if not engine.object_manager.has_object_with_name(node_name):
            continue
        _require_success(
            engine,
            DeleteNodeRequest(node_name=node_name),
            f"snapshot restore failed deleting node '{node_name}'",
        )

    # 2. Create added nodes (create command + element modifications, including position metadata).
    with engine.context_manager.flow(flow):
        for node_name in to_create:
            _require_success(
                engine,
                DeserializeNodeFromCommandsRequest(serialized_node_commands=name_to_node_commands[node_name]),
                f"snapshot restore failed creating node '{node_name}'",
            )

    # 3. Reconcile connections now that every endpoint exists.
    _reconcile_connections(engine, commands, uuid_to_name, target_names)

    # 4. Reconcile per-node values / position / lock. Created nodes are forced (nothing to compare
    #    against); survivors are diffed so unchanged state emits no events.
    for node_name in target_names:
        _reconcile_node_state(
            engine,
            node_name=node_name,
            node_commands=name_to_node_commands[node_name],
            commands=commands,
            explicit_value_keys=snapshot.explicit_value_keys.get(node_name),
            force=node_name in to_create,
        )

    # 5. Reconcile the flow's own metadata.
    _reconcile_flow_metadata(engine, flow=flow, target_metadata=snapshot.flow_metadata)


def _reconcile_connections(
    engine: Engine,
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
        list_result = engine.handle_request(ListConnectionsForNodeRequest(node_name=node_name))
        if not isinstance(list_result, ListConnectionsForNodeResultSuccess):
            continue
        for outgoing in list_result.outgoing_connections:
            current_connections.add(
                (node_name, outgoing.source_parameter_name, outgoing.target_node_name, outgoing.target_parameter_name)
            )

    for source_name, source_param, target_name, target_param in current_connections - target_connections:
        _require_success(
            engine,
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
            engine,
            CreateConnectionRequest(
                source_node_name=source_name,
                source_parameter_name=source_param,
                target_node_name=target_name,
                target_parameter_name=target_param,
            ),
            f"snapshot restore failed creating connection '{source_name}.{source_param}' -> '{target_name}.{target_param}'",
        )


def _reconcile_node_state(  # noqa: PLR0913
    engine: Engine,
    *,
    node_name: str,
    node_commands: SerializedNodeCommands,
    commands: SerializedFlowCommands,
    explicit_value_keys: set[str] | None,
    force: bool,
) -> None:
    """Restore a node's position, parameter values, and lock, setting only what differs (unless forced)."""
    node = engine.object_manager.attempt_get_object_by_name_as_type(node_name, BaseNode)
    if node is None:
        msg = f"snapshot restore could not find node '{node_name}' to reconcile"
        raise UndoEntryReplayError(msg)

    # Position / metadata. Created nodes already got it from their create command. SetNodeMetadata
    # merges keys (it cannot remove one), so a metadata key present live but absent from the snapshot
    # is not cleared; in practice the keys that change (e.g. position) are always present in both.
    target_metadata = node_commands.create_node_command.metadata
    if not force and target_metadata is not None and node.metadata != target_metadata:
        logger.debug(
            "Snapshot undo: reverting metadata on '%s'; keys that differ: %s.",
            node_name,
            sorted(_differing_keys(node.metadata, target_metadata)),
        )
        _try_reconcile(
            engine,
            SetNodeMetadataRequest(node_name=node_name, metadata=copy.deepcopy(target_metadata)),
            f"setting metadata on node '{node_name}'",
        )

    # Parameter values. This may lazily unlock the node (a locked node rejects value sets); the lock
    # step below then restores the target lock state, so this must run before it.
    _reconcile_node_values(
        engine,
        node=node,
        node_commands=node_commands,
        commands=commands,
        explicit_value_keys=explicit_value_keys,
        force=force,
    )

    # Lock state (last: after any value restore that required temporarily unlocking the node).
    lock_command = commands.set_lock_commands_per_node.get(node_commands.node_uuid)
    target_lock = lock_command.lock if lock_command is not None else False
    if node.lock != target_lock:
        _try_reconcile(
            engine,
            SetLockNodeStateRequest(node_name=node_name, lock=target_lock),
            f"setting lock on node '{node_name}'",
        )


def _reconcile_node_values(  # noqa: PLR0913
    engine: Engine,
    *,
    node: BaseNode,
    node_commands: SerializedNodeCommands,
    commands: SerializedFlowCommands,
    explicit_value_keys: set[str] | None,
    force: bool,
) -> None:
    """Restore a node's parameter values to the snapshot, setting only what differs (unless forced)."""
    node_name = node.name
    for indirect_command in commands.set_parameter_value_commands.get(node_commands.node_uuid, []):
        set_command = indirect_command.set_parameter_value_command
        # Output values live in parameter_output_values and are execution state, not editor state;
        # replaying them as internal sets would clobber the real input value, so skip them.
        if set_command.is_output:
            continue
        parameter_name = set_command.parameter_name
        if indirect_command.unique_value_uuid not in commands.unique_parameter_uuid_to_values:
            continue
        target_value = commands.unique_parameter_uuid_to_values[indirect_command.unique_value_uuid]
        if not force and _values_equal(node.get_parameter_value(parameter_name), target_value):
            continue
        _ensure_node_unlocked(engine, node)
        # Fresh request (do not mutate the snapshot's command; snapshots are reused across undo/redo).
        # initial_setup bypasses the input+property connection guard and avoids unresolving the node,
        # preserving execution state on nodes the restore did not otherwise change.
        _try_reconcile(
            engine,
            SetParameterValueRequest(
                node_name=node_name,
                parameter_name=parameter_name,
                value=copy.deepcopy(target_value),
                initial_setup=True,
            ),
            f"setting value '{node_name}.{parameter_name}'",
        )

    # A None key set (node missing from the capture map) means the snapshot cannot vouch for this
    # node, so nothing is cleared. Created nodes (force) start clean and need no clearing.
    if not force and explicit_value_keys is not None:
        _clear_added_node_values(engine, node, explicit_value_keys)


def _clear_added_node_values(engine: Engine, node: BaseNode, explicit_value_keys: set[str]) -> None:
    """Reset parameters that hold an explicit value now but did not at capture time to their default.

    Uses the captured explicit-value key set (not the serialized commands) so a live value the
    snapshot could not record (non-serializable, or None) is left intact rather than wiped.
    """
    node_name = node.name
    for parameter_name in list(node.parameter_values.keys()):
        if parameter_name in explicit_value_keys:
            continue
        parameter = node.get_parameter_by_name(parameter_name)
        if parameter is None:
            continue
        _ensure_node_unlocked(engine, node)
        _try_reconcile(
            engine,
            SetParameterValueRequest(
                node_name=node_name,
                parameter_name=parameter_name,
                value=copy.deepcopy(parameter.default_value),
                initial_setup=True,
            ),
            f"clearing value '{node_name}.{parameter_name}'",
        )


def _ensure_node_unlocked(engine: Engine, node: BaseNode) -> None:
    """Unlock a node so its values can be restored; idempotent (no-op if already unlocked).

    A locked node rejects value sets regardless of initial_setup. The caller restores the target
    lock state afterward, so unlocking here is only a transient step during reconcile.
    """
    if node.lock:
        _try_reconcile(
            engine,
            SetLockNodeStateRequest(node_name=node.name, lock=False),
            f"unlocking node '{node.name}' to restore its values",
        )


def _reconcile_flow_metadata(engine: Engine, *, flow: ControlFlow, target_metadata: dict[str, Any]) -> None:
    """Restore the flow's own metadata, setting it only when it differs from the snapshot.

    SetFlowMetadata merges keys (it cannot remove one), so a key added since capture stays, exactly as
    for per-node metadata.
    """
    if flow.metadata == target_metadata:
        return
    _try_reconcile(
        engine,
        SetFlowMetadataRequest(flow_name=flow.name, metadata=copy.deepcopy(target_metadata)),
        f"setting metadata on flow '{flow.name}'",
    )


def _differing_keys(current: dict[str, Any], target: dict[str, Any]) -> set[str]:
    """Keys whose values differ between two metadata dicts, including keys present in only one.

    Diagnostics only: names which part of a node's metadata an undo is about to change, so a
    seemingly inert undo step ("nothing moved") can be traced to the key that actually differed.
    """
    return {key for key in current.keys() | target.keys() if not _values_equal(current.get(key), target.get(key))}


def _values_equal(current: Any, target: Any) -> bool:
    """Best-effort value equality; treats an unorderable/ambiguous comparison as 'changed'."""
    try:
        return bool(current == target)
    except Exception:
        return False


def _require_success(engine: Engine, request: RequestPayload, failure_message: str) -> ResultPayload:
    result = engine.handle_request(request)
    if result.failed():
        msg = f"{failure_message}: {result.result_details}"
        raise UndoEntryReplayError(msg)
    return result


def _try_reconcile(engine: Engine, request: RequestPayload, description: str) -> None:
    """Apply a best-effort per-node reconcile step, logging (not raising) on failure.

    Per-parameter/metadata/lock restores are best-effort: a single un-settable parameter (a rejecting
    before_value_set hook, a default that is not a valid value, a type not accepted as input) must
    not abort the whole replay, which would clear the entire undo history. Structural steps (node
    create/delete, connections) stay fatal via _require_success because a wrong graph shape cannot be
    trusted.
    """
    result = engine.handle_request(request)
    if result.failed():
        logger.warning(
            "Snapshot undo: reconcile step (%s) failed; leaving as-is. %s", description, result.result_details
        )


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
