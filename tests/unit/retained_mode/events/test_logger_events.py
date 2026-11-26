"""Tests for LogHandlerEvent."""

from griptape_nodes.retained_mode.events.logger_events import LogHandlerEvent


class TestLogHandlerEvent:
    def test_node_name_defaults_to_none(self) -> None:
        event = LogHandlerEvent(message="hello", levelname="INFO", created=1234567890.0)

        assert event.node_name is None
