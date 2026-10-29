"""Tests for worker-side RemoteHandler dispatch and register_remote_handlers.

These tests pin down the two invariants that replaced the old
``ForwardFromWorkerMixin`` machinery:

1. ``RemoteHandler`` forwards to the orchestrator only while the worker is
   inside a ``node_execution_scope``. Outside that scope it delegates
   to the ``original`` handler it displaced, which preserves bootstrap and
   library-load behaviour (e.g. nodes calling ``self.add_parameter(...)``
   from ``__init__``).
2. ``register_remote_handlers`` swaps the dispatch table entry for every
   registered type outside ``LOCAL_ONLY_REQUEST_TYPES``, preserving the "one handler per
   request type" invariant enforced by ``assign_manager_to_request_type``.
   Missing an original handler raises with a bootstrap-order diagnostic.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock, patch

import pytest

from griptape_nodes.app.worker_routing import (
    LOCAL_ONLY_REQUEST_TYPES,
    ActivateProjectRequest,
    DropAllLocalObjectsRequest,
    DropAllLocalObjectsResultFailure,
    DropAllLocalObjectsResultSuccess,
    DropLocalObjectsRequest,
    DropLocalObjectsResultFailure,
    DropLocalObjectsResultSuccess,
    ReloadAllLibrariesRequest,
    RemoteHandler,
    _handle_drop_all_local_objects,
    _handle_drop_local_objects,
    register_broadcast_handlers,
    register_remote_handlers,
)
from griptape_nodes.retained_mode.events.base_events import (
    EventResultSuccess,
    RequestPayload,
    ResultPayloadSuccess,
)
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest
from griptape_nodes.retained_mode.events.parameter_events import (
    AddParameterToNodeRequest,
    SetParameterValueRequest,
)
from griptape_nodes.retained_mode.events.project_events import (
    AttemptMapAbsolutePathToProjectRequest,
    GetPathForMacroRequest,
    GetSituationRequest,
    SetCurrentProjectRequest,
    SetCurrentProjectResultSuccess,
)
from griptape_nodes.retained_mode.events.resource_events import GetExecutionDeviceRequest
from griptape_nodes.retained_mode.managers.event_manager import EventManager

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.managers.event_manager import ResultContext
    from griptape_nodes.retained_mode.managers.project_manager import ProjectManager


@dataclass(kw_only=True)
class _ProbeRequest(RequestPayload):
    """Minimal request used to exercise RemoteHandler's scope gate."""

    marker: str


@dataclass(kw_only=True)
class _ProbeResult(ResultPayloadSuccess):
    """Success payload paired with _ProbeRequest."""

    seen_by: str


class TestRemoteHandlerScopeGate:
    """RemoteHandler must forward in-scope and delegate out-of-scope."""

    @pytest.mark.asyncio
    async def test_out_of_scope_delegates_to_original(self) -> None:
        event_manager = EventManager()
        call_count = {"n": 0}

        async def original(request: _ProbeRequest) -> _ProbeResult:
            call_count["n"] += 1
            return _ProbeResult(seen_by=f"local:{request.marker}", result_details="ok")

        handler = RemoteHandler(original=original, event_manager=event_manager)

        assert not event_manager.in_node_execution()
        result = await handler(_ProbeRequest(marker="m1"))

        assert isinstance(result, _ProbeResult)
        assert result.seen_by == "local:m1"
        assert call_count["n"] == 1

    @pytest.mark.asyncio
    async def test_in_scope_forwards_to_orchestrator(self) -> None:
        event_manager = EventManager()
        original_called = {"n": 0}

        async def original(_request: _ProbeRequest) -> _ProbeResult:
            original_called["n"] += 1
            return _ProbeResult(seen_by="local", result_details="local")

        forwarded_with: dict[str, object] = {}

        async def fake_forward(
            request: RequestPayload,
            result_context: ResultContext,  # noqa: ARG001
        ) -> EventResultSuccess:
            forwarded_with["request"] = request
            return EventResultSuccess(
                request=request,
                result=_ProbeResult(seen_by="orchestrator", result_details="forwarded"),
            )

        event_manager.forward_to_orchestrator = fake_forward  # type: ignore[method-assign]

        handler = RemoteHandler(original=original, event_manager=event_manager)

        with event_manager.node_execution_scope():
            result = await handler(_ProbeRequest(marker="m2"))

        assert isinstance(result, _ProbeResult)
        assert result.seen_by == "orchestrator"
        assert original_called["n"] == 0
        assert isinstance(forwarded_with["request"], _ProbeRequest)

    @pytest.mark.asyncio
    async def test_supports_sync_original_out_of_scope(self) -> None:
        """The original displaced handler may be sync; call_function normalises it."""
        event_manager = EventManager()

        def sync_original(request: _ProbeRequest) -> _ProbeResult:
            return _ProbeResult(seen_by=f"sync:{request.marker}", result_details="ok")

        handler = RemoteHandler(original=sync_original, event_manager=event_manager)

        result = await handler(_ProbeRequest(marker="s1"))

        assert isinstance(result, _ProbeResult)
        assert result.seen_by == "sync:s1"


@dataclass(kw_only=True)
class _StubResult(ResultPayloadSuccess):
    """Concrete success payload used to populate stub handlers during swap tests."""


class TestInstallRemoteHandlersSwap:
    """register_remote_handlers forwards everything except the local-only exclusions."""

    def test_swap_replaces_every_registered_handler(self) -> None:
        event_manager = EventManager()

        originals: dict[type[RequestPayload], Any] = {}
        for request_type in (CreateNodeRequest, AddParameterToNodeRequest, SetParameterValueRequest):

            async def original(_request: RequestPayload) -> _StubResult:
                return _StubResult(result_details="ok")

            originals[request_type] = original
            event_manager.assign_manager_to_request_type(request_type, original)

        register_remote_handlers(event_manager)

        for request_type, original in originals.items():
            swapped = event_manager.get_manager_for_request_type(request_type)
            assert isinstance(swapped, RemoteHandler), (
                f"Expected RemoteHandler for {request_type.__name__}, got {type(swapped).__name__}"
            )
            assert swapped.original is original
            assert swapped.event_manager is event_manager

    def test_local_only_types_are_left_alone(self) -> None:
        """Excluded types must keep their own handler.

        ExecuteNodeRequest is the sharpest case: forwarding a worker's own execution request
        would send it back to the orchestrator, which would route it straight here again.
        """
        event_manager = EventManager()
        locals_registered: dict[type[RequestPayload], Any] = {}
        for request_type in LOCAL_ONLY_REQUEST_TYPES:

            async def original(_request: RequestPayload) -> _StubResult:
                return _StubResult(result_details="local")

            locals_registered[request_type] = original
            event_manager.assign_manager_to_request_type(request_type, original)

        register_remote_handlers(event_manager)

        for request_type, original in locals_registered.items():
            assert event_manager.get_manager_for_request_type(request_type) is original, (
                f"{request_type.__name__} must not be forwarded"
            )

    def test_a_project_activation_and_the_reload_it_causes_are_both_local(self) -> None:
        """The orchestrator decides when libraries reload; a worker must not ask it to.

        Adopting a project reloads the worker's libraries, and that dispatch goes through the bus.
        Forwarded, it reaches the orchestrator's pre-reload callback -- reset_workers -- which
        terminates the worker that asked, mid-node. The pair has to stay local together: making the
        activation local while its consequence forwards is the same defect with an extra hop.
        """
        assert ActivateProjectRequest in LOCAL_ONLY_REQUEST_TYPES
        assert ReloadAllLibrariesRequest in LOCAL_ONLY_REQUEST_TYPES

    def test_every_broadcast_a_worker_answers_itself_is_local(self) -> None:
        """Whatever `register_broadcast_handlers` installs is addressed to THIS worker, so none of it forwards.

        Derived rather than listed by name, because the cost of forgetting is invisible: the wrapping pass
        skips a type that has no handler yet, and broadcast handlers are registered after it runs. So a
        missing entry works by call order alone, and swapping those two calls -- or registering one of these
        earlier -- would start forwarding them. For the drop requests that means the orchestrator answering
        success having freed nothing, while the worker keeps a pipeline that may be gigabytes.
        """
        probe = EventManager()
        before = set(probe.registered_request_types())
        register_broadcast_handlers(
            probe,
            config_manager=MagicMock(),
            secrets_manager=MagicMock(),
            project_manager=MagicMock(),
        )
        installed = set(probe.registered_request_types()) - before

        assert installed, "expected register_broadcast_handlers to install something"
        forwarded = sorted(t.__name__ for t in installed - LOCAL_ONLY_REQUEST_TYPES)
        assert forwarded == [], f"broadcast handlers that would be forwarded to the orchestrator: {forwarded}"

    def test_per_file_project_reads_stay_local(self) -> None:
        """Three project-template reads on the per-saved-file path must not forward.

        Each is a pure read of a project the worker has already adopted, and each sits on the path
        taken for every file written, so forwarding them charged a round trip per file. The
        write-side one is easy to miss because it runs after the write rather than before it.
        """
        for request_type in (GetSituationRequest, GetPathForMacroRequest, AttemptMapAbsolutePathToProjectRequest):
            assert request_type in LOCAL_ONLY_REQUEST_TYPES, (
                f"{request_type.__name__} must be answered by the worker, not forwarded"
            )

    def test_the_device_read_is_answered_by_the_worker(self) -> None:
        """`execution_device` must describe the machine that will run the model.

        Forwarding asked the orchestrator about its own hardware. Today both processes share a
        machine so the answers coincide, which is exactly why this needs pinning: the moment a
        venue runs somewhere else, a forwarded answer is silently the wrong machine's.
        """
        assert GetExecutionDeviceRequest in LOCAL_ONLY_REQUEST_TYPES

    def test_unregistered_types_are_skipped_without_error(self) -> None:
        """Nothing to swap is not a bootstrap failure: only registered types are touched.

        The allowlist version raised when a listed type had no owner, because the list could
        name types nobody had registered. Iterating what IS registered removes that failure
        mode entirely.
        """
        event_manager = EventManager()

        async def original(_request: RequestPayload) -> _StubResult:
            return _StubResult(result_details="ok")

        event_manager.assign_manager_to_request_type(CreateNodeRequest, original)

        register_remote_handlers(event_manager)

        assert isinstance(event_manager.get_manager_for_request_type(CreateNodeRequest), RemoteHandler)

    def test_post_install_out_of_scope_still_runs_original(self) -> None:
        """Bootstrap-path regression guard: construction-time calls must stay local.

        A node's ``__init__`` issues an ``AddParameterToNodeRequest`` outside
        ``node_execution_scope``, including when the worker materializes a transient
        node for a dispatch. The RemoteHandler installed for that type must delegate
        to the original handler rather than trying to forward.
        """
        event_manager = EventManager()

        local_calls: list[AddParameterToNodeRequest] = []

        async def local_add_parameter(request: AddParameterToNodeRequest) -> _StubResult:
            local_calls.append(request)
            return _StubResult(result_details="local")

        event_manager.assign_manager_to_request_type(AddParameterToNodeRequest, local_add_parameter)

        register_remote_handlers(event_manager)

        assert not event_manager.in_node_execution()

        result_event = event_manager.handle_request(AddParameterToNodeRequest(node_name="n", parameter_name="p"))

        assert result_event.result.succeeded()
        assert len(local_calls) == 1


class TestDropAllLocalObjectsHandler:
    """The worker half of workflow teardown: release what this process is holding.

    This is the process with the pipeline the orchestrator has no torch to hold, so what this handler
    accepts decides whether gigabytes stay resident.
    """

    @pytest.mark.asyncio
    async def test_it_accepts_mid_execution_but_the_hook_waits(self, engine: Engine) -> None:
        """Teardown arriving mid-render takes the entries now and frees them once the node is done.

        Freeing under a running forward pass is what must not happen; forgetting where the object is cannot
        hurt a node that already holds it. Declining both would report success for work nothing re-issues.
        """
        released: list[str] = []
        key = engine.resource_manager.put_local_object(
            object(), owner="Lib A", source="N", key="cfg", on_drop=lambda _v: released.append("gone")
        )

        with engine.event_manager.node_execution_scope():
            result = await _handle_drop_all_local_objects(
                DropAllLocalObjectsRequest(), event_manager=engine.event_manager
            )
            assert isinstance(result, DropAllLocalObjectsResultSuccess)
            assert engine.resource_manager.entry_for(key) is None
            assert released == []

        assert engine.resource_manager.drain_deferred_releases() == 1
        assert released == ["gone"]

    @pytest.mark.asyncio
    async def test_releases_off_the_event_loop(self, engine: Engine) -> None:
        """A worker whose loop is blocked past the heartbeat timeout is evicted mid-load."""
        release_threads: list[int] = []
        engine.resource_manager.put_local_object(
            object(),
            owner="Lib A",
            source="N",
            key="cfg",
            on_drop=lambda _value: release_threads.append(threading.get_ident()),
        )

        result = await _handle_drop_all_local_objects(DropAllLocalObjectsRequest(), event_manager=engine.event_manager)

        assert isinstance(result, DropAllLocalObjectsResultSuccess)
        assert release_threads
        assert release_threads[0] != threading.get_ident()

    @pytest.mark.asyncio
    async def test_reports_failure_rather_than_raising_at_the_transport(self, engine: Engine) -> None:
        with patch.object(engine.resource_manager, "drop_all_local_objects", side_effect=RuntimeError("boom")):
            result = await _handle_drop_all_local_objects(
                DropAllLocalObjectsRequest(), event_manager=engine.event_manager
            )

        assert isinstance(result, DropAllLocalObjectsResultFailure)


class TestDropLocalObjectsHandler:
    """The worker half of a handle being replaced or its node deleted."""

    @pytest.mark.asyncio
    async def test_releases_the_named_objects(self, engine: Engine) -> None:
        released: list[str] = []
        keys = [
            engine.resource_manager.put_local_object(
                object(),
                owner="Lib A",
                source="N",
                key=label,
                slot=label,
                on_drop=lambda _v, label=label: released.append(label),
            )
            for label in ("one", "two")
        ]

        result = await _handle_drop_local_objects(
            DropLocalObjectsRequest(keys=keys), event_manager=engine.event_manager
        )

        assert isinstance(result, DropLocalObjectsResultSuccess)
        assert sorted(released) == ["one", "two"]

    @pytest.mark.asyncio
    async def test_a_key_this_worker_never_held_is_not_a_failure(self, engine: Engine) -> None:
        """The orchestrator broadcasts to every worker, and only one of them holds any given object."""
        result = await _handle_drop_local_objects(
            DropLocalObjectsRequest(keys=["Lib A:not-here"]), event_manager=engine.event_manager
        )

        assert isinstance(result, DropLocalObjectsResultSuccess)

    @pytest.mark.asyncio
    async def test_it_accepts_mid_execution_but_the_hook_waits(self, engine: Engine) -> None:
        """Accepted rather than declined, yet the object is not freed under a running node.

        The exemption used to rest on the keys having been replaced on the orchestrator before the message
        was sent, so nothing running here could hold them. That is true of displacement, which never travels
        this way -- it happens inside the worker. What arrives is node deletion, and there a consumer can be
        mid-forward-pass on the very object being destroyed.
        """
        released: list[str] = []
        key = engine.resource_manager.put_local_object(
            object(), owner="Lib A", source="N", key="cfg", slot="out", on_drop=lambda _v: released.append("gone")
        )

        with engine.event_manager.node_execution_scope():
            result = await _handle_drop_local_objects(
                DropLocalObjectsRequest(keys=[key]), event_manager=engine.event_manager
            )
            assert isinstance(result, DropLocalObjectsResultSuccess)
            # Entry gone from the map, so nothing resolves it again -- but the hook has not run.
            assert engine.resource_manager.entry_for(key) is None
            assert released == []

        assert engine.resource_manager.drain_deferred_releases() == 1
        assert released == ["gone"]

    @pytest.mark.asyncio
    async def test_releases_off_the_event_loop(self, engine: Engine) -> None:
        """A worker whose loop is blocked past the heartbeat timeout is evicted mid-load."""
        release_threads: list[int] = []
        key = engine.resource_manager.put_local_object(
            object(),
            owner="Lib A",
            source="N",
            key="cfg",
            slot="out",
            on_drop=lambda _v: release_threads.append(threading.get_ident()),
        )

        await _handle_drop_local_objects(DropLocalObjectsRequest(keys=[key]), event_manager=engine.event_manager)

        assert release_threads
        assert release_threads[0] != threading.get_ident()

    @pytest.mark.asyncio
    async def test_reports_failure_rather_than_raising_at_the_transport(self, engine: Engine) -> None:
        with patch.object(engine.resource_manager, "drop_parked_local_object", side_effect=RuntimeError("boom")):
            result = await _handle_drop_local_objects(
                DropLocalObjectsRequest(keys=["Lib A:cfg"]), event_manager=engine.event_manager
            )

        assert isinstance(result, DropLocalObjectsResultFailure)

    @pytest.mark.asyncio
    async def test_a_library_named_entry_is_refused(self, engine: Engine) -> None:
        """The orchestrator broadcasts keys it cannot check, so the provenance rule is enforced here."""
        released: list[str] = []
        key = engine.resource_manager.put_local_object(
            object(), owner="Lib A", source="N", key="sd-xl-1.0#a1b2c3d4", on_drop=lambda _v: released.append("gone")
        )

        result = await _handle_drop_local_objects(
            DropLocalObjectsRequest(keys=[key]), event_manager=engine.event_manager
        )

        assert isinstance(result, DropLocalObjectsResultSuccess)
        assert released == []
        assert engine.resource_manager.get_local_object(key, owner="Lib A") is not None


@pytest.fixture
def engine_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """A running loop on its own thread, standing in for the worker's engine loop beside the test's own."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name="engine-loop", daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join()
    loop.close()


def _bind_queue_to(event_manager: EventManager, loop: asyncio.AbstractEventLoop) -> None:
    """Initialize the event queue on `loop`, which is what makes it the engine loop."""

    async def initialize() -> None:
        event_manager.initialize_queue()

    asyncio.run_coroutine_threadsafe(initialize(), loop).result()


def _route_probe_through_a_remote_handler(event_manager: EventManager) -> None:
    """Install `_ProbeRequest` the way a worker installs every forwarded type: behind a RemoteHandler."""

    async def original(request: _ProbeRequest) -> _ProbeResult:
        return _ProbeResult(seen_by=f"local:{request.marker}", result_details="ok")

    event_manager.assign_manager_to_request_type(
        _ProbeRequest, RemoteHandler(original=original, event_manager=event_manager)
    )


class _ProjectManagerThatRequestsSynchronously:
    """Adopts by sending a request through sync `handle_request`, as workflow registration does."""

    def __init__(self, event_manager: EventManager) -> None:
        self._event_manager = event_manager
        self.adopted_on_thread: str | None = None
        self.probe_succeeded: bool | None = None

    def is_stale_adoption(self, project_id: str, generation: int) -> bool:  # noqa: ARG002
        return False

    async def ensure_project_loaded(self, project_id: str) -> bool:  # noqa: ARG002
        return True

    async def on_set_current_project_request(self, request: SetCurrentProjectRequest) -> SetCurrentProjectResultSuccess:  # noqa: ARG002
        self.adopted_on_thread = threading.current_thread().name
        probe_result = self._event_manager.handle_request(_ProbeRequest(marker="sync", broadcast_result=False))
        self.probe_succeeded = probe_result.succeeded()
        return SetCurrentProjectResultSuccess(result_details="ok")

    def record_adopted_generation(self, generation: int) -> None:
        pass


def _install_adoption(
    event_manager: EventManager, project_manager: _ProjectManagerThatRequestsSynchronously
) -> asyncio.Event:
    return register_broadcast_handlers(
        event_manager,
        config_manager=MagicMock(),
        secrets_manager=MagicMock(),
        project_manager=cast("ProjectManager", project_manager),
    )


def _configure_forwarding_on_this_loop(event_manager: EventManager) -> None:
    """Make the calling loop the worker's websocket loop, the one that receives ActivateProjectRequest."""
    event_manager.configure_worker_forwarding(
        request_client=MagicMock(),
        orchestrator_request_topic="orchestrator",
        worker_response_topic="worker",
        websocket_event_loop=asyncio.get_running_loop(),
    )


class TestActivateProjectRunsOnTheEngineLoop:
    """Adoption arrives on the websocket loop, which answers heartbeats, so it must not run there."""

    @pytest.mark.asyncio
    @pytest.mark.timeout(10, method="thread")
    async def test_an_adoption_that_requests_synchronously_completes(
        self, engine: Engine, engine_loop: asyncio.AbstractEventLoop
    ) -> None:
        """Run on the websocket loop, this adoption blocked that loop waiting on itself (#5946)."""
        event_manager = engine.event_manager
        _bind_queue_to(event_manager, engine_loop)
        _configure_forwarding_on_this_loop(event_manager)
        _route_probe_through_a_remote_handler(event_manager)
        project_manager = _ProjectManagerThatRequestsSynchronously(event_manager)
        worker_settled = _install_adoption(event_manager, project_manager)
        handler = event_manager.get_manager_for_request_type(ActivateProjectRequest)
        assert handler is not None

        result = await handler(ActivateProjectRequest(project_id="next-project", generation=1))

        assert result.succeeded()
        assert project_manager.adopted_on_thread == "engine-loop"
        assert project_manager.probe_succeeded is True
        assert worker_settled.is_set()

    @pytest.mark.asyncio
    async def test_without_an_engine_loop_adoption_is_refused(self, engine: Engine) -> None:
        event_manager = engine.event_manager
        assert event_manager.event_loop is None
        project_manager = _ProjectManagerThatRequestsSynchronously(event_manager)
        worker_settled = _install_adoption(event_manager, project_manager)
        handler = event_manager.get_manager_for_request_type(ActivateProjectRequest)
        assert handler is not None

        result = await handler(ActivateProjectRequest(project_id="next-project", generation=1))

        assert result.failed()
        assert project_manager.adopted_on_thread is None
        assert not worker_settled.is_set()


class TestSyncDispatchOnTheWebsocketLoop:
    """Blocking on a RemoteHandler from the loop it is dispatched to can never return."""

    @pytest.mark.asyncio
    @pytest.mark.timeout(10, method="thread")
    async def test_it_fails_instead_of_hanging(self, engine: Engine) -> None:
        event_manager = engine.event_manager
        _configure_forwarding_on_this_loop(event_manager)
        _route_probe_through_a_remote_handler(event_manager)

        result_event = event_manager.handle_request(_ProbeRequest(marker="sync", broadcast_result=False))

        assert not result_event.succeeded()
        assert "synchronously on the websocket event loop" in str(result_event.result.result_details)
