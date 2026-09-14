"""Cache of live MCP toolsets, keyed by the configuration behind them.

A stdio transport keeps its subprocess alive between runs, and the connection
config is baked in when the toolset is built - so a cached toolset talks to a
server launched from the *old* config until something rebuilds it.

Two values are derived per config: :func:`connection_fingerprint`, over only
the baked-in keys, decides whether to reconnect; :func:`digest_config`, over
the whole config, records which configuration a run used. The fingerprint
contains ``env`` and ``headers``, so it must never be logged or persisted -
only the digest is opaque.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Self

from griptape_nodes.agents.pydantic_ai.mcp_servers import disconnect_transport, mcp_server_from_config

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from fastmcp.client.transports import ClientTransport
    from pydantic_ai.toolsets import AbstractToolset

logger = logging.getLogger("griptape_nodes")


DIGEST_LENGTH = 12
"""Hex characters kept from the fingerprint hash - short enough to read in a log."""


CONNECTION_KEYS: frozenset[str] = frozenset({"transport", "command", "args", "env", "cwd", "url", "headers", "timeout"})
"""Config keys baked in when a toolset is built, and so requiring a restart.

Everything `mcp_server_from_config` reads, plus ``timeout`` (its
``init_timeout``). Every other key is prompt-side and re-read each run, so add
one here only if the builder starts reading it - a test enforces that.
"""


def connection_fingerprint(config: Mapping[str, Any]) -> str:
    """Return the comparison form of only the parts of ``config`` a connection depends on.

    Two configs matching here can share one running server. Embeds ``env`` and
    ``headers``: treat as a secret.
    """
    connection = {key: value for key, value in config.items() if key in CONNECTION_KEYS}
    return fingerprint_config(connection)


def fingerprint_config(config: Mapping[str, Any]) -> str:
    """Return the canonical comparison form of a resolved MCP server config.

    ``default=str`` keeps a non-JSON value comparable instead of raising.
    Contains ``env`` and ``headers``: treat as a secret.
    """
    return json.dumps(config, sort_keys=True, default=str)


def digest_config(config: Mapping[str, Any]) -> str:
    """Return a short, opaque, persistable identifier for a resolved config."""
    return digest_of_fingerprint(fingerprint_config(config))


def digest_of_fingerprint(fingerprint: str) -> str:
    """Hash an already-computed fingerprint, so callers holding one needn't rebuild it."""
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:DIGEST_LENGTH]


@dataclass
class ResolvedMCPServer:
    """One MCP server as it was actually used for a run, identified by config digest."""

    name: str
    digest: str


@dataclass
class _Entry:
    """A live toolset, the transport under it, and the connection it was built from.

    Holds only what the running server owns; anything per-run lives on the lease,
    since one entry is shared by every run using that server.
    """

    name: str
    toolset: AbstractToolset[Any]
    transport: ClientTransport
    # `repr=False` because this embeds `env` and `headers`.
    connection_fingerprint: str = field(repr=False)
    # Runs currently inside `async with toolset`; eviction waits for zero,
    # because a transport cannot be disconnected mid-session.
    users: int = 0
    # Eviction was requested while still in use; the last user out tears down.
    retired: bool = False


@dataclass
class _LeasedServer:
    """One entry as one run borrowed it, with the config *that* run supplied.

    The digest belongs here, not on the shared entry: two overlapping runs can
    differ in a prompt-side field while sharing the server, and an entry-level
    digest would make both report whichever acquired last.
    """

    entry: _Entry
    digest: str


@dataclass
class MCPToolsetCache:
    """Holds one live toolset per MCP server, rebuilding when its config changes.

    Not safe to share across event loops; it is owned by a single manager and
    guarded by an ``asyncio.Lock`` so concurrent runs can't both decide to
    rebuild the same entry.
    """

    _entries: dict[str, _Entry] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def acquire(self, configs: Sequence[Mapping[str, Any]]) -> MCPToolsetLease:
        """Return a lease over the toolsets for ``configs``.

        Each config must carry a ``name``. A server whose fingerprint matches
        its cached entry is reused, keeping its subprocess warm; one whose
        config changed is disconnected and rebuilt; one that fails to build is
        omitted from the lease, having already logged why.

        The returned lease holds a use count on every toolset it names, so it
        must be released - use it as an async context manager.
        """
        servers: list[_LeasedServer] = []
        async with self._lock:
            # Two passes on purpose: resolving can raise, and there is no lease
            # yet to release what came before, so nothing takes a use count
            # until every entry is in hand. A stray count is never reapable.
            for config in configs:
                entry = await self._entry_for(str(config["name"]), config)
                if entry is not None:
                    servers.append(_LeasedServer(entry=entry, digest=digest_config(config)))
            for server in servers:
                server.entry.users += 1
        return MCPToolsetLease(cache=self, servers=servers)

    async def retain_only(self, names: Iterable[str]) -> None:
        """Drop cached servers that are no longer configured or enabled.

        A server merely not asked for on this run keeps its warm subprocess; a
        deleted or disabled one should not keep a process alive.
        """
        keep = set(names)
        async with self._lock:
            dropped = [n for n in self._entries if n not in keep]
            for name in dropped:
                await self._retire(name)
        if dropped:
            logger.debug("Shut down MCP server(s) no longer enabled: %s", ", ".join(sorted(dropped)))

    async def aclose(self) -> None:
        """Disconnect every cached server.

        Nothing in the engine calls this - managers have no shutdown hook, so
        cached servers are reaped by the process exiting. For tests, embedders,
        and whenever a manager shutdown path does appear.
        """
        async with self._lock:
            for name in list(self._entries):
                await self._retire(name)

    async def _entry_for(self, name: str, config: Mapping[str, Any]) -> _Entry | None:
        """Return a usable entry for ``name``, rebuilding it only if its connection changed."""
        connection = connection_fingerprint(config)
        cached = self._entries.get(name)
        if cached is not None and cached.connection_fingerprint == connection:
            # Connection unchanged, so the subprocess is kept whatever else moved.
            return cached
        if cached is not None:
            logger.info("MCP server '%s' connection settings changed; restarting it for this run.", name)
            await self._retire(name)
        built = mcp_server_from_config(name, config)
        if built is None:
            return None
        entry = _Entry(
            name=name,
            toolset=built.toolset,
            transport=built.transport,
            connection_fingerprint=connection,
        )
        self._entries[name] = entry
        return entry

    async def _retire(self, name: str) -> None:
        """Remove ``name`` from the cache, disconnecting it once nobody is using it.

        Callers must hold ``self._lock``. An entry still inside a run is marked
        ``retired`` and left connected; :meth:`_release` finishes the job.
        """
        entry = self._entries.pop(name, None)
        if entry is None:
            return
        if entry.users > 0:
            entry.retired = True
            return
        await disconnect_transport(name, entry.transport)

    async def _release(self, entries: Sequence[_Entry]) -> None:
        """Drop one use count per entry, disconnecting any retired entry that hits zero."""
        async with self._lock:
            for entry in entries:
                entry.users -= 1
                if entry.users <= 0 and entry.retired:
                    await disconnect_transport(entry.name, entry.transport)


@dataclass
class MCPToolsetLease:
    """Borrowed toolsets, held against eviction for the duration of one run.

    Holds entries rather than names: an entry retired mid-run leaves the cache
    at once, and the lease is what keeps it reachable long enough to disconnect.
    """

    cache: MCPToolsetCache
    servers: list[_LeasedServer]

    @property
    def toolsets(self) -> list[AbstractToolset[Any]]:
        """The toolsets to attach to this run."""
        return [server.entry.toolset for server in self.servers]

    @property
    def resolved(self) -> list[ResolvedMCPServer]:
        """Which servers, at which configuration, this run actually got."""
        return [ResolvedMCPServer(name=server.entry.name, digest=server.digest) for server in self.servers]

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        # The lease is the cache's own handle type, so reaching into `_release`
        # is one object talking to its other half rather than a leak.
        await self.cache._release([server.entry for server in self.servers])
