"""Cache of live MCP toolsets, keyed by the configuration behind them.

An MCP toolset is cheap to build (~0.1 ms) but expensive to *run*: a stdio
server's transport keeps its subprocess alive between runs, so a toolset held
across runs saves a process spawn and an ``initialize`` round-trip. That reuse
is only correct while the configuration behind it is unchanged - the command,
args, env, cwd, url and headers are baked into the transport when it is built,
so a cached toolset speaks to a server launched from the *old* config forever.

This cache makes the trade explicitly: entries are keyed by server name and
carry a fingerprint of the config, so an unchanged server keeps its warm
subprocess and an edited one is torn down and rebuilt. Rebuilding on a
fingerprint change rather than on a config-change event means edits made
outside the event system - a hand-edited config file, another process writing
the same file - are picked up just the same.

Not every edit needs a restart, though, and restarting on the ones that don't
is expensive: respawning a subprocess costs hundreds of milliseconds where
reuse costs none. So two different questions are asked of each config, and the
difference between them matters:

* *Do we have to reconnect?* - answered by :func:`connection_fingerprint`, over
  only the keys baked into the transport at build time. A ``rules`` edit is
  prompt-side and leaves this untouched, so the server keeps its subprocess.
* *Which configuration did this run use?* - answered by :func:`digest_config`
  over the whole config, because ``rules`` genuinely change the answer the
  model gives even though they don't change the connection.

Both are derived from :func:`fingerprint_config` output, which contains ``env``
and ``headers`` and so holds secrets: a fingerprint lives in memory as a
comparison key and must never be logged or persisted. Only the short digest is
opaque enough to write to disk or send to a client.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Self

from pydantic_ai.toolsets import WrapperToolset

from griptape_nodes.agents.pydantic_ai.mcp_servers import disconnect_transport, mcp_server_from_config

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from fastmcp.client.transports import ClientTransport
    from pydantic_ai import RunContext
    from pydantic_ai.tools import ToolsetTool
    from pydantic_ai.toolsets import AbstractToolset

logger = logging.getLogger("griptape_nodes")


TIMING_LOG_PREFIX = "MCP-TIMING"
"""Marker on the temporary per-run MCP timing lines.

No square brackets: the engine's log handler is a `RichHandler` built with
`markup=True`, which parses `[mcp-timing]` as a style tag and silently drops it,
leaving the lines unfindable in the very logs they exist to annotate.

TODO(#5459): remove `TimedToolset` and every log line carrying this prefix once
we have confirmed in the field that per-run attachment costs what we measured
locally. Grep for the prefix to find all of it.
"""


DIGEST_LENGTH = 12
"""Characters of hex kept from the fingerprint hash.

Long enough that two configurations of one server won't collide in practice,
short enough to read in a log line or a thread's metadata.
"""


CONNECTION_KEYS: frozenset[str] = frozenset({"transport", "command", "args", "env", "cwd", "url", "headers", "timeout"})
"""Config keys that are baked in when a toolset is built, and so require a restart.

Everything `mcp_server_from_config` reads to construct a transport, plus
``timeout``, which becomes the toolset's ``init_timeout``. A change to any of
them cannot reach a server that is already running, so the only way to apply it
is to tear the server down and build a new one.

Every *other* key - ``rules``, ``description``, ``enabled``, and anything added
later - is prompt-side or bookkeeping and is re-read from the config on each
run, so editing it must not cost a reconnect. Adding a field here is therefore a
deliberate choice to trade warm reuse for correctness; the default of leaving it
out is only wrong if the field ends up passed to `mcp_server_from_config`.
"""


def connection_fingerprint(config: Mapping[str, Any]) -> str:
    """Return the comparison form of only the parts of ``config`` a connection depends on.

    Two configs with the same connection fingerprint can share one running
    server, however much the rest of them differs. Like
    :func:`fingerprint_config`, the result embeds ``env`` and ``headers``: treat
    it as a secret.
    """
    connection = {key: value for key, value in config.items() if key in CONNECTION_KEYS}
    return fingerprint_config(connection)


def fingerprint_config(config: Mapping[str, Any]) -> str:
    """Return the canonical comparison form of a resolved MCP server config.

    Sorted keys make the result independent of dict ordering, and ``default=str``
    keeps a config carrying a non-JSON value comparable instead of raising. The
    result contains ``env`` and ``headers``: treat it as a secret.
    """
    return json.dumps(config, sort_keys=True, default=str)


def digest_config(config: Mapping[str, Any]) -> str:
    """Return a short, opaque, persistable identifier for a resolved config."""
    return digest_of_fingerprint(fingerprint_config(config))


def digest_of_fingerprint(fingerprint: str) -> str:
    """Hash an already-computed fingerprint, so callers holding one needn't rebuild it."""
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:DIGEST_LENGTH]


@dataclass
class TimedToolset(WrapperToolset[Any]):
    """Temporary instrumentation: times the work `Agent.run` does on a toolset.

    Most of the per-run cost of an MCP server is not in our code - it is the
    connect and ``tools/list`` that happen when the agent enters the toolset. A
    wrapper is the only place we can see them from.

    TODO(#5459): delete this class along with the rest of the timing logs.
    """

    server_name: str = ""

    async def __aenter__(self) -> Self:
        started = time.monotonic()
        await super().__aenter__()
        logger.info(
            "%s server '%s' connect took %.1f ms",
            TIMING_LOG_PREFIX,
            self.server_name,
            (time.monotonic() - started) * 1000,
        )
        return self

    async def __aexit__(self, *args: object) -> bool | None:
        started = time.monotonic()
        result = await super().__aexit__(*args)
        logger.info(
            "%s server '%s' release took %.1f ms",
            TIMING_LOG_PREFIX,
            self.server_name,
            (time.monotonic() - started) * 1000,
        )
        return result

    async def get_tools(self, ctx: RunContext[Any]) -> dict[str, ToolsetTool[Any]]:
        started = time.monotonic()
        tools = await super().get_tools(ctx)
        logger.info(
            "%s server '%s' listed %d tool(s) in %.1f ms",
            TIMING_LOG_PREFIX,
            self.server_name,
            len(tools),
            (time.monotonic() - started) * 1000,
        )
        return tools


@dataclass
class ResolvedMCPServer:
    """One MCP server as it was actually used for a run.

    ``digest`` identifies the configuration, so two runs of the same named
    server can be told apart when the config changed between them.
    """

    name: str
    digest: str


@dataclass
class _Entry:
    """A live toolset, the transport under it, and the config it was built from."""

    name: str
    toolset: AbstractToolset[Any]
    transport: ClientTransport
    # What the running server was launched from: if this changes, the server has
    # to be replaced, because there is no way to tell a live subprocess about it.
    connection_fingerprint: str
    # The whole config this entry was last acquired with, as a persistable
    # digest. Unlike the fingerprint above it is updated in place when a
    # prompt-side field changes, so a run record names the config that actually
    # produced the answer rather than the one the server was started with.
    digest: str
    # Runs currently inside `async with toolset`. A transport cannot be
    # disconnected out from under a live session, so eviction waits for zero.
    users: int = 0
    # Set when eviction was requested while `users` was non-zero; the last
    # user out does the teardown. A retired entry is already out of the cache
    # dict, so it is reachable only through the leases still holding it - which
    # is why a lease holds entries rather than names.
    retired: bool = False


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
        started = time.monotonic()
        entries: list[_Entry] = []
        async with self._lock:
            for config in configs:
                entry = await self._entry_for(str(config["name"]), config)
                if entry is None:
                    continue
                entry.users += 1
                entries.append(entry)
        logger.info(
            "%s acquired %d of %d server(s) in %.1f ms",
            TIMING_LOG_PREFIX,
            len(entries),
            len(configs),
            (time.monotonic() - started) * 1000,
        )
        return MCPToolsetLease(cache=self, entries=entries)

    async def retain_only(self, names: Iterable[str]) -> None:
        """Drop cached servers that are no longer configured or enabled.

        A server the user simply didn't ask for on this run keeps its warm
        subprocess; one that has been deleted or disabled should not keep a
        process alive, so it is torn down here.
        """
        keep = set(names)
        started = time.monotonic()
        async with self._lock:
            dropped = [n for n in self._entries if n not in keep]
            for name in dropped:
                await self._retire(name)
        if dropped:
            logger.info(
                "%s shut down %s in %.1f ms",
                TIMING_LOG_PREFIX,
                ", ".join(sorted(dropped)),
                (time.monotonic() - started) * 1000,
            )

    async def aclose(self) -> None:
        """Disconnect every cached server. Called when the owning manager shuts down."""
        async with self._lock:
            for name in list(self._entries):
                await self._retire(name)

    async def _entry_for(self, name: str, config: Mapping[str, Any]) -> _Entry | None:
        """Return a usable entry for ``name``, rebuilding it only if its connection changed."""
        connection = connection_fingerprint(config)
        digest = digest_config(config)
        cached = self._entries.get(name)
        if cached is not None and cached.connection_fingerprint == connection:
            # The connection is still valid, so the subprocess is kept - but the
            # rest of the config may have moved, and the digest has to follow it.
            cached.digest = digest
            logger.info("%s server '%s' warm, reusing its connection", TIMING_LOG_PREFIX, name)
            return cached
        if cached is not None:
            logger.info("MCP server '%s' connection settings changed; restarting it for this run.", name)
            await self._retire(name)
        started = time.monotonic()
        built = mcp_server_from_config(name, config)
        if built is None:
            return None
        entry = _Entry(
            name=name,
            toolset=TimedToolset(wrapped=built.toolset, server_name=name),
            transport=built.transport,
            connection_fingerprint=connection,
            digest=digest,
        )
        self._entries[name] = entry
        logger.info(
            "%s server '%s' built in %.1f ms (digest %s)",
            TIMING_LOG_PREFIX,
            name,
            (time.monotonic() - started) * 1000,
            digest,
        )
        return entry

    async def _retire(self, name: str) -> None:
        """Remove ``name`` from the cache, disconnecting it once nobody is using it.

        Callers must hold ``self._lock``. An entry still inside a run is left
        connected and marked ``retired``; :meth:`_release` finishes the job when
        the last user exits, so an in-flight run never loses its server.
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

    Holds the cache entries themselves rather than server names: an entry
    retired mid-run is removed from the cache immediately, and the lease is
    what keeps it reachable long enough to be disconnected on release.
    """

    cache: MCPToolsetCache
    entries: list[_Entry]

    @property
    def toolsets(self) -> list[AbstractToolset[Any]]:
        """The toolsets to attach to this run."""
        return [entry.toolset for entry in self.entries]

    @property
    def resolved(self) -> list[ResolvedMCPServer]:
        """Which servers, at which configuration, this run actually got."""
        return [ResolvedMCPServer(name=entry.name, digest=entry.digest) for entry in self.entries]

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        # The lease is the cache's own handle type, so reaching into `_release`
        # is one object talking to its other half rather than a leak.
        await self.cache._release(self.entries)
