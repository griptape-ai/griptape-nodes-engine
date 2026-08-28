"""Nothing may pull task bookkeeping out from under a parked driver.

Field crash: a run died with `KeyError: <Task finished ... execute_node ...>` from
`ExecuteDagState.on_update`'s `task_to_node.pop(task)`. A driver suspended in
`asyncio.wait(task_to_node.keys())` woke for a completed node task and found it
already gone from the map. Two things produce that, both covered here: a second
coroutine driving the same machine, and a teardown (`ParallelResolutionContext.reset()`)
clearing the map from synchronous code in another coroutine.
"""

import asyncio
from typing import NamedTuple
from unittest.mock import AsyncMock, MagicMock

import pytest

from griptape_nodes.common.directed_graph import DirectedGraph
from griptape_nodes.exe_types.node_types import BaseNode, NodeResolutionState
from griptape_nodes.machines.dag_builder import DagBuilder, DagNode, NodeState
from griptape_nodes.machines.parallel_resolution import ExecuteDagState, ParallelResolutionMachine


class _ParkedMachine(NamedTuple):
    machine: ParallelResolutionMachine
    release: asyncio.Event


def _machine_parked_on_a_node_task() -> _ParkedMachine:
    """A machine with one PROCESSING node whose task is still pending.

    Driving it lands in `ExecuteDagState.on_update`'s `asyncio.wait` and stays
    there until the returned event is set, which is the window a second driver
    used to slip into.
    """
    node = MagicMock(spec=BaseNode)
    node.name = "n"
    node.lock = False
    node.state = NodeResolutionState.RESOLVING

    graph = DirectedGraph()
    graph.add_node("n")

    # A stub engine keeps EngineScoped.engine from falling back to
    # current_engine(), which would stand up the process-root engine here.
    engine = MagicMock()
    dag_builder = DagBuilder(engine)
    dag_builder.graphs["n"] = graph
    dag_builder.node_to_reference["n"] = DagNode(node_reference=node, node_state=NodeState.PROCESSING)

    machine = ParallelResolutionMachine("flow", max_nodes_in_parallel=1, dag_builder=dag_builder, engine=engine)

    release = asyncio.Event()
    node_task = asyncio.ensure_future(release.wait())
    machine.context.task_to_node[node_task] = dag_builder.node_to_reference["n"]
    machine.context.running_tasks_count = 1

    return _ParkedMachine(machine=machine, release=release)


@pytest.fixture(autouse=True)
def _stub_handle_done_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    """handle_done_nodes touches the full engine (events, connections, library registry).

    Stub it so these stay focused scheduler tests; they only care about who is
    allowed to drive the machine, not what happens after a node finishes.
    """
    monkeypatch.setattr(ExecuteDagState, "_compute_heavy_clusters", MagicMock())
    monkeypatch.setattr(ExecuteDagState, "handle_done_nodes", AsyncMock())


class TestParallelResolutionSingleDriver:
    @pytest.mark.asyncio
    async def test_second_driver_raises_instead_of_corrupting_task_bookkeeping(self) -> None:
        machine, release = _machine_parked_on_a_node_task()

        first_driver = asyncio.create_task(machine.resolve_node())
        # Let the first driver reach the asyncio.wait inside on_update.
        await asyncio.sleep(0)
        assert machine.context.task_to_node, "first driver should still be tracking the node task"

        # Let the node task finish while a second driver would be waiting on it.
        # Unguarded, both wake for this one task and the second `pop` raises the
        # KeyError seen in the field.
        release.set()

        with pytest.raises(RuntimeError, match="already running"):
            await machine.resolve_node()

        await first_driver

    @pytest.mark.asyncio
    async def test_sequential_drivers_are_allowed(self) -> None:
        machine, release = _machine_parked_on_a_node_task()

        release.set()
        await machine.resolve_node()
        assert not machine.is_advancing, "the drive released the machine when it returned"

        # The guard must not latch: the machine is drivable again once the
        # previous drive returned.
        await machine.resolve_node()
        assert not machine.is_advancing


class TestTeardownWhileDriverParked:
    """A run torn down mid-flight must abandon its driver, not crash it.

    `reset()` clears `task_to_node` synchronously from another coroutine, so a
    driver parked in `asyncio.wait` cannot assume the map it is holding keys
    into still exists when it wakes.
    """

    @pytest.mark.asyncio
    async def test_reset_while_parked_abandons_the_run_instead_of_crashing(self) -> None:
        machine, release = _machine_parked_on_a_node_task()

        driver = asyncio.create_task(machine.resolve_node())
        await asyncio.sleep(0)
        assert machine.is_advancing, "driver should hold the claim while parked"

        # A concurrent clear-all-state tears the run down, clearing the very map
        # the parked driver is waiting on keys from.
        machine.reset_machine()
        assert not machine.context.task_to_node

        release.set()
        # Before the generation check this raised
        # `KeyError: <Task finished ...>` out of on_update's reap loop.
        await driver

        assert not machine.is_advancing, "the abandoned driver released the machine"
        assert machine.current_state is None, "teardown left the machine reset, not resurrected"

    @pytest.mark.asyncio
    async def test_cancelling_reset_also_drops_task_bookkeeping(self) -> None:
        machine, release = _machine_parked_on_a_node_task()

        driver = asyncio.create_task(machine.resolve_node())
        await asyncio.sleep(0)

        machine.reset_machine(cancel=True)

        release.set()
        await driver

        # The abandoning driver no longer drains the map itself, so the reset has
        # to, or the leftover task lands on the next run on this context.
        assert not machine.context.task_to_node


class TestPausingAParkedDriver:
    """FlowManager declines to drive a running flow but still sets its paused flag.

    That only works if a parked driver observes the flag and stops itself, and if
    clearing the flag lets it carry on. These pin that against real objects: mock
    based tests would keep passing if `ExecuteDagState` stopped checking `paused`.
    """

    @pytest.mark.asyncio
    async def test_pause_set_from_outside_parks_the_driver_and_leaves_it_resumable(self) -> None:
        machine, release = _machine_parked_on_a_node_task()

        driver = asyncio.create_task(machine.resolve_node())
        await asyncio.sleep(0)
        assert machine.is_advancing, "driver should hold the claim while parked"

        # Pause arriving from a concurrent request handler.
        machine.change_debug_mode(debug_mode=True)
        release.set()
        await driver

        # Stopped without completing, and released the claim so a later step can drive.
        assert not machine.is_advancing
        assert machine.current_state is ExecuteDagState
        assert not machine.is_complete()
        assert machine.is_started()

        # Continue: clear the flag, then drive.
        machine.change_debug_mode(debug_mode=False)
        await machine.update()
        assert machine.is_complete()

    @pytest.mark.asyncio
    async def test_resume_while_still_parked_keeps_the_run_going(self) -> None:
        machine, release = _machine_parked_on_a_node_task()

        driver = asyncio.create_task(machine.resolve_node())
        await asyncio.sleep(0)

        # Pause then Continue, both while the driver is still on the node. The
        # resume must win, or the run halts once the node finishes even though
        # the user asked it to continue.
        machine.change_debug_mode(debug_mode=True)
        machine.change_debug_mode(debug_mode=False)
        release.set()
        await driver

        assert machine.is_complete()
