"""Tests for GriptapeNodes.handle_request and ahandle_request broadcast behavior."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from griptape_nodes.retained_mode.events.base_events import RequestPayload
from griptape_nodes.retained_mode.events.os_events import ReadFileRequest
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes


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
