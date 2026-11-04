"""A node that waits, so the timing library has something to time."""

from __future__ import annotations

import asyncio

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import ControlNode


class WaitNode(ControlNode):
    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="seconds",
                tooltip="How many seconds to wait",
                type="float",
                default_value=1.0,
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )

    async def aprocess(self) -> None:
        # Waits without holding up other nodes that run at the same time.
        await asyncio.sleep(self.get_parameter_value("seconds"))
