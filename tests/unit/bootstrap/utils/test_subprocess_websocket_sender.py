"""Tests for SubprocessWebSocketSenderMixin's explicit identity stamp."""

from griptape_nodes.bootstrap.utils.subprocess_websocket_sender import SubprocessWebSocketSenderMixin
from griptape_nodes.retained_mode.engine import Engine, engine_scope
from griptape_nodes.retained_mode.events.base_events import ExecutionEvent
from griptape_nodes.retained_mode.events.execution_events import ControlFlowCancelledEvent


class _Sender(SubprocessWebSocketSenderMixin):
    """Minimal instance for exercising send_engine_event without a real WebSocket connection."""

    def __init__(self, session_id: str) -> None:
        self._init_websocket_sender(session_id)


class TestSendEngineEvent:
    """send_engine_event bypasses the EventManager queue, so it has to stamp identity itself."""

    def test_send_engine_event_stamps_engine_and_session_identity(self) -> None:
        engine = Engine()
        engine.engine_identity_manager.active_engine_id = "engine-subprocess"
        engine.session_manager.active_session_id = "session-subprocess"

        event = ExecutionEvent(payload=ControlFlowCancelledEvent())
        assert event.engine_id is None
        assert event.session_id is None

        with engine_scope(engine):
            _Sender("session-subprocess").send_engine_event("execution_event", event)

        assert event.engine_id == "engine-subprocess"
        assert event.session_id == "session-subprocess"
