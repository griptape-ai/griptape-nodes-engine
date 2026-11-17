import itertools
from unittest.mock import MagicMock

from griptape_nodes.exe_types.connections import Connections
from griptape_nodes.exe_types.core_types import (
    ControlParameterInput,
    ControlParameterOutput,
    Parameter,
    ParameterMode,
)
from griptape_nodes.exe_types.node_types import NodeResolutionState

from .mocks import MockNode


def _node(name: str, state: NodeResolutionState = NodeResolutionState.RESOLVED) -> MockNode:
    node = MockNode(name=name, engine=MagicMock())
    node.add_parameter(Parameter(name="text", tooltip="In", type="str", allowed_modes={ParameterMode.INPUT}))
    node.add_parameter(Parameter(name="result", tooltip="Out", type="str", allowed_modes={ParameterMode.OUTPUT}))
    node.add_parameter(ControlParameterInput(name="exec_in"))
    node.add_parameter(ControlParameterOutput(name="exec_out"))
    node.state = state
    return node


def _connect(connections: Connections, source: MockNode, target: MockNode, *, control: bool = False) -> None:
    source_name, target_name = ("exec_out", "exec_in") if control else ("result", "text")
    source_parameter = source.get_parameter_by_name(source_name)
    target_parameter = target.get_parameter_by_name(target_name)
    assert source_parameter is not None
    assert target_parameter is not None
    connections.add_connection(source, source_parameter, target, target_parameter)


class TestUnresolveFutureNodes:
    def test_unresolves_through_an_unresolved_node(self) -> None:
        connections = Connections()
        start, middle, end = _node("start"), _node("middle", NodeResolutionState.UNRESOLVED), _node("end")
        _connect(connections, start, middle)
        _connect(connections, middle, end)

        connections.unresolve_future_nodes(start)

        assert start.state == NodeResolutionState.RESOLVED
        assert end.state == NodeResolutionState.UNRESOLVED

    def test_control_connections_are_not_followed(self) -> None:
        connections = Connections()
        start, after = _node("start"), _node("after")
        _connect(connections, start, after, control=True)

        connections.unresolve_future_nodes(start)

        assert after.state == NodeResolutionState.RESOLVED

    def test_a_cycle_back_to_the_start_unresolves_it(self) -> None:
        connections = Connections()
        start, other = _node("start"), _node("other")
        _connect(connections, start, other)
        _connect(connections, other, start)

        connections.unresolve_future_nodes(start)

        assert start.state == NodeResolutionState.UNRESOLVED
        assert other.state == NodeResolutionState.UNRESOLVED

    def test_a_chain_longer_than_the_recursion_limit(self) -> None:
        connections = Connections()
        nodes = [_node(f"n{index}") for index in range(3000)]
        for source, target in itertools.pairwise(nodes):
            _connect(connections, source, target)

        connections.unresolve_future_nodes(nodes[0])

        assert all(node.state == NodeResolutionState.UNRESOLVED for node in nodes[1:])
