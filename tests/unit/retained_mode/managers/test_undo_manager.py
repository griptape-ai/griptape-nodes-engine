"""Unit tests for `UndoManager`: recording and replaying undoable user actions.

Phase 1 covers node creation and deletion. These tests drive the engine end to end
through a registered probe library so that `CreateNodeRequest`, serialization, and
deserialization all exercise their real handlers rather than mocks.

User-initiated requests are simulated by dispatching through the EventManager with a
``request_id`` in the result context, which is what marks a request as originating from
an external caller (e.g. the editor) and therefore eligible for undo recording.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode, ParameterTypeBuiltin
from griptape_nodes.exe_types.node_types import BaseNode, NodeDependencies
from griptape_nodes.retained_mode.events.base_events import (
    RequestPayload,
    ResultPayloadSuccess,
    WorkflowAlteredMixin,
)
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
    DeleteConnectionRequest,
    DeleteConnectionResultSuccess,
    IncomingConnection,
    OutgoingConnection,
)
from griptape_nodes.retained_mode.events.context_events import SetWorkflowContextRequest
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    CreateNodeResultSuccess,
    DeleteNodeRequest,
    DeleteNodeResultSuccess,
    SerializedNodeCommands,
    SetLockNodeStateRequest,
    SetLockNodeStateResultSuccess,
)
from griptape_nodes.retained_mode.events.object_events import RenameObjectRequest
from griptape_nodes.retained_mode.events.parameter_events import (
    GetParameterValueRequest,
    GetParameterValueResultSuccess,
    SetParameterValueRequest,
    SetParameterValueResultSuccess,
)
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
from griptape_nodes.retained_mode.managers.undo import (
    RecorderCapture,
    RequestReplayUndoEntry,
    UndoBatch,
    UndoEntry,
    UndoEntryReplayError,
    UndoRecorder,
    dispatch_expecting,
    dispatch_expecting_success,
)
from griptape_nodes.retained_mode.managers.undo.core import DispatchTriage, triage_dispatch
from griptape_nodes.retained_mode.managers.undo.manager import MAX_UNDO_BATCHES, UndoManager
from griptape_nodes.retained_mode.managers.undo.recorders.flow import (
    CreateConnectionRecorder,
    DeleteConnectionRecorder,
    _ConnectionEndpoints,
    _resolve_node,
)
from griptape_nodes.retained_mode.managers.undo.recorders.node import (
    CreateNodeRecorder,
    CreateNodeUndoEntry,
    DeleteNodeRecorder,
    DeleteNodeUndoEntry,
    SetParameterValueRecorder,
    _ParameterEditCapture,
    _values_equal,
)
from griptape_nodes.retained_mode.managers.undo.recording import (
    DispatchCapture,
    RecordingSession,
    _as_sequence,
    _prepare_replay_request,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.events.base_events import ResultPayload


def _registered_recorders(undo_manager: UndoManager) -> dict[type[RequestPayload], UndoRecorder]:
    """Recorders on the default inverse RecordingSession (these tests run under the inverse strategy)."""
    return cast("RecordingSession", undo_manager._recording)._recorders


class _ProbeNode(BaseNode):
    """Concrete `BaseNode` with a single settable string parameter."""

    def __init__(self, name: str, metadata=None) -> None:  # noqa: ANN001
        super().__init__(name=name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="text",
                tooltip="probe text",
                type=ParameterTypeBuiltin.STR.value,
                allowed_modes={ParameterMode.PROPERTY, ParameterMode.INPUT, ParameterMode.OUTPUT},
                default_value="",
            )
        )

    def process(self) -> None:
        return None


@dataclass(kw_only=True)
class _RecordInverseProbeRequest(RequestPayload):
    """User request whose handler declares its own inverse via ``record_inverse``.

    Exercises the inline-declaration path (a handler that reverses itself) rather than a
    registered recorder. The handler applies ``new_value`` to a probe node's ``text`` via an
    internal set, then declares the inverse (restore ``old_value``) and forward (re-apply
    ``new_value``). When ``should_raise`` is set it raises after declaring the inverse, leaving
    the workflow partially mutated so the frame must invalidate history.
    """

    node_name: str
    old_value: str
    new_value: str
    should_raise: bool = False
    uncopyable_inverse: bool = False
    broadcast_result: bool = False


@dataclass
class _RecordInverseProbeResult(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Success result for `_RecordInverseProbeRequest`; always marks the workflow altered."""


def _handle_record_inverse_probe(request: _RecordInverseProbeRequest) -> ResultPayload:
    """Apply the edit and declare its inverse the way a self-reversing handler would."""
    # Internal (no request_id) set: nested inside this recording dispatch, so it is not recorded
    # separately; its reversal is owned by the inverse declared below.
    GriptapeNodes.handle_request(
        SetParameterValueRequest(node_name=request.node_name, parameter_name="text", value=request.new_value)
    )
    if request.uncopyable_inverse:
        # An inverse carrying an un-deep-copyable value cannot be snapshotted; the action is simply
        # not undoable and history is left untouched (the type is declared non-undoable).
        GriptapeNodes.UndoManager().record_inverse(
            SetParameterValueRequest(
                node_name=request.node_name, parameter_name="text", value=cast("Any", _Uncopyable())
            ),
            label="Custom edit",
        )
        return _RecordInverseProbeResult(result_details="probe edit applied")
    GriptapeNodes.UndoManager().record_inverse(
        SetParameterValueRequest(node_name=request.node_name, parameter_name="text", value=request.old_value),
        label="Custom edit",
        forward=SetParameterValueRequest(node_name=request.node_name, parameter_name="text", value=request.new_value),
    )
    if request.should_raise:
        msg = "boom-after-record"
        raise RuntimeError(msg)
    return _RecordInverseProbeResult(result_details="probe edit applied")


class _Uncopyable:
    """A value that refuses to be deep-copied, to exercise the snapshot-failure path."""

    def __deepcopy__(self, memo: dict) -> _Uncopyable:
        msg = "cannot deep-copy"
        raise RuntimeError(msg)


@dataclass
class _RaisingUndoEntry(UndoEntry):
    """Undo entry whose replay raises a non-`UndoEntryReplayError`, to hit the unexpected-error path."""

    def undo(self) -> None:
        msg = "entry undo exploded"
        raise ValueError(msg)

    def redo(self) -> None:
        msg = "entry redo exploded"
        raise ValueError(msg)


@dataclass
class _DispatchExpectingUndoEntry(UndoEntry):
    """Undo entry that replays a request expected to fail, exercising the `dispatch_expecting` guard."""

    def undo(self) -> None:
        dispatch_expecting(
            DeleteNodeRequest(node_name="definitely-not-a-real-node"),
            DeleteNodeResultSuccess,
            "undo via deleting a node that does not exist",
        )

    def redo(self) -> None:
        return None


class TestUndoManager:
    _LIBRARY_NAME = "undo-manager-test-library"
    _NODE_TYPE = "_ProbeNode"

    @pytest.fixture(autouse=True)
    def _clean_registry(self):  # noqa: ANN202
        from griptape_nodes.node_library.library_registry import LibraryRegistry

        LibraryRegistry._clear()
        yield
        LibraryRegistry._clear()

    @pytest.fixture(autouse=True)
    def _reset_undo_history(self, griptape_nodes: GriptapeNodes):  # noqa: ANN202, ARG002
        GriptapeNodes.UndoManager().clear_history()
        yield
        GriptapeNodes.UndoManager().clear_history()

    def _register_library(self) -> None:
        from griptape_nodes.node_library.library_registry import (
            LibraryMetadata,
            LibraryRegistry,
            LibrarySchema,
            NodeMetadata,
        )

        schema = LibrarySchema(
            name=self._LIBRARY_NAME,
            library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
            metadata=LibraryMetadata(
                author="t",
                description="d",
                library_version="1.0.0",
                engine_version="1.0.0",
                tags=[],
            ),
            categories=[],
            nodes=[],
        )
        library = LibraryRegistry.generate_new_library(library_data=schema)
        library.register_new_node_type(
            _ProbeNode,
            NodeMetadata(category="t", description="d", display_name="Probe"),
        )

    def _make_flow(self, griptape_nodes: GriptapeNodes) -> str:
        from griptape_nodes.node_library.workflow_registry import WorkflowRegistry
        from griptape_nodes.retained_mode.managers.workflow_manager import WorkflowMetadata

        context_manager = griptape_nodes.ContextManager()
        if not context_manager.has_current_workflow():
            workflow_key = "unsaved:undo-test"
            if workflow_key not in WorkflowRegistry._workflows:
                metadata = WorkflowMetadata(
                    name="Untitled",
                    schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
                    engine_version_created_with="",
                    node_libraries_referenced=[],
                    creation_date=datetime.now(UTC),
                )
                WorkflowRegistry.generate_new_workflow(registry_key=workflow_key, metadata=metadata, file_path=None)
            context_manager.push_workflow(workflow_name=workflow_key)
        create_result = griptape_nodes.handle_request(CreateFlowRequest(parent_flow_name=None))
        assert isinstance(create_result, CreateFlowResultSuccess)
        return create_result.flow_name

    @staticmethod
    def _user_request(request: RequestPayload, request_id: str = "test-request") -> ResultPayload:
        """Dispatch a request as if it came from an external caller (carries a request_id)."""
        event_manager = GriptapeNodes.EventManager()
        result_event = event_manager.handle_request(request, result_context={"request_id": request_id})
        return result_event.result

    def _create_node(self, flow_name: str, node_name: str | None = None) -> CreateNodeResultSuccess:
        result = self._user_request(
            CreateNodeRequest(
                node_type=self._NODE_TYPE,
                specific_library_name=self._LIBRARY_NAME,
                node_name=node_name,
                override_parent_flow_name=flow_name,
            )
        )
        assert isinstance(result, CreateNodeResultSuccess)
        return result

    @staticmethod
    def _connection_exists(target_node: str, target_param: str) -> bool:
        incoming = GriptapeNodes.FlowManager().get_connections().incoming_index.get(target_node, {})
        return target_param in incoming

    @staticmethod
    def _ensure_record_inverse_probe_handler() -> None:
        """Register the self-reversing probe handler once on the shared EventManager."""
        event_manager = GriptapeNodes.EventManager()
        if _RecordInverseProbeRequest not in event_manager._request_type_to_manager:
            event_manager.assign_manager_to_request_type(_RecordInverseProbeRequest, _handle_record_inverse_probe)
        # A type that declares its own inverse is, like the real inline-recording types, declared
        # non-undoable so a failed snapshot leaves history untouched instead of invalidating it.
        GriptapeNodes.UndoManager().register_non_undoable(_RecordInverseProbeRequest)

    # ---------- Create node ----------

    def test_create_node_records_undo_batch(self, griptape_nodes: GriptapeNodes) -> None:
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)

        self._create_node(flow_name, node_name="ProbeA")

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == ["Create node 'ProbeA'"]
        assert state.redo_labels == []

    def test_undo_create_deletes_node(self, griptape_nodes: GriptapeNodes) -> None:
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        create_result = self._create_node(flow_name, node_name="ProbeA")
        assert GriptapeNodes.ObjectManager().has_object_with_name(create_result.node_name)

        undo_result = GriptapeNodes.handle_request(UndoRequest())

        assert isinstance(undo_result, UndoResultSuccess)
        assert undo_result.undone_label == "Create node 'ProbeA'"
        assert not GriptapeNodes.ObjectManager().has_object_with_name("ProbeA")

    def test_redo_create_restores_node(self, griptape_nodes: GriptapeNodes) -> None:
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")

        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)
        assert not GriptapeNodes.ObjectManager().has_object_with_name("ProbeA")

        redo_result = GriptapeNodes.handle_request(RedoRequest())

        assert isinstance(redo_result, RedoResultSuccess)
        assert redo_result.redone_label == "Create node 'ProbeA'"
        assert GriptapeNodes.ObjectManager().has_object_with_name("ProbeA")

    def test_create_undo_redo_roundtrip_multiple(self, griptape_nodes: GriptapeNodes) -> None:
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")
        self._create_node(flow_name, node_name="ProbeB")

        # Undo B then A.
        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)
        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)
        assert not GriptapeNodes.ObjectManager().has_object_with_name("ProbeA")
        assert not GriptapeNodes.ObjectManager().has_object_with_name("ProbeB")

        # Redo A then B.
        assert isinstance(GriptapeNodes.handle_request(RedoRequest()), RedoResultSuccess)
        assert isinstance(GriptapeNodes.handle_request(RedoRequest()), RedoResultSuccess)
        assert GriptapeNodes.ObjectManager().has_object_with_name("ProbeA")
        assert GriptapeNodes.ObjectManager().has_object_with_name("ProbeB")

    # ---------- Delete node ----------

    def test_undo_delete_restores_node_with_value(self, griptape_nodes: GriptapeNodes) -> None:
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")

        set_value_result = self._user_request(
            SetParameterValueRequest(node_name="ProbeA", parameter_name="text", value="hello")
        )
        assert set_value_result.succeeded()

        delete_result = self._user_request(DeleteNodeRequest(node_name="ProbeA"))
        assert isinstance(delete_result, DeleteNodeResultSuccess)
        assert not GriptapeNodes.ObjectManager().has_object_with_name("ProbeA")

        undo_result = GriptapeNodes.handle_request(UndoRequest())

        assert isinstance(undo_result, UndoResultSuccess)
        assert undo_result.undone_label == "Delete node 'ProbeA'"
        assert GriptapeNodes.ObjectManager().has_object_with_name("ProbeA")
        value_result = GriptapeNodes.handle_request(GetParameterValueRequest(node_name="ProbeA", parameter_name="text"))
        assert isinstance(value_result, GetParameterValueResultSuccess)
        assert value_result.value == "hello"

    def test_undo_delete_restores_connection(self, griptape_nodes: GriptapeNodes) -> None:
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="Source")
        self._create_node(flow_name, node_name="Target")

        connect_result = self._user_request(
            CreateConnectionRequest(
                source_node_name="Source",
                source_parameter_name="text",
                target_node_name="Target",
                target_parameter_name="text",
            )
        )
        assert isinstance(connect_result, CreateConnectionResultSuccess)

        # Deleting Target cascades its connection away; undo must bring it back.
        assert isinstance(self._user_request(DeleteNodeRequest(node_name="Target")), DeleteNodeResultSuccess)

        undo_result = GriptapeNodes.handle_request(UndoRequest())

        assert isinstance(undo_result, UndoResultSuccess)
        assert GriptapeNodes.ObjectManager().has_object_with_name("Target")
        connections = GriptapeNodes.FlowManager().get_connections()
        target_incoming = connections.incoming_index.get("Target", {})
        assert "text" in target_incoming

    def test_redo_delete_removes_node_again(self, griptape_nodes: GriptapeNodes) -> None:
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")
        assert isinstance(self._user_request(DeleteNodeRequest(node_name="ProbeA")), DeleteNodeResultSuccess)

        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)
        assert GriptapeNodes.ObjectManager().has_object_with_name("ProbeA")

        redo_result = GriptapeNodes.handle_request(RedoRequest())

        assert isinstance(redo_result, RedoResultSuccess)
        assert not GriptapeNodes.ObjectManager().has_object_with_name("ProbeA")

    # ---------- Parameter value ----------

    def test_set_parameter_value_records_batch(self, griptape_nodes: GriptapeNodes) -> None:
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")

        set_result = self._user_request(
            SetParameterValueRequest(node_name="ProbeA", parameter_name="text", value="hello")
        )
        assert set_result.succeeded()

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == ["Create node 'ProbeA'", "Set 'ProbeA.text'"]

    def test_undo_set_parameter_value_restores_previous_value(self, griptape_nodes: GriptapeNodes) -> None:
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")
        assert self._user_request(
            SetParameterValueRequest(node_name="ProbeA", parameter_name="text", value="first")
        ).succeeded()
        assert self._user_request(
            SetParameterValueRequest(node_name="ProbeA", parameter_name="text", value="second")
        ).succeeded()

        undo_result = GriptapeNodes.handle_request(UndoRequest())
        assert isinstance(undo_result, UndoResultSuccess)
        assert undo_result.undone_label == "Set 'ProbeA.text'"

        value_result = GriptapeNodes.handle_request(GetParameterValueRequest(node_name="ProbeA", parameter_name="text"))
        assert isinstance(value_result, GetParameterValueResultSuccess)
        assert value_result.value == "first"

    def test_undo_redo_parameter_value_roundtrip(self, griptape_nodes: GriptapeNodes) -> None:
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")
        assert self._user_request(
            SetParameterValueRequest(node_name="ProbeA", parameter_name="text", value="hello")
        ).succeeded()

        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)
        after_undo = GriptapeNodes.handle_request(GetParameterValueRequest(node_name="ProbeA", parameter_name="text"))
        assert isinstance(after_undo, GetParameterValueResultSuccess)
        assert after_undo.value == ""

        redo_result = GriptapeNodes.handle_request(RedoRequest())
        assert isinstance(redo_result, RedoResultSuccess)
        assert redo_result.redone_label == "Set 'ProbeA.text'"
        after_redo = GriptapeNodes.handle_request(GetParameterValueRequest(node_name="ProbeA", parameter_name="text"))
        assert isinstance(after_redo, GetParameterValueResultSuccess)
        assert after_redo.value == "hello"

    def test_internal_parameter_set_not_recorded(self, griptape_nodes: GriptapeNodes) -> None:
        """A set with no request_id (internal origin) is not recorded."""
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")

        result = griptape_nodes.handle_request(
            SetParameterValueRequest(node_name="ProbeA", parameter_name="text", value="internal")
        )
        assert result.succeeded()

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == ["Create node 'ProbeA'"]

    # ---------- Connections ----------

    def test_undo_create_connection_removes_it(self, griptape_nodes: GriptapeNodes) -> None:
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="Source")
        self._create_node(flow_name, node_name="Target")

        connect_result = self._user_request(
            CreateConnectionRequest(
                source_node_name="Source",
                source_parameter_name="text",
                target_node_name="Target",
                target_parameter_name="text",
            )
        )
        assert isinstance(connect_result, CreateConnectionResultSuccess)
        assert self._connection_exists("Target", "text")

        undo_result = GriptapeNodes.handle_request(UndoRequest())
        assert isinstance(undo_result, UndoResultSuccess)
        assert not self._connection_exists("Target", "text")

        redo_result = GriptapeNodes.handle_request(RedoRequest())
        assert isinstance(redo_result, RedoResultSuccess)
        assert self._connection_exists("Target", "text")

    def test_undo_create_connection_restores_overwritten_property_value(self, griptape_nodes: GriptapeNodes) -> None:
        """Undoing a connection restores the PROPERTY value the connection overwrote on the target."""
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="Source")
        self._create_node(flow_name, node_name="Target")

        assert self._user_request(
            SetParameterValueRequest(node_name="Target", parameter_name="text", value="typed")
        ).succeeded()
        assert self._user_request(
            SetParameterValueRequest(node_name="Source", parameter_name="text", value="world")
        ).succeeded()

        # Connecting overwrites Target.text with Source's value.
        assert isinstance(
            self._user_request(
                CreateConnectionRequest(
                    source_node_name="Source",
                    source_parameter_name="text",
                    target_node_name="Target",
                    target_parameter_name="text",
                )
            ),
            CreateConnectionResultSuccess,
        )
        overwritten = GriptapeNodes.handle_request(GetParameterValueRequest(node_name="Target", parameter_name="text"))
        assert isinstance(overwritten, GetParameterValueResultSuccess)
        assert overwritten.value == "world"

        # Undo removes the edge and restores the value the connection overwrote.
        undo_result = GriptapeNodes.handle_request(UndoRequest())
        assert isinstance(undo_result, UndoResultSuccess)
        assert not self._connection_exists("Target", "text")
        restored = GriptapeNodes.handle_request(GetParameterValueRequest(node_name="Target", parameter_name="text"))
        assert isinstance(restored, GetParameterValueResultSuccess)
        assert restored.value == "typed"

    def test_undo_create_connection_to_locked_target_only_detaches(self, griptape_nodes: GriptapeNodes) -> None:
        """A locked target is not overwritten by the connection, so undo just detaches and preserves history."""
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="Source")
        self._create_node(flow_name, node_name="Target")

        assert self._user_request(
            SetParameterValueRequest(node_name="Target", parameter_name="text", value="typed")
        ).succeeded()
        assert isinstance(
            self._user_request(SetLockNodeStateRequest(node_name="Target", lock=True)),
            SetLockNodeStateResultSuccess,
        )
        assert self._user_request(
            SetParameterValueRequest(node_name="Source", parameter_name="text", value="world")
        ).succeeded()

        assert isinstance(
            self._user_request(
                CreateConnectionRequest(
                    source_node_name="Source",
                    source_parameter_name="text",
                    target_node_name="Target",
                    target_parameter_name="text",
                )
            ),
            CreateConnectionResultSuccess,
        )
        # Locked target keeps its own value; the connection did not overwrite it.
        blocked = GriptapeNodes.handle_request(GetParameterValueRequest(node_name="Target", parameter_name="text"))
        assert isinstance(blocked, GetParameterValueResultSuccess)
        assert blocked.value == "typed"

        # Undo must succeed by only detaching (no restore-set against the locked node) and must not
        # wipe the earlier history.
        undo_result = GriptapeNodes.handle_request(UndoRequest())
        assert isinstance(undo_result, UndoResultSuccess)
        assert not self._connection_exists("Target", "text")
        after_undo = GriptapeNodes.handle_request(GetParameterValueRequest(node_name="Target", parameter_name="text"))
        assert isinstance(after_undo, GetParameterValueResultSuccess)
        assert after_undo.value == "typed"
        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels != []

    def test_undo_delete_connection_restores_it(self, griptape_nodes: GriptapeNodes) -> None:
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="Source")
        self._create_node(flow_name, node_name="Target")
        assert isinstance(
            self._user_request(
                CreateConnectionRequest(
                    source_node_name="Source",
                    source_parameter_name="text",
                    target_node_name="Target",
                    target_parameter_name="text",
                )
            ),
            CreateConnectionResultSuccess,
        )

        delete_result = self._user_request(
            DeleteConnectionRequest(
                source_node_name="Source",
                source_parameter_name="text",
                target_node_name="Target",
                target_parameter_name="text",
            )
        )
        assert isinstance(delete_result, DeleteConnectionResultSuccess)
        assert not self._connection_exists("Target", "text")

        undo_result = GriptapeNodes.handle_request(UndoRequest())
        assert isinstance(undo_result, UndoResultSuccess)
        assert self._connection_exists("Target", "text")

        redo_result = GriptapeNodes.handle_request(RedoRequest())
        assert isinstance(redo_result, RedoResultSuccess)
        assert not self._connection_exists("Target", "text")

    def test_connection_records_batch_label(self, griptape_nodes: GriptapeNodes) -> None:
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="Source")
        self._create_node(flow_name, node_name="Target")

        self._user_request(
            CreateConnectionRequest(
                source_node_name="Source",
                source_parameter_name="text",
                target_node_name="Target",
                target_parameter_name="text",
            )
        )

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels[-1] == "Connect 'Source.text' to 'Target.text'"

    def test_delete_node_cascade_does_not_double_record_connections(self, griptape_nodes: GriptapeNodes) -> None:
        """Deleting a connected node records only the delete; its connection cascade must not add entries.

        Regression guard for record_inverse attributing a nested cascade dispatch to the outer frame:
        undoing the delete must restore the node and its connection exactly once.
        """
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="Source")
        self._create_node(flow_name, node_name="Target")
        assert isinstance(
            self._user_request(
                CreateConnectionRequest(
                    source_node_name="Source",
                    source_parameter_name="text",
                    target_node_name="Target",
                    target_parameter_name="text",
                )
            ),
            CreateConnectionResultSuccess,
        )

        # Deleting Target cascades a DeleteConnection internally. That cascade must not record a
        # separate entry attributed to the delete's frame.
        delete_result = self._user_request(DeleteNodeRequest(node_name="Target"))
        assert isinstance(delete_result, DeleteNodeResultSuccess)

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels[-1] == "Delete node 'Target'"

        undo_result = GriptapeNodes.handle_request(UndoRequest())
        assert isinstance(undo_result, UndoResultSuccess)
        assert undo_result.undone_label == "Delete node 'Target'"
        assert GriptapeNodes.ObjectManager().has_object_with_name("Target")
        assert self._connection_exists("Target", "text")

    # ---------- Recording eligibility ----------

    def test_internal_request_not_recorded(self, griptape_nodes: GriptapeNodes) -> None:
        """A request without a request_id (internal origin) is not recorded."""
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)

        # Dispatch through the public sync handler, which carries no request_id.
        result = griptape_nodes.handle_request(
            CreateNodeRequest(
                node_type=self._NODE_TYPE,
                specific_library_name=self._LIBRARY_NAME,
                node_name="Internal",
                override_parent_flow_name=flow_name,
            )
        )
        assert isinstance(result, CreateNodeResultSuccess)

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == []

    def test_untracked_user_mutation_clears_history(self, griptape_nodes: GriptapeNodes) -> None:
        """A user mutation with no recorder invalidates existing undo history."""
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == ["Create node 'ProbeA'"]

        # Rename is a user mutation with no undo recorder; it should clear history.
        rename_result = self._user_request(RenameObjectRequest(object_name="ProbeA", requested_name="ProbeRenamed"))
        assert rename_result.succeeded()

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == []

        undo_result = GriptapeNodes.handle_request(UndoRequest())
        assert isinstance(undo_result, UndoResultFailure)

    def test_noop_parameter_set_preserves_history(self, griptape_nodes: GriptapeNodes) -> None:
        """Setting a parameter to its current value changes nothing, so it neither records nor clears."""
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")

        # The parameter already holds its default (""); setting it again is a no-op.
        set_value_result = self._user_request(
            SetParameterValueRequest(node_name="ProbeA", parameter_name="text", value="")
        )
        assert set_value_result.succeeded()

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        # Create is still undoable; the no-op set neither cleared it nor added an entry.
        assert state.undo_labels == ["Create node 'ProbeA'"]

    def test_new_recorded_action_clears_redo_stack(self, griptape_nodes: GriptapeNodes) -> None:
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")

        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)
        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.redo_labels == ["Create node 'ProbeA'"]

        # A fresh recorded action invalidates the redo stack.
        self._create_node(flow_name, node_name="ProbeB")
        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == ["Create node 'ProbeB'"]
        assert state.redo_labels == []

    # ---------- Stack management ----------

    def test_undo_empty_stack_fails(self, griptape_nodes: GriptapeNodes) -> None:  # noqa: ARG002
        result = GriptapeNodes.handle_request(UndoRequest())
        assert isinstance(result, UndoResultFailure)

    def test_redo_empty_stack_fails(self, griptape_nodes: GriptapeNodes) -> None:  # noqa: ARG002
        result = GriptapeNodes.handle_request(RedoRequest())
        assert isinstance(result, RedoResultFailure)

    def test_clear_undo_state(self, griptape_nodes: GriptapeNodes) -> None:
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")

        clear_result = GriptapeNodes.handle_request(ClearUndoStateRequest())
        assert isinstance(clear_result, ClearUndoStateResultSuccess)

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == []
        assert state.redo_labels == []

    # ---------- Registration API (domain-owned reversal knowledge) ----------

    def test_recorders_registered_by_node_domain(self, griptape_nodes: GriptapeNodes) -> None:
        """NodeManager registers its recorders with the UndoManager; the manager holds no domain literals."""
        undo_manager = griptape_nodes.UndoManager()
        recorders = _registered_recorders(undo_manager)
        assert CreateNodeRequest in recorders
        assert DeleteNodeRequest in recorders
        assert SetParameterValueRequest in recorders
        assert CreateConnectionRequest in recorders
        assert DeleteConnectionRequest in recorders

    def test_register_recorder_rejects_duplicate(self, griptape_nodes: GriptapeNodes) -> None:
        """A second recorder for the same request type is a registration error, mirroring handlers."""
        from griptape_nodes.retained_mode.managers.undo.recorders.node import CreateNodeRecorder

        undo_manager = griptape_nodes.UndoManager()
        with pytest.raises(ValueError, match="already registered"):
            undo_manager.register_recorder(CreateNodeRequest, CreateNodeRecorder())

    # ---------- record_inverse (in-handler declaration) ----------

    def test_record_inverse_noop_outside_frame(self, griptape_nodes: GriptapeNodes) -> None:
        """record_inverse is a safe no-op when nothing is being recorded."""
        undo_manager = griptape_nodes.UndoManager()
        undo_manager.record_inverse(
            SetParameterValueRequest(node_name="X", parameter_name="text", value="v"),
            label="noop",
        )
        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == []

    # ---------- transaction (multi-request grouping) ----------

    def test_transaction_groups_multiple_creates_into_one_batch(self, griptape_nodes: GriptapeNodes) -> None:
        """Requests issued inside a transaction collapse into a single undo batch."""
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        undo_manager = griptape_nodes.UndoManager()

        with undo_manager.transaction("Add pair"):
            # Internal requests (no request_id) still contribute inside a transaction.
            griptape_nodes.handle_request(
                CreateNodeRequest(
                    node_type=self._NODE_TYPE,
                    specific_library_name=self._LIBRARY_NAME,
                    node_name="Pair1",
                    override_parent_flow_name=flow_name,
                )
            )
            griptape_nodes.handle_request(
                CreateNodeRequest(
                    node_type=self._NODE_TYPE,
                    specific_library_name=self._LIBRARY_NAME,
                    node_name="Pair2",
                    override_parent_flow_name=flow_name,
                )
            )

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == ["Add pair"]

        # One undo reverts both creations.
        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)
        assert not GriptapeNodes.ObjectManager().has_object_with_name("Pair1")
        assert not GriptapeNodes.ObjectManager().has_object_with_name("Pair2")

        # One redo restores both.
        assert isinstance(GriptapeNodes.handle_request(RedoRequest()), RedoResultSuccess)
        assert GriptapeNodes.ObjectManager().has_object_with_name("Pair1")
        assert GriptapeNodes.ObjectManager().has_object_with_name("Pair2")

    def test_transaction_rolls_back_history_on_error(self, griptape_nodes: GriptapeNodes) -> None:
        """An exception escaping a transaction clears history rather than committing a partial batch."""
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="Existing")
        undo_manager = griptape_nodes.UndoManager()

        def _doomed_transaction() -> None:
            with undo_manager.transaction("Doomed"):
                griptape_nodes.handle_request(
                    CreateNodeRequest(
                        node_type=self._NODE_TYPE,
                        specific_library_name=self._LIBRARY_NAME,
                        node_name="Partial",
                        override_parent_flow_name=flow_name,
                    )
                )
                msg = "boom"
                raise RuntimeError(msg)

        with pytest.raises(RuntimeError, match="boom"):
            _doomed_transaction()

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == []

    def test_nested_transaction_is_passthrough(self, griptape_nodes: GriptapeNodes) -> None:
        """A transaction opened inside another is a no-op passthrough: both collapse into one batch."""
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        undo_manager = griptape_nodes.UndoManager()

        with undo_manager.transaction("Outer"):
            griptape_nodes.handle_request(
                CreateNodeRequest(
                    node_type=self._NODE_TYPE,
                    specific_library_name=self._LIBRARY_NAME,
                    node_name="Outer1",
                    override_parent_flow_name=flow_name,
                )
            )
            with undo_manager.transaction("Inner"):
                griptape_nodes.handle_request(
                    CreateNodeRequest(
                        node_type=self._NODE_TYPE,
                        specific_library_name=self._LIBRARY_NAME,
                        node_name="Inner1",
                        override_parent_flow_name=flow_name,
                    )
                )

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        # The inner transaction contributed nothing of its own; the outer label wins the single batch.
        assert state.undo_labels == ["Outer"]

        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)
        assert not GriptapeNodes.ObjectManager().has_object_with_name("Outer1")
        assert not GriptapeNodes.ObjectManager().has_object_with_name("Inner1")

    def test_empty_transaction_commits_nothing(self, griptape_nodes: GriptapeNodes) -> None:
        """A transaction that records no entries commits no batch and leaves history untouched."""
        self._register_library()
        self._make_flow(griptape_nodes)
        undo_manager = griptape_nodes.UndoManager()

        with undo_manager.transaction("Nothing happens"):
            pass

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == []
        assert state.redo_labels == []

    # ---------- record_inverse (in-handler declaration, happy path) ----------

    def test_record_inverse_records_and_replays_declared_inverse(self, griptape_nodes: GriptapeNodes) -> None:
        """A handler that declares its own inverse is undone/redone by replaying those requests."""
        self._ensure_record_inverse_probe_handler()
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")
        # Seed the starting value internally so it is not itself recorded.
        assert griptape_nodes.handle_request(
            SetParameterValueRequest(node_name="ProbeA", parameter_name="text", value="before")
        ).succeeded()

        result = self._user_request(
            _RecordInverseProbeRequest(node_name="ProbeA", old_value="before", new_value="after")
        )
        assert isinstance(result, _RecordInverseProbeResult)
        applied = GriptapeNodes.handle_request(GetParameterValueRequest(node_name="ProbeA", parameter_name="text"))
        assert isinstance(applied, GetParameterValueResultSuccess)
        assert applied.value == "after"

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == ["Create node 'ProbeA'", "Custom edit"]

        undo_result = GriptapeNodes.handle_request(UndoRequest())
        assert isinstance(undo_result, UndoResultSuccess)
        assert undo_result.undone_label == "Custom edit"
        after_undo = GriptapeNodes.handle_request(GetParameterValueRequest(node_name="ProbeA", parameter_name="text"))
        assert isinstance(after_undo, GetParameterValueResultSuccess)
        assert after_undo.value == "before"

        redo_result = GriptapeNodes.handle_request(RedoRequest())
        assert isinstance(redo_result, RedoResultSuccess)
        assert redo_result.redone_label == "Custom edit"
        after_redo = GriptapeNodes.handle_request(GetParameterValueRequest(node_name="ProbeA", parameter_name="text"))
        assert isinstance(after_redo, GetParameterValueResultSuccess)
        assert after_redo.value == "after"

    def test_handler_raising_after_record_inverse_clears_history(self, griptape_nodes: GriptapeNodes) -> None:
        """A handler that raises after declaring an inverse leaves state partially mutated, so history clears."""
        self._ensure_record_inverse_probe_handler()
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")
        assert griptape_nodes.handle_request(
            SetParameterValueRequest(node_name="ProbeA", parameter_name="text", value="before")
        ).succeeded()

        with pytest.raises(RuntimeError, match="boom-after-record"):
            self._user_request(
                _RecordInverseProbeRequest(node_name="ProbeA", old_value="before", new_value="after", should_raise=True)
            )

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == []

    # ---------- Replay failure clears history ----------

    def test_redo_create_name_collision_clears_history(self, griptape_nodes: GriptapeNodes) -> None:
        """Redo that cannot restore a node under its original name clears history and fails."""
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")
        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)
        assert not GriptapeNodes.ObjectManager().has_object_with_name("ProbeA")

        # Occupy the original name with an unrelated, unrecorded node so redo cannot reuse it.
        squatter = griptape_nodes.handle_request(
            CreateNodeRequest(
                node_type=self._NODE_TYPE,
                specific_library_name=self._LIBRARY_NAME,
                node_name="ProbeA",
                override_parent_flow_name=flow_name,
            )
        )
        assert isinstance(squatter, CreateNodeResultSuccess)

        redo_result = GriptapeNodes.handle_request(RedoRequest())
        assert isinstance(redo_result, RedoResultFailure)
        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == []
        assert state.redo_labels == []

    def test_undo_delete_name_collision_clears_history(self, griptape_nodes: GriptapeNodes) -> None:
        """Undo of a delete that cannot restore under the original name clears history and fails."""
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")
        assert isinstance(self._user_request(DeleteNodeRequest(node_name="ProbeA")), DeleteNodeResultSuccess)

        # Occupy the original name so the deserialize restores under a different name.
        squatter = griptape_nodes.handle_request(
            CreateNodeRequest(
                node_type=self._NODE_TYPE,
                specific_library_name=self._LIBRARY_NAME,
                node_name="ProbeA",
                override_parent_flow_name=flow_name,
            )
        )
        assert isinstance(squatter, CreateNodeResultSuccess)

        undo_result = GriptapeNodes.handle_request(UndoRequest())
        assert isinstance(undo_result, UndoResultFailure)
        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == []
        assert state.redo_labels == []

    # ---------- Guards: flow running ----------

    def test_undo_fails_while_flow_running(
        self, griptape_nodes: GriptapeNodes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")
        monkeypatch.setattr(GriptapeNodes.FlowManager(), "check_for_existing_running_flow", lambda: True)

        undo_result = GriptapeNodes.handle_request(UndoRequest())
        assert isinstance(undo_result, UndoResultFailure)
        # The guard must not consume the stack.
        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == ["Create node 'ProbeA'"]

    def test_redo_fails_while_flow_running(
        self, griptape_nodes: GriptapeNodes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")
        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)
        monkeypatch.setattr(GriptapeNodes.FlowManager(), "check_for_existing_running_flow", lambda: True)

        redo_result = GriptapeNodes.handle_request(RedoRequest())
        assert isinstance(redo_result, RedoResultFailure)
        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.redo_labels == ["Create node 'ProbeA'"]

    # ---------- Lifecycle requests invalidate history ----------

    def test_set_workflow_context_clears_history(self, griptape_nodes: GriptapeNodes) -> None:
        """A workflow-context switch restructures the object graph, so it invalidates undo history."""
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")
        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == ["Create node 'ProbeA'"]

        # Even an internal dispatch of a history-clearing lifecycle type invalidates history.
        griptape_nodes.handle_request(SetWorkflowContextRequest(workflow_name="unsaved:undo-test"))

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == []

    # ---------- Non-undoable requests neither record nor clear ----------

    def test_non_undoable_request_preserves_history(self, griptape_nodes: GriptapeNodes) -> None:
        """A user mutation declared non-undoable (lock) neither records an entry nor clears history."""
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")

        lock_result = self._user_request(SetLockNodeStateRequest(node_name="ProbeA", lock=True))
        assert isinstance(lock_result, SetLockNodeStateResultSuccess)

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == ["Create node 'ProbeA'"]

    def test_failed_request_preserves_history(self, griptape_nodes: GriptapeNodes) -> None:
        """A failed user mutation records nothing and does not clear history."""
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")

        # Setting a nonexistent parameter fails; the failure must not touch history.
        set_result = self._user_request(
            SetParameterValueRequest(node_name="ProbeA", parameter_name="does_not_exist", value="x")
        )
        assert set_result.failed()

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == ["Create node 'ProbeA'"]

    # ---------- Delete node restores lock state ----------

    def test_undo_delete_restores_lock_state(self, griptape_nodes: GriptapeNodes) -> None:
        """Undoing a delete restores the node's lock state captured at deletion time."""
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")
        assert isinstance(
            self._user_request(SetLockNodeStateRequest(node_name="ProbeA", lock=True)),
            SetLockNodeStateResultSuccess,
        )
        assert isinstance(self._user_request(DeleteNodeRequest(node_name="ProbeA")), DeleteNodeResultSuccess)

        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)
        restored = GriptapeNodes.ObjectManager().attempt_get_object_by_name_as_type("ProbeA", BaseNode)
        assert restored is not None
        assert restored.lock is True

    # ---------- SetParameterValue recorder gates (end to end) ----------

    def test_output_value_write_not_recorded(self, griptape_nodes: GriptapeNodes) -> None:
        """An is_output write is an internal write folded elsewhere; it is not recorded here."""
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")

        result = self._user_request(
            SetParameterValueRequest(node_name="ProbeA", parameter_name="text", value="out", is_output=True)
        )
        assert result.succeeded()

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == ["Create node 'ProbeA'"]

    def test_initial_setup_value_write_not_recorded(self, griptape_nodes: GriptapeNodes) -> None:
        """An initial_setup write (workflow load) is not a user edit, so it is not recorded."""
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")

        result = self._user_request(
            SetParameterValueRequest(node_name="ProbeA", parameter_name="text", value="loaded", initial_setup=True)
        )
        assert result.succeeded()

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == ["Create node 'ProbeA'"]

    # ---------- Delete connection re-propagates the source value ----------

    def test_undo_delete_connection_repropagates_source_value(self, griptape_nodes: GriptapeNodes) -> None:
        """Undoing a connection delete re-creates the edge and re-propagates the source value."""
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="Source")
        self._create_node(flow_name, node_name="Target")
        assert self._user_request(
            SetParameterValueRequest(node_name="Source", parameter_name="text", value="payload")
        ).succeeded()
        assert isinstance(
            self._user_request(
                CreateConnectionRequest(
                    source_node_name="Source",
                    source_parameter_name="text",
                    target_node_name="Target",
                    target_parameter_name="text",
                )
            ),
            CreateConnectionResultSuccess,
        )
        assert isinstance(
            self._user_request(
                DeleteConnectionRequest(
                    source_node_name="Source",
                    source_parameter_name="text",
                    target_node_name="Target",
                    target_parameter_name="text",
                )
            ),
            DeleteConnectionResultSuccess,
        )

        undo_result = GriptapeNodes.handle_request(UndoRequest())
        assert isinstance(undo_result, UndoResultSuccess)
        assert self._connection_exists("Target", "text")
        propagated = GriptapeNodes.handle_request(GetParameterValueRequest(node_name="Target", parameter_name="text"))
        assert isinstance(propagated, GetParameterValueResultSuccess)
        assert propagated.value == "payload"

    def test_delete_connection_records_disconnect_label(self, griptape_nodes: GriptapeNodes) -> None:
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="Source")
        self._create_node(flow_name, node_name="Target")
        assert isinstance(
            self._user_request(
                CreateConnectionRequest(
                    source_node_name="Source",
                    source_parameter_name="text",
                    target_node_name="Target",
                    target_parameter_name="text",
                )
            ),
            CreateConnectionResultSuccess,
        )
        self._user_request(
            DeleteConnectionRequest(
                source_node_name="Source",
                source_parameter_name="text",
                target_node_name="Target",
                target_parameter_name="text",
            )
        )

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels[-1] == "Disconnect 'Source.text' from 'Target.text'"

    # ---------- Recorder failure paths invalidate history (via monkeypatch) ----------

    def test_recorder_declined_invalidates_history(
        self, griptape_nodes: GriptapeNodes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When a recorder declines a mutation that did happen, history is invalidated."""
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")
        recorder = _registered_recorders(griptape_nodes.UndoManager())[CreateNodeRequest]
        monkeypatch.setattr(recorder, "capture_before", lambda request: RecorderCapture(declined=True))  # noqa: ARG005

        self._create_node(flow_name, node_name="ProbeB")
        assert GriptapeNodes.ObjectManager().has_object_with_name("ProbeB")

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == []

    def test_recorder_capture_exception_invalidates_history(
        self, griptape_nodes: GriptapeNodes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A recorder that raises during capture is treated as unrecordable and invalidates history."""
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")
        recorder = _registered_recorders(griptape_nodes.UndoManager())[CreateNodeRequest]

        def _boom(request: RequestPayload) -> RecorderCapture:  # noqa: ARG001
            msg = "capture failed"
            raise RuntimeError(msg)

        monkeypatch.setattr(recorder, "capture_before", _boom)

        self._create_node(flow_name, node_name="ProbeB")
        assert GriptapeNodes.ObjectManager().has_object_with_name("ProbeB")

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == []

    def test_recorder_create_batch_exception_invalidates_history(
        self, griptape_nodes: GriptapeNodes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A recorder that raises while building its batch invalidates history."""
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")
        recorder = _registered_recorders(griptape_nodes.UndoManager())[CreateNodeRequest]

        def _boom(request, result, state):  # noqa: ANN001, ANN202, ARG001
            msg = "batch failed"
            raise RuntimeError(msg)

        monkeypatch.setattr(recorder, "create_batch", _boom)

        self._create_node(flow_name, node_name="ProbeB")
        assert GriptapeNodes.ObjectManager().has_object_with_name("ProbeB")

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == []

    def test_recorder_returning_none_invalidates_history(
        self, griptape_nodes: GriptapeNodes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A recorder that returns None (cannot faithfully reverse) invalidates history."""
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")
        recorder = _registered_recorders(griptape_nodes.UndoManager())[CreateNodeRequest]
        monkeypatch.setattr(recorder, "create_batch", lambda request, result, state: None)  # noqa: ARG005

        self._create_node(flow_name, node_name="ProbeB")
        assert GriptapeNodes.ObjectManager().has_object_with_name("ProbeB")

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == []

    # ---------- Stack cap ----------

    def test_undo_stack_respects_max_batches(self, griptape_nodes: GriptapeNodes) -> None:
        """The undo stack keeps at most MAX_UNDO_BATCHES entries, dropping the oldest first."""
        undo_manager = griptape_nodes.UndoManager()
        for index in range(MAX_UNDO_BATCHES + 5):
            undo_manager._commit_batch(UndoBatch(label=f"batch-{index}", entries=[]))

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert len(state.undo_labels) == MAX_UNDO_BATCHES
        # The five oldest were dropped; the window now starts at batch-5.
        assert state.undo_labels[0] == "batch-5"
        assert state.undo_labels[-1] == f"batch-{MAX_UNDO_BATCHES + 4}"

    # ---------- Replay guard and unexpected-error handling ----------

    def test_undo_blocked_while_replay_in_progress(self, griptape_nodes: GriptapeNodes) -> None:
        """The reentrancy guard rejects an undo issued while a replay is already running."""
        undo_manager = griptape_nodes.UndoManager()
        undo_manager._commit_batch(UndoBatch(label="anything", entries=[]))
        undo_manager._is_replaying = True
        try:
            result = GriptapeNodes.handle_request(UndoRequest())
        finally:
            undo_manager._is_replaying = False
        assert isinstance(result, UndoResultFailure)

    def test_unexpected_replay_error_clears_history(self, griptape_nodes: GriptapeNodes) -> None:
        """An entry raising something other than UndoEntryReplayError clears history and fails typed."""
        undo_manager = griptape_nodes.UndoManager()
        undo_manager._commit_batch(UndoBatch(label="Booby trap", entries=[_RaisingUndoEntry()]))

        result = GriptapeNodes.handle_request(UndoRequest())
        assert isinstance(result, UndoResultFailure)
        assert not undo_manager._is_replaying
        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == []
        assert state.redo_labels == []

    def test_dispatch_expecting_failure_clears_history(self, griptape_nodes: GriptapeNodes) -> None:
        """An entry whose replayed request fails raises UndoEntryReplayError and clears history."""
        undo_manager = griptape_nodes.UndoManager()
        undo_manager._commit_batch(UndoBatch(label="Bad replay", entries=[_DispatchExpectingUndoEntry()]))

        result = GriptapeNodes.handle_request(UndoRequest())
        assert isinstance(result, UndoResultFailure)
        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == []

    def test_request_replay_entry_failure_clears_history(self, griptape_nodes: GriptapeNodes) -> None:
        """A RequestReplayUndoEntry whose stored request fails clears history on undo."""
        undo_manager = griptape_nodes.UndoManager()
        entry = RequestReplayUndoEntry(
            undo_requests=[DeleteNodeRequest(node_name="definitely-not-a-real-node")],
            redo_requests=[],
        )
        undo_manager._commit_batch(UndoBatch(label="Replay a doomed request", entries=[entry]))

        result = GriptapeNodes.handle_request(UndoRequest())
        assert isinstance(result, UndoResultFailure)
        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == []

    def test_record_inverse_uncopyable_value_leaves_history_untouched(self, griptape_nodes: GriptapeNodes) -> None:
        """An inverse carrying an un-deep-copyable value is not recorded and does not invalidate history."""
        self._ensure_record_inverse_probe_handler()
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")

        result = self._user_request(
            _RecordInverseProbeRequest(
                node_name="ProbeA", old_value="before", new_value="after", uncopyable_inverse=True
            )
        )
        assert isinstance(result, _RecordInverseProbeResult)

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        # The create is still undoable; the un-snapshottable inverse neither recorded nor cleared it.
        assert state.undo_labels == ["Create node 'ProbeA'"]

    # ---------- Recorder unit branches (capture decline / no-op) ----------

    def test_create_node_recorder_declines_group_creation(self, griptape_nodes: GriptapeNodes) -> None:  # noqa: ARG002
        """Group creation carries side effects a plain delete cannot reverse, so the recorder declines."""
        recorder = CreateNodeRecorder()
        adopt = recorder.capture_before(CreateNodeRequest(node_type=self._NODE_TYPE, node_names_to_add=["a", "b"]))
        assert adopt.declined is True
        subflow = recorder.capture_before(CreateNodeRequest(node_type=self._NODE_TYPE, subflow_name="sub"))
        assert subflow.declined is True

    def test_create_node_recorder_declines_wrong_request_type(self, griptape_nodes: GriptapeNodes) -> None:  # noqa: ARG002
        recorder = CreateNodeRecorder()
        capture = recorder.capture_before(DeleteNodeRequest(node_name="x"))
        assert capture.declined is True

    def test_delete_node_recorder_declines_missing_node(self, griptape_nodes: GriptapeNodes) -> None:
        self._register_library()
        self._make_flow(griptape_nodes)
        recorder = DeleteNodeRecorder()
        capture = recorder.capture_before(DeleteNodeRequest(node_name="does-not-exist"))
        assert capture.declined is True

    def test_set_parameter_recorder_skips_incoming_connection_write(self, griptape_nodes: GriptapeNodes) -> None:  # noqa: ARG002
        """A write attributed to an incoming connection source is folded elsewhere, not recorded here."""
        recorder = SetParameterValueRecorder()
        capture = recorder.capture_before(
            SetParameterValueRequest(
                node_name="ProbeA",
                parameter_name="text",
                value="v",
                incoming_connection_source_node_name="Source",
                incoming_connection_source_parameter_name="text",
            )
        )
        assert capture.declined is False
        assert capture.state is None

    def test_connection_recorders_decline_wrong_request_type(self, griptape_nodes: GriptapeNodes) -> None:  # noqa: ARG002
        assert CreateConnectionRecorder().capture_before(DeleteNodeRequest(node_name="x")).declined is True
        assert DeleteConnectionRecorder().capture_before(DeleteNodeRequest(node_name="x")).declined is True

    def test_connection_recorder_no_op_when_endpoint_missing(self, griptape_nodes: GriptapeNodes) -> None:
        """A connection recorder records nothing (state None, not declined) when an endpoint is missing."""
        self._register_library()
        self._make_flow(griptape_nodes)
        capture = CreateConnectionRecorder().capture_before(
            CreateConnectionRequest(
                source_node_name="missing",
                source_parameter_name="text",
                target_node_name="also-missing",
                target_parameter_name="text",
            )
        )
        assert capture.declined is False
        assert capture.state is None

    # ---------- DeleteNodeUndoEntry restore-failure branches (engine-backed) ----------

    def _make_serialized_commands(self, *, lock: bool | None = None) -> SerializedNodeCommands:
        """Build a minimal SerializedNodeCommands for exercising DeleteNodeUndoEntry restore helpers."""
        lock_command = None
        if lock is not None:
            lock_command = SetLockNodeStateRequest(node_name="unused", lock=lock)
        return SerializedNodeCommands(
            create_node_command=CreateNodeRequest(node_type=self._NODE_TYPE),
            element_modification_commands=[],
            node_dependencies=NodeDependencies(),
            lock_node_command=lock_command,
        )

    def test_restore_incoming_connection_failure_raises(self, griptape_nodes: GriptapeNodes) -> None:
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="Target")
        entry = DeleteNodeUndoEntry(
            node_name="Target",
            serialized_node_commands=self._make_serialized_commands(),
            set_parameter_value_commands=[],
            unique_parameter_uuid_to_values={},
            incoming_connections=[
                IncomingConnection(source_node_name="ghost", source_parameter_name="text", target_parameter_name="text")
            ],
            outgoing_connections=[],
        )
        with pytest.raises(UndoEntryReplayError, match="incoming connection"):
            entry._restore_connections()

    def test_restore_outgoing_connection_failure_raises(self, griptape_nodes: GriptapeNodes) -> None:
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="Source")
        entry = DeleteNodeUndoEntry(
            node_name="Source",
            serialized_node_commands=self._make_serialized_commands(),
            set_parameter_value_commands=[],
            unique_parameter_uuid_to_values={},
            incoming_connections=[],
            outgoing_connections=[
                OutgoingConnection(source_parameter_name="text", target_node_name="ghost", target_parameter_name="text")
            ],
        )
        with pytest.raises(UndoEntryReplayError, match="outgoing connection"):
            entry._restore_connections()

    def test_restore_parameter_value_missing_uuid_raises(self, griptape_nodes: GriptapeNodes) -> None:
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")
        indirect = SerializedNodeCommands.IndirectSetParameterValueCommand(
            set_parameter_value_command=SetParameterValueRequest(node_name="ProbeA", parameter_name="text", value="v"),
            unique_value_uuid=SerializedNodeCommands.UniqueParameterValueUUID("missing"),
        )
        entry = DeleteNodeUndoEntry(
            node_name="ProbeA",
            serialized_node_commands=self._make_serialized_commands(),
            set_parameter_value_commands=[indirect],
            unique_parameter_uuid_to_values={},
            incoming_connections=[],
            outgoing_connections=[],
        )
        with pytest.raises(UndoEntryReplayError, match="missing from the captured unique values"):
            entry._restore_parameter_values()

    def test_restore_parameter_value_failed_set_raises(self, griptape_nodes: GriptapeNodes) -> None:
        self._register_library()
        self._make_flow(griptape_nodes)
        uuid = SerializedNodeCommands.UniqueParameterValueUUID("u1")
        indirect = SerializedNodeCommands.IndirectSetParameterValueCommand(
            set_parameter_value_command=SetParameterValueRequest(node_name="ghost", parameter_name="text", value="v"),
            unique_value_uuid=uuid,
        )
        entry = DeleteNodeUndoEntry(
            node_name="ghost",
            serialized_node_commands=self._make_serialized_commands(),
            set_parameter_value_commands=[indirect],
            unique_parameter_uuid_to_values={uuid: "v"},
            incoming_connections=[],
            outgoing_connections=[],
        )
        with pytest.raises(UndoEntryReplayError, match="Failed restoring the value"):
            entry._restore_parameter_values()

    def test_restore_lock_state_none_is_noop(self, griptape_nodes: GriptapeNodes) -> None:
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")
        entry = DeleteNodeUndoEntry(
            node_name="ProbeA",
            serialized_node_commands=self._make_serialized_commands(lock=None),
            set_parameter_value_commands=[],
            unique_parameter_uuid_to_values={},
            incoming_connections=[],
            outgoing_connections=[],
        )
        # No lock command captured: restoring lock state is a no-op and must not raise.
        entry._restore_lock_state()

    def test_restore_lock_state_failure_raises(self, griptape_nodes: GriptapeNodes) -> None:
        self._register_library()
        self._make_flow(griptape_nodes)
        entry = DeleteNodeUndoEntry(
            node_name="ghost",
            serialized_node_commands=self._make_serialized_commands(lock=True),
            set_parameter_value_commands=[],
            unique_parameter_uuid_to_values={},
            incoming_connections=[],
            outgoing_connections=[],
        )
        with pytest.raises(UndoEntryReplayError, match="lock state"):
            entry._restore_lock_state()

    # ---------- Redo replay guard + engine-backed capture no-ops ----------

    def test_redo_blocked_while_replay_in_progress(self, griptape_nodes: GriptapeNodes) -> None:
        undo_manager = griptape_nodes.UndoManager()
        undo_manager._redo_stack.append(UndoBatch(label="anything", entries=[]))
        undo_manager._is_replaying = True
        try:
            result = GriptapeNodes.handle_request(RedoRequest())
        finally:
            undo_manager._is_replaying = False
        assert isinstance(result, RedoResultFailure)

    def test_set_parameter_recorder_no_state_for_missing_parameter(self, griptape_nodes: GriptapeNodes) -> None:
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")
        capture = SetParameterValueRecorder().capture_before(
            SetParameterValueRequest(node_name="ProbeA", parameter_name="nope", value="v")
        )
        assert capture.declined is False
        assert capture.state is None

    def test_set_parameter_recorder_no_state_for_uncopyable_value(self, griptape_nodes: GriptapeNodes) -> None:
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")
        node = GriptapeNodes.ObjectManager().attempt_get_object_by_name_as_type("ProbeA", BaseNode)
        assert node is not None
        node.parameter_values["text"] = _Uncopyable()
        capture = SetParameterValueRecorder().capture_before(
            SetParameterValueRequest(node_name="ProbeA", parameter_name="text", value="v")
        )
        assert capture.state is None

    def test_create_connection_recorder_no_state_for_missing_param(self, griptape_nodes: GriptapeNodes) -> None:
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="Source")
        self._create_node(flow_name, node_name="Target")
        capture = CreateConnectionRecorder().capture_before(
            CreateConnectionRequest(
                source_node_name="Source",
                source_parameter_name="nope",
                target_node_name="Target",
                target_parameter_name="text",
            )
        )
        assert capture.declined is False
        assert capture.state is None

    # ---------- Current-context endpoint/node resolution ----------

    def test_delete_recorder_declines_when_no_current_node(self, griptape_nodes: GriptapeNodes) -> None:
        self._register_library()
        self._make_flow(griptape_nodes)
        assert not GriptapeNodes.ContextManager().has_current_node()
        capture = DeleteNodeRecorder().capture_before(DeleteNodeRequest(node_name=None))
        assert capture.declined is True

    def test_delete_recorder_resolves_current_node(self, griptape_nodes: GriptapeNodes) -> None:
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")
        node = GriptapeNodes.ObjectManager().attempt_get_object_by_name_as_type("ProbeA", BaseNode)
        assert node is not None
        with GriptapeNodes.ContextManager().node(node=node):
            capture = DeleteNodeRecorder().capture_before(DeleteNodeRequest(node_name=None))
        assert capture.declined is False
        assert isinstance(capture.state, DeleteNodeUndoEntry)
        assert capture.state.node_name == "ProbeA"

    def test_set_parameter_recorder_resolves_current_node(self, griptape_nodes: GriptapeNodes) -> None:
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")
        node = GriptapeNodes.ObjectManager().attempt_get_object_by_name_as_type("ProbeA", BaseNode)
        assert node is not None
        with GriptapeNodes.ContextManager().node(node=node):
            capture = SetParameterValueRecorder().capture_before(
                SetParameterValueRequest(node_name=None, parameter_name="text", value="v")
            )
        assert capture.state is not None
        assert capture.state.node_name == "ProbeA"

    def test_set_parameter_recorder_no_state_without_current_node(self, griptape_nodes: GriptapeNodes) -> None:
        self._register_library()
        self._make_flow(griptape_nodes)
        assert not GriptapeNodes.ContextManager().has_current_node()
        capture = SetParameterValueRecorder().capture_before(
            SetParameterValueRequest(node_name=None, parameter_name="text", value="v")
        )
        assert capture.state is None

    def test_resolve_node_uses_current_context(self, griptape_nodes: GriptapeNodes) -> None:
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")
        node = GriptapeNodes.ObjectManager().attempt_get_object_by_name_as_type("ProbeA", BaseNode)
        assert node is not None
        with GriptapeNodes.ContextManager().node(node=node):
            assert _resolve_node(None) is node

    def test_resolve_node_none_without_current_context(self, griptape_nodes: GriptapeNodes) -> None:
        self._register_library()
        self._make_flow(griptape_nodes)
        assert not GriptapeNodes.ContextManager().has_current_node()
        assert _resolve_node(None) is None

    def test_delete_connection_recorder_no_state_without_context(self, griptape_nodes: GriptapeNodes) -> None:
        self._register_library()
        self._make_flow(griptape_nodes)
        assert not GriptapeNodes.ContextManager().has_current_node()
        capture = DeleteConnectionRecorder().capture_before(
            DeleteConnectionRequest(source_parameter_name="out", target_parameter_name="in")
        )
        assert capture.state is None

    def test_restore_parameter_value_uncopyable_value_falls_back_to_reference(
        self, griptape_nodes: GriptapeNodes
    ) -> None:
        """A captured value that cannot be deep-copied is restored by reference rather than raising."""
        self._register_library()
        flow_name = self._make_flow(griptape_nodes)
        self._create_node(flow_name, node_name="ProbeA")
        uuid = SerializedNodeCommands.UniqueParameterValueUUID("u1")
        uncopyable = _Uncopyable()
        indirect = SerializedNodeCommands.IndirectSetParameterValueCommand(
            set_parameter_value_command=SetParameterValueRequest(node_name="ProbeA", parameter_name="text", value=None),
            unique_value_uuid=uuid,
        )
        entry = DeleteNodeUndoEntry(
            node_name="ProbeA",
            serialized_node_commands=self._make_serialized_commands(),
            set_parameter_value_commands=[indirect],
            unique_parameter_uuid_to_values={uuid: uncopyable},
            incoming_connections=[],
            outgoing_connections=[],
        )
        # The deep-copy of the value raises and is swallowed; the same object is restored by reference.
        entry._restore_parameter_values()
        node = GriptapeNodes.ObjectManager().attempt_get_object_by_name_as_type("ProbeA", BaseNode)
        assert node is not None
        assert node.get_parameter_value("text") is uncopyable


class TestUndoCore:
    """Unit tests for the domain-agnostic replay vocabulary in undo.core."""

    @pytest.fixture(autouse=True)
    def _reset_undo_history(self, griptape_nodes: GriptapeNodes):  # noqa: ANN202, ARG002
        GriptapeNodes.UndoManager().clear_history()
        yield
        GriptapeNodes.UndoManager().clear_history()

    def test_dispatch_expecting_success_returns_result(self, griptape_nodes: GriptapeNodes) -> None:  # noqa: ARG002
        result = dispatch_expecting_success(GetUndoStateRequest(), "read undo state")
        assert isinstance(result, GetUndoStateResultSuccess)

    def test_dispatch_expecting_success_raises_on_failure(self, griptape_nodes: GriptapeNodes) -> None:  # noqa: ARG002
        with pytest.raises(UndoEntryReplayError, match="delete a missing node"):
            dispatch_expecting_success(DeleteNodeRequest(node_name="nope"), "delete a missing node")

    def test_dispatch_expecting_returns_typed_result(self, griptape_nodes: GriptapeNodes) -> None:  # noqa: ARG002
        result = dispatch_expecting(GetUndoStateRequest(), GetUndoStateResultSuccess, "read undo state")
        assert isinstance(result, GetUndoStateResultSuccess)

    def test_dispatch_expecting_raises_on_wrong_result_type(self, griptape_nodes: GriptapeNodes) -> None:  # noqa: ARG002
        # The delete fails, so the result is not the expected DeleteNodeResultSuccess.
        with pytest.raises(UndoEntryReplayError, match="delete a missing node"):
            dispatch_expecting(DeleteNodeRequest(node_name="nope"), DeleteNodeResultSuccess, "delete a missing node")

    def test_request_replay_entry_replays_in_both_directions(self, griptape_nodes: GriptapeNodes) -> None:  # noqa: ARG002
        # GetUndoStateRequest always succeeds, so undo/redo replay without raising.
        entry = RequestReplayUndoEntry(
            undo_requests=[GetUndoStateRequest()],
            redo_requests=[GetUndoStateRequest()],
        )
        entry.undo()
        entry.redo()

    def test_request_replay_entry_raises_when_replayed_request_fails(self) -> None:
        entry = RequestReplayUndoEntry(
            undo_requests=[DeleteNodeRequest(node_name="nope")],
            redo_requests=[],
        )
        with pytest.raises(UndoEntryReplayError):
            entry.undo()

    def test_recorder_capture_defaults(self) -> None:
        capture = RecorderCapture()
        assert capture.declined is False
        assert capture.state is None

    def test_undo_batch_holds_label_and_entries(self) -> None:
        entry = RequestReplayUndoEntry(undo_requests=[], redo_requests=[])
        batch = UndoBatch(label="Some action", entries=[entry])
        assert batch.label == "Some action"
        assert batch.entries == [entry]


class TestRecordingSessionUnit:
    """Unit tests for RecordingSession helpers and framing, driven without the EventManager."""

    @staticmethod
    def _make_session() -> tuple[RecordingSession, list[UndoBatch], list[bool]]:
        committed: list[UndoBatch] = []
        invalidated: list[bool] = []
        session = RecordingSession(
            is_replaying=lambda: False,
            commit_batch=committed.append,
            invalidate_history=lambda: invalidated.append(True),
        )
        return session, committed, invalidated

    def test_as_sequence_wraps_single_and_passes_through_collections(self) -> None:
        request = GetUndoStateRequest()
        assert _as_sequence(request) == [request]
        assert _as_sequence([request, request]) == [request, request]
        assert _as_sequence((request,)) == [request]

    def test_prepare_replay_request_strips_id_and_copies(self) -> None:
        request = SetParameterValueRequest(node_name="N", parameter_name="text", value="v", request_id="abc")
        clone = _prepare_replay_request(request)
        assert clone is not request
        assert clone.request_id is None
        # The original is left untouched so the live request keeps its id.
        assert request.request_id == "abc"
        assert isinstance(clone, SetParameterValueRequest)
        assert clone.value == "v"

    def test_register_recorder_rejects_duplicate(self) -> None:
        session, _, _ = self._make_session()
        session.register_recorder(CreateNodeRequest, CreateNodeRecorder())
        with pytest.raises(ValueError, match="already registered"):
            session.register_recorder(CreateNodeRequest, CreateNodeRecorder())

    def test_contribute_to_frame_none_is_noop(self) -> None:
        session, committed, invalidated = self._make_session()
        capture = DispatchCapture(
            request=GetUndoStateRequest(),
            opened_frame=False,
            records=True,
            recorder=None,
            before_state=None,
            declined=False,
        )
        session._contribute_to_frame(
            None, capture, GetUndoStateRequest(), CreateConnectionResultSuccess(result_details="x")
        )
        assert committed == []
        assert invalidated == []

    def test_finalize_frame_none_is_noop(self) -> None:
        session, committed, invalidated = self._make_session()
        session._finalize_frame(None)
        assert committed == []
        assert invalidated == []

    def test_record_inverse_noop_in_nested_dispatch(self) -> None:
        """record_inverse called from a dispatch nested inside a recording one is a no-op."""
        session, _, _ = self._make_session()
        # Outer user-initiated dispatch opens the frame and records.
        session.begin_request_dispatch(CreateNodeRequest(node_type="x"), request_id="rid")
        # Nested internal dispatch (no request_id) does not record; the ancestor owns the reversal.
        session.begin_request_dispatch(CreateNodeRequest(node_type="y"), request_id=None)

        session.record_inverse(
            SetParameterValueRequest(node_name="N", parameter_name="text", value="v"),
            label="should not record",
        )

        assert session._active_frame is not None
        assert session._active_frame.entries == []

    def test_clear_history_request_inside_frame_invalidates(self) -> None:
        """A history-clearing lifecycle request seen while a frame is open invalidates that frame.

        The clear-history dispatch wipes the stacks immediately; without also flagging the open
        frame, finalizing the opening dispatch would re-commit a now-untrustworthy batch onto the
        just-cleared stack. Mirrors SnapshotRecordingSession's fail-closed handling.
        """
        session, committed, invalidated = self._make_session()
        outer = session.begin_request_dispatch(CreateNodeRequest(node_type="x"), request_id="rid")
        # A history-clearing lifecycle request cascades in mid-frame.
        inner = session.begin_request_dispatch(SetWorkflowContextRequest(workflow_name="w"), request_id=None)
        assert inner is None
        assert invalidated == [True]
        assert session._active_frame is not None
        assert session._active_frame.invalidate is True
        # Finalizing the opening dispatch must not commit onto the just-cleared stack.
        session.end_request_dispatch(
            outer, CreateNodeRequest(node_type="x"), CreateConnectionResultSuccess(result_details="ok")
        )
        assert committed == []

    def test_uncovered_handler_raise_fails_closed(self) -> None:
        """An uncovered request type whose handler raised mid-frame invalidates history (fail closed)."""
        session, committed, invalidated = self._make_session()
        capture = session.begin_request_dispatch(CreateNodeRequest(node_type="x"), request_id="rid")
        # result is None => handler raised; no recorder, no recorded inverse, not declared non-undoable.
        session.end_request_dispatch(capture, CreateNodeRequest(node_type="x"), None)
        assert committed == []
        assert invalidated == [True]

    def test_non_undoable_handler_raise_does_not_invalidate(self) -> None:
        """A declared-non-undoable request that raised leaves history intact (never affects it, by contract)."""
        session, committed, invalidated = self._make_session()
        session.register_non_undoable(RenameObjectRequest)
        capture = session.begin_request_dispatch(
            RenameObjectRequest(object_name="a", requested_name="b"), request_id="rid"
        )
        session.end_request_dispatch(capture, RenameObjectRequest(object_name="a", requested_name="b"), None)
        assert committed == []
        assert invalidated == []

    def test_triage_dispatch_classifies_by_lifecycle_policy(self) -> None:
        """triage_dispatch centralizes the ignore / clear-history / proceed policy shared by both strategies."""
        # Own events are ignored (never recorded, never clear history).
        assert triage_dispatch(GetUndoStateRequest(), is_replaying=False) is DispatchTriage.IGNORE
        # Replay is isolated before the clear-history check: a lifecycle type dispatched during a
        # replay is ignored, not treated as history-clearing.
        assert triage_dispatch(SetWorkflowContextRequest(workflow_name="w"), is_replaying=True) is DispatchTriage.IGNORE
        # A history-clearing lifecycle request clears when not replaying.
        assert (
            triage_dispatch(SetWorkflowContextRequest(workflow_name="w"), is_replaying=False)
            is DispatchTriage.CLEAR_HISTORY
        )
        # An ordinary edit proceeds to strategy-specific framing.
        assert triage_dispatch(CreateNodeRequest(node_type="x"), is_replaying=False) is DispatchTriage.PROCEED


class TestNodeRecorderUnit:
    """Unit tests for node recorders and their equality/no-op helpers, without engine state."""

    def test_values_equal_true_and_false(self) -> None:
        assert _values_equal("a", "a") is True
        assert _values_equal("a", "b") is False
        assert _values_equal(1, 1) is True

    def test_values_equal_treats_raising_comparison_as_not_equal(self) -> None:
        class _Raises:
            def __eq__(self, other: object) -> bool:
                msg = "no comparison"
                raise RuntimeError(msg)

            def __hash__(self) -> int:
                return 0

        assert _values_equal(_Raises(), _Raises()) is False

    def test_values_equal_treats_non_bool_comparison_as_not_equal(self) -> None:
        class _ArrayLike:
            def __eq__(self, other: object) -> list[bool]:  # type: ignore[override]
                return [False, True]

            def __hash__(self) -> int:
                return 0

        assert _values_equal(_ArrayLike(), _ArrayLike()) is False

    def test_create_node_recorder_declines_wrong_type(self) -> None:
        assert CreateNodeRecorder().capture_before(DeleteNodeRequest(node_name="x")).declined is True

    def test_create_node_recorder_declines_group_creation(self) -> None:
        recorder = CreateNodeRecorder()
        assert recorder.capture_before(CreateNodeRequest(node_type="T", node_names_to_add=["a"])).declined is True
        assert recorder.capture_before(CreateNodeRequest(node_type="T", subflow_name="sub")).declined is True

    def test_create_node_recorder_captures_request_copy(self) -> None:
        request = CreateNodeRequest(node_type="T", node_name="orig")
        capture = CreateNodeRecorder().capture_before(request)
        assert capture.declined is False
        assert isinstance(capture.state, CreateNodeRequest)
        assert capture.state is not request

    def test_create_node_recorder_create_batch_wrong_result_returns_none(self) -> None:
        batch = CreateNodeRecorder().create_batch(
            CreateNodeRequest(node_type="T"),
            CreateConnectionResultSuccess(result_details="x"),
            CreateNodeRequest(node_type="T"),
        )
        assert batch is None

    def test_create_node_recorder_create_batch_pins_name_and_flow(self) -> None:
        state = CreateNodeRequest(node_type="T", node_name=None, override_parent_flow_name=None)
        result = CreateNodeResultSuccess(
            node_name="Assigned", node_type="T", parent_flow_name="Flow1", result_details="x"
        )
        batch = CreateNodeRecorder().create_batch(CreateNodeRequest(node_type="T"), result, state)
        assert batch is not None
        assert batch.label == "Create node 'Assigned'"
        entry = batch.entries[0]
        assert isinstance(entry, CreateNodeUndoEntry)
        assert entry.node_name == "Assigned"
        assert entry.create_request.node_name == "Assigned"
        assert entry.create_request.override_parent_flow_name == "Flow1"
        assert entry.create_request.set_as_new_context is False
        assert entry.create_request.request_id is None

    def test_delete_node_recorder_declines_wrong_type(self) -> None:
        assert DeleteNodeRecorder().capture_before(CreateNodeRequest(node_type="T")).declined is True

    def test_set_parameter_recorder_declines_wrong_type(self) -> None:
        assert SetParameterValueRecorder().capture_before(DeleteNodeRequest(node_name="x")).declined is True

    def test_set_parameter_recorder_no_state_for_missing_node(self, griptape_nodes: GriptapeNodes) -> None:  # noqa: ARG002
        capture = SetParameterValueRecorder().capture_before(
            SetParameterValueRequest(node_name="ghost", parameter_name="text", value="v")
        )
        assert capture.declined is False
        assert capture.state is None

    def test_set_parameter_recorder_folds_internal_writes(self) -> None:
        recorder = SetParameterValueRecorder()
        for request in (
            SetParameterValueRequest(node_name="N", parameter_name="p", value="v", is_output=True),
            SetParameterValueRequest(node_name="N", parameter_name="p", value="v", initial_setup=True),
            SetParameterValueRequest(
                node_name="N",
                parameter_name="p",
                value="v",
                incoming_connection_source_node_name="S",
                incoming_connection_source_parameter_name="o",
            ),
        ):
            capture = recorder.capture_before(request)
            assert capture.declined is False
            assert capture.state is None

    def test_set_parameter_recorder_create_batch_no_state_is_empty(self) -> None:
        batch = SetParameterValueRecorder().create_batch(
            SetParameterValueRequest(node_name="N", parameter_name="p", value="v"),
            SetParameterValueResultSuccess(finalized_value="v", data_type="str", result_details="x"),
            None,
        )
        assert batch is not None
        assert batch.entries == []

    def test_set_parameter_recorder_create_batch_noop_when_unchanged(self) -> None:
        capture = _ParameterEditCapture(node_name="N", parameter_name="text", data_type="str", old_value="same")
        batch = SetParameterValueRecorder().create_batch(
            SetParameterValueRequest(node_name="N", parameter_name="text", value="same"),
            SetParameterValueResultSuccess(finalized_value="same", data_type="str", result_details="x"),
            capture,
        )
        assert batch is not None
        assert batch.entries == []

    def test_set_parameter_recorder_create_batch_records_edit(self) -> None:
        capture = _ParameterEditCapture(node_name="N", parameter_name="text", data_type="str", old_value="old")
        batch = SetParameterValueRecorder().create_batch(
            SetParameterValueRequest(node_name="N", parameter_name="text", value="new"),
            SetParameterValueResultSuccess(finalized_value="new", data_type="str", result_details="x"),
            capture,
        )
        assert batch is not None
        assert batch.label == "Set 'N.text'"
        entry = batch.entries[0]
        assert isinstance(entry, RequestReplayUndoEntry)
        undo_request = entry.undo_requests[0]
        redo_request = entry.redo_requests[0]
        assert isinstance(undo_request, SetParameterValueRequest)
        assert isinstance(redo_request, SetParameterValueRequest)
        assert undo_request.value == "old"
        assert redo_request.value == "new"


class TestConnectionRecorderUnit:
    """Unit tests for connection recorders' batch construction, without engine state."""

    @staticmethod
    def _endpoints(**overrides: object) -> _ConnectionEndpoints:
        fields: dict[str, object] = {
            "source_node_name": "S",
            "source_parameter_name": "out",
            "target_node_name": "T",
            "target_parameter_name": "in",
        }
        fields.update(overrides)
        return _ConnectionEndpoints(**fields)  # type: ignore[arg-type]

    def test_create_connection_recorder_declines_wrong_type(self) -> None:
        assert CreateConnectionRecorder().capture_before(DeleteNodeRequest(node_name="x")).declined is True

    def test_create_connection_recorder_create_batch_empty_without_state_or_result(self) -> None:
        recorder = CreateConnectionRecorder()
        request = CreateConnectionRequest(
            source_node_name="S", source_parameter_name="out", target_node_name="T", target_parameter_name="in"
        )
        assert recorder.create_batch(request, CreateConnectionResultSuccess(result_details="x"), None).entries == []
        assert (
            recorder.create_batch(request, DeleteConnectionResultSuccess(result_details="x"), self._endpoints()).entries
            == []
        )

    def test_create_connection_recorder_create_batch_detach_only(self) -> None:
        batch = CreateConnectionRecorder().create_batch(
            CreateConnectionRequest(
                source_node_name="S", source_parameter_name="out", target_node_name="T", target_parameter_name="in"
            ),
            CreateConnectionResultSuccess(result_details="x"),
            self._endpoints(),
        )
        assert batch.label == "Connect 'S.out' to 'T.in'"
        entry = batch.entries[0]
        assert isinstance(entry, RequestReplayUndoEntry)
        assert len(entry.undo_requests) == 1
        assert isinstance(entry.undo_requests[0], DeleteConnectionRequest)
        assert isinstance(entry.redo_requests[0], CreateConnectionRequest)

    def test_create_connection_recorder_create_batch_restores_overwritten_value(self) -> None:
        endpoints = self._endpoints(target_data_type="str", restore_target_value=True, target_restore_value="prior")
        batch = CreateConnectionRecorder().create_batch(
            CreateConnectionRequest(
                source_node_name="S", source_parameter_name="out", target_node_name="T", target_parameter_name="in"
            ),
            CreateConnectionResultSuccess(result_details="x"),
            endpoints,
        )
        entry = batch.entries[0]
        assert isinstance(entry, RequestReplayUndoEntry)
        assert [type(request) for request in entry.undo_requests] == [
            DeleteConnectionRequest,
            SetParameterValueRequest,
        ]
        restore = entry.undo_requests[1]
        assert isinstance(restore, SetParameterValueRequest)
        assert restore.value == "prior"
        assert restore.data_type == "str"

    def test_delete_connection_recorder_declines_wrong_type(self) -> None:
        assert DeleteConnectionRecorder().capture_before(DeleteNodeRequest(node_name="x")).declined is True

    def test_delete_connection_recorder_create_batch_empty_without_state_or_result(self) -> None:
        recorder = DeleteConnectionRecorder()
        request = DeleteConnectionRequest(
            source_node_name="S", source_parameter_name="out", target_node_name="T", target_parameter_name="in"
        )
        assert recorder.create_batch(request, DeleteConnectionResultSuccess(result_details="x"), None).entries == []
        assert (
            recorder.create_batch(request, CreateConnectionResultSuccess(result_details="x"), self._endpoints()).entries
            == []
        )

    def test_delete_connection_recorder_create_batch_recreates_edge(self) -> None:
        batch = DeleteConnectionRecorder().create_batch(
            DeleteConnectionRequest(
                source_node_name="S", source_parameter_name="out", target_node_name="T", target_parameter_name="in"
            ),
            DeleteConnectionResultSuccess(result_details="x"),
            self._endpoints(),
        )
        assert batch.label == "Disconnect 'S.out' from 'T.in'"
        entry = batch.entries[0]
        assert isinstance(entry, RequestReplayUndoEntry)
        assert isinstance(entry.undo_requests[0], CreateConnectionRequest)
        assert isinstance(entry.redo_requests[0], DeleteConnectionRequest)

    def test_capture_prior_target_value_skips_non_property_target(self) -> None:
        """A non-PROPERTY target is wiped on disconnect, so there is no prior value to restore."""
        node = _ProbeNode(name="T")
        source_param = node.get_parameter_by_name("text")
        assert source_param is not None
        target_param = Parameter(
            name="inp",
            tooltip="",
            type=ParameterTypeBuiltin.STR.value,
            allowed_modes={ParameterMode.INPUT},
        )
        endpoints = _ConnectionEndpoints(
            source_node_name="S", source_parameter_name="text", target_node_name="T", target_parameter_name="inp"
        )
        CreateConnectionRecorder._capture_prior_target_value(
            CreateConnectionRequest(
                source_node_name="S", source_parameter_name="text", target_node_name="T", target_parameter_name="inp"
            ),
            source_param,
            node,
            target_param,
            endpoints,
        )
        assert endpoints.restore_target_value is False

    def test_capture_prior_target_value_handles_uncopyable_value(self) -> None:
        """A prior value that cannot be deep-copied is simply not restored on undo."""
        node = _ProbeNode(name="T")
        node.parameter_values["text"] = _Uncopyable()
        param = node.get_parameter_by_name("text")
        assert param is not None
        endpoints = _ConnectionEndpoints(
            source_node_name="S", source_parameter_name="text", target_node_name="T", target_parameter_name="text"
        )
        CreateConnectionRecorder._capture_prior_target_value(
            CreateConnectionRequest(
                source_node_name="S", source_parameter_name="text", target_node_name="T", target_parameter_name="text"
            ),
            param,
            node,
            param,
            endpoints,
        )
        assert endpoints.restore_target_value is False
