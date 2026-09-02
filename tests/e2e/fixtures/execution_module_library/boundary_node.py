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
                name="probe_boundary",
                type="str",
                default_value="",
                tooltip="Set anything to make the value hook report what the boundary answers here",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="boundary_answer",
                type="str",
                default_value="",
                tooltip="What execution_module() answered in whichever process ran the hook",
                allowed_modes={ParameterMode.OUTPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="reported_version",
                type="str",
                default_value="",
                tooltip="Version the execution module read from the execution dependency",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def after_value_set(self, parameter: Parameter, value: Any) -> None:  # noqa: ARG002
        """Record what reaching for an execution module does wherever this hook runs.

        Value hooks fire on the orchestrator when a user edits, and again in the worker during
        hydration -- so this one parameter reports the boundary's answer from both sides without
        needing to inspect either process.
        """
        if parameter.name != "probe_boundary":
            return
        try:
            self.execution_module("runner")
            self.set_parameter_value("boundary_answer", "reached it")
        except RuntimeError as err:
            self.set_parameter_value("boundary_answer", str(err))

    def process(self) -> None:
        runner = self.execution_module("runner")
        self.parameter_output_values["reported_version"] = runner.dependency_version()
