"""Fixture node for the run-spend e2e tests.

``SpendingNode`` reports a fixed cost from inside its run, the way the standard library's
``report_model_usage_sync`` does after a direct provider call. It passes only what the library
passes -- the model, the cost and its own class name -- so every other tag on the report, the run
id among them, is the engine's to add.

Given a ``gate_file``, it then waits for that file to appear before finishing, so a test can cancel
the run while the node is still running.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import ControlNode
from griptape_nodes.retained_mode.events.budget_events import ReportUsageRequest
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

COST_MICRO_USD = 400_000
_MAX_GATE_WAIT_SECONDS = 60
_GATE_POLL_SECONDS = 0.01


class SpendingNode(ControlNode):
    """Reports one direct model call's cost each time it runs."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)
        self.add_parameter(
            Parameter(
                name="gate_file",
                tooltip="Path this node waits for after reporting, before finishing. Empty means finish at once.",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )

    async def aprocess(self) -> None:
        await GriptapeNodes.ahandle_request(
            ReportUsageRequest(
                declared_cost_micro_usd=COST_MICRO_USD,
                provider="openai",
                model="gpt-5-nano",
                activity_type="chat_completion",
                node_type=type(self).__name__,
            )
        )
        await self._wait_for_gate()

    async def _wait_for_gate(self) -> None:
        gate_path = self.get_parameter_value("gate_file") or ""
        if not gate_path:
            return

        gate_file = Path(gate_path)
        deadline = time.monotonic() + _MAX_GATE_WAIT_SECONDS
        while not gate_file.exists():  # noqa: ASYNC240
            if time.monotonic() > deadline:
                msg = f"Node '{self.name}' waited {_MAX_GATE_WAIT_SECONDS}s for its gate file, and it never appeared."
                raise TimeoutError(msg)
            await asyncio.sleep(_GATE_POLL_SECONDS)
