"""Fixture nodes for the serializable=False resolution-state regression test (issue #4994).

ProducerNode has a serializable=False output parameter that produces a Session object
(a non-serializable runtime resource, like a driver or connection handle). ConsumerNode
reads from the producer's output via a data connection and extracts a string from it.

When the workflow is saved after running, the producer is RESOLVED but its output
value is not persisted (serializable=False). On load, the producer must NOT remain
RESOLVED — otherwise the consumer sees None instead of the recomputed value.
"""

from __future__ import annotations

from typing import Any

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode


class Session:
    """A non-serializable runtime resource (analogous to a driver, connection handle, etc.)."""

    def __init__(self, marker: str) -> None:
        self.marker = marker


class ProducerNode(DataNode):
    """Produces a non-serializable Session output."""

    EXPECTED_MARKER = "live-session-marker"

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="session",
                type="Session",
                default_value=None,
                tooltip="Non-serializable session output",
                allowed_modes={ParameterMode.OUTPUT},
                serializable=False,
            )
        )

    def process(self) -> None:
        self.parameter_output_values["session"] = Session(marker=self.EXPECTED_MARKER)


class ConsumerNode(DataNode):
    """Reads a Session from the producer and extracts its marker."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="session",
                type="Session",
                default_value=None,
                tooltip="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                serializable=False,
            )
        )
        self.add_parameter(
            Parameter(
                name="marker",
                type="str",
                default_value="",
                tooltip="",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        session = self.get_parameter_value("session")
        self.parameter_output_values["marker"] = session.marker
