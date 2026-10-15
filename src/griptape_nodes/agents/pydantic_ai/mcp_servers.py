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
* **Approval** - calls that spend credits or run work wait for the user via
  :meth:`AbstractToolset.approval_required`, which runs against the prefixed
  names, since those are what grant keys are built from.

Connection handling is left to Pydantic AI: if a server is unreachable the
run fails. Graceful per-server degradation can be layered on later.

**Transport lifetime.** Pydantic AI enters and exits a toolset per
``Agent.run``, but ``StdioTransport`` defaults to ``keep_alive=True``: the
subprocess outlives the session, so a toolset pins one server launched from the
config it was *built* with until :func:`disconnect_transport` is called. See
:class:`~griptape_nodes.agents.pydantic_ai.mcp_toolset_cache.MCPToolsetCache`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastmcp.client.transports import ClientTransport, SSETransport, StdioTransport, StreamableHttpTransport
from pydantic_ai.mcp import MCPToolset

from griptape_nodes.agents.harness import GTN_SERVER_NAME
from griptape_nodes.agents.pydantic_ai.tool_approval import needs_approval

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

GTN_APPROVAL_REQUIRED_REQUESTS: frozenset[str] = frozenset(
    {
        "RunWorkflowWithCurrentStateRequest",
        "ResolveNodeRequest",
        "ExecuteNodeRequest",
        "StartFlowRequest",
        "StartFlowFromNodeRequest",
    }
)
"""Engine MCP tools (bare names) that run work, so the chat agent asks before calling them.

Every other engine tool only reads or edits the workflow and runs without asking.
Each name must be a key of ``servers.mcp.SUPPORTED_REQUEST_EVENTS``; a test holds
the two in step.
"""

GTN_BATCH_TOOL_NAME = f"{GTN_SERVER_NAME}_EventRequestBatch"
"""Prefixed name of the engine's batch tool, whose inner requests are gated one by one.

The bare name must equal ``servers.mcp.EVENT_REQUEST_BATCH_TOOL_NAME``; a test
holds the two in step.
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

    Never raises: returns ``None`` and logs a warning for any config it cannot
    build from, so one bad server doesn't stop the others being attached. The
    returned toolset is prefixed with ``name``.
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

    Deliberately no ``init_timeout`` - there is no network handshake to bound.
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
        toolset=_compose(
            name,
            MCPToolset(client, max_retries=DEFAULT_TOOL_MAX_RETRIES),
            requires_approval=_user_tool_requires_approval,
        ),
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
    # The constructor rejects a URL that isn't http(s), and nothing validates
    # the field on the way in, so a typo lands here.
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
            requires_approval=_user_tool_requires_approval,
        ),
        transport=client,
    )


async def disconnect_transport(name: str, transport: ClientTransport) -> None:
    """Tear down ``transport``, if its kind holds anything to tear down.

    Only :class:`StdioTransport` does - it keeps a subprocess alive between
    sessions. A deliberate no-op for the HTTP transports, which hold no process
    and expose no ``disconnect``.
    """
    if not isinstance(transport, StdioTransport):
        return
    try:
        await transport.disconnect()
    # A server that won't shut down cleanly is a warning, not a failed run.
    except Exception as e:
        logger.warning("Attempted to shut down MCP server '%s'. Failed because of: %s", name, e)


def streamable_http_local(url: str) -> AbstractToolset[Any]:
    """Convenience builder for the engine's own MCP server (streamable HTTP).

    Always prefixed with :data:`GTN_SERVER_NAME`: the approval gate and grant keys
    match the engine's tools by that prefix.
    """
    return _compose(
        GTN_SERVER_NAME,
        MCPToolset(StreamableHttpTransport(url=url), max_retries=DEFAULT_TOOL_MAX_RETRIES),
        requires_approval=_gtn_tool_requires_approval,
        tool_blocklist=DEFAULT_GTN_TOOL_BLOCKLIST,
    )


def prefixed_tool_name(server: str, tool: str) -> str:
    """Match the names produced by ``_compose``'s toolset prefix."""
    return f"{server}_{tool}"


def grant_keys_for_call(tool_name: str, args: Mapping[str, Any]) -> list[str]:
    """Return the grant keys for a call that needs approval.

    A grant key names one action by the tool name it has as a standalone call, so
    approving it for the conversation covers that action however it is invoked.
    The engine's batch tool gets one key per gated request type inside it, in
    order and without repeats; every other tool's only key is its own name.
    """
    if tool_name != GTN_BATCH_TOOL_NAME:
        return [tool_name]

    requests = args.get("requests")
    if not isinstance(requests, list):
        return []
    keys: list[str] = []
    for entry in requests:
        if not isinstance(entry, dict):
            continue
        request_type = entry.get("request_type")
        if request_type not in GTN_APPROVAL_REQUIRED_REQUESTS:
            continue
        key = f"{GTN_SERVER_NAME}_{request_type}"
        if key not in keys:
            keys.append(key)
    return keys


def _gtn_tool_requires_approval(ctx: RunContext[Any], tool_def: ToolDefinition, tool_args: dict[str, Any]) -> bool:
    """Gate the engine tools that run work, and batches containing any of them."""
    bare_name = tool_def.name.removeprefix(f"{GTN_SERVER_NAME}_")
    if tool_def.name != GTN_BATCH_TOOL_NAME and bare_name not in GTN_APPROVAL_REQUIRED_REQUESTS:
        return False
    return needs_approval(ctx.deps, grant_keys_for_call(tool_def.name, tool_args))


def _user_tool_requires_approval(ctx: RunContext[Any], tool_def: ToolDefinition, tool_args: dict[str, Any]) -> bool:
    """Gate every tool on a user-configured server: the engine cannot tell what they do."""
    return needs_approval(ctx.deps, grant_keys_for_call(tool_def.name, tool_args))


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
    requires_approval: Callable[[RunContext[Any], ToolDefinition, dict[str, Any]], bool],
    tool_blocklist: frozenset[str] = frozenset(),
) -> AbstractToolset[Any]:
    """Apply the optional blocklist, the name prefix, and the approval gate to a raw MCP toolset.

    Order matters: the blocklist filter runs on bare tool names, so it must be
    applied *before* :meth:`prefixed` adds the ``<name>_`` prefix, and the
    approval gate runs on prefixed names, so it goes on *after*.
    ``requires_approval`` has no default so no server can be attached ungated by
    omission.
    """
    if tool_blocklist:
        toolset = toolset.filtered(_blocklist_filter(tool_blocklist))
    # `prefixed` joins with "_", which `prefixed_tool_name` mirrors.
    return toolset.prefixed(name).approval_required(requires_approval)


def _blocklist_filter(
    tool_blocklist: frozenset[str],
) -> Callable[[RunContext[Any], ToolDefinition], bool]:
    """Build a `.filtered()` predicate that drops blocklisted tools by bare name."""

    def keep(_ctx: RunContext[Any], tool_def: ToolDefinition) -> bool:
        return tool_def.name not in tool_blocklist

    return keep
