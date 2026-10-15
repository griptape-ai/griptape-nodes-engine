"""The Pydantic AI harness: runs `pydantic_ai.Agent` for the chat sidebar.

``AgentManager`` reaches it through :meth:`PydanticAgentRunner.run_turn`, the
:class:`~griptape_nodes.agents.harness.AgentHarness` entry point, which attaches
the turn's MCP servers and calls :meth:`PydanticAgentRunner.run`. That:

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
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults, ToolDenied
from pydantic_ai_skills import SkillsCapability

from griptape_nodes.agents.harness import (
    RESULT_TRANSPORT_BYTES,
    AgentRunResult,
    AgentTurn,
    PendingApproval,
    RunEvent,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolResult,
    UserMessage,
    args_json,
    push_event,
    stringify,
    truncate,
)
from griptape_nodes.agents.pydantic_ai.image_tools import (
    IMAGE_TOOL_NAME,
    ImageGenerationToolset,
    ImageGenerationToolsetConfig,
    register_image_tools,
)
from griptape_nodes.agents.pydantic_ai.mcp_servers import grant_keys_for_call
from griptape_nodes.agents.pydantic_ai.model import build_model
from griptape_nodes.agents.pydantic_ai.tool_approval import ToolApprovalDeps, settle_cancelled_resume
from griptape_nodes.agents.thread_history import TOOL_DENIED_BY_NEW_MESSAGE, ends_in_tool_results, pending_tool_calls
from griptape_nodes.drivers.cloud_models import ProviderID

if TYPE_CHECKING:
    from collections.abc import AsyncIterable, Awaitable, Callable, Mapping, Sequence
    from pathlib import Path

    from pydantic_ai._run_context import RunContext
    from pydantic_ai.messages import ModelMessage, UserContent
    from pydantic_ai.settings import ModelSettings
    from pydantic_ai.toolsets import AbstractToolset
    from pydantic_ai.usage import UsageLimits

    from griptape_nodes.agents.pydantic_ai.mcp_toolset_cache import MCPToolsetCache
    from griptape_nodes.drivers.thread_storage.base_thread_storage_driver import BaseThreadStorageDriver
    from griptape_nodes.retained_mode.managers.static_files_manager import StaticFilesManager


logger = logging.getLogger("griptape_nodes")

DEFAULT_SKILLS_DIRECTORY = ".agents/skills"


TokenSink = "Callable[[str], Awaitable[None] | None]"
"""Callback that receives streamed text tokens for relay to clients."""


# Cap how much of any string we'll write to the log so a giant tool argument
# or file payload doesn't drown the console.
_LOG_PREVIEW_BYTES = 240


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
    model_settings: ModelSettings | None = None
    # Shared with every other runner, so it is owned by the caller: see
    # `AgentManager._mcp_toolsets`. `None` attaches no user MCP servers.
    mcp_toolset_cache: MCPToolsetCache | None = None
    history_rehydrator: Callable[[list[ModelMessage]], Awaitable[list[ModelMessage]]] | None = None

    _agent: Agent[Any, str] = field(init=False)
    _image_toolset: ImageGenerationToolset | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        toolsets: list[Any] = list(self.mcp_servers)
        instructions = self._build_instructions()
        agent_kwargs: dict[str, Any] = {
            "instructions": instructions,
            "toolsets": toolsets or None,
        }
        if self.system_prompt:
            agent_kwargs["system_prompt"] = self.system_prompt
        # `build_model` resolves `settings=None` to the catalog preset for this
        # model, so read the cap back off the built model rather than off
        # `self.model_settings` — the latter is None on the normal sidebar path.
        model = build_model(
            self.model_name,
            provider=self.provider,
            api_key=self.api_key,
            base_url=self.base_url,
            settings=self.model_settings,
        )
        self._agent = Agent(model, **agent_kwargs)
        if self.image_config is not None:
            if self.static_files_manager is None:
                msg = "image_config requires a static_files_manager to persist generated images."
                raise ValueError(msg)
            self._image_toolset = register_image_tools(self._agent, self.image_config, self.static_files_manager)
        resolved_settings = model.settings or {}
        logger.debug(
            "PydanticAgentRunner ready: model=%s workspace=%s mcp_servers=%d image_tool=%s skills_library=%s "
            "usage_limits=%s max_tokens=%s",
            self.model_name,
            self.workspace_root,
            len(self.mcp_servers),
            self._image_toolset is not None,
            self._skills_library(),
            self.usage_limits,
            # "provider default" is the honest description of sending no
            # max_tokens: the ceiling exists, we just don't choose it.
            resolved_settings.get("max_tokens", "provider default"),
        )

    def _build_instructions(self) -> str | None:
        """Compose the instruction string from the user's input.

        Each skill is its own deferred capability, so its guidance reaches the
        model through Pydantic AI's ``load_capability`` tool rather than being
        concatenated here.
        """
        return self.instructions or None

    def _skills_library(self) -> Path | None:
        """The skills directory to scan, or ``None`` when there is nothing to scan."""
        if not self.auto_load_skills:
            return None
        skills_dir = self.workspace_root / self.skills_directory
        if not skills_dir.is_dir():
            return None
        return skills_dir

    def _build_skills_capabilities(self) -> list[SkillsCapability]:
        """Build the skills capability exposing ``.agents/skills`` to the agent.

        Built per run rather than per runner: discovery is a construction-time
        snapshot, so rebuilding it each turn is what makes an edited skill land
        without restarting the engine. ``scripts=False`` leaves
        ``run_skill_script`` unregistered because the workspace already exposes a
        gated shell tool and skills here ship no scripts.

        Returns an empty list when skills are disabled, the directory is absent,
        no skill in it loads, or this platform cannot load skills, so the run
        proceeds without skills instead of failing.
        """
        skills_dir = self._skills_library()
        if skills_dir is None:
            return []
        try:
            capability = self._load_skills(skills_dir)
            if capability is None:
                capability = self._load_usable_skills(skills_dir)
        except NotImplementedError as e:
            # Raised for the whole library, not one skill, so probing skill by skill cannot help.
            logger.warning(
                "Attempted to load skills from %s. Failed because this platform does not support them: %s",
                skills_dir,
                e,
            )
            return []
        if capability is None:
            return []
        return [capability]

    def _load_skills(
        self,
        skills_dir: Path,
        include: list[str] | None = None,
        *,
        index_resources: bool = True,
    ) -> SkillsCapability | None:
        """Build a capability over ``skills_dir``, or ``None`` when the loader rejects a skill.

        The loader validates every selected skill while constructing and raises on the
        first one it rejects, so this succeeds only when all of them load. ``ValueError``
        is a frontmatter report and ``OSError`` an unreadable ``SKILL.md``; both cost
        skills rather than the run.

        ``index_resources=False`` skips indexing bundled *resources*, which a probe does
        not need and which is the bulk of what makes probing expensive. Scripts are still
        discovered, so a library shipping them pays more per probe than one that does not.
        """
        exclude_resources = None if index_resources else ["*"]
        try:
            return SkillsCapability(
                directories=[skills_dir],
                scripts=False,
                include=include,
                exclude_resources=exclude_resources,
            )
        except (ValueError, OSError) as e:
            logger.warning(
                "Attempted to load skills %s from %s. Failed because of: %s",
                include if include is not None else "(all)",
                skills_dir,
                e,
            )
            return None

    def _load_usable_skills(self, skills_dir: Path) -> SkillsCapability | None:
        """Build a capability over only the skills in ``skills_dir`` that load on their own.

        One rejected ``SKILL.md`` fails the whole library, so probe each skill by name
        first: ``include`` filters before the loader parses anything, which pins a
        rejection to the skill that caused it and keeps the others. Returns ``None`` when
        nothing survives.

        Probing costs a construction per skill, and every construction walks the whole
        library's bundled files, so probes skip indexing its resources: a probe only has
        to answer whether the frontmatter parses, and the survivors' resources are indexed
        by the build below.
        """
        try:
            candidates = sorted(entry.name for entry in skills_dir.iterdir() if (entry / "SKILL.md").is_file())
        except OSError as e:
            logger.warning("Attempted to list the skills in %s. Failed because of: %s", skills_dir, e)
            return None

        usable = [
            name
            for name in candidates
            if self._load_skills(skills_dir, include=[name], index_resources=False) is not None
        ]
        if not usable:
            return None
        return self._load_skills(skills_dir, include=usable)

    @property
    def agent(self) -> Agent[Any, str]:
        return self._agent

    async def run_turn(self, turn: AgentTurn, message: UserMessage) -> AgentRunResult:
        """Run a turn for a new message, declining any calls a paused turn is waiting on."""
        return await self._run_turn_with_mcp_servers(turn, message=message, answers=None)

    async def resume_turn(self, turn: AgentTurn, answers: DeferredToolResults) -> AgentRunResult:
        """Continue a paused turn from the user's answers."""
        return await self._run_turn_with_mcp_servers(turn, message=None, answers=answers)

    async def _run_turn_with_mcp_servers(
        self, turn: AgentTurn, *, message: UserMessage | None, answers: DeferredToolResults | None
    ) -> AgentRunResult:
        """Run the turn with its MCP servers attached.

        Leases a live toolset per server from :attr:`mcp_toolset_cache` for the
        length of the turn, so an edited server is restarted on the next turn
        rather than the next engine restart.
        """
        if self.mcp_toolset_cache is None:
            if turn.mcp_servers:
                logger.warning(
                    "Attempted to give the agent MCP server(s) %s. Failed because this agent has no MCP "
                    "connections, so it will run without them.",
                    ", ".join(server.name for server in turn.mcp_servers),
                )
            return await self._run_turn_with_toolsets(turn, message, answers, [])
        configs = [{**server.config, "name": server.name} for server in turn.mcp_servers]
        # Nothing may sit between this and the `async with` that releases it: a
        # server left with a stray use count is never shut down.
        lease = await self.mcp_toolset_cache.acquire(configs)
        async with lease:
            # Config digests answer "my edit didn't take effect": a digest that
            # moved between runs says the new config really was read.
            logger.debug(
                "Agent run attached MCP server(s) %s",
                ", ".join(f"{server.name}@{server.digest}" for server in lease.resolved) or "none",
            )
            return await self._run_turn_with_toolsets(turn, message, answers, lease.toolsets)

    async def _run_turn_with_toolsets(
        self,
        turn: AgentTurn,
        message: UserMessage | None,
        answers: DeferredToolResults | None,
        toolsets: Sequence[AbstractToolset[Any]],
    ) -> AgentRunResult:
        grants = self.storage.get_thread_metadata(turn.thread_id).tool_grants
        return await self.run(
            message.content if message is not None else None,
            thread_id=turn.thread_id,
            event_sink=turn.event_sink,
            cancel_event=turn.cancel_event,
            persist_prompt=message.persist_content if message is not None else None,
            history_rehydrator=self.history_rehydrator,
            extra_toolsets=toolsets,
            extra_instructions=turn.instructions,
            extra_headers=turn.extra_headers,
            approval=ToolApprovalDeps(granted=frozenset(grants)),
            deferred_tool_results=answers,
        )

    @property
    def image_toolset(self) -> ImageGenerationToolset | None:
        return self._image_toolset

    async def run(  # noqa: PLR0913, C901
        self,
        prompt: str | Sequence[UserContent] | None,
        *,
        thread_id: str | None = None,
        token_sink: Callable[[str], Awaitable[None] | None] | None = None,
        event_sink: Callable[[RunEvent], Awaitable[None] | None] | None = None,
        cancel_event: asyncio.Event | None = None,
        persist_prompt: str | Sequence[UserContent] | None = None,
        history_rehydrator: Callable[[list[ModelMessage]], Awaitable[list[ModelMessage]]] | None = None,
        extra_toolsets: Sequence[AbstractToolset[Any]] | None = None,
        extra_instructions: str | None = None,
        extra_headers: Mapping[str, str] | None = None,
        approval: ToolApprovalDeps | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
    ) -> AgentRunResult:
        """Run the agent against ``prompt``, streaming events and saving history.

        Args:
            prompt: The user prompt for this turn. Either plain text or a
                sequence of Pydantic AI user-content parts (e.g. text plus
                inlined ``BinaryContent`` images) for multimodal input. ``None``
                resumes a paused turn from ``deferred_tool_results`` alone.
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
                cancelled turn is not persisted, except that a run given
                ``deferred_tool_results`` saves a result for each paused call it
                finished, started, or denied.
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
            extra_toolsets: Toolsets to attach to this run only, on top of the
                ones baked into the agent. Pydantic AI treats run-level toolsets
                as additive, so these do not replace ``mcp_servers``. Use this
                for toolsets whose configuration can change between runs - an
                MCP server the user just edited - so the run picks up the
                current config without rebuilding the agent.
            extra_instructions: Instructions to append for this run only, in the
                same additive spirit. Guidance that belongs to a toolset passed
                via ``extra_toolsets`` travels here, so the two stay in sync.
            extra_headers: HTTP headers to send on this run's model calls, and on
                any Griptape Cloud call a tool makes. Per run, because a value
                such as the budget attribution header follows the project open
                when the turn starts.
            approval: The thread's conversation grants, handed to every tool as
                ``ctx.deps``. ``None`` means no grants, so every gated call
                pauses for the user.
            deferred_tool_results: Answers for the calls a paused turn is
                waiting on. Required exactly when the thread has such calls and
                no ``prompt`` is given; with a ``prompt`` too, the prompt follows
                the answers in the same request to the model. A ``prompt`` on
                its own declines the calls the thread is waiting on.

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
        if prompt is None and deferred_tool_results is None:
            msg = f"Attempted to run the agent on thread {thread_id}. Failed because there was no message to send."
            raise ValueError(msg)
        waiting = pending_tool_calls(history)
        if waiting and deferred_tool_results is None:
            # Sending a message instead of answering an approval request declines
            # every call it was asking about, in the same request as the message.
            deferred_tool_results = DeferredToolResults(
                approvals={call.tool_call_id: ToolDenied(TOOL_DENIED_BY_NEW_MESSAGE) for call in waiting}
            )
        model_history = history
        if history_rehydrator is not None:
            model_history = await history_rehydrator(history)
        run_id = thread_id[:8]

        # Rebuilt every run so skill edits land without an engine restart.
        capabilities = self._build_skills_capabilities()

        logger.debug(
            "[run %s] start: model=%s history_len=%d skills=%s run_toolsets=%d prompt=%r",
            run_id,
            self.model_name,
            len(history),
            [name for capability in capabilities for name in capability.skill_names],
            len(extra_toolsets or []),
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
                # Passed per run rather than at construction so an agent built
                # elsewhere (tests swap one in) still accepts a paused turn.
                output_type=[str, DeferredToolRequests],
                message_history=model_history,
                deferred_tool_results=deferred_tool_results,
                deps=approval or ToolApprovalDeps(),
                usage_limits=self.usage_limits,
                event_stream_handler=event_handler,
                capabilities=capabilities or None,
                toolsets=list(extra_toolsets) if extra_toolsets else None,
                instructions=extra_instructions or None,
                model_settings={"extra_headers": dict(extra_headers)} if extra_headers else None,
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
            logger.debug(
                "[run %s] cancelled after %.2fs: tool_calls=%d partial_output=%r",
                run_id,
                time.monotonic() - started,
                counters.tool_calls,
                _preview(text),
            )
            saved = await self._save_cancelled_resume(thread_id, history, deferred_tool_results, counters, event_sink)
            return AgentRunResult(
                thread_id=thread_id,
                output=text,
                message_count=len(saved),
                image_urls=list(counters.image_urls),
                cancelled=True,
                pending_approvals=[pending_approval_for(call) for call in pending_tool_calls(saved)],
            )

        # Persist the pristine history plus only this turn's new messages. Using
        # ``new_messages()`` rather than ``all_messages()`` keeps any rehydrated
        # bytes in ``model_history`` out of what we write back to disk.
        new_messages = agent_result.new_messages()
        usage = agent_result.usage
        pending_approvals: list[PendingApproval] = []
        if isinstance(agent_result.output, DeferredToolRequests):
            pending_approvals = [pending_approval_for(call) for call in agent_result.output.approvals]

        elapsed = time.monotonic() - started
        text = "".join(text_buffer)
        truncated = _hit_output_limit(new_messages)
        _log_run_outcome(
            run_id=run_id,
            elapsed=elapsed,
            usage=usage,
            counters=counters,
            new_messages=new_messages,
            text=text,
            truncated=truncated,
            paused_on=len(pending_approvals),
        )

        # We persist `history + new_messages` rather than `all_messages()`, which
        # assumes the loaded history is a sequence of complete turns ending in a
        # ModelResponse. A trailing bare ModelRequest would mean a partial turn
        # was saved earlier, so the two ways of reconstructing the transcript can
        # drift; fail loud rather than silently persist a malformed history.
        # Normal turns always end in a ModelResponse and cancelled turns are
        # never saved. The one trailing request allowed is a paused turn's, which
        # holds only tool results: those of the calls that ran without asking, or
        # those a cancelled resume saved.
        turn_messages = list(new_messages)
        if history and not isinstance(history[-1], ModelResponse) and not ends_in_tool_results(history):
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
            truncated=truncated,
            pending_approvals=pending_approvals,
        )

    async def _save_cancelled_resume(
        self,
        thread_id: str,
        history: list[ModelMessage],
        deferred_tool_results: DeferredToolResults | None,
        counters: _RunCounters,
        event_sink: Callable[[RunEvent], Awaitable[None] | None] | None,
    ) -> list[ModelMessage]:
        """Save what a cancelled run did to the calls its paused turn was waiting on.

        A fresh turn that is cancelled saves nothing. A resumed one cannot: a tool
        it started keeps going after the cancel (an engine flow runs to the end),
        so leaving the call unanswered would let it be approved and run a second
        time. Returns the history as it now stands on disk.
        """
        if deferred_tool_results is None:
            return history
        settled = settle_cancelled_resume(
            history,
            deferred_tool_results,
            started_tool_call_ids=counters.started_tool_call_ids,
            tool_results=counters.tool_results,
        )
        if settled is None:
            return history
        self.storage.save_history(thread_id, settled.messages)
        # The editor shows a call as running until it sees a result for it.
        for part in settled.added_parts:
            await push_event(
                event_sink,
                ToolResult(
                    tool_call_id=part.tool_call_id,
                    tool_name=part.tool_name,
                    content=truncate(stringify(part.content), RESULT_TRANSPORT_BYTES),
                ),
            )
        return settled.messages

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
    # Tool calls the run reached, and the results it got back, keyed by tool_call_id.
    # Both are recorded before any await in the handler: pydantic-ai drives tool
    # execution from this handler's iteration, so a call cannot start before its
    # id is in `started_tool_call_ids`, and a cancel cannot fall between the two.
    started_tool_call_ids: set[str] = field(default_factory=set)
    tool_results: dict[str, ToolReturnPart | RetryPromptPart] = field(default_factory=dict)

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
            self.started_tool_call_ids.add(event.part.tool_call_id)
            self.tool_calls += 1
            args_str = _args_str(event.part.args)
            logger.debug(
                "[run %s] tool call #%d -> %s(%s) id=%s",
                self.run_id,
                self.tool_calls,
                event.part.tool_name,
                _preview(args_str),
                event.part.tool_call_id,
            )
            await push_event(
                event_sink,
                ToolCall(
                    tool_call_id=event.part.tool_call_id,
                    tool_name=event.part.tool_name,
                    args=args_json(event.part.args),
                ),
            )
        elif isinstance(event, FunctionToolResultEvent):
            part = getattr(event, "part", None) or getattr(event, "result", None)
            if isinstance(part, (ToolReturnPart, RetryPromptPart)):
                self.tool_results[event.tool_call_id] = part
            content = getattr(part, "content", None)
            tool_name = getattr(part, "tool_name", "?")
            is_error = isinstance(part, RetryPromptPart)
            content_str = stringify(content)
            if tool_name == IMAGE_TOOL_NAME and not is_error and content_str:
                self.image_urls.append(content_str)
            logger.debug(
                "[run %s] tool result <- %s id=%s preview=%r is_error=%s",
                self.run_id,
                tool_name,
                event.tool_call_id,
                _preview(content_str),
                is_error,
            )
            await push_event(
                event_sink,
                ToolResult(
                    tool_call_id=event.tool_call_id,
                    tool_name=tool_name,
                    content=truncate(content_str, RESULT_TRANSPORT_BYTES),
                    is_error=is_error,
                ),
            )
        elif isinstance(event, FinalResultEvent):
            self.final_result_emitted = True
            logger.debug(
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
            logger.debug("[run %s] text part #%d started", self.run_id, self.text_parts)
            if event.part.content:
                text_buffer.append(event.part.content)
                await _push_token(token_sink, event.part.content)
                await push_event(event_sink, TextDelta(delta=event.part.content))
        elif isinstance(event.part, ThinkingPart):
            if event.part.content:
                await push_event(event_sink, ThinkingDelta(delta=event.part.content))
        elif isinstance(event.part, ToolCallPart):
            logger.debug(
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
            await push_event(event_sink, TextDelta(delta=chunk))
        elif isinstance(event.delta, ThinkingPartDelta):
            chunk = event.delta.content_delta
            if not chunk:
                return
            await push_event(event_sink, ThinkingDelta(delta=chunk))


async def _push_token(
    token_sink: Callable[[str], Awaitable[None] | None] | None,
    token: str,
) -> None:
    if token_sink is None:
        return
    result = token_sink(token)
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


def _log_run_outcome(  # noqa: PLR0913
    *,
    run_id: str,
    elapsed: float,
    usage: Any,
    counters: _RunCounters,
    new_messages: Sequence[ModelMessage],
    text: str,
    truncated: bool,
    paused_on: int,
) -> None:
    """Log the one-line run summary plus any warnings the outcome warrants.

    Split out of :meth:`PydanticAgentRunner.run` to keep that method under the
    complexity limit; it is pure logging and has no effect on the result.
    """
    logger.debug(
        "[run %s] done in %.2fs: requests=%d tool_calls=%d "
        "input_tokens=%d output_tokens=%d new_messages=%d finish_reason=%s paused_on=%d output=%r",
        run_id,
        elapsed,
        usage.requests,
        counters.tool_calls,
        usage.input_tokens,
        usage.output_tokens,
        len(new_messages),
        _final_finish_reason(new_messages),
        paused_on,
        _preview(text),
    )
    if paused_on:
        # A turn waiting for approval has no final text by design.
        return
    if truncated:
        # Worth a warning even though the run "succeeded": the reply ends
        # mid-thought and looks identical to a completed one, so without this
        # line a cap is indistinguishable from a dropped stream.
        logger.warning(
            "[run %s] response truncated at the output token limit (output_tokens=%d). "
            "The reply is incomplete; raise max_tokens for this model to allow longer responses.",
            run_id,
            usage.output_tokens,
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


def _final_finish_reason(messages: Sequence[ModelMessage]) -> str | None:
    """Return the ``finish_reason`` of the turn's last model response, if any.

    Pydantic AI normalizes provider-specific reasons onto OpenTelemetry values
    (``stop``, ``length``, ``content_filter``, ``tool_call``, ``error``). The
    final response is the one that ended the turn; earlier ones in a multi-step
    run finish with ``tool_call`` and say nothing about how the turn ended.
    Returns ``None`` when the provider reported no reason.
    """
    for message in reversed(messages):
        if isinstance(message, ModelResponse):
            return message.finish_reason
    return None


def _hit_output_limit(messages: Sequence[ModelMessage]) -> bool:
    """Return True when the turn ended because the model ran out of output tokens.

    ``length`` is the normalized reason for hitting ``max_tokens`` — or, when no
    ``max_tokens`` was sent, whatever default the upstream provider imposed.
    """
    return _final_finish_reason(messages) == "length"


def _preview(value: str) -> str:
    if len(value) <= _LOG_PREVIEW_BYTES:
        return value
    return value[:_LOG_PREVIEW_BYTES] + "..."


def _prompt_preview(prompt: str | Sequence[UserContent] | None) -> str:
    """Render a log-safe preview of a prompt that may carry binary content.

    Text parts are previewed inline; non-text parts (images, audio, etc.) are
    rendered as a ``<TypeName>`` marker so a binary payload never hits the log.
    """
    if prompt is None:
        return "<resuming paused turn>"
    if isinstance(prompt, str):
        return _preview(prompt)
    parts = [_preview(item) if isinstance(item, str) else f"<{type(item).__name__}>" for item in prompt]
    return " ".join(parts)


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


def pending_approval_for(call: ToolCallPart) -> PendingApproval:
    """Describe a paused tool call the way clients are sent it."""
    return PendingApproval(
        tool_call_id=call.tool_call_id,
        tool_name=call.tool_name,
        args=args_json(call.args),
        grant_keys=grant_keys_for_call(call.tool_name, call.args_as_dict()),
    )
