from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode, NodeResolutionState
from griptape_nodes.node_library.library_registry import (
    LibraryMetadata,
    LibraryRegistry,
    LibrarySchema,
    NodeMetadata,
)
from griptape_nodes.retained_mode.events.connection_events import CreateConnectionRequest, CreateConnectionResultSuccess
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.node_events import (
    BatchSetNodeLockStateRequest,
    BatchSetNodeLockStateResultFailure,
    CreateNodeRequest,
    CreateNodeResultSuccess,
    SetLockNodeStateRequest,
    SetLockNodeStateResultFailure,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.retained_mode import RetainedMode

if TYPE_CHECKING:
    from collections.abc import Generator

    from griptape_nodes.exe_types.node_types import BaseNode
    from griptape_nodes.retained_mode.engine import Engine

_LIBRARY_NAME = "retained-mode-lock-test-library"


class _ValueNode(DataNode):
    """Minimal DataNode with a connectable "value" parameter."""

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="value",
                tooltip="Value to echo",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY, ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        self.parameter_output_values["value"] = self.get_parameter_value("value")


class TestRetainedModeLock:
    def test_lock_without_current_context_returns_failure(self, engine: Engine) -> None:
        # Ensure empty current context for nodes
        ctx = engine.context_manager
        while ctx.has_current_node():
            ctx.pop_node()
        res = engine.handle_request(SetLockNodeStateRequest(node_name=None, lock=True))
        assert isinstance(res, SetLockNodeStateResultFailure)
        assert "Current Context" in str(res.result_details)

    def test_lock_all_missing_nodes_failure(self) -> None:
        missing_nodes = ["nope_a_123", "nope_b_456"]
        res = RetainedMode.batch_set_lock_node_state(node_names=missing_nodes, lock=True)
        assert isinstance(res, BatchSetNodeLockStateResultFailure)
        details = str(res.result_details)
        assert "Failed to update any nodes" in details


class TestUnlockUnresolvesNodeLeftUnexecuted:
    """Unlocking a node that has not resolved by executing must unresolve it and its consumers.

    While locked, a run passes through the node and marks it RESOLVED without executing it, and its
    consumers resolve on whatever it held. If either stays RESOLVED, a later run stops there and
    never executes the unlocked node.
    """

    @pytest.fixture
    def nodes(self, engine: Engine) -> Generator[tuple[BaseNode, BaseNode], None, None]:
        """A locked node wired into a consumer, both RESOLVED, in a fresh flow."""
        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        LibraryRegistry._clear()
        schema = LibrarySchema(
            name=_LIBRARY_NAME,
            library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
            metadata=LibraryMetadata(
                author="test", description="test", library_version="0.1.0", engine_version="0.0.0", tags=[]
            ),
            categories=[],
            nodes=[],
        )
        library = LibraryRegistry.generate_new_library(schema)
        library.register_new_node_type(_ValueNode, NodeMetadata(category="test", description="d", display_name="Value"))
        engine.context_manager.push_workflow(workflow_name="lock_wf")
        flow = engine.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="main", set_as_new_context=True)
        )
        assert isinstance(flow, CreateFlowResultSuccess), flow

        for name in ("locked", "consumer"):
            created = engine.handle_request(
                CreateNodeRequest(node_type="_ValueNode", specific_library_name=_LIBRARY_NAME, node_name=name)
            )
            assert isinstance(created, CreateNodeResultSuccess), created
        connected = engine.handle_request(
            CreateConnectionRequest(
                source_node_name="locked",
                source_parameter_name="value",
                target_node_name="consumer",
                target_parameter_name="value",
            )
        )
        assert isinstance(connected, CreateConnectionResultSuccess), connected
        engine.handle_request(SetLockNodeStateRequest(node_name="locked", lock=True))

        locked = engine.node_manager.get_node_by_name("locked")
        consumer = engine.node_manager.get_node_by_name("consumer")
        locked.state = NodeResolutionState.RESOLVED
        consumer.state = NodeResolutionState.RESOLVED
        try:
            yield locked, consumer
        finally:
            engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
            LibraryRegistry._clear()

    @pytest.mark.parametrize("batch", [False, True])
    @pytest.mark.parametrize(
        ("state", "resolved_while_locked"),
        [
            pytest.param(NodeResolutionState.RESOLVED, True, id="resolved-by-passing-through"),
            pytest.param(NodeResolutionState.UNRESOLVED, False, id="unresolved"),
        ],
    )
    def test_unlocking_unresolves_node_and_consumers(
        self,
        engine: Engine,
        nodes: tuple[BaseNode, BaseNode],
        state: NodeResolutionState,
        *,
        resolved_while_locked: bool,
        batch: bool,
    ) -> None:
        locked, consumer = nodes
        locked.state = state
        locked.resolved_while_locked = resolved_while_locked

        if batch:
            engine.handle_request(BatchSetNodeLockStateRequest(node_names=["locked"], lock=False))
        else:
            engine.handle_request(SetLockNodeStateRequest(node_name="locked", lock=False))

        assert locked.lock is False
        assert locked.resolved_while_locked is False
        assert locked.state == NodeResolutionState.UNRESOLVED
        assert consumer.state == NodeResolutionState.UNRESOLVED

    def test_unlocking_node_that_executed_keeps_it_resolved(
        self, engine: Engine, nodes: tuple[BaseNode, BaseNode]
    ) -> None:
        locked, consumer = nodes

        engine.handle_request(SetLockNodeStateRequest(node_name="locked", lock=False))

        assert locked.state == NodeResolutionState.RESOLVED
        assert consumer.state == NodeResolutionState.RESOLVED

    def test_locking_node_keeps_it_resolved(self, engine: Engine, nodes: tuple[BaseNode, BaseNode]) -> None:
        locked, consumer = nodes
        locked.resolved_while_locked = True

        engine.handle_request(SetLockNodeStateRequest(node_name="locked", lock=True))

        assert locked.state == NodeResolutionState.RESOLVED
        assert consumer.state == NodeResolutionState.RESOLVED
