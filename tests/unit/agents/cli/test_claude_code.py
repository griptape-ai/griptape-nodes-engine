"""Fixtures follow ``claude -p --output-format stream-json --include-partial-messages`` output."""

from __future__ import annotations

import base64
import json
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from griptape_nodes.agents.cli.base import (
    CliMCPServer,
    CliTurnContext,
    PromptImage,
    SessionStarted,
    TurnEnded,
)
from griptape_nodes.agents.cli.claude_code import ClaudeCodeHarness, ClaudeCodeStreamParser
from griptape_nodes.agents.harness import TextDelta, ThinkingDelta, ToolCall, ToolResult


def _harness() -> ClaudeCodeHarness:
    return ClaudeCodeHarness(workspace_root=Path("/ws"), storage=None, engine_mcp_url="http://localhost:1/mcp/")  # type: ignore[arg-type]


def _context(**overrides: Any) -> CliTurnContext:
    values: dict[str, Any] = {
        "prompt_text": "Build a flow",
        "images": [],
        "instructions": "Be brief.",
        "resume_session_id": None,
        "new_session_id": "new-id",
        "mcp_servers": [CliMCPServer(name="GriptapeNodes", transport="streamable_http", url="http://localhost:1/mcp/")],
        "model": "sonnet",
        "max_turns": 60,
        "workspace_root": Path("/ws"),
        "scratch_dir": Path("scratch"),
    }
    values.update(overrides)
    return CliTurnContext(**values)


@pytest.fixture(autouse=True)
def scratch_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``build_invocation`` writes config files to the context's relative ``scratch`` dir."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "scratch").mkdir()
    return tmp_path / "scratch"


def _flag(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


def test_new_session_is_started_with_the_chosen_id() -> None:
    """Pass the chosen session ID and turn options."""
    invocation = _harness().build_invocation(_context())
    args = list(invocation.args)

    assert _flag(args, "--session-id") == "new-id"
    assert "--resume" not in args
    assert _flag(args, "--model") == "sonnet"
    assert _flag(args, "--max-turns") == "60"
    assert Path(_flag(args, "--append-system-prompt-file")).read_text() == "Be brief."
    assert _flag(args, "--permission-mode") == "bypassPermissions"
    assert invocation.env == {"CLAUDECODE": None, "ANTHROPIC_API_KEY": None, "ANTHROPIC_AUTH_TOKEN": None}


def test_existing_session_is_resumed() -> None:
    """Resume instead of creating a session."""
    args = list(_harness().build_invocation(_context(resume_session_id="old-id")).args)

    assert _flag(args, "--resume") == "old-id"
    assert "--session-id" not in args


def test_unset_options_are_left_to_claude() -> None:
    """Omit unset model, turn-limit, and instruction options."""
    args = list(_harness().build_invocation(_context(model=None, max_turns=None, instructions="")).args)

    assert "--model" not in args
    assert "--max-turns" not in args
    assert "--append-system-prompt-file" not in args


def test_mcp_servers_are_translated_to_claude_config() -> None:
    """Translate supported MCP transports and skip the rest."""
    servers = [
        CliMCPServer(name="GriptapeNodes", transport="streamable_http", url="http://localhost:1/mcp/"),
        CliMCPServer(name="files", transport="stdio", command="npx", args=["-y", "fs"], env={"K": "V"}),
        CliMCPServer(name="legacy", transport="sse", url="http://s/sse", headers={"A": "B"}),
        CliMCPServer(name="ws", transport="websocket", url="ws://x"),
    ]
    args = list(_harness().build_invocation(_context(mcp_servers=servers)).args)

    config_path = Path(_flag(args, "--mcp-config"))
    if sys.platform != "win32":
        assert stat.S_IMODE(config_path.stat().st_mode) == stat.S_IRUSR | stat.S_IWUSR
    config = json.loads(config_path.read_text())["mcpServers"]
    assert config == {
        "GriptapeNodes": {"type": "http", "url": "http://localhost:1/mcp/", "headers": {}},
        "files": {"type": "stdio", "command": "npx", "args": ["-y", "fs"], "env": {"K": "V"}},
        "legacy": {"type": "sse", "url": "http://s/sse", "headers": {"A": "B"}},
    }


def test_prompt_and_images_go_to_stdin_as_content_blocks() -> None:
    """Send text and base64 images on stdin."""
    image = PromptImage(data=b"\x89PNG", media_type="image/png", path=Path("scratch/a.png"))
    invocation = _harness().build_invocation(_context(images=[image]))

    assert invocation.stdin is not None
    message = json.loads(invocation.stdin)
    assert message["type"] == "user"
    text, picture = message["message"]["content"]
    assert text == {"type": "text", "text": "Build a flow"}
    assert picture["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": base64.b64encode(b"\x89PNG").decode(),
    }


def _feed(records: list[dict[str, Any]]) -> list[Any]:
    parser = ClaudeCodeStreamParser(normalize_tool_name=_harness().normalize_tool_name)
    return [event for record in records for event in parser.feed(record)]


def _stream(event: dict[str, Any]) -> dict[str, Any]:
    return {"type": "stream_event", "event": event, "parent_tool_use_id": None, "session_id": "s"}


def test_a_turn_with_a_tool_call_is_parsed_in_order() -> None:
    """Preserve event order without repeating streamed text."""
    events = _feed(
        [
            {"type": "system", "subtype": "init", "session_id": "s-1", "tools": ["Bash"]},
            _stream({"type": "message_start", "message": {"id": "m1"}}),
            _stream({"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "Hmm"}}),
            _stream({"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": "{"}}),
            {
                "type": "assistant",
                "message": {
                    "id": "m1",
                    "content": [
                        {"type": "thinking", "thinking": "Hmm"},
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "mcp__GriptapeNodes__CreateNodeRequest",
                            "input": {"node_type": "Agent"},
                        },
                    ],
                },
                "parent_tool_use_id": None,
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "ok"}]}
                    ]
                },
                "parent_tool_use_id": None,
            },
            _stream({"type": "message_start", "message": {"id": "m2"}}),
            _stream({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Created "}}),
            _stream({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "it."}}),
            {"type": "assistant", "message": {"id": "m2", "content": [{"type": "text", "text": "Created it."}]}},
            {"type": "result", "subtype": "success", "is_error": False, "stop_reason": "end_turn", "result": "x"},
        ]
    )

    assert events == [
        SessionStarted(session_id="s-1"),
        ThinkingDelta(delta="Hmm"),
        ToolCall(tool_call_id="t1", tool_name="GriptapeNodes_CreateNodeRequest", args='{"node_type": "Agent"}'),
        ToolResult(tool_call_id="t1", tool_name="GriptapeNodes_CreateNodeRequest", content="ok"),
        TextDelta(delta="Created "),
        TextDelta(delta="it."),
        TurnEnded(),
    ]


def test_text_that_was_never_streamed_is_taken_from_the_message() -> None:
    """Fall back to complete message text."""
    events = _feed([{"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "Hi"}]}}])

    assert events == [TextDelta(delta="Hi")]


def test_subagent_records_are_skipped() -> None:
    """Exclude subagent output."""
    events = _feed(
        [
            {
                "type": "stream_event",
                "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "inner"}},
                "parent_tool_use_id": "task-1",
            }
        ]
    )

    assert events == []


def test_tool_errors_are_flagged() -> None:
    """Preserve tool-result error flags."""
    events = _feed(
        [
            {"type": "assistant", "message": {"id": "m", "content": [{"type": "tool_use", "id": "t", "name": "Bash"}]}},
            {
                "type": "user",
                "message": {
                    "content": [{"type": "tool_result", "tool_use_id": "t", "content": "nope", "is_error": True}]
                },
            },
        ]
    )

    assert events[1] == ToolResult(tool_call_id="t", tool_name="Bash", content="nope", is_error=True)


def test_output_limit_and_turn_limit_mark_the_reply_truncated() -> None:
    """Treat output and turn limits as truncation."""
    assert _feed([{"type": "result", "subtype": "success", "stop_reason": "max_tokens"}]) == [TurnEnded(truncated=True)]
    assert _feed([{"type": "result", "subtype": "error_max_turns", "is_error": True}]) == [TurnEnded(truncated=True)]


def test_failed_result_reports_claudes_error() -> None:
    """Preserve Claude Code's failure message."""
    record = {
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "errors": ["No conversation found with session ID: x"],
        "result": None,
    }

    assert _feed([record]) == [TurnEnded(error="No conversation found with session ID: x")]
