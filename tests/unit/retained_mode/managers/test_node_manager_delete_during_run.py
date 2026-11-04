"""Edges of the mid-run delete policy that a full run cannot easily reach.

The e2e suite covers what an artist can actually do to a workflow. Several branches sit outside that
reach: the gated-candidate rule, which needs the scheduler parked in a state a test cannot hold open
on demand; the delete that is refused because cancelling the run itself failed, since there is no
input that makes a healthy engine fail to cancel; and the exact shape of the forward walk over the
control graph, which a run reaches only through whichever topology it happens to be executing.
"""

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from griptape_nodes.exe_types.connections import Connections
from griptape_nodes.exe_types.core_types import ControlParameterOutput, Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import BaseNode, ControlNode
from griptape_nodes.machines.dag_builder import DagBuilder, DagNode, NodeState
from griptape_nodes.retained_mode.events.node_events import DeleteNodeResultFailure
from griptape_nodes.retained_mode.managers.event_manager import EventManager
from griptape_nodes.retained_mode.managers.node_manager import NodeManager

LOOP_BACK_PARAMETER = "loop_back"
DATA_PARAMETER = "value"


class _ChainNode(ControlNode):
    """A real control node, for the cases that need a real control graph to walk.

    `ControlNode.__init__` builds `exec_in`/`exec_out` on its own, so a bare subclass is already a
    valid link in a chain. The extra output exists so a node can branch two ways -- which is what
    lets a test build a control *cycle* without a parameter carrying two connections.
    """

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.add_parameter(ControlParameterOutput(name=LOOP_BACK_PARAMETER, display_name="Loop Back"))
        self.add_parameter(
            Parameter(
                name=DATA_PARAMETER,
                type="str",
                default_value="",
                tooltip="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY, ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None: ...


def _parameter(node: BaseNode, name: str) -> Parameter:
    parameter = node.get_parameter_by_name(name)
    assert parameter is not None, f"{node.name} is missing parameter {name!r}"
    return parameter


def _connect_control(connections: Connections, source: BaseNode, source_parameter: str, target: BaseNode) -> None:
    connections.add_connection(source, _parameter(source, source_parameter), target, _parameter(target, "exec_in"))


def _connect_data(connections: Connections, source: BaseNode, target: BaseNode) -> None:
    connections.add_connection(source, _parameter(source, DATA_PARAMETER), target, _parameter(target, DATA_PARAMETER))


def _node(name: str) -> MagicMock:
    """A stand-in node. `MagicMock(name=...)` names the mock, not the node, so set it after."""
    node = MagicMock()
    node.name = name
    return node


def _outgoing_to(*target_nodes: object) -> list[MagicMock]:
    """Outgoing connections from the node being deleted, one per target."""
    connections = []
    for target_node in target_nodes:
        connection = MagicMock()
        connection.target_node = target_node
        connections.append(connection)
    return connections


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


def test_a_consumer_the_run_has_already_gone_past_does_not_hold_the_node() -> None:
    """Nothing live leads to it, so the forward walk must not even be attempted.

    A consumer absent from the DAG is only "still coming" if some node the run has live right now
    leads to it. When every DAG entry has settled there is nothing to walk from, and asking anyway
    would be reading the topology instead of the run -- which would make every node with a consumer
    anywhere downstream permanently undeletable.
    """
    dag_builder = DagBuilder(MagicMock())
    dag_builder.node_to_reference["Finished"] = DagNode(node_reference=_node("Finished"), node_state=NodeState.DONE)

    engine = _engine(dag_builder, outgoing=_outgoing_to(_node("AbsentConsumer")))
    node_manager = _node_manager(engine)

    assert node_manager._find_entangled_live_node(_node("Deleted")) is None
    engine.flow_manager.get_connections.return_value.is_node_in_forward_control_path.assert_not_called()


def test_an_errored_node_whose_consumer_never_collected_is_still_entangled() -> None:
    """The deleted node being finished with is not the question -- its consumers are.

    An errored node has settled, so the rule about the node's own state lets it through. But a
    consumer still sitting in the DAG unstarted has not collected anything from it, because
    collection happens at the consumer's own dispatch. Deleting the supplier now would let that
    consumer run on its parameter default.
    """
    dag_builder = DagBuilder(MagicMock())
    deleted = _node("Errored")
    consumer = _node("Consumer")
    dag_builder.node_to_reference["Errored"] = DagNode(node_reference=deleted, node_state=NodeState.ERRORED)
    dag_builder.node_to_reference["Consumer"] = DagNode(node_reference=consumer, node_state=NodeState.WAITING)

    node_manager = _node_manager(_engine(dag_builder, outgoing=_outgoing_to(consumer)))

    assert node_manager._find_entangled_live_node(deleted) == "Consumer"


def test_a_consumer_beyond_a_control_loop_is_found_without_the_walk_hanging() -> None:
    """Loops are ordinary here, so the forward walk has to survive one.

    ``Head -> Body -> Tail`` with ``Body`` looping back to ``Head``. ``Consumer`` hangs off ``Tail``,
    past the loop, and the run has ``Head`` live. An unbounded walk would circle the loop forever and
    hang the artist's delete instead of answering it.
    """
    head = _ChainNode("Head")
    body = _ChainNode("Body")
    tail = _ChainNode("Tail")
    consumer = _ChainNode("Consumer")
    deleted = _ChainNode("Deleted")

    connections = Connections()
    _connect_control(connections, head, "exec_out", body)
    _connect_control(connections, body, "exec_out", tail)
    _connect_control(connections, body, LOOP_BACK_PARAMETER, head)
    _connect_control(connections, tail, "exec_out", consumer)
    _connect_data(connections, deleted, consumer)

    dag_builder = DagBuilder(MagicMock())
    dag_builder.node_to_reference["Head"] = DagNode(node_reference=head, node_state=NodeState.PROCESSING)

    engine = _engine(dag_builder)
    engine.flow_manager.get_connections.return_value = connections
    node_manager = _node_manager(engine)

    assert node_manager._find_entangled_live_node(deleted) == "Consumer"


def test_a_consumer_the_control_graph_never_reaches_is_free() -> None:
    """The same loop, and a consumer that hangs off nothing the run walks into.

    Paired with the test above so the walk is shown to discriminate rather than to say yes: if it
    answered True for anything absent, both tests would still pass individually and the policy would
    have quietly become "never allow a delete".
    """
    head = _ChainNode("Head")
    body = _ChainNode("Body")
    stranded = _ChainNode("Stranded")
    deleted = _ChainNode("Deleted")

    connections = Connections()
    _connect_control(connections, head, "exec_out", body)
    _connect_control(connections, body, LOOP_BACK_PARAMETER, head)
    _connect_data(connections, deleted, stranded)

    dag_builder = DagBuilder(MagicMock())
    dag_builder.node_to_reference["Head"] = DagNode(node_reference=head, node_state=NodeState.PROCESSING)

    engine = _engine(dag_builder)
    engine.flow_manager.get_connections.return_value = connections
    node_manager = _node_manager(engine)

    assert node_manager._find_entangled_live_node(deleted) is None


@dataclass
class _QueuedSiblingScene:
    """A run parked with one sibling queued, and a supplier feeding it from off the chain."""

    node_manager: NodeManager
    supplier: BaseNode


def _queued_sibling_scene(*, via_data_hop: bool) -> _QueuedSiblingScene:
    """`Start` (done) led to `Fast` (queued) and `Slow` (running); `Loader` feeds `Fast`.

    Nothing live leads *to* `Fast` any more -- `Start` has settled, and `Slow` is off on its own
    branch -- so the only thing that knows `Fast` has not collected yet is its DAG state. The two
    tests differ by exactly one data node standing between the supplier and `Fast`.
    """
    start = _ChainNode("Start")
    fast = _ChainNode("Fast")
    slow = _ChainNode("Slow")
    loader = _ChainNode("Loader")

    connections = Connections()
    _connect_control(connections, start, "exec_out", fast)
    _connect_control(connections, start, LOOP_BACK_PARAMETER, slow)
    if via_data_hop:
        resizer = _ChainNode("Resizer")
        _connect_data(connections, loader, resizer)
        _connect_data(connections, resizer, fast)
    else:
        _connect_data(connections, loader, fast)

    dag_builder = DagBuilder(MagicMock())
    dag_builder.node_to_reference["Start"] = DagNode(node_reference=start, node_state=NodeState.DONE)
    dag_builder.node_to_reference["Fast"] = DagNode(node_reference=fast, node_state=NodeState.QUEUED)
    dag_builder.node_to_reference["Slow"] = DagNode(node_reference=slow, node_state=NodeState.PROCESSING)

    engine = _engine(dag_builder)
    engine.flow_manager.get_connections.return_value = connections
    return _QueuedSiblingScene(node_manager=_node_manager(engine), supplier=loader)


def test_direct_data_connection_to_a_queued_sibling_cancels() -> None:
    """Wired straight in, the queued consumer is found by its DAG state alone.

    The control walk cannot help here and does not need to: the consumer is already in the DAG, and
    `QUEUED` says it has not collected its inputs yet. This is the shape the hop test is measured
    against, so that the hop is the only difference between them.
    """
    scene = _queued_sibling_scene(via_data_hop=False)

    assert scene.node_manager._find_entangled_live_node(scene.supplier) == "Fast"


def test_a_queued_sibling_reached_through_a_data_hop_also_cancels() -> None:
    """One data node in between must not hide it.

    `Resizer` is absent from the DAG because it is pulled in as `Fast`'s dependency when `Fast` is
    dispatched, so the question about `Resizer` is really the question about `Fast` -- and `Fast` is
    sitting there uncollected. The name reported is the direct target, `Resizer`, because that is the
    node whose own connection the artist deleted.
    """
    scene = _queued_sibling_scene(via_data_hop=True)

    assert scene.node_manager._find_entangled_live_node(scene.supplier) == "Resizer"


def test_the_walk_stops_at_a_consumer_that_has_already_collected() -> None:
    """A consumer already dispatched holds the good value, so nothing past it is damaged.

    `Fast` is running, which means it collected `Resizer`'s output before the delete. `Beyond` is
    further downstream and the run is still going to arrive at it, but only through a value that was
    never wrong -- so continuing the walk past `Fast` would cancel a run that is fine. Paired with the
    test above: without this one, answering True for anything reachable would still look correct.
    """
    fast = _ChainNode("Fast")
    slow = _ChainNode("Slow")
    loader = _ChainNode("Loader")
    resizer = _ChainNode("Resizer")
    beyond = _ChainNode("Beyond")

    connections = Connections()
    _connect_data(connections, loader, resizer)
    _connect_data(connections, resizer, fast)
    _connect_data(connections, fast, beyond)
    _connect_control(connections, slow, "exec_out", beyond)

    dag_builder = DagBuilder(MagicMock())
    dag_builder.node_to_reference["Fast"] = DagNode(node_reference=fast, node_state=NodeState.PROCESSING)
    dag_builder.node_to_reference["Slow"] = DagNode(node_reference=slow, node_state=NodeState.PROCESSING)

    engine = _engine(dag_builder)
    engine.flow_manager.get_connections.return_value = connections
    node_manager = _node_manager(engine)

    assert node_manager._find_entangled_live_node(loader) is None


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
