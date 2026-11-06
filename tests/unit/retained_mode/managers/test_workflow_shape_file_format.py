from typing import Any

import pytest

from griptape_nodes.exe_types.connections import Connections
from griptape_nodes.exe_types.core_types import Parameter
from griptape_nodes.exe_types.node_types import DataNode, StartNode
from griptape_nodes.retained_mode.managers.workflow.shape import (
    SHAPE_FILE_FORMAT_KEY,
    WorkflowShapeType,
    convert_parameter_to_minimal_dict,
    create_workflow_shape_from_nodes,
)
from griptape_nodes.traits.file_format import FileFormat, FileFormatKind
from griptape_nodes.traits.options import Options


class _FormatStartNode(StartNode):
    """StartNode exposing a plain `output_format` string, as the editor builds it."""

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata)
        self.add_parameter(Parameter(name="output_format", type="str", default_value="png", tooltip="format"))

    def process(self) -> None: ...


class _FormatNode(DataNode):
    """Node whose `output_format` carries a `FileFormat` of the given kind, or none."""

    def __init__(self, name: str, kind: FileFormatKind | None) -> None:
        super().__init__(name)
        traits: set = {Options(choices=["png", "exr", "auto"])}
        if kind is not None:
            traits.add(FileFormat(kind=kind))
        self.add_parameter(
            Parameter(name="output_format", type="str", default_value="png", tooltip="format", traits=traits)
        )

    def process(self) -> None: ...


def _input_shape(start: StartNode, connections: Connections) -> dict[str, Any]:
    shape = create_workflow_shape_from_nodes(
        nodes=[start],
        workflow_shape={WorkflowShapeType.INPUT: {}, WorkflowShapeType.OUTPUT: {}},
        workflow_shape_type=WorkflowShapeType.INPUT,
        connections=connections,
    )
    return shape[WorkflowShapeType.INPUT][start.name]["output_format"]


def _format_parameter(node: StartNode | DataNode) -> Parameter:
    parameter = node.get_parameter_by_name("output_format")
    assert parameter is not None
    return parameter


def _connect(connections: Connections, start: StartNode, target: DataNode) -> None:
    connections.add_connection(start, _format_parameter(start), target, _format_parameter(target))


@pytest.mark.usefixtures("engine")
class TestWorkflowShapeFileFormat:
    def test_parameter_with_trait_reports_its_kind(self) -> None:
        node = _FormatNode("Generate", FileFormatKind.IMAGE)

        info = convert_parameter_to_minimal_dict(_format_parameter(node))

        assert info[SHAPE_FILE_FORMAT_KEY] == "image"

    def test_parameter_without_trait_reports_no_kind(self) -> None:
        node = _FormatNode("Generate", None)

        info = convert_parameter_to_minimal_dict(_format_parameter(node))

        assert SHAPE_FILE_FORMAT_KEY not in info

    def test_start_parameter_takes_kind_of_connected_parameter(self) -> None:
        start = _FormatStartNode("Start Flow")
        target = _FormatNode("Generate", FileFormatKind.THREE_D)
        connections = Connections()
        _connect(connections, start, target)

        assert _input_shape(start, connections)[SHAPE_FILE_FORMAT_KEY] == "three_d"

    def test_start_parameter_feeding_unmarked_parameter_reports_no_kind(self) -> None:
        start = _FormatStartNode("Start Flow")
        target = _FormatNode("Generate", None)
        connections = Connections()
        _connect(connections, start, target)

        assert SHAPE_FILE_FORMAT_KEY not in _input_shape(start, connections)

    def test_start_parameter_feeding_disagreeing_kinds_reports_no_kind(self) -> None:
        start = _FormatStartNode("Start Flow")
        image = _FormatNode("Image", FileFormatKind.IMAGE)
        video = _FormatNode("Video", FileFormatKind.VIDEO)
        connections = Connections()
        _connect(connections, start, image)
        _connect(connections, start, video)

        assert SHAPE_FILE_FORMAT_KEY not in _input_shape(start, connections)

    def test_trait_state_round_trips(self) -> None:
        trait = FileFormat(kind=FileFormatKind.AUDIO)

        rebuilt = FileFormat.from_state(trait.to_state())

        assert rebuilt.kind is FileFormatKind.AUDIO
