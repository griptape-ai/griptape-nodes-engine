"""Fixture node for the usage-report e2e tests.

``ReportingNode`` reports two direct model calls' cost from inside its run, the way the standard
library's ``report_model_usage_sync`` does after each provider call.
"""

from __future__ import annotations

from griptape_nodes.exe_types.node_types import ControlNode
from griptape_nodes.retained_mode.events.budget_events import ReportUsageRequest
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

COST_MICRO_USD = 400_000


REPORTS_PER_RUN = 2


class ReportingNode(ControlNode):
    """Reports two direct model calls' cost each time it runs.

    Reports are sent one at a time, so while Cloud holds its reply to the first, the second is still
    queued: it has not left the engine when the run ends.
    """

    async def aprocess(self) -> None:
        for _ in range(REPORTS_PER_RUN):
            await GriptapeNodes.ahandle_request(
                ReportUsageRequest(
                    declared_cost_micro_usd=COST_MICRO_USD,
                    provider="openai",
                    model="gpt-5-nano",
                    activity_type="chat_completion",
                    node_type=type(self).__name__,
                )
            )
