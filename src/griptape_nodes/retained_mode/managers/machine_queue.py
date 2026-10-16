"""The queue a machine running nodes for other machines keeps, bundling nodes by the run they belong to.

One run has the machine at a time. Its nodes run as they arrive, several at once if the run sends
several; every other run's nodes wait until it ends. A run ends when its orchestrator says so, when
none of its nodes has been running for the idle timeout, or when its orchestrator goes away, whichever
comes first. A node sent without a run is a run of its own, which ends when the node does.

The queue holds no transport. It is told what arrives and what ends, and is handed what to do when the
machine passes to another orchestrator's run and when a waiting node's place in line changes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger("griptape_nodes")

_ONE_OFF_RUN_PREFIX = "one-off:"


class NodeNotAdmittedError(Exception):
    """Raised at a waiting node that will never run here, carrying why."""


@dataclass
class _WaitingNode:
    admission_id: str
    originator: str
    run_id: str
    node_name: str
    admitted: asyncio.Future[None]
    position: int = 0


@dataclass
class _RunHoldingMachine:
    originator: str
    run_id: str
    running: set[str] = field(default_factory=set)
    idle_timer: asyncio.TimerHandle | None = None

    @property
    def is_one_off(self) -> bool:
        return self.run_id.startswith(_ONE_OFF_RUN_PREFIX)


class MachineQueue:
    """Admits nodes from other machines' orchestrators one run at a time."""

    def __init__(
        self,
        *,
        idle_timeout_s: float,
        on_handoff: Callable[[str], Awaitable[None]],
        on_position: Callable[[str, str, int], Awaitable[None]],
    ) -> None:
        """Create an empty queue.

        Args:
            idle_timeout_s: How long a run keeps the machine with none of its nodes running.
            on_handoff: Awaited with the incoming run's orchestrator before the machine passes to a run
                from a different orchestrator than the last one, to free what the previous one left.
            on_position: Awaited with a waiting node's orchestrator, name, and its run's place in line,
                1 being next, whenever that place changes, and with 0 once the machine takes it.
        """
        self._idle_timeout_s = idle_timeout_s
        self._on_handoff = on_handoff
        self._on_position = on_position
        self._holder: _RunHoldingMachine | None = None
        self._waiting: list[_WaitingNode] = []
        # Whose run last had the machine, so a handoff to the same orchestrator frees nothing.
        self._last_originator: str | None = None
        self._advancing = False
        self._tasks: set[asyncio.Task] = set()

    @property
    def holder(self) -> tuple[str, str] | None:
        """The orchestrator and run that have the machine, or None while it is free."""
        if self._holder is None:
            return None
        return self._holder.originator, self._holder.run_id

    async def admit(self, *, admission_id: str, originator: str, run_id: str | None, node_name: str) -> str:
        """Return once the node may run, with the run it runs in.

        Raises:
            NodeNotAdmittedError: The node will not run here; its run ended or its orchestrator went away
                while it waited.
        """
        if run_id is None:
            run_id = f"{_ONE_OFF_RUN_PREFIX}{admission_id}"
        holder = self._holder
        if holder is not None and holder.originator == originator and holder.run_id == run_id:
            self._start_running(holder, admission_id)
            return run_id
        waiting = _WaitingNode(
            admission_id=admission_id,
            originator=originator,
            run_id=run_id,
            node_name=node_name,
            admitted=asyncio.get_running_loop().create_future(),
        )
        self._waiting.append(waiting)
        await self._advance()
        await waiting.admitted
        return run_id

    def node_ended(self, admission_id: str) -> None:
        """Record that a node finished, or was cancelled before or while it ran."""
        for waiting in self._waiting:
            if waiting.admission_id == admission_id:
                self._waiting.remove(waiting)
                if not waiting.admitted.done():
                    waiting.admitted.set_exception(NodeNotAdmittedError("it was cancelled while it waited."))
                self._schedule(self._report_positions())
                return
        holder = self._holder
        if holder is None or admission_id not in holder.running:
            return
        holder.running.discard(admission_id)
        if holder.running:
            return
        if holder.is_one_off:
            self._release_machine()
            return
        holder.idle_timer = asyncio.get_running_loop().call_later(self._idle_timeout_s, self._end_if_idle, holder)

    def end_run(self, originator: str, run_id: str) -> None:
        """Record that a run ended, freeing the machine if the run has it."""
        self._turn_away(
            lambda waiting: waiting.originator == originator and waiting.run_id == run_id,
            "its run ended before the machine was free.",
        )
        holder = self._holder
        if holder is not None and holder.originator == originator and holder.run_id == run_id:
            self._release_machine()

    def originator_gone(self, originator: str) -> None:
        """Forget an orchestrator that went away: its waiting nodes, and its run if it has the machine."""
        self._turn_away(lambda waiting: waiting.originator == originator, "its machine is no longer connected.")
        holder = self._holder
        if holder is not None and holder.originator == originator:
            self._release_machine()

    def _start_running(self, holder: _RunHoldingMachine, admission_id: str) -> None:
        holder.running.add(admission_id)
        if holder.idle_timer is not None:
            holder.idle_timer.cancel()
            holder.idle_timer = None

    def _end_if_idle(self, holder: _RunHoldingMachine) -> None:
        if self._holder is holder and not holder.running:
            logger.info(
                "Run %s from %s ended after %.0f seconds with no node running here.",
                holder.run_id,
                holder.originator,
                self._idle_timeout_s,
            )
            self._release_machine()

    def _release_machine(self) -> None:
        holder = self._holder
        if holder is not None and holder.idle_timer is not None:
            holder.idle_timer.cancel()
        self._holder = None
        self._schedule(self._advance())

    def _turn_away(self, matches: Callable[[_WaitingNode], bool], reason: str) -> None:
        turned_away = [waiting for waiting in self._waiting if matches(waiting)]
        for waiting in turned_away:
            self._waiting.remove(waiting)
            if not waiting.admitted.done():
                waiting.admitted.set_exception(NodeNotAdmittedError(reason))
        if turned_away:
            self._schedule(self._report_positions())

    async def _advance(self) -> None:
        """Give a free machine to the oldest waiting run, admitting every node of it that is waiting."""
        if self._advancing:
            return
        self._advancing = True
        admitted_after_waiting: list[_WaitingNode] = []
        try:
            while self._holder is None and self._waiting:
                head = self._waiting[0]
                if self._last_originator is not None and head.originator != self._last_originator:
                    logger.info(
                        "Freeing this machine before handing it from %s to %s.", self._last_originator, head.originator
                    )
                    # Freeing is best effort: a failure here must not leave the machine with no holder
                    # and every waiting node stuck until another node arrives.
                    try:
                        await self._on_handoff(head.originator)
                    except Exception:
                        logger.exception("Could not free this machine before the handoff; handing it over anyway.")
                    # Awaiting the handoff gave the head's orchestrator time to go away or end its run.
                    if not self._waiting or self._waiting[0] is not head:
                        continue
                holder = _RunHoldingMachine(originator=head.originator, run_id=head.run_id)
                self._holder = holder
                logger.info("Run %s from %s has this machine.", holder.run_id, holder.originator)
                self._last_originator = head.originator
                for waiting in [
                    w for w in self._waiting if (w.originator, w.run_id) == (holder.originator, holder.run_id)
                ]:
                    self._waiting.remove(waiting)
                    self._start_running(holder, waiting.admission_id)
                    if not waiting.admitted.done():
                        waiting.admitted.set_result(None)
                    if waiting.position:
                        admitted_after_waiting.append(waiting)
        finally:
            self._advancing = False
        for waiting in admitted_after_waiting:
            await self._on_position(waiting.originator, waiting.node_name, 0)
        await self._report_positions()

    async def _report_positions(self) -> None:
        """Tell each waiting node its run's place in line, when that place changed."""
        runs: list[tuple[str, str]] = []
        for waiting in self._waiting:
            run = (waiting.originator, waiting.run_id)
            if run not in runs:
                runs.append(run)
            position = runs.index(run) + 1
            if position != waiting.position:
                waiting.position = position
                await self._on_position(waiting.originator, waiting.node_name, position)

    def _schedule(self, coroutine: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
