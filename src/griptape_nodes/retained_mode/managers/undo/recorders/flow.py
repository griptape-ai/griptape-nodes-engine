"""Undo recorders for connection operations.

This module holds the flow domain's knowledge of how to reverse connection create/delete. The
UndoManager owns the mechanism (stacks, batching, replay); FlowManager registers these recorders
with it from its __init__. Nothing in the undo core imports this module -- FlowManager is the sole
wiring point.

A connection recorder mirrors the two things its handler decides that the request/result do not
carry: the resolved endpoint names (a handler resolves ``None`` names from the current context)
and, for create, whether the connection is a group proxy remap (which the handler does not record)
and the target's pre-connection PROPERTY value (which the connection overwrites and undo restores).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from griptape_nodes.exe_types.core_types import ParameterMode, ParameterType, ParameterTypeBuiltin
from griptape_nodes.exe_types.node_groups import SubflowNodeGroup
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
    DeleteConnectionRequest,
    DeleteConnectionResultSuccess,
)
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.undo import (
    RecorderCapture,
    RequestReplayUndoEntry,
    UndoBatch,
    UndoRecorder,
)

if TYPE_CHECKING:
    from griptape_nodes.exe_types.core_types import Parameter
    from griptape_nodes.exe_types.node_types import BaseNode as BaseNodeType
    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload


@dataclass
class _ConnectionEndpoints:
    """Resolved endpoints of a connection, plus the target's pre-connection value to restore on undo."""

    source_node_name: str
    source_parameter_name: str
    target_node_name: str
    target_parameter_name: str
    target_data_type: str | None = None
    restore_target_value: bool = False
    target_restore_value: Any = None


def _resolve_node(node_name: str | None) -> BaseNodeType | None:
    """Resolve a connection endpoint node the way the connection handlers do: name, else current context."""
    if node_name is None:
        context_manager = GriptapeNodes.ContextManager()
        if not context_manager.has_current_node():
            return None
        return context_manager.get_current_node()
    return GriptapeNodes.ObjectManager().attempt_get_object_by_name_as_type(node_name, BaseNode)


class CreateConnectionRecorder(UndoRecorder):
    """Records a connection creation so undo deletes it (restoring any overwritten PROPERTY value)."""

    def capture_before(self, request: RequestPayload) -> RecorderCapture:
        if not isinstance(request, CreateConnectionRequest):
            return RecorderCapture(declined=True)

        source_node = _resolve_node(request.source_node_name)
        target_node = _resolve_node(request.target_node_name)
        if source_node is None or target_node is None:
            # The create itself will fail; nothing to record.
            return RecorderCapture(state=None)

        # A connection into or out of a subflow group is remapped to a proxy parameter and the
        # handler returns before recording; skip it here too rather than record a wrong inverse.
        if self._is_proxy_remap(source_node, target_node):
            return RecorderCapture(state=None)

        source_param = source_node.get_parameter_by_name(request.source_parameter_name)
        target_param = target_node.get_parameter_by_name(request.target_parameter_name)
        if source_param is None or target_param is None:
            return RecorderCapture(state=None)

        endpoints = _ConnectionEndpoints(
            source_node_name=source_node.name,
            source_parameter_name=request.source_parameter_name,
            target_node_name=target_node.name,
            target_parameter_name=request.target_parameter_name,
            target_data_type=target_param.type,
        )
        self._capture_prior_target_value(request, source_param, target_node, target_param, endpoints)
        return RecorderCapture(state=endpoints)

    def create_batch(self, request: RequestPayload, result: ResultPayload, state: Any) -> UndoBatch | None:  # noqa: ARG002
        if state is None or not isinstance(result, CreateConnectionResultSuccess):
            return UndoBatch(label="", entries=[])
        endpoints: _ConnectionEndpoints = state

        undo_requests: list[RequestPayload] = [
            DeleteConnectionRequest(
                source_node_name=endpoints.source_node_name,
                source_parameter_name=endpoints.source_parameter_name,
                target_node_name=endpoints.target_node_name,
                target_parameter_name=endpoints.target_parameter_name,
            )
        ]
        if endpoints.restore_target_value:
            # Restore the property value the connection overwrote, after the edge is removed so the
            # INPUT+PROPERTY "connected, cannot set as property" guard no longer blocks the set.
            undo_requests.append(
                SetParameterValueRequest(
                    node_name=endpoints.target_node_name,
                    parameter_name=endpoints.target_parameter_name,
                    value=endpoints.target_restore_value,
                    data_type=endpoints.target_data_type,
                )
            )
        redo_requests: list[RequestPayload] = [
            CreateConnectionRequest(
                source_node_name=endpoints.source_node_name,
                source_parameter_name=endpoints.source_parameter_name,
                target_node_name=endpoints.target_node_name,
                target_parameter_name=endpoints.target_parameter_name,
            )
        ]
        label = (
            f"Connect '{endpoints.source_node_name}.{endpoints.source_parameter_name}' "
            f"to '{endpoints.target_node_name}.{endpoints.target_parameter_name}'"
        )
        return UndoBatch(
            label=label,
            entries=[RequestReplayUndoEntry(undo_requests=undo_requests, redo_requests=redo_requests)],
        )

    @staticmethod
    def _is_proxy_remap(source_node: BaseNodeType, target_node: BaseNodeType) -> bool:
        source_parent = source_node.parent_group
        target_parent = target_node.parent_group
        if (
            source_parent is not None
            and isinstance(source_parent, SubflowNodeGroup)
            and source_parent not in (target_parent, target_node)
        ):
            return True
        return (
            target_parent is not None
            and isinstance(target_parent, SubflowNodeGroup)
            and target_parent not in (source_parent, source_node)
        )

    @staticmethod
    def _capture_prior_target_value(
        request: CreateConnectionRequest,
        source_param: Parameter,
        target_node: BaseNodeType,
        target_param: Parameter,
        endpoints: _ConnectionEndpoints,
    ) -> None:
        """Snapshot the target's PROPERTY value when the connection is about to overwrite it.

        Mirrors the handler's value-passing gate: a control-parameter source, a locked target, or
        initial setup passes no value, so there is nothing to restore. Non-PROPERTY targets are
        wiped on disconnect, which already matches their pre-connection state.

        A PROPERTY target keeps whatever the connection propagated into it even after the edge is
        removed (disconnect only wipes non-PROPERTY targets), so undo must set it back explicitly.
        Snapshot the observable value even when the target had no explicit entry: that value is the
        parameter's default, which is exactly what removing the propagated value restores it to.
        """
        is_control_parameter = (
            ParameterType.attempt_get_builtin(source_param.output_type) == ParameterTypeBuiltin.CONTROL_TYPE
        )
        if is_control_parameter or target_node.lock or request.initial_setup:
            return
        if ParameterMode.PROPERTY not in target_param.allowed_modes:
            return
        try:
            endpoints.target_restore_value = copy.deepcopy(target_node.get_parameter_value(target_param.name))
            endpoints.restore_target_value = True
        except Exception:
            endpoints.restore_target_value = False


class DeleteConnectionRecorder(UndoRecorder):
    """Records a connection deletion so undo re-creates it (re-propagating the source value)."""

    def capture_before(self, request: RequestPayload) -> RecorderCapture:
        if not isinstance(request, DeleteConnectionRequest):
            return RecorderCapture(declined=True)

        source_node = _resolve_node(request.source_node_name)
        target_node = _resolve_node(request.target_node_name)
        if source_node is None or target_node is None:
            # The delete itself will fail; nothing to record.
            return RecorderCapture(state=None)

        # No proxy-remap skip is needed here (unlike CreateConnectionRecorder): the delete handler
        # has no proxy-remap path and validates the raw endpoints via _has_connection, so a
        # raw-endpoint delete of a subflow-proxied connection fails. create_batch only records on
        # DeleteConnectionResultSuccess, so a failed proxy delete records no (wrong) inverse.
        return RecorderCapture(
            state=_ConnectionEndpoints(
                source_node_name=source_node.name,
                source_parameter_name=request.source_parameter_name,
                target_node_name=target_node.name,
                target_parameter_name=request.target_parameter_name,
            )
        )

    def create_batch(self, request: RequestPayload, result: ResultPayload, state: Any) -> UndoBatch | None:  # noqa: ARG002
        if state is None or not isinstance(result, DeleteConnectionResultSuccess):
            return UndoBatch(label="", entries=[])
        endpoints: _ConnectionEndpoints = state
        undo_request = CreateConnectionRequest(
            source_node_name=endpoints.source_node_name,
            source_parameter_name=endpoints.source_parameter_name,
            target_node_name=endpoints.target_node_name,
            target_parameter_name=endpoints.target_parameter_name,
        )
        redo_request = DeleteConnectionRequest(
            source_node_name=endpoints.source_node_name,
            source_parameter_name=endpoints.source_parameter_name,
            target_node_name=endpoints.target_node_name,
            target_parameter_name=endpoints.target_parameter_name,
        )
        label = (
            f"Disconnect '{endpoints.source_node_name}.{endpoints.source_parameter_name}' "
            f"from '{endpoints.target_node_name}.{endpoints.target_parameter_name}'"
        )
        return UndoBatch(
            label=label,
            entries=[RequestReplayUndoEntry(undo_requests=[undo_request], redo_requests=[redo_request])],
        )
