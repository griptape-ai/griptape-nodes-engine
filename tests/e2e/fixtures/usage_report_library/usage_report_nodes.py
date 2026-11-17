"""Fixture node for the usage-report e2e tests.

``ReportingNode`` reports a fixed cost from inside its run, the way the standard library's
``report_model_usage_sync`` does after a direct provider call.
"""

from __future__ import annotations

from griptape_nodes.exe_types.node_types import ControlNode
from griptape_nodes.retained_mode.events.budget_events import ReportUsageRequest
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

COST_MICRO_USD = 400_000


class ReportingNode(ControlNode):
    """Reports one direct model call's cost each time it runs."""

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
