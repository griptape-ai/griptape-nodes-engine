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

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.events.execution_events import NodeMetadata
    from griptape_nodes.retained_mode.managers.node_manager import NodeManager


class TestExecuteNodeStrictMode:
    def _get_node_manager(self) -> NodeManager:
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        return GriptapeNodes.NodeManager()

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
        return m

    @pytest.mark.asyncio
    async def test_orchestrator_violation_stays_success(self) -> None:
        node = self._make_mock_node(aprocess_reports=True)
        obj_mgr = self._make_mock_obj_mgr(existing_node=node)
        lib_mgr = self._make_mock_library_manager(is_worker=False)

        with (
            patch(
                "griptape_nodes.retained_mode.managers.node_manager.GriptapeNodes.ObjectManager",
                return_value=obj_mgr,
            ),
            patch(
                "griptape_nodes.retained_mode.managers.node_manager.GriptapeNodes.LibraryManager",
                return_value=lib_mgr,
            ),
            patch(
                "griptape_nodes.retained_mode.managers.node_manager.GriptapeNodes.WorkerManager",
                return_value=None,
            ),
        ):
            request = ExecuteNodeRequest(node_name="n", node_metadata=cast("NodeMetadata", {"node_type": "T"}))
            result = await self._get_node_manager().on_execute_node_request(request)

        assert isinstance(result, ExecuteNodeResultSuccess)
        node.aprocess.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_worker_violation_elevates_to_failure(self) -> None:
        node = self._make_mock_node(aprocess_reports=True)
        lib_mgr = self._make_mock_library_manager(is_worker=True)
        node_manager = self._get_node_manager()

        with (
            patch(
                "griptape_nodes.retained_mode.managers.node_manager.GriptapeNodes.LibraryManager",
                return_value=lib_mgr,
            ),
            patch.object(node_manager, "_materialize_transient_node_from_metadata", return_value=node),
        ):
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
        node_manager = self._get_node_manager()

        with (
            patch(
                "griptape_nodes.retained_mode.managers.node_manager.GriptapeNodes.LibraryManager",
                return_value=lib_mgr,
            ),
            patch.object(node_manager, "_materialize_transient_node_from_metadata", return_value=node),
        ):
            request = ExecuteNodeRequest(node_name="n", node_metadata=cast("NodeMetadata", {"node_type": "T"}))
            result = await node_manager.on_execute_node_request(request)

        assert isinstance(result, ExecuteNodeResultSuccess)
        node.aprocess.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_orchestrator_no_violations_unchanged(self) -> None:
        node = self._make_mock_node(aprocess_reports=False)
        obj_mgr = self._make_mock_obj_mgr(existing_node=node)
        lib_mgr = self._make_mock_library_manager(is_worker=False)

        with (
            patch(
                "griptape_nodes.retained_mode.managers.node_manager.GriptapeNodes.ObjectManager",
                return_value=obj_mgr,
            ),
            patch(
                "griptape_nodes.retained_mode.managers.node_manager.GriptapeNodes.LibraryManager",
                return_value=lib_mgr,
            ),
            patch(
                "griptape_nodes.retained_mode.managers.node_manager.GriptapeNodes.WorkerManager",
                return_value=None,
            ),
        ):
            request = ExecuteNodeRequest(node_name="n", node_metadata=cast("NodeMetadata", {"node_type": "T"}))
            result = await self._get_node_manager().on_execute_node_request(request)

        assert isinstance(result, ExecuteNodeResultSuccess)
        node.aprocess.assert_awaited_once()
