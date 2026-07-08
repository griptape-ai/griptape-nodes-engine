"""Unit tests for `UndoManager`: recording and replaying undoable user actions.

Phase 1 covers node creation and deletion. These tests drive the engine end to end
through a registered probe library so that `CreateNodeRequest`, serialization, and
deserialization all exercise their real handlers rather than mocks.

User-initiated requests are simulated by dispatching through the EventManager with a
``request_id`` in the result context, which is what marks a request as originating from
an external caller (e.g. the editor) and therefore eligible for undo recording.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

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
    DeleteNodeResultSuccess,
    SetLockNodeStateRequest,
    SetLockNodeStateResultSuccess,
)
from griptape_nodes.retained_mode.events.object_events import RenameObjectRequest
from griptape_nodes.retained_mode.events.parameter_events import (
    GetParameterValueRequest,
    GetParameterValueResultSuccess,
    SetParameterValueRequest,
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

if TYPE_CHECKING:
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
        assert CreateNodeRequest in undo_manager._recording._recorders
        assert DeleteNodeRequest in undo_manager._recording._recorders

    def test_register_recorder_rejects_duplicate(self, griptape_nodes: GriptapeNodes) -> None:
        """A second recorder for the same request type is a registration error, mirroring handlers."""
        from griptape_nodes.retained_mode.managers.undo.node import CreateNodeRecorder

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
