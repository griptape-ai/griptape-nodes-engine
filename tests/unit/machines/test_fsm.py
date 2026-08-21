"""Tests for the FSM single-driver guard.

State machines are single-driver: two coroutines advancing the same machine
interleave inside the states' awaits and corrupt shared context. In the field a
step request arriving during a running flow became a second driver of that
flow's resolution machine and surfaced as a KeyError in ExecuteDagState. The
guard makes the second driver fail fast with a clear error instead.
"""

import asyncio

import pytest

from griptape_nodes.machines.fsm import FSM, State


class _Context:
    def __init__(self) -> None:
        self.entered_update = asyncio.Event()
        self.release_update = asyncio.Event()
        self.update_calls = 0


class _BlockingState(State):
    """State whose on_update suspends until the test releases it."""

    @staticmethod
    async def on_enter(context: _Context) -> type[State] | None:  # noqa: ARG004
        return _BlockingState

    @staticmethod
    async def on_update(context: _Context) -> type[State] | None:
        context.update_calls += 1
        context.entered_update.set()
        await context.release_update.wait()
        return None


class TestFsmSingleDriverGuard:
    @pytest.mark.asyncio
    async def test_second_driver_rejected_while_machine_advancing(self) -> None:
        context = _Context()
        machine = FSM(context)

        driver = asyncio.create_task(machine.start(_BlockingState))
        await context.entered_update.wait()

        # The machine is suspended inside on_update; a second driver must not
        # be able to advance it concurrently.
        # Every entry point that advances the machine must refuse. Each is
        # bounded by a timeout: if the guard regresses these calls block on the
        # blocking state instead of raising, and an unbounded await would hang
        # the whole test run rather than failing it.
        with pytest.raises(RuntimeError, match="already running"):
            await asyncio.wait_for(machine.update(), timeout=5)
        with pytest.raises(RuntimeError, match="already running"):
            await asyncio.wait_for(machine.transition_state(_BlockingState), timeout=5)
        with pytest.raises(RuntimeError, match="already running"):
            await asyncio.wait_for(machine.handle_event("some-event"), timeout=5)

        context.release_update.set()
        await driver
        assert context.update_calls == 1

    @pytest.mark.asyncio
    async def test_guard_clears_after_drive_completes(self) -> None:
        context = _Context()
        context.release_update.set()
        machine = FSM(context)

        await machine.start(_BlockingState)
        first_drive_calls = context.update_calls

        # Sequential driving is fine: the guard resets once a drive finishes.
        await machine.update()
        assert context.update_calls == first_drive_calls + 1

    @pytest.mark.asyncio
    async def test_guard_clears_after_state_raises(self) -> None:
        class _RaisingState(State):
            @staticmethod
            async def on_enter(context: _Context) -> type[State] | None:  # noqa: ARG004
                msg = "boom"
                raise RuntimeError(msg)

        context = _Context()
        context.release_update.set()
        machine = FSM(context)

        with pytest.raises(RuntimeError, match="boom"):
            await machine.start(_RaisingState)

        # A failed drive must not leave the machine permanently "advancing".
        await machine.start(_BlockingState)
        assert context.update_calls == 1
