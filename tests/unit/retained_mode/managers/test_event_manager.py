"""Test EventManager functionality including sync/async event broadcasting."""

import asyncio
import logging
import threading
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from griptape_nodes.app.worker_routing import RemoteHandler
from griptape_nodes.retained_mode.events.app_events import ConfigChanged
from griptape_nodes.retained_mode.events.base_events import (
    EventResultSuccess,
    RequestPayload,
    ResultDetail,
    ResultDetails,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    StrictModeViolationDetail,
)
from griptape_nodes.retained_mode.events.generic_events import GenericResultFailure
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry
from griptape_nodes.retained_mode.managers.authorization_checkpoint import (
    AuthorizationCheckpoint,
    CheckpointDenial,
    CheckpointFailure,
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


class TestAuthorizationCheckpointHooks:
    """The engine-side hook mechanism the app registers a policy into."""

    @staticmethod
    def _checkpoint() -> AuthorizationCheckpoint:
        return AuthorizationCheckpoint(
            action="LoadLibrary",
            subject_type="Library",
            subject_id="lib",
            attributes={"lifecycle_stage": "LABS"},
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
