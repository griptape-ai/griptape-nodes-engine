"""Fixtures follow pi 1.1.0's ``docs/json.md``.

Records were captured with a stdio MCP server and with an invalid model id.
"""

from __future__ import annotations

import json
import re
import stat
import sys
from pathlib import Path
from typing import Any

from griptape_nodes.agents.cli.base import (
    CliMCPServer,
    CliTurnContext,
    PromptImage,
    SessionStarted,
    TurnEnded,
)
from griptape_nodes.agents.cli.pi import PiHarness, PiStreamParser
from griptape_nodes.agents.harness import TextDelta, ThinkingDelta, ToolCall, ToolResult


def _harness() -> PiHarness:
    return PiHarness(workspace_root=Path("/ws"), storage=None, engine_mcp_url="http://localhost:1/mcp/")  # type: ignore[arg-type]


def _context(scratch_dir: Path, **overrides: Any) -> CliTurnContext:
    values: dict[str, Any] = {
        "prompt_text": "Build a flow",
        "images": [],
        "instructions": "Be brief.",
        "resume_session_id": None,
        "new_session_id": "new-id",
        "mcp_servers": [CliMCPServer(name="GriptapeNodes", transport="streamable_http", url="http://localhost:1/mcp/")],
        "model": "anthropic/sonnet",
        "max_turns": 60,
        "workspace_root": Path("/ws"),
        "scratch_dir": scratch_dir,
    }
    values.update(overrides)
    return CliTurnContext(**values)


def _flag(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


def _registered_servers(extension_path: Path) -> dict[str, Any]:
    match = re.search(r"const servers = (\{.*\});", extension_path.read_text(encoding="utf-8"))
    assert match is not None
    return json.loads(match.group(1))


def test_new_session_is_started_with_the_chosen_id(tmp_path: Path) -> None:
    """Pass the chosen session ID, turn options, and stdin text."""
    invocation = _harness().build_invocation(_context(tmp_path))
    args = list(invocation.args)

    assert args[:3] == ["--mode", "json", "-p"]
    assert _flag(args, "--session-id") == "new-id"
    assert "--session" not in args
    assert _flag(args, "--model") == "anthropic/sonnet"
    assert Path(_flag(args, "--append-system-prompt")).read_text() == "Be brief."
    assert invocation.stdin == b"Build a flow"
    assert invocation.env == {}


def test_existing_session_is_resumed(tmp_path: Path) -> None:
    """Resume instead of creating a session."""
    args = list(_harness().build_invocation(_context(tmp_path, resume_session_id="old-id")).args)

    assert _flag(args, "--session") == "old-id"
    assert "--session-id" not in args


def test_unset_options_are_left_to_pi(tmp_path: Path) -> None:
    """Omit unset model and instruction options."""
    args = list(_harness().build_invocation(_context(tmp_path, model=None, instructions="")).args)

    assert "--model" not in args
    assert "--append-system-prompt" not in args


def test_mcp_servers_are_registered_by_a_generated_extension(tmp_path: Path) -> None:
    """Register supported MCP servers through a scratch extension."""
    servers = [
        CliMCPServer(name="GriptapeNodes", transport="streamable_http", url="http://localhost:1/mcp/"),
        CliMCPServer(name="files", transport="stdio", command="npx", args=["-y", "fs"], env={"K": "V"}, cwd="/data"),
        CliMCPServer(name="remote api", transport="streamable_http", url="http://s/mcp", headers={"A": "B"}),
        CliMCPServer(name="legacy", transport="sse", url="http://s/sse"),
    ]
    args = list(_harness().build_invocation(_context(tmp_path, mcp_servers=servers)).args)

    extension_path = Path(_flag(args, "--extension"))
    assert extension_path.parent == tmp_path
    assert _registered_servers(extension_path) == {
        "GriptapeNodes": {"url": "http://localhost:1/mcp/", "headers": {}, "exposure": "direct"},
        "files": {"command": "npx", "args": ["-y", "fs"], "env": {"K": "V"}, "exposure": "direct", "cwd": "/data"},
        "remote_api": {"url": "http://s/mcp", "headers": {"A": "B"}, "exposure": "direct"},
    }
    assert "pi.registerMcpServer(name, server)" in extension_path.read_text(encoding="utf-8")
    if sys.platform != "win32":
        assert stat.S_IMODE(extension_path.stat().st_mode) == stat.S_IRUSR | stat.S_IWUSR


def test_mcp_secrets_are_escaped_from_pis_value_templates(tmp_path: Path) -> None:
    """Keep pi from expanding `$NAME` or running a value that starts with `!`."""
    servers = [
        CliMCPServer(name="files", transport="stdio", command="npx", env={"TOKEN": "pa$sword", "CMD": "!rm"}),
        CliMCPServer(name="api", transport="streamable_http", url="http://s/mcp", headers={"A": "Bearer ${X}"}),
    ]
    args = list(_harness().build_invocation(_context(tmp_path, mcp_servers=servers)).args)

    registered = _registered_servers(Path(_flag(args, "--extension")))
    assert registered["files"]["env"] == {"TOKEN": "pa$$sword", "CMD": "$!rm"}
    assert registered["api"]["headers"] == {"A": "Bearer $${X}"}


def test_images_are_attached_as_file_arguments(tmp_path: Path) -> None:
    """Attach images with @path arguments."""
    image = PromptImage(data=b"\x89PNG", media_type="image/png", path=tmp_path / "attachment-0.png")
    args = list(_harness().build_invocation(_context(tmp_path, images=[image])).args)

    assert f"@{tmp_path / 'attachment-0.png'}" in args


def test_mcp_tool_names_match_the_names_pi_gives_them() -> None:
    """Match pi's punctuation-normalized MCP names and normalize its prefix."""
    harness = _harness()

    assert harness.mcp_tool_name("GriptapeNodes", "CreateNodeRequest") == "mcp__GriptapeNodes__CreateNodeRequest"
    assert harness.mcp_tool_name("my-server", "get.thing") == "mcp__my_server__get_thing"
    assert harness.normalize_tool_name("mcp__demo__add_numbers") == "demo_add_numbers"
    assert harness.normalize_tool_name("bash") == "bash"


def _feed(records: list[dict[str, Any]]) -> list[Any]:
    parser = PiStreamParser(normalize_tool_name=_harness().normalize_tool_name)
    return [event for record in records for event in parser.feed(record)]


def _update(event: dict[str, Any]) -> dict[str, Any]:
    return {"type": "message_update", "usage": {"input": 0, "output": 0}, "assistantMessageEvent": event}


def _assistant(stop_reason: str, **fields: Any) -> dict[str, Any]:
    return {"role": "assistant", "content": [], "stopReason": stop_reason, **fields}


def test_a_turn_with_a_tool_call_is_parsed_in_order() -> None:
    """Preserve event order without repeating streamed blocks."""
    events = _feed(
        [
            {"type": "session", "version": 3, "id": "s-1", "timestamp": "2026-10-13T19:13:26.863Z", "cwd": "/ws"},
            {"type": "agent_start"},
            {"type": "message_start", "message": {"role": "user", "content": [{"type": "text", "text": "Add"}]}},
            {"type": "message_start", "message": _assistant("pending")},
            _update({"type": "thinking_start", "contentIndex": 0}),
            _update({"type": "thinking_delta", "contentIndex": 0, "delta": "Let me call it"}),
            _update({"type": "thinking_end", "contentIndex": 0, "content": "Let me call it"}),
            _update({"type": "toolcall_start", "contentIndex": 1, "id": "tu_1", "toolName": "mcp__demo__add_numbers"}),
            _update({"type": "toolcall_delta", "contentIndex": 1, "delta": '{"a": 2'}),
            {"type": "message_end", "message": _assistant("toolUse")},
            {
                "type": "tool_execution_start",
                "toolCallId": "tu_1",
                "toolName": "mcp__demo__add_numbers",
                "args": {"a": 2, "b": 3},
            },
            {
                "type": "tool_execution_end",
                "toolCallId": "tu_1",
                "toolName": "mcp__demo__add_numbers",
                "result": {"content": [{"type": "text", "text": "5"}], "details": {"server": "demo"}},
                "isError": False,
            },
            {"type": "turn_end", "message": _assistant("toolUse"), "toolResults": []},
            {"type": "message_start", "message": _assistant("pending")},
            _update({"type": "text_start", "contentIndex": 0}),
            _update({"type": "text_delta", "contentIndex": 0, "delta": "Two plus "}),
            _update({"type": "text_delta", "contentIndex": 0, "delta": "three is five."}),
            _update({"type": "text_end", "contentIndex": 0, "content": "Two plus three is five."}),
            {"type": "message_end", "message": _assistant("stop")},
            {"type": "agent_end", "messages": [], "willRetry": False},
            {"type": "agent_settled", "aborted": False},
        ]
    )

    assert events == [
        SessionStarted(session_id="s-1"),
        ThinkingDelta(delta="Let me call it"),
        ToolCall(tool_call_id="tu_1", tool_name="demo_add_numbers", args='{"a": 2, "b": 3}'),
        ToolResult(tool_call_id="tu_1", tool_name="demo_add_numbers", content="5"),
        TextDelta(delta="Two plus "),
        TextDelta(delta="three is five."),
        TurnEnded(),
        TurnEnded(),
    ]


def test_text_that_was_never_streamed_is_taken_from_its_end_record() -> None:
    """Fall back to complete blocks without duplicating streamed ones."""
    events = _feed(
        [
            {"type": "message_start", "message": _assistant("pending")},
            _update({"type": "text_end", "contentIndex": 0, "content": "Hi"}),
            _update({"type": "thinking_delta", "contentIndex": 1, "delta": "Hm"}),
            _update({"type": "thinking_end", "contentIndex": 1, "content": "Hm"}),
        ]
    )

    assert events == [TextDelta(delta="Hi"), ThinkingDelta(delta="Hm")]


def test_calls_made_by_another_tool_are_skipped() -> None:
    """Exclude nested codemode calls."""
    events = _feed(
        [
            {
                "type": "tool_execution_start",
                "toolCallId": "tu_9/1",
                "parentToolCallId": "tu_9",
                "toolName": "mcp__demo__add_numbers",
                "args": {},
            },
            {
                "type": "tool_execution_end",
                "toolCallId": "tu_9/1",
                "parentToolCallId": "tu_9",
                "toolName": "mcp__demo__add_numbers",
                "result": {"content": []},
                "isError": False,
            },
        ]
    )

    assert events == []


def test_tool_errors_are_flagged() -> None:
    """Preserve tool-result error flags."""
    events = _feed(
        [
            {"type": "tool_execution_start", "toolCallId": "t", "toolName": "mcp__demo__fail_tool", "args": {}},
            {
                "type": "tool_execution_end",
                "toolCallId": "t",
                "toolName": "mcp__demo__fail_tool",
                "result": {"content": [{"type": "text", "text": "Error executing tool fail_tool"}]},
                "isError": True,
            },
        ]
    )

    assert events[1] == ToolResult(
        tool_call_id="t", tool_name="demo_fail_tool", content="Error executing tool fail_tool", is_error=True
    )


def test_failed_reply_reports_pis_error() -> None:
    """A reply that ended in an error carries pi's error text, though pi exits with code 0."""
    events = _feed(
        [
            {"type": "message_start", "message": _assistant("error", errorMessage="The model identifier is invalid.")},
            {"type": "message_end", "message": _assistant("error", errorMessage="The model identifier is invalid.")},
            {"type": "agent_end", "messages": [], "willRetry": False},
            {"type": "agent_settled", "aborted": False},
        ]
    )

    assert events == [
        TurnEnded(error="The model identifier is invalid."),
        TurnEnded(error="The model identifier is invalid."),
    ]


def test_output_limit_marks_the_reply_truncated() -> None:
    """Treat output limits as truncation."""
    events = _feed(
        [{"type": "message_end", "message": _assistant("length")}, {"type": "agent_settled", "aborted": False}]
    )

    assert events == [TurnEnded(truncated=True)]


def test_an_aborted_run_is_an_error() -> None:
    """Report aborted runs as unfinished replies."""
    events = _feed([{"type": "agent_settled", "aborted": True}])

    assert events == [TurnEnded(error="pi stopped before finishing the reply.")]


def test_a_run_that_will_retry_has_not_ended() -> None:
    """Wait for retries before ending the turn."""
    events = _feed([{"type": "agent_end", "messages": [], "willRetry": True}])

    assert events == []
