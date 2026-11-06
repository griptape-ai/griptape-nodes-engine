from typing import Any

import pytest

from griptape_nodes.exe_types.core_types import Parameter
from griptape_nodes.exe_types.node_types import DataNode, StartNode
from griptape_nodes.retained_mode.managers.workflow.shape import (
    WorkflowShapeType,
    convert_parameter_to_minimal_dict,
    create_workflow_shape_from_nodes,
)
from griptape_nodes.traits.file_format import FILE_FORMAT_UI_OPTION, FileFormat, FileFormatKind
from griptape_nodes.traits.options import Options


class _FormatStartNode(StartNode):
    """StartNode whose `output_format` carries the UI options the editor copied from the parameter it feeds."""

    def __init__(self, name: str, ui_options: dict[str, Any] | None = None) -> None:
        super().__init__(name)
        self.add_parameter(
            Parameter(name="output_format", type="str", default_value="png", tooltip="format", ui_options=ui_options)
        )

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


def _format_parameter(node: StartNode | DataNode) -> Parameter:
    parameter = node.get_parameter_by_name("output_format")
    assert parameter is not None
    return parameter


def _shape_ui_options(node: StartNode | DataNode) -> dict[str, Any]:
    return convert_parameter_to_minimal_dict(_format_parameter(node))["ui_options"]


@pytest.mark.usefixtures("engine")
class TestWorkflowShapeFileFormat:
    def test_trait_renders_its_kind_as_a_ui_option(self) -> None:
        parameter = _format_parameter(_FormatNode("Generate", FileFormatKind.THREE_D))

        assert parameter.ui_options[FILE_FORMAT_UI_OPTION] == "threed"

    def test_parameter_with_trait_reports_its_kind_in_the_shape(self) -> None:
        assert _shape_ui_options(_FormatNode("Generate", FileFormatKind.IMAGE))[FILE_FORMAT_UI_OPTION] == "image"

    def test_parameter_without_trait_reports_no_kind(self) -> None:
        assert FILE_FORMAT_UI_OPTION not in _shape_ui_options(_FormatNode("Generate", None))

    def test_start_parameter_keeps_the_copied_kind(self) -> None:
        start = _FormatStartNode("Start Flow", ui_options={FILE_FORMAT_UI_OPTION: "threed"})

        shape = create_workflow_shape_from_nodes(
            nodes=[start],
            workflow_shape={WorkflowShapeType.INPUT: {}, WorkflowShapeType.OUTPUT: {}},
            workflow_shape_type=WorkflowShapeType.INPUT,
        )

        info = shape[WorkflowShapeType.INPUT]["Start Flow"]["output_format"]
        assert info["ui_options"][FILE_FORMAT_UI_OPTION] == "threed"

    def test_ui_option_write_updates_the_trait(self) -> None:
        parameter = _format_parameter(_FormatNode("Generate", FileFormatKind.IMAGE))

        parameter.ui_options = {FILE_FORMAT_UI_OPTION: "video"}

        trait = parameter.find_elements_by_type(FileFormat)[0]
        assert trait.kind is FileFormatKind.VIDEO
        assert parameter.ui_options[FILE_FORMAT_UI_OPTION] == "video"

    def test_trait_state_round_trips(self) -> None:
        trait = FileFormat(kind=FileFormatKind.AUDIO)

        rebuilt = FileFormat.from_state(trait.to_state())

        assert rebuilt.kind is FileFormatKind.AUDIO

    def test_traits_of_different_kinds_are_not_equal(self) -> None:
        assert FileFormat(kind=FileFormatKind.IMAGE) != FileFormat(kind=FileFormatKind.VIDEO)
