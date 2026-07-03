"""Pydantic AI bindings for Griptape Cloud.

:func:`build_griptape_cloud_model` returns a Pydantic AI :class:`OpenAIChatModel`
pointed at Griptape Cloud's OpenAI-compatible ``/api/v1`` endpoint, so a
`pydantic_ai.Agent` talks to Cloud through the standard Chat Completions client.
Text, native tool calls, structured output, and streaming usage all flow through
that built-in path with no custom message translation.
"""

from griptape_nodes.agents.pydantic_ai.model import build_griptape_cloud_model
from griptape_nodes.agents.pydantic_ai.runner import AgentRunResult, PydanticAgentRunner

__all__ = [
    "AgentRunResult",
    "PydanticAgentRunner",
    "build_griptape_cloud_model",
]
