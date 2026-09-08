"""Worker-side attribution is answered by the orchestrator, not locally.

A worker process has its own engine id and no workflow context, so composing the
attribution header there would report the wrong engine and omit the workflow. The
orchestrator holds the authoritative project chain and workflow name, so
`GetAttributionContextRequest` joins `FORWARDED_REQUEST_TYPES` and a worker-side
`RemoteHandler` forwards it -- but only inside a `worker_node_execution_scope`, which is
the only time a node spends credits.

No shipping library runs worker-hosted today; a `worker_mode_override` config entry can
flip one, which is why this is wired now rather than deferred.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from griptape_nodes.app.worker_routing import (
    FORWARDED_REQUEST_TYPES,
    RemoteHandler,
    register_remote_handlers,
)
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.base_events import EventResultSuccess
from griptape_nodes.retained_mode.events.budget_events import (
    GetAttributionContextRequest,
    GetAttributionContextResultSuccess,
)
from tests.unit.worker.harness import InProcessWorkerHarness


def _orchestrator_answer() -> GetAttributionContextResultSuccess:
    """A result only the orchestrator could have produced, so its origin is unambiguous."""
    return GetAttributionContextResultSuccess(
        header_value="from-the-orchestrator",
        workflow_name="shots/sh020/lighting",
        engine_id="orchestrator-engine",
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

        with harness.worker.worker_node_execution_scope():
            result_event = await harness.worker.ahandle_request(GetAttributionContextRequest())

        assert result_event.succeeded()
        assert isinstance(result_event.result, GetAttributionContextResultSuccess)
        assert result_event.result.header_value == "from-the-orchestrator"
        assert result_event.result.workflow_name == "shots/sh020/lighting"

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
    """The one-line wiring, and the bootstrap invariant it depends on."""

    def test_attribution_is_a_forwarded_request_type(self) -> None:
        assert GetAttributionContextRequest in FORWARDED_REQUEST_TYPES

    def test_register_remote_handlers_finds_a_budget_manager_owner(self) -> None:
        """`register_remote_handlers` raises for a forwarded type with no registered owner.

        BudgetManager is therefore constructed unconditionally, including on workers -- a
        bootstrap-order bug here would surface as a worker that cannot start at all.
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
        event_manager.remove_manager_from_request_type(GetAttributionContextRequest)
        event_manager.assign_manager_to_request_type(
            GetAttributionContextRequest,
            RemoteHandler(original=local_handler, event_manager=event_manager),
        )

        with event_manager.worker_node_execution_scope(), ThreadPoolExecutor(max_workers=1) as pool:
            event_result = pool.submit(event_manager.handle_request, GetAttributionContextRequest()).result()

        assert isinstance(event_result.result, GetAttributionContextResultSuccess)
        assert event_result.result.header_value == "from-the-orchestrator"
