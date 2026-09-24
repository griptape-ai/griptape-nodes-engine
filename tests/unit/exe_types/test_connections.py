from griptape_nodes.exe_types.connections import Connections
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode, ParameterTypeBuiltin
from griptape_nodes.exe_types.node_types import ControlNode, DataNode

EXPECTED_CONNECTION_COUNT = 2
EXPECTED_FIRST_TARGET_CONNECTION_COUNT = 1


class _AnyOutputNode(DataNode):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.value = Parameter(
            name="value",
            output_type=ParameterTypeBuiltin.ALL.value,
            allowed_modes={ParameterMode.OUTPUT},
        )
        self.add_parameter(self.value)

    def process(self) -> None:
        return None


class _FlowInNode(ControlNode):
    def process(self) -> None:
        return None


def test_duplicate_connection_is_idempotent_for_all_output_to_flow_in() -> None:
    """Duplicate endpoints reuse one stored connection while distinct targets remain independent."""
    source = _AnyOutputNode("Source")
    first_target = _FlowInNode("FirstTarget")
    second_target = _FlowInNode("SecondTarget")
    connections = Connections()

    first = connections.add_connection(source, source.value, first_target, first_target.control_parameter_in)
    duplicate = connections.add_connection(source, source.value, first_target, first_target.control_parameter_in)
    connections.add_connection(source, source.value, second_target, second_target.control_parameter_in)

    assert duplicate is first
    assert connections.has_connection("Source", "value", "FirstTarget", first_target.control_parameter_in.name)
    assert len(connections.connections) == EXPECTED_CONNECTION_COUNT
    assert len(connections.outgoing_index["Source"]["value"]) == EXPECTED_CONNECTION_COUNT
    assert (
        len(connections.incoming_index["FirstTarget"][first_target.control_parameter_in.name])
        == EXPECTED_FIRST_TARGET_CONNECTION_COUNT
    )
