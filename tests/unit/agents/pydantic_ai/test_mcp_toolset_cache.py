"""Tests for the MCP toolset cache.

The behaviour under test is a lifetime policy, not a protocol: an unchanged
server keeps its connection, an edited one is torn down and rebuilt, and a
server still inside a run is never disconnected out from under it. None of that
needs a real MCP server, so these tests use stub transports that record whether
they were disconnected.
"""

from __future__ import annotations

import inspect
import re
from typing import Any

import pytest
from fastmcp.client.transports import StdioTransport

from griptape_nodes.agents.pydantic_ai.mcp_servers import BuiltMCPServer, mcp_server_from_config
from griptape_nodes.agents.pydantic_ai.mcp_toolset_cache import (
    CONNECTION_KEYS,
    DIGEST_LENGTH,
    MCPToolsetCache,
    connection_fingerprint,
    digest_config,
    fingerprint_config,
)


class _StubTransport(StdioTransport):
    """A transport that records disconnection instead of owning a subprocess."""

    def __init__(self, tag: str) -> None:
        super().__init__(command="never-run", args=[])
        self.tag = tag
        self.disconnected = False

    async def disconnect(self) -> None:
        self.disconnected = True


def _stub_builder(built: list[_StubTransport]) -> Any:
    """A `mcp_server_from_config` stand-in that records each server it builds."""

    def build(name: str, config: dict[str, Any]) -> BuiltMCPServer | None:
        if config.get("broken"):
            return None
        # Tagged from `args`, a connection key, so a transport's tag identifies
        # the config it was actually launched from.
        transport = _StubTransport(tag=f"{name}:{''.join(config.get('args') or [])}")
        built.append(transport)
        return BuiltMCPServer(toolset=object(), transport=transport)  # type: ignore[arg-type]

    return build


@pytest.fixture
def transports(monkeypatch: pytest.MonkeyPatch) -> list[_StubTransport]:
    """Every transport the cache has built, in build order."""
    built: list[_StubTransport] = []
    monkeypatch.setattr(
        "griptape_nodes.agents.pydantic_ai.mcp_toolset_cache.mcp_server_from_config",
        _stub_builder(built),
    )
    return built


def _config(name: str, tag: str = "v1", rules: str = "") -> dict[str, Any]:
    """A server config. ``tag`` varies a connection key; ``rules`` varies a prompt-side one."""
    return {"name": name, "transport": "stdio", "command": "run", "args": [tag], "rules": rules}


class TestFingerprintAndDigest:
    """The comparison key ignores dict ordering; the persistable form is opaque."""

    def test_key_order_does_not_change_the_fingerprint(self) -> None:
        assert fingerprint_config({"a": 1, "b": 2}) == fingerprint_config({"b": 2, "a": 1})

    def test_a_changed_value_changes_the_fingerprint(self) -> None:
        assert fingerprint_config({"a": 1}) != fingerprint_config({"a": 2})

    def test_non_json_values_do_not_raise(self) -> None:
        assert fingerprint_config({"path": object()})

    def test_digest_is_short_and_hides_the_config(self) -> None:
        config = {"name": "svc", "env": {"TOKEN": "hunter2"}}
        digest = digest_config(config)

        assert len(digest) == DIGEST_LENGTH
        assert "hunter2" not in digest

    def test_digest_is_stable_for_the_same_config(self) -> None:
        assert digest_config({"a": 1}) == digest_config({"a": 1})


class TestWarmReuse:
    """An unchanged server keeps the connection it already has."""

    @pytest.mark.asyncio
    async def test_second_acquire_reuses_the_first_toolset(self, transports: list[_StubTransport]) -> None:
        cache = MCPToolsetCache()

        async with await cache.acquire([_config("alpha")]) as first:
            first_toolsets = first.toolsets
        async with await cache.acquire([_config("alpha")]) as second:
            assert second.toolsets == first_toolsets

        assert len(transports) == 1
        assert not transports[0].disconnected

    @pytest.mark.asyncio
    async def test_resolved_reports_the_config_each_server_ran_with(self, transports: list[_StubTransport]) -> None:
        cache = MCPToolsetCache()
        config = _config("alpha")

        async with await cache.acquire([config]) as lease:
            resolved = lease.resolved

        assert len(transports) == 1
        assert [server.name for server in resolved] == ["alpha"]
        assert resolved[0].digest == digest_config(config)


class TestConfigChange:
    """A server whose *connection* changed is restarted; its neighbours are left alone."""

    @pytest.mark.asyncio
    async def test_edited_server_is_disconnected_and_rebuilt(self, transports: list[_StubTransport]) -> None:
        cache = MCPToolsetCache()

        async with await cache.acquire([_config("alpha", tag="v1")]):
            pass
        async with await cache.acquire([_config("alpha", tag="v2")]) as lease:
            assert lease.toolsets

        assert [t.tag for t in transports] == ["alpha:v1", "alpha:v2"]
        assert transports[0].disconnected
        assert not transports[1].disconnected

    @pytest.mark.asyncio
    async def test_unedited_neighbour_keeps_its_connection(self, transports: list[_StubTransport]) -> None:
        cache = MCPToolsetCache()

        async with await cache.acquire([_config("alpha", tag="v1"), _config("beta")]):
            pass
        async with await cache.acquire([_config("alpha", tag="v2"), _config("beta")]):
            pass

        by_tag = {t.tag: t for t in transports}
        assert set(by_tag) == {"alpha:v1", "alpha:v2", "beta:v1"}
        assert by_tag["alpha:v1"].disconnected
        assert not by_tag["beta:v1"].disconnected


class TestPromptSideEditsKeepTheConnection:
    """A field that can't reach the transport must not cost a reconnect.

    Editing a server's Rules is the common case: the text is passed as run
    instructions every run regardless, so respawning the subprocess for it buys
    nothing and costs a process spawn.
    """

    @pytest.mark.asyncio
    async def test_editing_only_the_rules_reuses_the_server(self, transports: list[_StubTransport]) -> None:
        cache = MCPToolsetCache()

        async with await cache.acquire([_config("alpha", rules="be terse")]) as first:
            first_toolsets = first.toolsets
        async with await cache.acquire([_config("alpha", rules="reply in spanish")]) as second:
            assert second.toolsets == first_toolsets

        assert len(transports) == 1
        assert not transports[0].disconnected

    @pytest.mark.asyncio
    async def test_the_reused_server_still_reports_the_new_config(self, transports: list[_StubTransport]) -> None:
        edited = _config("alpha", rules="reply in spanish")

        cache = MCPToolsetCache()
        async with await cache.acquire([_config("alpha", rules="be terse")]):
            pass
        async with await cache.acquire([edited]) as lease:
            resolved = lease.resolved

        # Same subprocess, but the run record must name the config that produced
        # the answer - not the one the subprocess happened to be started with.
        assert len(transports) == 1
        assert resolved[0].digest == digest_config(edited)

    @pytest.mark.asyncio
    async def test_an_unknown_new_field_does_not_restart_the_server(self, transports: list[_StubTransport]) -> None:
        # A field this cache has never heard of is assumed prompt-side. Anything
        # that truly reaches the transport has to be added to `CONNECTION_KEYS`.
        config = _config("alpha")

        cache = MCPToolsetCache()
        async with await cache.acquire([config]):
            pass
        async with await cache.acquire([{**config, "description": "now documented"}]):
            pass

        assert len(transports) == 1
        assert not transports[0].disconnected


class TestConnectionFingerprint:
    """Only the keys baked in at build time are compared."""

    def test_prompt_side_fields_are_ignored(self) -> None:
        assert connection_fingerprint(_config("alpha", rules="a")) == connection_fingerprint(
            _config("alpha", rules="b")
        )

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("command", "other"),
            ("args", ["--flag"]),
            ("env", {"TOKEN": "x"}),
            ("cwd", "/opt/servers"),
            ("url", "https://example.com/mcp"),
            ("headers", {"Authorization": "Bearer x"}),
            ("timeout", 30),
            ("transport", "streamable_http"),
        ],
    )
    def test_every_connection_key_changes_the_fingerprint(self, key: str, value: Any) -> None:
        config = _config("alpha")

        assert connection_fingerprint(config) != connection_fingerprint({**config, key: value})

    def test_it_covers_what_the_builder_reads(self) -> None:
        # Guards the pair going out of sync: if `mcp_server_from_config` learns a
        # new transport field, this fails until `CONNECTION_KEYS` learns it too.
        source = inspect.getsource(mcp_server_from_config)
        read_by_builder = set(re.findall(r"""config\.get\(["'](\w+)["']""", source))

        assert read_by_builder <= CONNECTION_KEYS


class TestRetireWhileInUse:
    """A run in flight never loses the server it is talking to."""

    @pytest.mark.asyncio
    async def test_eviction_waits_for_the_run_to_finish(self, transports: list[_StubTransport]) -> None:
        cache = MCPToolsetCache()

        async with await cache.acquire([_config("alpha", tag="v1")]):
            # A concurrent edit lands mid-run: the entry leaves the cache, but
            # the transport this run is using must stay connected.
            async with await cache.acquire([_config("alpha", tag="v2")]):
                assert not transports[0].disconnected
            assert not transports[0].disconnected

        assert transports[0].disconnected

    @pytest.mark.asyncio
    async def test_the_replacement_is_not_torn_down_with_the_old_one(self, transports: list[_StubTransport]) -> None:
        cache = MCPToolsetCache()

        # Nested on purpose: the second acquire retires the first entry while
        # the first lease still holds it.
        async with (
            await cache.acquire([_config("alpha", tag="v1")]),
            await cache.acquire([_config("alpha", tag="v2")]),
        ):
            pass

        assert transports[0].disconnected
        assert not transports[1].disconnected


class TestRetainOnly:
    """A deleted or disabled server does not keep a connection for the session."""

    @pytest.mark.asyncio
    async def test_unlisted_servers_are_disconnected(self, transports: list[_StubTransport]) -> None:
        cache = MCPToolsetCache()

        async with await cache.acquire([_config("alpha"), _config("beta")]):
            pass
        await cache.retain_only(["beta"])

        by_tag = {t.tag: t for t in transports}
        assert by_tag["alpha:v1"].disconnected
        assert not by_tag["beta:v1"].disconnected

    @pytest.mark.asyncio
    async def test_retaining_nothing_disconnects_everything(self, transports: list[_StubTransport]) -> None:
        cache = MCPToolsetCache()

        async with await cache.acquire([_config("alpha")]):
            pass
        await cache.retain_only([])

        assert transports[0].disconnected


class TestUnbuildableServers:
    """A server that cannot be built is omitted rather than failing the run."""

    @pytest.mark.asyncio
    async def test_bad_config_is_left_out_of_the_lease(self, transports: list[_StubTransport]) -> None:
        cache = MCPToolsetCache()

        async with await cache.acquire([{"name": "broken", "broken": True}, _config("alpha")]) as lease:
            assert len(lease.toolsets) == 1
            assert [server.name for server in lease.resolved] == ["alpha"]

        assert len(transports) == 1

    @pytest.mark.asyncio
    async def test_acquiring_nothing_yields_an_empty_lease(self, transports: list[_StubTransport]) -> None:
        cache = MCPToolsetCache()

        async with await cache.acquire([]) as lease:
            assert lease.toolsets == []
            assert lease.resolved == []

        assert transports == []


class TestAclose:
    """Shutdown reaps every server the cache is holding."""

    @pytest.mark.asyncio
    async def test_every_server_is_disconnected(self, transports: list[_StubTransport]) -> None:
        cache = MCPToolsetCache()

        async with await cache.acquire([_config("alpha"), _config("beta")]):
            pass
        await cache.aclose()

        assert all(t.disconnected for t in transports)

    @pytest.mark.asyncio
    async def test_a_later_acquire_rebuilds_from_scratch(self, transports: list[_StubTransport]) -> None:
        cache = MCPToolsetCache()

        async with await cache.acquire([_config("alpha")]):
            pass
        await cache.aclose()
        async with await cache.acquire([_config("alpha")]):
            pass

        assert [transport.tag for transport in transports] == ["alpha:v1", "alpha:v1"]
