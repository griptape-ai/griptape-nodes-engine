"""Fixture nodes for the lazy-loading stable-namespace e2e tests.

LazyPayload is a plain picklable class defined inside a dynamically loaded library module.
When a workflow holding a LazyPayload parameter value is saved, the generated Python embeds
a ``from griptape_nodes.node_libraries.lazy_payload_library.lazy_payload_node import
LazyPayload`` statement plus pickled bytes referencing that stable namespace. Loading that
workflow therefore requires the stable namespace to be importable, which is exactly what the
lazy-node-loading regression broke (`No module named 'griptape_nodes.node_libraries'`).
"""

from __future__ import annotations

from typing import Any

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode


class LazyPayload:
    """Picklable value class whose only home is this dynamically loaded module."""

    def __init__(self, tag: str) -> None:
        self.tag = tag


class LazyPayloadNode(DataNode):
    """Holds a LazyPayload object in its `payload` parameter."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="payload",
                type="LazyPayload",
                default_value=None,
                tooltip="Payload object defined in this library module",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY, ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        self.parameter_output_values["payload"] = self.get_parameter_value("payload")
