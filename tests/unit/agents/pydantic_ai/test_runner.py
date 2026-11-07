"""Unit tests for `PydanticAgentRunner`.

These swap in `FunctionModel` so we exercise the persistence + streaming
plumbing without hitting Griptape Cloud.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
from pydantic_ai import Agent, RunContext
from pydantic_ai.exceptions import ApprovalRequired, UsageLimitExceeded
from pydantic_ai.messages import (
    BinaryContent,
    ImageUrl,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.tools import DeferredToolResults
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import UsageLimits

from griptape_nodes.agents.pydantic_ai.runner import (
    USAGE_LIMIT_NOTICE,
    PydanticAgentRunner,
    RunEvent,
    TextDelta,
    ToolCall,
    ToolResult,
    _run_messages,
)
from griptape_nodes.agents.pydantic_ai.tool_approval import pending_tool_calls
from griptape_nodes.drivers.cloud_models import MODEL_SETTINGS
from griptape_nodes.drivers.thread_storage.local_thread_storage_driver import LocalThreadStorageDriver

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from pydantic_ai.messages import FinishReason
    from pydantic_ai.settings import ModelSettings

# Anthropic's own default output cap, applied when a request carries no
# `max_tokens`. Deliberately a local literal rather than an import from
# pydantic-ai: the assertion's point is that we no longer depend on that value.
_PROVIDER_DEFAULT_MAX_TOKENS = 4096


def _model_settings_of(runner: PydanticAgentRunner) -> ModelSettings:
    """Return the settings baked into the runner's model.

    `Agent.model` is typed as `Model | KnownModelName | None`, so narrow to
    `Model` before reading `.settings` off it.
    """
    model = runner.agent.model
    assert isinstance(model, Model)
    return model.settings or {}


def _first_request(messages: list[ModelMessage]) -> ModelRequest:
    """The first message, narrowed off the request/response union."""
    first = messages[0]
    assert isinstance(first, ModelRequest)
    return first


def _runner_with_function_model(
    workspace: Path,
    threads_dir: Path,
    function: Callable[..., AsyncIterator[Any]],
    extra_tools: list[Callable[..., Any]] | None = None,
) -> PydanticAgentRunner:
    """Build a runner whose Agent uses a FunctionModel instead of GriptapeCloudModel."""
    storage = LocalThreadStorageDriver(threads_dir, config_manager=None, secrets_manager=None)  # type: ignore[arg-type]
    runner = PydanticAgentRunner(
        model_name="test",
        api_key="dummy",
        workspace_root=workspace,
        storage=storage,
        instructions="Be concise.",
    )
    new_agent: Agent[None, str] = Agent(FunctionModel(stream_function=function), instructions="Be concise.")
    for tool in extra_tools or []:
        new_agent.tool_plain(tool)
    runner._agent = new_agent
    return runner


@pytest.mark.asyncio
async def test_run_streams_tokens_and_persists_history(tmp_path: Path) -> None:
    """Tokens stream to the sink as they arrive and history persists to disk."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    threads_dir = tmp_path / "threads"

    async def stream(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        yield "Hi"
        yield " there"

    runner = _runner_with_function_model(workspace, threads_dir, stream)

    received: list[str] = []
    result = await runner.run("Greet me.", token_sink=received.append)

    assert result.output == "Hi there"
    assert "".join(received) == "Hi there"
    assert result.message_count >= 2  # noqa: PLR2004

    reloaded = runner.storage.load_history(result.thread_id)
    assert any(isinstance(p, TextPart) and "Hi there" in p.content for m in reloaded for p in getattr(m, "parts", []))


@pytest.mark.asyncio
async def test_run_cancel_event_returns_partial_and_skips_persist(tmp_path: Path) -> None:
    """Setting the cancel event mid-run returns partial text and persists nothing."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    threads_dir = tmp_path / "threads"

    hang = asyncio.Event()  # never set: the model blocks here until cancelled

    async def stream(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        yield "partial "
        await hang.wait()
        yield "never"

    runner = _runner_with_function_model(workspace, threads_dir, stream)

    cancel_event = asyncio.Event()
    got_token = asyncio.Event()
    received: list[str] = []

    def on_token(token: str) -> None:
        received.append(token)
        got_token.set()

    run_task = asyncio.create_task(runner.run("go", token_sink=on_token, cancel_event=cancel_event))
    # Wait until the first token is actually delivered, then cancel, so the
    # partial-output assertion is deterministic rather than racing the stream.
    await asyncio.wait_for(got_token.wait(), timeout=5)
    cancel_event.set()
    result = await asyncio.wait_for(run_task, timeout=5)

    assert result.cancelled is True
    assert result.output == "partial "
    assert result.message_count == 0
    # A cancelled turn leaves the thread untouched.
    assert runner.storage.load_history(result.thread_id) == []


@pytest.mark.asyncio
async def test_run_cancel_event_unset_runs_to_completion(tmp_path: Path) -> None:
    """Passing a cancel event that is never set does not disturb a normal run."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    threads_dir = tmp_path / "threads"

    async def stream(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        yield "all done"

    runner = _runner_with_function_model(workspace, threads_dir, stream)
    result = await runner.run("go", cancel_event=asyncio.Event())

    assert result.cancelled is False
    assert result.output == "all done"
    assert result.message_count >= 2  # noqa: PLR2004


def _endless_tool_caller(call_ids: list[str]) -> Callable[..., AsyncIterator[Any]]:
    """A model stream that calls `ping` on every request and never finishes the turn."""

    async def stream(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[Any]:
        call_id = f"c{len(call_ids)}"
        call_ids.append(call_id)
        yield {0: DeltaToolCall(name="ping", json_args="{}", tool_call_id=call_id)}

    return stream


def ping() -> str:
    """A tool that always succeeds."""
    return "pong"


@pytest.mark.asyncio
async def test_run_at_request_limit_saves_the_turn_and_says_it_stopped(tmp_path: Path) -> None:
    """A turn that reaches the request limit keeps what it did and tells the user why it stopped."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    threads_dir = tmp_path / "threads"
    call_ids: list[str] = []
    runner = _runner_with_function_model(workspace, threads_dir, _endless_tool_caller(call_ids), extra_tools=[ping])
    runner.usage_limits = UsageLimits(request_limit=2)

    events: list[RunEvent] = []
    result = await runner.run("Build it.", event_sink=events.append)

    assert result.output == USAGE_LIMIT_NOTICE
    assert events[-1] == TextDelta(delta=USAGE_LIMIT_NOTICE)
    history = runner.storage.load_history(result.thread_id)
    assert result.message_count == len(history)
    prompts = [p.content for m in history for p in m.parts if isinstance(p, UserPromptPart)]
    assert prompts == ["Build it."]
    saved_returns = [p.tool_call_id for m in history for p in m.parts if isinstance(p, ToolReturnPart)]
    assert saved_returns == call_ids
    last = history[-1]
    assert isinstance(last, ModelResponse)
    assert last.parts == [TextPart(content=USAGE_LIMIT_NOTICE)]


@pytest.mark.asyncio
async def test_run_after_request_limit_continues_from_the_saved_turn(tmp_path: Path) -> None:
    """The next message in the thread sends the stopped turn's work back to the model."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    threads_dir = tmp_path / "threads"
    call_ids: list[str] = []
    runner = _runner_with_function_model(workspace, threads_dir, _endless_tool_caller(call_ids), extra_tools=[ping])
    runner.usage_limits = UsageLimits(request_limit=2)
    stopped = await runner.run("Build it.")

    seen: list[list[ModelMessage]] = []

    async def finish(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        seen.append(messages)
        yield "Done."

    runner._agent = Agent(FunctionModel(stream_function=finish))
    result = await runner.run("continue", thread_id=stopped.thread_id)

    assert result.output == "Done."
    sent_returns = [p.tool_call_id for m in seen[0] for p in m.parts if isinstance(p, ToolReturnPart)]
    assert sent_returns == call_ids


@pytest.mark.asyncio
async def test_resumed_turn_at_request_limit_saves_its_answers_once(tmp_path: Path) -> None:
    """A resumed turn that reaches the limit saves each call's result once and leaves nothing waiting."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    threads_dir = tmp_path / "threads"
    call_ids: list[str] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[Any]:
        if not call_ids:
            call_ids.extend(["g1", "p1"])
            yield {
                0: DeltaToolCall(name="gated", json_args="{}", tool_call_id="g1"),
                1: DeltaToolCall(name="ping", json_args="{}", tool_call_id="p1"),
            }
            return
        async for chunk in _endless_tool_caller(call_ids)(messages, info):
            yield chunk

    runner = _runner_with_function_model(workspace, threads_dir, stream, extra_tools=[ping])

    @runner.agent.tool
    def gated(ctx: RunContext[Any]) -> str:
        if not ctx.tool_call_approved:
            raise ApprovalRequired
        return "ran"

    paused = await runner.run("Build it.")
    assert [pending.tool_call_id for pending in paused.pending_approvals] == ["g1"]

    runner.usage_limits = UsageLimits(request_limit=2)
    result = await runner.run(
        None, thread_id=paused.thread_id, deferred_tool_results=DeferredToolResults(approvals={"g1": True})
    )

    assert result.output == USAGE_LIMIT_NOTICE
    history = runner.storage.load_history(paused.thread_id)
    saved_returns = sorted(p.tool_call_id for m in history for p in m.parts if isinstance(p, ToolReturnPart))
    assert saved_returns == sorted(call_ids)
    assert pending_tool_calls(history) == []


@pytest.mark.asyncio
async def test_run_at_request_limit_before_any_tool_result_saves_nothing(tmp_path: Path) -> None:
    """A turn stopped before it has any tool results to keep re-raises and leaves the thread untouched."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    threads_dir = tmp_path / "threads"
    runner = _runner_with_function_model(workspace, threads_dir, _endless_tool_caller([]), extra_tools=[ping])
    runner.usage_limits = UsageLimits(request_limit=0)
    thread_id, _ = runner.storage.create_thread()

    with pytest.raises(UsageLimitExceeded):
        await runner.run("Build it.", thread_id=thread_id)

    assert runner.storage.load_history(thread_id) == []


@pytest.mark.asyncio
async def test_run_at_request_limit_sets_the_notice_apart_from_streamed_text(tmp_path: Path) -> None:
    """Text the turn already streamed is followed by the notice on its own paragraph."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    threads_dir = tmp_path / "threads"
    call_ids: list[str] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[Any]:
        yield "Working."
        async for chunk in _endless_tool_caller(call_ids)(messages, info):
            yield chunk

    runner = _runner_with_function_model(workspace, threads_dir, stream, extra_tools=[ping])
    runner.usage_limits = UsageLimits(request_limit=2)

    received: list[str] = []
    result = await runner.run("Build it.", token_sink=received.append)

    notice = f"\n\n{USAGE_LIMIT_NOTICE}"
    assert result.output == "Working." * len(call_ids) + notice
    assert received[-1] == notice


@pytest.mark.asyncio
async def test_run_at_request_limit_saves_the_persist_prompt(tmp_path: Path) -> None:
    """A turn stopped at the limit stores `persist_prompt`, not the live prompt."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    threads_dir = tmp_path / "threads"
    runner = _runner_with_function_model(workspace, threads_dir, _endless_tool_caller([]), extra_tools=[ping])
    runner.usage_limits = UsageLimits(request_limit=2)

    result = await runner.run("Build it with these bytes.", persist_prompt="Build it.")

    history = runner.storage.load_history(result.thread_id)
    prompts = [p.content for m in history for p in m.parts if isinstance(p, UserPromptPart)]
    assert prompts == ["Build it."]


def test_run_messages_is_empty_when_the_last_message_has_no_run_id() -> None:
    """Messages with no `run_id` can't be told apart from loaded history, so none count as the run's."""
    messages: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart(content="hi")])]

    assert _run_messages(messages) == []


@pytest.mark.asyncio
async def test_history_carries_across_runs(tmp_path: Path) -> None:
    """Calling `run` with the same thread_id feeds prior history back to the model."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    threads_dir = tmp_path / "threads"

    seen_history: list[int] = []

    async def stream(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        seen_history.append(len(messages))
        yield "ok"

    runner = _runner_with_function_model(workspace, threads_dir, stream)

    first = await runner.run("turn 1")
    second = await runner.run("turn 2", thread_id=first.thread_id)

    assert seen_history[0] < seen_history[1]
    assert first.thread_id == second.thread_id


@pytest.mark.asyncio
async def test_run_raises_when_history_ends_without_response(tmp_path: Path) -> None:
    """A partial-turn history (no trailing ModelResponse) fails loud, not silently.

    Persisting `history + new_messages` would duplicate the trailing request's
    parts if pydantic-ai merged this turn into it, so run() refuses.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    threads_dir = tmp_path / "threads"

    async def stream(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        yield "ok"

    runner = _runner_with_function_model(workspace, threads_dir, stream)
    thread_id, _ = runner.storage.create_thread()
    # A history that ends with a bare request (no assistant response) is the
    # partial-turn state the guard rejects.
    runner.storage.save_history(thread_id, [ModelRequest(parts=[UserPromptPart(content="orphan")])])

    with pytest.raises(ValueError, match="not a ModelResponse"):
        await runner.run("next", thread_id=thread_id)


@pytest.mark.asyncio
async def test_tool_call_round_trips_through_runner(tmp_path: Path) -> None:
    """A tool call from the model invokes a registered tool and lands in history."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "data.txt").write_text("payload-7")
    threads_dir = tmp_path / "threads"

    def read_file(path: str) -> str:
        return (workspace / path).read_text()

    call_count = 0

    async def stream(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[Any]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield {0: DeltaToolCall(name="read_file", json_args='{"path": "data.txt"}', tool_call_id="c1")}
            return
        for ch in "Got it.":
            yield ch

    runner = _runner_with_function_model(workspace, threads_dir, stream, extra_tools=[read_file])
    result = await runner.run("Read data.txt and confirm.")
    assert "Got it." in result.output
    history = runner.storage.load_history(result.thread_id)
    assert any(
        isinstance(p, ToolCallPart) and p.tool_name == "read_file" for m in history for p in getattr(m, "parts", [])
    )


@pytest.mark.asyncio
async def test_workspace_skill_reaches_the_model_on_a_run(tmp_path: Path) -> None:
    """A skill in the workspace is offered to the model, so the run wiring is not silently dropped."""
    workspace = tmp_path / "ws"
    skill_dir = workspace / ".agents/skills/demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Demo skill description.\n---\n\nGuidance for the task."
    )
    threads_dir = tmp_path / "threads"

    offered: list[AgentInfo] = []

    async def stream(_messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        offered.append(info)
        yield "Done."

    runner = _runner_with_function_model(workspace, threads_dir, stream)
    await runner.run("Use the demo skill.")

    assert len(offered) == 1
    # Each skill is a deferred capability: the model sees it in the catalog instructions
    # and pulls its guidance in with `load_capability`.
    assert "load_capability" in {tool.name for tool in offered[0].function_tools}
    assert "demo-skill" in (offered[0].instructions or "")


@pytest.mark.asyncio
async def test_run_captures_generate_image_urls(tmp_path: Path) -> None:
    """URLs returned by the `generate_image` tool surface on the run result."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    threads_dir = tmp_path / "threads"

    def generate_image(prompt: str, negative_prompt: str = "") -> str:  # noqa: ARG001
        return "https://files.local/generated.png"

    call_count = 0

    async def stream(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[Any]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield {0: DeltaToolCall(name="generate_image", json_args='{"prompt": "a cat"}', tool_call_id="img1")}
            return
        for ch in "Here you go.":
            yield ch

    runner = _runner_with_function_model(workspace, threads_dir, stream, extra_tools=[generate_image])
    result = await runner.run("Make a cat.")

    assert result.image_urls == ["https://files.local/generated.png"]
    assert "Here you go." in result.output


@pytest.mark.asyncio
async def test_run_sends_extra_headers_on_model_calls(tmp_path: Path) -> None:
    """Per-run headers, such as budget attribution, reach every model call of the run."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    threads_dir = tmp_path / "threads"
    seen: list[Any] = []

    async def stream(_messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        seen.append((info.model_settings or {}).get("extra_headers"))
        yield "ok"

    runner = _runner_with_function_model(workspace, threads_dir, stream)
    await runner.run("Hi", extra_headers={"X-Griptape-Attribution": "abc"})
    await runner.run("Again")

    assert seen == [{"X-Griptape-Attribution": "abc"}, None]


@pytest.mark.asyncio
async def test_event_sink_receives_text_tool_call_and_tool_result(tmp_path: Path) -> None:
    """The structured event sink sees text deltas, tool calls, and tool results.

    The chat sidebar drives tool-call cards and the streaming text bubble off
    these events, so this guards the wire format.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "data.txt").write_text("payload-7")
    threads_dir = tmp_path / "threads"

    def read_file(path: str) -> str:
        return (workspace / path).read_text()

    call_count = 0

    async def stream(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[Any]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield {0: DeltaToolCall(name="read_file", json_args='{"path": "data.txt"}', tool_call_id="c1")}
            return
        for ch in "Got it.":
            yield ch

    runner = _runner_with_function_model(workspace, threads_dir, stream, extra_tools=[read_file])

    events: list[RunEvent] = []
    await runner.run("Read data.txt and confirm.", event_sink=events.append)

    tool_calls = [e for e in events if isinstance(e, ToolCall)]
    tool_results = [e for e in events if isinstance(e, ToolResult)]
    text_deltas = [e for e in events if isinstance(e, TextDelta)]

    assert len(tool_calls) == 1
    assert tool_calls[0].tool_name == "read_file"
    assert tool_calls[0].tool_call_id == "c1"
    assert '"path": "data.txt"' in tool_calls[0].args

    assert len(tool_results) == 1
    assert tool_results[0].tool_call_id == "c1"
    assert "payload-7" in tool_results[0].content
    assert tool_results[0].is_error is False

    assert "".join(d.delta for d in text_deltas) == "Got it."


@pytest.mark.asyncio
async def test_persist_prompt_swaps_binary_for_image_url_in_saved_history(tmp_path: Path) -> None:
    """`persist_prompt` stores the URL-reference form, not the live bytes."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    threads_dir = tmp_path / "threads"

    async def stream(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        yield "done"

    runner = _runner_with_function_model(workspace, threads_dir, stream)

    url = "http://localhost:8124/workspace/cat.png"
    live = ["look", BinaryContent(data=b"png-bytes", media_type="image/png")]
    persist = ["look", ImageUrl(url=url)]
    result = await runner.run(live, persist_prompt=persist)

    reloaded = runner.storage.load_history(result.thread_id)
    user_parts = [
        part
        for message in reloaded
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart)
    ]
    assert len(user_parts) == 1
    content = user_parts[0].content
    assert any(isinstance(item, ImageUrl) and item.url == url for item in content)
    assert not any(isinstance(item, BinaryContent) for item in content)


@pytest.mark.asyncio
async def test_history_rehydrator_transforms_loaded_history_before_model_call(tmp_path: Path) -> None:
    """The rehydrator runs on loaded history before it reaches the model."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    threads_dir = tmp_path / "threads"

    seen_by_model: list[list[ModelMessage]] = []

    async def stream(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        seen_by_model.append(list(messages))
        yield "ok"

    runner = _runner_with_function_model(workspace, threads_dir, stream)

    # Seed a thread whose history carries an ImageUrl reference. A realistic
    # saved turn ends with a ModelResponse, which run() requires before it
    # appends the next turn.
    url = "http://localhost:8124/workspace/cat.png"
    thread_id, _ = runner.storage.create_thread()
    seed: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content=["look", ImageUrl(url=url)])]),
        ModelResponse(parts=[TextPart(content="a cat")]),
    ]
    runner.storage.save_history(thread_id, seed)

    # A non-mutating rehydrator, per the run() contract: returns fresh objects
    # and leaves the input pristine so the on-disk form is never touched.
    async def rehydrate(messages: list[ModelMessage]) -> list[ModelMessage]:
        rehydrated: list[ModelMessage] = []
        for message in messages:
            if not isinstance(message, ModelRequest):
                rehydrated.append(message)
                continue
            new_parts: list[Any] = []
            for part in message.parts:
                if isinstance(part, UserPromptPart) and not isinstance(part.content, str):
                    new_parts.append(
                        replace(
                            part,
                            content=[
                                BinaryContent(data=b"bytes", media_type="image/png")
                                if isinstance(item, ImageUrl)
                                else item
                                for item in part.content
                            ],
                        )
                    )
                else:
                    new_parts.append(part)
            rehydrated.append(replace(message, parts=new_parts))
        return rehydrated

    await runner.run("follow up", thread_id=thread_id, history_rehydrator=rehydrate)

    # The model saw the rehydrated history: the ImageUrl became BinaryContent.
    replayed_user_parts = [
        part
        for message in seen_by_model[0]
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart) and not isinstance(part.content, str)
    ]
    assert any(isinstance(item, BinaryContent) for part in replayed_user_parts for item in part.content)
    assert not any(isinstance(item, ImageUrl) for part in replayed_user_parts for item in part.content)

    # Regression: the rehydrated bytes must NOT be written back to disk. The
    # prior turn stays an ImageUrl reference so history never re-bloats.
    reloaded = runner.storage.load_history(thread_id)
    prior_user_parts = [
        part
        for message in reloaded
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart) and not isinstance(part.content, str)
    ]
    assert any(isinstance(item, ImageUrl) for part in prior_user_parts for item in part.content)
    assert not any(isinstance(item, BinaryContent) for part in prior_user_parts for item in part.content)


class _FinishReasonModel(FunctionModel):
    """A `FunctionModel` whose streamed response reports a chosen `finish_reason`.

    The runner always takes the streaming path (it passes an
    `event_stream_handler`), and `FunctionModel`'s `stream_function` yields only
    text deltas — it never sets `finish_reason`, which lives on the
    `StreamedResponse` and is read when the final `ModelResponse` is built. So
    the reason is stamped onto the response object before the stream is consumed.
    """

    _finish_reason: FinishReason | None

    def __init__(self, text: str, finish_reason: FinishReason | None) -> None:
        async def stream(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
            yield text

        super().__init__(stream_function=stream)
        self._finish_reason = finish_reason

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: Any = None,
        model_request_parameters: Any = None,
        run_context: Any = None,
    ) -> AsyncIterator[Any]:
        async with super().request_stream(
            messages, model_settings, model_request_parameters, run_context
        ) as streamed_response:
            streamed_response.finish_reason = self._finish_reason
            yield streamed_response


def _runner_with_finish_reason(
    workspace: Path,
    threads_dir: Path,
    text: str,
    finish_reason: FinishReason | None,
) -> PydanticAgentRunner:
    """Build a runner whose model streams `text` and reports `finish_reason`."""
    storage = LocalThreadStorageDriver(threads_dir, config_manager=None, secrets_manager=None)  # type: ignore[arg-type]
    runner = PydanticAgentRunner(
        model_name="test",
        api_key="dummy",
        workspace_root=workspace,
        storage=storage,
    )
    runner._agent = Agent(_FinishReasonModel(text, finish_reason))
    return runner


@pytest.mark.asyncio
async def test_run_flags_truncation_when_output_limit_hit(tmp_path: Path) -> None:
    """A `length` finish reason surfaces as `truncated=True` on the result."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    threads_dir = tmp_path / "threads"

    runner = _runner_with_finish_reason(workspace, threads_dir, "an essay that stops mid-", "length")
    result = await runner.run("write a long essay")

    assert result.truncated is True
    # The partial text is still returned and persisted — a capped reply is
    # incomplete, not worthless, and the user should see what they got.
    assert result.output == "an essay that stops mid-"
    assert result.message_count >= 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_run_does_not_flag_truncation_on_normal_stop(tmp_path: Path) -> None:
    """A `stop` finish reason is an ordinary completed turn."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    threads_dir = tmp_path / "threads"

    runner = _runner_with_finish_reason(workspace, threads_dir, "all done.", "stop")
    result = await runner.run("say something short")

    assert result.truncated is False
    assert result.output == "all done."


@pytest.mark.asyncio
async def test_run_does_not_flag_truncation_when_reason_absent(tmp_path: Path) -> None:
    """A provider that reports no finish reason must not read as truncated."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    threads_dir = tmp_path / "threads"

    runner = _runner_with_finish_reason(workspace, threads_dir, "no reason given", None)
    result = await runner.run("go")

    assert result.truncated is False


@pytest.mark.asyncio
async def test_streamed_run_is_not_truncated(tmp_path: Path) -> None:
    """The normal streaming path reports `truncated=False`, not None-ish."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    threads_dir = tmp_path / "threads"

    async def stream(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        yield "fine"

    runner = _runner_with_function_model(workspace, threads_dir, stream)
    result = await runner.run("go")

    assert result.truncated is False


def test_runner_applies_catalog_max_tokens_to_the_model(tmp_path: Path) -> None:
    """The sidebar's default construction sends the catalog's max_tokens.

    This is the regression guard for the reported bug: the runner passed no
    settings at all, so the provider's own 4096-token default silently capped
    every Claude reply.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    storage = LocalThreadStorageDriver(tmp_path / "threads", config_manager=None, secrets_manager=None)  # type: ignore[arg-type]

    runner = PydanticAgentRunner(
        model_name="claude-opus-5",
        api_key="dummy",
        workspace_root=workspace,
        storage=storage,
    )

    settings = _model_settings_of(runner)
    assert settings.get("max_tokens") == MODEL_SETTINGS["claude-opus-5"]["max_tokens"]
    assert settings.get("max_tokens", 0) > _PROVIDER_DEFAULT_MAX_TOKENS


def test_runner_explicit_settings_override_the_catalog(tmp_path: Path) -> None:
    """An explicit `model_settings` wins over the catalog preset."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    storage = LocalThreadStorageDriver(tmp_path / "threads", config_manager=None, secrets_manager=None)  # type: ignore[arg-type]

    runner = PydanticAgentRunner(
        model_name="claude-opus-5",
        api_key="dummy",
        workspace_root=workspace,
        storage=storage,
        model_settings={"max_tokens": 1234},
    )

    assert _model_settings_of(runner).get("max_tokens") == 1234  # noqa: PLR2004


@pytest.mark.asyncio
async def test_run_level_toolsets_and_instructions_are_additive(tmp_path: Path) -> None:
    """Per-run toolsets and instructions add to the agent's, they don't replace them.

    This is what lets an MCP server the user just edited be attached to the next
    run without rebuilding the agent: the agent keeps its own tools and base
    instructions, and the run adds the server's tools and rules on top.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    threads_dir = tmp_path / "threads"

    seen: dict[str, Any] = {}

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        seen["tools"] = sorted(tool.name for tool in info.function_tools)
        seen["instructions"] = _first_request(messages).instructions
        yield "ok"

    def baked_tool() -> str:
        """A tool the agent was built with."""
        return "baked"

    def per_run_tool() -> str:
        """A tool attached for this run only."""
        return "per-run"

    runner = _runner_with_function_model(workspace, threads_dir, stream, extra_tools=[baked_tool])

    await runner.run(
        "Go.",
        extra_toolsets=[FunctionToolset(tools=[per_run_tool])],
        extra_instructions="Rules for MCP server 'svc':\nbe terse",
    )

    assert seen["tools"] == ["baked_tool", "per_run_tool"]
    assert seen["instructions"] == "Be concise.\n\nRules for MCP server 'svc':\nbe terse"


@pytest.mark.asyncio
async def test_run_without_extras_leaves_the_agent_untouched(tmp_path: Path) -> None:
    """Passing no per-run extras must not blank the agent's own instructions."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    threads_dir = tmp_path / "threads"

    seen: dict[str, Any] = {}

    async def stream(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        seen["instructions"] = _first_request(messages).instructions
        yield "ok"

    runner = _runner_with_function_model(workspace, threads_dir, stream)

    await runner.run("Go.")

    assert seen["instructions"] == "Be concise."
