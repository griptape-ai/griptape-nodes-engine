"""Load Image -> paid generation node -> Compare Images, with the standard nodes' parameter shapes."""

from __future__ import annotations

from typing import Any, ClassVar

from griptape.artifacts import ImageUrlArtifact

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import ControlNode, SuccessFailureNode
from griptape_nodes.exe_types.param_types.parameter_dict import ParameterDict
from griptape_nodes.exe_types.param_types.parameter_image import ParameterImage
from griptape_nodes.traits.compare_images import CompareImagesTrait


class FakeLoadImage(SuccessFailureNode):
    """Same `image` parameter as the standard library's Load Image."""

    process_counts: ClassVar[dict[str, int]] = {}

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self._create_status_parameters()
        self.add_parameter(
            Parameter(
                name="image",
                input_types=["ImageUrlArtifact", "ImageArtifact", "str"],
                type="ImageUrlArtifact",
                output_type="ImageUrlArtifact",
                default_value=None,
                tooltip="The loaded image.",
            )
        )
        self.add_parameter(
            Parameter(
                name="path",
                type="str",
                default_value="",
                tooltip="Path to a local image file or URL",
                allowed_modes={ParameterMode.PROPERTY, ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="run_count",
                type="int",
                default_value=0,
                tooltip="How many times process() has run",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        count = FakeLoadImage.process_counts.get(self.name, 0) + 1
        FakeLoadImage.process_counts[self.name] = count
        self.parameter_output_values["run_count"] = count
        self._clear_execution_status()
        self.parameter_output_values["image"] = None
        image = self.get_parameter_value("image")
        self.parameter_output_values["image"] = image
        self._set_status_results(was_successful=True, result_details="ok")


class FakeGenerate(SuccessFailureNode):
    """An image generation node: `image` in, `image_url` out, and every run costs a generation."""

    process_counts: ClassVar[dict[str, int]] = {}

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self._create_status_parameters()
        self.add_parameter(
            Parameter(
                name="prompt",
                type="str",
                default_value="",
                tooltip="prompt",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            ParameterImage(
                name="image",
                default_value="",
                tooltip="Input image to edit",
                allowed_modes={ParameterMode.INPUT},
                hide_property=True,
            )
        )
        self.add_parameter(
            ParameterImage(
                name="image_url",
                tooltip="Generated image",
                allowed_modes={ParameterMode.OUTPUT, ParameterMode.PROPERTY},
                settable=False,
            )
        )
        self.add_parameter(
            Parameter(
                name="run_count",
                type="int",
                default_value=0,
                tooltip="How many times process() has run",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        count = FakeGenerate.process_counts.get(self.name, 0) + 1
        FakeGenerate.process_counts[self.name] = count
        self.parameter_output_values["run_count"] = count
        self._clear_execution_status()
        self.parameter_output_values["image_url"] = ImageUrlArtifact(
            value=f"/opt/renders/gen-{count}.png", name=f"gen-{count}.png"
        )
        self._set_status_results(was_successful=True, result_details="ok")


class FakeCompareImages(ControlNode):
    """Verbatim copy of the standard library's Compare Images."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.add_parameter(
            ParameterImage(
                name="Image_1",
                tooltip="Image 1",
                default_value=None,
                allowed_modes={ParameterMode.INPUT},
                hide_property=True,
            )
        )
        self.add_parameter(
            ParameterImage(
                name="Image_2",
                tooltip="Image 2",
                default_value=None,
                allowed_modes={ParameterMode.INPUT},
                hide_property=True,
            )
        )
        self.add_parameter(
            ParameterDict(
                name="Compare",
                tooltip="Compare two images",
                default_value={"input_image_1": None, "input_image_2": None},
                allowed_modes={ParameterMode.PROPERTY},
                traits={CompareImagesTrait()},
                ui_options={"compare": True},
            )
        )

    def _update_compare(self) -> None:
        image_1 = self.get_parameter_value("Image_1")
        image_2 = self.get_parameter_value("Image_2")
        result_dict = {"input_image_1": image_1, "input_image_2": image_2}
        self.set_parameter_value("Compare", result_dict)
        self.parameter_output_values["Compare"] = result_dict

    def after_value_set(self, parameter: Parameter, value: Any) -> None:
        if parameter.name not in {"Image_1", "Image_2"}:
            return super().after_value_set(parameter, value)
        self._update_compare()
        return super().after_value_set(parameter, value)

    def process(self) -> None:
        self._update_compare()
