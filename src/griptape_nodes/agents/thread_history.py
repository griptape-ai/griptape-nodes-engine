"""Helpers for reading the saved thread format, shared by every harness.

Threads are saved as Pydantic AI ``ModelMessage`` values whichever harness ran
them, so a thread can switch providers. A turn that paused for approval is saved
as a response carrying tool calls, optionally followed by a request holding only
tool results; these helpers read that shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic_ai.messages import ModelRequest, ModelResponse, RetryPromptPart, ToolReturnPart

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic_ai.messages import ModelMessage, ToolCallPart


TOOL_DENIED_BY_NEW_MESSAGE = "The user sent a new message instead of answering this approval request."
"""What the model reads for a pending call the user skipped by sending another message."""


@dataclass(frozen=True)
class ToolCallTail:
    """The response a turn ended on, and the request of results after it, if any."""

    response: ModelResponse
    results: ModelRequest | None


def pending_tool_calls(history: Sequence[ModelMessage]) -> list[ToolCallPart]:
    """Return the tool calls a paused turn is still waiting on, in call order.

    A turn that paused for approval ends in the ``ModelResponse`` carrying the
    calls, optionally followed by a ``ModelRequest`` holding only tool results:
    those of the calls that ran without asking, and any a cancelled resume
    saved. The pending calls are the ones with no result. Any other shape,
    including a trailing request with a user prompt in it, has nothing pending.
    """
    tail = tool_call_tail(history)
    if tail is None:
        return []
    answered = answered_ids(tail.results)
    return [call for call in tail.response.tool_calls if call.tool_call_id not in answered]


def ends_in_tool_results(history: Sequence[ModelMessage]) -> bool:
    """Whether history ends in a response's tool calls followed by a request of only their results."""
    tail = tool_call_tail(history)
    if tail is None:
        return False
    return tail.results is not None


def tool_call_tail(history: Sequence[ModelMessage]) -> ToolCallTail | None:
    """Return the response history ends on, with the request of only tool results after it if any.

    ``None`` when history is empty or ends any other way, such as a request
    carrying a user prompt.
    """
    if not history:
        return None
    last = history[-1]
    if isinstance(last, ModelResponse):
        return ToolCallTail(response=last, results=None)
    if not isinstance(last, ModelRequest) or len(history) < 2:  # noqa: PLR2004
        return None
    previous = history[-2]
    only_results = all(isinstance(part, (ToolReturnPart, RetryPromptPart)) for part in last.parts)
    if not isinstance(previous, ModelResponse) or not only_results:
        return None
    return ToolCallTail(response=previous, results=last)


def answered_ids(results: ModelRequest | None) -> set[str]:
    """Return the ids of the tool calls that ``results`` holds a result for."""
    if results is None:
        return set()
    return {part.tool_call_id for part in results.parts if isinstance(part, (ToolReturnPart, RetryPromptPart))}
