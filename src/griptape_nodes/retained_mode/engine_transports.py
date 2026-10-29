"""How an engine reaches other engines.

An engine whose distribution settings send a library's nodes to another engine asks that
engine to start the library's worker in the artist's session. The worker then registers with the
artist's engine like a local worker. Finding the other engine, choosing how to reach it, and carrying
the worker's traffic are the app transport's job, so the engine only names engines by ID.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload

# The version of the requests engines exchange with each other. While it is 0.x, any
# difference is refused, since both engines also have to run the same engine version.
DISTRIBUTED_EXECUTION_SCHEMA_VERSION = "0.1.0"


class EngineNotFoundError(Exception):
    """Raised when an engine cannot be reached or stops answering, carrying why, written for an artist."""


class EngineTransports(Protocol):
    """The transports an engine uses to reach other engines, by engine ID."""

    async def request(self, engine_id: str, request: RequestPayload, *, timeout_s: float | None) -> ResultPayload:
        """Send `request` to the engine `engine_id` and return its result.

        Args:
            engine_id: The engine to ask.
            request: What to ask it.
            timeout_s: How long to wait for the result, or None to wait until it arrives or the
                connection to that engine is lost.

        Raises:
            EngineNotFoundError: The engine cannot be found or reached, did not answer within
                `timeout_s`, or the connection to it was lost while waiting.
        """
        ...

    def hosted_worker_environment(self, engine_id: str) -> dict[str, str]:
        """The environment a worker this engine runs for `engine_id` needs to reach that engine.

        A worker reaches the engine whose session it joins the way that engine reached this one:
        over this engine's own server, or over the Griptape Cloud relay.
        """
        ...

    def route_worker(self, worker_engine_id: str, host_engine_id: str) -> None:
        """Carry this engine's traffic for the worker `worker_engine_id` over the transport to `host_engine_id`.

        `host_engine_id` is the engine that runs that worker.
        """
        ...

    def forget_worker(self, worker_engine_id: str) -> None:
        """Stop carrying traffic for that worker; forgetting a worker that isn't routed does nothing."""
        ...

    def on_engine_lost(self, callback: Callable[[str], Awaitable[None]]) -> None:
        """Call `callback` with an engine's ID once the connection to it is lost.

        A registered remote worker is not settled by this: heartbeats decide whether it is alive, as for a local one.
        """
        ...
