from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from griptape_nodes.exe_types.node_types import BaseNode, NodeResolutionState
from griptape_nodes.retained_mode.events.node_events import (
    BatchSetNodeLockStateRequest,
    BatchSetNodeLockStateResultFailure,
    SetLockNodeStateRequest,
    SetLockNodeStateResultFailure,
)
from griptape_nodes.retained_mode.retained_mode import RetainedMode

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine


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

    @staticmethod
    def _patch_node(
        engine: Engine,
        monkeypatch: pytest.MonkeyPatch,
        state: NodeResolutionState,
        *,
        resolved_while_locked: bool,
    ) -> tuple[MagicMock, MagicMock]:
        node = MagicMock(spec=BaseNode)
        node.name = "locked"
        node.lock = True
        node.state = state
        node.resolved_while_locked = resolved_while_locked
        connections = MagicMock()
        monkeypatch.setattr(engine.node_manager, "get_node_by_name", lambda _name: node)
        monkeypatch.setattr(engine.flow_manager, "get_connections", lambda: connections)
        return node, connections

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
        monkeypatch: pytest.MonkeyPatch,
        state: NodeResolutionState,
        *,
        resolved_while_locked: bool,
        batch: bool,
    ) -> None:
        node, connections = self._patch_node(engine, monkeypatch, state, resolved_while_locked=resolved_while_locked)

        if batch:
            engine.handle_request(BatchSetNodeLockStateRequest(node_names=["locked"], lock=False))
        else:
            engine.handle_request(SetLockNodeStateRequest(node_name="locked", lock=False))

        assert node.lock is False
        assert node.resolved_while_locked is False
        node.make_node_unresolved.assert_called_once()
        connections.unresolve_future_nodes.assert_called_once_with(node)

    def test_unlocking_node_that_executed_keeps_it_resolved(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        node, connections = self._patch_node(
            engine, monkeypatch, NodeResolutionState.RESOLVED, resolved_while_locked=False
        )

        engine.handle_request(SetLockNodeStateRequest(node_name="locked", lock=False))

        node.make_node_unresolved.assert_not_called()
        connections.unresolve_future_nodes.assert_not_called()

    def test_locking_node_keeps_it_resolved(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        node, connections = self._patch_node(
            engine, monkeypatch, NodeResolutionState.RESOLVED, resolved_while_locked=True
        )

        engine.handle_request(SetLockNodeStateRequest(node_name="locked", lock=True))

        node.make_node_unresolved.assert_not_called()
        connections.unresolve_future_nodes.assert_not_called()
