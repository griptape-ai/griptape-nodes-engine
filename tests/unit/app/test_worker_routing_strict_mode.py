"""Tests for the worker-reach-into-orchestrator strict-mode tripwire.

RemoteHandler is the single chokepoint where a worker-side ``aprocess``
reaches into orchestrator-owned state. When it forwards a request back
to the orchestrator while a strict-mode scope is active, it records a
``worker-reach-into-orchestrator`` violation. Outside of node execution
(bootstrap, LOAD_PROBE) the tripwire does not fire because the
RemoteHandler delegates to the original handler before reaching the
violation path; outside any strict-mode scope, ``STRICT_MODE.report``
is a no-op and the handler still forwards without crashing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.app.worker_routing import (
    FORWARDED_REQUEST_TYPES,
    RemoteHandler,
    register_remote_handlers,
)
from griptape_nodes.common.strict_mode import (
    STRICT_MODE,
    StrictModeScopeKind,
    StrictModeSeverity,
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


@dataclass(kw_only=True)
class _ProbeRequest(RequestPayload):
    """Minimal request used to exercise RemoteHandler's tripwire."""

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


class TestWorkerReachIntoOrchestrator:
    """The tripwire records one violation per forwarded request while in scope."""

    @pytest.mark.asyncio
    async def test_in_scope_in_node_execution_records_violation(self) -> None:
        event_manager = EventManager()
        handler = _make_handler_with_fake_forward(event_manager)

        with (
            STRICT_MODE.open_scope(
                kind=StrictModeScopeKind.RUNTIME_EXECUTE,
                subject="node-1",
                library_name="libA",
                is_worker=True,
            ) as scope,
            event_manager.worker_node_execution_scope(),
        ):
            result = await handler(_ProbeRequest(marker="m1"))

        assert isinstance(result, _ProbeResult)
        assert result.seen_by == "orchestrator"
        assert len(scope.violations) == 1
        violation = scope.violations[0]
        assert violation.rule_id == "worker-reach-into-orchestrator"
        assert violation.severity is StrictModeSeverity.WARNING
        assert violation.subject == "node-1"
        assert violation.library_name == "libA"
        assert "_ProbeRequest" in violation.message

    @pytest.mark.asyncio
    async def test_in_scope_out_of_node_execution_does_not_record(self) -> None:
        event_manager = EventManager()
        local_calls: list[_ProbeRequest] = []

        async def original(request: _ProbeRequest) -> _ProbeResult:
            local_calls.append(request)
            return _ProbeResult(seen_by="local", result_details="local")

        handler = RemoteHandler(original=original, event_manager=event_manager)

        with STRICT_MODE.open_scope(
            kind=StrictModeScopeKind.LOAD_PROBE,
            subject="MyClass",
            library_name="libA",
            is_worker=True,
        ) as scope:
            result = await handler(_ProbeRequest(marker="m2"))

        assert isinstance(result, _ProbeResult)
        assert result.seen_by == "local"
        assert len(local_calls) == 1
        assert scope.violations == []

    @pytest.mark.asyncio
    async def test_no_scope_forwards_without_crashing(self) -> None:
        event_manager = EventManager()
        handler = _make_handler_with_fake_forward(event_manager)

        with event_manager.worker_node_execution_scope():
            result = await handler(_ProbeRequest(marker="m3"))

        assert isinstance(result, _ProbeResult)
        assert result.seen_by == "orchestrator"

    @pytest.mark.asyncio
    async def test_remediation_message_does_not_claim_from_aprocess(self) -> None:
        """Regression guard: remediation must not claim "from aprocess".

        The rule's gate is ``worker_node_execution_scope``, which covers
        both hydration and aprocess. The remediation must not claim "from
        aprocess" because the rule fires for both. See PR8 for the same
        factual-claim bug Collin flagged on parameter-mutation.
        """
        event_manager = EventManager()
        handler = _make_handler_with_fake_forward(event_manager)

        with (
            STRICT_MODE.open_scope(
                kind=StrictModeScopeKind.RUNTIME_EXECUTE,
                subject="node-r1",
                library_name="libA",
                is_worker=True,
            ) as scope,
            event_manager.worker_node_execution_scope(),
        ):
            await handler(_ProbeRequest(marker="m4"))

        assert len(scope.violations) == 1
        message = scope.violations[0].message
        assert "from aprocess" not in message
        assert "during node execution" in message

    @pytest.mark.asyncio
    async def test_fires_during_hydration_phase_not_just_aprocess(self) -> None:
        """The rule's gate is wider than aprocess.

        ``worker_node_execution_scope`` is opened by
        ``_hydrate_and_run_node_inner`` around BOTH hydration and
        aprocess. Forwarded requests issued during hydration (e.g.
        dynamic-pipeline nodes calling ListConnectionsForNodeRequest
        from before/after_value_set) must trip the rule. This pins
        down that the gate is intentionally wider than
        ``aprocess_scope()``.
        """
        event_manager = EventManager()
        handler = _make_handler_with_fake_forward(event_manager)

        # No aprocess_scope() entered. Only the worker_node_execution_scope
        # (refcount) is active. The rule must still fire.
        with (
            STRICT_MODE.open_scope(
                kind=StrictModeScopeKind.RUNTIME_EXECUTE,
                subject="node-h1",
                library_name="libA",
                is_worker=True,
            ) as scope,
            event_manager.worker_node_execution_scope(),
        ):
            await handler(_ProbeRequest(marker="hydrate"))

        assert len(scope.violations) == 1
        assert scope.violations[0].rule_id == "worker-reach-into-orchestrator"

    @pytest.mark.asyncio
    async def test_one_violation_per_call(self) -> None:
        """Two forwarded requests from the same scope produce two violations."""
        event_manager = EventManager()
        handler = _make_handler_with_fake_forward(event_manager)

        with (
            STRICT_MODE.open_scope(
                kind=StrictModeScopeKind.RUNTIME_EXECUTE,
                subject="node-c1",
                library_name="libA",
                is_worker=True,
            ) as scope,
            event_manager.worker_node_execution_scope(),
        ):
            await handler(_ProbeRequest(marker="first"))
            await handler(_ProbeRequest(marker="second"))

        expected_violations = 2
        assert len(scope.violations) == expected_violations

    @pytest.mark.asyncio
    async def test_forwarded_type_member_through_register_remote_handlers(self) -> None:
        """End-to-end: a real FORWARDED type goes through the swap and shim.

        ``register_remote_handlers`` swaps a real FORWARDED type's handler
        for a ``RemoteHandler`` shim, and dispatching that real type
        directly through the shim records the violation. Locks in the
        ``FORWARDED_REQUEST_TYPES`` <-> ``RemoteHandler`` integration: if
        a future change drops a type from ``FORWARDED_REQUEST_TYPES``,
        this test stays green only because
        ``ListConnectionsForNodeRequest`` is still a member.
        """
        event_manager = EventManager()

        # Capture what the RemoteHandler shim forwards. Skip going through
        # event_manager.handle_request because that path requires a configured
        # WebSocket loop; the rule itself fires inside RemoteHandler.__call__,
        # which is what we want to exercise.
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

        # register_remote_handlers requires every FORWARDED type to have an
        # original handler registered first. Stub them all with a trivial
        # local handler; the swap installs RemoteHandler in their place.
        for request_type in FORWARDED_REQUEST_TYPES:

            async def stub(_request: RequestPayload) -> ResultPayloadSuccess:
                return ListConnectionsForNodeResultSuccess(
                    incoming_connections=[],
                    outgoing_connections=[],
                    result_details="local",
                )

            event_manager.assign_manager_to_request_type(request_type, stub)

        register_remote_handlers(event_manager)

        # Pull the swapped handler out of the dispatch table and invoke it.
        # The shim is what production code reaches when the EventManager
        # dispatches ListConnectionsForNodeRequest on a worker.
        installed = event_manager.get_manager_for_request_type(ListConnectionsForNodeRequest)
        assert isinstance(installed, RemoteHandler)

        request = ListConnectionsForNodeRequest(node_name="some-node")
        with (
            STRICT_MODE.open_scope(
                kind=StrictModeScopeKind.RUNTIME_EXECUTE,
                subject="some-node",
                library_name="libA",
                is_worker=True,
            ) as scope,
            event_manager.worker_node_execution_scope(),
        ):
            result = await installed(request)

        assert isinstance(result, ListConnectionsForNodeResultSuccess)
        assert len(forwarded_calls) == 1
        assert isinstance(forwarded_calls[0], ListConnectionsForNodeRequest)
        assert len(scope.violations) == 1
        violation = scope.violations[0]
        assert violation.rule_id == "worker-reach-into-orchestrator"
        assert "ListConnectionsForNodeRequest" in violation.message
