"""Record how the run queue ends each run, shared by the unit and e2e run-queue tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.managers.flow_manager import FlowManager
    from griptape_nodes.retained_mode.managers.run_queue import Run


def record_end_states(flow_manager: FlowManager) -> list[str]:
    """Record the state each run is in as the run queue ends it, which is how it says the run ended."""
    ended: list[str] = []
    finish_run = flow_manager._finish_run

    def recording_finish_run(run: Run) -> None:
        if run.flows.run_in_progress is run:
            ended.append(run.state.value)
        finish_run(run)

    flow_manager._finish_run = recording_finish_run  # type: ignore[method-assign]
    return ended
