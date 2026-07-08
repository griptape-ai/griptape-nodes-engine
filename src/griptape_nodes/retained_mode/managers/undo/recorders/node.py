"""Undo recorders and entries for node operations.

This module holds the node domain's knowledge of how to reverse its own request types. The
UndoManager owns the mechanism (stacks, batching, replay); NodeManager registers these recorders
with it from its __init__. The recorders live in the undo package alongside the mechanism they
plug into, but stay decoupled from it: nothing in the undo core imports this module -- NodeManager
is the sole wiring point, so adding undo support for a node request touches only this file and that
registration call.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from griptape_nodes.exe_types.node_groups.base_node_group import BaseNodeGroup
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    IncomingConnection,
    ListConnectionsForNodeRequest,
    ListConnectionsForNodeResultSuccess,
    OutgoingConnection,
)
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    CreateNodeResultSuccess,
    DeleteNodeRequest,
    DeleteNodeResultSuccess,
    DeserializeNodeFromCommandsRequest,
    DeserializeNodeFromCommandsResultSuccess,
    SerializedNodeCommands,
    SerializeNodeToCommandsRequest,
    SerializeNodeToCommandsResultSuccess,
    SetLockNodeStateRequest,
)
from griptape_nodes.retained_mode.events.parameter_events import (
    SetParameterValueRequest,
    SetParameterValueResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.undo import (
    RecorderCapture,
    RequestReplayUndoEntry,
    UndoBatch,
    UndoEntry,
    UndoEntryReplayError,
    UndoRecorder,
    dispatch_expecting,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload


@dataclass
class CreateNodeUndoEntry(UndoEntry):
    """Reverses a node creation by deleting the node; redo re-creates it under its original name."""

    node_name: str
    create_request: CreateNodeRequest

    def undo(self) -> None:
        dispatch_expecting(
            DeleteNodeRequest(node_name=self.node_name),
            DeleteNodeResultSuccess,
            f"undo creation of node '{self.node_name}' by deleting it",
        )

    def redo(self) -> None:
        # Deepcopy so the stored request stays pristine if the handler mutates its fields.
        result = dispatch_expecting(
            copy.deepcopy(self.create_request),
            CreateNodeResultSuccess,
            f"redo creation of node '{self.node_name}'",
        )
        if result.node_name != self.node_name:
            msg = (
                f"Attempted to redo creation of node '{self.node_name}'. Failed because the node was "
                f"re-created under a different name '{result.node_name}', indicating the original name was taken."
            )
            raise UndoEntryReplayError(msg)


@dataclass
class DeleteNodeUndoEntry(UndoEntry):
    """Reverses a node deletion by restoring the node from its serialized state.

    Restores the node's parameters, values, lock state, and the connections it had to
    other nodes at the moment it was deleted.
    """

    node_name: str
    serialized_node_commands: SerializedNodeCommands
    set_parameter_value_commands: list[SerializedNodeCommands.IndirectSetParameterValueCommand]
    unique_parameter_uuid_to_values: dict[SerializedNodeCommands.UniqueParameterValueUUID, Any]
    incoming_connections: list[IncomingConnection]
    outgoing_connections: list[OutgoingConnection]

    def undo(self) -> None:
        deserialize_result = dispatch_expecting(
            DeserializeNodeFromCommandsRequest(serialized_node_commands=self.serialized_node_commands),
            DeserializeNodeFromCommandsResultSuccess,
            f"undo deletion of node '{self.node_name}' by restoring it",
        )
        restored_node_name = deserialize_result.node_name
        if restored_node_name != self.node_name:
            # The original name is occupied by another node; the restored copy is under a different
            # name and every recorded reference to the original name is now unreliable.
            msg = (
                f"Attempted to undo deletion of node '{self.node_name}'. Failed because the node was "
                f"restored under a different name '{restored_node_name}', indicating the original name was taken."
            )
            raise UndoEntryReplayError(msg)

        self._restore_connections()
        self._restore_parameter_values()
        self._restore_lock_state()

    def redo(self) -> None:
        dispatch_expecting(
            DeleteNodeRequest(node_name=self.node_name),
            DeleteNodeResultSuccess,
            f"redo deletion of node '{self.node_name}'",
        )

    def _restore_connections(self) -> None:
        for incoming in self.incoming_connections:
            create_connection_request = CreateConnectionRequest(
                source_node_name=incoming.source_node_name,
                source_parameter_name=incoming.source_parameter_name,
                target_node_name=self.node_name,
                target_parameter_name=incoming.target_parameter_name,
            )
            result = GriptapeNodes.handle_request(create_connection_request)
            if result.failed():
                msg = (
                    f"Attempted to undo deletion of node '{self.node_name}'. Failed restoring incoming connection "
                    f"from '{incoming.source_node_name}.{incoming.source_parameter_name}' with result details: {result.result_details}"
                )
                raise UndoEntryReplayError(msg)
        for outgoing in self.outgoing_connections:
            create_connection_request = CreateConnectionRequest(
                source_node_name=self.node_name,
                source_parameter_name=outgoing.source_parameter_name,
                target_node_name=outgoing.target_node_name,
                target_parameter_name=outgoing.target_parameter_name,
            )
            result = GriptapeNodes.handle_request(create_connection_request)
            if result.failed():
                msg = (
                    f"Attempted to undo deletion of node '{self.node_name}'. Failed restoring outgoing connection "
                    f"to '{outgoing.target_node_name}.{outgoing.target_parameter_name}' with result details: {result.result_details}"
                )
                raise UndoEntryReplayError(msg)

    def _restore_parameter_values(self) -> None:
        for indirect_command in self.set_parameter_value_commands:
            set_value_command = indirect_command.set_parameter_value_command
            if indirect_command.unique_value_uuid not in self.unique_parameter_uuid_to_values:
                msg = (
                    f"Attempted to undo deletion of node '{self.node_name}'. Failed because the recorded value for "
                    f"parameter '{set_value_command.parameter_name}' was missing from the captured unique values."
                )
                raise UndoEntryReplayError(msg)
            set_value_command.value = self.unique_parameter_uuid_to_values[indirect_command.unique_value_uuid]
            set_value_command.node_name = self.node_name
            result = GriptapeNodes.handle_request(set_value_command)
            if result.failed():
                msg = (
                    f"Attempted to undo deletion of node '{self.node_name}'. Failed restoring the value of "
                    f"parameter '{set_value_command.parameter_name}' with result details: {result.result_details}"
                )
                raise UndoEntryReplayError(msg)

    def _restore_lock_state(self) -> None:
        lock_command = self.serialized_node_commands.lock_node_command
        if lock_command is None:
            return
        result = GriptapeNodes.handle_request(SetLockNodeStateRequest(node_name=self.node_name, lock=lock_command.lock))
        if result.failed():
            msg = (
                f"Attempted to undo deletion of node '{self.node_name}'. Failed restoring the node's lock state "
                f"with result details: {result.result_details}"
            )
            raise UndoEntryReplayError(msg)


class CreateNodeRecorder(UndoRecorder):
    """Records node creation so undo deletes the node and redo re-creates it under its original name."""

    def capture_before(self, request: RequestPayload) -> RecorderCapture:
        if not isinstance(request, CreateNodeRequest):
            return RecorderCapture(declined=True)
        # Group creation carries side effects (adopting existing nodes, subflow creation) that a
        # simple delete does not reverse. Decline so history is invalidated instead of recorded wrong.
        if request.node_names_to_add or request.subflow_name is not None:
            return RecorderCapture(declined=True)
        return RecorderCapture(state=copy.deepcopy(request))

    def create_batch(self, request: RequestPayload, result: ResultPayload, state: Any) -> UndoBatch | None:  # noqa: ARG002
        if not isinstance(result, CreateNodeResultSuccess):
            return None
        create_request: CreateNodeRequest = state
        # Pin the assigned name and flow so redo is deterministic regardless of context changes.
        create_request.node_name = result.node_name
        if create_request.override_parent_flow_name is None:
            create_request.override_parent_flow_name = result.parent_flow_name
        create_request.set_as_new_context = False
        create_request.request_id = None
        entry = CreateNodeUndoEntry(node_name=result.node_name, create_request=create_request)
        return UndoBatch(label=f"Create node '{result.node_name}'", entries=[entry])


class DeleteNodeRecorder(UndoRecorder):
    """Records node deletion so undo restores the node, its values, lock state, and connections."""

    def capture_before(self, request: RequestPayload) -> RecorderCapture:  # noqa: PLR0911
        if not isinstance(request, DeleteNodeRequest):
            return RecorderCapture(declined=True)

        node_name = request.node_name
        if node_name is None:
            context_manager = GriptapeNodes.ContextManager()
            if not context_manager.has_current_node():
                # The delete itself will fail; nothing to record.
                return RecorderCapture(declined=True)
            node_name = context_manager.get_current_node().name

        node = GriptapeNodes.ObjectManager().attempt_get_object_by_name_as_type(node_name, BaseNode)
        if node is None:
            # The delete itself will fail; nothing to record.
            return RecorderCapture(declined=True)

        # Group nodes cascade into their children; single-node serialization does not capture
        # that, so restoring one from here would be incorrect.
        if isinstance(node, BaseNodeGroup):
            return RecorderCapture(declined=True)

        try:
            parent_flow_name = GriptapeNodes.NodeManager().get_node_parent_flow_by_name(node_name)
        except KeyError:
            return RecorderCapture(declined=True)

        serialize_request = SerializeNodeToCommandsRequest(node_name=node_name)
        serialize_result = GriptapeNodes.handle_request(serialize_request)
        if not isinstance(serialize_result, SerializeNodeToCommandsResultSuccess):
            return RecorderCapture(declined=True)

        list_connections_result = GriptapeNodes.handle_request(ListConnectionsForNodeRequest(node_name=node_name))
        if not isinstance(list_connections_result, ListConnectionsForNodeResultSuccess):
            return RecorderCapture(declined=True)

        # Pin the flow and group membership so restoration lands where the node came from,
        # regardless of what the Current Context looks like at undo time.
        create_node_command = serialize_result.serialized_node_commands.create_node_command
        create_node_command.override_parent_flow_name = parent_flow_name
        if node.parent_group is not None:
            create_node_command.parent_group_name = node.parent_group.name

        entry = DeleteNodeUndoEntry(
            node_name=node_name,
            serialized_node_commands=serialize_result.serialized_node_commands,
            set_parameter_value_commands=serialize_result.set_parameter_value_commands,
            unique_parameter_uuid_to_values=serialize_request.unique_parameter_uuid_to_values,
            incoming_connections=list_connections_result.incoming_connections,
            outgoing_connections=list_connections_result.outgoing_connections,
        )
        return RecorderCapture(state=entry)

    def create_batch(self, request: RequestPayload, result: ResultPayload, state: Any) -> UndoBatch | None:  # noqa: ARG002
        entry: DeleteNodeUndoEntry = state
        return UndoBatch(label=f"Delete node '{entry.node_name}'", entries=[entry])


@dataclass
class _ParameterEditCapture:
    """Pre-set snapshot of a property edit: the values needed to reverse and re-apply it."""

    node_name: str
    parameter_name: str
    data_type: str
    old_value: Any


class SetParameterValueRecorder(UndoRecorder):
    """Records a user property edit so undo restores the previous value and redo re-applies the new one.

    Only genuine user property edits are reversible. Internal writes -- workflow load
    (initial_setup), output-value writes, and downstream propagation (an incoming connection
    source) -- are folded into the originating action instead, so they are treated as no-ops here.
    A set that does not change the value is likewise a no-op.
    """

    def capture_before(self, request: RequestPayload) -> RecorderCapture:  # noqa: PLR0911
        if not isinstance(request, SetParameterValueRequest):
            return RecorderCapture(declined=True)
        # Not a user property edit: fold into the originating action rather than record it here.
        incoming_node_set = request.incoming_connection_source_node_name is not None
        if request.initial_setup or request.is_output or incoming_node_set:
            return RecorderCapture(state=None)

        node_name = request.node_name
        node = None
        if node_name is None:
            context_manager = GriptapeNodes.ContextManager()
            if not context_manager.has_current_node():
                # The set itself will fail; nothing to record.
                return RecorderCapture(state=None)
            node = context_manager.get_current_node()
            node_name = node.name
        if node is None:
            node = GriptapeNodes.ObjectManager().attempt_get_object_by_name_as_type(node_name, BaseNode)
        if node is None:
            return RecorderCapture(state=None)

        parameter = node.get_parameter_by_name(request.parameter_name)
        if parameter is None:
            return RecorderCapture(state=None)

        try:
            # Snapshot before the mutation so an in-place change during the set cannot corrupt it.
            old_value = copy.deepcopy(node.get_parameter_value(request.parameter_name))
        except Exception:
            # A value that cannot be deep-copied simply will not be undoable.
            return RecorderCapture(state=None)

        return RecorderCapture(
            state=_ParameterEditCapture(
                node_name=node_name,
                parameter_name=request.parameter_name,
                data_type=parameter.type,
                old_value=old_value,
            )
        )

    def create_batch(self, request: RequestPayload, result: ResultPayload, state: Any) -> UndoBatch | None:  # noqa: ARG002
        # No captured state (internal write, missing node/parameter) or an unexpected result type:
        # record nothing without invalidating history.
        if state is None or not isinstance(result, SetParameterValueResultSuccess):
            return UndoBatch(label="", entries=[])
        capture: _ParameterEditCapture = state
        # A set that did not change the value is a no-op.
        if capture.old_value == result.finalized_value:
            return UndoBatch(label="", entries=[])

        undo_request = SetParameterValueRequest(
            node_name=capture.node_name,
            parameter_name=capture.parameter_name,
            value=copy.deepcopy(capture.old_value),
            data_type=capture.data_type,
        )
        redo_request = SetParameterValueRequest(
            node_name=capture.node_name,
            parameter_name=capture.parameter_name,
            value=copy.deepcopy(result.finalized_value),
            data_type=capture.data_type,
        )
        return UndoBatch(
            label=f"Set '{capture.node_name}.{capture.parameter_name}'",
            entries=[RequestReplayUndoEntry(undo_requests=[undo_request], redo_requests=[redo_request])],
        )
