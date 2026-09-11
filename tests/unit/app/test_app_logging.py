"""Tests for the engine-role log filter and handler in engine_log.py."""

from __future__ import annotations

import io
import logging
import re

import pytest
from rich.console import Console
from rich.table import Table
from rich.text import Text

from griptape_nodes.app.engine_log import _EngineRoleFilter, _EngineRoleHandler, _prefix_to_color


def _make_record(prefix: str = "", msg: str = "test message") -> logging.LogRecord:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg=msg,
        args=(),
        exc_info=None,
    )
    record.engine_prefix = prefix  # type: ignore[attr-defined]
    return record


class TestPrefixToColor:
    def test_returns_hex_color_string(self) -> None:
        color = _prefix_to_color("Orchestrator")

        assert re.match(r"^#[0-9a-f]{6}$", color)

    def test_is_deterministic(self) -> None:
        assert _prefix_to_color("Worker-abc") == _prefix_to_color("Worker-abc")

    def test_different_prefixes_produce_different_colors(self) -> None:
        assert _prefix_to_color("Orchestrator") != _prefix_to_color("Worker-abc123")

    def test_empty_prefix_returns_hex_color(self) -> None:
        color = _prefix_to_color("")

        assert re.match(r"^#[0-9a-f]{6}$", color)


class TestEngineRoleFilter:
    def test_default_prefix_is_empty_string(self) -> None:
        f = _EngineRoleFilter()

        assert f.prefix == ""

    def test_filter_sets_engine_prefix_on_record(self) -> None:
        f = _EngineRoleFilter()
        f.prefix = "Orchestrator"
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0, msg="hi", args=(), exc_info=None
        )

        f.filter(record)

        assert record.engine_prefix == "Orchestrator"  # type: ignore[attr-defined]

    def test_filter_returns_true(self) -> None:
        f = _EngineRoleFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0, msg="hi", args=(), exc_info=None
        )

        assert f.filter(record) is True

    def test_filter_reflects_prefix_change(self) -> None:
        f = _EngineRoleFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0, msg="hi", args=(), exc_info=None
        )
        f.prefix = "Worker-xyz"

        f.filter(record)

        assert record.engine_prefix == "Worker-xyz"  # type: ignore[attr-defined]


class TestEngineRoleHandlerRender:
    @pytest.fixture
    def handler(self) -> _EngineRoleHandler:
        return _EngineRoleHandler(show_time=True, show_path=False, markup=False, rich_tracebacks=True)

    def test_render_without_prefix_returns_a_value(self, handler: _EngineRoleHandler) -> None:
        record = _make_record(prefix="")

        result = handler.render(record=record, traceback=None, message_renderable=Text("hello"))

        assert result is not None

    def test_render_with_prefix_returns_table(self, handler: _EngineRoleHandler) -> None:
        record = _make_record(prefix="Orchestrator")

        result = handler.render(record=record, traceback=None, message_renderable=Text("hello"))

        assert isinstance(result, Table)

    def test_render_with_prefix_and_no_traceback_returns_table(self, handler: _EngineRoleHandler) -> None:
        record = _make_record(prefix="Worker-abc12345")

        result = handler.render(record=record, traceback=None, message_renderable=Text("error"))

        assert isinstance(result, Table)

    def test_render_with_prefix_and_traceback_returns_table(self, handler: _EngineRoleHandler) -> None:
        # The render method uses `traceback: object` and cast() is a no-op at runtime,
        # so any ConsoleRenderable (e.g. Text) is valid as a stand-in for a real Traceback.
        record = _make_record(prefix="Worker-abc12345")

        result = handler.render(record=record, traceback=Text("traceback text"), message_renderable=Text("error"))

        assert isinstance(result, Table)


class TestBracketedTextInLogMessages:
    """Bracketed text in a log message must render literally, never as Rich markup.

    Prompts, tool arguments, tool results and exception strings all reach the console
    handler. Rich reads `[/SECTION]` as a closing style tag, and an unmatched one raises
    MarkupError from a spot inside RichHandler.emit that is not guarded, so the error
    escapes the logging call and takes down the caller rather than dropping a log line.
    """

    @pytest.fixture
    def output(self) -> io.StringIO:
        return io.StringIO()

    @pytest.fixture
    def logger(self, output: io.StringIO, request: pytest.FixtureRequest) -> logging.Logger:
        handler = _EngineRoleHandler(
            show_time=False,
            show_path=False,
            show_level=False,
            markup=False,
            rich_tracebacks=True,
            console=Console(file=output, width=200, no_color=True),
        )
        engine_logger = logging.getLogger(f"test_engine_log.{request.node.name}")
        engine_logger.handlers = [handler]
        engine_logger.propagate = False
        engine_logger.setLevel(logging.INFO)
        return engine_logger

    def test_unmatched_closing_marker_does_not_raise(self, logger: logging.Logger, output: io.StringIO) -> None:
        prompt = "[INSTRUCTIONS]\ndo the thing\n[/INSTRUCTIONS]"

        logger.info("start: prompt=%r", prompt)

        assert "[/INSTRUCTIONS]" in output.getvalue()

    def test_lowercase_markers_are_not_swallowed(self, logger: logging.Logger, output: io.StringIO) -> None:
        # A lowercase pair parses as a real style tag, so with markup on it would render
        # as styled text with the markers -- and the body's meaning -- stripped out.
        logger.info("prompt=%s", "[section]body[/section]")

        assert "[section]body[/section]" in output.getvalue()

    def test_bracketed_prefix_survives_in_the_message(self, logger: logging.Logger, output: io.StringIO) -> None:
        # `[run abc12345]` starts with a lowercase letter, so markup would read it as an
        # opening style tag and consume the run id that makes these lines traceable.
        logger.info("[run %s] tool call #%d", "abc12345", 1)

        assert "[run abc12345] tool call #1" in output.getvalue()

    def test_error_message_echoing_a_marker_does_not_raise(self, logger: logging.Logger, output: io.StringIO) -> None:
        # A MarkupError's own message quotes the offending tag, so with markup on, logging
        # the failure crashes the handler a second time and poisons error reporting itself.
        error_message = "closing tag '[/INSTRUCTIONS]' at position 33 doesn't match any open tag"

        logger.error("Node '%s' failed: %s", "Agent_1", error_message)

        assert "[/INSTRUCTIONS]" in output.getvalue()

    def test_markup_is_still_available_per_record(self, logger: logging.Logger, output: io.StringIO) -> None:
        logger.info("[green]OK[/green] all good", extra={"markup": True})

        rendered = output.getvalue()
        assert "OK all good" in rendered
        assert "[green]" not in rendered
