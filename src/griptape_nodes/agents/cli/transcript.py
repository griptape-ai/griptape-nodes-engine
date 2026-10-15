from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from griptape_nodes.agents.harness import RunEvent, TextDelta, ThinkingDelta, ToolCall, ToolResult

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic_ai.messages import ModelMessage, ModelRequestPart, ModelResponsePart

# Recorded for a call the CLI started but never reported a result for, such as
# one cut off by a turn limit. Without it the call would look like one waiting
# for approval, and the next turn would refuse to run.
UNANSWERED_TOOL_CALL_MESSAGE = "The tool call ended without returning a result."

# Bounds what an earlier conversation adds to a prompt when a CLI joins a thread
# it has not seen all of. The newest messages are kept.
CATCH_UP_MAX_CHARS = 20000


class TranscriptBuilder:
    """Mirror CLI turns into thread history for sidebar rendering and provider switches."""

    def __init__(self, *, provider_name: str, model_name: str | None) -> None:
        self._provider_name = provider_name
        self._model_name = model_name
        self._messages: list[ModelMessage] = []
        self._response_parts: list[ModelResponsePart] = []
        self._result_parts: list[ModelRequestPart] = []
        self._open_call_names: dict[str, str] = {}

    def add(self, event: RunEvent) -> None:
        if isinstance(event, TextDelta):
            self._add_text(event.delta)
        elif isinstance(event, ThinkingDelta):
            self._add_thinking(event.delta)
        elif isinstance(event, ToolCall):
            self._close_request()
            self._response_parts.append(
                ToolCallPart(tool_name=event.tool_name, args=event.args or None, tool_call_id=event.tool_call_id)
            )
            self._open_call_names[event.tool_call_id] = event.tool_name
        elif isinstance(event, ToolResult):
            self._close_response()
            self._open_call_names.pop(event.tool_call_id, None)
            self._result_parts.append(
                ToolReturnPart(
                    tool_name=event.tool_name,
                    content=event.content,
                    tool_call_id=event.tool_call_id,
                    outcome="failed" if event.is_error else "success",
                )
            )

    def messages(self) -> list[ModelMessage]:
        """Return the turn's messages, ending in a ``ModelResponse``.

        Calls left without a result are answered as interrupted, so nothing in
        the saved turn looks like it is waiting for approval.
        """
        if self._open_call_names:
            self._close_response()
            for tool_call_id, tool_name in self._open_call_names.items():
                self._result_parts.append(
                    _tool_return(tool_name, tool_call_id, UNANSWERED_TOOL_CALL_MESSAGE, outcome="interrupted")
                )
            self._open_call_names = {}
        self._close_request()
        self._close_response()
        if not self._messages or not isinstance(self._messages[-1], ModelResponse):
            self._messages.append(self._response([]))
        return list(self._messages)

    def _add_text(self, delta: str) -> None:
        if not delta:
            return
        self._close_request()
        last = self._response_parts[-1] if self._response_parts else None
        if isinstance(last, TextPart):
            last.content += delta
            return
        self._response_parts.append(TextPart(content=delta))

    def _add_thinking(self, delta: str) -> None:
        if not delta:
            return
        self._close_request()
        last = self._response_parts[-1] if self._response_parts else None
        if isinstance(last, ThinkingPart):
            last.content += delta
            return
        self._response_parts.append(ThinkingPart(content=delta))

    def _close_response(self) -> None:
        if not self._response_parts:
            return
        self._messages.append(self._response(self._response_parts))
        self._response_parts = []

    def _close_request(self) -> None:
        if not self._result_parts:
            return
        self._messages.append(ModelRequest(parts=self._result_parts))
        self._result_parts = []

    def _response(self, parts: list[ModelResponsePart]) -> ModelResponse:
        return ModelResponse(parts=parts, model_name=self._model_name, provider_name=self._provider_name)


def user_turn(
    prompt: str | Sequence[object],
    *,
    denied: Sequence[ToolReturnPart] = (),
) -> ModelRequest:
    """Place declined tool answers before the new user prompt."""
    parts: list[ModelRequestPart] = [*denied, UserPromptPart(content=prompt)]  # type: ignore[arg-type]
    return ModelRequest(parts=parts)


def denied_tool_return(tool_name: str, tool_call_id: str, message: str) -> ToolReturnPart:
    """Match Pydantic AI's denied-tool result format."""
    return _tool_return(tool_name, tool_call_id, message, outcome="denied")


def render_catch_up(messages: Sequence[ModelMessage]) -> str:
    """Catch-up includes user text, replies, and tool names, not tool arguments or results."""
    lines: list[str] = []
    for message in messages:
        if isinstance(message, ModelRequest):
            lines.extend(_request_lines(message))
        elif isinstance(message, ModelResponse):
            lines.extend(_response_lines(message))
    if not lines:
        return ""
    transcript = "\n\n".join(lines)
    if len(transcript) > CATCH_UP_MAX_CHARS:
        transcript = "[Earlier messages omitted.]\n\n" + transcript[-CATCH_UP_MAX_CHARS:]
    return (
        "These earlier messages in this conversation happened outside your session. "
        "Treat them as context for the message that follows.\n"
        f"<earlier_conversation>\n{transcript}\n</earlier_conversation>\n\n"
    )


def _request_lines(message: ModelRequest) -> list[str]:
    lines: list[str] = []
    for part in message.parts:
        if not isinstance(part, UserPromptPart):
            continue
        if isinstance(part.content, str):
            lines.append(f"User: {part.content}")
            continue
        texts = [item for item in part.content if isinstance(item, str)]
        attachments = len(part.content) - len(texts)
        text = " ".join(texts)
        if attachments:
            text = f"{text} [{attachments} attachment(s)]".strip()
        lines.append(f"User: {text}")
    return lines


def _response_lines(message: ModelResponse) -> list[str]:
    lines: list[str] = []
    for part in message.parts:
        if isinstance(part, TextPart) and part.content.strip():
            lines.append(f"Assistant: {part.content}")
        elif isinstance(part, ToolCallPart):
            lines.append(f"Assistant called the tool {part.tool_name}.")
    return lines


def _tool_return(
    tool_name: str,
    tool_call_id: str,
    content: str,
    *,
    outcome: Literal["success", "failed", "denied", "interrupted"],
) -> ToolReturnPart:
    return ToolReturnPart(tool_name=tool_name, content=content, tool_call_id=tool_call_id, outcome=outcome)
