"""How many times a tool call gets retried after a `ModelRetry`.

One knob for every tool the chat agent can call: the agent's own tools, MCP
toolsets, and the skills capability's bundled-file tools.
"""

from __future__ import annotations

DEFAULT_TOOL_MAX_RETRIES = 3
"""How many times Pydantic AI retries a single tool call after a `ModelRetry`.

The Pydantic AI default is 1, which is too tight: when an LLM (especially Claude)
fumbles the args for a tool with a structured `list[dict]` parameter, it usually
gets a validation error, sees the retry message, and corrects on the second
attempt. With `max_retries=1` that second attempt is the last one, so a single
schema misunderstanding kills the whole run.
"""
