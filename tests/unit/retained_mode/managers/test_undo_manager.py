"""Unit tests for `UndoManager`: the strategy-agnostic mechanism (stacks, guards, replay, lifecycle).

These exercise the manager itself -- undo/redo stacks, the replay guard, the flow-running guard,
history invalidation, and the shared lifecycle policy -- independently of which recording strategy
is installed. Snapshot-specific behavior (capture/reconcile) lives in ``test_undo_snapshot.py``.

User-initiated requests are simulated by dispatching through the EventManager with a ``request_id``
in the result context, which is what marks a request as originating from an external caller.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode, ParameterTypeBuiltin
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.retained_mode.events.context_events import SetWorkflowContextRequest
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from griptape_nodes.retained_mode.events.undo_events import (
    ClearUndoStateRequest,
    ClearUndoStateResultSuccess,
    GetUndoStateRequest,
    GetUndoStateResultSuccess,
    RedoRequest,
    RedoResultFailure,
    UndoRequest,
    UndoResultFailure,
    UndoResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.undo import UndoBatch, UndoEntry, UndoEntryReplayError
from griptape_nodes.retained_mode.managers.undo.manager import MAX_UNDO_BATCHES
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

    def process(self) -> None:
        return None


class _NoopUndoEntry(UndoEntry):
    """Entry that does nothing; used to populate the stacks in mechanism tests."""

    def undo(self) -> None:
        return None

    def redo(self) -> None:
        return None


class _RaisingUndoEntry(UndoEntry):
    """Entry that raises a given error on replay; used to drive the replay-failure paths."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def undo(self) -> None:
        raise self._error

    def redo(self) -> None:
        raise self._error


class TestUndoManager:
    _LIBRARY_NAME = "undo-manager-test-library"
    _NODE_TYPE = "_ProbeNode"

    @pytest.fixture
    def engine(self) -> Iterator[GriptapeNodes]:
        """Rebuild the singleton so the UndoManager wires the (default) snapshot recording strategy."""
        from griptape_nodes.node_library.library_registry import LibraryRegistry
        from griptape_nodes.utils.metaclasses import SingletonMeta

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
            workflow_key = "unsaved:undo-manager-test"
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

    # ---------- Empty stacks / state reporting ----------

    def test_undo_empty_stack_fails(self, engine: GriptapeNodes) -> None:  # noqa: ARG002
        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultFailure)

    def test_redo_empty_stack_fails(self, engine: GriptapeNodes) -> None:  # noqa: ARG002
        assert isinstance(GriptapeNodes.handle_request(RedoRequest()), RedoResultFailure)

    def test_get_undo_state_reports_labels(self, engine: GriptapeNodes) -> None:
        flow_name = self._make_flow(engine)
        self._create_node(flow_name, node_name="ProbeA")

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert len(state.undo_labels) == 1
        assert state.redo_labels == []

    def test_clear_undo_state(self, engine: GriptapeNodes) -> None:
        flow_name = self._make_flow(engine)
        self._create_node(flow_name, node_name="ProbeA")

        assert isinstance(GriptapeNodes.handle_request(ClearUndoStateRequest()), ClearUndoStateResultSuccess)
        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == []
        assert state.redo_labels == []

    def test_new_recorded_action_clears_redo_stack(self, engine: GriptapeNodes) -> None:
        flow_name = self._make_flow(engine)
        self._create_node(flow_name, node_name="ProbeA")
        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultSuccess)

        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.redo_labels  # a redo is available

        self._create_node(flow_name, node_name="ProbeB")
        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.redo_labels == []

    # ---------- Stack bound ----------

    def test_undo_stack_respects_max_batches(self, engine: GriptapeNodes) -> None:  # noqa: ARG002
        undo_manager = GriptapeNodes.UndoManager()
        for index in range(MAX_UNDO_BATCHES + 5):
            undo_manager._commit_batch(UndoBatch(label=f"batch-{index}", entries=[_NoopUndoEntry()]))
        assert len(undo_manager._undo_stack) == MAX_UNDO_BATCHES

    # ---------- Guards ----------

    def test_undo_blocked_while_replay_in_progress(self, engine: GriptapeNodes) -> None:  # noqa: ARG002
        undo_manager = GriptapeNodes.UndoManager()
        undo_manager._undo_stack.append(UndoBatch(label="x", entries=[_NoopUndoEntry()]))
        undo_manager._is_replaying = True
        try:
            assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultFailure)
        finally:
            undo_manager._is_replaying = False

    def test_redo_blocked_while_replay_in_progress(self, engine: GriptapeNodes) -> None:  # noqa: ARG002
        undo_manager = GriptapeNodes.UndoManager()
        undo_manager._redo_stack.append(UndoBatch(label="x", entries=[_NoopUndoEntry()]))
        undo_manager._is_replaying = True
        try:
            assert isinstance(GriptapeNodes.handle_request(RedoRequest()), RedoResultFailure)
        finally:
            undo_manager._is_replaying = False

    def test_undo_fails_while_flow_running(self, engine: GriptapeNodes, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        undo_manager = GriptapeNodes.UndoManager()
        undo_manager._undo_stack.append(UndoBatch(label="x", entries=[_NoopUndoEntry()]))
        monkeypatch.setattr(GriptapeNodes.FlowManager(), "check_for_existing_running_flow", lambda: True)
        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultFailure)

    def test_redo_fails_while_flow_running(self, engine: GriptapeNodes, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        undo_manager = GriptapeNodes.UndoManager()
        undo_manager._redo_stack.append(UndoBatch(label="x", entries=[_NoopUndoEntry()]))
        monkeypatch.setattr(GriptapeNodes.FlowManager(), "check_for_existing_running_flow", lambda: True)
        assert isinstance(GriptapeNodes.handle_request(RedoRequest()), RedoResultFailure)

    # ---------- Replay failure clears history ----------

    def test_replay_error_clears_history(self, engine: GriptapeNodes) -> None:  # noqa: ARG002
        undo_manager = GriptapeNodes.UndoManager()
        undo_manager._undo_stack.append(UndoBatch(label="x", entries=[_RaisingUndoEntry(UndoEntryReplayError("boom"))]))
        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultFailure)
        assert not undo_manager._undo_stack
        assert not undo_manager._redo_stack

    def test_unexpected_replay_error_clears_history(self, engine: GriptapeNodes) -> None:  # noqa: ARG002
        undo_manager = GriptapeNodes.UndoManager()
        undo_manager._undo_stack.append(UndoBatch(label="x", entries=[_RaisingUndoEntry(ValueError("boom"))]))
        assert isinstance(GriptapeNodes.handle_request(UndoRequest()), UndoResultFailure)
        assert not undo_manager._undo_stack
        assert not undo_manager._redo_stack

    # ---------- Lifecycle policy ----------

    def test_set_workflow_context_clears_history(self, engine: GriptapeNodes) -> None:
        flow_name = self._make_flow(engine)
        self._create_node(flow_name, node_name="ProbeA")

        # A whole-graph lifecycle request invalidates all undo history.
        self._user_request(SetWorkflowContextRequest(workflow_name=None))
        state = GriptapeNodes.handle_request(GetUndoStateRequest())
        assert isinstance(state, GetUndoStateResultSuccess)
        assert state.undo_labels == []
