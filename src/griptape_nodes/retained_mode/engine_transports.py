"""How an engine reaches other engines.

An engine whose distribution settings send a library's nodes to another engine asks that
engine to start the library's worker. That engine runs the worker as its own child process and
carries the worker's traffic to the artist's engine as requests between the two engines, so only
engines reach each other. Finding the other engine and choosing how to reach it are the app
transport's job, so the engine only names engines by ID.
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

    async def request(
        self,
        engine_id: str,
        request: RequestPayload,
        *,
        timeout_s: float | None,
        open_workflow_id: str | None = None,
        worker_engine_id: str | None = None,
    ) -> ResultPayload:
        """Send `request` to the engine `engine_id` and return its result.

        Args:
            engine_id: The engine to ask.
            request: What to ask it.
            timeout_s: How long to wait for the result, or None to wait until it arrives or the
                connection to that engine is lost.
            open_workflow_id: Set as the envelope's `open_workflow_id`: the workflow, on that engine,
                the request runs in.
            worker_engine_id: Set as the envelope's `worker_engine_id`, on a request a worker this engine
                runs for that engine sent, which that engine answers as its own worker's.

        Raises:
            EngineNotFoundError: The engine cannot be found or reached, did not answer within
                `timeout_s`, or the connection to it was lost while waiting.
        """
        ...

    def on_engine_lost(self, callback: Callable[[str], Awaitable[None]]) -> None:
        """Call `callback` with an engine's ID once the connection to it is lost."""
        ...
