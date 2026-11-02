"""Worker-side attribution is answered by the orchestrator, not locally.

A worker's project manager is a replica populated by broadcast, so it can serve a stale
chain; the orchestrator holds the authoritative one. So `GetAttributionContextRequest` stays
out of `LOCAL_ONLY_REQUEST_TYPES` and a worker-side `RemoteHandler` forwards it -- but only
inside a `node_execution_scope`, which is the only time a node spends credits.
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from griptape_nodes.api_client.request_client import RequestClient
from griptape_nodes.app.worker_routing import (
    LOCAL_ONLY_REQUEST_TYPES,
    RemoteHandler,
    register_remote_handlers,
)
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events import converters
from griptape_nodes.retained_mode.events.base_events import EventRequest, EventResultFailure, EventResultSuccess
from griptape_nodes.retained_mode.events.budget_events import (
    BudgetAccessRequest,
    BudgetAccessResultFailure,
    BudgetAccessResultSuccess,
    GetAttributionContextRequest,
    GetAttributionContextResultSuccess,
    ReportUsageRequest,
)
from griptape_nodes.retained_mode.managers.event_manager import EventManager, ResultContext
from griptape_nodes.retained_mode.managers.node_manager import NodeManager
from griptape_nodes.utils.budget_refusal import BUDGET_HALT_PREFIX, BudgetExceededError, describe, refusal_from_check
from tests.unit.worker.harness import InProcessWorkerHarness


def _orchestrator_answer() -> GetAttributionContextResultSuccess:
    """A result only the orchestrator could have produced, so its origin is unambiguous."""
    return GetAttributionContextResultSuccess(
        header_value="from-the-orchestrator",
        project_chain=["orchestrator-project"],
        result_details="ok",
    )


class TestAttributionIsForwardedFromWorkers:
    """Inside node execution the orchestrator answers; outside it the worker does."""

    @pytest.mark.asyncio
    async def test_attribution_forwards_from_a_worker_in_node_execution(self) -> None:
        harness = InProcessWorkerHarness()

        async def orchestrator_handler(request: GetAttributionContextRequest) -> GetAttributionContextResultSuccess:  # noqa: ARG001
            return _orchestrator_answer()

        async def worker_local_handler(request: GetAttributionContextRequest) -> GetAttributionContextResultSuccess:  # noqa: ARG001
            return GetAttributionContextResultSuccess(header_value="from-the-worker", result_details="ok")

        harness.orchestrator.assign_manager_to_request_type(GetAttributionContextRequest, orchestrator_handler)
        harness.worker.assign_manager_to_request_type(GetAttributionContextRequest, worker_local_handler)
        harness.install_remote_handler(GetAttributionContextRequest)

        with harness.worker.node_execution_scope():
            result_event = await harness.worker.ahandle_request(GetAttributionContextRequest())

        assert result_event.succeeded()
        assert isinstance(result_event.result, GetAttributionContextResultSuccess)
        assert result_event.result.header_value == "from-the-orchestrator"
        assert result_event.result.project_chain == ["orchestrator-project"]

    @pytest.mark.asyncio
    async def test_attribution_is_local_outside_node_execution(self) -> None:
        """Worker bootstrap makes no metered calls, so answering locally there is correct."""
        harness = InProcessWorkerHarness()

        async def orchestrator_handler(request: GetAttributionContextRequest) -> GetAttributionContextResultSuccess:  # noqa: ARG001
            return _orchestrator_answer()

        async def worker_local_handler(request: GetAttributionContextRequest) -> GetAttributionContextResultSuccess:  # noqa: ARG001
            return GetAttributionContextResultSuccess(header_value="from-the-worker", result_details="ok")

        harness.orchestrator.assign_manager_to_request_type(GetAttributionContextRequest, orchestrator_handler)
        harness.worker.assign_manager_to_request_type(GetAttributionContextRequest, worker_local_handler)
        harness.install_remote_handler(GetAttributionContextRequest)

        result_event = await harness.worker.ahandle_request(GetAttributionContextRequest())

        assert result_event.succeeded()
        assert isinstance(result_event.result, GetAttributionContextResultSuccess)
        assert result_event.result.header_value == "from-the-worker"


class TestAttributionForwardingIsWired:
    """The wiring, and the bootstrap invariant it depends on."""

    def test_attribution_is_not_answered_locally(self) -> None:
        """Forwarding is the default, so this pins that nothing has excluded it."""
        assert GetAttributionContextRequest not in LOCAL_ONLY_REQUEST_TYPES

    def test_register_remote_handlers_installs_a_remote_handler(self) -> None:
        """A type with no registered owner is skipped, so the handler is the only proof.

        `register_remote_handlers` walks the types the event manager knows about and passes
        over any without an owner, silently. BudgetManager is constructed on every engine,
        including a worker's, and this pins that its request comes out forwarded rather than
        answered against the worker's own replica.
        """
        worker_engine = Engine()

        register_remote_handlers(worker_engine.event_manager)

        handler = worker_engine.event_manager.get_manager_for_request_type(GetAttributionContextRequest)
        assert isinstance(handler, RemoteHandler)

    def test_forwarding_works_from_a_pool_thread(self) -> None:
        """Covers the sync caller: a driver on a library pool thread inside `process()`.

        Every credit-consuming Cloud node is `aprocess()` today, and that path -- awaiting
        the RemoteHandler on the worker's own loop -- is what the two harness tests above
        exercise. A sync `process()` node reaches the same handler through a different
        bridge: the pool thread has no running loop, so `_invoke_handler_from_sync` takes
        its `RemoteHandler` branch and drives the coroutine with `asyncio.run`. Both need
        to work, and only this one is off the async path.

        The node-execution scope is a lock-guarded int on the EventManager rather than a
        ContextVar precisely so entering it here is visible on the pool thread.
        """
        worker_engine = Engine()
        event_manager = worker_engine.event_manager
        local_handler = event_manager.get_manager_for_request_type(GetAttributionContextRequest)
        assert local_handler is not None

        async def fake_forward(request: Any, result_context: Any) -> EventResultSuccess:  # noqa: ARG001
            return EventResultSuccess(request=request, result=_orchestrator_answer())

        event_manager.forward_to_orchestrator = fake_forward  # type: ignore[method-assign]
        event_manager.replace_manager_for_request_type(
            GetAttributionContextRequest,
            RemoteHandler(original=local_handler, event_manager=event_manager),
        )

        with event_manager.node_execution_scope(), ThreadPoolExecutor(max_workers=1) as pool:
            event_result = pool.submit(event_manager.handle_request, GetAttributionContextRequest()).result()

        assert isinstance(event_result.result, GetAttributionContextResultSuccess)
        assert event_result.result.header_value == "from-the-orchestrator"


class TestSpendRequestsAreForwardedFromWorkers:
    """The check and the report also run on the orchestrator, which holds the authoritative chain."""

    @pytest.mark.parametrize("request_type", [BudgetAccessRequest, ReportUsageRequest])
    def test_not_answered_locally(self, request_type: type) -> None:
        assert request_type not in LOCAL_ONLY_REQUEST_TYPES

    @pytest.mark.asyncio
    async def test_the_node_fields_cross_the_boundary(self) -> None:
        """The orchestrator has no current node, so `node_type` and `node_id` ride on the request."""
        harness = InProcessWorkerHarness()
        received: list[BudgetAccessRequest] = []

        async def orchestrator_handler(
            request: BudgetAccessRequest,
            workflow: object | None = None,  # noqa: ARG001
        ) -> BudgetAccessResultSuccess:
            received.append(request)
            return BudgetAccessResultSuccess(correlation_id="c", checked=True, result_details="ok")

        async def worker_local_handler(
            request: BudgetAccessRequest,  # noqa: ARG001
            workflow: object | None = None,  # noqa: ARG001
        ) -> BudgetAccessResultSuccess:
            return BudgetAccessResultSuccess(correlation_id="local", checked=False, result_details="ok")

        harness.orchestrator.assign_manager_to_request_type(BudgetAccessRequest, orchestrator_handler)
        harness.worker.assign_manager_to_request_type(BudgetAccessRequest, worker_local_handler)
        harness.install_remote_handler(BudgetAccessRequest)

        with harness.worker.node_execution_scope():
            result_event = await harness.worker.ahandle_request(
                BudgetAccessRequest(model="m", estimated_cost_micro_usd=5, node_type="T", node_id="n-1")
            )

        assert isinstance(result_event.result, BudgetAccessResultSuccess)
        assert result_event.result.correlation_id == "c"
        assert (received[0].model, received[0].estimated_cost_micro_usd) == ("m", 5)
        assert (received[0].node_type, received[0].node_id) == ("T", "n-1")

    @pytest.mark.asyncio
    async def test_a_refusal_reaches_the_worker_s_node_as_a_budget_refusal(self) -> None:
        """Over the real wire, a refusal reaches the node as a refusal that still names its budget.

        It arrives as `BudgetAccessResultFailure` carrying a `BudgetExceededError`, so raising it stops
        the run as it would in process.
        """
        refusal = refusal_from_check({"blocked_by": [STUDIO_MONTHLY]})
        message = describe(refusal)
        orchestrator_answer = BudgetAccessResultFailure(
            blocked_by=[STUDIO_MONTHLY],
            correlation_id="corr-1",
            result_details=message,
            exception=BudgetExceededError(message, refusal),
        )
        wire = _WireToOrchestrator(orchestrator_answer)
        worker = EventManager()
        async with RequestClient(
            client=wire,  # type: ignore[arg-type]
            request_topic_fn=lambda: "request",
            response_topic_fn=lambda: "response",
        ) as request_client:
            wire.request_client = request_client
            worker.configure_worker_forwarding(
                request_client=request_client,
                orchestrator_request_topic="sessions/s/request",
                worker_response_topic="sessions/s/workers/w1/response",
                websocket_event_loop=asyncio.get_running_loop(),
                timeout_ms=5000,
            )
            event = await worker.forward_to_orchestrator(BudgetAccessRequest(model="gpt-4o"), ResultContext())

        result = event.result
        assert isinstance(result, BudgetAccessResultFailure)
        assert isinstance(result.exception, BudgetExceededError)
        assert [budget.budget_name for budget in result.exception.refusal.budgets] == ["Studio monthly"]

        # The node raises it, as the docs say to, and the worker words the halt for that node.
        failure = NodeManager(MagicMock(), engine=MagicMock())._execution_failure(result.exception, "Upscale")
        assert str(failure.result_details).startswith(BUDGET_HALT_PREFIX)
        assert "'Upscale'" in str(failure.result_details)
        assert '"Studio monthly"' in str(failure.result_details)


# One `blocked_by` entry as Griptape Cloud's POST /api/budget-checks returns it.
STUDIO_MONTHLY = {
    "budget_id": "b1",
    "budget_name": "Studio monthly",
    "scope_type": "TAG",
    "reset_period": "MONTHLY",
    "enforcement": "HARD",
    "limit_credits": 1000,
    "spent_credits": 1200,
    "remaining_credits": -200,
}


class _WireToOrchestrator:
    """A transport whose orchestrator answers every published request with `answer`, as the real one would.

    The answer goes through the engine's converter and back in through the RequestClient's message
    filter, the path a reply from another process takes.
    """

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.request_client: RequestClient | None = None
        self.subscribe = AsyncMock()
        self._filters: list[Any] = []

    def add_message_filter(self, fn: Any) -> None:
        self._filters.append(fn)

    def remove_message_filter(self, fn: Any) -> None:
        self._filters.remove(fn)

    async def publish(self, _event_type: str, payload: dict[str, Any], _topic: str) -> None:
        request = converters.engine.structure(payload, EventRequest)
        reply = json.loads(converters.engine.dumps(EventResultFailure(request=request.request, result=self.answer)))
        reply["request_id"] = payload["request_id"]
        for message_filter in self._filters:
            if await message_filter({"payload": reply}):
                return
