"""Two edges of the mid-run delete policy that a full run cannot easily reach.

The e2e suite covers what an artist can actually do to a workflow. Two branches sit outside that
reach: the gated-candidate rule, which needs the scheduler parked in a state a test cannot hold open
on demand, and the delete that is refused because cancelling the run itself failed -- there is no
input that makes a healthy engine fail to cancel.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from griptape_nodes.machines.dag_builder import DagBuilder, DagNode, NodeState
from griptape_nodes.retained_mode.events.node_events import DeleteNodeResultFailure
from griptape_nodes.retained_mode.managers.event_manager import EventManager
from griptape_nodes.retained_mode.managers.node_manager import NodeManager


def _node(name: str) -> MagicMock:
    """A stand-in node. `MagicMock(name=...)` names the mock, not the node, so set it after."""
    node = MagicMock()
    node.name = name
    return node


def _engine(dag_builder: DagBuilder, *, outgoing: list | None = None) -> MagicMock:
    """An engine whose only real part is the DAG the deletion policy reads."""
    engine = MagicMock()
    engine.flow_manager.global_dag_builder = dag_builder
    engine.flow_manager.get_connections.return_value.get_all_outgoing_connections.return_value = outgoing or []
    return engine


def _node_manager(engine: MagicMock) -> NodeManager:
    return NodeManager(MagicMock(spec=EventManager), engine=engine)


def test_deleting_a_node_the_run_is_gated_on_is_entangled() -> None:
    """A data node registered as reachable only once this node finishes is waiting on it.

    `start_node_candidates` is how the scheduler records "start this data node once these boundary
    nodes are done". Deleting a boundary node leaves the candidate waiting for a completion that is
    never coming, so it counts as entangled even though the candidate is not in the DAG yet and the
    deleted node has no connection to it.
    """
    dag_builder = DagBuilder(MagicMock())
    dag_builder.start_node_candidates["GatedDataNode"] = {"default": {"Boundary"}}

    node_manager = _node_manager(_engine(dag_builder))

    assert node_manager._find_entangled_live_node(_node("Boundary")) == "GatedDataNode"


def test_deleting_a_node_nothing_is_gated_on_is_free() -> None:
    """The same shape with a different name is not entangled -- the rule has to discriminate."""
    dag_builder = DagBuilder(MagicMock())
    dag_builder.start_node_candidates["GatedDataNode"] = {"default": {"Boundary"}}

    node_manager = _node_manager(_engine(dag_builder))

    assert node_manager._find_entangled_live_node(_node("Unrelated")) is None


@pytest.mark.asyncio
async def test_a_delete_that_cannot_cancel_the_run_fails_rather_than_proceeding() -> None:
    """If the run will not stop, the node must stay.

    Deleting it anyway would leave the scheduler holding work for a node that no longer exists, which
    is the wedge this whole policy exists to avoid -- so the delete is refused, and says why in terms
    of the workflow rather than the machinery.
    """
    dag_builder = DagBuilder(MagicMock())
    dag_builder.node_to_reference["Runner"] = DagNode(node_reference=_node("Runner"), node_state=NodeState.PROCESSING)

    engine = _engine(dag_builder)
    engine.flow_manager.check_for_existing_running_flow.return_value = True
    failed_result = MagicMock()
    failed_result.failed.return_value = True
    engine.ahandle_request = AsyncMock(return_value=failed_result)
    node_manager = _node_manager(engine)

    parent_flow = MagicMock()
    outcome = await node_manager.cancel_conditionally(parent_flow, "Flow", _node("Runner"))

    assert isinstance(outcome.failure, DeleteNodeResultFailure)
    assert "Runner" in str(outcome.failure.result_details)
    assert "could not cancel" in str(outcome.failure.result_details)

    # The queue must not be cleared on the way out: the run is still going, and clearing the work it
    # has yet to reach would break it in a second way.
    parent_flow.clear_execution_queue.assert_not_called()
