"""End-to-end tests for worker->orchestrator request forwarding under reentrancy.

When a node executes off the orchestrator, every request it makes mid-execution forwards to
the orchestrator, which must service those requests while it is itself awaiting the execution
that produced them. These tests pin that machinery -- ``EventManager.forward_to_orchestrator``,
``RequestClient`` response matching, and the cross-loop dispatch topology -- without a real
websocket, using a fake broker that loops forwarded requests into a real engine's dispatch.

The topology reproduced here is the real one, because it is exactly the topology that has
bitten before (see ``configure_worker_forwarding``'s docstring): the ``RequestClient`` and its
futures live on a daemon thread's event loop, while the code that forwards runs on the main
loop, so every forward crosses loops twice. Awaiting the client's primitives from the wrong
loop historically stalled for seconds per request; the latency assertions here exist to fail
if that regression returns.

The contract these tests pin:

- **Service while awaiting**: a forwarded request is dispatched and answered while the main
  loop coroutine that sent it is still awaiting the reply.
- **Contention**: many concurrent forwards, including from foreign threads (the diffusers
  thread-pool shape), all complete, and none stalls for seconds.
- **Bounded failure**: when the orchestrator never replies, the forward raises TimeoutError
  within the configured bound instead of hanging.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import TYPE_CHECKING, Any, cast

import pytest
import pytest_asyncio

from griptape_nodes.api_client.request_client import RequestClient
from griptape_nodes.retained_mode.engine import current_engine
from griptape_nodes.retained_mode.events.app_events import GetEngineVersionRequest
from griptape_nodes.retained_mode.events.base_events import (
    EventResultFailure,
    EventResultSuccess,
)
from griptape_nodes.retained_mode.events.event_converter import converter
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry
from griptape_nodes.retained_mode.managers.event_manager import ResultContext

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from griptape_nodes.retained_mode.events.base_events import RequestPayload

ORCHESTRATOR_REQUEST_TOPIC = "sessions/test/request"
WORKER_RESPONSE_TOPIC = "engines/test-worker/response"

# Generous enough for CI noise, far below the seconds-long stalls this guards against.
SINGLE_FORWARD_BUDGET_S = 2.0
CONTENTION_TOTAL_BUDGET_S = 10.0
CONTENTION_COUNT = 50


class FakeBrokerClient:
    """Stands in for the websocket Client: loops forwarded requests into a real engine.

    ``publish`` to the orchestrator request topic dispatches the deserialized request into
    the orchestrator engine on ``orchestrator_loop`` as its own task (mirroring the app's
    task-per-request ingress), then delivers the result back through the registered message
    filters on the broker loop, which is where ``RequestClient._try_match`` and its futures
    live. Set ``drop_requests`` to simulate an orchestrator that never answers.
    """

    def __init__(self, orchestrator_loop: asyncio.AbstractEventLoop) -> None:
        self.orchestrator_loop = orchestrator_loop
        self.drop_requests = False
        self._filters: list[Callable[[dict[str, Any]], Any]] = []
        self.subscribed_topics: list[str] = []

    def add_message_filter(self, filter_fn: Callable[[dict[str, Any]], Any]) -> None:
        self._filters.append(filter_fn)

    def remove_message_filter(self, filter_fn: Callable[[dict[str, Any]], Any]) -> None:
        self._filters.remove(filter_fn)

    async def subscribe(self, topic: str) -> None:
        self.subscribed_topics.append(topic)

    async def publish(self, event_type: str, payload: dict[str, Any], topic: str) -> None:
        assert event_type == "EventRequest"
        assert topic == ORCHESTRATOR_REQUEST_TOPIC
        if self.drop_requests:
            return

        broker_loop = asyncio.get_running_loop()
        request_type = PayloadRegistry.get_type(payload["request_type"])
        assert request_type is not None
        request = cast("RequestPayload", converter.structure(payload["request"], request_type))
        request_id = payload.get("request_id")

        async def dispatch_and_reply() -> None:
            # Task-per-request, exactly like the app's ingress: nothing serializes behind
            # whatever the orchestrator is currently awaiting.
            result = await current_engine().ahandle_request(request)
            event_cls = EventResultSuccess if result.succeeded() else EventResultFailure
            reply = event_cls(request=request, request_id=request_id, result=result)
            reply_payload = json.loads(reply.json())
            broker_loop.call_soon_threadsafe(
                lambda: broker_loop.create_task(self._deliver(reply_payload)),
            )

        self.orchestrator_loop.call_soon_threadsafe(
            lambda: self.orchestrator_loop.create_task(dispatch_and_reply()),
        )

    async def _deliver(self, payload: dict[str, Any]) -> None:
        message = {"payload": payload}
        for filter_fn in list(self._filters):
            claimed = await filter_fn(message)
            if claimed:
                return


class BrokerThread:
    """A daemon thread running its own event loop, like the app's websocket thread."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._thread.start()

    def run(self, coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=30)

    def shutdown(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)
        self.loop.close()


@pytest_asyncio.fixture
async def forwarding() -> AsyncIterator[FakeBrokerClient]:
    """Configure worker-style forwarding on a real engine, against the fake broker."""
    orchestrator_loop = asyncio.get_running_loop()
    broker = BrokerThread()
    fake_client = FakeBrokerClient(orchestrator_loop)
    request_client = RequestClient(fake_client)  # type: ignore[arg-type]
    broker.run(request_client.__aenter__())

    event_manager = current_engine().event_manager
    event_manager.configure_worker_forwarding(
        request_client=request_client,
        orchestrator_request_topic=ORCHESTRATOR_REQUEST_TOPIC,
        worker_response_topic=WORKER_RESPONSE_TOPIC,
        websocket_event_loop=broker.loop,
        timeout_ms=1_000,
    )
    yield fake_client
    broker.run(request_client.__aexit__(None, None, None))
    broker.shutdown()


class TestServiceWhileAwaiting:
    @pytest.mark.asyncio
    async def test_forward_is_serviced_while_sender_awaits(self, forwarding: FakeBrokerClient) -> None:  # noqa: ARG002 (fixture wires forwarding)
        """The main loop services the forwarded request while the forwarding coroutine awaits it.

        This is the reentrancy shape the whole design depends on: the awaiting side and the
        servicing side share the orchestrator loop.
        """
        event_manager = current_engine().event_manager
        started = time.monotonic()

        result = await event_manager.forward_to_orchestrator(GetEngineVersionRequest(), ResultContext())

        elapsed = time.monotonic() - started
        assert isinstance(result, EventResultSuccess)
        assert result.result.succeeded()
        assert elapsed < SINGLE_FORWARD_BUDGET_S, f"single forward took {elapsed:.2f}s; cross-loop stall regression"

    @pytest.mark.asyncio
    async def test_forward_inside_node_execution_scope(self, forwarding: FakeBrokerClient) -> None:  # noqa: ARG002 (fixture wires forwarding)
        """Forwarding works from inside the scope that marks node execution."""
        event_manager = current_engine().event_manager
        with event_manager.worker_node_execution_scope():
            assert event_manager.in_node_execution()
            result = await event_manager.forward_to_orchestrator(GetEngineVersionRequest(), ResultContext())
        assert isinstance(result, EventResultSuccess)


class TestContention:
    @pytest.mark.asyncio
    async def test_many_concurrent_forwards_from_the_main_loop(self, forwarding: FakeBrokerClient) -> None:  # noqa: ARG002 (fixture wires forwarding)
        event_manager = current_engine().event_manager
        started = time.monotonic()

        results = await asyncio.gather(
            *(
                event_manager.forward_to_orchestrator(GetEngineVersionRequest(), ResultContext())
                for _ in range(CONTENTION_COUNT)
            )
        )

        elapsed = time.monotonic() - started
        assert all(isinstance(r, EventResultSuccess) for r in results)
        assert elapsed < CONTENTION_TOTAL_BUDGET_S, f"{CONTENTION_COUNT} forwards took {elapsed:.2f}s"

    @pytest.mark.asyncio
    async def test_forwards_from_foreign_threads(self, forwarding: FakeBrokerClient) -> None:  # noqa: ARG002 (fixture wires forwarding)
        """Requests emitted from library-internal threads (the diffusers thread-pool shape).

        A foreign thread cannot await on the orchestrator loop, so it does what node code
        effectively does: schedule the coroutine onto the loop and block on the result.
        """
        event_manager = current_engine().event_manager
        orchestrator_loop = asyncio.get_running_loop()
        errors: list[BaseException] = []

        def forward_from_thread() -> None:
            future = asyncio.run_coroutine_threadsafe(
                event_manager.forward_to_orchestrator(GetEngineVersionRequest(), ResultContext()),
                orchestrator_loop,
            )
            try:
                result = future.result(timeout=SINGLE_FORWARD_BUDGET_S * 5)
                assert isinstance(result, EventResultSuccess)
            except BaseException as e:
                errors.append(e)

        with event_manager.worker_node_execution_scope():
            threads = [threading.Thread(target=forward_from_thread) for _ in range(8)]
            for t in threads:
                t.start()
            # The orchestrator loop must stay free to service while threads block on replies.
            await asyncio.sleep(0)
            deadline = time.monotonic() + CONTENTION_TOTAL_BUDGET_S
            while any(t.is_alive() for t in threads):
                if time.monotonic() > deadline:
                    pytest.fail("foreign-thread forwards did not complete within budget")
                await asyncio.sleep(0.05)

        assert not errors, f"foreign-thread forwards failed: {errors!r}"


class TestBoundedFailure:
    @pytest.mark.asyncio
    async def test_unanswered_forward_times_out_instead_of_hanging(self, forwarding: FakeBrokerClient) -> None:
        """An orchestrator that never replies produces a TimeoutError within the bound."""
        forwarding.drop_requests = True
        event_manager = current_engine().event_manager
        started = time.monotonic()

        with pytest.raises(TimeoutError):
            await event_manager.forward_to_orchestrator(GetEngineVersionRequest(), ResultContext())

        elapsed = time.monotonic() - started
        # Configured timeout is 1s; anything wildly past it means the bound is not real.
        assert elapsed < SINGLE_FORWARD_BUDGET_S * 2, f"timeout took {elapsed:.2f}s to fire"
