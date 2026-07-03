"""Tests for building Pydantic AI MCP toolsets from engine configs.

These cover two concerns without spinning up a real MCP server:

* config -> transport mapping in :func:`mcp_server_from_config`, and
* the combinator composition (optional blocklist filter -> name prefix) in
  :func:`_compose`.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

from fastmcp.client.transports import SSETransport, StdioTransport, StreamableHttpTransport
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.toolsets.filtered import FilteredToolset
from pydantic_ai.toolsets.prefixed import PrefixedToolset

from griptape_nodes.agents.pydantic_ai.mcp_servers import (
    _blocklist_filter,
    _compose,
    mcp_server_from_config,
    streamable_http_local,
)


def _mcp_toolset(composed: Any) -> MCPToolset:
    """Walk the combinator chain down to the wrapped MCPToolset."""
    node = composed
    while not isinstance(node, MCPToolset):
        node = node.wrapped
    return node


def test_stdio_without_command_returns_none() -> None:
    """A stdio config missing `command` is rejected."""
    assert mcp_server_from_config("svc", {"transport": "stdio"}) is None


def test_sse_without_url_returns_none() -> None:
    """An sse config missing `url` is rejected."""
    assert mcp_server_from_config("svc", {"transport": "sse"}) is None


def test_streamable_http_without_url_returns_none() -> None:
    """A streamable-http config missing `url` is rejected."""
    assert mcp_server_from_config("svc", {"transport": "streamable_http"}) is None


def test_unsupported_transport_returns_none() -> None:
    """An unknown transport is rejected rather than guessed."""
    assert mcp_server_from_config("svc", {"transport": "carrier-pigeon"}) is None


def test_websocket_transport_returns_none() -> None:
    """Websocket has no FastMCP transport, so it is rejected rather than routed over HTTP."""
    assert mcp_server_from_config("svc", {"transport": "websocket", "url": "ws://h/mcp"}) is None


def test_stdio_maps_command_args_env_cwd() -> None:
    """Stdio config fields land on the FastMCP StdioTransport.

    The subprocess inherits the engine's environment with the configured ``env``
    layered on top, so the launcher (e.g. ``uv``) keeps the toolchain variables
    it needs to resolve its target command.
    """
    composed = mcp_server_from_config(
        "svc",
        {"transport": "stdio", "command": "uvx", "args": ["server"], "env": {"K": "V"}, "cwd": "/srv/app"},
    )
    transport = _mcp_toolset(composed).client.transport
    assert isinstance(transport, StdioTransport)
    assert transport.command == "uvx"
    assert transport.args == ["server"]
    assert transport.env is not None
    assert transport.env == {**os.environ, "K": "V"}
    assert transport.cwd == "/srv/app"


def test_stdio_inherits_parent_env_when_config_env_empty() -> None:
    """An empty config ``env`` still inherits the parent environment.

    Regression guard: forwarding only the MCP SDK allowlist stripped the PATH and
    toolchain variables a launcher like ``uv`` needs, so stdio servers failed to
    spawn while HTTP servers (no subprocess) worked.
    """
    composed = mcp_server_from_config("svc", {"transport": "stdio", "command": "uv", "env": {}})
    transport = _mcp_toolset(composed).client.transport
    assert isinstance(transport, StdioTransport)
    assert transport.env == dict(os.environ)


def test_sse_maps_url_and_headers() -> None:
    """An sse config builds an SSETransport at the configured URL."""
    composed = mcp_server_from_config("svc", {"transport": "sse", "url": "http://h/sse", "headers": {"A": "1"}})
    transport = _mcp_toolset(composed).client.transport
    assert isinstance(transport, SSETransport)
    assert str(transport.url) == "http://h/sse"


def test_streamable_http_maps_url() -> None:
    """A streamable-http config builds a StreamableHttpTransport at the configured URL."""
    composed = mcp_server_from_config("svc", {"transport": "streamable_http", "url": "http://h/mcp/"})
    transport = _mcp_toolset(composed).client.transport
    assert isinstance(transport, StreamableHttpTransport)
    assert str(transport.url) == "http://h/mcp/"


def test_default_transport_is_stdio() -> None:
    """A config with no `transport` key defaults to stdio."""
    composed = mcp_server_from_config("svc", {"command": "run"})
    assert isinstance(_mcp_toolset(composed).client.transport, StdioTransport)


def test_compose_applies_name_prefix() -> None:
    """`_compose` exposes tools under the server name prefix."""
    composed = _compose("Svc", MCPToolset(StreamableHttpTransport(url="http://h/mcp/"), max_retries=3))
    assert isinstance(composed, PrefixedToolset)
    assert composed.prefix == "Svc"
    assert isinstance(composed.wrapped, MCPToolset)


def test_compose_without_blocklist_skips_filter() -> None:
    """No blocklist means no filter layer is inserted."""
    composed = _compose("Svc", MCPToolset(StreamableHttpTransport(url="http://h/mcp/"), max_retries=3))
    assert isinstance(composed, PrefixedToolset)
    assert not isinstance(composed.wrapped, FilteredToolset)


def test_compose_with_blocklist_inserts_filter_under_prefix() -> None:
    """A non-empty blocklist inserts a filter between the prefix and the MCP toolset."""
    composed = _compose(
        "Svc",
        MCPToolset(StreamableHttpTransport(url="http://h/mcp/"), max_retries=3),
        tool_blocklist=frozenset({"danger"}),
    )
    assert isinstance(composed, PrefixedToolset)
    assert isinstance(composed.wrapped, FilteredToolset)
    assert isinstance(composed.wrapped.wrapped, MCPToolset)


def test_empty_blocklist_is_not_applied() -> None:
    """An empty blocklist must not wrap the toolset in a filter at all."""
    composed = _compose(
        "Svc",
        MCPToolset(StreamableHttpTransport(url="http://h/mcp/"), max_retries=3),
        tool_blocklist=frozenset(),
    )
    assert isinstance(composed, PrefixedToolset)
    assert not isinstance(composed.wrapped, FilteredToolset)


def test_streamable_http_local_prefixes_with_default_name() -> None:
    """The engine's own server defaults to the `GriptapeNodes` prefix over streamable HTTP."""
    composed = streamable_http_local("http://localhost:9/mcp/")
    assert isinstance(composed, PrefixedToolset)
    assert composed.prefix == "GriptapeNodes"
    assert isinstance(_mcp_toolset(composed).client.transport, StreamableHttpTransport)


def test_blocklist_filter_drops_only_listed_bare_names() -> None:
    """The predicate keeps unlisted tools and drops listed ones by bare name."""
    keep = _blocklist_filter(frozenset({"EventRequestBatch"}))
    assert keep(None, SimpleNamespace(name="CreateNodeRequest")) is True  # type: ignore[arg-type]
    assert keep(None, SimpleNamespace(name="EventRequestBatch")) is False  # type: ignore[arg-type]
