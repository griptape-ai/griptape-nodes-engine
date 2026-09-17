"""Tests for worker-side RemoteHandler dispatch and register_remote_handlers.

These tests pin down the two invariants that replaced the old
``ForwardFromWorkerMixin`` machinery:

1. ``RemoteHandler`` forwards to the orchestrator only while the worker is
   inside a ``worker_node_execution_scope``. Outside that scope it delegates
   to the ``original`` handler it displaced, which preserves bootstrap and
   library-load behaviour (e.g. nodes calling ``self.add_parameter(...)``
   during LOAD_PROBE).
2. ``register_remote_handlers`` swaps the dispatch table entry for every
   entry in ``FORWARDED_REQUEST_TYPES``, preserving the "one handler per
   request type" invariant enforced by ``assign_manager_to_request_type``.
   Missing an original handler raises with a bootstrap-order diagnostic.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from griptape_nodes.app.worker_routing import (
    FORWARDED_REQUEST_TYPES,
    DropAllLocalObjectsRequest,
    DropAllLocalObjectsResultFailure,
    DropAllLocalObjectsResultSuccess,
    RemoteHandler,
    _handle_drop_all_local_objects,
    register_remote_handlers,
)
from griptape_nodes.retained_mode.events.base_events import (
    EventResultSuccess,
    RequestPayload,
    ResultPayloadSuccess,
)
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest
from griptape_nodes.retained_mode.events.parameter_events import AddParameterToNodeRequest
from griptape_nodes.retained_mode.managers.event_manager import EventManager

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.managers.event_manager import ResultContext


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

        with event_manager.worker_node_execution_scope():
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
    """register_remote_handlers must displace each FORWARDED type's handler."""

    def test_swap_replaces_each_forwarded_handler(self) -> None:
        event_manager = EventManager()

        # Give every forwarded type a uniquely-identifiable async original.
        originals: dict[type[RequestPayload], Any] = {}
        for request_type in FORWARDED_REQUEST_TYPES:

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

    def test_missing_original_raises_bootstrap_error(self) -> None:
        event_manager = EventManager()

        # Register everything except CreateNodeRequest so register_remote_handlers
        # trips on the first missing original it encounters.
        for request_type in FORWARDED_REQUEST_TYPES:
            if request_type is CreateNodeRequest:
                continue

            async def original(_request: RequestPayload) -> _StubResult:
                return _StubResult(result_details="ok")

            event_manager.assign_manager_to_request_type(request_type, original)

        with pytest.raises(RuntimeError, match="CreateNodeRequest"):
            register_remote_handlers(event_manager)

    def test_post_install_out_of_scope_still_runs_original(self) -> None:
        """Bootstrap-path regression guard: LOAD_PROBE-style calls must stay local.

        A node's ``__init__`` running under LOAD_PROBE will issue an
        ``AddParameterToNodeRequest`` outside ``worker_node_execution_scope``.
        The RemoteHandler installed for that type must delegate to the
        original handler rather than trying to forward.
        """
        event_manager = EventManager()

        local_calls: list[AddParameterToNodeRequest] = []

        async def local_add_parameter(request: AddParameterToNodeRequest) -> _StubResult:
            local_calls.append(request)
            return _StubResult(result_details="local")

        # Register the single type we assert on; register_remote_handlers needs
        # every forwarded type registered, so fill in trivial stubs for the rest.
        event_manager.assign_manager_to_request_type(AddParameterToNodeRequest, local_add_parameter)
        for request_type in FORWARDED_REQUEST_TYPES:
            if request_type is AddParameterToNodeRequest:
                continue

            async def stub(_request: RequestPayload) -> _StubResult:
                return _StubResult(result_details="ok")

            event_manager.assign_manager_to_request_type(request_type, stub)

        register_remote_handlers(event_manager)

        assert not event_manager.in_node_execution()

        result_event = event_manager.handle_request(AddParameterToNodeRequest(node_name="n", parameter_name="p"))

        assert result_event.result.succeeded()
        assert len(local_calls) == 1


class TestDropAllLocalObjectsHandler:
    """The worker half of workflow teardown: release what this process is holding.

    This is the process with the pipeline the orchestrator has no torch to hold, so the branch that
    declines to release decides whether gigabytes stay resident.
    """

    @pytest.mark.asyncio
    async def test_declines_while_executing_a_node(self, engine: Engine) -> None:
        """Releasing mid-execution would free the pipeline under a forward pass already running."""
        held = object()
        key = engine.resource_manager.put_local_object(held, owner="Lib A", source="N")

        with engine.event_manager.worker_node_execution_scope():
            result = await _handle_drop_all_local_objects(
                DropAllLocalObjectsRequest(), event_manager=engine.event_manager
            )

        assert isinstance(result, DropAllLocalObjectsResultSuccess)
        assert engine.resource_manager.get_local_object(key, owner="Lib A") is held

    @pytest.mark.asyncio
    async def test_releases_off_the_event_loop(self, engine: Engine) -> None:
        """A worker whose loop is blocked past the heartbeat timeout is evicted mid-load."""
        release_threads: list[int] = []
        engine.resource_manager.put_local_object(
            object(),
            owner="Lib A",
            source="N",
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
