"""Build Pydantic AI :class:`MCPToolset` toolsets from engine MCP configs.

The engine speaks ``MCPServerConfig`` (a TypedDict with a ``transport`` plus
transport-specific fields). Pydantic AI exposes a single :class:`MCPToolset`
that takes a FastMCP transport (:class:`StdioTransport`, :class:`SSETransport`,
:class:`StreamableHttpTransport`). This module is the bridge.

Two cross-cutting concerns are layered on with Pydantic AI's toolset
combinators rather than hand-rolled logic:

* **Prefixing** - every tool is exposed under ``<name>_<tool>`` via
  :meth:`AbstractToolset.prefixed` so tools from different servers can't
  collide.
* **Blocklisting** - tools the agent should never see are dropped with
  :meth:`AbstractToolset.filtered`, which runs against the bare (unprefixed)
  tool names.

Connection handling is left to Pydantic AI: if a server is unreachable the
run fails. Graceful per-server degradation can be layered on later.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from fastmcp.client.transports import SSETransport, StdioTransport, StreamableHttpTransport
from pydantic_ai.mcp import MCPToolset

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pydantic_ai._run_context import RunContext
    from pydantic_ai.tools import ToolDefinition
    from pydantic_ai.toolsets import AbstractToolset


logger = logging.getLogger("griptape_nodes")


DEFAULT_TOOL_MAX_RETRIES = 3
"""How many times Pydantic AI retries a single MCP tool call after a `ModelRetry`.

The Pydantic AI default is 1, which is too tight: when an LLM (especially Claude)
fumbles the args for a tool with a structured `list[dict]` parameter, it usually
gets a validation error, sees the retry message, and corrects on the second
attempt. With `max_retries=1` that second attempt is the last one, so a single
schema misunderstanding kills the whole run.
"""

DEFAULT_CONNECT_TIMEOUT = 5.0
"""Initial-connection timeout (seconds) for HTTP-based transports."""

DEFAULT_GTN_TOOL_BLOCKLIST: frozenset[str] = frozenset()
"""GTN MCP tools the chat-sidebar agent never sees.

Left empty by default; the composer supports arbitrary blocklists for callers
that want to hide specific tools (e.g. tests, alternate harnesses).
"""


def mcp_server_from_config(name: str, config: Mapping[str, Any]) -> AbstractToolset[Any] | None:  # noqa: PLR0911
    """Build a Pydantic AI MCP toolset from an engine ``MCPServerConfig``.

    Returns ``None`` and logs a warning when the config is missing required
    fields for its declared transport. The returned toolset is prefixed with
    ``name`` so tools from different servers can't collide.
    """
    transport = config.get("transport", "stdio")

    if transport == "stdio":
        command = config.get("command")
        if not command:
            logger.warning("MCP server %r: stdio transport requires `command`; skipping.", name)
            return None
        client = StdioTransport(
            command=command,
            args=list(config.get("args") or []),
            env=_stdio_env(config.get("env")),
            cwd=config.get("cwd"),
        )
        return _compose(name, MCPToolset(client, max_retries=DEFAULT_TOOL_MAX_RETRIES))

    if transport == "sse":
        url = config.get("url")
        if not url:
            logger.warning("MCP server %r: sse transport requires `url`; skipping.", name)
            return None
        client = SSETransport(url=url, headers=dict(config.get("headers") or {}))
        return _compose(
            name,
            MCPToolset(client, max_retries=DEFAULT_TOOL_MAX_RETRIES, init_timeout=_connect_timeout(config)),
        )

    if transport == "streamable_http":
        url = config.get("url")
        if not url:
            logger.warning("MCP server %r: %s transport requires `url`; skipping.", name, transport)
            return None
        client = StreamableHttpTransport(url=url, headers=dict(config.get("headers") or {}))
        return _compose(
            name,
            MCPToolset(client, max_retries=DEFAULT_TOOL_MAX_RETRIES, init_timeout=_connect_timeout(config)),
        )

    logger.warning("MCP server %r: unsupported transport %r; skipping.", name, transport)
    return None


def streamable_http_local(url: str, *, name: str | None = None) -> AbstractToolset[Any]:
    """Convenience builder for the engine's own MCP server (streamable HTTP)."""
    server_name = name or "GriptapeNodes"
    return _compose(
        server_name,
        MCPToolset(StreamableHttpTransport(url=url), max_retries=DEFAULT_TOOL_MAX_RETRIES),
        tool_blocklist=DEFAULT_GTN_TOOL_BLOCKLIST,
    )


def _connect_timeout(config: Mapping[str, Any]) -> float:
    return float(config.get("timeout") or DEFAULT_CONNECT_TIMEOUT)


def _stdio_env(config_env: Mapping[str, str] | None) -> dict[str, str]:
    """Build the environment for an stdio MCP subprocess.

    The subprocess inherits the engine's full environment, with the server's
    configured ``env`` layered on top. Without this the MCP SDK forwards only a
    tiny allowlist (``HOME``/``LOGNAME``/``PATH``/``SHELL``/``TERM``/``USER``),
    which strips the toolchain variables a launcher like ``uv`` needs to resolve
    its target command. A launcher that survives but can't find its entry point
    then fails with ``Failed to spawn: <command>`` and the agent sees the MCP
    connection close. Inheriting the parent environment matches both the engine's
    other subprocess spawns and how desktop MCP clients launch stdio servers.
    """
    return {**os.environ, **(config_env or {})}


def _compose(
    name: str,
    toolset: AbstractToolset[Any],
    *,
    tool_blocklist: frozenset[str] = frozenset(),
) -> AbstractToolset[Any]:
    """Apply the optional blocklist and the name prefix to a raw MCP toolset.

    Order matters: the blocklist filter runs on bare tool names, so it must be
    applied *before* :meth:`prefixed` adds the ``<name>_`` prefix.
    """
    if tool_blocklist:
        toolset = toolset.filtered(_blocklist_filter(tool_blocklist))
    return toolset.prefixed(name)


def _blocklist_filter(
    tool_blocklist: frozenset[str],
) -> Callable[[RunContext[Any], ToolDefinition], bool]:
    """Build a `.filtered()` predicate that drops blocklisted tools by bare name."""

    def keep(_ctx: RunContext[Any], tool_def: ToolDefinition) -> bool:
        return tool_def.name not in tool_blocklist

    return keep
