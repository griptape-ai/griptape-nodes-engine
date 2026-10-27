"""A stand-in worker that hosts libraries in this process, for e2e tests that cross the worker boundary.

A Workflow node always runs on the orchestrator, because it reads the flows it imports. The nodes inside
its subflow are routed to their own libraries' workers. A library whose nodes run in a worker process
hands every result back to the orchestrator through ``converters.engine``, which turns each exception into a
``ForwardedException`` that keeps only the type name, message and traceback. Requests a worker node makes
while it runs are forwarded to the orchestrator and their results cross the same way.

The worker here is a stand-in running in this process. It mirrors the wire at four points:

- ``NodeManager._execute_node_via_worker`` builds a fresh transient node from the request, runs it with the
  worker role active, and round-trips the result through JSON and ``converters.engine``.
- While a worker-role node runs, every request it sends through ``engine.handle_request`` or
  ``engine.ahandle_request`` that the real worker would forward (anything outside
  ``LOCAL_ONLY_REQUEST_TYPES``) is handled with the worker role dropped, as the orchestrator would, and its
  result is round-tripped the same way. Requests that edit the node being hydrated are refused as
  ``RemoteHandler`` refuses them.
- The worker role is a ``ContextVar``, so nested, concurrent, and re-entrant dispatch (a node of the
  subflow sent to the same worker while the Workflow node is still running) are each scoped correctly.
- While the worker role is active, ``engine.object_manager`` holds nothing, as on a real worker, where the
  orchestrator's flows and nodes were never created. Code that reads them directly instead of by request
  (``flow_manager.get_flow_by_name``) fails as it would there. Forwarded requests run with the role
  dropped, so they see the orchestrator's objects.

Fidelity limits: it is one process. Requests the real worker answers locally are answered by the shared engine.
Only requests made while a worker-role node runs are forwarded; the transient node's construction runs
without the worker role. Nothing crosses a real transport, so size limits and timeouts are not modelled.
"""

from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NamedTuple

from griptape_nodes.app.worker_routing import LOCAL_ONLY_REQUEST_TYPES, RemoteHandler, edits_node
from griptape_nodes.exe_types.core_types import WORKFLOW_NODE_KEY
from griptape_nodes.retained_mode.events import converters
from griptape_nodes.retained_mode.events.execution_events import ExecuteNodeRequest, ExecuteNodeResultFailure
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager
from griptape_nodes.retained_mode.managers.worker_manager import WorkerRegistration

if TYPE_CHECKING:
    import pytest

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload

WORKER_TRACEBACK_MARKER = "\nWorker traceback:\n"

# Set while a node runs in the stand-in worker, so requests it makes are forwarded.
_in_worker: ContextVar[bool] = ContextVar("in_stand_in_worker", default=False)


class Forwarded(NamedTuple):
    """A request a worker node forwarded to the orchestrator, and the result that crossed back."""

    request_type: str
    result: ResultPayload


@dataclass
class Crossings:
    """What the stand-in worker sent back across the boundary."""

    node_requests: list[ExecuteNodeRequest] = field(default_factory=list)
    node_results: list[ResultPayload] = field(default_factory=list)
    forwarded: list[Forwarded] = field(default_factory=list)

    def workflow_node_names(self) -> list[str]:
        """Return the names of the Workflow nodes that were sent to the worker."""
        return [r.node_name for r in self.node_requests if (r.node_metadata or {}).get(WORKFLOW_NODE_KEY)]

    def node_names(self) -> list[str]:
        """Return the names of every node that was sent to the worker."""
        return [r.node_name for r in self.node_requests]

    def assert_only_inner_nodes_crossed(self) -> None:
        """Assert no Workflow node crossed, and that nodes from inside the subflows did."""
        assert self.workflow_node_names() == [], self
        assert self.node_names(), self

    def node_failures(self) -> list[ExecuteNodeResultFailure]:
        """Return the node results that were failures."""
        return [result for result in self.node_results if isinstance(result, ExecuteNodeResultFailure)]


class StandInWorker:
    """Hosts libraries in this process behind the same result conversion a real worker's wire applies."""

    def __init__(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        self.engine = engine
        self.crossings = Crossings()
        self._monkeypatch = monkeypatch
        self._real_handle = engine.handle_request
        self._real_ahandle = engine.ahandle_request
        real_is_worker = LibraryManager.is_worker

        def is_worker(manager: LibraryManager) -> bool:
            return _in_worker.get() or bool(real_is_worker.fget(manager))  # type: ignore[misc]

        monkeypatch.setattr(LibraryManager, "is_worker", property(is_worker))
        self._hide_orchestrator_objects(engine, monkeypatch)
        monkeypatch.setattr(engine, "handle_request", self._handle_request)
        monkeypatch.setattr(engine, "ahandle_request", self._ahandle_request)
        monkeypatch.setattr(engine.node_manager, "_execute_node_via_worker", self._run_on_worker)

    def host(self, library_name: str) -> None:
        """Make a registered library execute in this worker, until the test's monkeypatch is undone."""
        info = self.engine.library_manager.get_library_info_by_library_name(library_name)
        assert info is not None, library_name
        registration = WorkerRegistration(
            request_topic=f"sessions/test/workers/{library_name}/request", worker_key=library_name
        )
        self._monkeypatch.setattr(info, "executes_in_worker", True)
        self._monkeypatch.setitem(self.engine.worker_manager._workers, f"stand-in-{library_name}", registration)

    def _handle_request(self, request: RequestPayload) -> ResultPayload:
        if not _forwards(request):
            return self._real_handle(request)
        refused = self._refusal(request)
        if refused is not None:
            return refused
        token = _in_worker.set(False)
        try:
            result = self._real_handle(request)
        finally:
            _in_worker.reset(token)
        return self._record(request, result)

    async def _ahandle_request(self, request: RequestPayload) -> ResultPayload:
        if not _forwards(request):
            return await self._real_ahandle(request)
        refused = self._refusal(request)
        if refused is not None:
            return refused
        token = _in_worker.set(False)
        try:
            result = await self._real_ahandle(request)
        finally:
            _in_worker.reset(token)
        return self._record(request, result)

    async def _run_on_worker(self, request: ExecuteNodeRequest, *_args: Any) -> ResultPayload:
        node_manager = self.engine.node_manager
        self.crossings.node_requests.append(request)
        fresh_copy = node_manager._materialize_transient_node_from_metadata(request)
        if isinstance(fresh_copy, ExecuteNodeResultFailure):
            result: ResultPayload = fresh_copy
        else:
            token = _in_worker.set(True)
            try:
                result = await node_manager._hydrate_and_run_node(fresh_copy, request)
            finally:
                _in_worker.reset(token)
        crossed = across_wire(result)
        self.crossings.node_results.append(crossed)
        return crossed

    def _refusal(self, request: RequestPayload) -> ResultPayload | None:
        rebuilding = self.engine.event_manager.hydrating_node_name()
        if rebuilding is None or not edits_node(request, rebuilding):
            return None
        return RemoteHandler._refuse(request, rebuilding)

    def _record(self, request: RequestPayload, result: ResultPayload) -> ResultPayload:
        crossed = across_wire(result)
        self.crossings.forwarded.append(Forwarded(type(request).__name__, crossed))
        return crossed

    @staticmethod
    def _hide_orchestrator_objects(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        """Make the object manager answer as an empty one while the worker role is active."""
        object_manager = engine.object_manager
        real_attempt_get = object_manager.attempt_get_object_by_name
        real_has = object_manager.has_object_with_name
        real_get = object_manager.get_object_by_name
        real_filtered = object_manager.get_filtered_subset

        def attempt_get(name: str) -> Any | None:
            if _in_worker.get():
                return None
            return real_attempt_get(name)

        def has(name: str) -> bool:
            if _in_worker.get():
                return False
            return real_has(name)

        def get(name: str) -> object:
            if _in_worker.get():
                raise KeyError(name)
            return real_get(name)

        def filtered(*args: Any, **kwargs: Any) -> dict[str, Any]:
            if _in_worker.get():
                return {}
            return real_filtered(*args, **kwargs)

        monkeypatch.setattr(object_manager, "attempt_get_object_by_name", attempt_get)
        monkeypatch.setattr(object_manager, "has_object_with_name", has)
        monkeypatch.setattr(object_manager, "get_object_by_name", get)
        monkeypatch.setattr(object_manager, "get_filtered_subset", filtered)


def _forwards(request: RequestPayload) -> bool:
    return _in_worker.get() and type(request) not in LOCAL_ONLY_REQUEST_TYPES


def without_worker_traceback(details: str) -> str:
    """Return the message without the traceback block a worker failure appends, which repeats the message."""
    return details.split(WORKER_TRACEBACK_MARKER, maxsplit=1)[0]


def across_wire(result: ResultPayload) -> ResultPayload:
    """Send a result through the converter the way a worker sends it back."""
    sent = json.loads(json.dumps(converters.engine.unstructure(result)))
    return converters.engine.structure(sent, type(result))
