"""A node module that violates the boundary, for the detector to catch.

It imports the execution module at module scope -- through a helper, which is the case a
source scan of node modules misses -- so importing this on the orchestrator would drag the
library's execution dependency onto the editing process's import path.
"""

from __future__ import annotations

from typing import Any

import leak_helper  # noqa: F401

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode


class LeakyNode(DataNode):
    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="unused",
                type="str",
                default_value="",
                tooltip="",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        return
