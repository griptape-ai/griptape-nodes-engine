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


class TestUnlockUnresolvesConsumers:
    """Unlocking a node without resolved outputs must unresolve its consumers.

    While locked, the node is skipped and its consumers resolve on missing results. If they stay
    RESOLVED, a later run stops at them and never reaches the unlocked node.
    """

    @staticmethod
    def _patch_node(
        engine: Engine, monkeypatch: pytest.MonkeyPatch, state: NodeResolutionState
    ) -> tuple[MagicMock, MagicMock]:
        node = MagicMock(spec=BaseNode)
        node.name = "locked"
        node.lock = True
        node.state = state
        connections = MagicMock()
        monkeypatch.setattr(engine.node_manager, "get_node_by_name", lambda _name: node)
        monkeypatch.setattr(engine.flow_manager, "get_connections", lambda: connections)
        return node, connections

    @pytest.mark.parametrize("batch", [False, True])
    def test_unlocking_unresolved_node_unresolves_consumers(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, *, batch: bool
    ) -> None:
        node, connections = self._patch_node(engine, monkeypatch, NodeResolutionState.UNRESOLVED)

        if batch:
            engine.handle_request(BatchSetNodeLockStateRequest(node_names=["locked"], lock=False))
        else:
            engine.handle_request(SetLockNodeStateRequest(node_name="locked", lock=False))

        assert node.lock is False
        connections.unresolve_future_nodes.assert_called_once_with(node)

    def test_unlocking_resolved_node_keeps_consumers(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        _, connections = self._patch_node(engine, monkeypatch, NodeResolutionState.RESOLVED)

        engine.handle_request(SetLockNodeStateRequest(node_name="locked", lock=False))

        connections.unresolve_future_nodes.assert_not_called()

    def test_locking_node_keeps_consumers(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        _, connections = self._patch_node(engine, monkeypatch, NodeResolutionState.UNRESOLVED)

        engine.handle_request(SetLockNodeStateRequest(node_name="locked", lock=True))

        connections.unresolve_future_nodes.assert_not_called()
