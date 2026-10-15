from __future__ import annotations

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from griptape_nodes.agents.cli.transcript import (
    CATCH_UP_MAX_CHARS,
    UNANSWERED_TOOL_CALL_MESSAGE,
    TranscriptBuilder,
    render_catch_up,
)
from griptape_nodes.agents.harness import TextDelta, ThinkingDelta, ToolCall, ToolResult
from griptape_nodes.agents.thread_history import pending_tool_calls


def _builder() -> TranscriptBuilder:
    return TranscriptBuilder(provider_name="claude_code", model_name="sonnet")


def test_deltas_join_into_parts_and_results_open_a_request() -> None:
    """Join deltas and group tool answers in a request."""
    builder = _builder()
    for event in [
        ThinkingDelta(delta="Let me "),
        ThinkingDelta(delta="look."),
        ToolCall(tool_call_id="a", tool_name="Bash", args='{"command": "ls"}'),
        ToolCall(tool_call_id="b", tool_name="Read", args="{}"),
        ToolResult(tool_call_id="a", tool_name="Bash", content="x"),
        ToolResult(tool_call_id="b", tool_name="Read", content="boom", is_error=True),
        TextDelta(delta="Found "),
        TextDelta(delta="it."),
    ]:
        builder.add(event)

    first, results, final = builder.messages()

    assert isinstance(first, ModelResponse)
    assert first.parts[0] == ThinkingPart(content="Let me look.")
    assert [part.tool_call_id for part in first.parts[1:] if isinstance(part, ToolCallPart)] == ["a", "b"]
    assert (first.model_name, first.provider_name) == ("sonnet", "claude_code")
    assert isinstance(results, ModelRequest)
    assert [(part.tool_call_id, part.outcome) for part in results.parts if isinstance(part, ToolReturnPart)] == [
        ("a", "success"),
        ("b", "failed"),
    ]
    assert isinstance(final, ModelResponse)
    assert final.parts == [TextPart(content="Found it.")]


def test_unanswered_calls_are_closed_so_nothing_looks_pending() -> None:
    """Close unanswered calls as interrupted, not pending approval."""
    builder = _builder()
    builder.add(ToolCall(tool_call_id="a", tool_name="Bash", args="{}"))

    messages = builder.messages()

    history = [ModelRequest(parts=[UserPromptPart(content="go")]), *messages]
    assert pending_tool_calls(history) == []
    results = messages[1]
    assert isinstance(results, ModelRequest)
    (answer,) = results.parts
    assert isinstance(answer, ToolReturnPart)
    assert (answer.content, answer.outcome) == (UNANSWERED_TOOL_CALL_MESSAGE, "interrupted")
    assert isinstance(messages[-1], ModelResponse)


def test_an_empty_turn_still_ends_in_a_response() -> None:
    """End empty turns with a model response."""
    (only,) = _builder().messages()

    assert isinstance(only, ModelResponse)


def test_catch_up_keeps_what_was_said() -> None:
    """Include dialogue and tool names, but not tool results."""
    text = render_catch_up(
        [
            ModelRequest(parts=[UserPromptPart(content="Make a node")]),
            ModelResponse(parts=[ToolCallPart(tool_name="GriptapeNodes_CreateNodeRequest", args="{}")]),
            ModelRequest(parts=[ToolReturnPart(tool_name="GriptapeNodes_CreateNodeRequest", content="secret")]),
            ModelResponse(parts=[TextPart(content="Made Agent_1.")]),
        ]
    )

    assert "User: Make a node" in text
    assert "Assistant called the tool GriptapeNodes_CreateNodeRequest." in text
    assert "Assistant: Made Agent_1." in text
    assert "secret" not in text


def test_catch_up_is_empty_without_messages() -> None:
    """Omit catch-up when history is empty."""
    assert render_catch_up([]) == ""


def test_catch_up_keeps_the_newest_messages_when_long() -> None:
    """Keep the newest messages within the catch-up limit."""
    old = ModelRequest(parts=[UserPromptPart(content="old " * CATCH_UP_MAX_CHARS)])
    new = ModelRequest(parts=[UserPromptPart(content="newest question")])

    text = render_catch_up([old, new])

    assert "[Earlier messages omitted.]" in text
    assert "newest question" in text
    assert len(text) < CATCH_UP_MAX_CHARS + 1000
