"""Handler-level tests for strict-mode routing in on_execute_node_request.

Uses a fixture runtime detector that calls ``STRICT_MODE.report`` inside
``node.aprocess`` to simulate a rule firing during execution. The scope
wrapper on ``on_execute_node_request`` is then responsible for turning
worker violations into ``ExecuteNodeResultFailure`` and leaving
orchestrator violations alone.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.common.strict_mode import STRICT_MODE
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.retained_mode.events.execution_events import (
    ExecuteNodeRequest,
    ExecuteNodeResultFailure,
    ExecuteNodeResultSuccess,
)
from griptape_nodes.retained_mode.managers.node_manager import NodeManager

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.events.execution_events import NodeMetadata


class TestExecuteNodeStrictMode:
    def _make_node_manager(
        self,
        *,
        object_manager: MagicMock | None = None,
        library_manager: MagicMock | None = None,
        worker_manager: MagicMock | None = None,
    ) -> NodeManager:
        """Build a NodeManager wired to a mock engine instead of the process-wide facade."""
        mock_engine = MagicMock()
        mock_engine.object_manager = object_manager
        mock_engine.library_manager = library_manager
        mock_engine.worker_manager = worker_manager
        return NodeManager(MagicMock(), engine=mock_engine)

    def _make_mock_node(self, *, aprocess_reports: bool = False) -> MagicMock:
        node = MagicMock(spec=BaseNode)
        node.name = "n"
        node.parameter_values = {}
        node.parameter_output_values = {"out": 1}
        node.metadata = {"library": "libA"}
        node._cancellation_requested = threading.Event()
        node.parameters = []

        async def _aprocess() -> None:
            if aprocess_reports:
                STRICT_MODE.report(rule_id="fixture-rule", message="fixture violation")

        node.aprocess = AsyncMock(side_effect=_aprocess)
        return node

    def _make_mock_obj_mgr(self, existing_node: MagicMock) -> MagicMock:
        m = MagicMock()
        m.attempt_get_object_by_name_as_type.return_value = existing_node
        return m

    def _make_mock_library_manager(self, *, is_worker: bool) -> MagicMock:
        m = MagicMock()
        m.is_worker = is_worker
        m._is_worker = is_worker
        m.get_worker_for_library.return_value = None
        # Awaited on the orchestrator route before a node is routed, so it has to be a coroutine
        # rather than a plain MagicMock attribute.
        m.wait_for_worker_library_load = AsyncMock(return_value=None)
        return m

    @pytest.mark.asyncio
    async def test_orchestrator_violation_stays_success(self) -> None:
        node = self._make_mock_node(aprocess_reports=True)
        obj_mgr = self._make_mock_obj_mgr(existing_node=node)
        lib_mgr = self._make_mock_library_manager(is_worker=False)
        node_manager = self._make_node_manager(object_manager=obj_mgr, library_manager=lib_mgr)

        request = ExecuteNodeRequest(node_name="n", node_metadata=cast("NodeMetadata", {"node_type": "T"}))
        result = await node_manager.on_execute_node_request(request)

        assert isinstance(result, ExecuteNodeResultSuccess)
        node.aprocess.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_worker_violation_elevates_to_failure(self) -> None:
        node = self._make_mock_node(aprocess_reports=True)
        lib_mgr = self._make_mock_library_manager(is_worker=True)
        node_manager = self._make_node_manager(library_manager=lib_mgr)

        with patch.object(node_manager, "_materialize_transient_node_from_metadata", return_value=node):
            request = ExecuteNodeRequest(node_name="n", node_metadata=cast("NodeMetadata", {"node_type": "T"}))
            result = await node_manager.on_execute_node_request(request)

        assert isinstance(result, ExecuteNodeResultFailure)
        details = str(result.result_details)
        assert "fixture-rule" in details
        assert "fixture violation" in details
        node.aprocess.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_worker_no_violations_unchanged(self) -> None:
        node = self._make_mock_node(aprocess_reports=False)
        lib_mgr = self._make_mock_library_manager(is_worker=True)
        node_manager = self._make_node_manager(library_manager=lib_mgr)

        with patch.object(node_manager, "_materialize_transient_node_from_metadata", return_value=node):
            request = ExecuteNodeRequest(node_name="n", node_metadata=cast("NodeMetadata", {"node_type": "T"}))
            result = await node_manager.on_execute_node_request(request)

        assert isinstance(result, ExecuteNodeResultSuccess)
        node.aprocess.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_orchestrator_no_violations_unchanged(self) -> None:
        node = self._make_mock_node(aprocess_reports=False)
        obj_mgr = self._make_mock_obj_mgr(existing_node=node)
        lib_mgr = self._make_mock_library_manager(is_worker=False)
        node_manager = self._make_node_manager(object_manager=obj_mgr, library_manager=lib_mgr)

        request = ExecuteNodeRequest(node_name="n", node_metadata=cast("NodeMetadata", {"node_type": "T"}))
        result = await node_manager.on_execute_node_request(request)

        assert isinstance(result, ExecuteNodeResultSuccess)
        node.aprocess.assert_awaited_once()
