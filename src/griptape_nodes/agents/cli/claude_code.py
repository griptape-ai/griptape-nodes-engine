from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from griptape_nodes.agents.cli.base import (
    CliEvent,
    CliHarness,
    CliInvocation,
    CliStreamParser,
    CliTurnContext,
    SessionStarted,
    TurnEnded,
)
from griptape_nodes.agents.harness import TextDelta, ThinkingDelta, ToolCall, ToolResult, stringify

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from griptape_nodes.agents.cli.base import CliMCPServer, PromptImage

logger = logging.getLogger("griptape_nodes")

_MCP_CONFIG_FILE_NAME = "mcp-config.json"
_INSTRUCTIONS_FILE_NAME = "instructions.md"


# Claude Code refuses to start inside another Claude Code session, which
# `CLAUDECODE` marks, and an engine launched from one inherits it. In print mode
# it bills an Anthropic key over the user's sign-in, and the engine's environment
# holds any key set up for the Anthropic nodes.
_REMOVED_ENV: dict[str, str | None] = {"CLAUDECODE": None, "ANTHROPIC_API_KEY": None, "ANTHROPIC_AUTH_TOKEN": None}


@dataclass
class ClaudeCodeHarness(CliHarness):
    harness_id: ClassVar[str] = "claude_code"
    display_name: ClassVar[str] = "Claude Code"
    executable: ClassVar[str] = "claude"
    install_hint: ClassVar[str] = (
        "Install Claude Code from https://claude.com/product/claude-code, then run `claude` once to sign in."
    )
    suggested_models: ClassVar[tuple[str, ...]] = ("sonnet", "opus", "haiku")

    def build_invocation(self, context: CliTurnContext) -> CliInvocation:
        args = [
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--permission-mode",
            "bypassPermissions",
            "--mcp-config",
            str(_write_mcp_config(context)),
        ]
        if context.model:
            args += ["--model", context.model]
        if context.max_turns:
            args += ["--max-turns", str(context.max_turns)]
        if context.instructions:
            # MCP server rules in the instructions may hold secrets.
            instructions_path = _write_private(context.scratch_dir / _INSTRUCTIONS_FILE_NAME, context.instructions)
            args += ["--append-system-prompt-file", str(instructions_path)]
        if context.resume_session_id:
            args += ["--resume", context.resume_session_id]
        else:
            args += ["--session-id", context.new_session_id]
        message = {
            "type": "user",
            "message": {"role": "user", "content": _content_blocks(context.prompt_text, context.images)},
        }
        return CliInvocation(args=args, stdin=(json.dumps(message) + "\n").encode("utf-8"), env=_REMOVED_ENV)

    def new_parser(self) -> CliStreamParser:
        return ClaudeCodeStreamParser(normalize_tool_name=self.normalize_tool_name)


class ClaudeCodeStreamParser(CliStreamParser):
    def __init__(self, *, normalize_tool_name: Callable[[str], str]) -> None:
        self._normalize_tool_name = normalize_tool_name
        self._tool_names: dict[str, str] = {}
        self._streaming_message_id: str | None = None
        self._streamed_message_ids: set[str] = set()

    def feed(self, record: dict[str, Any]) -> list[CliEvent]:
        # The sidebar shows only the main agent.
        if record.get("parent_tool_use_id"):
            return []
        events: list[CliEvent]
        match record.get("type"):
            case "system":
                events = _session_started(record)
            case "stream_event":
                events = self._stream_event(record.get("event") or {})
            case "assistant":
                # Complete messages carry finished tool arguments.
                events = self._assistant_message(record.get("message") or {})
            case "user":
                events = self._tool_results(record.get("message") or {})
            case "result":
                events = [_turn_ended(record)]
            case _:
                events = []
        return events

    def _stream_event(self, event: dict[str, Any]) -> list[CliEvent]:
        event_type = event.get("type")
        if event_type == "message_start":
            message_id = (event.get("message") or {}).get("id")
            self._streaming_message_id = message_id if isinstance(message_id, str) else None
            return []
        if event_type != "content_block_delta":
            return []
        delta = event.get("delta") or {}
        delta_type = delta.get("type")
        if delta_type == "text_delta" and delta.get("text"):
            if self._streaming_message_id is not None:
                self._streamed_message_ids.add(self._streaming_message_id)
            return [TextDelta(delta=str(delta["text"]))]
        if delta_type == "thinking_delta" and delta.get("thinking"):
            return [ThinkingDelta(delta=str(delta["thinking"]))]
        return []

    def _assistant_message(self, message: dict[str, Any]) -> list[CliEvent]:
        # Some CLI versions omit partial-message events.
        text_was_streamed = message.get("id") in self._streamed_message_ids
        events: list[CliEvent] = []
        for block in message.get("content") or []:
            block_type = block.get("type")
            if block_type == "text" and not text_was_streamed and block.get("text"):
                events.append(TextDelta(delta=str(block["text"])))
            elif block_type == "tool_use":
                tool_call_id = str(block.get("id") or "")
                tool_name = self._normalize_tool_name(str(block.get("name") or "?"))
                self._tool_names[tool_call_id] = tool_name
                events.append(
                    ToolCall(
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                        args=json.dumps(block.get("input") or {}, default=str),
                    )
                )
        return events

    def _tool_results(self, message: dict[str, Any]) -> list[CliEvent]:
        content = message.get("content")
        if not isinstance(content, list):
            return []
        events: list[CliEvent] = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_call_id = str(block.get("tool_use_id") or "")
            events.append(
                ToolResult(
                    tool_call_id=tool_call_id,
                    tool_name=self._tool_names.get(tool_call_id, "?"),
                    content=_tool_result_text(block.get("content")),
                    is_error=bool(block.get("is_error")),
                )
            )
        return events


def _session_started(record: dict[str, Any]) -> list[CliEvent]:
    session_id = record.get("session_id")
    if record.get("subtype") != "init" or not isinstance(session_id, str) or not session_id:
        return []
    return [SessionStarted(session_id=session_id)]


def _turn_ended(record: dict[str, Any]) -> TurnEnded:
    subtype = record.get("subtype")
    # A turn limit keeps partial work; treat it like an output limit.
    if subtype == "error_max_turns":
        return TurnEnded(truncated=True)
    if record.get("is_error") or subtype != "success":
        errors = [str(error) for error in record.get("errors") or []]
        message = "; ".join(errors) or record.get("result") or subtype or "unknown error"
        return TurnEnded(error=str(message))
    return TurnEnded(truncated=record.get("stop_reason") == "max_tokens")


def _tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, dict):
                parts.append(f"[{item.get('type', 'content')}]")
        return "\n".join(parts)
    return stringify(content)


def _content_blocks(text: str, images: Sequence[PromptImage]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if text:
        blocks.append({"type": "text", "text": text})
    blocks.extend(
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": image.media_type,
                "data": base64.b64encode(image.data).decode("ascii"),
            },
        }
        for image in images
    )
    return blocks


def _write_mcp_config(context: CliTurnContext) -> Path:
    """Write the config to a file, since the command line is readable by other local users."""
    config = json.dumps({"mcpServers": _mcp_config(context.mcp_servers)})
    return _write_private(context.scratch_dir / _MCP_CONFIG_FILE_NAME, config)


def _write_private(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    return path


def _mcp_config(servers: Sequence[CliMCPServer]) -> dict[str, dict[str, Any]]:
    config: dict[str, dict[str, Any]] = {}
    for server in servers:
        if server.transport == "stdio" and server.command:
            config[server.name] = {
                "type": "stdio",
                "command": server.command,
                "args": list(server.args),
                "env": dict(server.env),
            }
            if server.cwd:
                logger.warning(
                    "Attempted to start Claude Code MCP server '%s' in '%s'. Failed because Claude Code can't "
                    "set a working directory for a server, so it will start in the workspace.",
                    server.name,
                    server.cwd,
                )
        elif server.transport in ("streamable_http", "sse") and server.url:
            config[server.name] = {
                "type": "http" if server.transport == "streamable_http" else "sse",
                "url": server.url,
                "headers": dict(server.headers),
            }
        else:
            logger.warning(
                "Attempted to give Claude Code MCP server '%s'. Failed because its %s connection is not one "
                "Claude Code supports, so it will run without it.",
                server.name,
                server.transport,
            )
    return config
