from __future__ import annotations

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from griptape_nodes.agents.thread_history import pending_tool_calls


def _call(tool_call_id: str, tool_name: str = "GriptapeNodes_StartFlowRequest") -> ToolCallPart:
    return ToolCallPart(tool_name=tool_name, args={}, tool_call_id=tool_call_id)


def _return(tool_call_id: str) -> ToolReturnPart:
    return ToolReturnPart(
        tool_name="GriptapeNodes_ListFlowsInCurrentContextRequest", content="ok", tool_call_id=tool_call_id
    )


class TestPendingToolCalls:
    def test_a_completed_turn_has_nothing_pending(self) -> None:
        history: list[ModelMessage] = [
            ModelRequest(parts=[UserPromptPart(content="hi")]),
            ModelResponse(parts=[TextPart(content="hello")]),
        ]

        assert pending_tool_calls(history) == []

    def test_a_response_ending_in_calls_is_waiting_on_all_of_them(self) -> None:
        history: list[ModelMessage] = [
            ModelRequest(parts=[UserPromptPart(content="run it")]),
            ModelResponse(parts=[_call("a"), _call("b")]),
        ]

        assert [call.tool_call_id for call in pending_tool_calls(history)] == ["a", "b"]

    def test_calls_that_already_ran_are_not_pending(self) -> None:
        history: list[ModelMessage] = [
            ModelRequest(parts=[UserPromptPart(content="run it")]),
            ModelResponse(parts=[_call("list"), _call("start")]),
            ModelRequest(parts=[_return("list")]),
        ]

        assert [call.tool_call_id for call in pending_tool_calls(history)] == ["start"]

    def test_a_trailing_user_prompt_is_not_a_paused_turn(self) -> None:
        history: list[ModelMessage] = [
            ModelResponse(parts=[_call("a")]),
            ModelRequest(parts=[UserPromptPart(content="orphan")]),
        ]

        assert pending_tool_calls(history) == []

    def test_empty_history_has_nothing_pending(self) -> None:
        assert pending_tool_calls([]) == []
