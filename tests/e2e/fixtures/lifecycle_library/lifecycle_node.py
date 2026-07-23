"""Node fixture used by tests/e2e/test_library_module_lifecycle.py.

Defines a StrEnum used as a parameter default, mirroring the shape of the real
``SetVariablesFromData`` node's ``collision_behavior`` parameter. That shape is exactly what
reproduced the stable-namespace regression this suite guards against:
pickle stamps the enum class's ``__module__`` into any pickle of its members, so a value
pickled by one engine process must still resolve that module in a different one.

``CLASS_MARKER`` is bumped by tests that rewrite this file on disk to simulate an edit, and is
also exposed as the ``class_marker`` output parameter so a test driving the node purely through
public request APIs (no local Python import of this file) can observe which version is loaded.
Keep this file free of third-party imports; the library it belongs to must load with nothing
beyond the engine itself on the Python path.
"""

from __future__ import annotations

from enum import StrEnum

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode

CLASS_MARKER = "v1"


class TriggerBehavior(StrEnum):
    OVERWRITE = "Overwrite existing"
    PRESERVE = "Preserve existing"


class LifecycleNode(DataNode):
    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="trigger",
                tooltip="Trigger behavior",
                type="str",
                default_value=TriggerBehavior.OVERWRITE,
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="class_marker",
                tooltip="Marker identifying which version of this file is loaded",
                type="str",
                default_value=CLASS_MARKER,
                allowed_modes={ParameterMode.OUTPUT, ParameterMode.PROPERTY},
            )
        )

    def process(self) -> None:
        self.parameter_output_values["trigger"] = self.get_parameter_value("trigger")
        self.parameter_output_values["class_marker"] = CLASS_MARKER
