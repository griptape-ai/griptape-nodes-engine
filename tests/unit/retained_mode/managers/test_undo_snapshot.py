"""Tests for the whole-flow snapshot undo strategy.

These build a fresh engine so the UndoManager wires the SnapshotRecordingSession
(the default and only strategy), then drive user-request flows and verify the snapshot round trip:
undo reconciles the flow back to the previous whole-flow state; redo re-applies it.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode, ParameterTypeBuiltin
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.retained_mode.engine import current_engine
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
    DeleteConnectionRequest,
)
from griptape_nodes.retained_mode.events.context_events import SetWorkflowContextRequest
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    SerializeFlowToCommandsRequest,
    SerializeFlowToCommandsResultFailure,
    SetFlowMetadataRequest,
)
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    CreateNodeResultSuccess,
    DeleteNodeRequest,
    DeleteNodeResultFailure,
    SetLockNodeStateRequest,
    SetNodeMetadataRequest,
    SetNodeMetadataResultSuccess,
)
from griptape_nodes.retained_mode.events.object_events import RenameObjectRequest
from griptape_nodes.retained_mode.events.parameter_events import (
    GetParameterValueRequest,
    GetParameterValueResultSuccess,
    SetParameterValueRequest,
)
from griptape_nodes.retained_mode.events.undo_events import (
    GetUndoStateRequest,
    GetUndoStateResultSuccess,
    RedoRequest,
    RedoResultSuccess,
    UndoRequest,
    UndoResultFailure,
    UndoResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.undo.snapshot import (
    SnapshotRecordingSession,
    _differing_keys,
    capture_workflow_snapshot,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload


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
        # A parameter whose default is None and which starts unset, so serialization records no
        # value command for it until it is explicitly set (exercises the reconcile clear path).
        self.add_parameter(
            Parameter(
                name="opt",
                tooltip="optional value",
                type=ParameterTypeBuiltin.STR.value,
                allowed_modes={ParameterMode.PROPERTY, ParameterMode.INPUT, ParameterMode.OUTPUT},
                default_value=None,
            )
        )

    def process(self) -> None:
        return None


class TestSnapshotStrategy:
    _LIBRARY_NAME = "undo-snapshot-test-library"
    _NODE_TYPE = "_ProbeNode"

    @pytest.fixture
    def snapshot_engine(self) -> Iterator[Engine]:
        """A fresh engine with the undo-snapshot test library registered.

        Confirms the UndoManager wires the (default) snapshot session.
        """
        from griptape_nodes.node_library.library_registry import LibraryRegistry

        LibraryRegistry._clear()
        engine = current_engine()
        assert isinstance(engine.undo_manager._recording, SnapshotRecordingSession)
        self._register_library()
        yield engine
        LibraryRegistry._clear()

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
                author="t", description="d", library_version="1.0.0", engine_version="1.0.0", tags=[]
            ),
            categories=[],
            nodes=[],
        )
        library = LibraryRegistry.generate_new_library(library_data=schema)
        library.register_new_node_type(_ProbeNode, NodeMetadata(category="t", description="d", display_name="Probe"))

    def _make_flow(self, griptape_nodes: Engine) -> str:
        from griptape_nodes.node_library.workflow_registry import WorkflowRegistry
        from griptape_nodes.retained_mode.managers.workflow_manager import WorkflowMetadata

        context_manager = griptape_nodes.ContextManager()
        if not context_manager.has_current_workflow():
            workflow_key = "unsaved:undo-snapshot-test"
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
        event_manager = GriptapeNodes.EventManager()
        return event_manager.handle_request(request, result_context={"request_id": request_id}).result

    def _create_node(self, flow_name: str, node_name: str | None = None) -> str:
        result = self._user_request(
            CreateNodeRequest(
                node_type=self._NODE_TYPE,
                specific_library_name=self._LIBRARY_NAME,
                node_name=node_name,
                override_parent_flow_name=flow_name,
            )
        )
        assert isinstance(result, CreateNodeResultSuccess)
        return result.node_name

    @staticmethod
    def _patch_handler(
        monkeypatch: pytest.MonkeyPatch,
        request_type: type[RequestPayload],
        handler: Callable[[RequestPayload], ResultPayload],
    ) -> None:
        """Replace the EventManager's dispatch-table entry for request_type with a stand-in handler.

        FlowManager/NodeManager hand `assign_manager_to_request_type` their already-bound method at
        construction time, so monkeypatching the manager's method attribute afterward is a no-op: the
        dispatch table holds a fixed reference to that original bound method, not a live attribute
        lookup. The dispatch-table entry itself is looked up fresh per dispatch, so replacing it here
        is the only way to observe a stand-in handler.
        """
        monkeypatch.setitem(GriptapeNodes.EventManager()._request_type_to_manager, request_type, handler)

    # ---------- End-to-end via the snapshot session ----------

    def test_undo_restores_prior_whole_flow_state(self, snapshot_engine: Engine) -> None:
        flow_name = self._make_flow(snapshot_engine)
        self._create_node(flow_name, node_name="ProbeA")
        self._create_node(flow_name, node_name="ProbeB")

        # Undo the second creation: the flow is restored to the snapshot taken before it.
        undo_result = GriptapeNodes.handle_request(UndoRequest())
        assert isinstance(undo_result, UndoResultSuccess)
        assert GriptapeNodes.ObjectManager().has_object_with_name("ProbeA")
        assert not GriptapeNodes.ObjectManager().has_object_with_name("ProbeB")

        # Redo re-applies the after-snapshot.
        redo_result = GriptapeNodes.handle_request(RedoRequest())
        assert isinstance(redo_result, RedoResultSuccess)
        assert GriptapeNodes.ObjectManager().has_object_with_name("ProbeA")
        assert GriptapeNodes.ObjectManager().has_object_with_name("ProbeB")

    def test_undo_restores_parameter_value(self, snapshot_engine: Engine) -> None:
        flow_name = self._make_flow(snapshot_engine)
        self._create_node(flow_name, node_name="ProbeA")
        assert self._user_request(
            SetParameterValueRequest(node_name="ProbeA", parameter_name="text", value="hello")
        ).succeeded()

        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)
        value_result = GriptapeNodes.handle_request(GetParameterValueRequest(node_name="ProbeA", parameter_name="text"))
        assert isinstance(value_result, GetParameterValueResultSuccess)
        assert value_result.value == ""

    def test_undo_restores_connection(self, snapshot_engine: Engine) -> None:
        flow_name = self._make_flow(snapshot_engine)
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

        def connected() -> bool:
            incoming = GriptapeNodes.FlowManager().get_connections().incoming_index.get("Target", {})
            return "text" in incoming

        assert connected()
        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)
        assert not connected()

    def test_transaction_groups_into_one_snapshot(self, snapshot_engine: Engine) -> None:
        flow_name = self._make_flow(snapshot_engine)
        undo_manager = GriptapeNodes.UndoManager()

        with undo_manager.transaction("Add pair"):
            snapshot_engine.handle_request(
                CreateNodeRequest(
                    node_type=self._NODE_TYPE,
                    specific_library_name=self._LIBRARY_NAME,
                    node_name="Pair1",
                    override_parent_flow_name=flow_name,
                )
            )
            snapshot_engine.handle_request(
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

        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)
        assert not GriptapeNodes.ObjectManager().has_object_with_name("Pair1")
        assert not GriptapeNodes.ObjectManager().has_object_with_name("Pair2")

    # ---------- Reconcile (surgical restore, not teardown/rebuild) ----------

    def test_undo_value_edit_reconciles_survivors_in_place(self, snapshot_engine: Engine) -> None:
        """Undoing a value edit updates only the changed node; survivors keep their instance identity."""
        flow_name = self._make_flow(snapshot_engine)
        self._create_node(flow_name, node_name="ProbeA")
        self._create_node(flow_name, node_name="ProbeB")
        assert self._user_request(
            SetParameterValueRequest(node_name="ProbeB", parameter_name="text", value="hello")
        ).succeeded()

        obj = GriptapeNodes.ObjectManager()
        node_a_before = obj.attempt_get_object_by_name_as_type("ProbeA", BaseNode)
        node_b_before = obj.attempt_get_object_by_name_as_type("ProbeB", BaseNode)

        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)

        # Neither node was deleted/recreated: the restore reconciled in place (no teardown blink).
        assert obj.attempt_get_object_by_name_as_type("ProbeA", BaseNode) is node_a_before
        assert obj.attempt_get_object_by_name_as_type("ProbeB", BaseNode) is node_b_before
        value_result = GriptapeNodes.handle_request(GetParameterValueRequest(node_name="ProbeB", parameter_name="text"))
        assert isinstance(value_result, GetParameterValueResultSuccess)
        assert value_result.value == ""

    def test_undo_delete_recreates_only_changed_node(self, snapshot_engine: Engine) -> None:
        """Undoing a delete recreates the removed node while leaving untouched survivors intact."""
        flow_name = self._make_flow(snapshot_engine)
        self._create_node(flow_name, node_name="ProbeA")
        self._create_node(flow_name, node_name="ProbeB")

        obj = GriptapeNodes.ObjectManager()
        node_a_before = obj.attempt_get_object_by_name_as_type("ProbeA", BaseNode)

        assert self._user_request(DeleteNodeRequest(node_name="ProbeB")).succeeded()
        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)

        # A was never touched (same instance); only B was recreated.
        assert obj.attempt_get_object_by_name_as_type("ProbeA", BaseNode) is node_a_before
        assert obj.attempt_get_object_by_name_as_type("ProbeB", BaseNode) is not None

    # ---------- Editor mutations that a snapshot captures as their own steps ----------

    def test_node_move_is_its_own_undo_step(self, snapshot_engine: Engine) -> None:
        """A node move is captured as its own snapshot step and is undoable.

        A move only changes node metadata, so it is easy to mistake for something the undo system can
        skip. Skipping it means moves cannot be undone, and an unrelated undo silently discards them.
        """
        flow_name = self._make_flow(snapshot_engine)
        create_result = self._user_request(
            CreateNodeRequest(
                node_type=self._NODE_TYPE,
                specific_library_name=self._LIBRARY_NAME,
                node_name="ProbeA",
                override_parent_flow_name=flow_name,
                metadata={"position": {"x": 10, "y": 20}},
            )
        )
        assert isinstance(create_result, CreateNodeResultSuccess)
        obj = GriptapeNodes.ObjectManager()
        node = obj.attempt_get_object_by_name_as_type("ProbeA", BaseNode)
        assert node is not None
        original_position = copy.deepcopy(node.metadata.get("position"))

        moved_metadata = {**copy.deepcopy(node.metadata), "position": {"x": 500, "y": 600}}
        assert self._user_request(SetNodeMetadataRequest(node_name="ProbeA", metadata=moved_metadata)).succeeded()
        assert node.metadata.get("position") == {"x": 500, "y": 600}

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert len(state.undo_labels) == 2  # create + move  # noqa: PLR2004

        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)
        node_after = obj.attempt_get_object_by_name_as_type("ProbeA", BaseNode)
        assert node_after is not None
        assert node_after.metadata.get("position") == original_position

    def test_undo_edit_preserves_an_earlier_move(self, snapshot_engine: Engine) -> None:
        """Undoing a value edit reverts only the edit; an earlier move stays applied."""
        flow_name = self._make_flow(snapshot_engine)
        self._create_node(flow_name, node_name="ProbeA")
        obj = GriptapeNodes.ObjectManager()
        node = obj.attempt_get_object_by_name_as_type("ProbeA", BaseNode)
        assert node is not None

        moved_metadata = {**copy.deepcopy(node.metadata), "position": {"x": 500, "y": 600}}
        assert self._user_request(SetNodeMetadataRequest(node_name="ProbeA", metadata=moved_metadata)).succeeded()
        assert self._user_request(
            SetParameterValueRequest(node_name="ProbeA", parameter_name="text", value="hello")
        ).succeeded()

        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)
        node_after = obj.attempt_get_object_by_name_as_type("ProbeA", BaseNode)
        assert node_after is not None
        assert node_after.metadata.get("position") == {"x": 500, "y": 600}
        value_result = GriptapeNodes.handle_request(GetParameterValueRequest(node_name="ProbeA", parameter_name="text"))
        assert isinstance(value_result, GetParameterValueResultSuccess)
        assert value_result.value == ""

    def test_undo_clears_first_time_value_on_none_default_param(self, snapshot_engine: Engine) -> None:
        """Undoing the first set of a None-default parameter clears it (it had no snapshot command)."""
        flow_name = self._make_flow(snapshot_engine)
        self._create_node(flow_name, node_name="ProbeA")
        assert self._user_request(
            SetParameterValueRequest(node_name="ProbeA", parameter_name="opt", value="set-once")
        ).succeeded()

        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)
        value_result = GriptapeNodes.handle_request(GetParameterValueRequest(node_name="ProbeA", parameter_name="opt"))
        assert isinstance(value_result, GetParameterValueResultSuccess)
        assert value_result.value is None

    def test_unrelated_undo_preserves_other_node_value(self, snapshot_engine: Engine) -> None:
        """Undoing one node's move must not clear a value set on a different, untouched node.

        Guards the reconcile clear-path: it runs over every survivor, and must only clear values
        added since the snapshot, never a value that was already present.
        """
        flow_name = self._make_flow(snapshot_engine)
        self._create_node(flow_name, node_name="ProbeA")
        self._create_node(flow_name, node_name="ProbeB")
        assert self._user_request(
            SetParameterValueRequest(node_name="ProbeB", parameter_name="text", value="keepB")
        ).succeeded()

        obj = GriptapeNodes.ObjectManager()
        node_a = obj.attempt_get_object_by_name_as_type("ProbeA", BaseNode)
        assert node_a is not None
        moved_metadata = {**copy.deepcopy(node_a.metadata), "position": {"x": 500, "y": 600}}
        assert self._user_request(SetNodeMetadataRequest(node_name="ProbeA", metadata=moved_metadata)).succeeded()

        # Undo the move on A; B's value is unrelated and must survive the reconcile over B.
        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)
        value_result = GriptapeNodes.handle_request(GetParameterValueRequest(node_name="ProbeB", parameter_name="text"))
        assert isinstance(value_result, GetParameterValueResultSuccess)
        assert value_result.value == "keepB"

    def test_undo_restores_value_on_a_node_that_ends_locked(self, snapshot_engine: Engine) -> None:
        """Undo restores a value even when the batch also locked the node (unlock happens first).

        Order matters in the reconcile: a locked node rejects value sets, so applying values before
        restoring lock state leaves stale post-edit values behind after an undo.
        """
        flow_name = self._make_flow(snapshot_engine)
        self._create_node(flow_name, node_name="ProbeA")
        undo_manager = GriptapeNodes.UndoManager()

        # One undoable batch that both sets a value and locks the node.
        with undo_manager.transaction("Edit and lock"):
            snapshot_engine.handle_request(
                SetParameterValueRequest(node_name="ProbeA", parameter_name="text", value="hello")
            )
            snapshot_engine.handle_request(SetLockNodeStateRequest(node_name="ProbeA", lock=True))

        obj = GriptapeNodes.ObjectManager()
        node = obj.attempt_get_object_by_name_as_type("ProbeA", BaseNode)
        assert node is not None
        assert node.lock is True

        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)
        # The node must be unlocked again and its value reverted (the reconcile unlocks before setting).
        assert node.lock is False
        value_result = GriptapeNodes.handle_request(GetParameterValueRequest(node_name="ProbeA", parameter_name="text"))
        assert isinstance(value_result, GetParameterValueResultSuccess)
        assert value_result.value == ""

        # Redo re-applies the after-snapshot against the now unlocked+empty node, exercising the
        # unlock-during-restore-then-relock branch: the value is set while unlocked, then relocked.
        assert isinstance(GriptapeNodes.handle_request(RedoRequest()), RedoResultSuccess)
        assert node.lock is True
        redo_value = GriptapeNodes.handle_request(GetParameterValueRequest(node_name="ProbeA", parameter_name="text"))
        assert isinstance(redo_value, GetParameterValueResultSuccess)
        assert redo_value.value == "hello"

    def test_undo_restores_a_renamed_node(self, snapshot_engine: Engine) -> None:
        """Undoing a rename brings the old name back, via delete+recreate since nodes are matched by name."""
        flow_name = self._make_flow(snapshot_engine)
        self._create_node(flow_name, node_name="ProbeA")
        assert self._user_request(
            SetParameterValueRequest(node_name="ProbeA", parameter_name="text", value="kept")
        ).succeeded()
        assert self._user_request(RenameObjectRequest(object_name="ProbeA", requested_name="Renamed")).succeeded()

        obj = GriptapeNodes.ObjectManager()
        assert obj.has_object_with_name("Renamed")
        assert not obj.has_object_with_name("ProbeA")

        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)
        assert obj.has_object_with_name("ProbeA")
        assert not obj.has_object_with_name("Renamed")
        value = GriptapeNodes.handle_request(GetParameterValueRequest(node_name="ProbeA", parameter_name="text"))
        assert isinstance(value, GetParameterValueResultSuccess)
        assert value.value == "kept"

    def test_undo_restores_flow_metadata(self, snapshot_engine: Engine) -> None:
        """Changing the flow's own properties is undoable: the snapshot carries the flow's metadata.

        The serialized commands omit it (they are captured without a create-flow command, which is
        what carries it), so the flow's metadata is captured alongside them.
        """
        flow_name = self._make_flow(snapshot_engine)
        assert self._user_request(SetFlowMetadataRequest(flow_name=flow_name, metadata={"note": "before"})).succeeded()
        assert self._user_request(SetFlowMetadataRequest(flow_name=flow_name, metadata={"note": "after"})).succeeded()

        flow = GriptapeNodes.FlowManager().get_flow_by_name(flow_name)
        assert flow.metadata["note"] == "after"

        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)
        assert flow.metadata["note"] == "before"

        assert isinstance(GriptapeNodes.handle_request(RedoRequest()), RedoResultSuccess)
        assert flow.metadata["note"] == "after"

    def test_undo_survives_a_top_level_flow_rename(self, snapshot_engine: Engine) -> None:
        """An edit recorded before the top-level flow was renamed is still undoable afterward.

        Restore resolves the top-level flow by its status rather than by the name the snapshot
        captured, so a rename in between does not fail the replay and take the whole undo history
        down with it. Drains the stack instead of assuming a batch count, since whether the rename
        itself is recorded depends on capture succeeding after it.
        """
        flow_name = self._make_flow(snapshot_engine)
        self._create_node(flow_name, node_name="ProbeA")
        assert self._user_request(RenameObjectRequest(object_name=flow_name, requested_name="RenamedFlow")).succeeded()

        while True:
            state = GriptapeNodes.handle_request(GetUndoStateRequest())
            assert isinstance(state, GetUndoStateResultSuccess)
            if not state.undo_labels:
                break
            assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)

        assert not GriptapeNodes.ObjectManager().has_object_with_name("ProbeA")

    def test_undo_entries_are_labeled_by_their_operation(self, snapshot_engine: Engine) -> None:
        """Each entry is named for the operation that opened it, so the stack says what it will revert.

        This is what makes a multi-request gesture legible: a client that creates a node and then
        writes its metadata produces two entries, and the labels show that the first undo reverts the
        move rather than the creation.
        """
        flow_name = self._make_flow(snapshot_engine)
        self._create_node(flow_name, node_name="ProbeA")
        assert self._user_request(
            SetNodeMetadataRequest(node_name="ProbeA", metadata={"position": {"x": 120, "y": 40}})
        ).succeeded()

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == ["Create node", "Move node"]

        undo_result = GriptapeNodes.handle_request(UndoRequest())
        assert isinstance(undo_result, UndoResultSuccess)
        assert undo_result.undone_label == "Move node"
        # The node survives the first undo: only its position was reverted.
        assert GriptapeNodes.ObjectManager().has_object_with_name("ProbeA")

        undo_result = GriptapeNodes.handle_request(UndoRequest())
        assert isinstance(undo_result, UndoResultSuccess)
        assert undo_result.undone_label == "Create node"
        assert not GriptapeNodes.ObjectManager().has_object_with_name("ProbeA")

    # ---------- Fail-closed capture ----------

    def test_capture_returns_none_when_serialization_fails(
        self, snapshot_engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """capture_workflow_snapshot must return None (not a partial snapshot) when serialization fails."""
        self._make_flow(snapshot_engine)
        self._patch_handler(
            monkeypatch,
            SerializeFlowToCommandsRequest,
            lambda _request: SerializeFlowToCommandsResultFailure(result_details="forced failure"),
        )
        assert capture_workflow_snapshot(snapshot_engine) is None

    def test_capture_returns_none_when_serialization_raises(
        self, snapshot_engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """capture_workflow_snapshot's broad except must swallow a raising handler rather than propagate."""
        self._make_flow(snapshot_engine)

        def _raise(_request: RequestPayload) -> ResultPayload:
            msg = "boom"
            raise RuntimeError(msg)

        self._patch_handler(monkeypatch, SerializeFlowToCommandsRequest, _raise)
        assert capture_workflow_snapshot(snapshot_engine) is None

    def test_capture_returns_none_on_partial_value_key_capture(
        self, snapshot_engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_capture_explicit_value_keys must refuse a partial map rather than let capture proceed with one."""
        flow_name = self._make_flow(snapshot_engine)
        self._create_node(flow_name, node_name="ProbeA")
        self._create_node(flow_name, node_name="ProbeB")

        object_manager = GriptapeNodes.ObjectManager()
        original = object_manager.attempt_get_object_by_name_as_type

        def _partial(name: str, cast_type: type[object]) -> object | None:
            if name == "ProbeB":
                return None
            return original(name, cast_type)

        monkeypatch.setattr(object_manager, "attempt_get_object_by_name_as_type", _partial)
        assert capture_workflow_snapshot(snapshot_engine) is None

    def test_capture_failure_makes_the_action_not_undoable(
        self, snapshot_engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A capture failure degrades the edit to not-undoable; it must never fail the edit itself."""
        flow_name = self._make_flow(snapshot_engine)
        self._patch_handler(
            monkeypatch,
            SerializeFlowToCommandsRequest,
            lambda _request: SerializeFlowToCommandsResultFailure(result_details="forced failure"),
        )
        self._create_node(flow_name, node_name="ProbeA")

        assert self._user_request(
            SetParameterValueRequest(node_name="ProbeA", parameter_name="text", value="hello")
        ).succeeded()

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == []

    # ---------- Fail-closed framing / replay ----------

    def test_handler_raising_mid_frame_records_no_batch(
        self, snapshot_engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A frame whose handler raised without changing anything must commit nothing.

        The complement of test_dispatch_raising_after_a_nested_mutation_commits_the_partial_edit:
        nothing altered state before the raise, so there is no partial edit to keep.
        """
        flow_name = self._make_flow(snapshot_engine)
        # Dispatched without a request_id, so this setup step is itself not recorded, leaving the
        # undo stack empty going into the raising dispatch below.
        snapshot_engine.handle_request(
            CreateNodeRequest(
                node_type=self._NODE_TYPE,
                specific_library_name=self._LIBRARY_NAME,
                node_name="ProbeA",
                override_parent_flow_name=flow_name,
            )
        )

        def _raise(_request: RequestPayload) -> ResultPayload:
            msg = "boom"
            raise RuntimeError(msg)

        self._patch_handler(monkeypatch, SetNodeMetadataRequest, _raise)

        with pytest.raises(RuntimeError):
            self._user_request(SetNodeMetadataRequest(node_name="ProbeA", metadata={"position": {"x": 1, "y": 2}}))

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == []

    def test_transaction_exception_still_commits_the_partial_edit(self, snapshot_engine: Engine) -> None:
        """A raise inside a transaction commits what the block managed to change, so undo can back it out.

        The after-snapshot is taken from live state, so it records the partial mutation faithfully;
        discarding it instead would leave the half-applied edit stuck.
        """
        flow_name = self._make_flow(snapshot_engine)
        self._create_node(flow_name, node_name="ProbeA")

        undo_manager = GriptapeNodes.UndoManager()

        def _mutate_then_raise() -> None:
            with undo_manager.transaction("Boom"):
                snapshot_engine.handle_request(
                    CreateNodeRequest(
                        node_type=self._NODE_TYPE,
                        specific_library_name=self._LIBRARY_NAME,
                        node_name="ProbeC",
                        override_parent_flow_name=flow_name,
                    )
                )
                msg = "boom"
                raise RuntimeError(msg)

        with pytest.raises(RuntimeError):
            _mutate_then_raise()

        assert GriptapeNodes.ObjectManager().has_object_with_name("ProbeC")
        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels[-1] == "Boom"

        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)
        assert not GriptapeNodes.ObjectManager().has_object_with_name("ProbeC")

    def test_dispatch_raising_after_a_nested_mutation_commits_the_partial_edit(
        self, snapshot_engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The per-dispatch path treats a raise the same way transaction() does: the partial edit stays undoable."""
        flow_name = self._make_flow(snapshot_engine)
        self._create_node(flow_name, node_name="ProbeA")

        def _mutate_then_raise(_request: RequestPayload) -> ResultPayload:
            GriptapeNodes.handle_request(
                SetParameterValueRequest(node_name="ProbeA", parameter_name="text", value="partial")
            )
            msg = "boom"
            raise RuntimeError(msg)

        self._patch_handler(monkeypatch, SetNodeMetadataRequest, _mutate_then_raise)

        with pytest.raises(RuntimeError):
            self._user_request(SetNodeMetadataRequest(node_name="ProbeA", metadata={"position": {"x": 1, "y": 2}}))

        partial = GriptapeNodes.handle_request(GetParameterValueRequest(node_name="ProbeA", parameter_name="text"))
        assert isinstance(partial, GetParameterValueResultSuccess)
        assert partial.value == "partial"

        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)
        reverted = GriptapeNodes.handle_request(GetParameterValueRequest(node_name="ProbeA", parameter_name="text"))
        assert isinstance(reverted, GetParameterValueResultSuccess)
        assert reverted.value == ""

    def test_transaction_without_mutation_records_nothing(self, snapshot_engine: Engine) -> None:
        """Transaction commits only if something inside it altered workflow state; an empty block pushes nothing."""
        self._make_flow(snapshot_engine)
        undo_manager = GriptapeNodes.UndoManager()

        with undo_manager.transaction("No-op"):
            pass

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == []

    def test_clear_history_request_inside_open_frame_records_no_batch(self, snapshot_engine: Engine) -> None:
        """A history-clearing lifecycle request seen mid-frame makes the frame finalize without committing."""
        flow_name = self._make_flow(snapshot_engine)
        self._create_node(flow_name, node_name="ProbeA")

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels  # sanity: something to lose

        undo_manager = GriptapeNodes.UndoManager()
        with undo_manager.transaction("Mixed"):
            snapshot_engine.handle_request(
                CreateNodeRequest(
                    node_type=self._NODE_TYPE,
                    specific_library_name=self._LIBRARY_NAME,
                    node_name="ProbeB",
                    override_parent_flow_name=flow_name,
                )
            )
            self._user_request(SetWorkflowContextRequest(workflow_name=None))

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == []
        assert state.redo_labels == []

    def test_overlapping_user_request_inside_open_frame_clears_history(
        self, snapshot_engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second externally-initiated request seen mid-frame drops both the frame and the history.

        Only a nested dispatch can belong to an open frame, and a nested dispatch carries no
        request_id. One that does is a separate gesture running at the same time, so folding it in
        would record its edit as part of the open action.
        """
        flow_name = self._make_flow(snapshot_engine)
        self._create_node(flow_name, node_name="ProbeA")

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels  # sanity: something to lose

        def _overlapping_gesture(_request: RequestPayload) -> ResultPayload:
            self._user_request(
                SetParameterValueRequest(node_name="ProbeA", parameter_name="text", value="other-gesture"),
                request_id="other-request",
            )
            return SetNodeMetadataResultSuccess(result_details="stand-in metadata handler")

        self._patch_handler(monkeypatch, SetNodeMetadataRequest, _overlapping_gesture)
        assert self._user_request(
            SetNodeMetadataRequest(node_name="ProbeA", metadata={"position": {"x": 1, "y": 2}})
        ).succeeded()

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == []
        assert state.redo_labels == []

    def test_structural_restore_failure_clears_history_and_reports_failure(
        self, snapshot_engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A structural replay failure (_require_success) surfaces as UndoResultFailure and clears all history."""
        flow_name = self._make_flow(snapshot_engine)
        self._create_node(flow_name, node_name="ProbeA")
        self._create_node(flow_name, node_name="ProbeB")  # undoing this must DELETE it

        self._patch_handler(
            monkeypatch,
            DeleteNodeRequest,
            lambda _request: DeleteNodeResultFailure(result_details="forced failure"),
        )

        result = GriptapeNodes.handle_request(UndoRequest())
        assert isinstance(result, UndoResultFailure)
        assert "history has been cleared" in str(result.result_details).lower()

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == []
        assert state.redo_labels == []

    def test_undo_restores_a_deleted_connection(self, snapshot_engine: Engine) -> None:
        """Undo recreates a deleted connection: the mirror of the removal branch other tests cover."""
        flow_name = self._make_flow(snapshot_engine)
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

        def connected() -> bool:
            incoming = GriptapeNodes.FlowManager().get_connections().incoming_index.get("Target", {})
            return "text" in incoming

        assert connected()
        assert self._user_request(
            DeleteConnectionRequest(
                source_node_name="Source",
                source_parameter_name="text",
                target_node_name="Target",
                target_parameter_name="text",
            )
        ).succeeded()
        assert not connected()

        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)
        assert connected()


class TestDifferingKeys:
    """Covers the metadata diff used to report which part of a node an undo is about to change."""

    def test_reports_only_keys_whose_values_changed(self) -> None:
        current = {"position": {"x": 1, "y": 2}, "size": {"width": 260}}
        target = {"position": {"x": 1, "y": 2}, "size": {"width": 200}}
        assert _differing_keys(current, target) == {"size"}

    def test_reports_keys_present_on_only_one_side(self) -> None:
        """A key added or dropped since capture differs, so it must not be silently equal."""
        assert _differing_keys({"tempId": "t1"}, {}) == {"tempId"}
        assert _differing_keys({}, {"tempId": "t1"}) == {"tempId"}

    def test_reports_nothing_when_metadata_matches(self) -> None:
        metadata = {"position": {"x": 1, "y": 2}, "size": {"width": 200}}
        assert _differing_keys(dict(metadata), dict(metadata)) == set()
