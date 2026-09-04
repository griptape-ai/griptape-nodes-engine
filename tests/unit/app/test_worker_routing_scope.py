"""Tests for when RemoteHandler forwards versus answers locally.

RemoteHandler is the single chokepoint where a worker services a request whose authoritative state
lives on the orchestrator. What decides its behavior is one thing: whether the worker is inside a
node-execution scope. Inside, it forwards; outside (bootstrap, library load, LOAD_PROBE) it
delegates to the handler it replaced, so a node's ``__init__`` can still call ``add_parameter``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.app.worker_routing import (
    LOCAL_ONLY_REQUEST_TYPES,
    RemoteHandler,
    register_remote_handlers,
)
from griptape_nodes.retained_mode.events.base_events import (
    EventResultSuccess,
    RequestPayload,
    ResultPayloadSuccess,
)
from griptape_nodes.retained_mode.events.connection_events import (
    ListConnectionsForNodeRequest,
    ListConnectionsForNodeResultSuccess,
)
from griptape_nodes.retained_mode.managers.event_manager import EventManager

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.managers.event_manager import ResultContext


def _forwarded_sample() -> tuple[type, ...]:
    """A few representative types that forward (i.e. are not local-only)."""
    from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest
    from griptape_nodes.retained_mode.events.parameter_events import AddParameterToNodeRequest

    sample = (CreateNodeRequest, AddParameterToNodeRequest)
    assert not set(sample) & LOCAL_ONLY_REQUEST_TYPES
    return sample


@dataclass(kw_only=True)
class _ProbeRequest(RequestPayload):
    """Minimal request used to exercise RemoteHandler's routing decision."""

    marker: str


@dataclass(kw_only=True)
class _ProbeResult(ResultPayloadSuccess):
    """Success payload paired with _ProbeRequest."""

    seen_by: str


def _make_handler_with_fake_forward(event_manager: EventManager) -> RemoteHandler:
    async def original(_request: _ProbeRequest) -> _ProbeResult:
        return _ProbeResult(seen_by="local", result_details="local")

    async def fake_forward(
        request: RequestPayload,
        result_context: ResultContext,  # noqa: ARG001
    ) -> EventResultSuccess:
        return EventResultSuccess(
            request=request,
            result=_ProbeResult(seen_by="orchestrator", result_details="forwarded"),
        )

    event_manager.forward_to_orchestrator = fake_forward  # type: ignore[method-assign]
    return RemoteHandler(original=original, event_manager=event_manager)


class TestRemoteHandlerRouting:
    """Node-execution scope is the whole routing decision."""

    @pytest.mark.asyncio
    async def test_in_node_execution_forwards(self) -> None:
        event_manager = EventManager()
        handler = _make_handler_with_fake_forward(event_manager)

        with event_manager.worker_node_execution_scope():
            result = await handler(_ProbeRequest(marker="m1"))

        assert isinstance(result, _ProbeResult)
        assert result.seen_by == "orchestrator"

    @pytest.mark.asyncio
    async def test_out_of_node_execution_delegates_locally(self) -> None:
        """Bootstrap and library load must keep working against the replaced handler."""
        event_manager = EventManager()
        local_calls: list[_ProbeRequest] = []

        async def original(request: _ProbeRequest) -> _ProbeResult:
            local_calls.append(request)
            return _ProbeResult(seen_by="local", result_details="local")

        handler = RemoteHandler(original=original, event_manager=event_manager)

        result = await handler(_ProbeRequest(marker="m2"))

        assert isinstance(result, _ProbeResult)
        assert result.seen_by == "local"
        assert len(local_calls) == 1

    @pytest.mark.asyncio
    async def test_forwards_during_hydration_not_only_aprocess(self) -> None:
        """The gate is deliberately wider than ``aprocess``.

        ``worker_node_execution_scope`` is opened by ``_hydrate_and_run_node_inner`` around BOTH
        hydration and aprocess, so a request issued from ``before/after_value_set`` forwards too.
        No ``aprocess_scope()`` is entered here; only the refcount is active.
        """
        event_manager = EventManager()
        handler = _make_handler_with_fake_forward(event_manager)

        with event_manager.worker_node_execution_scope():
            result = await handler(_ProbeRequest(marker="hydrate"))

        assert isinstance(result, _ProbeResult)
        assert result.seen_by == "orchestrator"

    @pytest.mark.asyncio
    async def test_every_call_forwards_independently(self) -> None:
        event_manager = EventManager()
        forwarded: list[RequestPayload] = []

        async def original(_request: _ProbeRequest) -> _ProbeResult:
            return _ProbeResult(seen_by="local", result_details="local")

        async def fake_forward(
            request: RequestPayload,
            result_context: ResultContext,  # noqa: ARG001
        ) -> EventResultSuccess:
            forwarded.append(request)
            return EventResultSuccess(
                request=request,
                result=_ProbeResult(seen_by="orchestrator", result_details="forwarded"),
            )

        event_manager.forward_to_orchestrator = fake_forward  # type: ignore[method-assign]
        handler = RemoteHandler(original=original, event_manager=event_manager)

        with event_manager.worker_node_execution_scope():
            await handler(_ProbeRequest(marker="first"))
            await handler(_ProbeRequest(marker="second"))

        expected_forwards = 2
        assert len(forwarded) == expected_forwards

    @pytest.mark.asyncio
    async def test_real_forwarded_type_goes_through_the_swap_and_shim(self) -> None:
        """End-to-end: ``register_remote_handlers`` swaps a real forwarded type and it forwards.

        Locks in the ``LOCAL_ONLY_REQUEST_TYPES`` <-> ``RemoteHandler`` integration. This stays
        green only while ``ListConnectionsForNodeRequest`` is still NOT local-only -- connections
        are authoritative orchestrator state, so it must keep forwarding.
        """
        event_manager = EventManager()
        forwarded_calls: list[RequestPayload] = []

        async def fake_forward(
            request: RequestPayload,
            result_context: ResultContext,  # noqa: ARG001
        ) -> EventResultSuccess:
            forwarded_calls.append(request)
            return EventResultSuccess(
                request=request,
                result=ListConnectionsForNodeResultSuccess(
                    incoming_connections=[],
                    outgoing_connections=[],
                    result_details="forwarded",
                ),
            )

        event_manager.forward_to_orchestrator = fake_forward  # type: ignore[method-assign]

        # register_remote_handlers swaps whatever is registered, so the type this test
        # asserts on has to be registered first. Stub it plus a couple of representatives.
        for request_type in (ListConnectionsForNodeRequest, *_forwarded_sample()):

            async def stub(_request: RequestPayload) -> ResultPayloadSuccess:
                return ListConnectionsForNodeResultSuccess(
                    incoming_connections=[],
                    outgoing_connections=[],
                    result_details="local",
                )

            event_manager.assign_manager_to_request_type(request_type, stub)

        register_remote_handlers(event_manager)

        installed = event_manager.get_manager_for_request_type(ListConnectionsForNodeRequest)
        assert isinstance(installed, RemoteHandler)

        with event_manager.worker_node_execution_scope():
            result = await installed(ListConnectionsForNodeRequest(node_name="some-node"))

        assert isinstance(result, ListConnectionsForNodeResultSuccess)
        assert len(forwarded_calls) == 1
        assert isinstance(forwarded_calls[0], ListConnectionsForNodeRequest)
