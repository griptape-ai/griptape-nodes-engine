"""A fake CLI exercises real subprocess I/O without installed coding agents."""

from __future__ import annotations

import asyncio
import json
import re
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import pytest
from pydantic_ai.messages import (
    BinaryContent,
    ImageUrl,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from griptape_nodes.agents.cli.base import (
    CliEvent,
    CliHarness,
    CliHarnessError,
    CliInvocation,
    CliStreamParser,
    CliTurnContext,
    SessionStarted,
    TurnEnded,
)
from griptape_nodes.agents.harness import (
    AgentTurn,
    MCPServerSpec,
    RunEvent,
    TextDelta,
    ToolApprovalHarness,
    ToolCall,
    ToolResult,
    UserMessage,
)
from griptape_nodes.agents.thread_history import TOOL_DENIED_BY_NEW_MESSAGE, pending_tool_calls
from griptape_nodes.drivers.thread_storage.base_thread_storage_driver import HarnessSession
from griptape_nodes.drivers.thread_storage.local_thread_storage_driver import LocalThreadStorageDriver

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage

_FAKE_CLI = textwrap.dedent(
    """
    import json, os, sys, time
    out = os.environ["FAKE_CLI_OUT"]
    calls = json.load(open(out)) if os.path.exists(out) else []
    calls.append({"argv": sys.argv[1:], "stdin": sys.stdin.read()})
    json.dump(calls, open(out, "w"))
    script = json.loads(os.environ["FAKE_CLI_SCRIPT"])
    runs = script["runs"]
    run = runs[min(len(calls), len(runs)) - 1]
    for step in run.get("records", []):
        if "sleep" in step:
            time.sleep(step["sleep"])
            continue
        print(json.dumps(step), flush=True)
    sys.stderr.write(run.get("stderr", ""))
    sys.exit(run.get("exit", 0))
    """
)


class _FakeParser(CliStreamParser):
    def feed(self, record: dict[str, Any]) -> list[CliEvent]:
        kind = record.get("t")
        if kind == "session":
            return [SessionStarted(session_id=record["id"])]
        if kind == "text":
            return [TextDelta(delta=record["d"])]
        if kind == "call":
            return [ToolCall(tool_call_id=record["id"], tool_name=record["name"], args=json.dumps(record["args"]))]
        if kind == "result":
            return [ToolResult(tool_call_id=record["id"], tool_name=record["name"], content=record["content"])]
        if kind == "end":
            return [TurnEnded(truncated=record.get("truncated", False), error=record.get("error"))]
        return []


@dataclass
class _FakeHarness(CliHarness):
    harness_id: ClassVar[str] = "fake"
    display_name: ClassVar[str] = "Fake CLI"
    executable: ClassVar[str] = "fake-cli-that-does-not-exist"
    install_hint: ClassVar[str] = "Install the fake CLI."

    script_path: Path = field(default_factory=Path)
    out_path: Path = field(default_factory=Path)
    runs: list[dict[str, Any]] = field(default_factory=list)
    contexts: list[CliTurnContext] = field(default_factory=list)

    def build_invocation(self, context: CliTurnContext) -> CliInvocation:
        self.contexts.append(context)
        args = [str(self.script_path)]
        if context.resume_session_id:
            args += ["--resume", context.resume_session_id]
        return CliInvocation(
            args=args,
            stdin=context.prompt_text.encode("utf-8"),
            env={"FAKE_CLI_OUT": str(self.out_path), "FAKE_CLI_SCRIPT": json.dumps({"runs": self.runs})},
        )

    def new_parser(self) -> CliStreamParser:
        return _FakeParser()

    def invocations(self) -> list[dict[str, Any]]:
        return json.loads(self.out_path.read_text())


def _ok_run(text: str = "Done.", session_id: str = "s-1") -> dict[str, Any]:
    return {"records": [{"t": "session", "id": session_id}, {"t": "text", "d": text}, {"t": "end"}]}


@pytest.fixture
def storage(tmp_path: Path) -> LocalThreadStorageDriver:
    """Use temporary thread storage."""
    return LocalThreadStorageDriver(tmp_path / "threads", config_manager=None, secrets_manager=None)  # type: ignore[arg-type]


def _harness(tmp_path: Path, storage: LocalThreadStorageDriver, *runs: dict[str, Any]) -> _FakeHarness:
    script = tmp_path / "fake_cli.py"
    script.write_text(_FAKE_CLI, encoding="utf-8")
    return _FakeHarness(
        workspace_root=tmp_path,
        storage=storage,
        engine_mcp_url="http://localhost:1/mcp/",
        instructions="Base instructions.",
        command=sys.executable,
        script_path=script,
        out_path=tmp_path / "calls.json",
        runs=list(runs),
    )


@pytest.mark.asyncio
async def test_turn_streams_events_and_saves_the_transcript(tmp_path: Path, storage: LocalThreadStorageDriver) -> None:
    """Persist streamed events and the CLI session."""
    run = {
        "records": [
            {"t": "session", "id": "s-1"},
            {"t": "text", "d": "Looking. "},
            {"t": "call", "id": "c1", "name": "GriptapeNodes_ListRegisteredLibrariesRequest", "args": {}},
            {"t": "result", "id": "c1", "name": "GriptapeNodes_ListRegisteredLibrariesRequest", "content": "[]"},
            {"t": "text", "d": "No libraries."},
            {"t": "end"},
        ]
    }
    harness = _harness(tmp_path, storage, run)
    thread_id, _ = storage.create_thread()
    events: list[RunEvent] = []

    result = await harness.run_turn(
        AgentTurn(thread_id=thread_id, event_sink=events.append), UserMessage("List libraries")
    )

    assert result.output == "Looking. No libraries."
    assert [type(event).__name__ for event in events] == ["TextDelta", "ToolCall", "ToolResult", "TextDelta"]
    history = storage.load_history(thread_id)
    assert result.message_count == len(history)
    assert isinstance(history[0], ModelRequest)
    assert isinstance(history[0].parts[0], UserPromptPart)
    assert history[0].parts[0].content == "List libraries"
    assert isinstance(history[1], ModelResponse)
    assert [part.part_kind for part in history[1].parts] == ["text", "tool-call"]
    assert history[1].provider_name == "fake"
    assert isinstance(history[2], ModelRequest)
    assert isinstance(history[2].parts[0], ToolReturnPart)
    assert isinstance(history[3], ModelResponse)
    assert storage.get_harness_session(thread_id, "fake") == HarnessSession(
        session_id="s-1", message_count=len(history)
    )


@pytest.mark.asyncio
async def test_tool_call_ids_a_cli_reuses_are_saved_unique(tmp_path: Path, storage: LocalThreadStorageDriver) -> None:
    """Keep ids unique across turns and within the characters and length every provider accepts."""
    name = "GriptapeNodes_ListRegisteredLibrariesRequest"
    long_id = "toolu_01ABCDEFGHIJKLMNOPQRSTUVWX"
    run = {
        "records": [
            {"t": "session", "id": "s-1"},
            {"t": "call", "id": "item.1", "name": name, "args": {}},
            {"t": "result", "id": "item.1", "name": name, "content": "[]"},
            {"t": "call", "id": long_id, "name": name, "args": {}},
            {"t": "result", "id": long_id, "name": name, "content": "[]"},
            {"t": "end"},
        ]
    }
    harness = _harness(tmp_path, storage, run, run)
    thread_id, _ = storage.create_thread()
    events: list[RunEvent] = []

    prompts = ["one", "two"]
    for prompt in prompts:
        await harness.run_turn(AgentTurn(thread_id=thread_id, event_sink=events.append), UserMessage(prompt))

    history = storage.load_history(thread_id)
    call_ids = [part.tool_call_id for message in history for part in message.parts if isinstance(part, ToolCallPart)]
    return_ids = [
        part.tool_call_id for message in history for part in message.parts if isinstance(part, ToolReturnPart)
    ]
    assert len(set(call_ids)) == len(prompts) * 2
    assert return_ids == call_ids
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{1,40}", call_id) for call_id in call_ids)
    streamed = [event.tool_call_id for event in events if isinstance(event, ToolCall)]
    assert streamed == call_ids


@pytest.mark.asyncio
async def test_next_turn_resumes_the_session_without_catch_up(
    tmp_path: Path, storage: LocalThreadStorageDriver
) -> None:
    """Resume with only the new prompt."""
    harness = _harness(tmp_path, storage, _ok_run("First."), _ok_run("Second."))
    thread_id, _ = storage.create_thread()

    await harness.run_turn(AgentTurn(thread_id=thread_id), UserMessage("one"))
    await harness.run_turn(AgentTurn(thread_id=thread_id), UserMessage("two"))

    second = harness.invocations()[1]
    assert second["argv"][-2:] == ["--resume", "s-1"]
    assert second["stdin"] == "two"


@pytest.mark.asyncio
async def test_messages_added_by_another_provider_are_sent_as_catch_up(
    tmp_path: Path, storage: LocalThreadStorageDriver
) -> None:
    """Include unseen history before the new prompt."""
    harness = _harness(tmp_path, storage, _ok_run())
    thread_id, _ = storage.create_thread()
    earlier: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="Make a cat node")]),
        ModelResponse(parts=[TextPart(content="Made it.")]),
    ]
    storage.save_history(thread_id, earlier)
    storage.set_harness_session(thread_id, "fake", HarnessSession(session_id="s-0", message_count=0))

    await harness.run_turn(AgentTurn(thread_id=thread_id), UserMessage("Now a dog"))

    call = harness.invocations()[0]
    assert call["argv"][-2:] == ["--resume", "s-0"]
    assert "User: Make a cat node" in call["stdin"]
    assert "Assistant: Made it." in call["stdin"]
    assert call["stdin"].endswith("Now a dog")


@pytest.mark.asyncio
async def test_instructions_and_mcp_servers_reach_the_invocation(
    tmp_path: Path, storage: LocalThreadStorageDriver
) -> None:
    """Combine instructions and attach the engine's MCP server first."""
    harness = _harness(tmp_path, storage, _ok_run())
    thread_id, _ = storage.create_thread()

    await harness.run_turn(
        AgentTurn(
            thread_id=thread_id,
            instructions="Server rules.",
            mcp_servers=[MCPServerSpec(name="docs", config={"transport": "streamable_http", "url": "http://d/mcp"})],
        ),
        UserMessage("hi"),
    )

    context = harness.contexts[0]
    assert context.instructions == "Base instructions.\n\nServer rules."
    assert [(server.name, server.url) for server in context.mcp_servers] == [
        ("GriptapeNodes", "http://localhost:1/mcp/"),
        ("docs", "http://d/mcp"),
    ]


@pytest.mark.asyncio
async def test_lost_session_is_replaced_by_a_new_one(tmp_path: Path, storage: LocalThreadStorageDriver) -> None:
    """Catch up a new session when resuming fails."""
    lost = {"records": [{"t": "end", "error": "No conversation found"}], "exit": 1}
    harness = _harness(tmp_path, storage, lost, _ok_run(session_id="s-new"))
    thread_id, _ = storage.create_thread()
    storage.save_history(
        thread_id,
        [ModelRequest(parts=[UserPromptPart(content="earlier")]), ModelResponse(parts=[TextPart(content="ok")])],
    )
    storage.set_harness_session(thread_id, "fake", HarnessSession(session_id="s-gone", message_count=2))

    result = await harness.run_turn(AgentTurn(thread_id=thread_id), UserMessage("again"))

    retry = harness.invocations()[1]
    assert "--resume" not in retry["argv"]
    assert "User: earlier" in retry["stdin"]
    assert result.output == "Done."
    assert storage.get_harness_session(thread_id, "fake") == HarnessSession(session_id="s-new", message_count=4)


@pytest.mark.asyncio
async def test_cli_error_is_raised_and_nothing_is_saved(tmp_path: Path, storage: LocalThreadStorageDriver) -> None:
    """Leave history unchanged after CLI errors."""
    harness = _harness(tmp_path, storage, {"records": [{"t": "end", "error": "Not signed in"}], "exit": 1})
    thread_id, _ = storage.create_thread()

    with pytest.raises(CliHarnessError, match="Fake CLI stopped with an error: Not signed in"):
        await harness.run_turn(AgentTurn(thread_id=thread_id), UserMessage("hi"))

    assert storage.load_history(thread_id) == []


@pytest.mark.asyncio
async def test_crash_reports_exit_code_and_stderr(tmp_path: Path, storage: LocalThreadStorageDriver) -> None:
    """Include exit code and stderr in crash errors."""
    harness = _harness(tmp_path, storage, {"records": [], "stderr": "boom: bad flag", "exit": 2})
    thread_id, _ = storage.create_thread()

    with pytest.raises(CliHarnessError, match=r"exited with code 2 .*boom: bad flag"):
        await harness.run_turn(AgentTurn(thread_id=thread_id), UserMessage("hi"))


@pytest.mark.asyncio
async def test_missing_cli_names_the_command_and_how_to_install_it(
    tmp_path: Path, storage: LocalThreadStorageDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Include the missing command and installation hint."""
    harness = _harness(tmp_path, storage, _ok_run())
    harness.command = None
    monkeypatch.setenv("PATH", str(tmp_path))
    thread_id, _ = storage.create_thread()

    with pytest.raises(
        CliHarnessError, match=r"`fake-cli-that-does-not-exist` command was not found.*Install the fake"
    ):
        await harness.run_turn(AgentTurn(thread_id=thread_id), UserMessage("hi"))


@pytest.mark.asyncio
async def test_missing_workspace_is_not_reported_as_a_missing_cli(
    tmp_path: Path, storage: LocalThreadStorageDriver
) -> None:
    """Name the missing workspace folder."""
    harness = _harness(tmp_path, storage, _ok_run())
    harness.workspace_root = tmp_path / "gone"
    thread_id, _ = storage.create_thread()

    with pytest.raises(CliHarnessError, match=r"workspace '.*gone'.*folder does not exist"):
        await harness.run_turn(AgentTurn(thread_id=thread_id), UserMessage("hi"))


@pytest.mark.asyncio
async def test_cancel_stops_the_cli_and_saves_nothing(tmp_path: Path, storage: LocalThreadStorageDriver) -> None:
    """Keep partial output, but not the cancelled turn's history."""
    slow = {"records": [{"t": "session", "id": "s-1"}, {"t": "text", "d": "Partial"}, {"sleep": 30}, {"t": "end"}]}
    harness = _harness(tmp_path, storage, slow)
    thread_id, _ = storage.create_thread()
    cancel_event = asyncio.Event()

    def cancel_on_text(event: RunEvent) -> None:
        if isinstance(event, TextDelta):
            cancel_event.set()

    result = await asyncio.wait_for(
        harness.run_turn(
            AgentTurn(thread_id=thread_id, event_sink=cancel_on_text, cancel_event=cancel_event), UserMessage("hi")
        ),
        timeout=10,
    )

    assert result.cancelled
    assert result.output == "Partial"
    assert storage.load_history(thread_id) == []
    assert storage.get_harness_session(thread_id, "fake") == HarnessSession(session_id="s-1", message_count=0)


def _paused_history() -> list[ModelMessage]:
    return [
        ModelRequest(parts=[UserPromptPart(content="Run it")]),
        ModelResponse(parts=[ToolCallPart(tool_name="GriptapeNodes_StartFlowRequest", args="{}", tool_call_id="p1")]),
    ]


@pytest.mark.asyncio
async def test_a_new_message_declines_calls_left_waiting_by_another_provider(
    tmp_path: Path, storage: LocalThreadStorageDriver
) -> None:
    """Persist declined calls before the new message."""
    harness = _harness(tmp_path, storage, _ok_run())
    thread_id, _ = storage.create_thread()
    storage.save_history(thread_id, _paused_history())

    await harness.run_turn(AgentTurn(thread_id=thread_id), UserMessage("Never mind"))

    history = storage.load_history(thread_id)
    opener = history[2]
    assert isinstance(opener, ModelRequest)
    denied, prompt = opener.parts
    assert isinstance(denied, ToolReturnPart)
    assert (denied.tool_call_id, denied.outcome, denied.content) == ("p1", "denied", TOOL_DENIED_BY_NEW_MESSAGE)
    assert isinstance(prompt, UserPromptPart)
    assert pending_tool_calls(history) == []


def test_a_cli_harness_does_not_resume_paused_turns(tmp_path: Path, storage: LocalThreadStorageDriver) -> None:
    """A CLI runs its tools without asking, so the engine never asks it to resume."""
    harness = _harness(tmp_path, storage)

    assert not isinstance(harness, ToolApprovalHarness)


@pytest.mark.asyncio
async def test_images_are_written_for_the_cli_and_saved_as_urls(
    tmp_path: Path, storage: LocalThreadStorageDriver
) -> None:
    """Send image bytes and files; persist URL references."""
    harness = _harness(tmp_path, storage, _ok_run())
    thread_id, _ = storage.create_thread()
    image = BinaryContent(data=b"\x89PNG", media_type="image/png")

    await harness.run_turn(
        AgentTurn(thread_id=thread_id),
        UserMessage(
            content=["Describe", image],
            persist_content=["Describe", ImageUrl(url="http://localhost/cat.png")],
        ),
    )

    (attached,) = harness.contexts[0].images
    assert (attached.data, attached.media_type, attached.path.suffix) == (b"\x89PNG", "image/png", ".png")
    assert harness.contexts[0].prompt_text == "Describe"
    opener = storage.load_history(thread_id)[0]
    assert isinstance(opener, ModelRequest)
    assert isinstance(opener.parts[0], UserPromptPart)
    assert opener.parts[0].content == ["Describe", ImageUrl(url="http://localhost/cat.png")]


def test_mcp_tool_names_are_normalized_to_the_engine_form(tmp_path: Path, storage: LocalThreadStorageDriver) -> None:
    """Normalize MCP names without changing built-in tool names."""
    harness = _harness(tmp_path, storage)

    assert harness.normalize_tool_name("mcp__GriptapeNodes__CreateNodeRequest") == "GriptapeNodes_CreateNodeRequest"
    assert harness.normalize_tool_name("Bash") == "Bash"
    assert harness.mcp_tool_name("GriptapeNodes", "CreateNodeRequest") == "mcp__GriptapeNodes__CreateNodeRequest"
