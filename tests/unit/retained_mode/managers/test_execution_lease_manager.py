"""Tests for ExecutionLeaseManager: the engine-side lease client and its watchdog.

A stub balancer (in-memory transport) plays the admission authority, and the
running-flow signal is monkeypatched on the engine's FlowManager, so every
lease-lifecycle path is drivable without starting a real flow. These pin the
engine-side invariants the design owes regardless of who wrote the balancer:
fail-closed, no duplicate acquires, watchdog-owned release, teardown before
release, and lease-loss handling.
"""

from __future__ import annotations

import asyncio
import os
import signal
from typing import TYPE_CHECKING, Any

import pytest

from griptape_nodes.retained_mode.events.app_events import SessionHeartbeatRequest
from griptape_nodes.retained_mode.events.execution_lease_events import ExecutionLeaseReleasing
from griptape_nodes.retained_mode.managers.authorization_checkpoint import (
    AuthorizationCheckpoint,
    CheckpointAction,
    CheckpointAttribute,
    CheckpointDenial,
    CheckpointFailure,
    CheckpointSubjectType,
)
from griptape_nodes.retained_mode.managers.execution_lease_manager import ExecutionLeaseManager
from griptape_nodes.retained_mode.managers.settings import (
    EXECUTION_LEASE_ENABLED_KEY,
    EXECUTION_LEASE_RENEW_INTERVAL_KEY,
    EXECUTION_LEASE_TEARDOWN_TIMEOUT_KEY,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine


class StubBalancer:
    """In-memory admission authority: records every request, grants on demand."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.grant_gate = asyncio.Event()
        self.grant_gate.set()  # grant immediately unless a test blocks it
        self.acquire_result_type = "AcquireExecutionLeaseResultSuccess"
        self.renew_result_type = "RenewExecutionLeaseResultSuccess"

    async def send_request(self, request_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.requests.append((request_type, payload))
        if request_type == "AcquireExecutionLeaseRequest":
            await self.grant_gate.wait()
            details = "granted" if "Success" in self.acquire_result_type else "refused by test"
            return {
                "result_type": self.acquire_result_type,
                # The real wire shape: result_details is an unstructured
                # ResultDetails object nested under the envelope's result key.
                "result": {"result_details": {"result_details": [{"level": 10, "message": details}]}},
                "lease_id": payload["lease_id"],
            }
        if request_type == "RenewExecutionLeaseRequest":
            return {"result_type": self.renew_result_type, "result": {"result_details": "renewed"}}
        return {
            "result_type": f"{request_type.removesuffix('Request')}ResultSuccess",
            "result": {"result_details": "ok"},
        }

    async def send_no_response(self, request_type: str, payload: dict[str, Any]) -> None:
        self.requests.append((request_type, payload))

    def sent(self, request_type: str) -> list[dict[str, Any]]:
        return [payload for sent_type, payload in self.requests if sent_type == request_type]


class RunningFlowSignal:
    """Scriptable stand-in for FlowManager.check_for_existing_running_flow."""

    def __init__(self) -> None:
        self.running = False

    def __call__(self) -> bool:
        return self.running


def make_manager(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    *,
    renew_interval_s: float = 30.0,
    startup_grace_s: float = 0.5,
    teardown_timeout_s: float = 2.0,
) -> tuple[ExecutionLeaseManager, StubBalancer, RunningFlowSignal]:
    """Build a manager against the stub balancer with test-scaled timings; always enabled."""
    engine.config_manager.set_config_value(EXECUTION_LEASE_ENABLED_KEY, value=True)
    engine.config_manager.set_config_value(EXECUTION_LEASE_RENEW_INTERVAL_KEY, value=renew_interval_s)
    engine.config_manager.set_config_value(EXECUTION_LEASE_TEARDOWN_TIMEOUT_KEY, value=teardown_timeout_s)
    monkeypatch.setattr(ExecutionLeaseManager, "_STARTUP_GRACE_S", startup_grace_s)
    monkeypatch.setattr(ExecutionLeaseManager, "_POLL_INTERVAL_S", 0.01)

    signal = RunningFlowSignal()
    monkeypatch.setattr(engine.flow_manager, "check_for_existing_running_flow", signal)

    manager = ExecutionLeaseManager(engine=engine)
    balancer = StubBalancer()
    manager.attach_transport(send_request=balancer.send_request, send_no_response=balancer.send_no_response)
    # The gate requires evidence a balancer is connected (its beacons are the
    # only connection signal, since it dials in); stamp one for these tests.
    manager._on_balancer_heartbeat(None)
    return manager, balancer, signal


async def wait_for(condition, deadline_s: float = 2.0) -> None:  # noqa: ANN001
    """Poll `condition` until true or fail the test after `deadline_s` seconds."""
    deadline = asyncio.get_event_loop().time() + deadline_s
    while not condition():
        if asyncio.get_event_loop().time() > deadline:
            pytest.fail("Condition not met within timeout")
        await asyncio.sleep(0.01)


class TestGateDisabledAndFailClosed:
    @pytest.mark.asyncio
    async def test_disabled_gate_is_a_no_op(self, engine: Engine) -> None:
        engine.config_manager.set_config_value(EXECUTION_LEASE_ENABLED_KEY, value=False)
        manager = ExecutionLeaseManager(engine=engine)

        await manager.gate_execution_start()  # no transport attached; must not raise

        assert manager.lease_id is None

    @pytest.mark.asyncio
    async def test_enabled_without_transport_fails_closed(self, engine: Engine) -> None:
        engine.config_manager.set_config_value(EXECUTION_LEASE_ENABLED_KEY, value=True)
        manager = ExecutionLeaseManager(engine=engine)

        with pytest.raises(RuntimeError, match="refuses to run unmanaged"):
            await manager.gate_execution_start()

    @pytest.mark.asyncio
    async def test_transport_error_fails_closed(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        manager, _, _ = make_manager(engine, monkeypatch)

        async def broken(_request_type: str, _payload: dict[str, Any]) -> dict[str, Any]:
            msg = "connection refused"
            raise ConnectionError(msg)

        async def broken_no_response(_request_type: str, _payload: dict[str, Any]) -> None:
            pass

        manager.attach_transport(send_request=broken, send_no_response=broken_no_response)

        with pytest.raises(RuntimeError, match="could not be reached"):
            await manager.gate_execution_start()
        assert manager.lease_id is None


class TestAcquire:
    @pytest.mark.asyncio
    async def test_grant_holds_lease_and_sends_engine_identity(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager, balancer, _ = make_manager(engine, monkeypatch)

        await manager.gate_execution_start(scope="single_node")

        assert manager.lease_id is not None
        acquire = balancer.sent("AcquireExecutionLeaseRequest")[0]
        assert acquire["lease_id"] == manager.lease_id
        assert acquire["scope"] == "single_node"
        assert acquire["engine_id"]
        manager._cancel_watchdog()

    @pytest.mark.asyncio
    async def test_refused_acquire_raises_with_reason(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        manager, balancer, _ = make_manager(engine, monkeypatch)
        balancer.acquire_result_type = "AcquireExecutionLeaseResultFailure"

        with pytest.raises(RuntimeError, match="refused by test"):
            await manager.gate_execution_start()
        assert manager.lease_id is None

    @pytest.mark.asyncio
    async def test_second_gate_while_waiting_raises(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        """The double-acquire race: two run requests both passed the running guard."""
        manager, balancer, _ = make_manager(engine, monkeypatch)
        balancer.grant_gate.clear()  # first acquire waits in line

        first = asyncio.create_task(manager.gate_execution_start())
        await wait_for(lambda: len(balancer.sent("AcquireExecutionLeaseRequest")) == 1)

        with pytest.raises(RuntimeError, match="already waiting"):
            await manager.gate_execution_start()

        balancer.grant_gate.set()
        await first
        manager._cancel_watchdog()

    @pytest.mark.asyncio
    async def test_post_grant_recheck_returns_lease_when_run_raced(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If a run started while the acquire waited, the grant is handed back."""
        manager, balancer, signal = make_manager(engine, monkeypatch)
        balancer.grant_gate.clear()

        gate_task = asyncio.create_task(manager.gate_execution_start())
        await wait_for(lambda: len(balancer.sent("AcquireExecutionLeaseRequest")) == 1)
        signal.running = True  # another run wins the race before the grant lands
        balancer.grant_gate.set()

        with pytest.raises(RuntimeError, match="another run started"):
            await gate_task
        assert manager.lease_id is None
        assert len(balancer.sent("ReleaseExecutionLeaseRequest")) == 1

    @pytest.mark.asyncio
    async def test_cancelled_wait_abandons_acquire(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        manager, balancer, _ = make_manager(engine, monkeypatch)
        balancer.grant_gate.clear()

        gate_task = asyncio.create_task(manager.gate_execution_start())
        await wait_for(lambda: len(balancer.sent("AcquireExecutionLeaseRequest")) == 1)
        minted_lease_id = balancer.sent("AcquireExecutionLeaseRequest")[0]["lease_id"]

        gate_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await gate_task

        await wait_for(lambda: len(balancer.sent("CancelExecutionLeaseRequest")) == 1)
        assert balancer.sent("CancelExecutionLeaseRequest")[0]["lease_id"] == minted_lease_id
        assert manager.lease_id is None


class TestAdmissionEntitlement:
    """The paid-tier gate: an authorization hook can deny managed execution."""

    @pytest.mark.asyncio
    async def test_denied_checkpoint_refuses_the_start_before_any_acquire(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager, balancer, _ = make_manager(engine, monkeypatch)
        seen: list[AuthorizationCheckpoint] = []

        def denying_hook(checkpoint: AuthorizationCheckpoint) -> CheckpointDenial | None:
            seen.append(checkpoint)
            return CheckpointDenial(failures=(CheckpointFailure(detail="Your license does not include GPU queueing."),))

        engine.event_manager.add_authorization_hook(denying_hook)
        try:
            with pytest.raises(RuntimeError, match="does not include GPU queueing"):
                await manager.gate_execution_start(scope="single_node")
        finally:
            engine.event_manager.remove_authorization_hook(denying_hook)

        assert len(balancer.sent("AcquireExecutionLeaseRequest")) == 0
        assert manager.lease_id is None
        checkpoint = seen[0]
        assert checkpoint.action == CheckpointAction.ACQUIRE_EXECUTION_LEASE
        assert checkpoint.subject_type == CheckpointSubjectType.EXECUTION
        assert checkpoint.attributes[CheckpointAttribute.SCOPE] == "single_node"

    @pytest.mark.asyncio
    async def test_allowing_hook_lets_the_acquire_proceed(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager, balancer, _ = make_manager(engine, monkeypatch)

        def allowing_hook(_checkpoint: AuthorizationCheckpoint) -> CheckpointDenial | None:
            return None

        engine.event_manager.add_authorization_hook(allowing_hook)
        try:
            await manager.gate_execution_start()
        finally:
            engine.event_manager.remove_authorization_hook(allowing_hook)

        assert manager.lease_id is not None
        assert len(balancer.sent("AcquireExecutionLeaseRequest")) == 1
        manager._cancel_watchdog()

    @pytest.mark.asyncio
    async def test_unmanaged_engine_never_asks_the_checkpoint(self, engine: Engine) -> None:
        engine.config_manager.set_config_value(EXECUTION_LEASE_ENABLED_KEY, value=False)
        manager = ExecutionLeaseManager(engine=engine)
        seen: list[AuthorizationCheckpoint] = []

        def recording_hook(checkpoint: AuthorizationCheckpoint) -> CheckpointDenial | None:
            seen.append(checkpoint)
            return None

        engine.event_manager.add_authorization_hook(recording_hook)
        try:
            await manager.gate_execution_start()
        finally:
            engine.event_manager.remove_authorization_hook(recording_hook)

        assert seen == []


class TestCancelWhileQueued:
    @pytest.mark.asyncio
    async def test_cancel_with_no_pending_acquire_returns_false(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager, balancer, _ = make_manager(engine, monkeypatch)

        assert await manager.cancel_pending_acquire() is False
        assert len(balancer.sent("CancelExecutionLeaseRequest")) == 0

    @pytest.mark.asyncio
    async def test_cancel_is_a_noop_when_unmanaged(self, engine: Engine) -> None:
        engine.config_manager.set_config_value(EXECUTION_LEASE_ENABLED_KEY, value=False)
        manager = ExecutionLeaseManager(engine=engine)

        assert await manager.cancel_pending_acquire() is False

    @pytest.mark.asyncio
    async def test_cancel_unblocks_the_waiting_gate(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        """Cancel-while-queued: the blocked gate resolves as a refused start."""
        manager, balancer, _ = make_manager(engine, monkeypatch)
        balancer.grant_gate.clear()  # acquire waits in line

        gate_task = asyncio.create_task(manager.gate_execution_start())
        await wait_for(lambda: len(balancer.sent("AcquireExecutionLeaseRequest")) == 1)
        minted_lease_id = balancer.sent("AcquireExecutionLeaseRequest")[0]["lease_id"]

        assert await manager.cancel_pending_acquire() is True
        assert balancer.sent("CancelExecutionLeaseRequest")[0]["lease_id"] == minted_lease_id
        # The real balancer answers the abandoned acquire with a failure.
        balancer.acquire_result_type = "AcquireExecutionLeaseResultFailure"
        balancer.grant_gate.set()

        with pytest.raises(RuntimeError, match="cancelled while it was waiting"):
            await gate_task
        assert manager.lease_id is None

    @pytest.mark.asyncio
    async def test_grant_racing_the_cancel_is_handed_back(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A grant that lands after the cancel must be returned, never run."""
        manager, balancer, _ = make_manager(engine, monkeypatch)
        balancer.grant_gate.clear()

        gate_task = asyncio.create_task(manager.gate_execution_start())
        await wait_for(lambda: len(balancer.sent("AcquireExecutionLeaseRequest")) == 1)

        assert await manager.cancel_pending_acquire() is True
        balancer.grant_gate.set()  # balancer grants anyway: the race the engine must not trust

        with pytest.raises(RuntimeError, match="cancelled while it was waiting"):
            await gate_task
        assert manager.lease_id is None
        assert len(balancer.sent("ReleaseExecutionLeaseRequest")) == 1

    @pytest.mark.asyncio
    async def test_gate_works_again_after_a_cancelled_wait(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager, balancer, _ = make_manager(engine, monkeypatch)
        balancer.grant_gate.clear()

        gate_task = asyncio.create_task(manager.gate_execution_start())
        await wait_for(lambda: len(balancer.sent("AcquireExecutionLeaseRequest")) == 1)
        assert await manager.cancel_pending_acquire() is True
        balancer.acquire_result_type = "AcquireExecutionLeaseResultFailure"
        balancer.grant_gate.set()
        with pytest.raises(RuntimeError, match="cancelled while it was waiting"):
            await gate_task

        balancer.acquire_result_type = "AcquireExecutionLeaseResultSuccess"
        await manager.gate_execution_start()

        assert manager.lease_id is not None
        manager._cancel_watchdog()


class TestWatchdogRelease:
    @pytest.mark.asyncio
    async def test_release_after_run_completes(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        manager, balancer, signal = make_manager(engine, monkeypatch)

        await manager.gate_execution_start()
        signal.running = True  # the run starts...
        await asyncio.sleep(0.05)
        signal.running = False  # ...and finishes

        await wait_for(lambda: len(balancer.sent("ReleaseExecutionLeaseRequest")) == 1)
        assert manager.lease_id is None

    @pytest.mark.asyncio
    async def test_release_when_run_never_starts(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        """Startup-grace backstop: a lease whose run never launched is returned."""
        manager, balancer, _ = make_manager(engine, monkeypatch, startup_grace_s=0.1)

        await manager.gate_execution_start()

        await wait_for(lambda: len(balancer.sent("ReleaseExecutionLeaseRequest")) == 1)
        assert manager.lease_id is None

    @pytest.mark.asyncio
    async def test_on_execution_start_failed_releases_immediately(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager, balancer, _ = make_manager(engine, monkeypatch, startup_grace_s=60.0)

        await manager.gate_execution_start()
        await manager.on_execution_start_failed()

        assert manager.lease_id is None
        assert len(balancer.sent("ReleaseExecutionLeaseRequest")) == 1

    @pytest.mark.asyncio
    async def test_teardown_runs_before_release_is_sent(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ordering is the contract: memory is reclaimed BEFORE the next admission."""
        manager, balancer, signal = make_manager(engine, monkeypatch)
        order: list[str] = []

        async def recording_teardown() -> None:
            order.append("teardown")

        monkeypatch.setattr(manager, "_run_pre_release_teardown", recording_teardown)
        original_send = balancer.send_request

        async def recording_send(request_type: str, payload: dict[str, Any]) -> dict[str, Any]:
            if request_type == "ReleaseExecutionLeaseRequest":
                order.append("release")
            return await original_send(request_type, payload)

        manager.attach_transport(send_request=recording_send, send_no_response=balancer.send_no_response)

        await manager.gate_execution_start()
        signal.running = True
        await asyncio.sleep(0.05)
        signal.running = False

        await wait_for(lambda: "release" in order)
        assert order == ["teardown", "release"]

    @pytest.mark.asyncio
    async def test_held_lease_is_reused_for_back_to_back_runs(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Queued starts draining while the lease is still held must not re-acquire."""
        manager, balancer, signal = make_manager(engine, monkeypatch, startup_grace_s=60.0)

        await manager.gate_execution_start()
        signal.running = False  # previous run finished; watchdog has not observed idle yet
        await manager.gate_execution_start()  # next queued start

        assert len(balancer.sent("AcquireExecutionLeaseRequest")) == 1
        manager._cancel_watchdog()


class TestRenewal:
    @pytest.mark.asyncio
    async def test_renew_fires_while_run_is_live(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        manager, balancer, signal = make_manager(engine, monkeypatch, renew_interval_s=0.05)

        await manager.gate_execution_start()
        signal.running = True

        await wait_for(lambda: len(balancer.sent("RenewExecutionLeaseRequest")) >= 1)
        signal.running = False
        await wait_for(lambda: len(balancer.sent("ReleaseExecutionLeaseRequest")) == 1)

    @pytest.mark.asyncio
    async def test_lost_lease_is_not_released(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        """A refused renewal means the balancer reclaimed the lease; there is nothing to return."""
        manager, balancer, signal = make_manager(engine, monkeypatch, renew_interval_s=0.05)
        balancer.renew_result_type = "RenewExecutionLeaseResultFailure"

        await manager.gate_execution_start()
        signal.running = True
        await wait_for(lambda: len(balancer.sent("RenewExecutionLeaseRequest")) >= 1)
        signal.running = False

        await wait_for(lambda: manager.lease_id is None)
        assert len(balancer.sent("ReleaseExecutionLeaseRequest")) == 0


class TestTeardownBroadcast:
    """The real _run_pre_release_teardown: ExecutionLeaseReleasing listeners."""

    @pytest.mark.asyncio
    async def test_listener_completes_before_release(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        manager, balancer, signal = make_manager(engine, monkeypatch)
        order: list[str] = []

        async def slow_teardown(event: ExecutionLeaseReleasing) -> None:
            await asyncio.sleep(0.05)  # long enough that fire-and-forget would lose the race
            order.append(f"teardown:{event.scope}")

        engine.event_manager.add_listener_to_app_event(ExecutionLeaseReleasing, slow_teardown)
        original_send = balancer.send_request

        async def recording_send(request_type: str, payload: dict[str, Any]) -> dict[str, Any]:
            if request_type == "ReleaseExecutionLeaseRequest":
                order.append("release")
            return await original_send(request_type, payload)

        manager.attach_transport(send_request=recording_send, send_no_response=balancer.send_no_response)
        try:
            await manager.gate_execution_start(scope="single_node")
            signal.running = True
            await asyncio.sleep(0.05)
            signal.running = False

            await wait_for(lambda: "release" in order)
            assert order == ["teardown:single_node", "release"]
        finally:
            engine.event_manager.remove_listener_for_app_event(ExecutionLeaseReleasing, slow_teardown)

    @pytest.mark.asyncio
    async def test_raising_listener_does_not_block_release(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager, balancer, signal = make_manager(engine, monkeypatch)

        async def broken_teardown(_event: ExecutionLeaseReleasing) -> None:
            msg = "cache clear failed"
            raise RuntimeError(msg)

        engine.event_manager.add_listener_to_app_event(ExecutionLeaseReleasing, broken_teardown)
        try:
            await manager.gate_execution_start()
            signal.running = True
            await asyncio.sleep(0.05)
            signal.running = False

            await wait_for(lambda: len(balancer.sent("ReleaseExecutionLeaseRequest")) == 1)
            assert manager.lease_id is None
        finally:
            engine.event_manager.remove_listener_for_app_event(ExecutionLeaseReleasing, broken_teardown)

    @pytest.mark.asyncio
    async def test_hung_listener_is_abandoned_at_timeout(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        """A wedged teardown must not hold the admission queue forever."""
        manager, balancer, signal = make_manager(engine, monkeypatch, teardown_timeout_s=0.1)

        async def hung_teardown(_event: ExecutionLeaseReleasing) -> None:
            await asyncio.sleep(30)

        engine.event_manager.add_listener_to_app_event(ExecutionLeaseReleasing, hung_teardown)
        try:
            await manager.gate_execution_start()
            signal.running = True
            await asyncio.sleep(0.05)
            signal.running = False

            await wait_for(lambda: len(balancer.sent("ReleaseExecutionLeaseRequest")) == 1)
        finally:
            engine.event_manager.remove_listener_for_app_event(ExecutionLeaseReleasing, hung_teardown)

    @pytest.mark.asyncio
    async def test_event_carries_the_held_lease_id(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        manager, _balancer, signal = make_manager(engine, monkeypatch)
        seen: list[ExecutionLeaseReleasing] = []

        async def observing_teardown(event: ExecutionLeaseReleasing) -> None:
            seen.append(event)

        engine.event_manager.add_listener_to_app_event(ExecutionLeaseReleasing, observing_teardown)
        try:
            await manager.gate_execution_start()
            granted_lease_id = manager.lease_id
            signal.running = True
            await asyncio.sleep(0.05)
            signal.running = False

            await wait_for(lambda: len(seen) == 1)
            assert seen[0].lease_id == granted_lease_id
        finally:
            engine.event_manager.remove_listener_for_app_event(ExecutionLeaseReleasing, observing_teardown)


class TestBalancerLiveness:
    @pytest.mark.asyncio
    async def test_gate_fails_fast_when_no_balancer_ever_beaconed(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No beacon = no balancer: refuse with a message, never wait forever."""
        manager, _, _ = make_manager(engine, monkeypatch)
        manager._balancer_last_seen = 0.0

        with pytest.raises(RuntimeError, match=r"load balancer\) has not connected to this engine yet"):
            await manager.gate_execution_start()

    @pytest.mark.asyncio
    async def test_monitor_terminates_when_beacons_stop(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        manager, _, _ = make_manager(engine, monkeypatch)
        manager.balancer_grace_s = 0.05
        manager.balancer_timeout_s = 0.1
        terminated = asyncio.Event()

        async def fake_terminate() -> None:
            terminated.set()

        monkeypatch.setattr(manager, "_terminate_process", fake_terminate)
        monitor = asyncio.create_task(manager.run_balancer_liveness_monitor())
        try:
            await asyncio.wait_for(terminated.wait(), timeout=2.0)
        finally:
            monitor.cancel()

    @pytest.mark.asyncio
    async def test_fresh_beacons_keep_the_engine_alive(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        manager, _, _ = make_manager(engine, monkeypatch)
        manager.balancer_grace_s = 0.05
        manager.balancer_timeout_s = 0.3
        terminated = asyncio.Event()

        async def fake_terminate() -> None:
            terminated.set()

        monkeypatch.setattr(manager, "_terminate_process", fake_terminate)
        monitor = asyncio.create_task(manager.run_balancer_liveness_monitor())

        async def keep_beaconing() -> None:
            for _ in range(10):
                manager._on_balancer_heartbeat(None)
                await asyncio.sleep(0.08)

        try:
            await keep_beaconing()
            assert not terminated.is_set()
        finally:
            monitor.cancel()

    @pytest.mark.asyncio
    async def test_monitor_is_a_noop_when_unmanaged(self, engine: Engine) -> None:
        engine.config_manager.set_config_value(EXECUTION_LEASE_ENABLED_KEY, value=False)
        manager = ExecutionLeaseManager(engine=engine)

        await manager.run_balancer_liveness_monitor()  # returns immediately


class TestSessionLiveness:
    @pytest.mark.asyncio
    async def test_heartbeat_handler_stamps_liveness(self, engine: Engine) -> None:
        engine.session_manager.active_session_id = "test-session"
        assert engine.session_manager.last_heartbeat_monotonic == 0.0

        result = engine.session_manager.handle_session_heartbeat_request(SessionHeartbeatRequest())

        assert result.succeeded()
        assert engine.session_manager.last_heartbeat_monotonic > 0.0

    @pytest.mark.asyncio
    async def test_monitor_is_a_noop_unless_session_lifetime_enabled(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lease-managed alone is not enough: the client must be known to heartbeat."""
        manager, _, _ = make_manager(engine, monkeypatch)
        assert manager.session_lifetime_enabled is False

        await manager.run_session_liveness_monitor()  # returns immediately

    @pytest.mark.asyncio
    async def test_monitor_terminates_when_heartbeats_stop(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager, _, _ = make_manager(engine, monkeypatch)
        manager.session_lifetime_enabled = True
        manager.session_grace_s = 0.05
        manager.session_timeout_s = 0.1
        terminated = asyncio.Event()

        async def fake_terminate() -> None:
            terminated.set()

        monkeypatch.setattr(manager, "_terminate_process", fake_terminate)
        monitor = asyncio.create_task(manager.run_session_liveness_monitor())
        try:
            await asyncio.wait_for(terminated.wait(), timeout=2.0)
        finally:
            monitor.cancel()

    @pytest.mark.asyncio
    async def test_fresh_heartbeats_keep_the_engine_alive(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager, _, _ = make_manager(engine, monkeypatch)
        manager.session_lifetime_enabled = True
        manager.session_grace_s = 0.05
        manager.session_timeout_s = 0.3
        engine.session_manager.active_session_id = "test-session"
        terminated = asyncio.Event()

        async def fake_terminate() -> None:
            terminated.set()

        monkeypatch.setattr(manager, "_terminate_process", fake_terminate)
        monitor = asyncio.create_task(manager.run_session_liveness_monitor())

        try:
            for _ in range(10):
                engine.session_manager.handle_session_heartbeat_request(SessionHeartbeatRequest())
                await asyncio.sleep(0.08)
            assert not terminated.is_set()
        finally:
            monitor.cancel()


class TestGracefulTermination:
    """How a managed engine ends its own life (design 4.5, 8.4).

    The exit must be graceful on EVERY platform: the watchdog releases the
    lease with memory teardown on the way out, so the next artist is admitted
    into a box that is actually free.
    """

    @pytest.mark.asyncio
    async def test_shutdown_hook_is_preferred_over_signalling(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A registered host shutdown wins, and no signal is sent.

        os.kill(SIGTERM) is a graceful request only on POSIX; on Windows it is
        an unconditional TerminateProcess with no handler, so the lease release
        and teardown would be skipped. Hosts register their own cooperative
        shutdown to make the path portable.
        """
        manager, _, _ = make_manager(engine, monkeypatch)
        reasons: list[str] = []
        manager.set_shutdown_hook(reasons.append)

        killed: list[tuple[int, int]] = []
        monkeypatch.setattr("os.kill", lambda pid, sig: killed.append((pid, sig)))
        # The escalation sleep would otherwise hold the test for 15s; stub the
        # forced exit so only the request path is exercised.
        monkeypatch.setattr(ExecutionLeaseManager, "_TERMINATE_ESCALATION_S", 0.01)
        monkeypatch.setattr("os._exit", lambda _code: None)

        await manager._terminate_process("session heartbeats stopped")

        assert reasons == ["session heartbeats stopped"]
        assert killed == []

    @pytest.mark.asyncio
    async def test_without_a_hook_it_signals_itself(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        """The fallback stays: an embedder with no cooperative shutdown."""
        manager, _, _ = make_manager(engine, monkeypatch)
        killed: list[tuple[int, int]] = []
        monkeypatch.setattr("os.kill", lambda pid, sig: killed.append((pid, sig)))
        monkeypatch.setattr(ExecutionLeaseManager, "_TERMINATE_ESCALATION_S", 0.01)
        monkeypatch.setattr("os._exit", lambda _code: None)

        await manager._terminate_process()

        assert killed == [(os.getpid(), signal.SIGTERM)]
