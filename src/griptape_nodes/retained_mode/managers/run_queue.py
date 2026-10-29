"""Runs, and the queue of runs waiting to start.

A run is what one request starts: running a workflow, from a node, or a single node. The queue only
holds the waiting runs, in the order they were submitted; `FlowManager` decides when each starts,
drives it, and ends it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import asyncio
    import contextvars
    from datetime import datetime

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload


class RunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    # The request that started the run has returned, but the run goes on, as a debug run does at a pause.
    PAUSED = "paused"
    FINISHED = "finished"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunSpec(Protocol):
    """What a run executes: the request that started it, with the flow it runs."""

    @property
    def flow_name(self) -> str:
        """The flow the run executes."""
        raise NotImplementedError

    async def execute(self, engine: Engine) -> ResultPayload:
        """Execute the run, returning what its request returns."""
        raise NotImplementedError

    def failure(self, details: str) -> ResultPayload:
        """The failure its request returns for a run that did not execute."""
        raise NotImplementedError


@dataclass
class Run:
    run_id: str
    spec: RunSpec
    flow_name: str
    queued_at: datetime
    state: RunState
    # What the run's request returns, set once the request is done.
    outcome: asyncio.Future[ResultPayload]
    # The submitter's context, so the run executes as if the request had run it directly.
    context: contextvars.Context
    # The place in line it was last reported at, 0 while it is not waiting.
    position: int = 0
    # The id the client gave the request that submitted it, so the client can tell its own run apart.
    request_id: str | None = None


class RunQueue:
    """The runs waiting to start, first submitted first."""

    def __init__(self) -> None:
        self._runs: list[Run] = []

    def enqueue(self, run: Run) -> None:
        self._runs.append(run)

    def pop_next(self) -> Run | None:
        """Remove and return the run that has waited longest, or None if none waits."""
        if not self._runs:
            return None
        return self._runs.pop(0)

    def remove(self, run_id: str) -> Run | None:
        """Remove a waiting run, returning it, or None if it is not waiting."""
        for run in self._runs:
            if run.run_id == run_id:
                self._runs.remove(run)
                return run
        return None

    def runs_for_flow(self, flow_name: str) -> list[Run]:
        """The waiting runs of `flow_name`, next first."""
        return [run for run in self._runs if run.flow_name == flow_name]

    def snapshot(self) -> list[Run]:
        """The waiting runs, next first."""
        return list(self._runs)
