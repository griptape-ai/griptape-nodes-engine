"""``Connections.unresolve_future_nodes`` with a caller-owned ``visited`` set.

The scheduler marks every node downstream of an executing node stale. Done with a fresh set per
call, a chain of N nodes is walked N times per run. Handing the walk one set per run makes it
incremental: a node already in the set is skipped, and the scheduler takes a node back out of the
set when it finishes executing so a later upstream re-run can reach it again.
"""

import itertools
from unittest.mock import MagicMock

from griptape_nodes.exe_types.connections import Connections
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import BaseNode, NodeResolutionState

DATA_PARAMETER = "value"


class _Link(BaseNode):
    def __init__(self, name: str) -> None:
        super().__init__(name, state=NodeResolutionState.RESOLVED, engine=MagicMock())
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


def _parameter(node: _Link) -> Parameter:
    parameter = node.get_parameter_by_name(DATA_PARAMETER)
    assert parameter is not None
    return parameter


def _chain(*names: str) -> tuple[Connections, list[_Link]]:
    connections = Connections()
    nodes = [_Link(name) for name in names]
    for upstream, downstream in itertools.pairwise(nodes):
        connections.add_connection(upstream, _parameter(upstream), downstream, _parameter(downstream))
    return connections, nodes


def test_fresh_walk_unresolves_everything_downstream() -> None:
    """With no shared set, every RESOLVED node downstream of the root is marked stale."""
    connections, (a, b, c) = _chain("A", "B", "C")

    connections.unresolve_future_nodes(a)

    assert a.state == NodeResolutionState.RESOLVED  # the walk never touches its root
    assert b.state == NodeResolutionState.UNRESOLVED
    assert c.state == NodeResolutionState.UNRESOLVED


def test_shared_visited_set_skips_nodes_already_walked_this_run() -> None:
    """A root already in the shared set is not walked again."""
    connections, (a, b, c) = _chain("A", "B", "C")
    visited: set[str] = set()

    connections.unresolve_future_nodes(a, visited=visited)
    assert visited == {"A", "B", "C"}

    # B and C re-resolve (as they would after executing), but nothing removed them from the set,
    # so a second walk from A is a no-op: the run already marked them.
    b.state = NodeResolutionState.RESOLVED
    c.state = NodeResolutionState.RESOLVED
    connections.unresolve_future_nodes(a, visited=visited)

    assert b.state == NodeResolutionState.RESOLVED
    assert c.state == NodeResolutionState.RESOLVED


def test_node_taken_out_of_the_set_is_walked_again_but_not_past_nodes_still_in_it() -> None:
    """Discarding a node re-arms its walk; the walk still stops at nodes left in the set."""
    connections, (a, b, c, d) = _chain("A", "B", "C", "D")
    visited: set[str] = set()
    connections.unresolve_future_nodes(a, visited=visited)

    # A and B finish executing, so the scheduler discards them. C and D have not run yet and stay
    # in the set. (They are RESOLVED here only so the test can see which of them the walk touched.)
    for node in (b, c, d):
        node.state = NodeResolutionState.RESOLVED
    visited.discard("A")
    visited.discard("B")

    connections.unresolve_future_nodes(a, visited=visited)

    # The walk flips a target when it crosses the edge into it, then stops descending at a node
    # already in the set: B (fresh) and C (B's direct target) are marked, D behind C is not.
    assert b.state == NodeResolutionState.UNRESOLVED
    assert c.state == NodeResolutionState.UNRESOLVED
    assert d.state == NodeResolutionState.RESOLVED
    assert visited == {"A", "B", "C", "D"}


def test_collect_downstream_data_nodes_matches_the_unresolve_walk_without_touching_state() -> None:
    """The collect-only walk names the same nodes the unresolve walk would flip, and flips nothing."""
    connections, (a, b, c) = _chain("A", "B", "C")

    downstream = connections.collect_downstream_data_nodes(a)

    assert downstream == {"B", "C"}
    assert (a.state, b.state, c.state) == (NodeResolutionState.RESOLVED,) * 3


def test_collect_downstream_data_nodes_shares_a_visited_set_across_roots() -> None:
    """Two roots feeding one chain are walked once; the shared set stops the second descent."""
    connections, (a, b, _c) = _chain("A", "B", "C")
    visited: set[str] = set()

    first = connections.collect_downstream_data_nodes(a, visited)
    second = connections.collect_downstream_data_nodes(b, visited)

    assert first == {"B", "C"}
    assert second == set()  # B was already visited through A
    assert visited == {"A", "B", "C"}
