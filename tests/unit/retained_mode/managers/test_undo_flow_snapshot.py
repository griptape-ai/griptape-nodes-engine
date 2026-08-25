"""Tests for `flow_snapshot`: the pure capture/restore layer, exercised directly.

These call `capture_workflow_snapshot` and `restore_workflow_snapshot` themselves rather than going
through `UndoRequest`/`RedoRequest` or the recording session, so nothing here depends on any request
type being declared undoable, on undo stacks, or on dispatch framing. What is covered is the
reconcile itself: given a snapshot and a live flow that has since diverged from it, restore emits
only the minimal set of mutations needed to bring the live flow back in line.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode, ParameterTypeBuiltin
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.retained_mode.engine import current_engine
from griptape_nodes.retained_mode.events.connection_events import CreateConnectionRequest, DeleteConnectionRequest
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
)
from griptape_nodes.retained_mode.events.object_events import RenameObjectRequest
from griptape_nodes.retained_mode.events.parameter_events import (
    GetParameterValueRequest,
    GetParameterValueResultSuccess,
    SetParameterValueRequest,
)
from griptape_nodes.retained_mode.managers.undo import UndoEntryReplayError
from griptape_nodes.retained_mode.managers.undo.flow_snapshot import (
    _differing_keys,
    capture_workflow_snapshot,
    restore_workflow_snapshot,
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


class TestFlowSnapshot:
    """Direct capture/restore round trips: given a diverged live flow, restore reconciles it back."""

    _LIBRARY_NAME = "flow-snapshot-test-library"
    _NODE_TYPE = "_ProbeNode"

    @pytest.fixture
    def flow_engine(self) -> Iterator[Engine]:
        """A fresh engine with the flow-snapshot test library registered.

        No undo machinery (recording strategy, stacks, request framing) is involved: capture and
        restore are called directly against this engine.
        """
        from griptape_nodes.node_library.library_registry import LibraryRegistry

        LibraryRegistry._clear()
        engine = current_engine()
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

    def _make_flow(self, engine: Engine) -> str:
        from griptape_nodes.node_library.workflow_registry import WorkflowRegistry
        from griptape_nodes.retained_mode.managers.workflow_manager import WorkflowMetadata

        context_manager = engine.context_manager
        if not context_manager.has_current_workflow():
            workflow_key = "unsaved:flow-snapshot-test"
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
        create_result = engine.handle_request(CreateFlowRequest(parent_flow_name=None))
        assert isinstance(create_result, CreateFlowResultSuccess)
        return create_result.flow_name

    def _create_node(self, engine: Engine, flow_name: str, node_name: str | None = None) -> str:
        result = engine.handle_request(
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
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        monkeypatch.setitem(GriptapeNodes.EventManager()._request_type_to_manager, request_type, handler)

    # ---------- Capture/restore round trips ----------

    def test_restore_recreates_a_node_deleted_after_capture(self, flow_engine: Engine) -> None:
        """A node present in the snapshot but missing live is synthesized back from its captured commands."""
        flow_name = self._make_flow(flow_engine)
        self._create_node(flow_engine, flow_name, node_name="ProbeA")
        snapshot = capture_workflow_snapshot(flow_engine)
        assert snapshot is not None

        assert flow_engine.handle_request(DeleteNodeRequest(node_name="ProbeA")).succeeded()
        assert not flow_engine.object_manager.has_object_with_name("ProbeA")

        restore_workflow_snapshot(flow_engine, snapshot)
        assert flow_engine.object_manager.has_object_with_name("ProbeA")

    def test_restore_deletes_a_node_created_after_capture(self, flow_engine: Engine) -> None:
        """A node absent from the snapshot but present live is deleted to match the captured state."""
        flow_name = self._make_flow(flow_engine)
        self._create_node(flow_engine, flow_name, node_name="ProbeA")
        snapshot = capture_workflow_snapshot(flow_engine)
        assert snapshot is not None

        self._create_node(flow_engine, flow_name, node_name="ProbeB")
        assert flow_engine.object_manager.has_object_with_name("ProbeB")

        restore_workflow_snapshot(flow_engine, snapshot)
        assert not flow_engine.object_manager.has_object_with_name("ProbeB")
        assert flow_engine.object_manager.has_object_with_name("ProbeA")

    def test_restore_puts_back_a_changed_parameter_value(self, flow_engine: Engine) -> None:
        """A survivor's parameter value reverts to what the snapshot captured, via the value-reconcile path."""
        flow_name = self._make_flow(flow_engine)
        self._create_node(flow_engine, flow_name, node_name="ProbeA")
        snapshot = capture_workflow_snapshot(flow_engine)
        assert snapshot is not None

        assert flow_engine.handle_request(
            SetParameterValueRequest(node_name="ProbeA", parameter_name="text", value="hello")
        ).succeeded()

        restore_workflow_snapshot(flow_engine, snapshot)
        value_result = flow_engine.handle_request(GetParameterValueRequest(node_name="ProbeA", parameter_name="text"))
        assert isinstance(value_result, GetParameterValueResultSuccess)
        assert value_result.value == ""

    def test_restore_clears_a_value_first_set_after_capture(self, flow_engine: Engine) -> None:
        """A parameter with no explicit value at capture time is cleared back to unset, not to a stale command.

        `opt` defaults to None and starts unset, so serialization records no value command for it.
        The reconcile clear path, not a replayed set command, is what has to reset it.
        """
        flow_name = self._make_flow(flow_engine)
        self._create_node(flow_engine, flow_name, node_name="ProbeA")
        snapshot = capture_workflow_snapshot(flow_engine)
        assert snapshot is not None

        assert flow_engine.handle_request(
            SetParameterValueRequest(node_name="ProbeA", parameter_name="opt", value="set-once")
        ).succeeded()

        restore_workflow_snapshot(flow_engine, snapshot)
        value_result = flow_engine.handle_request(GetParameterValueRequest(node_name="ProbeA", parameter_name="opt"))
        assert isinstance(value_result, GetParameterValueResultSuccess)
        assert value_result.value is None

    def test_restore_recreates_a_connection_deleted_after_capture(self, flow_engine: Engine) -> None:
        """A connection present in the snapshot but missing live is recreated, after every endpoint exists."""
        flow_name = self._make_flow(flow_engine)
        self._create_node(flow_engine, flow_name, node_name="Source")
        self._create_node(flow_engine, flow_name, node_name="Target")
        assert flow_engine.handle_request(
            CreateConnectionRequest(
                source_node_name="Source",
                source_parameter_name="text",
                target_node_name="Target",
                target_parameter_name="text",
            )
        ).succeeded()
        snapshot = capture_workflow_snapshot(flow_engine)
        assert snapshot is not None

        def connected() -> bool:
            incoming = flow_engine.flow_manager.get_connections().incoming_index.get("Target", {})
            return "text" in incoming

        assert flow_engine.handle_request(
            DeleteConnectionRequest(
                source_node_name="Source",
                source_parameter_name="text",
                target_node_name="Target",
                target_parameter_name="text",
            )
        ).succeeded()
        assert not connected()

        restore_workflow_snapshot(flow_engine, snapshot)
        assert connected()

    def test_restore_removes_a_connection_created_after_capture(self, flow_engine: Engine) -> None:
        """A connection absent from the snapshot but present live is deleted to match the captured state."""
        flow_name = self._make_flow(flow_engine)
        self._create_node(flow_engine, flow_name, node_name="Source")
        self._create_node(flow_engine, flow_name, node_name="Target")
        snapshot = capture_workflow_snapshot(flow_engine)
        assert snapshot is not None

        assert flow_engine.handle_request(
            CreateConnectionRequest(
                source_node_name="Source",
                source_parameter_name="text",
                target_node_name="Target",
                target_parameter_name="text",
            )
        ).succeeded()

        def connected() -> bool:
            incoming = flow_engine.flow_manager.get_connections().incoming_index.get("Target", {})
            return "text" in incoming

        assert connected()
        restore_workflow_snapshot(flow_engine, snapshot)
        assert not connected()

    def test_restore_puts_back_changed_node_metadata(self, flow_engine: Engine) -> None:
        """A survivor's position reverts to the snapshot, via the metadata-reconcile path.

        A move only changes node metadata, so it is easy to mistake for something restore can skip;
        this pins the metadata-reconcile branch as its own case, independent of any value change.
        """
        flow_name = self._make_flow(flow_engine)
        create_result = flow_engine.handle_request(
            CreateNodeRequest(
                node_type=self._NODE_TYPE,
                specific_library_name=self._LIBRARY_NAME,
                node_name="ProbeA",
                override_parent_flow_name=flow_name,
                metadata={"position": {"x": 10, "y": 20}},
            )
        )
        assert isinstance(create_result, CreateNodeResultSuccess)
        snapshot = capture_workflow_snapshot(flow_engine)
        assert snapshot is not None

        node = flow_engine.object_manager.attempt_get_object_by_name_as_type("ProbeA", BaseNode)
        assert node is not None
        moved_metadata = {**node.metadata, "position": {"x": 500, "y": 600}}
        assert flow_engine.handle_request(
            SetNodeMetadataRequest(node_name="ProbeA", metadata=moved_metadata)
        ).succeeded()
        assert node.metadata.get("position") == {"x": 500, "y": 600}

        restore_workflow_snapshot(flow_engine, snapshot)
        assert node.metadata.get("position") == {"x": 10, "y": 20}

    def test_restore_reverts_a_node_rename(self, flow_engine: Engine) -> None:
        """A rename undoes via delete+recreate, since nodes are matched by name (the only teardown/rebuild case)."""
        flow_name = self._make_flow(flow_engine)
        self._create_node(flow_engine, flow_name, node_name="ProbeA")
        assert flow_engine.handle_request(
            SetParameterValueRequest(node_name="ProbeA", parameter_name="text", value="kept")
        ).succeeded()
        snapshot = capture_workflow_snapshot(flow_engine)
        assert snapshot is not None

        assert flow_engine.handle_request(
            RenameObjectRequest(object_name="ProbeA", requested_name="Renamed")
        ).succeeded()
        assert flow_engine.object_manager.has_object_with_name("Renamed")
        assert not flow_engine.object_manager.has_object_with_name("ProbeA")

        restore_workflow_snapshot(flow_engine, snapshot)
        assert flow_engine.object_manager.has_object_with_name("ProbeA")
        assert not flow_engine.object_manager.has_object_with_name("Renamed")
        value_result = flow_engine.handle_request(GetParameterValueRequest(node_name="ProbeA", parameter_name="text"))
        assert isinstance(value_result, GetParameterValueResultSuccess)
        assert value_result.value == "kept"

    def test_restore_puts_back_changed_flow_metadata(self, flow_engine: Engine) -> None:
        """The top-level flow's own metadata reverts to the snapshot.

        The serialized node/connection commands omit it (captured without a create-flow command,
        which is what carries it), so the flow's metadata has to be captured and restored separately.
        """
        flow_name = self._make_flow(flow_engine)
        assert flow_engine.handle_request(
            SetFlowMetadataRequest(flow_name=flow_name, metadata={"note": "before"})
        ).succeeded()
        snapshot = capture_workflow_snapshot(flow_engine)
        assert snapshot is not None

        assert flow_engine.handle_request(
            SetFlowMetadataRequest(flow_name=flow_name, metadata={"note": "after"})
        ).succeeded()
        flow = flow_engine.flow_manager.get_flow_by_name(flow_name)
        assert flow.metadata["note"] == "after"

        restore_workflow_snapshot(flow_engine, snapshot)
        assert flow.metadata["note"] == "before"

    def test_restore_reconciles_survivors_in_place(self, flow_engine: Engine) -> None:
        """An untouched node keeps its exact instance identity across a restore.

        Only the changed node's values/position/lock/connections are updated; survivors are never
        deleted and recreated, so the editor updates surgically instead of blinking through a
        teardown/rebuild.
        """
        flow_name = self._make_flow(flow_engine)
        self._create_node(flow_engine, flow_name, node_name="ProbeA")
        self._create_node(flow_engine, flow_name, node_name="ProbeB")
        snapshot = capture_workflow_snapshot(flow_engine)
        assert snapshot is not None

        obj = flow_engine.object_manager
        node_a_before = obj.attempt_get_object_by_name_as_type("ProbeA", BaseNode)
        node_b_before = obj.attempt_get_object_by_name_as_type("ProbeB", BaseNode)

        assert flow_engine.handle_request(
            SetParameterValueRequest(node_name="ProbeB", parameter_name="text", value="hello")
        ).succeeded()

        restore_workflow_snapshot(flow_engine, snapshot)
        assert obj.attempt_get_object_by_name_as_type("ProbeA", BaseNode) is node_a_before
        assert obj.attempt_get_object_by_name_as_type("ProbeB", BaseNode) is node_b_before

    def test_restore_reaches_a_value_on_a_node_locked_at_restore_time(self, flow_engine: Engine) -> None:
        """A value restore reaches a node that is locked when restore runs; the lock is lifted and reapplied.

        A locked node rejects value sets, so the reconcile has to unlock it before setting the value
        and then restore the lock state afterward, rather than leaving the stale post-capture value
        stuck behind the lock.
        """
        flow_name = self._make_flow(flow_engine)
        self._create_node(flow_engine, flow_name, node_name="ProbeA")
        snapshot = capture_workflow_snapshot(flow_engine)
        assert snapshot is not None

        assert flow_engine.handle_request(
            SetParameterValueRequest(node_name="ProbeA", parameter_name="text", value="hello")
        ).succeeded()
        assert flow_engine.handle_request(SetLockNodeStateRequest(node_name="ProbeA", lock=True)).succeeded()

        node = flow_engine.object_manager.attempt_get_object_by_name_as_type("ProbeA", BaseNode)
        assert node is not None
        assert node.lock is True

        restore_workflow_snapshot(flow_engine, snapshot)
        assert node.lock is False
        value_result = flow_engine.handle_request(GetParameterValueRequest(node_name="ProbeA", parameter_name="text"))
        assert isinstance(value_result, GetParameterValueResultSuccess)
        assert value_result.value == ""

    def test_restore_raises_on_a_fatal_structural_failure(
        self, flow_engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A structural step (node delete) failing raises UndoEntryReplayError rather than leaving a half restore.

        Structural steps stay fatal (unlike the best-effort per-node value/metadata/lock steps)
        because a wrong graph shape cannot be trusted; the caller decides how to react to the raise.
        """
        flow_name = self._make_flow(flow_engine)
        self._create_node(flow_engine, flow_name, node_name="ProbeA")
        snapshot = capture_workflow_snapshot(flow_engine)
        assert snapshot is not None

        self._create_node(flow_engine, flow_name, node_name="ProbeB")
        self._patch_handler(
            monkeypatch,
            DeleteNodeRequest,
            lambda _request: DeleteNodeResultFailure(result_details="forced failure"),
        )

        with pytest.raises(UndoEntryReplayError):
            restore_workflow_snapshot(flow_engine, snapshot)

    # ---------- Fail-closed capture ----------

    def test_capture_returns_none_when_serialization_fails(
        self, flow_engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """capture_workflow_snapshot must return None (not a partial snapshot) when serialization fails."""
        self._make_flow(flow_engine)
        self._patch_handler(
            monkeypatch,
            SerializeFlowToCommandsRequest,
            lambda _request: SerializeFlowToCommandsResultFailure(result_details="forced failure"),
        )
        assert capture_workflow_snapshot(flow_engine) is None

    def test_capture_returns_none_when_serialization_raises(
        self, flow_engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """capture_workflow_snapshot's broad except must swallow a raising handler rather than propagate."""
        self._make_flow(flow_engine)

        def _raise(_request: RequestPayload) -> ResultPayload:
            msg = "boom"
            raise RuntimeError(msg)

        self._patch_handler(monkeypatch, SerializeFlowToCommandsRequest, _raise)
        assert capture_workflow_snapshot(flow_engine) is None

    def test_capture_returns_none_on_partial_value_key_capture(
        self, flow_engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_capture_explicit_value_keys must refuse a partial map rather than let capture proceed with one."""
        flow_name = self._make_flow(flow_engine)
        self._create_node(flow_engine, flow_name, node_name="ProbeA")
        self._create_node(flow_engine, flow_name, node_name="ProbeB")

        object_manager = flow_engine.object_manager
        original = object_manager.attempt_get_object_by_name_as_type

        def _partial(name: str, cast_type: type[object]) -> object | None:
            if name == "ProbeB":
                return None
            return original(name, cast_type)

        monkeypatch.setattr(object_manager, "attempt_get_object_by_name_as_type", _partial)
        assert capture_workflow_snapshot(flow_engine) is None


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
