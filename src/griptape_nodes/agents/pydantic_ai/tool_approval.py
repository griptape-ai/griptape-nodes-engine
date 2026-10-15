"""Per-run approval state for chat-agent tool calls that spend credits or run work.

A gated call ends the run with Pydantic AI's ``DeferredToolRequests`` instead of
executing, and the caller resumes the thread later with ``DeferredToolResults``.

Which calls are gated is decided per toolset (see ``mcp_servers`` and
``image_tools``), but every gate reads the same :class:`ToolApprovalDeps` off
``ctx.deps``. The state has to arrive per run: the engine's MCP toolset is baked
into a cached agent and user MCP toolsets are cached across runs, so anything
captured at construction would go stale the moment the user grants or clears a
permission.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic_ai.messages import ModelRequest, RetryPromptPart, ToolReturnPart
from pydantic_ai.tools import ToolDenied

from griptape_nodes.agents.thread_history import answered_ids, tool_call_tail

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from collections.abc import Set as AbstractSet

    from pydantic_ai.messages import ModelMessage, ModelRequestPart
    from pydantic_ai.tools import DeferredToolResults


TOOL_DENIED_BY_USER_MESSAGE = (
    "The user declined to run this. Do not retry it or work around it with a different tool. "
    "Answer in text, or ask the user before trying again."
)
"""What the model reads in place of a tool result the user denied."""

TOOL_STOPPED_WHILE_RUNNING_MESSAGE = (
    "The user stopped the reply after this started. It may still be running or may have finished. "
    "Check before running it again."
)
"""What the model reads for an approved call that was running when the user cancelled the reply."""


@dataclass(frozen=True)
class ToolApprovalDeps:
    """Approval state for one agent run, passed as the run's ``deps``.

    Attributes:
        granted: Grant keys the user approved for the whole conversation. A gated
            call runs without asking when all of its grant keys are in here.
    """

    granted: frozenset[str] = field(default_factory=frozenset)


def needs_approval(deps: object, grant_keys: Sequence[str]) -> bool:
    """Whether a call with these grant keys must wait for the user.

    ``deps`` is the run's ``ctx.deps``. Anything other than a
    :class:`ToolApprovalDeps` raises rather than letting a gated call run
    unasked. An empty ``grant_keys`` means the call has nothing that needs
    approving.

    Raises:
        TypeError: ``deps`` is not a :class:`ToolApprovalDeps`.
    """
    if not isinstance(deps, ToolApprovalDeps):
        msg = (
            "Attempted to check whether a chat agent tool call needs the user's approval. Failed because the "
            f"run carried no approval state (got {type(deps).__name__})."
        )
        raise TypeError(msg)
    if not grant_keys:
        return False
    return not deps.granted.issuperset(grant_keys)


@dataclass(frozen=True)
class SettledResume:
    """History after saving what a cancelled resume did, and the results it added.

    Attributes:
        messages: The full history to save.
        added_parts: The results this added for calls the run had not reported a
            result for, so the caller can tell the editor those calls are over.
    """

    messages: list[ModelMessage]
    added_parts: list[ToolReturnPart]


def settle_cancelled_resume(
    history: Sequence[ModelMessage],
    deferred_tool_results: DeferredToolResults,
    *,
    started_tool_call_ids: AbstractSet[str],
    tool_results: Mapping[str, ToolReturnPart | RetryPromptPart],
) -> SettledResume | None:
    """Give each paused call a result for what a cancelled resume did to it.

    A call that finished keeps its result. One that started but did not report a
    result gets :data:`TOOL_STOPPED_WHILE_RUNNING_MESSAGE`, since it may still be
    running. A denied one gets its denial. An approved one that never started
    gets nothing, so it stays pending and the user can answer it again. Returns
    ``None`` when there is nothing to add.

    Started means the run reached the call, which pydantic-ai reports for every
    call in a step before any of them executes, so a call that only got as far
    as validation is also treated as started. That errs toward telling the user
    to check rather than offering to run it twice.
    """
    tail = tool_call_tail(history)
    if tail is None:
        return None
    answered = answered_ids(tail.results)
    decisions = deferred_tool_results.to_tool_call_results()

    added: list[ToolReturnPart | RetryPromptPart] = []
    synthesized: list[ToolReturnPart] = []
    for call in tail.response.tool_calls:
        if call.tool_call_id in answered:
            continue
        finished = tool_results.get(call.tool_call_id)
        if finished is not None:
            added.append(finished)
            continue
        decision = decisions.get(call.tool_call_id)
        if isinstance(decision, ToolDenied):
            part = ToolReturnPart(
                tool_name=call.tool_name, content=decision.message, tool_call_id=call.tool_call_id, outcome="denied"
            )
        elif call.tool_call_id in started_tool_call_ids:
            part = ToolReturnPart(
                tool_name=call.tool_name,
                content=TOOL_STOPPED_WHILE_RUNNING_MESSAGE,
                tool_call_id=call.tool_call_id,
                outcome="interrupted",
            )
        else:
            continue
        added.append(part)
        synthesized.append(part)

    if not added:
        return None

    kept = list(history)
    existing_parts: list[ModelRequestPart] = []
    if tail.results is not None:
        kept = kept[:-1]
        existing_parts = list(tail.results.parts)
    return SettledResume(messages=[*kept, ModelRequest(parts=[*existing_parts, *added])], added_parts=synthesized)
