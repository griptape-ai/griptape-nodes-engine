"""Tests for GriptapeNodes.handle_request and ahandle_request broadcast behavior, and its manager accessors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from griptape_nodes.common.strict_mode import STRICT_MODE, StrictModeScopeKind, StrictModeSeverity
from griptape_nodes.retained_mode.events.base_events import RequestPayload
from griptape_nodes.retained_mode.events.os_events import ReadFileRequest
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine


@dataclass(kw_only=True)
class _BroadcastingRequest(RequestPayload):
    """Minimal request with the default broadcast_result=True for testing."""


class TestHandleRequestBroadcast:
    def test_broadcasts_result_when_broadcast_result_is_true(self) -> None:
        """handle_request queues a GriptapeNodeEvent when broadcast_result=True."""
        event_mgr = GriptapeNodes.EventManager()
        mock_result_event = MagicMock()

        with (
            patch.object(event_mgr, "handle_request", return_value=mock_result_event),
            patch.object(event_mgr, "should_suppress_event", return_value=False),
            patch.object(event_mgr, "put_event") as mock_put,
            # GriptapeNodeEvent is a Pydantic model that validates its wrapped_event arg;
            # patching it here prevents the Pydantic validation from rejecting the MagicMock.
            patch("griptape_nodes.retained_mode.engine.GriptapeNodeEvent"),
        ):
            GriptapeNodes.handle_request(_BroadcastingRequest())

        mock_put.assert_called_once()

    def test_skips_broadcast_when_broadcast_result_is_false(self) -> None:
        """handle_request does not queue when request.broadcast_result=False."""
        event_mgr = GriptapeNodes.EventManager()
        mock_result_event = MagicMock()

        with (
            patch.object(event_mgr, "handle_request", return_value=mock_result_event),
            patch.object(event_mgr, "should_suppress_event", return_value=False),
            patch.object(event_mgr, "put_event") as mock_put,
        ):
            GriptapeNodes.handle_request(ReadFileRequest())

        mock_put.assert_not_called()

    def test_instance_broadcast_result_overrides_class_default(self) -> None:
        """broadcast_result set on the instance at construction time is respected."""
        event_mgr = GriptapeNodes.EventManager()
        mock_result_event = MagicMock()

        with (
            patch.object(event_mgr, "handle_request", return_value=mock_result_event),
            patch.object(event_mgr, "should_suppress_event", return_value=False),
            patch.object(event_mgr, "put_event") as mock_put,
            patch("griptape_nodes.retained_mode.engine.GriptapeNodeEvent"),
        ):
            GriptapeNodes.handle_request(ReadFileRequest(broadcast_result=True))

        mock_put.assert_called_once()

    def test_skips_broadcast_when_event_is_suppressed(self) -> None:
        """handle_request does not queue when the event is suppressed."""
        event_mgr = GriptapeNodes.EventManager()
        mock_result_event = MagicMock()

        with (
            patch.object(event_mgr, "handle_request", return_value=mock_result_event),
            patch.object(event_mgr, "should_suppress_event", return_value=True),
            patch.object(event_mgr, "put_event") as mock_put,
        ):
            GriptapeNodes.handle_request(_BroadcastingRequest())

        mock_put.assert_not_called()


class TestAHandleRequestBroadcast:
    @pytest.mark.asyncio
    async def test_broadcasts_result_when_broadcast_result_is_true(self) -> None:
        """ahandle_request queues a GriptapeNodeEvent when broadcast_result=True."""
        event_mgr = GriptapeNodes.EventManager()
        mock_result_event = MagicMock()

        with (
            patch.object(event_mgr, "ahandle_request", return_value=mock_result_event),
            patch.object(event_mgr, "should_suppress_event", return_value=False),
            patch.object(event_mgr, "aput_event") as mock_aput,
            patch("griptape_nodes.retained_mode.engine.GriptapeNodeEvent"),
        ):
            await GriptapeNodes.ahandle_request(_BroadcastingRequest())

        mock_aput.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_broadcast_when_broadcast_result_is_false(self) -> None:
        """ahandle_request does not queue when request.broadcast_result=False."""
        event_mgr = GriptapeNodes.EventManager()
        mock_result_event = MagicMock()

        with (
            patch.object(event_mgr, "ahandle_request", return_value=mock_result_event),
            patch.object(event_mgr, "should_suppress_event", return_value=False),
            patch.object(event_mgr, "aput_event") as mock_aput,
        ):
            await GriptapeNodes.ahandle_request(ReadFileRequest())

        mock_aput.assert_not_called()

    @pytest.mark.asyncio
    async def test_instance_broadcast_result_overrides_class_default(self) -> None:
        """broadcast_result set on the instance at construction time is respected."""
        event_mgr = GriptapeNodes.EventManager()
        mock_result_event = MagicMock()

        with (
            patch.object(event_mgr, "ahandle_request", return_value=mock_result_event),
            patch.object(event_mgr, "should_suppress_event", return_value=False),
            patch.object(event_mgr, "aput_event") as mock_aput,
            patch("griptape_nodes.retained_mode.engine.GriptapeNodeEvent"),
        ):
            await GriptapeNodes.ahandle_request(ReadFileRequest(broadcast_result=True))

        mock_aput.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_broadcast_when_event_is_suppressed(self) -> None:
        """ahandle_request does not queue when the event is suppressed."""
        event_mgr = GriptapeNodes.EventManager()
        mock_result_event = MagicMock()

        with (
            patch.object(event_mgr, "ahandle_request", return_value=mock_result_event),
            patch.object(event_mgr, "should_suppress_event", return_value=True),
            patch.object(event_mgr, "aput_event") as mock_aput,
        ):
            await GriptapeNodes.ahandle_request(_BroadcastingRequest())

        mock_aput.assert_not_called()


class TestStateReadBypassingRequests:
    """Strict-mode rule `state-read-bypassing-requests`: a node reaching for a manager while it runs."""

    def test_a_manager_read_while_a_node_runs_in_process_is_reported_and_allowed(self, engine: Engine) -> None:
        with (
            STRICT_MODE.open_scope(
                kind=StrictModeScopeKind.RUNTIME_EXECUTE, subject="Runner", library_name=None, is_worker=False
            ) as scope,
            engine.event_manager.node_execution_scope(),
        ):
            config_manager = GriptapeNodes.ConfigManager()

        assert config_manager is engine.config_manager
        assert [violation.rule_id for violation in scope.violations] == ["state-read-bypassing-requests"]
        assert scope.violations[0].severity is StrictModeSeverity.WARNING
        assert "ConfigManager" in scope.violations[0].message
        assert "GetConfigValueRequest" in scope.violations[0].message

    def test_a_manager_read_while_a_node_runs_in_a_worker_is_reported_and_refused(self, engine: Engine) -> None:
        engine.library_manager._is_worker = True

        with (
            STRICT_MODE.open_scope(
                kind=StrictModeScopeKind.RUNTIME_EXECUTE, subject="Runner", library_name=None, is_worker=True
            ) as scope,
            engine.event_manager.node_execution_scope(),
            pytest.raises(RuntimeError, match="GetConfigValueRequest"),
        ):
            GriptapeNodes.ConfigManager()

        assert [violation.rule_id for violation in scope.violations] == ["state-read-bypassing-requests"]
        assert scope.violations[0].severity is StrictModeSeverity.ERROR

    def test_a_manager_read_outside_a_run_is_not_reported(self, engine: Engine) -> None:
        """Engine boot and library load legitimately use their own managers."""
        with STRICT_MODE.open_scope(
            kind=StrictModeScopeKind.RUNTIME_EXECUTE, subject="Runner", library_name=None, is_worker=False
        ) as scope:
            GriptapeNodes.ConfigManager()

        assert engine.event_manager.in_node_execution() is False
        assert scope.violations == []

    def test_a_manager_read_while_a_worker_loads_its_copy_is_allowed_while_another_node_runs(
        self, engine: Engine
    ) -> None:
        """The load answers from the worker's own graph, whatever else the worker is doing."""
        engine.library_manager._is_worker = True

        with (
            STRICT_MODE.open_scope(
                kind=StrictModeScopeKind.RUNTIME_EXECUTE, subject="Loading", library_name=None, is_worker=True
            ) as scope,
            engine.event_manager.node_execution_scope(),
            engine.event_manager.rebuild_scope("Loading"),
        ):
            config_manager = GriptapeNodes.ConfigManager()

        assert config_manager is engine.config_manager
        assert scope.violations == []
