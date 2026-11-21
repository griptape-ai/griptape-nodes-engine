"""When the upstream data walk trusts a RESOLVED feeder's value, and when it makes the reader wait.

A RESOLVED feeder holds a current value unless the run can still give it a new one. Only a node wired
into a control flow is run by control, only in a run that hands control on, and not again once it has
finished in this run. A plain data node gets a new value when something it reads does.
"""

from unittest.mock import MagicMock

from griptape_nodes.exe_types.connections import Connections
from griptape_nodes.exe_types.core_types import (
    ControlParameterInput,
    ControlParameterOutput,
    Parameter,
    ParameterMode,
)
from griptape_nodes.exe_types.node_types import BaseNode, NodeResolutionState
from griptape_nodes.machines.dag_builder import DagBuilder, DagNode, NodeState


class _Node(BaseNode):
    """Reads `text`, writes `result`, and has control ports that are only wired when a test wires them."""

    def __init__(self, name: str) -> None:
        super().__init__(name, state=NodeResolutionState.RESOLVED, engine=MagicMock())
        self.add_parameter(ControlParameterInput(tooltip="Flow in", name="exec_in"))
        self.add_parameter(ControlParameterOutput(tooltip="Flow out", name="exec_out"))
        self.add_parameter(
            Parameter(
                name="text",
                type="str",
                default_value="",
                tooltip="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="result",
                type="str",
                default_value="",
                tooltip="",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None: ...


def _feed(connections: Connections, source: _Node, target: _Node) -> None:
    """Wire source.result -> target.text."""
    source_param = source.get_parameter_by_name("result")
    target_param = target.get_parameter_by_name("text")
    assert source_param is not None
    assert target_param is not None
    connections.add_connection(source, source_param, target, target_param)


def _control(connections: Connections, source: _Node, target: _Node) -> None:
    """Wire source.exec_out -> target.exec_in."""
    source_param = source.get_parameter_by_name("exec_out")
    target_param = target.get_parameter_by_name("exec_in")
    assert source_param is not None
    assert target_param is not None
    connections.add_connection(source, source_param, target, target_param)


def _waits_for(dag_builder: DagBuilder, reader: _Node, feeder: _Node) -> bool:
    """Seed `reader` and report whether the walk made it wait for `feeder`."""
    dag_builder.add_node_with_dependencies(reader, reader.name)
    graph = dag_builder.graphs[reader.name]
    return feeder.name in graph.nodes() and graph.in_degree(reader.name) > 0


class TestResolvedFeeder:
    def test_a_plain_data_feeder_is_trusted(self) -> None:
        connections = Connections()
        feeder, reader = _Node("Feeder"), _Node("Reader")
        _feed(connections, feeder, reader)

        assert not _waits_for(DagBuilder(connections), reader, feeder)

    def test_a_control_wired_feeder_makes_the_reader_wait(self) -> None:
        connections = Connections()
        entry, feeder, reader = _Node("Entry"), _Node("Feeder"), _Node("Reader")
        _control(connections, entry, feeder)
        _feed(connections, feeder, reader)

        assert _waits_for(DagBuilder(connections), reader, feeder)

    def test_a_control_wired_feeder_is_trusted_in_a_run_that_does_not_advance_control(self) -> None:
        connections = Connections()
        entry, feeder, reader = _Node("Entry"), _Node("Feeder"), _Node("Reader")
        _control(connections, entry, feeder)
        _feed(connections, feeder, reader)

        dag_builder = DagBuilder(connections, run_advances_control=lambda: False)

        assert not _waits_for(dag_builder, reader, feeder)

    def test_a_control_wired_feeder_that_finished_this_run_is_trusted(self) -> None:
        connections = Connections()
        entry, feeder, reader = _Node("Entry"), _Node("Feeder"), _Node("Reader")
        _control(connections, entry, feeder)
        _feed(connections, feeder, reader)
        dag_builder = DagBuilder(connections)
        dag_builder.node_to_reference["Feeder"] = DagNode(node_reference=feeder, node_state=NodeState.DONE)

        dag_builder.add_node_with_dependencies(reader, reader.name)

        assert dag_builder.graphs["Reader"].in_degree("Reader") == 0

    def test_a_data_feeder_that_reads_a_control_wired_node_makes_the_reader_wait(self) -> None:
        connections = Connections()
        entry, control_node, data_node, reader = _Node("Entry"), _Node("Ctrl"), _Node("Data"), _Node("Reader")
        _control(connections, entry, control_node)
        _feed(connections, control_node, data_node)
        _feed(connections, data_node, reader)

        dag_builder = DagBuilder(connections)

        assert _waits_for(dag_builder, reader, data_node)
        assert "Ctrl" in dag_builder.graphs["Reader"].nodes(), "the walk should go on to the node that will change"


class TestHasControlConnection:
    def test_data_wires_alone_are_not_control(self) -> None:
        connections = Connections()
        feeder, reader = _Node("Feeder"), _Node("Reader")
        _feed(connections, feeder, reader)

        assert not connections.has_control_connection(feeder)
        assert not connections.has_control_connection(reader)

    def test_a_control_wire_counts_at_both_ends(self) -> None:
        connections = Connections()
        source, target = _Node("Source"), _Node("Target")
        _control(connections, source, target)

        assert connections.has_control_connection(source)
        assert connections.has_control_connection(target)
