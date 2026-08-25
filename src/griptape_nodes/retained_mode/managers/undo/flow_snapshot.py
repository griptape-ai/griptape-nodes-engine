"""A point-in-time image of one top-level flow, and the reconciling restore that puts it back.

Capture serializes the flow's contents; restore diffs the live flow against that image and applies
only the difference. This is state-centric -- it reasons about *what changed* (which
nodes/connections/values exist now vs. then), not about which request produced the change -- so
every way of producing an effect (create a node directly, duplicate, paste, import) reverts
identically.

Deliberately simple over efficient: it is the coarse baseline the undo system's snapshot recording
strategy is built on.

Scope and known limitations:

- Single top-level flow (the common editor case). Multiple top-level flows or subflow-only state
  are not handled.
- A snapshot models nodes, connections, parameter values, node metadata, and lock state. State
  outside the flow (variables, libraries, MCP servers, the workflow registry) is invisible to it, so
  callers must not expect changes to that state to be reverted.
- Restore reconciles the live flow against the snapshot: it deletes only removed nodes, creates only
  added ones, and updates only changed values/positions/locks/connections on survivors. Nodes that
  did not change are left untouched, so the canvas updates surgically (no teardown/rebuild blink) and
  selection/viewport/execution state on unchanged nodes is preserved. Cost is O(changed) to apply,
  though capture is O(workflow size).
- Nodes are matched by name, so reverting a *rename* is the one case that does teardown/rebuild: the
  new-named node is deleted and the old-named one recreated. Non-serializable transient state on that
  node is lost, exactly as it would be for a freshly created node. Renaming the top-level *flow* is
  not reverted: restore resolves that flow by its top-level status, so a snapshot captured before the
  rename still replays, and only the new flow name stays in place.
- Nodes living in a subflow (including a node group's private subflow) are outside the snapshot, so
  moves between flows are not reverted.
- Survivor parameter *structure* changes (a dynamic parameter added or removed) are not reconciled;
  values, positions, locks, connections, and whole-node add/delete are.
- The top-level flow's own metadata is captured and restored, but `SetFlowMetadataRequest` merges
  keys rather than replacing the dict, so a key added since capture stays (the same caveat applies to
  per-node metadata).
- Serialization mints fresh UUIDs per capture, so two captures of identical state do not compare
  equal. Callers must not use snapshot equality to detect whether anything changed.
"""

from __future__ import annotations

import copy
import logging
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
from griptape_nodes.retained_mode.managers.undo.core import UndoEntryReplayError

if TYPE_CHECKING:
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
