"""A node that consumes its own library's request handler.

Dispatch from `process()`, never from `__init__`. A node constructor that sends a request
trips the `reentrant-bus-in-init` strict-mode rule, and it can deadlock against handlers
that await engine startup.
"""

from __future__ import annotations

from typing import cast

from colorspace_events import (
    HSV,
    RGB,
    ConvertColorspaceRequest,
    ConvertColorspaceResultSuccess,
)

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.traits.options import Options


class ConvertColorspaceNode(DataNode):
    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="color",
                tooltip="Three channel values in 0.0-1.0",
                type="list",
                default_value=[1.0, 0.0, 0.0],
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="source",
                tooltip="Colorspace the input color is in",
                type="str",
                default_value=RGB,
                traits={Options(choices=[RGB, HSV])},
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="target",
                tooltip="Colorspace to convert to",
                type="str",
                default_value=HSV,
                traits={Options(choices=[RGB, HSV])},
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="converted",
                tooltip="The converted color",
                type="list",
                default_value=[0.0, 0.0, 0.0],
                allowed_modes={ParameterMode.OUTPUT, ParameterMode.PROPERTY},
            )
        )

    def process(self) -> None:
        color = self.get_parameter_value("color")
        request = ConvertColorspaceRequest(
            color=tuple(color),
            source=self.get_parameter_value("source"),
            target=self.get_parameter_value("target"),
        )
        result = GriptapeNodes.handle_request(request)

        # Always handle failure. The providing library may not be loaded at all, in which
        # case the engine returns a generic failure rather than this library's own.
        if result.failed():
            msg = f"Attempted to convert a color in '{self.name}'. Failed because {result.result_details}"
            raise RuntimeError(msg)

        success = cast("ConvertColorspaceResultSuccess", result)
        self.parameter_output_values["converted"] = list(success.color)
