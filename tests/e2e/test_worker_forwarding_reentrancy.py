"""End-to-end tests for worker->orchestrator request forwarding under reentrancy.

When a node executes off the orchestrator, every request it makes mid-execution forwards to
the orchestrator, which must service those requests while it is itself awaiting the execution
that produced them. These tests pin that machinery -- ``EventManager.forward_to_orchestrator``,
the worker's channel sender, and the cross-loop dispatch topology -- over a real channel to an
end that dispatches each forwarded request into a real engine.

The topology reproduced here is the real one, because it is exactly the topology that has
bitten before (see ``configure_worker_forwarding``'s docstring): the forward is dispatched onto
a loop of its own, the channel runs on its own I/O thread, and the request is answered on the
loop the code that forwards runs on, so every forward crosses loops several times. Awaiting
across loops wrongly has historically stalled for seconds per request; the latency assertions
here exist to fail if that regression returns.

The contract these tests pin:

- **Service while awaiting**: a forwarded request is dispatched and answered while the main
  loop coroutine that sent it is still awaiting the reply.
- **Contention**: many concurrent forwards, including from foreign threads (the diffusers
  thread-pool shape), all complete, and none stalls for seconds.
- **Bounded failure**: when the orchestrator never replies, the forward raises TimeoutError
  within the worker's bound instead of hanging.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

from griptape_nodes.retained_mode.engine import current_engine
from griptape_nodes.retained_mode.events.app_events import GetEngineVersionRequest
from griptape_nodes.retained_mode.events.base_events import EventResultSuccess
from griptape_nodes.retained_mode.managers.event_manager import ResultContext
from griptape_nodes.worker import runtime
from griptape_nodes.worker.channel import WorkerLink, listen_for_worker
from griptape_nodes.worker.runtime import WorkerSide
from griptape_nodes.worker.wire import answer_event_request, read_event_request

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

# Generous enough for CI noise, far below the seconds-long stalls this guards against.
SINGLE_FORWARD_BUDGET_S = 2.0
CONTENTION_TOTAL_BUDGET_S = 10.0
CONTENTION_COUNT = 50
ACCEPT_TIMEOUT_S = 5.0
# How long the worker waits on an orchestrator that never answers, shortened from the real bound.
UNANSWERED_BOUND_S = 1.0


class Orchestrator:
    """The engine end of the worker's channel: dispatches each forwarded request into a real engine.

    The channel runs each request it receives as a task of its own, as the engine end does, so
    nothing serializes behind whatever the orchestrator is currently awaiting. Set
    ``drop_requests`` to simulate an orchestrator that never answers.
    """

    def __init__(self) -> None:
        self.drop_requests = False
        self.released = asyncio.Event()

    async def on_request(self, body: dict[str, Any]) -> dict[str, Any]:
        if self.drop_requests:
            await self.released.wait()
        return await answer_event_request(current_engine(), read_event_request(body))


class ForwardingLoop:
    """A daemon thread running its own event loop, like the worker's forwarding loop."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)
        self.loop.close()


@pytest_asyncio.fixture
async def forwarding(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Orchestrator]:
    """Configure worker forwarding on a real engine, over a real channel to an end that dispatches into it."""
    monkeypatch.setattr(runtime, "PARENT_REQUEST_TIMEOUT_S", UNANSWERED_BOUND_S)
    orchestrator = Orchestrator()
    pending = await listen_for_worker()
    # Built off this loop: the handshake it waits for is answered on this loop.
    link = await asyncio.to_thread(WorkerLink, pending.child_environment)
    parent = await pending.accept(ACCEPT_TIMEOUT_S)
    parent.start(on_request=orchestrator.on_request, on_notification=lambda _body: None, on_closed=lambda: None)
    side = WorkerSide(link, on_parent_gone=lambda: None)
    side.start(asyncio.get_running_loop())
    side.begin_serving()
    forwarding_loop = ForwardingLoop()
    current_engine().event_manager.configure_worker_forwarding(
        request_client=runtime._ChannelRequestSender(side),
        websocket_event_loop=forwarding_loop.loop,
    )
    yield orchestrator
    orchestrator.released.set()
    link.close()
    parent.close()
    forwarding_loop.shutdown()


class TestServiceWhileAwaiting:
    @pytest.mark.asyncio
    async def test_forward_is_serviced_while_sender_awaits(self, forwarding: Orchestrator) -> None:  # noqa: ARG002 (fixture wires forwarding)
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
    async def test_forward_inside_node_execution_scope(self, forwarding: Orchestrator) -> None:  # noqa: ARG002 (fixture wires forwarding)
        """Forwarding works from inside the scope that marks node execution."""
        event_manager = current_engine().event_manager
        with event_manager.node_execution_scope():
            assert event_manager.in_node_execution()
            result = await event_manager.forward_to_orchestrator(GetEngineVersionRequest(), ResultContext())
        assert isinstance(result, EventResultSuccess)


class TestContention:
    @pytest.mark.asyncio
    async def test_many_concurrent_forwards_from_the_main_loop(self, forwarding: Orchestrator) -> None:  # noqa: ARG002 (fixture wires forwarding)
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
    async def test_forwards_from_foreign_threads(self, forwarding: Orchestrator) -> None:  # noqa: ARG002 (fixture wires forwarding)
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
            # Exception, not BaseException: AssertionError is what this collects, and letting
            # KeyboardInterrupt/SystemExit through keeps a stuck run interruptible.
            except Exception as e:
                errors.append(e)

        with event_manager.node_execution_scope():
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
    async def test_unanswered_forward_times_out_instead_of_hanging(self, forwarding: Orchestrator) -> None:
        """An orchestrator that never replies produces a TimeoutError within the bound."""
        forwarding.drop_requests = True
        event_manager = current_engine().event_manager
        started = time.monotonic()

        with pytest.raises(TimeoutError):
            await event_manager.forward_to_orchestrator(GetEngineVersionRequest(), ResultContext())

        elapsed = time.monotonic() - started
        # The worker's bound is 1s here; anything wildly past it means the bound is not real.
        assert elapsed < SINGLE_FORWARD_BUDGET_S * 2, f"timeout took {elapsed:.2f}s to fire"
