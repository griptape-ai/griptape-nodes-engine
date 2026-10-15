"""Tests for the approval card round trip through `AgentManager`.

A real `PydanticAgentRunner` on real thread storage talks to a scripted
`FunctionModel`, so pausing, resuming, grants, and denials are checked against
the history the editor would reload, while the engine around the manager is a
stand-in.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from pydantic_ai.messages import ModelRequest, ToolReturnPart, UserPromptPart

from griptape_nodes.agents.pydantic_ai.mcp_toolset_cache import MCPToolsetCache
from griptape_nodes.agents.pydantic_ai.tool_approval import (
    TOOL_DENIED_BY_USER_MESSAGE,
    TOOL_STOPPED_WHILE_RUNNING_MESSAGE,
)
from griptape_nodes.agents.thread_history import TOOL_DENIED_BY_NEW_MESSAGE
from griptape_nodes.drivers.thread_storage.local_thread_storage_driver import LocalThreadStorageDriver
from griptape_nodes.retained_mode.events.agent_events import (
    CancelAgentRequest,
    CancelAgentResultSuccess,
    ClearToolGrantsRequest,
    ClearToolGrantsResultFailure,
    ClearToolGrantsResultSuccess,
    ResolveToolApprovalsRequest,
    RunAgentRequest,
    RunAgentResultFailure,
    RunAgentResultSuccess,
    ToolApprovalDecision,
)
from griptape_nodes.retained_mode.events.budget_events import GetAttributionContextResultFailure
from griptape_nodes.retained_mode.events.mcp_events import GetEnabledMCPServersResultSuccess
from griptape_nodes.retained_mode.managers.agent_manager import AgentManager, ComposedPrompt
from tests.unit.agents.pydantic_ai.test_tool_approval import ScriptedModel, ToolGate, runner_with

if TYPE_CHECKING:
    from pathlib import Path

    from griptape_nodes.agents.pydantic_ai.runner import PydanticAgentRunner
    from griptape_nodes.retained_mode.events.agent_events import RunAgentRequestArtifact

_AGENT_MANAGER_MODULE = "griptape_nodes.retained_mode.managers.agent_manager"

START = "GriptapeNodes_StartFlowRequest"
LIST = "GriptapeNodes_ListFlowsInCurrentContextRequest"
BATCH = "GriptapeNodes_EventRequestBatch"


async def _plain_prompt(text: str, _url_artifacts: list[RunAgentRequestArtifact]) -> ComposedPrompt:
    return ComposedPrompt(live=text, persist=text)


async def _no_attribution(_request: object) -> GetAttributionContextResultFailure:
    return GetAttributionContextResultFailure(result_details="no project")


class _Harness:
    """An `AgentManager` wired to one runner on real thread storage."""

    def __init__(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: ScriptedModel, gate: ToolGate | None = None
    ) -> None:
        self.ran: list[str] = []
        self.model = model
        self.runner: PydanticAgentRunner = runner_with(tmp_path, model, self.ran, gate)
        self.storage: LocalThreadStorageDriver = self.runner.storage  # type: ignore[assignment]
        self.manager = _manager_on(self.storage, self.runner, monkeypatch)

    def thread(self) -> str:
        thread_id, _ = self.storage.create_thread()
        return thread_id

    async def send(self, thread_id: str, text: str) -> RunAgentResultSuccess:
        result = await self.manager.on_handle_run_agent_request(
            RunAgentRequest(input=text, url_artifacts=[], thread_id=thread_id)
        )
        assert isinstance(result, RunAgentResultSuccess), result
        return result

    async def resolve(self, thread_id: str, **decisions: str) -> Any:
        return await self.manager.on_handle_resolve_tool_approvals_request(
            ResolveToolApprovalsRequest(
                thread_id=thread_id,
                decisions=[ToolApprovalDecision(tool_call_id=key, decision=value) for key, value in decisions.items()],
            )
        )

    def tool_returns(self, thread_id: str) -> dict[str, str]:
        return {
            part.tool_call_id: str(part.content)
            for message in self.storage.load_history(thread_id)
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        }


def _manager_on(
    storage: LocalThreadStorageDriver,
    runner: PydanticAgentRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> AgentManager:
    manager = AgentManager.__new__(AgentManager)
    manager._active_runs = {}
    manager._mcp_toolsets = MCPToolsetCache()
    manager._thread_storage = storage
    manager._providers = []
    manager._active_provider_name = "griptape_cloud"
    monkeypatch.setattr(manager, "_build_runner", lambda *_a, **_k: runner)
    monkeypatch.setattr(_AGENT_MANAGER_MODULE + "._compose_prompt", _plain_prompt)
    manager._engine = SimpleNamespace(  # type: ignore[assignment]
        event_manager=SimpleNamespace(put_event=lambda _e: None),
        handle_request=lambda _r: GetEnabledMCPServersResultSuccess(servers={}, result_details="none"),
        ahandle_request=_no_attribution,
    )
    return manager


@pytest.mark.asyncio
class TestPausing:
    async def test_a_gated_call_pauses_with_a_card_to_answer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Harness(tmp_path, monkeypatch, ScriptedModel([(START, {"flow_name": "main"}, "c1")]))

        result = await harness.send(harness.thread(), "Run the flow.")

        assert harness.ran == []
        assert result.output["pending_approvals"] == [
            {"tool_call_id": "c1", "tool_name": START, "args": '{"flow_name": "main"}', "grant_keys": [START]}
        ]
        assert result.output["cancelled"] is False
        assert result.output["truncated"] is False

    async def test_the_agent_is_told_to_answer_questions_in_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Harness(tmp_path, monkeypatch, ScriptedModel("It wires nodes."))

        await harness.send(harness.thread(), "How does this work?")

        instructions = harness.model.requests[0][0].instructions or ""  # type: ignore[union-attr]
        assert "need the user's approval" in instructions
        assert "answer in text" in instructions
        assert "If the user later asks for it again, call it again" in instructions


@pytest.mark.asyncio
class TestResolving:
    async def test_approve_once_runs_it_and_remembers_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Harness(tmp_path, monkeypatch, ScriptedModel([(START, {}, "c1")], "Started.", [(START, {}, "c2")]))
        thread_id = harness.thread()
        await harness.send(thread_id, "Run the flow.")

        result = await harness.resolve(thread_id, c1="approve_once")

        assert isinstance(result, RunAgentResultSuccess)
        assert result.output["text"] == "Started."
        assert result.output["pending_approvals"] == []
        assert harness.ran == ["StartFlowRequest"]
        assert harness.storage.get_thread_metadata(thread_id).tool_grants == []
        # The next time, it asks again.
        again = await harness.send(thread_id, "Run it again.")
        assert [card["tool_call_id"] for card in again.output["pending_approvals"]] == ["c2"]

    async def test_approve_for_conversation_stops_asking_in_this_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Harness(
            tmp_path,
            monkeypatch,
            ScriptedModel([(START, {}, "c1")], "Started.", [(START, {}, "c2")], "Started again."),
        )
        thread_id = harness.thread()
        await harness.send(thread_id, "Run the flow.")

        await harness.resolve(thread_id, c1="approve_for_conversation")
        again = await harness.send(thread_id, "Run it again.")

        assert harness.storage.get_thread_metadata(thread_id).tool_grants == [START]
        assert again.output["pending_approvals"] == []
        assert harness.ran == ["StartFlowRequest", "StartFlowRequest"]

    async def test_deny_tells_the_agent_not_to_retry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        harness = _Harness(tmp_path, monkeypatch, ScriptedModel([(START, {}, "c1")], "Understood."))
        thread_id = harness.thread()
        await harness.send(thread_id, "Run the flow.")

        result = await harness.resolve(thread_id, c1="deny")

        assert isinstance(result, RunAgentResultSuccess)
        assert harness.ran == []
        assert harness.tool_returns(thread_id)["c1"] == TOOL_DENIED_BY_USER_MESSAGE

    async def test_a_conversation_grant_also_approves_a_covered_sibling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        batch_args = {"requests": [{"request_type": "StartFlowRequest", "request": {}}]}
        model = ScriptedModel([(LIST, {}, "c0"), (START, {}, "c1"), (BATCH, batch_args, "c2")], "All done.")
        harness = _Harness(tmp_path, monkeypatch, model)
        thread_id = harness.thread()
        paused = await harness.send(thread_id, "Run everything.")
        assert [card["tool_call_id"] for card in paused.output["pending_approvals"]] == ["c1", "c2"]

        result = await harness.resolve(thread_id, c1="approve_for_conversation")

        assert isinstance(result, RunAgentResultSuccess)
        assert result.output["text"] == "All done."
        # The read-only call ran once, before the pause, and is not run again. The
        # resumed calls run in parallel, so their order is not fixed.
        assert harness.ran[0] == "ListFlowsInCurrentContextRequest"
        assert sorted(harness.ran[1:]) == ["EventRequestBatch", "StartFlowRequest"]

    async def test_survives_an_engine_restart(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        harness = _Harness(tmp_path, monkeypatch, ScriptedModel([(START, {}, "c1")], "Started."))
        thread_id = harness.thread()
        await harness.send(thread_id, "Run the flow.")

        reopened = LocalThreadStorageDriver(harness.storage.threads_directory, None, None)  # type: ignore[arg-type]
        harness.runner.storage = reopened
        restarted = _manager_on(reopened, harness.runner, monkeypatch)
        result = await restarted.on_handle_resolve_tool_approvals_request(
            ResolveToolApprovalsRequest(
                thread_id=thread_id, decisions=[ToolApprovalDecision(tool_call_id="c1", decision="approve_once")]
            )
        )

        assert isinstance(result, RunAgentResultSuccess)
        assert harness.ran == ["StartFlowRequest"]


@pytest.mark.asyncio
class TestResolvingFailures:
    @staticmethod
    async def _paused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[_Harness, str]:
        model = ScriptedModel([(START, {}, "c1"), (START, {"flow_name": "other"}, "c2")])
        harness = _Harness(tmp_path, monkeypatch, model)
        thread_id = harness.thread()
        await harness.send(thread_id, "Run both.")
        return harness, thread_id

    async def test_a_call_left_undecided_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        harness, thread_id = await self._paused(tmp_path, monkeypatch)

        result = await harness.resolve(thread_id, c1="approve_once")

        assert isinstance(result, RunAgentResultFailure)
        assert "still waiting for you to approve or deny" in result.error["message"]
        assert harness.ran == []

    async def test_an_unknown_call_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        harness, thread_id = await self._paused(tmp_path, monkeypatch)

        result = await harness.resolve(thread_id, c1="approve_once", c2="approve_once", nope="deny")

        assert isinstance(result, RunAgentResultFailure)
        assert "not waiting on that call" in result.error["message"]

    async def test_an_unknown_choice_fails_without_saving_grants(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness, thread_id = await self._paused(tmp_path, monkeypatch)

        result = await harness.resolve(thread_id, c1="approve_for_conversation", c2="maybe")

        assert isinstance(result, RunAgentResultFailure)
        assert "'maybe' is not a choice" in result.error["message"]
        assert harness.storage.get_thread_metadata(thread_id).tool_grants == []

    async def test_a_harness_that_never_asks_refuses_without_saving_grants(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness, thread_id = await self._paused(tmp_path, monkeypatch)
        monkeypatch.setattr(harness.manager, "_build_harness", lambda **_k: SimpleNamespace(run_turn=None))

        result = await harness.resolve(thread_id, c1="approve_for_conversation", c2="approve_once")

        assert isinstance(result, RunAgentResultFailure)
        assert "runs its tools without asking" in result.error["message"]
        assert harness.storage.get_thread_metadata(thread_id).tool_grants == []

    async def test_a_thread_that_is_not_paused_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        harness = _Harness(tmp_path, monkeypatch, ScriptedModel("Hi."))
        thread_id = harness.thread()
        await harness.send(thread_id, "Hello.")

        result = await harness.resolve(thread_id, c1="approve_once")

        assert isinstance(result, RunAgentResultFailure)
        assert "not waiting for approval" in result.error["message"]

    async def test_a_missing_thread_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        harness = _Harness(tmp_path, monkeypatch, ScriptedModel())

        result = await harness.resolve("missing", c1="approve_once")

        assert isinstance(result, RunAgentResultFailure)
        assert "does not exist" in result.error["message"]


@pytest.mark.asyncio
async def test_a_new_message_declines_what_was_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sending a message instead of answering the card declines it, and the agent answers the message."""
    harness = _Harness(tmp_path, monkeypatch, ScriptedModel([(START, {}, "c1")], "Here is how."))
    thread_id = harness.thread()
    await harness.send(thread_id, "Run the flow.")

    result = await harness.send(thread_id, "Wait, how does this work?")

    assert result.output["text"] == "Here is how."
    assert result.output["pending_approvals"] == []
    assert harness.ran == []
    assert harness.tool_returns(thread_id)["c1"] == TOOL_DENIED_BY_NEW_MESSAGE
    # The decline and the new message reach the model together.
    last_request = harness.model.requests[-1][-1]
    assert isinstance(last_request, ModelRequest)
    assert [type(part) for part in last_request.parts] == [ToolReturnPart, UserPromptPart]


class TestClearToolGrants:
    @pytest.mark.asyncio
    async def test_clearing_makes_the_agent_ask_again(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        harness = _Harness(tmp_path, monkeypatch, ScriptedModel([(START, {}, "c1")], "Started.", [(START, {}, "c2")]))
        thread_id = harness.thread()
        await harness.send(thread_id, "Run the flow.")
        await harness.resolve(thread_id, c1="approve_for_conversation")

        cleared = harness.manager.on_handle_clear_tool_grants_request(ClearToolGrantsRequest(thread_id=thread_id))
        again = await harness.send(thread_id, "Run it again.")

        assert isinstance(cleared, ClearToolGrantsResultSuccess)
        assert cleared.thread_id == thread_id
        assert harness.storage.get_thread_metadata(thread_id).tool_grants == []
        assert [card["tool_call_id"] for card in again.output["pending_approvals"]] == ["c2"]

    def test_a_missing_thread_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        harness = _Harness(tmp_path, monkeypatch, ScriptedModel())

        result = harness.manager.on_handle_clear_tool_grants_request(ClearToolGrantsRequest(thread_id="missing"))

        assert isinstance(result, ClearToolGrantsResultFailure)


@pytest.mark.asyncio
class TestOneReplyAtATime:
    """Two requests on one thread must not both resume the same paused calls."""

    async def test_a_second_answer_to_the_same_card_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Harness(tmp_path, monkeypatch, ScriptedModel([(START, {}, "c1")], "Started."))
        thread_id = harness.thread()
        await harness.send(thread_id, "Run the flow.")

        first, second = await asyncio.gather(
            harness.resolve(thread_id, c1="approve_once"),
            harness.resolve(thread_id, c1="approve_once"),
        )

        assert isinstance(first, RunAgentResultSuccess)
        assert isinstance(second, RunAgentResultFailure)
        assert "a reply is already in progress in this conversation" in second.error["message"]
        assert harness.ran == ["StartFlowRequest"]

    async def test_a_message_during_an_answer_is_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        harness = _Harness(tmp_path, monkeypatch, ScriptedModel([(START, {}, "c1")], "Started."))
        thread_id = harness.thread()
        await harness.send(thread_id, "Run the flow.")

        resolved, sent = await asyncio.gather(
            harness.resolve(thread_id, c1="approve_once"),
            harness.manager.on_handle_run_agent_request(
                RunAgentRequest(input="Never mind.", url_artifacts=[], thread_id=thread_id)
            ),
        )

        assert isinstance(resolved, RunAgentResultSuccess)
        assert isinstance(sent, RunAgentResultFailure)
        assert "a reply is already in progress in this conversation" in sent.error["message"]
        assert harness.ran == ["StartFlowRequest"]
        assert TOOL_DENIED_BY_NEW_MESSAGE not in harness.tool_returns(thread_id).values()

    async def test_a_rejected_answer_frees_the_conversation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Harness(tmp_path, monkeypatch, ScriptedModel([(START, {}, "c1")], "Started."))
        thread_id = harness.thread()
        await harness.send(thread_id, "Run the flow.")

        rejected = await harness.resolve(thread_id, c1="maybe")
        accepted = await harness.resolve(thread_id, c1="approve_once")

        assert isinstance(rejected, RunAgentResultFailure)
        assert isinstance(accepted, RunAgentResultSuccess)
        assert harness.manager._active_runs == {}

    async def test_a_failed_run_frees_the_conversation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        harness = _Harness(tmp_path, monkeypatch, ScriptedModel("Hello."))
        thread_id = harness.thread()
        real_run = harness.runner.run
        calls = 0

        async def fail_once(*args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                msg = "the model went away"
                raise RuntimeError(msg)
            return await real_run(*args, **kwargs)

        monkeypatch.setattr(harness.runner, "run", fail_once)

        failed = await harness.manager.on_handle_run_agent_request(
            RunAgentRequest(input="Hi.", url_artifacts=[], thread_id=thread_id)
        )
        retried = await harness.send(thread_id, "Hi again.")

        assert isinstance(failed, RunAgentResultFailure)
        assert retried.output["text"] == "Hello."
        assert harness.manager._active_runs == {}


@pytest.mark.asyncio
async def test_cancelling_an_answer_mid_run_never_runs_the_call_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flow keeps running after Cancel, so the call must not stay answerable or read as never run."""
    gate = ToolGate(blocked=frozenset({"StartFlowRequest"}))
    harness = _Harness(tmp_path, monkeypatch, ScriptedModel([(START, {}, "c1")], "It may still be running."), gate)
    thread_id = harness.thread()
    await harness.send(thread_id, "Run the flow.")

    answering = asyncio.ensure_future(harness.resolve(thread_id, c1="approve_for_conversation"))
    await asyncio.wait_for(gate.reached.wait(), timeout=5)
    cancel = harness.manager.on_handle_cancel_agent_request(CancelAgentRequest(thread_id=thread_id))
    cancelled = await asyncio.wait_for(answering, timeout=5)

    assert isinstance(cancel, CancelAgentResultSuccess)
    assert cancel.was_running is True
    assert isinstance(cancelled, RunAgentResultSuccess)
    assert cancelled.output["cancelled"] is True
    assert cancelled.output["pending_approvals"] == []

    again = await harness.resolve(thread_id, c1="approve_once")
    assert isinstance(again, RunAgentResultFailure)
    assert "not waiting for approval" in again.error["message"]

    follow_up = await harness.send(thread_id, "Did it run?")
    assert follow_up.output["text"] == "It may still be running."
    assert harness.tool_returns(thread_id)["c1"] == TOOL_STOPPED_WHILE_RUNNING_MESSAGE
    assert harness.ran == ["StartFlowRequest"]
