"""A worker simulated in this process, for tests of what crosses the orchestrator/worker boundary.

A real worker is another process with an engine of its own: its own object store, graph and managers, so the copy
of a node it loads never meets the orchestrator's node of the same name. `simulated_worker` builds that second
`Engine` beside the test's, in the worker role, and joins the two the way the wire does:

- The orchestrator's `NodeManager` finds the worker registered for the library, prepares the request as it does for
  any worker, and hands it to `SimulatedWorker.route_to_worker`, which passes it through its wire form and has the
  worker engine handle it.
- Requests the worker's node code sends while it runs are forwarded back through the wire form and answered by the
  orchestrator engine. Sync sends from node code reach the forwarder on a loop of its own, as they do in a worker.

Each side handles a request in a context of its own, as two processes would, so no context variable (strict-mode
scopes, the rebuild scope, the bound engine) carries from one to the other.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, patch

from griptape_nodes.app.worker_routing import register_remote_handlers
from griptape_nodes.retained_mode.engine import Engine, engine_scope
from griptape_nodes.retained_mode.events import converters
from griptape_nodes.retained_mode.events.base_events import EventResultFailure, EventResultSuccess

if TYPE_CHECKING:
    from collections.abc import Coroutine, Iterator

    from griptape_nodes.retained_mode.events.base_events import EventRequest, RequestPayload, ResultPayload
    from griptape_nodes.retained_mode.events.execution_events import ExecuteNodeRequest
    from griptape_nodes.retained_mode.managers.event_manager import ResultContext

_WORKER_ID = ("simulated-worker", "sessions/test/workers/simulated-worker/request")


def through_the_wire[T](payload: T) -> T:
    """`payload` as the other side of the wire rebuilds it: unstructured, sent as JSON, structured back."""
    sent = json.loads(converters.engine.dumps(payload))
    return cast("T", converters.engine.structure(sent, type(payload)))


@dataclass
class SimulatedWorker:
    """A worker engine and the orchestrator engine it serves.

    Attributes:
        orchestrator: The engine that holds the real nodes.
        engine: The worker's own engine.
        received: Every ExecuteNodeRequest the worker was sent, as it arrived.
        forwarded: Every request the worker forwarded to the orchestrator, as it was sent.
    """

    orchestrator: Engine
    engine: Engine
    received: list[ExecuteNodeRequest] = field(default_factory=list)
    forwarded: list[RequestPayload] = field(default_factory=list)

    async def route_to_worker(self, event_request: EventRequest, *_worker: Any) -> dict[str, Any]:
        """Stands in for `WorkerManager.route_to_worker`, returning the raw result dict it does."""
        result = await self.execute(cast("ExecuteNodeRequest", event_request.request))
        return {"result_type": type(result).__name__, "result": json.loads(converters.engine.dumps(result))}

    async def execute(self, request: ExecuteNodeRequest) -> ResultPayload:
        """Have the worker handle `request` as it arrives over the wire."""
        sent = through_the_wire(request)
        self.received.append(sent)
        return await _in_own_context(_handled_by(self.engine, sent))

    async def forward_to_orchestrator(
        self,
        request: RequestPayload,
        result_context: ResultContext,
    ) -> EventResultSuccess | EventResultFailure:
        """Stands in for the worker's `EventManager.forward_to_orchestrator`."""
        self.forwarded.append(request)
        result = await _in_own_context(_handled_by(self.orchestrator, through_the_wire(request)))
        event_type = EventResultSuccess if result.succeeded() else EventResultFailure
        return event_type(
            request=request,
            request_id=result_context.get("request_id"),
            result=through_the_wire(result),
            response_topic=result_context.get("response_topic"),
        )


@contextmanager
def simulated_worker(orchestrator: Engine, library_name: str) -> Iterator[SimulatedWorker]:
    """Build a worker engine and route every node of `library_name` that `orchestrator` runs to it."""

    def worker_for(name: str | None) -> tuple[str, str] | None:
        if name == library_name:
            return _WORKER_ID
        return None

    with (
        simulated_worker_engine(orchestrator) as worker,
        patch.object(orchestrator.library_manager.workers, "get_worker_for_library", side_effect=worker_for),
        patch.object(orchestrator.worker_manager, "wait_until_executable", new=AsyncMock()),
        patch.object(orchestrator.worker_manager, "route_to_worker", side_effect=worker.route_to_worker),
    ):
        yield worker


@contextmanager
def simulated_worker_engine(orchestrator: Engine) -> Iterator[SimulatedWorker]:
    """Build a worker engine that forwards to `orchestrator`, leaving how requests reach it to the caller."""
    worker_engine = Engine()
    worker_engine.library_manager._is_worker = True
    worker = SimulatedWorker(orchestrator=orchestrator, engine=worker_engine)
    event_manager = worker_engine.event_manager
    register_remote_handlers(event_manager)
    event_manager.forward_to_orchestrator = worker.forward_to_orchestrator  # type: ignore[method-assign]

    forwarding_loop = asyncio.new_event_loop()
    forwarding_thread = threading.Thread(target=forwarding_loop.run_forever, daemon=True)
    forwarding_thread.start()
    event_manager._websocket_event_loop = forwarding_loop
    try:
        yield worker
    finally:
        forwarding_loop.call_soon_threadsafe(forwarding_loop.stop)
        forwarding_thread.join(timeout=5)
        forwarding_loop.close()


async def _handled_by(engine: Engine, request: RequestPayload) -> ResultPayload:
    with engine_scope(engine):
        return await engine.ahandle_request(request)


async def _in_own_context[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Run `coroutine` in a context of its own, as another process would, and await it."""
    return await asyncio.get_running_loop().create_task(coroutine, context=contextvars.Context())
