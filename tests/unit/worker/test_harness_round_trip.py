"""Round-trip tests for the in-process worker harness.

Covers the two routing paths the harness is meant to exercise:

1. Orchestrator -> worker: a request dispatched via
   `harness.route_to_worker` reaches the worker-side handler and the
   result comes back shaped like the production `route_to_worker`
   wire response (`event_type` / `result_type` / `result` /
   `request_id`).
2. Worker -> orchestrator: a request whose worker-side handler is a
   ``RemoteHandler`` (installed via
   ``harness.install_remote_handler(...)`` for test purposes, mirroring
   production ``register_remote_handlers``) is forwarded to the
   orchestrator's handler while the worker is inside
   ``node_execution_scope``.

These are the invariants the harness owes its callers. Anything
finer-grained (serialization fidelity, concurrency) belongs in
dedicated tests.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from griptape_nodes.retained_mode.events.base_events import (
    EventRequest,
    EventResultSuccess,
    RequestPayload,
    ResultPayloadSuccess,
)
from tests.unit.worker.harness import (
    InProcessWorkerHarness,
    build_result_from_route_to_worker_dict,
)


@dataclass(kw_only=True)
class _EchoRequest(RequestPayload):
    """Worker-side probe request. Carries a value the handler echoes back."""

    value: str


@dataclass(kw_only=True)
class _EchoResult(ResultPayloadSuccess):
    """Success payload paired with _EchoRequest."""

    echoed: str


@dataclass(kw_only=True)
class _ForwardableRequest(RequestPayload):
    """Worker-side request that should forward to the orchestrator during node execution."""

    marker: str


@dataclass(kw_only=True)
class _ForwardableResult(ResultPayloadSuccess):
    """Success payload paired with _ForwardableRequest."""

    seen_by: str


class TestHarnessOrchestratorToWorker:
    """`route_to_worker` should reach the worker-side handler and return a dict-shaped result."""

    @pytest.mark.asyncio
    async def test_route_to_worker_reaches_worker_handler_and_returns_result_dict(self) -> None:
        harness = InProcessWorkerHarness()

        async def echo_handler(request: _EchoRequest) -> _EchoResult:
            return _EchoResult(echoed=request.value, result_details="ok")

        harness.worker.assign_manager_to_request_type(_EchoRequest, echo_handler)

        await harness.start()
        try:
            payload = await harness.route_to_worker(EventRequest(request=_EchoRequest(value="hello")))
        finally:
            await harness.stop()

        # Wire-shape mirror: production returns these keys from route_to_worker.
        assert payload["event_type"] == EventResultSuccess.__name__
        assert payload["result_type"] == _EchoResult.__name__
        assert payload["request_id"]  # non-empty; harness fills one in if caller omitted

        unpacked = build_result_from_route_to_worker_dict(
            payload,
            success_type=_EchoResult,
            failure_type=_EchoResult,  # unused on success path
        )
        assert isinstance(unpacked, _EchoResult)
        assert unpacked.echoed == "hello"

    @pytest.mark.asyncio
    async def test_route_to_worker_before_start_raises(self) -> None:
        harness = InProcessWorkerHarness()
        with pytest.raises(RuntimeError, match="Harness not started"):
            await harness.route_to_worker(EventRequest(request=_EchoRequest(value="x")))


class TestHarnessWorkerToOrchestratorForwarding:
    """Worker-side RemoteHandler should forward to the orchestrator during node execution."""

    @pytest.mark.asyncio
    async def test_forwardable_request_reaches_orchestrator_handler(self) -> None:
        harness = InProcessWorkerHarness()

        async def orchestrator_handler(request: _ForwardableRequest) -> _ForwardableResult:
            return _ForwardableResult(seen_by=f"orchestrator:{request.marker}", result_details="ok")

        # Orchestrator owns the real handler; worker side installs a RemoteHandler for it.
        harness.orchestrator.assign_manager_to_request_type(_ForwardableRequest, orchestrator_handler)
        harness.install_remote_handler(_ForwardableRequest)

        # Forwarding only activates inside node_execution_scope.
        with harness.worker.node_execution_scope():
            result_event = await harness.worker.ahandle_request(_ForwardableRequest(marker="m1"))

        assert result_event.succeeded()
        assert isinstance(result_event.result, _ForwardableResult)
        assert result_event.result.seen_by == "orchestrator:m1"
