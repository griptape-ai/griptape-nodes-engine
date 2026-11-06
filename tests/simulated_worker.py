"""A worker simulated in this process, for tests of what crosses the orchestrator/worker boundary.

A real worker is another process with an engine of its own: its own object store, graph and managers, so the copy
of a node it loads never meets the orchestrator's node of the same name. `simulated_worker` builds that second
`Engine` beside the test's, in the worker role, and joins the two the way their channel does, minus the socket:

- The orchestrator's `NodeManager` finds the worker registered for the library, prepares the request as it does for
  any worker, and hands it to `SimulatedWorker.route_to_worker`. That passes it through the channel's wire form and
  has the worker engine answer it as the worker's end of the channel does.
- Requests the worker's node code sends while it runs are forwarded by the worker's real `RemoteHandler`s, as the
  same envelope a real worker sends, through the orchestrator's real worker request gate, and answered by the
  orchestrator engine as the orchestrator's end of the channel does. Sync sends from node code reach the forwarder
  on a loop of its own, as they do in a worker.

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
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events import converters
from griptape_nodes.retained_mode.events.base_events import EventRequest
from griptape_nodes.retained_mode.managers.event_manager import read_forwarded_result
from griptape_nodes.worker.parent import ParentEnd
from griptape_nodes.worker.wire import (
    CHANNEL_REQUEST_TOPIC,
    answer_event_request,
    event_request_envelope,
    read_event_request,
)

if TYPE_CHECKING:
    from collections.abc import Coroutine, Iterator

    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload
    from griptape_nodes.retained_mode.events.execution_events import ExecuteNodeRequest

_WORKER_ID = ("simulated-worker", "")


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
        parent_end: The orchestrator's end of the channel, whose gate every forwarded request passes.
        received: Every ExecuteNodeRequest the worker was sent, as it arrived.
        forwarded: Every request the worker forwarded to the orchestrator, as it was sent.
    """

    orchestrator: Engine
    engine: Engine
    parent_end: ParentEnd
    received: list[ExecuteNodeRequest] = field(default_factory=list)
    forwarded: list[RequestPayload] = field(default_factory=list)

    async def route_to_worker(self, event_request: EventRequest, *_worker: Any) -> dict[str, Any]:
        """Stands in for `WorkerManager.route_to_worker`, returning the raw result the worker's channel end replies with."""
        sent = read_event_request(event_request_envelope(event_request))
        self.received.append(cast("ExecuteNodeRequest", sent.request))
        return await _in_own_context(answer_event_request(self.engine, sent))

    async def execute(self, request: ExecuteNodeRequest) -> ResultPayload:
        """Have the worker handle `request` as it arrives over the wire."""
        raw = await self.route_to_worker(EventRequest(request=request))
        return read_forwarded_result(request, raw)

    async def request_from_worker(
        self,
        event_request: EventRequest,
        request_topic: str,
        worker_response_topic: str,  # noqa: ARG002
        timeout_ms: int | None = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """Stands in for the worker's channel: carries a forwarded request to the orchestrator's end and back."""
        self.forwarded.append(event_request.request)
        decision = self.parent_end.gate(event_request_envelope(event_request, request_topic))
        if decision.dispatch is None:
            return cast("dict[str, Any]", decision.reply)
        sent = read_event_request(decision.dispatch)
        return await _in_own_context(self.parent_end.answer_worker_request(sent))


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
    parent_end = ParentEnd(
        orchestrator, on_hello=_not_simulated, on_closed=lambda: None, relay_to_editor=_not_simulated
    )
    worker = SimulatedWorker(orchestrator=orchestrator, engine=worker_engine, parent_end=parent_end)
    event_manager = worker_engine.event_manager

    forwarding_loop = asyncio.new_event_loop()
    forwarding_thread = threading.Thread(target=forwarding_loop.run_forever, daemon=True)
    forwarding_thread.start()
    event_manager.configure_worker_forwarding(
        request_client=worker,
        orchestrator_request_topic=CHANNEL_REQUEST_TOPIC,
        worker_response_topic="worker",
        websocket_event_loop=forwarding_loop,
    )
    register_remote_handlers(event_manager)
    try:
        yield worker
    finally:
        forwarding_loop.call_soon_threadsafe(forwarding_loop.stop)
        forwarding_thread.join(timeout=5)
        forwarding_loop.close()


def sent_from_open_workflow(request: ExecuteNodeRequest, orchestrator: Engine) -> ExecuteNodeRequest:
    """`request` carrying `orchestrator`'s open workflow, as its node executor sends it. A worker refuses one without."""
    snapshot = orchestrator.project_manager.workflow_context_for_dispatch(orchestrator.current_workflow())
    request.orchestrator_workflow_id = snapshot.workflow_id
    request.workflow_name = snapshot.name
    request.workflow_file_path = snapshot.file_path
    request.workflow_working_directory = snapshot.working_directory
    return request


async def _not_simulated(*_args: Any) -> Any:
    msg = "A simulated worker neither says hello nor relays results."
    raise AssertionError(msg)


async def _in_own_context[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Run `coroutine` in a context of its own, as another process would, and await it."""
    return await asyncio.get_running_loop().create_task(coroutine, context=contextvars.Context())
