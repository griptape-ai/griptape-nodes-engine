"""Shared scaffolding for the parallel-resolution scheduler tests.

These build a machine that is already mid-run - one node PROCESSING, its task still
pending - because that is the state every interesting scheduler question is about: what
happens to a live DAG when something changes underneath the parked driver.
"""

from __future__ import annotations

import asyncio
from typing import NamedTuple
from unittest.mock import AsyncMock, MagicMock

from griptape_nodes.common.directed_graph import DirectedGraph
from griptape_nodes.exe_types.node_types import BaseNode, NodeResolutionState
from griptape_nodes.machines.dag_builder import DagBuilder, DagNode, NodeState
from griptape_nodes.machines.parallel_resolution import ExecuteDagState, ParallelResolutionMachine


class RunningMachine(NamedTuple):
    machine: ParallelResolutionMachine
    release: asyncio.Event
    dag_builder: DagBuilder


def node_stub(name: str) -> BaseNode:
    """A node that is inert enough to be scheduled but never really executed."""
    node = MagicMock(spec=BaseNode)
    node.name = name
    node.lock = False
    node.state = NodeResolutionState.UNRESOLVED
    node.parameters = []
    # Instance attributes, so spec=BaseNode does not supply them. on_update touches both
    # on the way to dispatching a node; validate must report no problems or we land in
    # ErrorState instead of executing.
    node.parameter_output_values = MagicMock()
    node.validate_before_node_run = MagicMock(return_value=None)
    return node


def machine_running_one_node(max_nodes_in_parallel: int) -> RunningMachine:
    """A machine with one PROCESSING node whose task is still pending.

    Driving it parks it in `ExecuteDagState.on_update`'s `asyncio.wait`, which is
    exactly the window an injection has to be able to interrupt.
    """
    running_node = node_stub("running")
    running_node.state = NodeResolutionState.RESOLVING

    graph = DirectedGraph()
    graph.add_node("running")

    # A stub engine keeps EngineScoped.engine from falling back to current_engine(),
    # which would stand up the process-root engine here.
    engine = MagicMock()
    # Dispatching a node awaits this; a bare MagicMock is not awaitable.
    engine.event_manager.aput_event = AsyncMock()
    dag_builder = DagBuilder(engine)
    dag_builder.graphs["running"] = graph
    dag_builder.graph_to_nodes["running"] = {"running"}
    dag_builder.node_to_reference["running"] = DagNode(node_reference=running_node, node_state=NodeState.PROCESSING)

    machine = ParallelResolutionMachine(
        "flow", max_nodes_in_parallel=max_nodes_in_parallel, dag_builder=dag_builder, engine=engine
    )

    release = asyncio.Event()
    node_task = asyncio.ensure_future(release.wait())
    machine.context.task_to_node[node_task] = dag_builder.node_to_reference["running"]
    dag_builder.node_to_reference["running"].task_reference = node_task
    machine.context.running_tasks_count = 1

    return RunningMachine(machine=machine, release=release, dag_builder=dag_builder)


def add_waiting_node(dag_builder: DagBuilder, name: str, graph_name: str) -> DagNode:
    """Add a WAITING node to a graph, creating the graph if this is its first node."""
    node = node_stub(name)
    graph = dag_builder.graphs.setdefault(graph_name, DirectedGraph())
    graph.add_node(name)
    dag_builder.graph_to_nodes.setdefault(graph_name, set()).add(name)
    dag_node = DagNode(node_reference=node, node_state=NodeState.WAITING)
    dag_builder.node_to_reference[name] = dag_node
    return dag_node


def add_dependency(dag_builder: DagBuilder, graph_name: str, upstream: str, downstream: str) -> None:
    """Make `downstream` depend on `upstream`, so it cannot be queued until `upstream` leaves."""
    dag_builder.graphs[graph_name].add_edge(upstream, downstream)


def queue_node(machine: ParallelResolutionMachine, name: str) -> None:
    """Route a WAITING node through the real queueing funnel."""
    ExecuteDagState._try_queue_waiting_node(machine.context, name)


async def let_the_run_finish(driver: asyncio.Task, release: asyncio.Event, hold: asyncio.Event) -> None:
    """Release both the pre-seeded node task and any dispatched node, then join.

    The timeout is what turns "the driver never woke up" into a failure rather than a
    hung test run.
    """
    release.set()
    hold.set()
    await asyncio.wait_for(driver, timeout=1)
