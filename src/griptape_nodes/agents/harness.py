from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from pydantic_ai.messages import UserContent
    from pydantic_ai.tools import DeferredToolResults

GTN_SERVER_NAME = "GriptapeNodes"
"""Name the engine's own MCP server is attached under, and so the prefix its tools are exposed under."""

# Caps on what reaches clients. Larger than a log preview so tool cards show real
# detail, smaller than unbounded so a giant file payload doesn't bloat a frame.
ARGS_TRANSPORT_BYTES = 4096
RESULT_TRANSPORT_BYTES = 16384


@dataclass
class RunEvent:
    """Base class for structured events emitted while a turn runs."""


@dataclass
class TextDelta(RunEvent):
    """Incremental text from the agent's reply."""

    delta: str


@dataclass
class ToolCall(RunEvent):
    """The agent has committed to a tool call."""

    tool_call_id: str
    tool_name: str
    args: str


@dataclass
class ToolResult(RunEvent):
    """A tool call has returned a value, or an error."""

    tool_call_id: str
    tool_name: str
    content: str
    is_error: bool = False


@dataclass
class ThinkingDelta(RunEvent):
    """Incremental reasoning text from the agent."""

    delta: str


@dataclass
class PendingApproval:
    """A tool call a paused turn is waiting on the user to approve or deny.

    Attributes:
        tool_call_id: Identifier to answer the call by.
        tool_name: Name of the tool the agent wants to call.
        args: JSON of the call's arguments, truncated like ``ToolCall.args``.
        grant_keys: What approving this call for the whole conversation grants.
    """

    tool_call_id: str
    tool_name: str
    args: str
    grant_keys: list[str]


@dataclass
class AgentRunResult:
    """The outcome of one turn.

    Attributes:
        thread_id: The thread that was used (created if the caller passed None).
        output: The final assistant text response (or the partial text streamed
            so far when ``cancelled`` is ``True``).
        message_count: Total messages in the thread after this run.
        image_urls: URLs of images produced by the ``generate_image`` tool
            during this run, in call order. Empty when no image was generated.
        cancelled: ``True`` when the run was stopped via its cancel event before
            completing. A cancelled run does not persist its turn to the thread,
            except that a resumed one saves what happened to the calls it was
            answering; ``message_count`` is the thread's length after that.
        truncated: ``True`` when the model stopped because it hit the output
            token limit rather than finishing its reply. ``output`` holds the
            partial text, which is complete-looking but ends mid-thought, so
            callers should tell the user rather than render it as a normal turn.
        pending_approvals: Tool calls the turn paused on, waiting for the user to
            approve or deny them. The turn is persisted with these calls open, so
            the next run on the thread must answer them. Empty when the turn did
            not pause.
    """

    thread_id: str
    output: str
    message_count: int
    image_urls: list[str] = field(default_factory=list)
    cancelled: bool = False
    truncated: bool = False
    pending_approvals: list[PendingApproval] = field(default_factory=list)


@dataclass(frozen=True)
class MCPServerSpec:
    """A user-configured MCP server to attach to one turn.

    ``config`` is the engine's ``MCPServerConfig`` for the server (``transport``
    plus ``command``/``args``/``env``/``cwd`` or ``url``/``headers``). It holds
    secrets in ``env`` and ``headers``, so never log it.
    """

    name: str
    config: Mapping[str, Any]


@dataclass(kw_only=True)
class AgentTurn:
    """Everything a turn needs besides what starts it.

    Attributes:
        thread_id: The thread to run in. It must already exist.
        event_sink: Receives the turn's events as they happen.
        cancel_event: Set to stop the turn. See ``AgentRunResult.cancelled``.
        instructions: Instructions for this turn only, on top of the harness's
            own, such as the rules attached to ``mcp_servers``.
        mcp_servers: User-configured MCP servers to attach to this turn.
        extra_headers: HTTP headers for this turn's model calls. Only harnesses
            that call a model directly can send them.
    """

    thread_id: str
    event_sink: Callable[[RunEvent], Awaitable[None] | None] | None = None
    cancel_event: asyncio.Event | None = None
    instructions: str | None = None
    mcp_servers: Sequence[MCPServerSpec] = ()
    extra_headers: Mapping[str, str] | None = None


@dataclass(frozen=True)
class UserMessage:
    """The message that starts a turn.

    Attributes:
        content: The user's message: plain text, or text plus inlined
            ``BinaryContent`` images.
        persist_content: The form of ``content`` to save, with images as
            ``ImageUrl`` references. ``None`` saves ``content`` as-is.
    """

    content: str | Sequence[UserContent]
    persist_content: str | Sequence[UserContent] | None = None


class AgentHarness(Protocol):
    """Persist turns as Pydantic AI ``ModelMessage`` values so threads can switch providers."""

    async def run_turn(self, turn: AgentTurn, message: UserMessage) -> AgentRunResult:
        """Run a turn for a new message, streaming events to ``turn.event_sink`` and saving it.

        Tool calls a paused turn is still waiting on are declined with
        ``TOOL_DENIED_BY_NEW_MESSAGE`` in the same request as the message.
        """
        ...


@runtime_checkable
class ToolApprovalHarness(AgentHarness, Protocol):
    """A harness that can pause a turn for the user to approve tool calls.

    A harness that cannot runs every tool without asking, and the engine leaves
    its approval instructions out of the turn.
    """

    async def resume_turn(self, turn: AgentTurn, answers: DeferredToolResults) -> AgentRunResult:
        """Continue a paused turn from the user's answers, streaming and saving like ``run_turn``.

        ``answers`` must cover every call the thread's paused turn is waiting on.
        """
        ...


async def push_event(
    event_sink: Callable[[RunEvent], Awaitable[None] | None] | None,
    event: RunEvent,
) -> None:
    """Deliver the event, awaiting coroutine callbacks."""
    if event_sink is None:
        return
    result = event_sink(event)
    if asyncio.iscoroutine(result):
        await result


def truncate(value: str, limit: int) -> str:
    """Limit the prefix to ``limit`` characters, appending ``...`` if cut."""
    if len(value) <= limit:
        return value
    return value[:limit] + "..."


def stringify(value: Any) -> str:
    """Format tool results as text; ``None`` becomes an empty string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)


def args_json(args: Any) -> str:
    """Return tool arguments for transport without log redaction, truncated to ``ARGS_TRANSPORT_BYTES``."""
    if args is None:
        return "{}"
    if isinstance(args, str):
        text = args or "{}"
    else:
        try:
            text = json.dumps(args, default=str)
        except (TypeError, ValueError):
            text = str(args)
    return truncate(text, ARGS_TRANSPORT_BYTES)
