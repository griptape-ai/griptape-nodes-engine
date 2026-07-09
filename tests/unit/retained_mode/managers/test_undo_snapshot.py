"""Tests for the experimental whole-flow snapshot undo strategy (prototype).

These force GRIPTAPE_NODES_UNDO_STRATEGY=snapshot and rebuild the GriptapeNodes singleton so the
UndoManager wires the SnapshotRecordingSession, then drive the same user-request flow the inverse
strategy is tested with. They verify the snapshot round trip (undo restores the previous whole-flow
state; redo re-applies it) rather than per-request inverses.
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
)
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    CreateNodeResultSuccess,
    DeleteNodeRequest,
    SetNodeMetadataRequest,
)
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
from griptape_nodes.retained_mode.managers.undo.snapshot import SnapshotRecordingSession

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
    def snapshot_engine(self) -> Iterator[GriptapeNodes]:
        """Rebuild the singleton under snapshot mode so UndoManager uses the snapshot session."""
        from griptape_nodes.node_library.library_registry import LibraryRegistry
        from griptape_nodes.utils.metaclasses import SingletonMeta

        with patch.dict(os.environ, {"GRIPTAPE_NODES_UNDO_STRATEGY": "snapshot"}):
            SingletonMeta._instances.clear()
            LibraryRegistry._clear()
            griptape_nodes = GriptapeNodes()
            assert isinstance(GriptapeNodes.UndoManager()._recording, SnapshotRecordingSession)
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

    # ---------- End-to-end via the snapshot session ----------

    def test_undo_restores_prior_whole_flow_state(self, snapshot_engine: GriptapeNodes) -> None:
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

    def test_undo_restores_parameter_value(self, snapshot_engine: GriptapeNodes) -> None:
        flow_name = self._make_flow(snapshot_engine)
        self._create_node(flow_name, node_name="ProbeA")
        assert self._user_request(
            SetParameterValueRequest(node_name="ProbeA", parameter_name="text", value="hello")
        ).succeeded()

        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)
        value_result = GriptapeNodes.handle_request(GetParameterValueRequest(node_name="ProbeA", parameter_name="text"))
        assert isinstance(value_result, GetParameterValueResultSuccess)
        assert value_result.value == ""

    def test_undo_restores_connection(self, snapshot_engine: GriptapeNodes) -> None:
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

    def test_transaction_groups_into_one_snapshot(self, snapshot_engine: GriptapeNodes) -> None:
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

    def test_undo_value_edit_reconciles_survivors_in_place(self, snapshot_engine: GriptapeNodes) -> None:
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

    def test_undo_delete_recreates_only_changed_node(self, snapshot_engine: GriptapeNodes) -> None:
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

    # ---------- Editor mutations that the inverse strategy floors are undoable under snapshot ----------

    def test_node_move_is_its_own_undo_step(self, snapshot_engine: GriptapeNodes) -> None:
        """A node move is captured as its own snapshot step and is undoable.

        Regression: SetNodeMetadata is declared as an inverse-only floor. When that floor was reused
        as the snapshot strategy's "do not snapshot" set, moving a node produced no undo step, so
        moves could not be undone and unrelated undos appeared to ignore them.
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

    def test_undo_edit_preserves_an_earlier_move(self, snapshot_engine: GriptapeNodes) -> None:
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

    def test_undo_clears_first_time_value_on_none_default_param(self, snapshot_engine: GriptapeNodes) -> None:
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

    def test_unrelated_undo_preserves_other_node_value(self, snapshot_engine: GriptapeNodes) -> None:
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
