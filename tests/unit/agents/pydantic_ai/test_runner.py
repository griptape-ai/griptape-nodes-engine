"""Unit tests for `PydanticAgentRunner`.

These swap in `FunctionModel` so we exercise the persistence + streaming
plumbing without hitting Griptape Cloud.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    BinaryContent,
    ImageUrl,
    ModelMessage,
    ModelRequest,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from griptape_nodes.agents.pydantic_ai.runner import (
    PydanticAgentRunner,
    RunEvent,
    TextDelta,
    ToolCall,
    ToolResult,
)
from griptape_nodes.drivers.thread_storage.local_thread_storage_driver import LocalThreadStorageDriver

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path


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

    # Seed a thread whose history carries an ImageUrl reference.
    url = "http://localhost:8124/workspace/cat.png"
    thread_id, _ = runner.storage.create_thread()
    seed: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart(content=["look", ImageUrl(url=url)])])]
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
