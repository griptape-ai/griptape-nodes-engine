"""High-level runner that wraps `pydantic_ai.Agent` for the chat sidebar.

This module is the single point of contact between the request-handling layer
(``AgentManager``) and the Pydantic AI harness. It exposes one async entry
point :meth:`PydanticAgentRunner.run` that:

  * builds (or reuses) a Pydantic AI ``Agent`` configured with the Griptape
    Cloud model, the skills capability, and any MCP servers the caller passed,
  * loads message history for the requested thread from the storage driver,
  * runs the conversation while emitting Griptape Nodes ``AgentStreamEvent``
    tokens through the supplied sink so the chat sidebar UI streams as before,
  * logs every model request, tool call, and tool result so we can debug the
    "agent stopped after planning" failure mode end-to-end,
  * persists the new message history back through the storage driver.

The runner stays framework-agnostic on purpose: it doesn't know about
``RunAgentRequest`` or the global event manager, so it's easy to test and
swap.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from pydantic_ai import Agent
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import (
    FinalResultEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    RetryPromptPart,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai_skills import SkillsCapability

from griptape_nodes.agents.pydantic_ai.image_tools import (
    IMAGE_TOOL_NAME,
    ImageGenerationToolset,
    ImageGenerationToolsetConfig,
    register_image_tools,
)
from griptape_nodes.agents.pydantic_ai.model import build_model
from griptape_nodes.drivers.cloud_models import ProviderID

if TYPE_CHECKING:
    from collections.abc import AsyncIterable, Awaitable, Callable, Sequence
    from pathlib import Path

    from pydantic_ai._run_context import RunContext
    from pydantic_ai.messages import ModelMessage, UserContent
    from pydantic_ai.toolsets import AbstractToolset
    from pydantic_ai.usage import UsageLimits

    from griptape_nodes.drivers.thread_storage.base_thread_storage_driver import BaseThreadStorageDriver
    from griptape_nodes.retained_mode.managers.static_files_manager import StaticFilesManager


logger = logging.getLogger("griptape_nodes")

DEFAULT_SKILLS_DIRECTORY = ".agents/skills"


TokenSink = "Callable[[str], Awaitable[None] | None]"
"""Callback that receives streamed text tokens for relay to clients."""

EventSink = "Callable[[RunEvent], Awaitable[None] | None]"
"""Callback that receives structured run events for relay to clients."""


# Cap how much of any string we'll write to the log so a giant tool argument
# or file payload doesn't drown the console.
_LOG_PREVIEW_BYTES = 240


@dataclass
class RunEvent:
    """Base class for structured events emitted during a runner ``run`` call."""


@dataclass
class TextDelta(RunEvent):
    """Incremental text token from the model's final response."""

    delta: str


@dataclass
class ToolCall(RunEvent):
    """The model has committed to a tool call."""

    tool_call_id: str
    tool_name: str
    args: str


@dataclass
class ToolResult(RunEvent):
    """A tool call has returned a value (or a retry prompt for an error)."""

    tool_call_id: str
    tool_name: str
    content: str
    is_error: bool = False


@dataclass
class ThinkingDelta(RunEvent):
    """Incremental reasoning text from the model."""

    delta: str


@dataclass
class AgentRunResult:
    """The return value of :meth:`PydanticAgentRunner.run`.

    Attributes:
        thread_id: The thread that was used (created if the caller passed None).
        output: The final assistant text response (or the partial text streamed
            so far when ``cancelled`` is ``True``).
        message_count: Total messages in the thread after this run.
        image_urls: URLs of images produced by the ``generate_image`` tool
            during this run, in call order. Empty when no image was generated.
        cancelled: ``True`` when the run was stopped via its cancel event before
            completing. A cancelled run does not persist its turn to the thread,
            so ``message_count`` reflects the pre-run history length.
    """

    thread_id: str
    output: str
    message_count: int
    image_urls: list[str] = field(default_factory=list)
    cancelled: bool = False


@dataclass
class PydanticAgentRunner:
    """Runs Pydantic AI agents and persists message history through a thread store."""

    model_name: str
    api_key: str
    workspace_root: Path
    storage: BaseThreadStorageDriver
    instructions: str | None = None
    system_prompt: str | None = None
    provider: str = ProviderID.GRIPTAPE_CLOUD
    base_url: str | None = None
    mcp_servers: list[AbstractToolset[Any]] = field(default_factory=list)
    image_config: ImageGenerationToolsetConfig | None = None
    static_files_manager: StaticFilesManager | None = None
    auto_load_skills: bool = True
    skills_directory: str = DEFAULT_SKILLS_DIRECTORY
    usage_limits: UsageLimits | None = None

    _agent: Agent[Any, str] = field(init=False)
    _image_toolset: ImageGenerationToolset | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        toolsets: list[Any] = list(self.mcp_servers)
        instructions = self._build_instructions()
        capabilities = self._build_skills_capabilities()
        agent_kwargs: dict[str, Any] = {
            "instructions": instructions,
            "toolsets": toolsets or None,
            "capabilities": capabilities or None,
        }
        if self.system_prompt:
            agent_kwargs["system_prompt"] = self.system_prompt
        self._agent = Agent(
            build_model(self.model_name, provider=self.provider, api_key=self.api_key, base_url=self.base_url),
            **agent_kwargs,
        )
        if self.image_config is not None:
            if self.static_files_manager is None:
                msg = "image_config requires a static_files_manager to persist generated images."
                raise ValueError(msg)
            self._image_toolset = register_image_tools(self._agent, self.image_config, self.static_files_manager)
        logger.info(
            "PydanticAgentRunner ready: model=%s workspace=%s mcp_servers=%d image_tool=%s skills=%d usage_limits=%s",
            self.model_name,
            self.workspace_root,
            len(self.mcp_servers),
            self._image_toolset is not None,
            len(capabilities),
            self.usage_limits,
        )

    def _build_instructions(self) -> str | None:
        """Compose the instruction string from the user's input.

        Skill guidance is injected separately by :class:`SkillsCapability` via
        its own ``get_instructions`` hook, so it is not concatenated here.
        """
        return self.instructions or None

    def _build_skills_capabilities(self) -> list[SkillsCapability]:
        """Build the skills capability exposing ``.agents/skills`` to the agent.

        Returns an empty list when skills are disabled or the skills directory
        is absent so the agent is created without a skills capability rather
        than an empty one. ``run_skill_script`` is excluded because the workspace
        already exposes a gated shell tool and skills here ship no scripts;
        ``auto_reload`` re-scans the directory before each run so edits land
        without restarting the engine.
        """
        if not self.auto_load_skills:
            return []
        skills_dir = self.workspace_root / self.skills_directory
        if not skills_dir.is_dir():
            return []
        return [
            SkillsCapability(
                directories=[skills_dir],
                exclude_tools={"run_skill_script"},
                auto_reload=True,
            )
        ]

    @property
    def agent(self) -> Agent[Any, str]:
        return self._agent

    @property
    def image_toolset(self) -> ImageGenerationToolset | None:
        return self._image_toolset

    async def run(  # noqa: PLR0913
        self,
        prompt: str | Sequence[UserContent],
        *,
        thread_id: str | None = None,
        token_sink: Callable[[str], Awaitable[None] | None] | None = None,
        event_sink: Callable[[RunEvent], Awaitable[None] | None] | None = None,
        cancel_event: asyncio.Event | None = None,
        persist_prompt: str | Sequence[UserContent] | None = None,
        history_rehydrator: Callable[[list[ModelMessage]], Awaitable[list[ModelMessage]]] | None = None,
    ) -> AgentRunResult:
        """Run the agent against ``prompt``, streaming events and saving history.

        Args:
            prompt: The user prompt for this turn. Either plain text or a
                sequence of Pydantic AI user-content parts (e.g. text plus
                inlined ``BinaryContent`` images) for multimodal input.
            thread_id: Existing thread id, or ``None`` to start a fresh one.
            token_sink: Callback invoked with each text-delta token as it
                arrives from the model. Convenience hook for text-only
                consumers; use ``event_sink`` for the structured stream.
            event_sink: Callback invoked for every structured run event
                (text deltas, tool calls, tool results, thinking deltas).
                Use this to drive rich UI surfaces.
            cancel_event: When set while the run is in flight, the agent task is
                cancelled and :meth:`run` returns an :class:`AgentRunResult` with
                ``cancelled=True`` carrying the text streamed so far. The
                cancelled turn is not persisted.
            persist_prompt: When the live ``prompt`` inlines binary content, the
                URL-reference form to store in this turn's user message instead
                of it, keeping saved history small. ``None`` persists ``prompt``
                as-is.
            history_rehydrator: Optional async hook that returns a transformed
                copy of the loaded message history for the model call (e.g.
                re-downloading image URLs back to bytes). It MUST NOT mutate its
                input: the pristine history is persisted again after this turn,
                so any in-place edit would leak back onto disk. ``None`` sends
                the loaded history to the model unchanged.

        Returns:
            An :class:`AgentRunResult` describing the new state of the thread.
        """
        if thread_id is None or not self.storage.thread_exists(thread_id):
            thread_id, _ = self.storage.create_thread()

        # ``history`` is the pristine on-disk form (images as URL references); it
        # is what we persist again after this turn. ``model_history`` may inline
        # the bytes for the model call, but we never write it back, so rehydrated
        # bytes never leak onto disk.
        history = self.storage.load_history(thread_id)
        model_history = history
        if history_rehydrator is not None:
            model_history = await history_rehydrator(history)
        run_id = thread_id[:8]

        logger.info(
            "[run %s] start: model=%s history_len=%d prompt=%r",
            run_id,
            self.model_name,
            len(history),
            _prompt_preview(prompt),
        )
        started = time.monotonic()

        text_buffer: list[str] = []
        # Log counters - every event funnels through `_event_handler` which
        # bumps these in lockstep with what the model is actually doing.
        counters = _RunCounters(run_id=run_id)

        async def event_handler(_ctx: RunContext[Any], events: AsyncIterable[Any]) -> None:
            await counters.consume(events, token_sink, event_sink, text_buffer)

        run_task = asyncio.ensure_future(
            self._agent.run(
                prompt,
                message_history=model_history,
                usage_limits=self.usage_limits,
                event_stream_handler=event_handler,
            )
        )
        try:
            agent_result = await self._await_run(run_task, cancel_event)
        except UsageLimitExceeded as exc:
            logger.warning(
                "[run %s] usage limit exceeded: %s. Aborting run; no history saved for this turn.",
                run_id,
                exc,
            )
            raise

        if agent_result is None:
            text = "".join(text_buffer)
            logger.info(
                "[run %s] cancelled after %.2fs: tool_calls=%d partial_output=%r",
                run_id,
                time.monotonic() - started,
                counters.tool_calls,
                _preview(text),
            )
            return AgentRunResult(
                thread_id=thread_id,
                output=text,
                message_count=len(history),
                image_urls=list(counters.image_urls),
                cancelled=True,
            )

        # Persist the pristine history plus only this turn's new messages. Using
        # ``new_messages()`` rather than ``all_messages()`` keeps any rehydrated
        # bytes in ``model_history`` out of what we write back to disk.
        new_messages = agent_result.new_messages()
        usage = agent_result.usage

        elapsed = time.monotonic() - started
        text = "".join(text_buffer)
        logger.info(
            "[run %s] done in %.2fs: requests=%d tool_calls=%d "
            "input_tokens=%d output_tokens=%d new_messages=%d output=%r",
            run_id,
            elapsed,
            usage.requests,
            counters.tool_calls,
            usage.input_tokens,
            usage.output_tokens,
            len(new_messages),
            _preview(text),
        )
        if not text and counters.tool_calls == 0:
            logger.warning(
                "[run %s] empty assistant turn: model returned no text and no tool calls.",
                run_id,
            )
        elif not text:
            logger.warning(
                "[run %s] assistant produced no final text after %d tool calls. "
                "The chat sidebar may render this turn as silent.",
                run_id,
                counters.tool_calls,
            )

        # We persist `history + new_messages` rather than `all_messages()`, which
        # assumes the loaded history is a sequence of complete turns ending in a
        # ModelResponse. A trailing bare ModelRequest would mean a partial turn
        # was saved earlier, so the two ways of reconstructing the transcript can
        # drift; fail loud rather than silently persist a malformed history.
        # Normal turns always end in a ModelResponse and cancelled turns are
        # never saved, so this never fires today.
        turn_messages = list(new_messages)
        if history and not isinstance(history[-1], ModelResponse):
            msg = (
                f"Attempted to persist thread {thread_id}. Failed because loaded history ends with "
                f"{type(history[-1]).__name__}, not a ModelResponse, indicating a partial turn was "
                "saved earlier."
            )
            raise ValueError(msg)
        # Rewrite only this turn's messages so a restore can never reach into
        # pristine history and clobber an older user turn.
        if persist_prompt is not None:
            _apply_persist_prompt(turn_messages, persist_prompt)
        messages_to_save = list(history) + turn_messages
        self.storage.save_history(thread_id, messages_to_save)
        return AgentRunResult(
            thread_id=thread_id,
            output=text,
            message_count=len(messages_to_save),
            image_urls=list(counters.image_urls),
        )

    @staticmethod
    async def _await_run(run_task: asyncio.Future[Any], cancel_event: asyncio.Event | None) -> Any:
        """Await the agent run, racing it against ``cancel_event`` when provided.

        Returns the agent result, or ``None`` when the run was cancelled before
        completing. If the run finishes first, its result (or exception) wins
        even when cancellation was requested in the same tick.
        """
        if cancel_event is None:
            return await run_task

        cancel_waiter = asyncio.ensure_future(cancel_event.wait())
        try:
            await asyncio.wait({run_task, cancel_waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            cancel_waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancel_waiter

        if run_task.done():
            # Run won the race (possibly raising); surface its outcome.
            return run_task.result()

        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_task
        return None


@dataclass
class _RunCounters:
    """Aggregate per-run counters fed by the event-stream handler."""

    run_id: str
    text_parts: int = 0
    tool_calls: int = 0
    final_result_emitted: bool = False
    image_urls: list[str] = field(default_factory=list)

    async def consume(
        self,
        events: AsyncIterable[Any],
        token_sink: Callable[[str], Awaitable[None] | None] | None,
        event_sink: Callable[[RunEvent], Awaitable[None] | None] | None,
        text_buffer: list[str],
    ) -> None:
        async for event in events:
            await self._on_event(event, token_sink, event_sink, text_buffer)

    async def _on_event(
        self,
        event: Any,
        token_sink: Callable[[str], Awaitable[None] | None] | None,
        event_sink: Callable[[RunEvent], Awaitable[None] | None] | None,
        text_buffer: list[str],
    ) -> None:
        if isinstance(event, PartStartEvent):
            await self._on_part_start(event, token_sink, event_sink, text_buffer)
        elif isinstance(event, PartDeltaEvent):
            await self._on_part_delta(event, token_sink, event_sink, text_buffer)
        elif isinstance(event, FunctionToolCallEvent):
            self.tool_calls += 1
            args_str = _args_str(event.part.args)
            logger.info(
                "[run %s] tool call #%d -> %s(%s) id=%s",
                self.run_id,
                self.tool_calls,
                event.part.tool_name,
                _preview(args_str),
                event.part.tool_call_id,
            )
            await _push_event(
                event_sink,
                ToolCall(
                    tool_call_id=event.part.tool_call_id,
                    tool_name=event.part.tool_name,
                    args=_args_json(event.part.args),
                ),
            )
        elif isinstance(event, FunctionToolResultEvent):
            part = getattr(event, "part", None) or getattr(event, "result", None)
            content = getattr(part, "content", None)
            tool_name = getattr(part, "tool_name", "?")
            is_error = isinstance(part, RetryPromptPart)
            content_str = _stringify(content)
            if tool_name == IMAGE_TOOL_NAME and not is_error and content_str:
                self.image_urls.append(content_str)
            logger.info(
                "[run %s] tool result <- %s id=%s preview=%r is_error=%s",
                self.run_id,
                tool_name,
                event.tool_call_id,
                _preview(content_str),
                is_error,
            )
            await _push_event(
                event_sink,
                ToolResult(
                    tool_call_id=event.tool_call_id,
                    tool_name=tool_name,
                    content=_truncate(content_str, _RESULT_TRANSPORT_BYTES),
                    is_error=is_error,
                ),
            )
        elif isinstance(event, FinalResultEvent):
            self.final_result_emitted = True
            logger.info(
                "[run %s] final result event: tool_name=%s tool_call_id=%s",
                self.run_id,
                event.tool_name,
                event.tool_call_id,
            )

    async def _on_part_start(
        self,
        event: PartStartEvent,
        token_sink: Callable[[str], Awaitable[None] | None] | None,
        event_sink: Callable[[RunEvent], Awaitable[None] | None] | None,
        text_buffer: list[str],
    ) -> None:
        if isinstance(event.part, TextPart):
            self.text_parts += 1
            logger.info("[run %s] text part #%d started", self.run_id, self.text_parts)
            if event.part.content:
                text_buffer.append(event.part.content)
                await _push_token(token_sink, event.part.content)
                await _push_event(event_sink, TextDelta(delta=event.part.content))
        elif isinstance(event.part, ThinkingPart):
            if event.part.content:
                await _push_event(event_sink, ThinkingDelta(delta=event.part.content))
        elif isinstance(event.part, ToolCallPart):
            logger.info(
                "[run %s] tool-call part started: %s id=%s",
                self.run_id,
                event.part.tool_name,
                event.part.tool_call_id,
            )

    @staticmethod
    async def _on_part_delta(
        event: PartDeltaEvent,
        token_sink: Callable[[str], Awaitable[None] | None] | None,
        event_sink: Callable[[RunEvent], Awaitable[None] | None] | None,
        text_buffer: list[str],
    ) -> None:
        if isinstance(event.delta, TextPartDelta):
            chunk = event.delta.content_delta
            if not chunk:
                return
            text_buffer.append(chunk)
            await _push_token(token_sink, chunk)
            await _push_event(event_sink, TextDelta(delta=chunk))
        elif isinstance(event.delta, ThinkingPartDelta):
            chunk = event.delta.content_delta
            if not chunk:
                return
            await _push_event(event_sink, ThinkingDelta(delta=chunk))


async def _push_token(
    token_sink: Callable[[str], Awaitable[None] | None] | None,
    token: str,
) -> None:
    if token_sink is None:
        return
    result = token_sink(token)
    if asyncio.iscoroutine(result):
        await result


async def _push_event(
    event_sink: Callable[[RunEvent], Awaitable[None] | None] | None,
    event: RunEvent,
) -> None:
    if event_sink is None:
        return
    result = event_sink(event)
    if asyncio.iscoroutine(result):
        await result


def _apply_persist_prompt(messages: list[ModelMessage], persist_prompt: str | Sequence[UserContent]) -> None:
    """Rewrite the newest user turn's content to the URL-reference form in place.

    Locates the last ``ModelRequest`` carrying a ``UserPromptPart`` and replaces
    that part's ``content`` so history stores ``ImageUrl`` references instead of
    inlined ``BinaryContent``. Does nothing when no user turn is found.
    """
    for message in reversed(messages):
        if not isinstance(message, ModelRequest):
            continue
        for index, part in enumerate(message.parts):
            if not isinstance(part, UserPromptPart):
                continue
            new_parts = list(message.parts)
            new_parts[index] = replace(part, content=persist_prompt)
            message.parts = new_parts
            return


def _preview(value: str) -> str:
    if len(value) <= _LOG_PREVIEW_BYTES:
        return value
    return value[:_LOG_PREVIEW_BYTES] + "..."


def _prompt_preview(prompt: str | Sequence[UserContent]) -> str:
    """Render a log-safe preview of a prompt that may carry binary content.

    Text parts are previewed inline; non-text parts (images, audio, etc.) are
    rendered as a ``<TypeName>`` marker so a binary payload never hits the log.
    """
    if isinstance(prompt, str):
        return _preview(prompt)
    parts = [_preview(item) if isinstance(item, str) else f"<{type(item).__name__}>" for item in prompt]
    return " ".join(parts)


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)


def _args_str(args: Any) -> str:
    if args is None:
        return "<empty: None>"
    if isinstance(args, str):
        if not args:
            return "<empty: ''>"
        return args
    if isinstance(args, dict):
        if not args:
            return "<empty: {}>"
        # Keys only: argument values are often huge file payloads.
        return "{" + ", ".join(f"{k}=..." for k in args) + "}"
    return str(args)


# Cap on how much arg JSON we ship to clients. Larger than the log preview so
# UI surfaces show meaningful detail, smaller than "unbounded" so a giant file
# payload doesn't bloat the WebSocket frame.
_ARGS_TRANSPORT_BYTES = 4096
_RESULT_TRANSPORT_BYTES = 16384


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "..."


def _args_json(args: Any) -> str:
    """Return a JSON-ish string of ``args`` suitable for transport to the UI.

    Unlike :func:`_args_str` which redacts dict values for log readability,
    this preserves the full structure so callers can render real tool-call
    detail. The result is truncated at :data:`_ARGS_TRANSPORT_BYTES` to bound
    the on-the-wire size.
    """
    if args is None:
        return "{}"
    if isinstance(args, str):
        text = args or "{}"
    else:
        try:
            text = json.dumps(args, default=str)
        except (TypeError, ValueError):
            text = str(args)
    if len(text) <= _ARGS_TRANSPORT_BYTES:
        return text
    return text[:_ARGS_TRANSPORT_BYTES] + "..."
