"""Tests for pausing chat-agent tool calls until the user approves them.

Covers which calls are gated and with which grant keys, how a paused turn is
found in saved history, and the runner's pause and resume round trip against a
`FunctionModel`, so no MCP server or Griptape Cloud call is involved.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from pydantic_ai import Agent, Tool
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.tools import DeferredToolResults, ToolDenied
from pydantic_ai.toolsets import FunctionToolset

from griptape_nodes.agents.harness import GTN_SERVER_NAME, PendingApproval, RunEvent, ToolResult
from griptape_nodes.agents.pydantic_ai.mcp_servers import (
    GTN_APPROVAL_REQUIRED_REQUESTS,
    GTN_BATCH_TOOL_NAME,
    _compose,
    _gtn_tool_requires_approval,
    _user_tool_requires_approval,
    grant_keys_for_call,
)
from griptape_nodes.agents.pydantic_ai.runner import PydanticAgentRunner
from griptape_nodes.agents.pydantic_ai.tool_approval import (
    TOOL_STOPPED_WHILE_RUNNING_MESSAGE,
    ToolApprovalDeps,
    needs_approval,
)
from griptape_nodes.agents.thread_history import TOOL_DENIED_BY_NEW_MESSAGE, pending_tool_calls
from griptape_nodes.drivers.thread_storage.local_thread_storage_driver import LocalThreadStorageDriver
from griptape_nodes.servers.mcp import EVENT_REQUEST_BATCH_TOOL_NAME, SUPPORTED_REQUEST_EVENTS

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

    from pydantic_ai import RunContext
    from pydantic_ai.tools import ToolDefinition


ON = ToolApprovalDeps()


def _ctx(deps: ToolApprovalDeps) -> RunContext[Any]:
    return cast("RunContext[Any]", SimpleNamespace(deps=deps))


def _tool(name: str) -> ToolDefinition:
    return cast("ToolDefinition", SimpleNamespace(name=name))


def _batch(*request_types: str) -> dict[str, Any]:
    return {"requests": [{"request_type": request_type, "request": {}} for request_type in request_types]}


class TestGatedSetMatchesTheEngineServer:
    """The agent-side gate names engine tools by string; these hold it to the server's names."""

    def test_every_gated_request_is_a_tool_the_server_offers(self) -> None:
        assert set(GTN_APPROVAL_REQUIRED_REQUESTS) <= set(SUPPORTED_REQUEST_EVENTS)

    def test_batch_tool_name_matches_the_server(self) -> None:
        assert f"{GTN_SERVER_NAME}_{EVENT_REQUEST_BATCH_TOOL_NAME}" == GTN_BATCH_TOOL_NAME


class TestGrantKeysForCall:
    def test_a_single_call_is_keyed_by_its_own_name(self) -> None:
        assert grant_keys_for_call("GriptapeNodes_StartFlowRequest", {"flow_name": "f"}) == [
            "GriptapeNodes_StartFlowRequest"
        ]

    def test_image_and_user_tools_are_keyed_by_their_own_name(self) -> None:
        assert grant_keys_for_call("generate_image", {"prompt": "a cat"}) == ["generate_image"]
        assert grant_keys_for_call("brave_search", {"q": "x"}) == ["brave_search"]

    def test_a_batch_is_keyed_by_its_gated_inner_requests_in_order_without_repeats(self) -> None:
        args = _batch(
            "CreateNodeRequest",
            "ResolveNodeRequest",
            "SetParameterValueRequest",
            "StartFlowRequest",
            "ResolveNodeRequest",
        )

        assert grant_keys_for_call(GTN_BATCH_TOOL_NAME, args) == [
            "GriptapeNodes_ResolveNodeRequest",
            "GriptapeNodes_StartFlowRequest",
        ]

    def test_a_batch_of_edits_has_nothing_to_grant(self) -> None:
        assert grant_keys_for_call(GTN_BATCH_TOOL_NAME, _batch("CreateNodeRequest", "CreateConnectionRequest")) == []

    @pytest.mark.parametrize("args", [{}, {"requests": "nope"}, {"requests": ["nope", {"request": {}}]}])
    def test_a_malformed_batch_has_nothing_to_grant(self, args: dict[str, Any]) -> None:
        assert grant_keys_for_call(GTN_BATCH_TOOL_NAME, args) == []


class TestNeedsApproval:
    def test_asks_until_every_key_is_granted(self) -> None:
        partly = ToolApprovalDeps(granted=frozenset({"GriptapeNodes_ResolveNodeRequest"}))
        fully = ToolApprovalDeps(
            granted=frozenset({"GriptapeNodes_ResolveNodeRequest", "GriptapeNodes_StartFlowRequest"}),
        )
        keys = ["GriptapeNodes_ResolveNodeRequest", "GriptapeNodes_StartFlowRequest"]

        assert needs_approval(ON, keys) is True
        assert needs_approval(partly, keys) is True
        assert needs_approval(fully, keys) is False

    def test_nothing_to_grant_never_asks(self) -> None:
        assert needs_approval(ON, []) is False

    def test_a_run_without_approval_state_fails_instead_of_running_unasked(self) -> None:
        with pytest.raises(TypeError, match="no approval state"):
            needs_approval(None, ["generate_image"])


class TestGtnToolRequiresApproval:
    @pytest.mark.parametrize("request_type", sorted(GTN_APPROVAL_REQUIRED_REQUESTS))
    def test_tools_that_run_work_ask(self, request_type: str) -> None:
        assert _gtn_tool_requires_approval(_ctx(ON), _tool(f"GriptapeNodes_{request_type}"), {}) is True

    @pytest.mark.parametrize(
        "request_type", ["CreateNodeRequest", "SaveWorkflowRequest", "ListFlowsInCurrentContextRequest"]
    )
    def test_tools_that_only_edit_or_read_never_ask(self, request_type: str) -> None:
        assert _gtn_tool_requires_approval(_ctx(ON), _tool(f"GriptapeNodes_{request_type}"), {}) is False

    def test_a_granted_tool_runs_without_asking(self) -> None:
        deps = ToolApprovalDeps(granted=frozenset({"GriptapeNodes_StartFlowRequest"}))

        assert _gtn_tool_requires_approval(_ctx(deps), _tool("GriptapeNodes_StartFlowRequest"), {}) is False

    def test_a_batch_asks_only_when_it_holds_an_ungranted_gated_request(self) -> None:
        mixed = _batch("CreateNodeRequest", "StartFlowRequest")
        granted = ToolApprovalDeps(granted=frozenset({"GriptapeNodes_StartFlowRequest"}))
        batch = _tool(GTN_BATCH_TOOL_NAME)

        assert _gtn_tool_requires_approval(_ctx(ON), batch, _batch("CreateNodeRequest")) is False
        assert _gtn_tool_requires_approval(_ctx(ON), batch, mixed) is True
        assert _gtn_tool_requires_approval(_ctx(granted), batch, mixed) is False


class TestUserToolRequiresApproval:
    def test_every_tool_asks(self) -> None:
        assert _user_tool_requires_approval(_ctx(ON), _tool("brave_search"), {}) is True

    def test_a_granted_tool_runs_without_asking(self) -> None:
        deps = ToolApprovalDeps(granted=frozenset({"brave_search"}))

        assert _user_tool_requires_approval(_ctx(deps), _tool("brave_search"), {}) is False


class ScriptedModel:
    """Streams one scripted turn per model request: text, or a list of tool calls."""

    def __init__(self, *turns: str | list[tuple[str, dict[str, Any], str]]) -> None:
        self._turns = list(turns)
        self.requests: list[list[ModelMessage]] = []
        self.infos: list[AgentInfo] = []

    async def stream(self, messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[Any]:
        self.requests.append(list(messages))
        self.infos.append(info)
        turn = self._turns.pop(0)
        if isinstance(turn, str):
            yield turn
            return
        yield {
            index: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=tool_call_id)
            for index, (name, args, tool_call_id) in enumerate(turn)
        }


@dataclass
class ToolGate:
    """Holds a run at a chosen point so a test can cancel it there.

    Tools named in ``blocked`` stop once they start, standing in for an engine flow
    that is still running. ``stall_before_tools`` stops the run before it reaches
    any tool call. ``reached`` is set when the run is held; nothing ever releases it.
    """

    blocked: frozenset[str] = frozenset()
    stall_before_tools: bool = False
    reached: asyncio.Event = field(default_factory=asyncio.Event)

    async def hold(self) -> None:
        self.reached.set()
        await asyncio.Event().wait()


class _StallingToolset(FunctionToolset[Any]):
    """No tools of its own; holds the run while tools are gathered, when the gate says to."""

    def __init__(self, gate: ToolGate) -> None:
        super().__init__()
        self._gate = gate

    async def get_tools(self, ctx: Any) -> dict[str, Any]:
        if self._gate.stall_before_tools:
            await self._gate.hold()
        return await super().get_tools(ctx)


def gtn_like_toolset(ran: list[str], gate: ToolGate | None = None) -> Any:
    """Stand in for the engine's MCP server, composed and gated exactly as the real one is."""

    def recorder(name: str) -> Callable[..., Awaitable[str]]:
        async def run(**_kwargs: Any) -> str:
            ran.append(name)
            if gate is not None and name in gate.blocked:
                await gate.hold()
            return f"{name} ok"

        return run

    tools = [
        Tool(recorder(name), name=name, takes_ctx=False)
        for name in ("StartFlowRequest", "ListFlowsInCurrentContextRequest", "EventRequestBatch")
    ]
    return _compose(GTN_SERVER_NAME, FunctionToolset(tools=tools), requires_approval=_gtn_tool_requires_approval)


def runner_with(
    tmp_path: Path, model: ScriptedModel, ran: list[str], gate: ToolGate | None = None
) -> PydanticAgentRunner:
    """A runner on real thread storage whose agent talks to `model` and has the gated toolset."""
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    storage = LocalThreadStorageDriver(tmp_path / "threads", config_manager=None, secrets_manager=None)  # type: ignore[arg-type]
    runner = PydanticAgentRunner(model_name="test", api_key="dummy", workspace_root=workspace, storage=storage)
    toolsets: list[Any] = [gtn_like_toolset(ran, gate)]
    if gate is not None:
        toolsets.append(_StallingToolset(gate))
    runner._agent = Agent(FunctionModel(stream_function=model.stream), toolsets=toolsets)
    return runner


class TestRunnerPauseAndResume:
    @pytest.mark.asyncio
    async def test_a_run_without_approval_state_still_gates(self, tmp_path: Path) -> None:
        """Leaving `approval` out means no grants, never an ungated run."""
        ran: list[str] = []
        model = ScriptedModel([("GriptapeNodes_StartFlowRequest", {}, "c1")])
        runner = runner_with(tmp_path, model, ran)

        result = await runner.run("Run the flow.")

        assert ran == []
        assert [pending.tool_call_id for pending in result.pending_approvals] == ["c1"]

    @pytest.mark.asyncio
    async def test_a_granted_call_runs_without_pausing(self, tmp_path: Path) -> None:
        ran: list[str] = []
        model = ScriptedModel([("GriptapeNodes_StartFlowRequest", {}, "c1")], "Done.")
        runner = runner_with(tmp_path, model, ran)

        result = await runner.run(
            "Run the flow.", approval=ToolApprovalDeps(granted=frozenset({"GriptapeNodes_StartFlowRequest"}))
        )

        assert ran == ["StartFlowRequest"]
        assert result.pending_approvals == []
        assert result.output == "Done."

    @pytest.mark.asyncio
    async def test_a_gated_call_pauses_the_turn_without_running(self, tmp_path: Path) -> None:
        ran: list[str] = []
        model = ScriptedModel(
            [
                ("GriptapeNodes_ListFlowsInCurrentContextRequest", {}, "c0"),
                ("GriptapeNodes_StartFlowRequest", {"flow_name": "main"}, "c1"),
            ]
        )
        runner = runner_with(tmp_path, model, ran)

        result = await runner.run("Run the flow.", approval=ON)

        assert ran == ["ListFlowsInCurrentContextRequest"]
        assert result.pending_approvals == [
            PendingApproval(
                tool_call_id="c1",
                tool_name="GriptapeNodes_StartFlowRequest",
                args='{"flow_name": "main"}',
                grant_keys=["GriptapeNodes_StartFlowRequest"],
            )
        ]
        # The paused turn is saved, and what it is waiting on can be read back from it.
        history = runner.storage.load_history(result.thread_id)
        assert [call.tool_call_id for call in pending_tool_calls(history)] == ["c1"]

    @pytest.mark.asyncio
    async def test_resuming_with_an_approval_runs_the_call_and_finishes_the_turn(self, tmp_path: Path) -> None:
        ran: list[str] = []
        model = ScriptedModel([("GriptapeNodes_StartFlowRequest", {}, "c1")], "Flow started.")
        runner = runner_with(tmp_path, model, ran)
        paused = await runner.run("Run the flow.", approval=ON)

        resumed = await runner.run(
            None,
            thread_id=paused.thread_id,
            approval=ON,
            deferred_tool_results=DeferredToolResults(approvals={"c1": True}),
        )

        assert ran == ["StartFlowRequest"]
        assert resumed.pending_approvals == []
        assert resumed.output == "Flow started."
        assert pending_tool_calls(runner.storage.load_history(paused.thread_id)) == []

    @pytest.mark.asyncio
    async def test_a_prompt_can_ride_along_with_the_answers(self, tmp_path: Path) -> None:
        """The answers and the new prompt reach the model in one request, answers first."""
        ran: list[str] = []
        model = ScriptedModel([("GriptapeNodes_StartFlowRequest", {}, "c1")], "Okay, I won't.")
        runner = runner_with(tmp_path, model, ran)
        paused = await runner.run("Run the flow.", approval=ON)

        await runner.run(
            "Actually, just explain it.",
            thread_id=paused.thread_id,
            approval=ON,
            deferred_tool_results=DeferredToolResults(approvals={"c1": ToolDenied("skipped")}),
        )

        last_request = model.requests[-1][-1]
        assert isinstance(last_request, ModelRequest)
        kinds = [type(part) for part in last_request.parts]
        assert kinds == [ToolReturnPart, UserPromptPart]
        assert ran == []

    @pytest.mark.asyncio
    async def test_a_prompt_alone_declines_what_the_thread_was_waiting_on(self, tmp_path: Path) -> None:
        ran: list[str] = []
        model = ScriptedModel([("GriptapeNodes_StartFlowRequest", {}, "c1")], "Okay.")
        runner = runner_with(tmp_path, model, ran)
        paused = await runner.run("Run the flow.", approval=ON)

        await runner.run("Something else.", thread_id=paused.thread_id, approval=ON)

        history = runner.storage.load_history(paused.thread_id)
        assert ran == []
        assert _results_by_call(history) == {"c1": TOOL_DENIED_BY_NEW_MESSAGE}
        assert pending_tool_calls(history) == []

    @pytest.mark.asyncio
    async def test_running_with_neither_a_prompt_nor_answers_fails(self, tmp_path: Path) -> None:
        runner = runner_with(tmp_path, ScriptedModel("Hi."), [])

        with pytest.raises(ValueError, match="no message to send"):
            await runner.run(None, approval=ON)


def _results_by_call(messages: list[ModelMessage]) -> dict[str, str]:
    return {
        part.tool_call_id: str(part.content)
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    }


async def _cancel_once(run: Awaitable[Any], cancel_event: asyncio.Event, reached: asyncio.Event) -> Any:
    """Run `run`, cancel it as soon as `reached` is set, and return its result."""
    task = asyncio.ensure_future(run)
    await asyncio.wait_for(reached.wait(), timeout=5)
    cancel_event.set()
    return await asyncio.wait_for(task, timeout=5)


class TestCancellingAResumedTurn:
    """A cancelled resume saves what it did, so an approved call can never run twice."""

    @pytest.mark.asyncio
    async def test_a_call_cancelled_while_running_is_no_longer_pending(self, tmp_path: Path) -> None:
        ran: list[str] = []
        gate = ToolGate(blocked=frozenset({"StartFlowRequest"}))
        runner = runner_with(tmp_path, ScriptedModel([("GriptapeNodes_StartFlowRequest", {}, "c1")]), ran, gate)
        paused = await runner.run("Run the flow.", approval=ON)
        events: list[RunEvent] = []
        cancel_event = asyncio.Event()

        cancelled = await _cancel_once(
            runner.run(
                None,
                thread_id=paused.thread_id,
                approval=ON,
                deferred_tool_results=DeferredToolResults(approvals={"c1": True}),
                cancel_event=cancel_event,
                event_sink=events.append,
            ),
            cancel_event,
            gate.reached,
        )

        history = runner.storage.load_history(paused.thread_id)
        assert cancelled.cancelled is True
        assert cancelled.pending_approvals == []
        assert ran == ["StartFlowRequest"]
        assert pending_tool_calls(history) == []
        assert _results_by_call(history) == {"c1": TOOL_STOPPED_WHILE_RUNNING_MESSAGE}
        # The editor is told the call is over, so its row stops reading as running.
        assert any(
            isinstance(event, ToolResult) and event.tool_call_id == "c1" and "stopped the reply" in event.content
            for event in events
        )

    @pytest.mark.asyncio
    async def test_a_call_that_finished_keeps_its_result(self, tmp_path: Path) -> None:
        ran: list[str] = []
        gate = ToolGate(blocked=frozenset({"StartFlowRequest"}))
        batch_args = {"requests": [{"request_type": "ResolveNodeRequest", "request": {}}]}
        model = ScriptedModel(
            [("GriptapeNodes_StartFlowRequest", {}, "c1"), ("GriptapeNodes_EventRequestBatch", batch_args, "c2")]
        )
        runner = runner_with(tmp_path, model, ran, gate)
        paused = await runner.run("Run both.", approval=ON)
        batch_done = asyncio.Event()
        cancel_event = asyncio.Event()

        def on_event(event: RunEvent) -> None:
            if isinstance(event, ToolResult) and event.tool_call_id == "c2":
                batch_done.set()

        await _cancel_once(
            runner.run(
                None,
                thread_id=paused.thread_id,
                approval=ON,
                deferred_tool_results=DeferredToolResults(approvals={"c1": True, "c2": True}),
                cancel_event=cancel_event,
                event_sink=on_event,
            ),
            cancel_event,
            batch_done,
        )

        results = _results_by_call(runner.storage.load_history(paused.thread_id))
        assert results == {"c1": TOOL_STOPPED_WHILE_RUNNING_MESSAGE, "c2": "EventRequestBatch ok"}

    @pytest.mark.asyncio
    async def test_an_approved_call_that_never_started_stays_pending_and_can_be_answered(self, tmp_path: Path) -> None:
        ran: list[str] = []
        gate = ToolGate()
        model = ScriptedModel(
            [
                ("GriptapeNodes_StartFlowRequest", {}, "c1"),
                ("GriptapeNodes_StartFlowRequest", {"flow_name": "b"}, "c2"),
            ],
            "Started.",
        )
        runner = runner_with(tmp_path, model, ran, gate)
        paused = await runner.run("Run it.", approval=ON)
        gate.stall_before_tools = True
        cancel_event = asyncio.Event()

        cancelled = await _cancel_once(
            runner.run(
                None,
                thread_id=paused.thread_id,
                approval=ON,
                deferred_tool_results=DeferredToolResults(approvals={"c1": True, "c2": ToolDenied("no")}),
                cancel_event=cancel_event,
            ),
            cancel_event,
            gate.reached,
        )

        history = runner.storage.load_history(paused.thread_id)
        assert ran == []
        assert [pending.tool_call_id for pending in cancelled.pending_approvals] == ["c1"]
        assert [call.tool_call_id for call in pending_tool_calls(history)] == ["c1"]
        assert _results_by_call(history) == {"c2": "no"}

        # The saved mix of answered and waiting calls resumes cleanly.
        gate.stall_before_tools = False
        resumed = await runner.run(
            None,
            thread_id=paused.thread_id,
            approval=ON,
            deferred_tool_results=DeferredToolResults(approvals={"c1": True}),
        )
        assert ran == ["StartFlowRequest"]
        assert resumed.output == "Started."
        assert pending_tool_calls(runner.storage.load_history(paused.thread_id)) == []
