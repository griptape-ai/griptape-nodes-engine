"""A node module that is base-clean because it has nowhere to put a heavy import.

Nothing here imports the library's execution dependency, not even deferred inside a method.
The heavy work lives behind the declared execution-module boundary, and this module reaches it
through the engine rather than importing it -- so there is no import discipline to forget.
"""

from __future__ import annotations

from typing import Any

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode


class BoundaryNode(DataNode):
    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="reported_version",
                type="str",
                default_value="",
                tooltip="Version the execution module read from the execution dependency",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        runner = self.execution_module("runner")
        self.parameter_output_values["reported_version"] = runner.dependency_version()
