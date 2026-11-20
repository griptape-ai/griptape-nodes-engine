"""Tests for the engine state a client connected to a session reads from the session's requests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from griptape_nodes.retained_mode.engine_lifecycle import EngineLifecycleState
from griptape_nodes.retained_mode.events.app_events import (
    AppGetSessionRequest,
    AppGetSessionResultSuccess,
    SessionHeartbeatRequest,
    SessionHeartbeatResultFailure,
    SessionHeartbeatResultSuccess,
)
from griptape_nodes.retained_mode.managers.os_manager import OSManager
from griptape_nodes.utils.version_utils import engine_version

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine


@pytest.fixture
def session_engine(engine: Engine) -> Engine:
    """An engine holding a session, which the heartbeat requires."""
    engine._session_manager.save_session("session-hb")
    return engine


class TestGetSessionSnapshot:
    def test_reports_the_session(self, session_engine: Engine) -> None:
        result = session_engine.handle_get_session_request(AppGetSessionRequest())

        assert isinstance(result, AppGetSessionResultSuccess)
        assert result.session_id == "session-hb"

    def test_reports_no_session_when_none_is_active(self, engine: Engine) -> None:
        result = engine.handle_get_session_request(AppGetSessionRequest())

        assert isinstance(result, AppGetSessionResultSuccess)
        assert result.session_id is None

    def test_reports_the_engine(self, session_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(OSManager, "platform_name", staticmethod(lambda: "sentinel-os"))

        result = session_engine.handle_get_session_request(AppGetSessionRequest())

        assert isinstance(result, AppGetSessionResultSuccess)
        assert result.engine_version == engine_version
        assert result.engine_run_id == session_engine.engine_run_id
        assert result.engine_os == "sentinel-os"

    @pytest.mark.parametrize("state", list(EngineLifecycleState))
    def test_reads_the_lifecycle(self, session_engine: Engine, state: EngineLifecycleState) -> None:
        session_engine.lifecycle._state = state

        result = session_engine.handle_get_session_request(AppGetSessionRequest())

        assert isinstance(result, AppGetSessionResultSuccess)
        assert result.is_initializing is (state is not EngineLifecycleState.READY)


class TestSessionHeartbeat:
    def test_fails_without_an_active_session(self, engine: Engine) -> None:
        result = engine.handle_session_heartbeat_request(SessionHeartbeatRequest())

        assert isinstance(result, SessionHeartbeatResultFailure)

    def test_reports_the_session_and_run(self, session_engine: Engine) -> None:
        result = session_engine.handle_session_heartbeat_request(SessionHeartbeatRequest())

        assert isinstance(result, SessionHeartbeatResultSuccess)
        assert result.session_id == "session-hb"
        assert result.engine_run_id == session_engine.engine_run_id

    @pytest.mark.parametrize("state", list(EngineLifecycleState))
    def test_reads_the_lifecycle(self, session_engine: Engine, state: EngineLifecycleState) -> None:
        session_engine.lifecycle._state = state

        result = session_engine.handle_session_heartbeat_request(SessionHeartbeatRequest())

        assert isinstance(result, SessionHeartbeatResultSuccess)
        assert result.is_initializing is (state is not EngineLifecycleState.READY)
