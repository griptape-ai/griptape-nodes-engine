"""Fixture nodes for subflow e2e tests.

EchoNode: copies its ``text`` input to its ``text`` output.

SubflowGroupNode: minimal concrete SubflowNodeGroup that runs all child nodes
in-process (LOCAL execution).  Used to verify that parameter values survive
the subflow round-trip without corruption.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_groups.subflow_node_group import SubflowNodeGroup
from griptape_nodes.exe_types.node_types import DataNode


class EchoNode(DataNode):
    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="text",
                tooltip="Text to echo",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY, ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        if not self.get_parameter_value("text"):
            msg = "Echo node must have text input"
            raise ValueError(msg)
        self.parameter_output_values["text"] = self.get_parameter_value("text")


class TimedEchoNode(DataNode):
    """Echo node whose aprocess sleeps and records its start/end wall-clock times.

    Independent instances that resolve concurrently will have overlapping
    [start_ts, end_ts] intervals; instances forced to run serially will not.
    Used to assert parallel execution inside a SubflowNodeGroup.
    """

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="text",
                tooltip="Text to echo",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY, ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="delay",
                tooltip="Seconds to sleep during processing",
                type="float",
                default_value=0.3,
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="start_ts",
                tooltip="monotonic() time when processing started",
                type="float",
                default_value=0.0,
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="end_ts",
                tooltip="monotonic() time when processing finished",
                type="float",
                default_value=0.0,
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    async def aprocess(self) -> None:
        self.parameter_output_values["start_ts"] = time.monotonic()
        await asyncio.sleep(self.get_parameter_value("delay") or 0.0)
        self.parameter_output_values["text"] = self.get_parameter_value("text") or ""
        self.parameter_output_values["end_ts"] = time.monotonic()


class SubflowGroupNode(SubflowNodeGroup):
    """Minimal concrete SubflowNodeGroup for fixture use."""

    async def aprocess(self) -> None:
        await self.execute_subflow()

    def process(self) -> Any:
        pass
