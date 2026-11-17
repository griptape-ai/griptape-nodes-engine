"""The scheduler's per-run set of nodes already marked stale.

``ParallelResolutionContext.unresolved_this_run`` is handed to every downstream-unresolve walk
in a run so each node is visited once per run instead of once per upstream execution. A node
leaves the set when it finishes executing, and a reset empties it.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from griptape_nodes.common.directed_graph import DirectedGraph
from griptape_nodes.exe_types.node_types import BaseNode, NodeResolutionState
from griptape_nodes.machines.dag_builder import DagBuilder, DagNode, NodeState
from griptape_nodes.machines.parallel_resolution import ExecuteDagState, ParallelResolutionContext
from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime


def _context() -> ParallelResolutionContext:
    context = ParallelResolutionContext(
        workflow=WorkflowRuntime("wf", MagicMock()),
        flow_name="flow",
        dag_builder=DagBuilder(MagicMock()),
        engine=MagicMock(),
    )
    context.engine.event_manager.aput_event = AsyncMock()
    return context


def test_starts_empty_and_reset_clears_it() -> None:
    """The set is empty on a new context and emptied again by reset()."""
    context = _context()
    assert context.unresolved_this_run == set()

    context.unresolved_this_run.update({"A", "B"})
    context.reset()

    assert context.unresolved_this_run == set()


@pytest.mark.asyncio
async def test_finished_node_leaves_the_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """handle_done_nodes discards the finished node and nothing else."""
    context = _context()
    context.unresolved_this_run.update({"Done", "Other"})
    node = MagicMock(spec=BaseNode)
    node.name = "Done"
    node.lock = False
    node.state = NodeResolutionState.RESOLVING
    node.parameter_values = {}
    node.parameter_output_values = {}
    graph = DirectedGraph()
    graph.add_node("Done")
    assert context.dag_builder is not None
    context.dag_builder.graphs["Done"] = graph
    dag_node = DagNode(node_reference=node, node_state=NodeState.DONE)
    context.dag_builder.node_to_reference["Done"] = dag_node
    monkeypatch.setattr(ExecuteDagState, "get_next_control_graph", MagicMock())
    monkeypatch.setattr(ExecuteDagState, "check_for_new_start_nodes", MagicMock())

    await ExecuteDagState.handle_done_nodes(context, dag_node, "Done")

    assert context.unresolved_this_run == {"Other"}
