"""Tests for the experimental hybrid snapshot/inverse undo strategy (prototype).

These force GRIPTAPE_NODES_UNDO_STRATEGY=hybrid and rebuild the GriptapeNodes singleton so the
UndoManager wires the HybridRecordingSession, then drive the same user-request flow the other
strategies are tested with. They verify the routing contract: recorder-backed "surgical" types are
reversed by per-request inverses (no whole-flow snapshot), while every other mutation is reversed by
a whole-flow snapshot instead of clearing history the way the pure inverse strategy would.
"""

from __future__ import annotations

import copy
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode, ParameterTypeBuiltin
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
    DeleteConnectionRequest,
    DeleteConnectionResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    CreateNodeResultSuccess,
    DeleteNodeRequest,
    SetLockNodeStateRequest,
    SetNodeMetadataRequest,
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
    UndoResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.undo.core import RequestReplayUndoEntry, UndoBatch
from griptape_nodes.retained_mode.managers.undo.hybrid import HybridRecordingSession
from griptape_nodes.retained_mode.managers.undo.recorders.node import CreateNodeUndoEntry, DeleteNodeUndoEntry
from griptape_nodes.retained_mode.managers.undo.snapshot import FlowSnapshotEntry

if TYPE_CHECKING:
    from collections.abc import Iterator

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

    def process(self) -> None:
        return None


class TestHybridStrategy:
    _LIBRARY_NAME = "undo-hybrid-test-library"
    _NODE_TYPE = "_ProbeNode"

    @pytest.fixture
    def hybrid_engine(self) -> Iterator[GriptapeNodes]:
        """Rebuild the singleton under hybrid mode so UndoManager uses the hybrid session."""
        from griptape_nodes.node_library.library_registry import LibraryRegistry
        from griptape_nodes.utils.metaclasses import SingletonMeta

        with patch.dict(os.environ, {"GRIPTAPE_NODES_UNDO_STRATEGY": "hybrid"}):
            SingletonMeta._instances.clear()
            LibraryRegistry._clear()
            griptape_nodes = GriptapeNodes()
            assert isinstance(GriptapeNodes.UndoManager()._recording, HybridRecordingSession)
            self._register_library()
            yield griptape_nodes
            SingletonMeta._instances.clear()
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

    def _make_flow(self, griptape_nodes: GriptapeNodes) -> str:
        from griptape_nodes.node_library.workflow_registry import WorkflowRegistry
        from griptape_nodes.retained_mode.managers.workflow_manager import WorkflowMetadata

        context_manager = griptape_nodes.ContextManager()
        if not context_manager.has_current_workflow():
            workflow_key = "unsaved:undo-hybrid-test"
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

    def _create_node(self, flow_name: str, node_name: str | None = None, position: dict | None = None) -> str:
        metadata = {"position": position} if position is not None else None
        result = self._user_request(
            CreateNodeRequest(
                node_type=self._NODE_TYPE,
                specific_library_name=self._LIBRARY_NAME,
                node_name=node_name,
                override_parent_flow_name=flow_name,
                metadata=metadata,
            )
        )
        assert isinstance(result, CreateNodeResultSuccess)
        return result.node_name

    @staticmethod
    def _latest_batch() -> UndoBatch:
        undo_stack = GriptapeNodes.UndoManager()._undo_stack
        assert undo_stack, "expected at least one recorded batch"
        return undo_stack[-1]

    @staticmethod
    def _move(node_name: str, position: dict) -> ResultPayload:
        obj = GriptapeNodes.ObjectManager()
        node = obj.attempt_get_object_by_name_as_type(node_name, BaseNode)
        assert node is not None
        moved_metadata = {**copy.deepcopy(node.metadata), "position": position}
        return TestHybridStrategy._user_request(SetNodeMetadataRequest(node_name=node_name, metadata=moved_metadata))

    # ---------- Routing: surgical types use inverse entries, no whole-flow snapshot ----------

    def test_create_node_routes_to_surgical_entry(self, hybrid_engine: GriptapeNodes) -> None:
        flow_name = self._make_flow(hybrid_engine)
        self._create_node(flow_name, node_name="ProbeA")

        batch = self._latest_batch()
        assert [type(entry) for entry in batch.entries] == [CreateNodeUndoEntry]

    def test_delete_node_routes_to_surgical_entry(self, hybrid_engine: GriptapeNodes) -> None:
        flow_name = self._make_flow(hybrid_engine)
        self._create_node(flow_name, node_name="ProbeA")
        assert self._user_request(DeleteNodeRequest(node_name="ProbeA")).succeeded()

        batch = self._latest_batch()
        assert [type(entry) for entry in batch.entries] == [DeleteNodeUndoEntry]

    def test_set_parameter_value_routes_to_surgical_entry(self, hybrid_engine: GriptapeNodes) -> None:
        flow_name = self._make_flow(hybrid_engine)
        self._create_node(flow_name, node_name="ProbeA")
        assert self._user_request(
            SetParameterValueRequest(node_name="ProbeA", parameter_name="text", value="hello")
        ).succeeded()

        batch = self._latest_batch()
        assert [type(entry) for entry in batch.entries] == [RequestReplayUndoEntry]

    def test_connection_edits_route_to_surgical_entries(self, hybrid_engine: GriptapeNodes) -> None:
        flow_name = self._make_flow(hybrid_engine)
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
        assert [type(entry) for entry in self._latest_batch().entries] == [RequestReplayUndoEntry]

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
        assert [type(entry) for entry in self._latest_batch().entries] == [RequestReplayUndoEntry]

    def test_surgical_undo_redo_roundtrip(self, hybrid_engine: GriptapeNodes) -> None:
        flow_name = self._make_flow(hybrid_engine)
        self._create_node(flow_name, node_name="ProbeA")

        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)
        assert not GriptapeNodes.ObjectManager().has_object_with_name("ProbeA")
        assert isinstance(GriptapeNodes.handle_request(RedoRequest()), RedoResultSuccess)
        assert GriptapeNodes.ObjectManager().has_object_with_name("ProbeA")

    # ---------- Routing: uncovered edits take the snapshot path (not history invalidation) ----------

    def test_node_move_routes_to_snapshot_entry(self, hybrid_engine: GriptapeNodes) -> None:
        flow_name = self._make_flow(hybrid_engine)
        self._create_node(flow_name, node_name="ProbeA")
        assert self._move("ProbeA", {"x": 500, "y": 600}).succeeded()

        batch = self._latest_batch()
        assert [type(entry) for entry in batch.entries] == [FlowSnapshotEntry]

    def test_node_move_is_undoable_and_preserves_surgical_history(self, hybrid_engine: GriptapeNodes) -> None:
        """The key hybrid win: an uncovered edit snapshots instead of clearing the surgical history.

        Under the pure inverse strategy a node move (no recorder) would invalidate the whole undo
        stack. Under the hybrid it becomes its own snapshot step, and the earlier surgical create
        stays undoable.
        """
        flow_name = self._make_flow(hybrid_engine)
        self._create_node(flow_name, node_name="ProbeA", position={"x": 10, "y": 20})
        obj = GriptapeNodes.ObjectManager()
        node = obj.attempt_get_object_by_name_as_type("ProbeA", BaseNode)
        assert node is not None
        original_position = copy.deepcopy(node.metadata.get("position"))

        assert self._move("ProbeA", {"x": 500, "y": 600}).succeeded()

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert len(state.undo_labels) == 2  # create (surgical) + move (snapshot)  # noqa: PLR2004

        # Undo the move: snapshot restore reverts the position.
        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)
        assert node.metadata.get("position") == original_position

        # The earlier surgical create is still undoable.
        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)
        assert not obj.has_object_with_name("ProbeA")

    def test_lock_toggle_folds_into_a_transaction_snapshot(self, hybrid_engine: GriptapeNodes) -> None:
        """A lock toggle reports no altered state, so it is undoable only when folded into a snapshot frame.

        Standalone it records nothing (matching the snapshot strategy, which commits on
        altered_workflow_state); inside a transaction the surrounding snapshot captures the lock.
        """
        flow_name = self._make_flow(hybrid_engine)
        self._create_node(flow_name, node_name="ProbeA")
        obj = GriptapeNodes.ObjectManager()
        node = obj.attempt_get_object_by_name_as_type("ProbeA", BaseNode)
        assert node is not None
        undo_manager = GriptapeNodes.UndoManager()

        with undo_manager.transaction("Lock it"):
            hybrid_engine.handle_request(SetLockNodeStateRequest(node_name="ProbeA", lock=True))
        assert node.lock is True
        assert [type(entry) for entry in self._latest_batch().entries] == [FlowSnapshotEntry]

        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)
        assert node.lock is False

    def test_untracked_user_mutation_snapshots_instead_of_clearing(self, hybrid_engine: GriptapeNodes) -> None:
        """A mutation with no recorder (a rename) is captured by snapshot, keeping prior history.

        RenameObject alters workflow state and has no recorder; the pure inverse strategy clears
        history on it, but the hybrid routes it to a snapshot and preserves the earlier surgical step.
        """
        flow_name = self._make_flow(hybrid_engine)
        self._create_node(flow_name, node_name="ProbeA")

        assert self._user_request(RenameObjectRequest(object_name="ProbeA", requested_name="Renamed")).succeeded()

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert len(state.undo_labels) == 2  # noqa: PLR2004

    # ---------- Mixed sequences and grouping ----------

    def test_mixed_surgical_then_snapshot_undo_in_order(self, hybrid_engine: GriptapeNodes) -> None:
        flow_name = self._make_flow(hybrid_engine)
        self._create_node(flow_name, node_name="ProbeA", position={"x": 10, "y": 20})
        assert self._user_request(
            SetParameterValueRequest(node_name="ProbeA", parameter_name="text", value="hello")
        ).succeeded()
        assert self._move("ProbeA", {"x": 500, "y": 600}).succeeded()

        obj = GriptapeNodes.ObjectManager()
        node = obj.attempt_get_object_by_name_as_type("ProbeA", BaseNode)
        assert node is not None

        # Undo move (snapshot), then value (surgical), then create (surgical).
        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)
        assert node.metadata.get("position") != {"x": 500, "y": 600}

        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)
        value_result = GriptapeNodes.handle_request(GetParameterValueRequest(node_name="ProbeA", parameter_name="text"))
        assert isinstance(value_result, GetParameterValueResultSuccess)
        assert value_result.value == ""

        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)
        assert not obj.has_object_with_name("ProbeA")

    def test_transaction_groups_into_one_snapshot_batch(self, hybrid_engine: GriptapeNodes) -> None:
        flow_name = self._make_flow(hybrid_engine)
        undo_manager = GriptapeNodes.UndoManager()

        with undo_manager.transaction("Add pair"):
            hybrid_engine.handle_request(
                CreateNodeRequest(
                    node_type=self._NODE_TYPE,
                    specific_library_name=self._LIBRARY_NAME,
                    node_name="Pair1",
                    override_parent_flow_name=flow_name,
                )
            )
            hybrid_engine.handle_request(
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
        # A transaction is always one snapshot pair, even though its body is all surgical types.
        assert [type(entry) for entry in self._latest_batch().entries] == [FlowSnapshotEntry]

        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)
        assert not GriptapeNodes.ObjectManager().has_object_with_name("Pair1")
        assert not GriptapeNodes.ObjectManager().has_object_with_name("Pair2")

    def test_delete_node_cascade_records_single_surgical_batch(self, hybrid_engine: GriptapeNodes) -> None:
        """A surgical delete folds its connection cascade into one batch, not a separate snapshot."""
        flow_name = self._make_flow(hybrid_engine)
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

        undo_len_before = len(GriptapeNodes.UndoManager()._undo_stack)
        assert self._user_request(DeleteNodeRequest(node_name="Target")).succeeded()
        # Exactly one new batch, and it is the surgical delete entry (the cascade folded in).
        assert len(GriptapeNodes.UndoManager()._undo_stack) == undo_len_before + 1
        assert [type(entry) for entry in self._latest_batch().entries] == [DeleteNodeUndoEntry]

    def test_noop_parameter_set_records_nothing(self, hybrid_engine: GriptapeNodes) -> None:
        flow_name = self._make_flow(hybrid_engine)
        self._create_node(flow_name, node_name="ProbeA")
        undo_len_before = len(GriptapeNodes.UndoManager()._undo_stack)

        # Setting the value to its current value is a no-op the surgical recorder records nothing for.
        assert self._user_request(
            SetParameterValueRequest(node_name="ProbeA", parameter_name="text", value="")
        ).succeeded()
        assert len(GriptapeNodes.UndoManager()._undo_stack) == undo_len_before

    # ---------- Lifecycle policy still clears history ----------

    def test_new_surgical_action_clears_redo_stack(self, hybrid_engine: GriptapeNodes) -> None:
        flow_name = self._make_flow(hybrid_engine)
        self._create_node(flow_name, node_name="ProbeA")
        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.redo_labels  # something to redo

        # A fresh recorded action drops the redo stack.
        self._create_node(flow_name, node_name="ProbeB")
        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.redo_labels == []
