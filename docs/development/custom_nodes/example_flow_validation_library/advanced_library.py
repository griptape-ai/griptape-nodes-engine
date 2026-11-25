"""Advanced library that blocks a flow from starting, controlled by a flag file on disk.

validate_flow_before_run runs once per library, before a top-level flow starts, and can
block the run by returning or raising an exception -- unlike every other hook on
AdvancedNodeLibrary, which is notification only. This example reads a flag file each time
it's called, so you can flip pass/fail from a terminal while the engine keeps running:

    touch /tmp/gtn_flow_validation_should_fail   # next flow start is blocked
    rm /tmp/gtn_flow_validation_should_fail       # next flow start succeeds

Override the path with GTN_EXAMPLE_FLOW_VALIDATION_FLAG if /tmp isn't convenient.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from griptape_nodes.node_library.advanced_node_library import AdvancedNodeLibrary

if TYPE_CHECKING:
    from griptape_nodes.exe_types.flow import ControlFlow
    from griptape_nodes.exe_types.node_types import BaseNode

logger = logging.getLogger("griptape_nodes")

FLAG_PATH = Path(os.environ.get("GTN_EXAMPLE_FLOW_VALIDATION_FLAG", "/tmp/gtn_flow_validation_should_fail"))  # noqa: S108


class FlowValidationLibrary(AdvancedNodeLibrary):
    def validate_flow_before_run(self, flow: ControlFlow, nodes: list[BaseNode]) -> list[Exception] | None:
        logger.info("Flow Validation Demo: checking flow '%s' (%d nodes) against %s", flow.name, len(nodes), FLAG_PATH)
        if FLAG_PATH.exists():
            return [RuntimeError(f"Flow '{flow.name}' was blocked because {FLAG_PATH} exists.")]
        return None
