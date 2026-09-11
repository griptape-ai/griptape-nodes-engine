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

**Transport lifetime.** Pydantic AI enters and exits a toolset once per
``Agent.run``, but that is not the lifetime of the server. ``StdioTransport``
defaults to ``keep_alive=True``, so exiting a session leaves the subprocess
running and the next run reuses it. A toolset therefore pins one subprocess,
launched from the config the transport was *built* with, for as long as the
toolset is held. Whoever caches a toolset owns calling
:func:`disconnect_transport` when the *connection* behind it changes; otherwise
the edit cannot take effect and the old subprocess is never reaped. Which
config keys those are is recorded as
:data:`~griptape_nodes.agents.pydantic_ai.mcp_toolset_cache.CONNECTION_KEYS`,
so a new transport field read here has to be added there too. See
:class:`~griptape_nodes.agents.pydantic_ai.mcp_toolset_cache.MCPToolsetCache`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastmcp.client.transports import ClientTransport, SSETransport, StdioTransport, StreamableHttpTransport
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


@dataclass(frozen=True)
class BuiltMCPServer:
    """A composed MCP toolset paired with the transport it speaks over.

    The transport is carried alongside the toolset because whoever caches the
    toolset also owns tearing it down, and reaching the transport back through
    the toolset means walking Pydantic AI and FastMCP internals
    (``toolset.wrapped.client.transport``). Returning it explicitly keeps that
    coupling in one place.
    """

    toolset: AbstractToolset[Any]
    transport: ClientTransport


_HTTP_TRANSPORTS: dict[str, type[SSETransport | StreamableHttpTransport]] = {
    "sse": SSETransport,
    "streamable_http": StreamableHttpTransport,
}
"""The URL-based transports, which differ only in the class they instantiate."""


def mcp_server_from_config(name: str, config: Mapping[str, Any]) -> BuiltMCPServer | None:
    """Build a Pydantic AI MCP toolset from an engine ``MCPServerConfig``.

    Returns ``None`` and logs a warning for any config this cannot build from -
    a missing required field, an unusable URL, an unknown transport. Never
    raises: one unbuildable server must not stop the agent attaching the others,
    and a caller part-way through building a set of them would otherwise be left
    holding a half-built result. The returned toolset is prefixed with ``name``
    so tools from different servers can't collide.
    """
    transport = config.get("transport", "stdio")

    if transport == "stdio":
        return _stdio_server_from_config(name, config)

    if transport in _HTTP_TRANSPORTS:
        return _http_server_from_config(name, config, str(transport))

    logger.warning("MCP server %r: unsupported transport %r; skipping.", name, transport)
    return None


def _stdio_server_from_config(name: str, config: Mapping[str, Any]) -> BuiltMCPServer | None:
    """Build a subprocess-backed MCP server.

    No ``init_timeout``: unlike the URL transports there is no network handshake
    to bound, and a slow-starting local server is not a failure.
    """
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
    return BuiltMCPServer(
        toolset=_compose(name, MCPToolset(client, max_retries=DEFAULT_TOOL_MAX_RETRIES)),
        transport=client,
    )


def _http_server_from_config(name: str, config: Mapping[str, Any], transport: str) -> BuiltMCPServer | None:
    """Build a URL-backed MCP server (``sse`` or ``streamable_http``)."""
    url = config.get("url")
    if not url:
        logger.warning("MCP server %r: %s transport requires `url`; skipping.", name, transport)
        return None
    try:
        client = _HTTP_TRANSPORTS[transport](url=url, headers=dict(config.get("headers") or {}))
    # The transport constructor rejects a URL that isn't http:// or https://,
    # and nothing validates the field on the way in, so a plain typo lands here.
    except ValueError as e:
        logger.warning(
            "Attempted to reach MCP server '%s' at '%s'. That is not a usable web address, so the server "
            "will be skipped. Check it starts with http:// or https://. Failed due to: %s",
            name,
            url,
            e,
        )
        return None
    return BuiltMCPServer(
        toolset=_compose(
            name,
            MCPToolset(client, max_retries=DEFAULT_TOOL_MAX_RETRIES, init_timeout=_connect_timeout(config)),
        ),
        transport=client,
    )


async def disconnect_transport(name: str, transport: ClientTransport) -> None:
    """Tear down ``transport``, if its kind holds anything to tear down.

    Only :class:`StdioTransport` owns a long-lived resource: it defaults to
    ``keep_alive=True``, so its subprocess outlives each session and survives
    until something disconnects it explicitly. The HTTP transports expose no
    ``disconnect`` at all and hold no process, so for them this is a no-op
    rather than a missing case.
    """
    if not isinstance(transport, StdioTransport):
        return
    try:
        await transport.disconnect()
    # Teardown must not fail the caller: a server that will not shut down
    # cleanly is a warning, not a reason to abandon the run that outlived it.
    except Exception as e:
        logger.warning("Attempted to shut down MCP server '%s'. Failed because of: %s", name, e)


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
