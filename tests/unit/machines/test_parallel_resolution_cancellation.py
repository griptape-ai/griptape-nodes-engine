"""A cancelled node must be able to run again on the next run.

Cancelling a run sets the cooperative cancellation flag on every node in the DAG
(`cancel_all_nodes`), but the only thing that clears it is `BaseNode.clear_node()`, which
the teardown reaches solely for the run's entry nodes: `ControlFlowContext.reset()`
iterates `current_nodes`, not `node_to_reference`. A node that was cancelled mid-run as a
data dependency therefore kept the flag, and the *next* run dispatched it already
believing it was cancelled — so it short-circuited through its cooperative-cancel branch
and reported whatever that leaves behind (for a node reading its structure's output, a
bare "no output" error) instead of running.

The fix clears the flag where the node is dispatched, so it is scoped to the execution it
belongs to regardless of which cancel path ran, or whether a reset followed at all.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, NamedTuple
from unittest.mock import AsyncMock

import pytest

from griptape_nodes.machines.dag_builder import NodeState
from tests.unit.machines.scheduler_stubs import (
    let_the_run_finish,
    machine_running_one_node,
    node_stub,
)

if TYPE_CHECKING:
    from griptape_nodes.exe_types.node_types import BaseNode


class CancellableNode(NamedTuple):
    """A node stub alongside the event backing its cancellation flag."""

    node: BaseNode
    flag: threading.Event


def cancellable_node_stub(name: str) -> CancellableNode:
    """A schedulable node stub whose cancellation flag really behaves like a node's.

    `node_stub` builds a `MagicMock(spec=BaseNode)`, so `clear_cancellation()` would be a
    no-op that merely records the call — which cannot show whether the flag was actually
    cleared. Wire the two mutators to the same `threading.Event` the real node uses and
    hand it back, so assertions read the flag itself rather than a Mock attribute.
    """
    node = node_stub(name)
    flag = threading.Event()
    node._cancellation_requested = flag
    node.request_cancellation = flag.set
    node.clear_cancellation = flag.clear
    return CancellableNode(node=node, flag=flag)


class TestStaleCancellationDoesNotLeakIntoTheNextRun:
    @pytest.mark.asyncio
    async def test_dispatch_clears_a_cancellation_flag_left_over_from_a_previous_run(
        self, held_node_execution: asyncio.Event
    ) -> None:
        """A node carrying a stale flag must have it cleared before its task starts."""
        machine, release, dag_builder = machine_running_one_node(max_nodes_in_parallel=2)

        node, flag = cancellable_node_stub("previously_cancelled")
        # What a cancelled run leaves behind: the flag is set, and nothing in the teardown
        # cleared it because this node was never one of the run's entry nodes.
        node.request_cancellation()
        assert flag.is_set()

        driver = asyncio.create_task(machine.resolve_node())
        await asyncio.sleep(0)

        machine.inject_node(node)

        for _ in range(20):
            if dag_builder.node_to_reference["previously_cancelled"].node_state is NodeState.PROCESSING:
                break
            await asyncio.sleep(0)

        assert dag_builder.node_to_reference["previously_cancelled"].node_state is NodeState.PROCESSING, (
            "node should have been dispatched"
        )
        assert not flag.is_set(), "a stale cancellation flag must not survive into the run that dispatches the node"

        await let_the_run_finish(driver, release, held_node_execution)

    @pytest.mark.asyncio
    async def test_cancel_after_dispatch_still_sets_the_flag(self, held_node_execution: asyncio.Event) -> None:
        """Clearing at dispatch must not swallow a cancellation aimed at the live run."""
        machine, release, dag_builder = machine_running_one_node(max_nodes_in_parallel=2)

        node, flag = cancellable_node_stub("running_then_cancelled")
        # cancel_all_nodes awaits this per node; a bare MagicMock attribute is not awaitable.
        machine.context.engine.node_manager.cancel_worker_execution = AsyncMock()

        driver = asyncio.create_task(machine.resolve_node())
        await asyncio.sleep(0)
        machine.inject_node(node)

        for _ in range(20):
            if dag_builder.node_to_reference["running_then_cancelled"].node_state is NodeState.PROCESSING:
                break
            await asyncio.sleep(0)
        assert not flag.is_set()

        await machine.cancel_all_nodes()

        assert flag.is_set(), "a cancel arriving after dispatch must still reach the node it is meant to stop"

        await let_the_run_finish(driver, release, held_node_execution)
