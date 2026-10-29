"""A node's `validate_before_node_run` runs in a thread, so a slow check doesn't freeze the engine.

The node still waits for its own result: it runs only when validation passes, and a failure
stops the run exactly as before.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from griptape_nodes.machines.dag_builder import DagNode, NodeState
from griptape_nodes.machines.parallel_resolution import ErrorState, ExecuteDagState
from griptape_nodes.retained_mode.events.execution_events import NodeErrorEvent
from tests.unit.machines.scheduler_stubs import machine_running_one_node, node_stub

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.exe_types.node_types import BaseNode


async def _until(condition: Callable[[], object], message: str) -> None:
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.005)
    pytest.fail(message)


class TestValidationRunsOffTheEventLoop:
    @pytest.mark.asyncio
    async def test_validation_runs_in_another_thread(self, held_node_execution: asyncio.Event) -> None:
        machine, release, _dag_builder = machine_running_one_node(max_nodes_in_parallel=2)
        loop_thread = threading.get_ident()
        validation_threads: list[int] = []

        def validate() -> None:
            validation_threads.append(threading.get_ident())

        injected = node_stub("injected")
        injected.validate_before_node_run = MagicMock(side_effect=validate)
        driver = asyncio.create_task(machine.resolve_node())
        machine.inject_node(injected)

        await _until(lambda: validation_threads, "the injected node was never validated")
        assert validation_threads[0] != loop_thread

        release.set()
        held_node_execution.set()
        await asyncio.wait_for(driver, timeout=1)

    @pytest.mark.asyncio
    async def test_the_event_loop_keeps_running_while_a_node_validates(
        self, held_node_execution: asyncio.Event
    ) -> None:
        machine, release, dag_builder = machine_running_one_node(max_nodes_in_parallel=2)
        validating = threading.Event()
        finish_validation = threading.Event()

        def slow_validate() -> None:
            validating.set()
            # Blocks this thread, not the loop: the loop must stay free to run other work.
            finish_validation.wait(timeout=5)

        injected = node_stub("injected")
        injected.validate_before_node_run = MagicMock(side_effect=slow_validate)
        machine.inject_node(injected)
        # Started before the driver, so it is already waiting on the loop when validation blocks.
        other_work = asyncio.create_task(asyncio.sleep(0.01, result="done"))
        driver = asyncio.create_task(machine.resolve_node())

        # Run on a plain thread so this check can't itself be what keeps the loop busy.
        await asyncio.to_thread(validating.wait, 5)
        assert validating.is_set(), "the injected node never started validating"
        # Validation is still blocked. Had it run on the loop, nothing else could run until it returned.
        assert await asyncio.wait_for(other_work, timeout=1) == "done"
        assert dag_builder.node_to_reference["injected"].node_state is not NodeState.PROCESSING, (
            "the node must not run before its validation returns"
        )

        finish_validation.set()
        await _until(
            lambda: dag_builder.node_to_reference["injected"].node_state is NodeState.PROCESSING,
            "the node should run once its validation passes",
        )

        release.set()
        held_node_execution.set()
        await asyncio.wait_for(driver, timeout=1)

    @pytest.mark.asyncio
    async def test_a_failed_validation_still_stops_the_run(
        self, held_node_execution: asyncio.Event, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The pre-seeded node finishes after the failure; publishing it reaches into the full engine.
        monkeypatch.setattr(ExecuteDagState, "publish_resolved_node", AsyncMock())
        machine, release, dag_builder = machine_running_one_node(max_nodes_in_parallel=2)
        injected = node_stub("injected")
        injected.validate_before_node_run = MagicMock(return_value=[ValueError("'Prompt' is empty.")])

        driver = asyncio.create_task(machine.resolve_node())
        machine.inject_node(injected)
        await _until(lambda: machine.current_state is ErrorState, "the failed validation never stopped the run")

        assert dag_builder.node_to_reference["injected"].node_state is not NodeState.PROCESSING
        put_event = cast("AsyncMock", machine.context.engine.event_manager.aput_event)
        events = [call.args[0].wrapped_event.payload for call in put_event.call_args_list]
        errors = [event for event in events if isinstance(event, NodeErrorEvent)]
        assert [cast("BaseNode", injected).name] == [event.node_name for event in errors]

        release.set()
        held_node_execution.set()
        await asyncio.wait_for(driver, timeout=1)

    @pytest.mark.asyncio
    async def test_a_run_reset_while_a_node_validates_does_not_run_it(
        self, held_node_execution: asyncio.Event, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        executed: list[str] = []

        async def record(_engine: object, dag_node: DagNode) -> None:
            executed.append(dag_node.node_reference.name)

        monkeypatch.setattr(ExecuteDagState, "execute_node", record)
        machine, release, _dag_builder = machine_running_one_node(max_nodes_in_parallel=2)
        validating = threading.Event()
        finish_validation = threading.Event()

        def slow_validate() -> None:
            validating.set()
            finish_validation.wait(timeout=5)

        injected = node_stub("injected")
        injected.validate_before_node_run = MagicMock(side_effect=slow_validate)
        driver = asyncio.create_task(machine.resolve_node())
        machine.inject_node(injected)
        await asyncio.to_thread(validating.wait, 5)

        # What cancelling the flow does while the node is still being checked.
        release.set()
        machine.reset_machine(cancel=True)
        finish_validation.set()
        await asyncio.wait_for(driver, timeout=2)
        held_node_execution.set()

        assert executed == [], "a node whose run was cancelled during its checks must not run"
        assert not machine.context.task_to_node, "no task may be left behind for the next run"
