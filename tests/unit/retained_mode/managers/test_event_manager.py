"""Test EventManager functionality including sync/async event broadcasting."""

import asyncio
import inspect
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from unittest.mock import AsyncMock

import pytest

from griptape_nodes.app.worker_routing import RemoteHandler
from griptape_nodes.retained_mode.engine import Engine, current_engine
from griptape_nodes.retained_mode.events.app_events import ConfigChanged
from griptape_nodes.retained_mode.events.base_events import (
    AppEvent,
    AppPayload,
    EventRequest,
    EventResultSuccess,
    ExecutionEvent,
    ExecutionGriptapeNodeEvent,
    ExecutionPayload,
    ProgressEvent,
    RequestPayload,
    ResultDetail,
    ResultDetails,
    ResultPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    StrictModeViolationDetail,
)
from griptape_nodes.retained_mode.events.generic_events import GenericResultFailure
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry
from griptape_nodes.retained_mode.managers.authorization_checkpoint import (
    AuthorizationCheckpoint,
    CheckpointAction,
    CheckpointAttribute,
    CheckpointDenial,
    CheckpointFailure,
    CheckpointSubjectType,
)
from griptape_nodes.retained_mode.managers.event_manager import EventManager


class TestEventManagerBroadcasting:
    """Test event broadcasting functionality in EventManager."""

    @pytest.mark.asyncio
    async def test_abroadcast_app_event_calls_all_listeners(self) -> None:
        """Test that abroadcast_app_event calls all registered listeners."""
        event_manager = EventManager()

        # Create mock listeners
        listener1 = AsyncMock()
        listener2 = AsyncMock()

        # Register listeners for ConfigChanged event
        event_manager.add_listener_to_app_event(ConfigChanged, listener1)
        event_manager.add_listener_to_app_event(ConfigChanged, listener2)

        # Create and broadcast event
        event = ConfigChanged(key="test_key", old_value="old", new_value="new")
        await event_manager.abroadcast_app_event(event)

        # Verify both listeners were called
        listener1.assert_called_once_with(event)
        listener2.assert_called_once_with(event)

    @pytest.mark.asyncio
    async def test_abroadcast_app_event_with_no_listeners(self) -> None:
        """Test that abroadcast_app_event handles events with no listeners gracefully."""
        event_manager = EventManager()

        # Create event with no registered listeners
        event = ConfigChanged(key="test_key", old_value="old", new_value="new")

        # Should not raise any exceptions
        await event_manager.abroadcast_app_event(event)

    def test_broadcast_app_event_calls_all_listeners(self) -> None:
        """Test that broadcast_app_event (sync) calls all registered listeners."""
        event_manager = EventManager()

        # Create mock listeners (async functions)
        listener1 = AsyncMock()
        listener2 = AsyncMock()

        # Register listeners
        event_manager.add_listener_to_app_event(ConfigChanged, listener1)
        event_manager.add_listener_to_app_event(ConfigChanged, listener2)

        # Create and broadcast event (sync)
        event = ConfigChanged(key="test_key", old_value="old", new_value="new")
        event_manager.broadcast_app_event(event)

        # Verify both listeners were called
        listener1.assert_called_once_with(event)
        listener2.assert_called_once_with(event)

    def test_broadcast_app_event_with_no_listeners(self) -> None:
        """Test that broadcast_app_event handles events with no listeners gracefully."""
        event_manager = EventManager()

        # Create event with no registered listeners
        event = ConfigChanged(key="test_key", old_value="old", new_value="new")

        # Should not raise any exceptions
        event_manager.broadcast_app_event(event)

    @pytest.mark.asyncio
    async def test_abroadcast_app_event_handles_listener_exceptions(self) -> None:
        """Test that abroadcast_app_event raises ExceptionGroup when a listener raises an exception."""
        event_manager = EventManager()

        # Create listeners where one raises an exception
        listener1 = AsyncMock(side_effect=ValueError("Test error"))
        listener2 = AsyncMock()

        event_manager.add_listener_to_app_event(ConfigChanged, listener1)
        event_manager.add_listener_to_app_event(ConfigChanged, listener2)

        event = ConfigChanged(key="test_key", old_value="old", new_value="new")

        # TaskGroup raises ExceptionGroup when a task fails
        with pytest.raises(ExceptionGroup):
            await event_manager.abroadcast_app_event(event)

    @pytest.mark.asyncio
    async def test_abroadcast_app_event_with_mixed_listener_types(self) -> None:
        """Test that abroadcast_app_event works with both sync and async listeners."""
        event_manager = EventManager()

        # Track calls
        calls = []

        # Create async listener
        async def async_listener(event: ConfigChanged) -> None:
            calls.append(("async", event.key))

        # Create sync listener
        def sync_listener(event: ConfigChanged) -> None:
            calls.append(("sync", event.key))

        event_manager.add_listener_to_app_event(ConfigChanged, async_listener)
        event_manager.add_listener_to_app_event(ConfigChanged, sync_listener)

        event = ConfigChanged(key="test_key", old_value="old", new_value="new")
        await event_manager.abroadcast_app_event(event)

        # Verify both listeners were called
        assert len(calls) == 2  # noqa: PLR2004
        assert ("async", "test_key") in calls
        assert ("sync", "test_key") in calls

    def test_remove_listener_from_app_event(self) -> None:
        """Test that listeners can be removed and won't be called after removal."""
        event_manager = EventManager()

        listener = AsyncMock()
        event_manager.add_listener_to_app_event(ConfigChanged, listener)

        # Broadcast event - listener should be called
        event = ConfigChanged(key="test_key", old_value="old", new_value="new")
        event_manager.broadcast_app_event(event)
        listener.assert_called_once()

        # Remove listener and broadcast again
        event_manager.remove_listener_for_app_event(ConfigChanged, listener)
        listener.reset_mock()

        event2 = ConfigChanged(key="test_key2", old_value="old2", new_value="new2")
        event_manager.broadcast_app_event(event2)

        # Listener should not be called after removal
        listener.assert_not_called()

    @pytest.mark.asyncio
    async def test_abroadcast_app_event_preserves_event_data(self) -> None:
        """Test that event data is correctly passed to listeners."""
        event_manager = EventManager()

        received_events = []

        async def listener(event: ConfigChanged) -> None:
            received_events.append(event)

        event_manager.add_listener_to_app_event(ConfigChanged, listener)

        # Create event with specific data
        original_event = ConfigChanged(
            key="workspace_directory",
            old_value="/old/path",
            new_value="/new/path",
        )

        await event_manager.abroadcast_app_event(original_event)

        # Verify listener received the correct event data
        assert len(received_events) == 1
        received = received_events[0]
        assert received.key == "workspace_directory"
        assert received.old_value == "/old/path"
        assert received.new_value == "/new/path"


@dataclass(kw_only=True)
class _ProbeRequest(RequestPayload):
    """Minimal request type used only to exercise dispatch routing in tests."""


@dataclass(kw_only=True)
class _ProbeResult(ResultPayloadSuccess):
    """Minimal success payload paired with _ProbeRequest."""


class TestHandleRequestLoopSafety:
    """`handle_request` drives async handlers via ThreadRunner from inside a running loop.

    The #4469 deadlock is specific to callbacks whose coroutines share
    primitives with the caller's loop; ``RemoteHandler`` is the only such
    shape and is routed onto the WS loop via ``run_coroutine_threadsafe``.
    For every other async handler the side-loop path is safe, so we keep
    it to preserve back-compat for pre-#4449 workflows that ``exec()``
    sync code from inside the engine loop.
    """

    @pytest.mark.asyncio
    async def test_sync_dispatch_from_running_loop_drives_async_handler_via_thread_runner(self) -> None:
        event_manager = EventManager()

        captured: dict[str, object] = {}

        async def async_handler(_request: _ProbeRequest) -> _ProbeResult:
            captured["handler_loop"] = asyncio.get_running_loop()
            return _ProbeResult(result_details="ok")

        event_manager.assign_manager_to_request_type(_ProbeRequest, async_handler)

        caller_loop = asyncio.get_running_loop()
        event = event_manager.handle_request(_ProbeRequest())

        assert event.result.succeeded()
        assert captured["handler_loop"] is not caller_loop

    @pytest.mark.asyncio
    async def test_sync_dispatch_from_running_loop_works_when_handler_is_sync(self) -> None:
        event_manager = EventManager()

        def sync_handler(_request: _ProbeRequest) -> _ProbeResult:
            return _ProbeResult(result_details="ok")

        event_manager.assign_manager_to_request_type(_ProbeRequest, sync_handler)

        event = event_manager.handle_request(_ProbeRequest())
        assert event.result.succeeded()

    def test_sync_dispatch_outside_running_loop_drives_async_handler(self) -> None:
        event_manager = EventManager()

        async def async_handler(_request: _ProbeRequest) -> _ProbeResult:
            return _ProbeResult(result_details="ok")

        event_manager.assign_manager_to_request_type(_ProbeRequest, async_handler)

        event = event_manager.handle_request(_ProbeRequest())
        assert event.result.succeeded()

    @pytest.mark.asyncio
    async def test_ahandle_request_is_the_recommended_async_alternative(self) -> None:
        event_manager = EventManager()

        async def async_handler(_request: _ProbeRequest) -> _ProbeResult:
            return _ProbeResult(result_details="ok")

        event_manager.assign_manager_to_request_type(_ProbeRequest, async_handler)

        event = await event_manager.ahandle_request(_ProbeRequest())
        assert event.result.succeeded()


class TestHandleRequestStampsIdentity:
    """`_handle_request_core` stamps identity on its result even when the queue is never touched.

    The app's inbound path awaits `ahandle_request`/`handle_request` and broadcasts the
    returned result straight to the transport, so a result that never reaches `put_event`/
    `aput_event` still needs its origin recorded here.
    """

    def test_handle_request_stamps_identity_without_initializing_the_queue(self) -> None:
        engine = Engine()
        engine.engine_identity_manager.active_engine_id = "engine-a"
        engine.session_manager.active_session_id = "session-a"

        def sync_handler(_request: _ProbeRequest) -> _ProbeResult:
            return _ProbeResult(result_details="ok")

        engine.event_manager.assign_manager_to_request_type(_ProbeRequest, sync_handler)

        result = engine.event_manager.handle_request(_ProbeRequest())

        assert result.engine_id == "engine-a"
        assert result.session_id == "session-a"

    @pytest.mark.asyncio
    async def test_ahandle_request_stamps_identity_without_initializing_the_queue(self) -> None:
        engine = Engine()
        engine.engine_identity_manager.active_engine_id = "engine-a"
        engine.session_manager.active_session_id = "session-a"

        async def async_handler(_request: _ProbeRequest) -> _ProbeResult:
            return _ProbeResult(result_details="ok")

        engine.event_manager.assign_manager_to_request_type(_ProbeRequest, async_handler)

        result = await engine.event_manager.ahandle_request(_ProbeRequest())

        assert result.engine_id == "engine-a"
        assert result.session_id == "session-a"


@dataclass(kw_only=True)
class _ForwardableProbeRequest(RequestPayload):
    """Minimal request used to drive the worker-forwarding path in tests."""


@dataclass(kw_only=True)
class _ForwardableProbeResult(ResultPayloadSuccess):
    """Minimal success payload paired with _ForwardableProbeRequest."""


class TestHandleRequestForwardingFromRunningLoop:
    """``handle_request`` dispatch of a RemoteHandler from inside a running loop.

    A RemoteHandler's forwarding path hops onto a dedicated websocket event
    loop running on a separate thread. That loop does not share primitives
    with the caller's loop, so blocking the caller thread on the resulting
    future is safe -- the #4469 deadlock shape does not apply. This is
    distinct from the local async-handler case (still fails fast) because
    dispatching a regular async handler locally would block the caller's own
    loop.
    """

    @pytest.mark.asyncio
    async def test_sync_dispatch_from_running_loop_forwards_via_websocket_loop(self) -> None:
        event_manager = EventManager()

        # Spin up a dedicated loop on another thread to stand in for the websocket loop.
        ws_loop = asyncio.new_event_loop()
        ws_thread = threading.Thread(target=ws_loop.run_forever, daemon=True)
        ws_thread.start()

        captured: dict[str, object] = {}

        async def fake_forward(
            _request: RequestPayload,
            _result_context: object,
        ) -> EventResultSuccess:
            # Record which loop actually executed the forward.
            captured["forward_loop"] = asyncio.get_running_loop()
            return EventResultSuccess(
                request=_request,
                result=_ForwardableProbeResult(result_details="forwarded"),
            )

        # Register a RemoteHandler shape: dispatch table entry points at it directly.
        # ``handle_request`` detects RemoteHandler via isinstance and routes onto the WS loop.
        async def original(_request: _ForwardableProbeRequest) -> _ForwardableProbeResult:
            return _ForwardableProbeResult(result_details="local")

        remote = RemoteHandler(original=original, event_manager=event_manager)
        event_manager.assign_manager_to_request_type(_ForwardableProbeRequest, remote)
        event_manager._websocket_event_loop = ws_loop
        event_manager.forward_to_orchestrator = fake_forward  # type: ignore[method-assign]

        try:
            with event_manager.worker_node_execution_scope():
                result = event_manager.handle_request(_ForwardableProbeRequest())
        finally:
            ws_loop.call_soon_threadsafe(ws_loop.stop)
            ws_thread.join(timeout=1.0)
            ws_loop.close()

        assert captured["forward_loop"] is ws_loop
        assert isinstance(result, EventResultSuccess)
        assert isinstance(result.result, _ForwardableProbeResult)
        assert "forwarded" in str(result.result.result_details)


@dataclass(kw_only=True)
class _StampProbeRequest(RequestPayload):
    """Minimal request used to exercise forward_to_orchestrator's real body (not stubbed out)."""


@dataclass(kw_only=True)
@PayloadRegistry.register
class _StampProbeResult(ResultPayloadSuccess):
    """Minimal registered success payload.

    Registered so forward_to_orchestrator's real body can resolve it via
    PayloadRegistry.get_type on the fake orchestrator response, the same way a real
    result type would round-trip on the worker-forward path.
    """


class TestForwardToOrchestratorStampsIdentity:
    """`forward_to_orchestrator` bypasses the queue, so it has to stamp identity itself."""

    @pytest.mark.asyncio
    async def test_forward_to_orchestrator_stamps_identity_on_the_forwarded_request(self) -> None:
        engine = Engine()
        engine.engine_identity_manager.active_engine_id = "engine-worker"
        engine.session_manager.active_session_id = "session-worker"

        # Spin up a dedicated loop on another thread to stand in for the websocket loop that
        # configure_worker_forwarding expects, matching the RequestClient's real threading model.
        ws_loop = asyncio.new_event_loop()
        ws_thread = threading.Thread(target=ws_loop.run_forever, daemon=True)
        ws_thread.start()

        captured: dict[str, object] = {}

        class _FakeRequestClient:
            async def request_to_orchestrator(
                self,
                event_request: EventRequest,
                orchestrator_request_topic: str,  # noqa: ARG002
                worker_response_topic: str,  # noqa: ARG002
                timeout_ms: int | None = None,  # noqa: ARG002
            ) -> dict:
                captured["event_request"] = event_request
                return {
                    "event_type": "EventResultSuccess",
                    "result_type": _StampProbeResult.__name__,
                    "result": {"result_details": "ok"},
                }

        engine.event_manager.configure_worker_forwarding(
            request_client=_FakeRequestClient(),  # type: ignore[arg-type]
            orchestrator_request_topic="orchestrator/request",
            worker_response_topic="worker/response",
            websocket_event_loop=ws_loop,
        )

        try:
            await engine.event_manager.forward_to_orchestrator(_StampProbeRequest(), {})
        finally:
            ws_loop.call_soon_threadsafe(ws_loop.stop)
            ws_thread.join(timeout=1.0)
            ws_loop.close()

        event_request = captured["event_request"]
        assert isinstance(event_request, EventRequest)
        assert event_request.engine_id == "engine-worker"
        assert event_request.session_id == "session-worker"


class TestBroadcastAppEventLoopSafety:
    """`broadcast_app_event` drives async listeners via ThreadRunner from inside a running loop."""

    @pytest.mark.asyncio
    async def test_sync_broadcast_from_running_loop_drives_async_listener(self) -> None:
        event_manager = EventManager()

        captured: dict[str, object] = {}

        async def async_listener(_event: ConfigChanged) -> None:
            captured["listener_loop"] = asyncio.get_running_loop()

        event_manager.add_listener_to_app_event(ConfigChanged, async_listener)

        caller_loop = asyncio.get_running_loop()
        event = ConfigChanged(key="k", old_value="a", new_value="b")
        event_manager.broadcast_app_event(event)

        assert "listener_loop" in captured
        assert captured["listener_loop"] is not caller_loop


class TestLogResultDetailsSkipsStrictModeViolations:
    """``_log_result_details`` must not duplicate strict-mode violation logs.

    ``StrictModeReporter.report`` already logs each violation at
    detection time with the scope's ``node=... library=...`` prefix
    (see ``StrictModeReporter`` in ``common/strict_mode.py``).
    Without the skip in ``_log_result_details`` every violation
    that is also attached to the result payload would log a second
    time as a bare message, doubling the noise in the worker log
    that motivated the fix.
    """

    def _make_result_with_details(self, *details: ResultDetail) -> _ProbeResult:
        return _ProbeResult(result_details=ResultDetails(*details))

    def test_strict_mode_violation_details_are_not_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        event_manager = EventManager()
        violation = StrictModeViolationDetail(
            level=logging.WARNING,
            message="violation message that must not appear here",
            rule_id="parameter-mutation-during-aprocess",
            severity="warning",
            subject="some-node",
            library_name="some-library",
        )
        result = self._make_result_with_details(violation)

        caplog.set_level(logging.DEBUG, logger="griptape_nodes")
        event_manager._log_result_details(result)

        assert violation.message not in [r.message for r in caplog.records]

    def test_non_violation_details_still_log(self, caplog: pytest.LogCaptureFixture) -> None:
        event_manager = EventManager()
        ordinary = ResultDetail(level=logging.WARNING, message="ordinary detail")
        result = self._make_result_with_details(ordinary)

        caplog.set_level(logging.DEBUG, logger="griptape_nodes")
        event_manager._log_result_details(result)

        assert ordinary.message in [r.message for r in caplog.records]

    def test_mixed_details_log_only_non_violations(self, caplog: pytest.LogCaptureFixture) -> None:
        """A result with both kinds logs only the non-violation detail.

        The ordinary detail logs; the violation does not.
        """
        event_manager = EventManager()
        ordinary = ResultDetail(level=logging.WARNING, message="ordinary mixed-case detail")
        violation = StrictModeViolationDetail(
            level=logging.WARNING,
            message="mixed-case violation message",
            rule_id="parameter-mutation-during-aprocess",
            severity="warning",
            subject="n",
            library_name=None,
        )
        result = self._make_result_with_details(ordinary, violation)

        caplog.set_level(logging.DEBUG, logger="griptape_nodes")
        event_manager._log_result_details(result)

        messages = [r.message for r in caplog.records]
        assert ordinary.message in messages
        assert violation.message not in messages


@dataclass(kw_only=True)
class _DeniedResult(ResultPayloadFailure):
    """Minimal failure payload a pre-dispatch hook can use to short-circuit dispatch."""


class TestPreDispatchHooks:
    """Pre-dispatch hooks gate request dispatch before the manager callback runs."""

    def test_hook_returning_none_falls_through_to_callback(self) -> None:
        event_manager = EventManager()
        handler_calls: list[RequestPayload] = []

        def handler(request: _ProbeRequest) -> _ProbeResult:
            handler_calls.append(request)
            return _ProbeResult(result_details="ok")

        event_manager.assign_manager_to_request_type(_ProbeRequest, handler)

        hook_calls: list[RequestPayload] = []

        def hook(request: RequestPayload, _context: object) -> None:
            hook_calls.append(request)

        event_manager.add_pre_dispatch_hook(hook)

        event = event_manager.handle_request(_ProbeRequest())

        assert event.result.succeeded()
        assert len(hook_calls) == 1
        assert len(handler_calls) == 1

    def test_hook_short_circuits_before_callback(self) -> None:
        event_manager = EventManager()
        handler_calls: list[RequestPayload] = []

        def handler(request: _ProbeRequest) -> _ProbeResult:
            handler_calls.append(request)
            return _ProbeResult(result_details="ok")

        event_manager.assign_manager_to_request_type(_ProbeRequest, handler)

        def hook(_request: RequestPayload, _context: object) -> _DeniedResult:
            return _DeniedResult(result_details="denied")

        event_manager.add_pre_dispatch_hook(hook)

        event = event_manager.handle_request(_ProbeRequest())

        assert event.result.failed()
        assert "denied" in str(event.result.result_details)
        assert handler_calls == []

    def test_hooks_run_in_registration_order_and_first_result_wins(self) -> None:
        event_manager = EventManager()

        def handler(_request: _ProbeRequest) -> _ProbeResult:
            return _ProbeResult(result_details="ok")

        event_manager.assign_manager_to_request_type(_ProbeRequest, handler)

        order: list[str] = []

        def first(_request: RequestPayload, _context: object) -> _DeniedResult:
            order.append("first")
            return _DeniedResult(result_details="first")

        def second(_request: RequestPayload, _context: object) -> _DeniedResult:
            order.append("second")
            return _DeniedResult(result_details="second")

        event_manager.add_pre_dispatch_hook(first)
        event_manager.add_pre_dispatch_hook(second)

        event = event_manager.handle_request(_ProbeRequest())

        assert order == ["first"]
        assert "first" in str(event.result.result_details)

    def test_hook_raising_fails_closed(self) -> None:
        event_manager = EventManager()
        handler_calls: list[RequestPayload] = []

        def handler(request: _ProbeRequest) -> _ProbeResult:
            handler_calls.append(request)
            return _ProbeResult(result_details="ok")

        event_manager.assign_manager_to_request_type(_ProbeRequest, handler)

        def hook(_request: RequestPayload, _context: object) -> None:
            msg = "boom"
            raise ValueError(msg)

        event_manager.add_pre_dispatch_hook(hook)

        event = event_manager.handle_request(_ProbeRequest())

        # Fail closed: deny with a failure result, and never run the callback.
        assert event.result.failed()
        assert handler_calls == []
        # The denial must use a concrete, registered failure type so it can
        # round-trip on the worker-forward path (PayloadRegistry.get_type).
        assert isinstance(event.result, GenericResultFailure)
        assert PayloadRegistry.get_type(type(event.result).__name__) is GenericResultFailure

    def test_reentrant_hook_is_bypassed_not_recursive(self) -> None:
        event_manager = EventManager()
        handler_calls: list[RequestPayload] = []

        def handler(request: _ProbeRequest) -> _ProbeResult:
            handler_calls.append(request)
            return _ProbeResult(result_details="ok")

        event_manager.assign_manager_to_request_type(_ProbeRequest, handler)

        hook_calls: list[RequestPayload] = []

        def hook(request: RequestPayload, _context: object) -> None:
            hook_calls.append(request)
            # Re-enter the dispatcher from inside a hook. The nested call must
            # bypass the chain rather than re-trigger this hook and recurse.
            if len(hook_calls) == 1:
                event_manager.handle_request(_ProbeRequest())

        event_manager.add_pre_dispatch_hook(hook)

        event = event_manager.handle_request(_ProbeRequest())

        assert event.result.succeeded()
        # Hook ran only for the outer request; the re-entrant dispatch skipped it.
        assert len(hook_calls) == 1
        # Handler ran for both the re-entrant and the outer dispatch.
        assert len(handler_calls) == 2  # noqa: PLR2004

    def test_hook_error_does_not_wedge_later_dispatch(self) -> None:
        event_manager = EventManager()

        def handler(_request: _ProbeRequest) -> _ProbeResult:
            return _ProbeResult(result_details="ok")

        event_manager.assign_manager_to_request_type(_ProbeRequest, handler)

        calls: list[int] = []

        def hook(_request: RequestPayload, _context: object) -> None:
            calls.append(1)
            if len(calls) == 1:
                msg = "boom"
                raise ValueError(msg)

        event_manager.add_pre_dispatch_hook(hook)

        # First dispatch is denied by the erroring hook...
        first = event_manager.handle_request(_ProbeRequest())
        assert first.result.failed()

        # ...and the thread-local guard is cleared, so the next dispatch still
        # evaluates the chain and succeeds.
        second = event_manager.handle_request(_ProbeRequest())
        assert second.result.succeeded()
        assert len(calls) == 2  # noqa: PLR2004

    def test_add_pre_dispatch_hook_dedupes(self) -> None:
        event_manager = EventManager()

        def handler(_request: _ProbeRequest) -> _ProbeResult:
            return _ProbeResult(result_details="ok")

        event_manager.assign_manager_to_request_type(_ProbeRequest, handler)

        hook_calls: list[RequestPayload] = []

        def hook(request: RequestPayload, _context: object) -> None:
            hook_calls.append(request)

        event_manager.add_pre_dispatch_hook(hook)
        event_manager.add_pre_dispatch_hook(hook)

        event_manager.handle_request(_ProbeRequest())

        assert len(hook_calls) == 1

    def test_remove_pre_dispatch_hook_stops_evaluation(self) -> None:
        event_manager = EventManager()

        def handler(_request: _ProbeRequest) -> _ProbeResult:
            return _ProbeResult(result_details="ok")

        event_manager.assign_manager_to_request_type(_ProbeRequest, handler)

        hook_calls: list[RequestPayload] = []

        def hook(request: RequestPayload, _context: object) -> None:
            hook_calls.append(request)

        event_manager.add_pre_dispatch_hook(hook)
        event_manager.remove_pre_dispatch_hook(hook)

        event_manager.handle_request(_ProbeRequest())

        assert hook_calls == []

    def test_remove_unregistered_hook_is_noop(self) -> None:
        event_manager = EventManager()

        def hook(_request: RequestPayload, _context: object) -> None:
            return None

        # Removing a hook that was never registered must not raise.
        event_manager.remove_pre_dispatch_hook(hook)

    @pytest.mark.asyncio
    async def test_async_dispatch_short_circuits_on_hook_result(self) -> None:
        event_manager = EventManager()
        handler_calls: list[RequestPayload] = []

        async def handler(request: _ProbeRequest) -> _ProbeResult:
            handler_calls.append(request)
            return _ProbeResult(result_details="ok")

        event_manager.assign_manager_to_request_type(_ProbeRequest, handler)

        def hook(_request: RequestPayload, _context: object) -> _DeniedResult:
            return _DeniedResult(result_details="denied")

        event_manager.add_pre_dispatch_hook(hook)

        event = await event_manager.ahandle_request(_ProbeRequest())

        assert event.result.failed()
        assert handler_calls == []


@dataclass(kw_only=True)
class _SubProbeRequest(_ProbeRequest):
    """Subclass of the probe request, for asserting hooks match the exact type only."""


@dataclass(kw_only=True)
class _CycleProbeRequest(RequestPayload):
    """A second request type, for hooks that issue each other's requests."""


@dataclass(kw_only=True)
class _OmitFieldProbeRequest(RequestPayload):
    """Carries a field the engine must null out before any hook sees the request."""

    secret: str | None = field(default=None, metadata={"omit_from_result": True})


@dataclass
class _RecordingHook:
    """A callable object rather than a function.

    Deliberately an ordinary ``@dataclass``, which sets ``__hash__ = None``, so
    registering it would raise if the hook store were a set. ``RemoteHandler`` has
    exactly this shape.
    """

    seen: list[ResultPayload]

    def __call__(self, _request: RequestPayload, result: ResultPayload) -> None:
        self.seen.append(result)


@dataclass
class _AsyncRecordingHook:
    """A callable object whose ``__call__`` is async, as ``RemoteHandler``'s is.

    ``inspect.iscoroutinefunction`` returns False for an *instance* of this class, so it
    is the shape that distinguishes a correct async-callable check from a naive one.
    """

    seen: list[ResultPayload]

    async def __call__(self, _request: RequestPayload, result: ResultPayload) -> None:
        self.seen.append(result)


# The re-entrancy guard is what actually bounds _ReissuingHook; this only keeps a
# regression from wedging the test run.
_REISSUE_SAFETY_CAP = 20


@dataclass
class _ReissuingHook:
    """A hook that re-dispatches its own request type, and can be field-equal to a peer.

    Both fields hold shared objects, so two instances compare equal while staying
    distinct registrations. That is the shape that tells an identity-based re-entrancy
    guard apart from an equality-based one.
    """

    event_manager: EventManager
    seen: list[ResultPayload]

    def __call__(self, _request: RequestPayload, result: ResultPayload) -> None:
        self.seen.append(result)
        if len(self.seen) > _REISSUE_SAFETY_CAP:
            return
        self.event_manager.handle_request(_ProbeRequest())


# Hooks reach the loop via call_soon_threadsafe, so the asyncio.Task does not exist yet
# when the dispatch call returns. Ticking the loop creates it; gathering waits it out.
_HOOK_DRAIN_TICKS = 50


async def _drain_post_dispatch_hooks(event_manager: EventManager) -> None:
    """Run the loop until every detached hook task has finished."""
    for _ in range(_HOOK_DRAIN_TICKS):
        await asyncio.sleep(0)
        pending = list(event_manager._inflight_post_dispatch_hook_tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


class TestPostDispatchHooks:
    """Post-dispatch hooks observe a completed request without being able to affect it.

    Most cases run against a bare ``EventManager``, whose ``_event_loop`` is None, so
    hooks take the inline fallback and complete before the dispatch call returns. That
    keeps the assertions deterministic. The loop path -- where the hook is detached and
    the result comes back first -- has its own tests below.
    """

    @staticmethod
    def _manager_with_handler(handler: Callable[[_ProbeRequest], ResultPayload]) -> EventManager:
        event_manager = EventManager()
        event_manager.assign_manager_to_request_type(_ProbeRequest, handler)
        return event_manager

    def test_reentrancy_guard_skips_only_the_hook_that_is_actually_up_chain(self) -> None:
        """The guard scans by identity, so it must not suppress a field-equal peer.

        Two libraries can register field-equal hook objects on one request type. With an
        equality-based scan, one of them running up-chain silently swallows the other's
        re-entrant invocation, and that library's telemetry just goes missing.

        Registration order is [a, b], so with the guard scanning by identity: a fires,
        re-issues, and inside that nested dispatch a is skipped while b fires; then b
        fires at the outer level, re-issues, and inside *that* nesting a fires while b is
        skipped. Four invocations. An equality-based scan skips a's field-equal peer in
        both nestings and yields two.
        """

        def handler(_request: _ProbeRequest) -> _ProbeResult:
            return _ProbeResult(result_details="ok")

        event_manager = self._manager_with_handler(handler)

        shared_seen: list[ResultPayload] = []
        hook_a = _ReissuingHook(event_manager, shared_seen)
        hook_b = _ReissuingHook(event_manager, shared_seen)
        assert hook_a == hook_b
        assert hook_a is not hook_b

        event_manager.add_post_dispatch_hook(_ProbeRequest, hook_a)
        event_manager.add_post_dispatch_hook(_ProbeRequest, hook_b)

        event_manager.handle_request(_ProbeRequest())

        assert len(shared_seen) == 4  # noqa: PLR2004

    def test_hook_fires_for_a_result_a_pre_dispatch_hook_short_circuited(self) -> None:
        """Hooks fire from `_handle_request_core`, which is also the short-circuit's exit.

        A request a pre-dispatch hook denied never reaches the manager callback, and a
        denial is exactly what a library's audit hook wants to see. Moving the fire site
        into the two dispatch methods would silence this path.
        """
        handler_calls: list[RequestPayload] = []

        def handler(request: _ProbeRequest) -> _ProbeResult:
            handler_calls.append(request)
            return _ProbeResult(result_details="ok")

        event_manager = self._manager_with_handler(handler)

        def pre_dispatch(_request: RequestPayload, _context: object) -> _DeniedResult:
            return _DeniedResult(result_details="denied")

        event_manager.add_pre_dispatch_hook(pre_dispatch)

        seen: list[ResultPayload] = []
        event_manager.add_post_dispatch_hook(_ProbeRequest, _RecordingHook(seen))

        event = event_manager.handle_request(_ProbeRequest())

        assert event.result.failed()
        assert handler_calls == []
        assert len(seen) == 1
        assert isinstance(seen[0], _DeniedResult)

    def test_hook_receives_the_request_and_result_after_the_handler(self) -> None:
        order: list[str] = []

        def handler(_request: _ProbeRequest) -> _ProbeResult:
            order.append("handler")
            return _ProbeResult(result_details="ok")

        event_manager = self._manager_with_handler(handler)

        seen_requests: list[RequestPayload] = []
        seen_results: list[ResultPayload] = []

        def hook(request: RequestPayload, result: ResultPayload) -> None:
            order.append("hook")
            seen_requests.append(request)
            seen_results.append(result)

        event_manager.add_post_dispatch_hook(_ProbeRequest, hook)

        request = _ProbeRequest()
        event = event_manager.handle_request(request)

        assert event.result.succeeded()
        assert order == ["handler", "hook"]
        assert seen_requests == [request]
        assert seen_results == [event.result]

    def test_hook_fires_for_a_failure_result(self) -> None:
        def handler(_request: _ProbeRequest) -> _DeniedResult:
            return _DeniedResult(result_details="denied")

        event_manager = self._manager_with_handler(handler)

        seen: list[ResultPayload] = []
        event_manager.add_post_dispatch_hook(_ProbeRequest, _RecordingHook(seen))

        event = event_manager.handle_request(_ProbeRequest())

        assert event.result.failed()
        assert len(seen) == 1
        assert seen[0].failed()

    def test_hook_fires_when_the_handler_raises(self) -> None:
        def handler(_request: _ProbeRequest) -> _ProbeResult:
            msg = "handler exploded"
            raise RuntimeError(msg)

        event_manager = self._manager_with_handler(handler)

        seen: list[ResultPayload] = []
        event_manager.add_post_dispatch_hook(_ProbeRequest, _RecordingHook(seen))

        # The exception propagates to Engine.handle_request, which synthesizes the
        # failure the client sees. Hooks get an equivalent payload rather than that one.
        with pytest.raises(RuntimeError, match="handler exploded"):
            event_manager.handle_request(_ProbeRequest())

        assert len(seen) == 1
        failure = seen[0]
        assert failure.failed()
        assert isinstance(failure, GenericResultFailure)
        assert isinstance(failure.exception, RuntimeError)

    @pytest.mark.asyncio
    async def test_hook_fires_when_an_async_handler_raises(self) -> None:
        event_manager = EventManager()

        async def handler(_request: _ProbeRequest) -> _ProbeResult:
            msg = "async handler exploded"
            raise RuntimeError(msg)

        event_manager.assign_manager_to_request_type(_ProbeRequest, handler)

        seen: list[ResultPayload] = []
        event_manager.add_post_dispatch_hook(_ProbeRequest, _RecordingHook(seen))

        with pytest.raises(RuntimeError, match="async handler exploded"):
            await event_manager.ahandle_request(_ProbeRequest())

        assert len(seen) == 1
        assert seen[0].failed()

    def test_hook_is_not_invoked_for_a_subclass_of_its_request_type(self) -> None:
        def handler(_request: _ProbeRequest) -> _ProbeResult:
            return _ProbeResult(result_details="ok")

        event_manager = self._manager_with_handler(handler)
        event_manager.assign_manager_to_request_type(_SubProbeRequest, handler)

        seen: list[ResultPayload] = []
        event_manager.add_post_dispatch_hook(_ProbeRequest, _RecordingHook(seen))

        event_manager.handle_request(_SubProbeRequest())

        assert seen == []

    def test_async_hook_is_awaited(self) -> None:
        def handler(_request: _ProbeRequest) -> _ProbeResult:
            return _ProbeResult(result_details="ok")

        event_manager = self._manager_with_handler(handler)

        seen: list[ResultPayload] = []

        async def hook(_request: RequestPayload, result: ResultPayload) -> None:
            await asyncio.sleep(0)
            seen.append(result)

        event_manager.add_post_dispatch_hook(_ProbeRequest, hook)

        event_manager.handle_request(_ProbeRequest())

        assert len(seen) == 1

    def test_raising_hook_leaves_the_result_intact_and_siblings_still_run(self) -> None:
        def handler(_request: _ProbeRequest) -> _ProbeResult:
            return _ProbeResult(result_details="ok")

        event_manager = self._manager_with_handler(handler)

        def boom(_request: RequestPayload, _result: ResultPayload) -> None:
            msg = "hook exploded"
            raise RuntimeError(msg)

        seen: list[ResultPayload] = []
        event_manager.add_post_dispatch_hook(_ProbeRequest, boom)
        event_manager.add_post_dispatch_hook(_ProbeRequest, _RecordingHook(seen))

        event = event_manager.handle_request(_ProbeRequest())

        assert event.result.succeeded()
        assert len(seen) == 1

    def test_unhashable_callable_registers_and_fires(self) -> None:
        def handler(_request: _ProbeRequest) -> _ProbeResult:
            return _ProbeResult(result_details="ok")

        event_manager = self._manager_with_handler(handler)

        seen: list[ResultPayload] = []
        hook = _RecordingHook(seen)
        with pytest.raises(TypeError):
            hash(hook)  # Guards the premise: a set-backed store could not hold this.

        event_manager.add_post_dispatch_hook(_ProbeRequest, hook)
        event_manager.handle_request(_ProbeRequest())

        assert len(seen) == 1

        event_manager.remove_post_dispatch_hook(_ProbeRequest, hook)
        event_manager.handle_request(_ProbeRequest())

        assert len(seen) == 1

    def test_add_post_dispatch_hook_dedupes(self) -> None:
        def handler(_request: _ProbeRequest) -> _ProbeResult:
            return _ProbeResult(result_details="ok")

        event_manager = self._manager_with_handler(handler)

        seen: list[ResultPayload] = []
        hook = _RecordingHook(seen)
        event_manager.add_post_dispatch_hook(_ProbeRequest, hook)
        event_manager.add_post_dispatch_hook(_ProbeRequest, hook)

        event_manager.handle_request(_ProbeRequest())

        assert len(seen) == 1

    def test_remove_post_dispatch_hook_stops_invocation(self) -> None:
        def handler(_request: _ProbeRequest) -> _ProbeResult:
            return _ProbeResult(result_details="ok")

        event_manager = self._manager_with_handler(handler)

        seen: list[ResultPayload] = []
        hook = _RecordingHook(seen)
        event_manager.add_post_dispatch_hook(_ProbeRequest, hook)
        event_manager.remove_post_dispatch_hook(_ProbeRequest, hook)

        event_manager.handle_request(_ProbeRequest())

        assert seen == []

    def test_remove_unregistered_post_dispatch_hook_is_noop(self) -> None:
        def handler(_request: _ProbeRequest) -> _ProbeResult:
            return _ProbeResult(result_details="ok")

        event_manager = self._manager_with_handler(handler)
        seen: list[ResultPayload] = []
        registered = _RecordingHook(seen)
        other = _RecordingHook([])

        # Neither an unknown request type nor an unknown callback may raise.
        event_manager.remove_post_dispatch_hook(_ProbeRequest, other)
        event_manager.add_post_dispatch_hook(_ProbeRequest, registered)
        event_manager.remove_post_dispatch_hook(_ProbeRequest, other)
        event_manager.remove_post_dispatch_hook(_SubProbeRequest, registered)

        # And none of it may have disturbed the hook that *is* registered. Without this
        # the test passes even when `other` removes `registered`: the two are separate
        # objects but a `@dataclass` compares by field, and both were built empty.
        assert other == registered
        event_manager.handle_request(_ProbeRequest())
        assert len(seen) == 1

    def test_field_equal_hooks_from_two_libraries_are_kept_apart(self) -> None:
        """Hooks are matched by identity, never by `__eq__`.

        Two libraries can register callable objects that happen to compare equal -- a
        `@dataclass` hook with the same field values is the obvious case. If the store
        matched by equality the second registration would be silently dropped, and either
        library's teardown would disarm the other's live hook.
        """

        def handler(_request: _ProbeRequest) -> _ProbeResult:
            return _ProbeResult(result_details="ok")

        event_manager = self._manager_with_handler(handler)

        shared_log: list[ResultPayload] = []
        hook_a = _RecordingHook(shared_log)
        hook_b = _RecordingHook(shared_log)
        assert hook_a == hook_b
        assert hook_a is not hook_b

        event_manager.add_post_dispatch_hook(_ProbeRequest, hook_a)
        event_manager.add_post_dispatch_hook(_ProbeRequest, hook_b)
        event_manager.handle_request(_ProbeRequest())

        # Both registered, so both fired.
        assert len(shared_log) == 2  # noqa: PLR2004

        # Removing one leaves the other armed.
        event_manager.remove_post_dispatch_hook(_ProbeRequest, hook_b)
        shared_log.clear()
        event_manager.handle_request(_ProbeRequest())

        assert len(shared_log) == 1
        assert event_manager._post_dispatch_hooks[_ProbeRequest] == [hook_a]
        assert event_manager._post_dispatch_hooks[_ProbeRequest][0] is hook_a

    def test_hook_reissuing_its_own_request_type_does_not_recurse(self) -> None:
        def handler(_request: _ProbeRequest) -> _ProbeResult:
            return _ProbeResult(result_details="ok")

        event_manager = self._manager_with_handler(handler)

        calls: list[RequestPayload] = []

        def hook(request: RequestPayload, _result: ResultPayload) -> None:
            calls.append(request)
            # Hooks are told not to do this, but it must terminate if one does.
            event_manager.handle_request(_ProbeRequest())

        event_manager.add_post_dispatch_hook(_ProbeRequest, hook)

        event_manager.handle_request(_ProbeRequest())

        assert len(calls) == 1

    def test_hook_cycle_across_two_request_types_terminates(self) -> None:
        """The re-entrancy marker has to survive from one hook generation to the next.

        Each hook runs in a *fresh* context, which resets every ContextVar -- so the
        accumulated chain is threaded in explicitly rather than read from the context
        inside the hook. Without that, this pair alternates forever: each new task starts
        from an empty chain, matches nothing, and schedules the other hook again.
        """

        def handler(_request: RequestPayload) -> _ProbeResult:
            return _ProbeResult(result_details="ok")

        event_manager = EventManager()
        event_manager.assign_manager_to_request_type(_ProbeRequest, handler)
        event_manager.assign_manager_to_request_type(_CycleProbeRequest, handler)

        calls: list[str] = []
        # Bounded so a regression fails the assertion instead of hanging the suite.
        cap = 20

        def hook_on_probe(_request: RequestPayload, _result: ResultPayload) -> None:
            calls.append("probe")
            if len(calls) < cap:
                event_manager.handle_request(_CycleProbeRequest())

        def hook_on_cycle(_request: RequestPayload, _result: ResultPayload) -> None:
            calls.append("cycle")
            if len(calls) < cap:
                event_manager.handle_request(_ProbeRequest())

        event_manager.add_post_dispatch_hook(_ProbeRequest, hook_on_probe)
        event_manager.add_post_dispatch_hook(_CycleProbeRequest, hook_on_cycle)

        event_manager.handle_request(_ProbeRequest())

        assert calls == ["probe", "cycle"]

    def test_async_callable_object_hook_is_awaited(self) -> None:
        """A hook whose `__call__` is async must be awaited, not handed to a thread.

        `inspect.iscoroutinefunction` says False for an instance of such a class, so
        without the `type(callback).__call__` check the coroutine it returns is dropped on
        the floor and the hook silently does nothing. `RemoteHandler` has this shape.
        """

        def handler(_request: _ProbeRequest) -> _ProbeResult:
            return _ProbeResult(result_details="ok")

        event_manager = self._manager_with_handler(handler)

        seen: list[ResultPayload] = []
        hook = _AsyncRecordingHook(seen)
        assert not inspect.iscoroutinefunction(hook)  # Guards the premise.

        event_manager.add_post_dispatch_hook(_ProbeRequest, hook)
        event_manager.handle_request(_ProbeRequest())

        assert len(seen) == 1

    def test_omitted_request_fields_are_scrubbed_on_the_ordinary_path(self) -> None:
        """The guarantee is stated unconditionally, so pin it where most requests go.

        On this path it rests entirely on the fire site sitting *after* the scrub in
        `_handle_request_core`. Moving the fire earlier keeps every result-event test
        green while handing hooks the secret on every success.
        """

        def handler(_request: _OmitFieldProbeRequest) -> _ProbeResult:
            return _ProbeResult(result_details="ok")

        event_manager = EventManager()
        event_manager.assign_manager_to_request_type(_OmitFieldProbeRequest, handler)

        # Sampled inside the hook, not by holding the request: the scrub mutates the
        # object in place, so a saved reference reads post-scrub state whenever the
        # assertion runs and would pass even if the hook had been handed the secret.
        seen: list[str | None] = []

        def hook(request: RequestPayload, _result: ResultPayload) -> None:
            assert isinstance(request, _OmitFieldProbeRequest)
            seen.append(request.secret)

        event_manager.add_post_dispatch_hook(_OmitFieldProbeRequest, hook)

        event = event_manager.handle_request(_OmitFieldProbeRequest(secret="super-secret"))  # noqa: S106

        assert event.result.succeeded()
        assert seen == [None]

    def test_hook_runs_outside_the_dispatching_operation_depth(self) -> None:
        """Fired after the operation-depth context closes, so a hook does not inflate it.

        `OperationDepthManager._depth` is a plain counter -- neither thread-local nor
        context-local -- so a hook fired from inside the block would still be at depth 1.
        A request the hook then issued would reach depth 2, `is_top_level()` would be
        False, and its retained-mode echo would be silently suppressed.

        Uses its own `Engine` so the depth counter starts from a known zero.
        """
        engine = Engine()
        event_manager = engine.event_manager

        def handler(_request: _ProbeRequest) -> _ProbeResult:
            return _ProbeResult(result_details="ok")

        event_manager.assign_manager_to_request_type(_ProbeRequest, handler)

        depths: list[int] = []

        def hook(_request: RequestPayload, _result: ResultPayload) -> None:
            depths.append(engine.operation_depth_manager.get_depth())

        event_manager.add_post_dispatch_hook(_ProbeRequest, hook)

        event_manager.handle_request(_ProbeRequest())

        assert depths == [0]

    def test_omitted_request_fields_are_scrubbed_when_the_handler_raises(self) -> None:
        """The scrub lives on the result-building path, which a raising handler skips.

        `omit_from_result` exists to keep sensitive and bulky values out of anything
        downstream, so the exception path -- the one this feature advertises as its most
        interesting case -- must not be the hole that leaks them to a library.
        """

        def handler(_request: _OmitFieldProbeRequest) -> _ProbeResult:
            msg = "handler blew up"
            raise RuntimeError(msg)

        event_manager = EventManager()
        event_manager.assign_manager_to_request_type(_OmitFieldProbeRequest, handler)

        # Sampled inside the hook for the reason given in the test above.
        seen: list[str | None] = []

        def hook(request: RequestPayload, _result: ResultPayload) -> None:
            assert isinstance(request, _OmitFieldProbeRequest)
            seen.append(request.secret)

        event_manager.add_post_dispatch_hook(_OmitFieldProbeRequest, hook)

        with pytest.raises(RuntimeError):
            event_manager.handle_request(_OmitFieldProbeRequest(secret="super-secret"))  # noqa: S106

        assert seen == [None]

    def test_hook_runs_inline_when_the_stored_loop_is_open_but_never_driven(self) -> None:
        """`create_task` on an undriven loop returns a task that never executes.

        `asyncio.run` closes the loop it created, and nothing clears `_event_loop`, so a
        stale loop reference is a real possibility. Scheduling onto one must fall back to
        inline rather than silently dropping the hook.
        """

        def handler(_request: _ProbeRequest) -> _ProbeResult:
            return _ProbeResult(result_details="ok")

        event_manager = self._manager_with_handler(handler)

        seen: list[ResultPayload] = []
        event_manager.add_post_dispatch_hook(_ProbeRequest, _RecordingHook(seen))

        stale_loop = asyncio.new_event_loop()
        try:
            event_manager._event_loop = stale_loop
            event_manager.handle_request(_ProbeRequest())
        finally:
            stale_loop.close()

        assert len(seen) == 1

    def test_hook_runs_inline_when_the_stored_loop_is_closed(self) -> None:
        def handler(_request: _ProbeRequest) -> _ProbeResult:
            return _ProbeResult(result_details="ok")

        event_manager = self._manager_with_handler(handler)

        seen: list[ResultPayload] = []
        event_manager.add_post_dispatch_hook(_ProbeRequest, _RecordingHook(seen))

        closed_loop = asyncio.new_event_loop()
        closed_loop.close()
        event_manager._event_loop = closed_loop

        event_manager.handle_request(_ProbeRequest())

        assert len(seen) == 1

    def test_hook_completes_when_sync_dispatch_runs_inside_a_running_loop(self) -> None:
        """Regression guard for scheduling onto `asyncio.get_running_loop()`.

        Sync `handle_request` drives async work on a transient `ThreadRunner` side loop
        that stops as soon as the handler returns. A hook detached onto that loop would
        lose the race with teardown, so the inline path awaits it instead. An `AsyncMock`
        resolves synchronously and would mask the bug; this hook yields many times.
        """
        event_manager = EventManager()

        async def handler(_request: _ProbeRequest) -> _ProbeResult:
            return _ProbeResult(result_details="ok")

        event_manager.assign_manager_to_request_type(_ProbeRequest, handler)

        seen: list[ResultPayload] = []

        async def hook(_request: RequestPayload, result: ResultPayload) -> None:
            for _ in range(50):
                await asyncio.sleep(0)
            seen.append(result)

        event_manager.add_post_dispatch_hook(_ProbeRequest, hook)

        async def driver() -> None:
            # A running loop on this thread makes `handle_request` take the ThreadRunner
            # branch -- the same branch a sync handler hits in production.
            event_manager.handle_request(_ProbeRequest())

        asyncio.run(driver())

        assert len(seen) == 1

    @pytest.mark.asyncio
    async def test_hook_does_not_delay_the_result(self) -> None:
        """The point of the feature: the result comes back before the hook runs."""
        event_manager = EventManager()
        event_manager.initialize_queue(asyncio.Queue())

        async def handler(_request: _ProbeRequest) -> _ProbeResult:
            return _ProbeResult(result_details="ok")

        event_manager.assign_manager_to_request_type(_ProbeRequest, handler)

        seen: list[ResultPayload] = []

        async def hook(_request: RequestPayload, result: ResultPayload) -> None:
            await asyncio.sleep(0)
            seen.append(result)

        event_manager.add_post_dispatch_hook(_ProbeRequest, hook)

        event = await event_manager.ahandle_request(_ProbeRequest())

        assert event.result.succeeded()
        assert seen == []

        await _drain_post_dispatch_hooks(event_manager)

        assert len(seen) == 1

    @pytest.mark.asyncio
    async def test_detached_hook_task_is_retained_then_released(self) -> None:
        """A detached task needs a strong reference, because asyncio keeps only a weak one.

        Nothing else in the process references a detached hook task, so without the
        in-flight set it can be garbage collected mid-await and the hook silently stops
        part-way through. The set also must not grow without bound, hence the second half.
        """
        event_manager = EventManager()
        event_manager.initialize_queue(asyncio.Queue())

        async def handler(_request: _ProbeRequest) -> _ProbeResult:
            return _ProbeResult(result_details="ok")

        event_manager.assign_manager_to_request_type(_ProbeRequest, handler)

        async def hook(_request: RequestPayload, _result: ResultPayload) -> None:
            await asyncio.sleep(0)

        event_manager.add_post_dispatch_hook(_ProbeRequest, hook)

        await event_manager.ahandle_request(_ProbeRequest())

        # The task is created by a call_soon_threadsafe callback, so it does not exist
        # until the loop turns once.
        await asyncio.sleep(0)
        assert len(event_manager._inflight_post_dispatch_hook_tasks) == 1

        await _drain_post_dispatch_hooks(event_manager)

        assert event_manager._inflight_post_dispatch_hook_tasks == set()

    @pytest.mark.asyncio
    async def test_a_backlog_of_inflight_hooks_logs_a_warning(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Back-pressure is unbounded by design, so this log is the only signal of a backlog.

        Nothing is dropped or coalesced, which means a hook slower than its request rate
        accumulates silently unless this fires.
        """
        monkeypatch.setattr(
            "griptape_nodes.retained_mode.managers.event_manager.POST_DISPATCH_HOOK_INFLIGHT_WARNING_THRESHOLD",
            2,
        )
        event_manager = EventManager()
        event_manager.initialize_queue(asyncio.Queue())

        async def handler(_request: _ProbeRequest) -> _ProbeResult:
            return _ProbeResult(result_details="ok")

        event_manager.assign_manager_to_request_type(_ProbeRequest, handler)

        release = asyncio.Event()

        async def blocked_hook(_request: RequestPayload, _result: ResultPayload) -> None:
            await release.wait()

        event_manager.add_post_dispatch_hook(_ProbeRequest, blocked_hook)

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            for _ in range(3):
                await event_manager.ahandle_request(_ProbeRequest())
            await asyncio.sleep(0)

            # Guards the test itself: without three live tasks there is no backlog to warn
            # about and the assertion below would pass or fail for the wrong reason.
            assert len(event_manager._inflight_post_dispatch_hook_tasks) == 3  # noqa: PLR2004
            warnings = [r for r in caplog.records if "post-dispatch hooks are still running" in r.getMessage()]

        release.set()
        await _drain_post_dispatch_hooks(event_manager)

        assert warnings

    @pytest.mark.asyncio
    async def test_hook_sees_the_engine_that_dispatched_the_request(self) -> None:
        """The fresh context drops the engine binding, so the hook re-establishes it.

        Without it a hook reaching the engine through the `GriptapeNodes` facade -- which
        is how a library gets at managers -- would silently operate on the process-root
        engine instead of the one whose request it is observing.
        """
        engine = Engine()
        event_manager = engine.event_manager
        event_manager.initialize_queue(asyncio.Queue())

        # Not the ambient engine, so resolving to the process root would be visible.
        assert current_engine() is not engine

        async def handler(_request: _ProbeRequest) -> _ProbeResult:
            return _ProbeResult(result_details="ok")

        event_manager.assign_manager_to_request_type(_ProbeRequest, handler)

        seen_engines: list[Engine] = []

        async def hook(_request: RequestPayload, _result: ResultPayload) -> None:
            seen_engines.append(current_engine())

        event_manager.add_post_dispatch_hook(_ProbeRequest, hook)

        await event_manager.ahandle_request(_ProbeRequest())
        await _drain_post_dispatch_hooks(event_manager)

        assert seen_engines == [engine]

    @pytest.mark.asyncio
    async def test_every_dispatch_gets_its_own_hook_invocation(self) -> None:
        """Nothing is dropped or coalesced, however slow the hook is relative to dispatch."""
        event_manager = EventManager()
        event_manager.initialize_queue(asyncio.Queue())

        async def handler(_request: _ProbeRequest) -> _ProbeResult:
            return _ProbeResult(result_details="ok")

        event_manager.assign_manager_to_request_type(_ProbeRequest, handler)

        seen: list[ResultPayload] = []

        async def slow_hook(_request: RequestPayload, result: ResultPayload) -> None:
            for _ in range(5):
                await asyncio.sleep(0)
            seen.append(result)

        event_manager.add_post_dispatch_hook(_ProbeRequest, slow_hook)

        dispatch_count = 12
        for _ in range(dispatch_count):
            await event_manager.ahandle_request(_ProbeRequest())

        await _drain_post_dispatch_hooks(event_manager)

        assert len(seen) == dispatch_count

    @pytest.mark.asyncio
    async def test_sync_hook_does_not_run_on_the_engine_loop(self) -> None:
        """A blocking sync hook would stall the event queue, so it is handed to a thread."""
        event_manager = EventManager()
        event_manager.initialize_queue(asyncio.Queue())
        engine_loop_thread = threading.get_ident()

        async def handler(_request: _ProbeRequest) -> _ProbeResult:
            return _ProbeResult(result_details="ok")

        event_manager.assign_manager_to_request_type(_ProbeRequest, handler)

        hook_threads: list[int] = []

        def hook(_request: RequestPayload, _result: ResultPayload) -> None:
            hook_threads.append(threading.get_ident())

        event_manager.add_post_dispatch_hook(_ProbeRequest, hook)

        await event_manager.ahandle_request(_ProbeRequest())
        await _drain_post_dispatch_hooks(event_manager)

        assert len(hook_threads) == 1
        assert hook_threads[0] != engine_loop_thread


class TestAuthorizationCheckpointHooks:
    """The engine-side hook mechanism the app registers a policy into."""

    @staticmethod
    def _checkpoint() -> AuthorizationCheckpoint:
        return AuthorizationCheckpoint(
            action=CheckpointAction.LOAD_LIBRARY,
            subject_type=CheckpointSubjectType.LIBRARY,
            subject_id="lib",
            attributes={CheckpointAttribute.LIFECYCLE_STAGE: "LABS"},
        )

    def test_no_hooks_allows(self) -> None:
        assert EventManager().evaluate_authorization_checkpoint(self._checkpoint()) is None

    def test_hook_denial_is_returned(self) -> None:
        denial = CheckpointDenial(failures=(CheckpointFailure(detail="blocked", capability="cap"),))
        manager = EventManager()
        manager.add_authorization_hook(lambda _checkpoint: denial)
        assert manager.evaluate_authorization_checkpoint(self._checkpoint()) is denial

    def test_first_denial_wins_and_allowing_hooks_fall_through(self) -> None:
        denial = CheckpointDenial(failures=(CheckpointFailure(detail="second"),))
        manager = EventManager()
        manager.add_authorization_hook(lambda _checkpoint: None)
        manager.add_authorization_hook(lambda _checkpoint: denial)
        assert manager.evaluate_authorization_checkpoint(self._checkpoint()) is denial

    def test_hook_exception_fails_closed(self) -> None:
        def boom(_checkpoint: AuthorizationCheckpoint) -> None:
            msg = "boom"
            raise RuntimeError(msg)

        manager = EventManager()
        manager.add_authorization_hook(boom)
        denial = manager.evaluate_authorization_checkpoint(self._checkpoint())
        assert denial is not None
        assert "boom" in denial.failures[0].detail

    def test_remove_hook(self) -> None:
        manager = EventManager()

        def hook(_checkpoint: AuthorizationCheckpoint) -> None:
            return None

        manager.add_authorization_hook(hook)
        manager.remove_authorization_hook(hook)
        # Removing again is a no-op, not an error.
        manager.remove_authorization_hook(hook)
        assert manager.evaluate_authorization_checkpoint(self._checkpoint()) is None

    def test_reentrant_hook_is_bypassed_not_recursive(self) -> None:
        manager = EventManager()
        hook_calls: list[AuthorizationCheckpoint] = []

        def hook(checkpoint: AuthorizationCheckpoint) -> None:
            hook_calls.append(checkpoint)
            # Re-enter the checkpoint from inside the hook. The nested call must
            # bypass the chain rather than re-trigger this hook and recurse.
            if len(hook_calls) == 1:
                assert manager.evaluate_authorization_checkpoint(self._checkpoint()) is None

        manager.add_authorization_hook(hook)

        assert manager.evaluate_authorization_checkpoint(self._checkpoint()) is None
        # Hook ran only for the outer checkpoint; the re-entrant call skipped it.
        assert len(hook_calls) == 1

    def test_hook_error_does_not_wedge_later_evaluation(self) -> None:
        manager = EventManager()
        calls: list[int] = []

        def hook(_checkpoint: AuthorizationCheckpoint) -> None:
            calls.append(1)
            if len(calls) == 1:
                msg = "boom"
                raise RuntimeError(msg)

        manager.add_authorization_hook(hook)

        # First evaluation is denied by the erroring hook...
        first = manager.evaluate_authorization_checkpoint(self._checkpoint())
        assert first is not None

        # ...and the thread-local guard is cleared, so the next evaluation still
        # runs the chain and allows.
        second = manager.evaluate_authorization_checkpoint(self._checkpoint())
        assert second is None
        assert len(calls) == 2  # noqa: PLR2004


@dataclass
class _FakeStreamEvent(ExecutionPayload):
    """Minimal ExecutionPayload for exercising the execution-event feed."""

    text: str = ""


@dataclass
class _OtherExecEvent(ExecutionPayload):
    """A second ExecutionPayload type to prove type-scoped delivery."""

    value: int = 0


@dataclass
class _ProbeAppEvent(AppPayload):
    """Minimal AppPayload for exercising emit_app."""

    label: str = ""


class TestEmitApi:
    """`emit_execution`/`emit_app` own both the wrapping and the identity stamp.

    A caller cannot build the event without both: no hand-built
    `ExecutionGriptapeNodeEvent(wrapped_event=ExecutionEvent(payload=...))` nesting to get
    wrong, and no stamp to forget.
    """

    @staticmethod
    def _engine() -> Engine:
        engine = Engine()
        engine.engine_identity_manager.active_engine_id = "engine-a"
        engine.session_manager.active_session_id = "session-a"
        engine.event_manager.initialize_queue(asyncio.Queue())
        return engine

    def test_emit_execution_wraps_the_payload_for_the_transport(self) -> None:
        engine = self._engine()

        engine.event_manager.emit_execution(_FakeStreamEvent(text="hi"))

        event = engine.event_manager.event_queue.get_nowait()
        assert isinstance(event, ExecutionGriptapeNodeEvent)
        assert isinstance(event.wrapped_event, ExecutionEvent)
        assert event.wrapped_event.payload.text == "hi"

    def test_emit_execution_stamps_the_event_that_reaches_the_wire(self) -> None:
        """The transport publishes `wrapped_event`, so the inner event is the one that matters."""
        engine = self._engine()

        engine.event_manager.emit_execution(_FakeStreamEvent(text="hi"))

        event = engine.event_manager.event_queue.get_nowait()
        assert (event.wrapped_event.engine_id, event.wrapped_event.session_id) == ("engine-a", "session-a")

    @pytest.mark.asyncio
    async def test_aemit_execution_matches_the_sync_form(self) -> None:
        engine = self._engine()

        await engine.event_manager.aemit_execution(_FakeStreamEvent(text="hi"))

        event = engine.event_manager.event_queue.get_nowait()
        assert event.wrapped_event.payload.text == "hi"
        assert (event.wrapped_event.engine_id, event.wrapped_event.session_id) == ("engine-a", "session-a")

    def test_emit_app_wraps_and_stamps(self) -> None:
        engine = self._engine()

        engine.event_manager.emit_app(_ProbeAppEvent(label="ready"))

        event = engine.event_manager.event_queue.get_nowait()
        assert isinstance(event, AppEvent)
        assert event.payload.label == "ready"
        assert (event.engine_id, event.session_id) == ("engine-a", "session-a")

    @pytest.mark.asyncio
    async def test_aemit_app_matches_the_sync_form(self) -> None:
        engine = self._engine()

        await engine.event_manager.aemit_app(_ProbeAppEvent(label="ready"))

        event = engine.event_manager.event_queue.get_nowait()
        assert event.payload.label == "ready"
        assert (event.engine_id, event.session_id) == ("engine-a", "session-a")

    def test_emit_execution_still_reaches_execution_listeners(self) -> None:
        """The emit path must keep feeding in-process execution-event subscribers."""
        engine = self._engine()
        received: list[str] = []
        engine.event_manager.add_listener_to_execution_event(_FakeStreamEvent, lambda p: received.append(p.text))

        engine.event_manager.emit_execution(_FakeStreamEvent(text="hi"))

        assert received == ["hi"]


class TestExecutionEventSubscription:
    """Nodes can tap the live execution-event feed via add_listener_to_execution_event."""

    @staticmethod
    def _wrap(payload: ExecutionPayload) -> ExecutionGriptapeNodeEvent:
        return ExecutionGriptapeNodeEvent(wrapped_event=ExecutionEvent(payload=payload))

    @pytest.mark.asyncio
    async def test_listener_receives_matching_payloads_in_order(self) -> None:
        manager = EventManager()
        manager.initialize_queue(asyncio.Queue())
        received: list[str] = []
        manager.add_listener_to_execution_event(_FakeStreamEvent, lambda p: received.append(p.text))

        manager.put_event(self._wrap(_FakeStreamEvent(text="he")))
        manager.put_event(self._wrap(_FakeStreamEvent(text="llo")))

        assert received == ["he", "llo"]

    @pytest.mark.asyncio
    async def test_only_subscribed_payload_type_is_delivered(self) -> None:
        manager = EventManager()
        manager.initialize_queue(asyncio.Queue())
        received: list[str] = []
        manager.add_listener_to_execution_event(_FakeStreamEvent, lambda p: received.append(p.text))

        manager.put_event(self._wrap(_OtherExecEvent(value=1)))

        assert received == []

    @pytest.mark.asyncio
    async def test_remove_listener_stops_delivery(self) -> None:
        manager = EventManager()
        manager.initialize_queue(asyncio.Queue())
        received: list[str] = []

        def callback(payload: _FakeStreamEvent) -> None:
            received.append(payload.text)

        manager.add_listener_to_execution_event(_FakeStreamEvent, callback)
        manager.remove_listener_for_execution_event(_FakeStreamEvent, callback)

        manager.put_event(self._wrap(_FakeStreamEvent(text="x")))

        assert received == []

    @pytest.mark.asyncio
    async def test_listener_exception_does_not_break_delivery(self) -> None:
        manager = EventManager()
        manager.initialize_queue(asyncio.Queue())
        received: list[str] = []

        def boom(_payload: _FakeStreamEvent) -> None:
            msg = "boom"
            raise RuntimeError(msg)

        manager.add_listener_to_execution_event(_FakeStreamEvent, boom)
        manager.add_listener_to_execution_event(_FakeStreamEvent, lambda p: received.append(p.text))

        manager.put_event(self._wrap(_FakeStreamEvent(text="ok")))

        assert received == ["ok"]

    @pytest.mark.asyncio
    async def test_aput_event_dispatches_to_listener(self) -> None:
        manager = EventManager()
        manager.initialize_queue(asyncio.Queue())
        received: list[str] = []
        manager.add_listener_to_execution_event(_FakeStreamEvent, lambda p: received.append(p.text))

        await manager.aput_event(self._wrap(_FakeStreamEvent(text="async")))

        assert received == ["async"]

    @pytest.mark.asyncio
    async def test_duplicate_add_is_idempotent(self) -> None:
        manager = EventManager()
        manager.initialize_queue(asyncio.Queue())
        received: list[str] = []

        def callback(payload: _FakeStreamEvent) -> None:
            received.append(payload.text)

        manager.add_listener_to_execution_event(_FakeStreamEvent, callback)
        manager.add_listener_to_execution_event(_FakeStreamEvent, callback)

        manager.put_event(self._wrap(_FakeStreamEvent(text="once")))

        assert received == ["once"]

    @pytest.mark.asyncio
    async def test_remove_unregistered_callback_is_noop(self) -> None:
        manager = EventManager()
        manager.initialize_queue(asyncio.Queue())

        # Removing a callback that was never registered (and an unknown type) must not raise.
        manager.remove_listener_for_execution_event(_FakeStreamEvent, lambda _p: None)
        manager.remove_listener_for_execution_event(_OtherExecEvent, lambda _p: None)

    @pytest.mark.asyncio
    async def test_concurrent_emit_and_subscribe_is_thread_safe(self) -> None:
        manager = EventManager()
        manager.initialize_queue(asyncio.Queue())
        received: list[str] = []
        received_lock = threading.Lock()

        def callback(payload: _FakeStreamEvent) -> None:
            with received_lock:
                received.append(payload.text)

        manager.add_listener_to_execution_event(_FakeStreamEvent, callback)

        stop = threading.Event()

        def emit() -> None:
            while not stop.is_set():
                manager.put_event(self._wrap(_FakeStreamEvent(text="t")))

        # A worker thread emits continuously (the production path) while the main thread
        # churns subscribe/unsubscribe. This must never raise "dict changed size during
        # iteration" or corrupt the listener registry.
        emitter = threading.Thread(target=emit)
        emitter.start()
        try:
            for _ in range(500):
                extra = lambda _p: None  # noqa: E731
                manager.add_listener_to_execution_event(_FakeStreamEvent, extra)
                manager.remove_listener_for_execution_event(_FakeStreamEvent, extra)
        finally:
            stop.set()
            emitter.join()

        # The originally-registered callback survives the churn and keeps receiving.
        with received_lock:
            baseline = len(received)
        manager.put_event(self._wrap(_FakeStreamEvent(text="final")))
        with received_lock:
            assert len(received) == baseline + 1
            assert received[-1] == "final"

    @pytest.mark.asyncio
    async def test_base_class_subscription_does_not_receive_subclasses(self) -> None:
        manager = EventManager()
        manager.initialize_queue(asyncio.Queue())
        received: list[ExecutionPayload] = []
        # Only the exact payload type is matched; subscribing to the base ExecutionPayload
        # must not receive concrete subclasses like _FakeStreamEvent.
        manager.add_listener_to_execution_event(ExecutionPayload, received.append)

        manager.put_event(self._wrap(_FakeStreamEvent(text="x")))

        assert received == []

    @pytest.mark.asyncio
    async def test_async_callback_is_rejected(self) -> None:
        manager = EventManager()
        manager.initialize_queue(asyncio.Queue())

        async def async_callback(_payload: _FakeStreamEvent) -> None:
            pass

        with pytest.raises(TypeError, match="coroutine"):
            manager.add_listener_to_execution_event(_FakeStreamEvent, async_callback)  # pyright: ignore[reportArgumentType]

    @pytest.mark.asyncio
    async def test_async_callable_object_is_rejected(self) -> None:
        manager = EventManager()
        manager.initialize_queue(asyncio.Queue())

        class AsyncCallable:
            async def __call__(self, _payload: _FakeStreamEvent) -> None:
                pass

        with pytest.raises(TypeError, match="coroutine"):
            manager.add_listener_to_execution_event(_FakeStreamEvent, AsyncCallable())  # pyright: ignore[reportArgumentType]

    @pytest.mark.asyncio
    async def test_reentrant_emission_is_enqueued_after_trigger(self) -> None:
        manager = EventManager()
        queue: asyncio.Queue = asyncio.Queue()
        manager.initialize_queue(queue)

        # A callback that re-enters put_event (as a node writing a streamed token would)
        # must enqueue its follow-up event *after* the event that triggered it.
        def on_stream(_payload: _FakeStreamEvent) -> None:
            manager.put_event(self._wrap(_OtherExecEvent(value=99)))

        manager.add_listener_to_execution_event(_FakeStreamEvent, on_stream)

        trigger = self._wrap(_FakeStreamEvent(text="a"))
        manager.put_event(trigger)

        first = queue.get_nowait()
        second = queue.get_nowait()
        assert first is trigger
        assert isinstance(second.wrapped_event.payload, _OtherExecEvent)

    @pytest.mark.asyncio
    async def test_non_execution_events_are_ignored(self) -> None:
        manager = EventManager()
        manager.initialize_queue(asyncio.Queue())
        received: list[ExecutionPayload] = []
        manager.add_listener_to_execution_event(_FakeStreamEvent, received.append)

        # A ProgressEvent is not an ExecutionGriptapeNodeEvent; it must neither crash
        # dispatch nor be delivered to execution listeners.
        manager.put_event(ProgressEvent(value="x", node_name="n", parameter_name="output"))

        assert received == []
