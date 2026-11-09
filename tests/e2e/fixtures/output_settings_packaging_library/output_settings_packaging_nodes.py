"""Nodes for checking that packaging a group carries in the output settings wired to it from outside.

``SettingsNode`` stands in for File Output Settings: it supplies a destination through
``FileDestinationProvider`` and has an output to wire into a saving node. ``SaverNode`` has the
input a settings node is wired to, and ``PlainNode`` feeds an ordinary value.

The library is registered under the name ``Griptape Nodes Library`` because that is the name the
packager asks for its flow endpoints by.
"""

from __future__ import annotations

from typing import Any

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_groups import SubflowNodeGroup
from griptape_nodes.exe_types.node_types import DataNode, EndNode, StartNode
from griptape_nodes.files.file import FileDestination


class StartFlow(StartNode):
    """The packaged flow's entry node."""

    def process(self) -> None:
        return None


class EndFlow(EndNode):
    """The packaged flow's exit node."""


class GroupNode(SubflowNodeGroup):
    """Minimal concrete SubflowNodeGroup."""

    async def aprocess(self) -> None:
        await self.execute_subflow()

    def process(self) -> Any:
        pass


class SettingsNode(DataNode):
    """Supplies a destination built from ``filename``, as File Output Settings does."""

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="filename",
                tooltip="Where the destination points",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="file_destination",
                tooltip="Wire to a saving node",
                type="str",
                output_type="str",
                default_value=None,
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    @property
    def file_destination(self) -> FileDestination | None:
        filename = self.get_parameter_value("filename")
        if not filename:
            return None
        return FileDestination(filename)

    def process(self) -> None:
        pass


class SaverNode(DataNode):
    """Has the input a settings node is wired to, and one an ordinary node feeds."""

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        for parameter_name in ("output", "note"):
            self.add_parameter(
                Parameter(
                    name=parameter_name,
                    tooltip="",
                    type="str",
                    input_types=["str"],
                    default_value="",
                    allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                )
            )

    def process(self) -> None:
        pass


class PlainNode(DataNode):
    """Feeds an ordinary value."""

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="text",
                tooltip="",
                type="str",
                output_type="str",
                default_value="hello",
                allowed_modes={ParameterMode.OUTPUT, ParameterMode.PROPERTY},
            )
        )

    def process(self) -> None:
        self.parameter_output_values["text"] = self.get_parameter_value("text")
