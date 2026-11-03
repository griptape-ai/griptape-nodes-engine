"""Tests for when RemoteHandler forwards versus answers locally.

RemoteHandler is the single chokepoint where a worker services a request whose authoritative state
lives on the orchestrator. What decides its behavior is one thing: whether the worker is inside a
node-execution scope. Inside, it forwards; outside (bootstrap, library load, node construction) it
delegates to the handler it replaced, so a node's ``__init__`` can still call ``add_parameter``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
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
    DeleteConnectionRequest,
    DeleteConnectionResultSuccess,
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

        with event_manager.node_execution_scope():
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
    async def test_forwards_whenever_the_scope_is_open_not_only_inside_aprocess(self) -> None:
        """The gate is the refcount alone: no ``aprocess_scope()`` is entered here."""
        event_manager = EventManager()
        handler = _make_handler_with_fake_forward(event_manager)

        with event_manager.node_execution_scope():
            result = await handler(_ProbeRequest(marker="scope only"))

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

        with event_manager.node_execution_scope():
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

        with event_manager.node_execution_scope():
            result = await installed(ListConnectionsForNodeRequest(node_name="some-node"))

        assert isinstance(result, ListConnectionsForNodeResultSuccess)
        assert len(forwarded_calls) == 1
        assert isinstance(forwarded_calls[0], ListConnectionsForNodeRequest)


REBUILT = "Load Component"


def _drop_own_wire() -> DeleteConnectionRequest:
    """What Load Pipeline Component sends from its value hook when its component changes."""
    return DeleteConnectionRequest(
        source_node_name=REBUILT,
        source_parameter_name="component_output",
        target_node_name="Pipeline Builder",
        target_parameter_name="component_vae",
    )


@dataclass
class _Answers:
    """Who answered each request a RemoteHandler was registered for: "local" or "orchestrator"."""

    seen: list[str] = field(default_factory=list)


def _register_forwarded_delete(event_manager: EventManager, answers: _Answers) -> None:
    """Register DeleteConnectionRequest behind a RemoteHandler, as a worker does, recording who answers it."""

    async def original(_request: DeleteConnectionRequest) -> ResultPayloadSuccess:
        answers.seen.append("local")
        return DeleteConnectionResultSuccess(result_details="local")

    async def fake_forward(
        request: RequestPayload,
        result_context: ResultContext,  # noqa: ARG001
    ) -> EventResultSuccess:
        answers.seen.append("orchestrator")
        return EventResultSuccess(request=request, result=DeleteConnectionResultSuccess(result_details="forwarded"))

    event_manager.forward_to_orchestrator = fake_forward  # type: ignore[method-assign]
    event_manager.assign_manager_to_request_type(DeleteConnectionRequest, original)
    register_remote_handlers(event_manager)
    assert isinstance(event_manager.get_manager_for_request_type(DeleteConnectionRequest), RemoteHandler)


async def _another_chain_running_a_node(event_manager: EventManager, release: asyncio.Event) -> None:
    with event_manager.node_execution_scope():
        await release.wait()


class TestRequestsWhileRebuildingACopy:
    """A worker builds and discards its copy of a node in its own graph, so a rebuild's requests are answered here.

    The node-execution refcount is process-wide, and another node can be running in the worker at the same time.
    Its requests still go to the orchestrator; only the chain doing the rebuild is answered locally.
    """

    @pytest.mark.asyncio
    async def test_ahandle_request_is_answered_here_while_another_chain_runs_a_node(self) -> None:
        event_manager = EventManager()
        answers = _Answers()
        _register_forwarded_delete(event_manager, answers)
        release = asyncio.Event()
        running = asyncio.create_task(_another_chain_running_a_node(event_manager, release))
        await asyncio.sleep(0)
        assert event_manager.in_node_execution()

        try:
            with event_manager.rebuild_scope(REBUILT):
                result = await event_manager.ahandle_request(_drop_own_wire())
        finally:
            release.set()
            await running

        assert isinstance(result.result, DeleteConnectionResultSuccess)
        assert answers.seen == ["local"]

    @pytest.mark.asyncio
    async def test_handle_request_is_answered_here_while_another_chain_runs_a_node(self) -> None:
        """Sync sends from library code during a load must not hop to the forwarding loop at all."""
        event_manager = EventManager()
        answers = _Answers()
        _register_forwarded_delete(event_manager, answers)
        release = asyncio.Event()
        running = asyncio.create_task(_another_chain_running_a_node(event_manager, release))
        await asyncio.sleep(0)
        assert event_manager.in_node_execution()

        try:
            with event_manager.rebuild_scope(REBUILT):
                result = event_manager.handle_request(_drop_own_wire())
        finally:
            release.set()
            await running

        assert isinstance(result.result, DeleteConnectionResultSuccess)
        assert answers.seen == ["local"]

    @pytest.mark.asyncio
    async def test_the_running_chain_still_forwards_while_another_rebuilds(self) -> None:
        event_manager = EventManager()
        answers = _Answers()
        _register_forwarded_delete(event_manager, answers)
        rebuilding = asyncio.Event()
        finished_rebuilding = asyncio.Event()

        async def rebuild() -> None:
            with event_manager.rebuild_scope(REBUILT):
                rebuilding.set()
                await finished_rebuilding.wait()

        rebuild_task = asyncio.create_task(rebuild())
        await rebuilding.wait()
        try:
            with event_manager.node_execution_scope():
                result = await event_manager.ahandle_request(_drop_own_wire())
        finally:
            finished_rebuilding.set()
            await rebuild_task

        assert isinstance(result.result, DeleteConnectionResultSuccess)
        assert answers.seen == ["orchestrator"]

    @pytest.mark.asyncio
    async def test_an_edit_forwards_once_the_rebuild_is_over(self) -> None:
        """Once the copy is built, an edit is the running node's own and belongs to the orchestrator."""
        event_manager = EventManager()
        answers = _Answers()
        _register_forwarded_delete(event_manager, answers)

        with event_manager.node_execution_scope():
            with event_manager.rebuild_scope(REBUILT):
                pass
            await event_manager.ahandle_request(_drop_own_wire())

        assert answers.seen == ["orchestrator"]
