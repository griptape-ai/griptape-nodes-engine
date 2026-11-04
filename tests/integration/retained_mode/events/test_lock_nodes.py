from __future__ import annotations

from typing import TYPE_CHECKING

import pytest  # type: ignore[reportMissingImports]

from griptape_nodes.retained_mode.events.node_events import (
    BatchSetNodeLockStateRequest,
    BatchSetNodeLockStateResultFailure,
    BatchSetNodeLockStateResultSuccess,
    CreateNodeRequest,
    GetAllNodeInfoRequest,
    GetAllNodeInfoResultSuccess,
    SetLockNodeStateRequest,
    SetLockNodeStateResultFailure,
    SetLockNodeStateResultSuccess,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine


class TestLockNodes:
    def test_lock_without_current_context_returns_failure(self, engine: Engine) -> None:
        # Ensure no current node in context
        ctx = engine.context_manager
        while ctx.has_current_node():
            ctx.pop_node()

        res = engine.handle_request(SetLockNodeStateRequest(node_name=None, lock=True))
        assert isinstance(res, SetLockNodeStateResultFailure)
        assert "Current Context" in str(res.result_details)

    @pytest.fixture
    def create_node(self, engine: Engine) -> str:
        """Create a single test node and return its name."""
        request = CreateNodeRequest(node_type="RunAgentNode", override_parent_flow_name="canvas")
        result = engine.handle_request(request)
        assert hasattr(result, "node_name")
        return result.node_name  # type: ignore[attr-defined]

    @pytest.fixture
    def create_two_nodes(self, engine: Engine) -> tuple[str, str]:
        """Create two test nodes and return their names."""
        r1 = engine.handle_request(CreateNodeRequest(node_type="RunAgentNode", override_parent_flow_name="canvas"))
        r2 = engine.handle_request(CreateNodeRequest(node_type="RunAgentNode", override_parent_flow_name="canvas"))
        assert hasattr(r1, "node_name")
        assert hasattr(r2, "node_name")
        return r1.node_name, r2.node_name  # type: ignore[attr-defined]

    @pytest.mark.xfail(strict=True, reason="Drifted due to not running on CI - see #5237")
    def test_lock_single_node_by_name(self, create_node: str, engine: Engine) -> None:
        node_name = create_node
        # Lock
        lock_req = SetLockNodeStateRequest(lock=True, node_name=node_name)
        lock_res = engine.handle_request(lock_req)
        assert isinstance(lock_res, SetLockNodeStateResultSuccess)
        assert lock_res.locked is True
        assert lock_res.node_name == node_name

        # Verify via GetAllNodeInfo
        info_res = engine.handle_request(GetAllNodeInfoRequest(node_name=node_name))
        assert isinstance(info_res, GetAllNodeInfoResultSuccess)
        assert info_res.locked is True

        # Unlock
        unlock_req = SetLockNodeStateRequest(lock=False, node_name=node_name)
        unlock_res = engine.handle_request(unlock_req)
        assert isinstance(unlock_res, SetLockNodeStateResultSuccess)
        assert unlock_res.locked is False
        assert unlock_res.node_name == node_name

        info_res2 = engine.handle_request(GetAllNodeInfoRequest(node_name=node_name))
        assert isinstance(info_res2, GetAllNodeInfoResultSuccess)
        assert info_res2.locked is False

    @pytest.mark.xfail(strict=True, reason="Drifted due to not running on CI - see #5237")
    def test_lock_multiple_nodes(self, create_two_nodes: tuple[str, str], engine: Engine) -> None:
        node_a, node_b = create_two_nodes

        # Lock both (batch)
        lock_req = BatchSetNodeLockStateRequest(lock=True, node_names=[node_a, node_b])
        lock_res = engine.handle_request(lock_req)
        assert isinstance(lock_res, BatchSetNodeLockStateResultSuccess)
        assert lock_res.updated_nodes == [node_a, node_b]
        assert lock_res.failed_nodes == {}

        # Verify both locked
        info_a = engine.handle_request(GetAllNodeInfoRequest(node_name=node_a))
        info_b = engine.handle_request(GetAllNodeInfoRequest(node_name=node_b))
        assert isinstance(info_a, GetAllNodeInfoResultSuccess)
        assert isinstance(info_b, GetAllNodeInfoResultSuccess)
        assert info_a.locked is True
        assert info_b.locked is True

        # Unlock both (batch)
        unlock_req = BatchSetNodeLockStateRequest(lock=False, node_names=[node_a, node_b])
        unlock_res = engine.handle_request(unlock_req)
        assert isinstance(unlock_res, BatchSetNodeLockStateResultSuccess)
        assert unlock_res.updated_nodes == [node_a, node_b]

        info_a2 = engine.handle_request(GetAllNodeInfoRequest(node_name=node_a))
        info_b2 = engine.handle_request(GetAllNodeInfoRequest(node_name=node_b))
        assert isinstance(info_a2, GetAllNodeInfoResultSuccess)
        assert isinstance(info_b2, GetAllNodeInfoResultSuccess)
        assert info_a2.locked is False
        assert info_b2.locked is False

    @pytest.mark.xfail(strict=True, reason="Drifted due to not running on CI - see #5237")
    def test_lock_with_current_context_when_name_missing(self, engine: Engine) -> None:
        # Create a node and set as current context
        create_req = CreateNodeRequest(
            node_type="RunAgentNode", override_parent_flow_name="canvas", set_as_new_context=True
        )
        create_res = engine.handle_request(create_req)
        assert hasattr(create_res, "node_name")
        node_name = create_res.node_name  # type: ignore[attr-defined]

        # Lock without specifying node_name
        lock_res = engine.handle_request(SetLockNodeStateRequest(node_name=None, lock=True))
        assert isinstance(lock_res, SetLockNodeStateResultSuccess)
        assert lock_res.locked is True
        # In current-context path, node_name should be populated for backward compatibility
        assert lock_res.node_name == node_name

        info_res = engine.handle_request(GetAllNodeInfoRequest(node_name=node_name))
        assert isinstance(info_res, GetAllNodeInfoResultSuccess)
        assert info_res.locked is True

    @pytest.mark.xfail(strict=True, reason="Drifted due to not running on CI - see #5237")
    def test_lock_multiple_with_missing_node_returns_failure(self, create_node: str, engine: Engine) -> None:
        existing = create_node
        missing = "does_not_exist_123"

        res = engine.handle_request(BatchSetNodeLockStateRequest(lock=True, node_names=[existing, missing]))
        assert isinstance(res, BatchSetNodeLockStateResultFailure)
        # Ensure the error mentions the missing node
        assert missing in str(res.result_details)

    def test_lock_single_missing_node_returns_failure(self, engine: Engine) -> None:
        missing = "not_here_456"
        res = engine.handle_request(SetLockNodeStateRequest(lock=True, node_name=missing))
        assert isinstance(res, SetLockNodeStateResultFailure)
        assert missing in str(res.result_details)
