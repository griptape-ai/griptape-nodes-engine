"""A node added to a live run must start as soon as there is room for it.

Running a single node while another single node is already resolving used to add the
new node to the DAG and then leave it there: nothing queued it, and the driver was
parked in `asyncio.wait`, whose awaitable set is snapshotted at call time. The node
only started once some unrelated in-flight node happened to finish, so injection
latency was "however long the shortest running node takes".

These cover the two halves of the fix - queueing at injection time, and a wakeup that
can actually break the driver's wait - plus the properties that make the wakeup safe
to add to a loop that is also reaping node tasks.
"""

import asyncio

import pytest

from griptape_nodes.machines.dag_builder import NodeState
from tests.unit.machines.scheduler_stubs import (
    let_the_run_finish,
    machine_running_one_node,
    node_stub,
)


class TestInjectedNodeStartsImmediately:
    @pytest.mark.asyncio
    async def test_injected_node_runs_without_waiting_for_the_running_node(
        self, held_node_execution: asyncio.Event
    ) -> None:
        machine, release, dag_builder = machine_running_one_node(max_nodes_in_parallel=2)

        driver = asyncio.create_task(machine.resolve_node())
        await asyncio.sleep(0)
        assert machine.is_advancing, "driver should be parked on the running node"

        injected = node_stub("injected")
        machine.inject_node(injected)

        # No release of the running node: the whole point is that the injected node
        # does not have to wait for it.
        for _ in range(20):
            if dag_builder.node_to_reference["injected"].node_state is NodeState.PROCESSING:
                break
            await asyncio.sleep(0)

        assert dag_builder.node_to_reference["injected"].node_state is NodeState.PROCESSING, (
            "injected node should have been dispatched while the other node is still running"
        )
        expected_running = 2  # the pre-seeded node plus the injected one
        assert machine.context.running_tasks_count == expected_running

        await let_the_run_finish(driver, release, held_node_execution)

    @pytest.mark.asyncio
    async def test_injection_queues_the_node_even_with_no_driver_running(self) -> None:
        machine, _release, dag_builder = machine_running_one_node(max_nodes_in_parallel=2)

        injected = node_stub("injected")
        added = machine.inject_node(injected)

        assert injected in added
        assert dag_builder.node_to_reference["injected"].node_state is NodeState.QUEUED
        assert machine.context.new_work_event.is_set()


class TestInjectionRespectsTheConcurrencyCap:
    @pytest.mark.asyncio
    async def test_injected_node_waits_for_a_free_slot(self, held_node_execution: asyncio.Event) -> None:
        machine, release, dag_builder = machine_running_one_node(max_nodes_in_parallel=1)

        driver = asyncio.create_task(machine.resolve_node())
        await asyncio.sleep(0)

        injected = node_stub("injected")
        machine.inject_node(injected)

        # Give the woken driver every chance to overshoot the cap.
        for _ in range(20):
            await asyncio.sleep(0)

        assert dag_builder.node_to_reference["injected"].node_state is NodeState.QUEUED, (
            "the pool is full, so the injected node must stay queued"
        )
        assert machine.context.running_tasks_count == 1, "must not oversubscribe max_nodes_in_parallel"

        await let_the_run_finish(driver, release, held_node_execution)


class TestTheWakeupIsSafeToAddToTheReapLoop:
    @pytest.mark.asyncio
    async def test_a_wakeup_does_not_reap_the_still_running_node(self, held_node_execution: asyncio.Event) -> None:
        """The waiter is not a node task, so it must not be popped from task_to_node."""
        machine, release, _dag_builder = machine_running_one_node(max_nodes_in_parallel=2)

        driver = asyncio.create_task(machine.resolve_node())
        await asyncio.sleep(0)

        machine.inject_node(node_stub("injected"))
        for _ in range(20):
            await asyncio.sleep(0)

        node_names = {dag_node.node_reference.name for dag_node in machine.context.task_to_node.values()}
        assert "running" in node_names, "waking for an injection must not retire the running node"
        assert machine.context.running_tasks_count == len(machine.context.task_to_node)

        await let_the_run_finish(driver, release, held_node_execution)

    @pytest.mark.asyncio
    async def test_wakeup_waiter_does_not_leak(self, held_node_execution: asyncio.Event) -> None:
        machine, release, _dag_builder = machine_running_one_node(max_nodes_in_parallel=2)
        before = asyncio.all_tasks()

        driver = asyncio.create_task(machine.resolve_node())
        await asyncio.sleep(0)
        machine.inject_node(node_stub("injected"))
        await let_the_run_finish(driver, release, held_node_execution)
        # Let the cancelled waiters actually finish unwinding.
        for _ in range(5):
            await asyncio.sleep(0)

        leaked = {task for task in asyncio.all_tasks() if task not in before and task is not driver}
        leaked = {task for task in leaked if not task.done()}
        assert not leaked, f"scheduling waiters should not outlive the drive: {leaked}"

    @pytest.mark.asyncio
    async def test_driver_does_not_spin_when_nothing_can_be_dispatched(
        self, held_node_execution: asyncio.Event
    ) -> None:
        """A set flag with nothing dispatchable must suspend, not busy-loop.

        The flag is consumed at the top of every dispatch pass, so a node the queue
        refuses cannot leave the driver re-entering on_update forever at 100% CPU.
        """
        machine, release, _dag_builder = machine_running_one_node(max_nodes_in_parallel=2)

        driver = asyncio.create_task(machine.resolve_node())
        await asyncio.sleep(0)

        # Signal work that does not exist: nothing was added to the queue.
        machine.context.signal_new_work()
        for _ in range(50):
            await asyncio.sleep(0)

        assert not driver.done(), "driver should still be parked on the running node"
        assert not machine.context.new_work_event.is_set(), "the spurious signal was consumed, not left to spin"

        await let_the_run_finish(driver, release, held_node_execution)


class TestInjectionIntoAPausedRun:
    @pytest.mark.asyncio
    async def test_paused_run_queues_the_node_but_does_not_start_it(self, held_node_execution: asyncio.Event) -> None:
        machine, release, dag_builder = machine_running_one_node(max_nodes_in_parallel=2)

        driver = asyncio.create_task(machine.resolve_node())
        await asyncio.sleep(0)
        machine.change_debug_mode(debug_mode=True)

        machine.inject_node(node_stub("injected"))
        for _ in range(20):
            await asyncio.sleep(0)

        assert dag_builder.node_to_reference["injected"].node_state is NodeState.QUEUED, (
            "a paused run must not start the injected node"
        )

        release.set()
        held_node_execution.set()
        await asyncio.wait_for(driver, timeout=1)
        assert not machine.is_advancing

        # Continuing the run picks the queued node up. Hold the DagNode itself: finishing
        # the run clears node_to_reference, so it cannot be looked up by name afterwards.
        injected_dag_node = dag_builder.node_to_reference["injected"]
        machine.change_debug_mode(debug_mode=False)
        await machine.update()
        assert injected_dag_node.node_state is NodeState.DONE, "continuing the run must run the queued node"


class TestTeardownWhileParkedOnTheWakeup:
    @pytest.mark.asyncio
    async def test_reset_unparks_the_driver_without_waiting_for_the_running_node(self) -> None:
        """reset() must release the FSM's single-driver claim promptly.

        It clears task_to_node but cannot itself break the driver's wait, so before the
        wakeup existed the driver held the claim until some *old* node task finished,
        and any resolve_node in that window died on the single-driver guard.
        """
        machine, release, _dag_builder = machine_running_one_node(max_nodes_in_parallel=2)

        driver = asyncio.create_task(machine.resolve_node())
        await asyncio.sleep(0)
        assert machine.is_advancing

        machine.reset_machine()

        # The running node is deliberately never released.
        await asyncio.wait_for(driver, timeout=1)

        assert not machine.is_advancing, "the abandoned driver released the machine"
        assert machine.current_state is None, "teardown left the machine reset, not resurrected"
        assert not machine.context.task_to_node

        release.set()
