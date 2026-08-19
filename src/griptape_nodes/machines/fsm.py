import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from typing import Any, TypeVar

T = TypeVar("T")


class WorkflowState(StrEnum):
    """Workflow execution states."""

    NO_ERROR = "no_error"
    WORKFLOW_COMPLETE = "workflow_complete"
    ERRORED = "errored"
    CANCELED = "canceled"


class State:
    @staticmethod
    async def on_enter(context: Any) -> type["State"] | None:  # noqa: ARG004
        """Called when entering the state."""
        return None

    @staticmethod
    async def on_update(context: Any) -> type["State"] | None:  # noqa: ARG004
        """Called each update until a transition occurs."""
        return None

    @staticmethod
    async def on_exit(context: Any) -> None:  # noqa: ARG004
        """Called when exiting the state."""
        return

    @staticmethod
    async def on_event(context: Any, event: Any) -> type["State"] | None:  # noqa: ARG004
        """Called on an event, which may trigger a State transition."""
        return None


class FSM[T]:
    def __init__(self, context: T) -> None:
        self._context = context
        self._current_state = None
        # The task currently advancing this machine, or None. State machines are
        # single-driver: the states suspend, so two coroutines advancing one
        # machine interleave inside those awaits and corrupt the shared
        # context. Concretely, two drivers of a resolution machine both wait on
        # the same set of node tasks, both wake for the same completed task, and
        # the second one to look it up finds it already consumed. A second
        # driver raises instead. Holding the task rather than a bool lets
        # ``_advance`` tell "I hold the claim" from "someone else does".
        self._advancing_task: asyncio.Task | None = None

    async def start(self, initial_state: type[State]) -> None:
        # Enter the initial state.
        await self.transition_state(initial_state)

    @property
    def current_state(self) -> type[State] | None:
        return self._current_state

    @property
    def context(self) -> T:
        return self._context

    @property
    def is_advancing(self) -> bool:
        """True while a coroutine is driving this machine.

        Callers that would otherwise become a second driver check this and skip
        their drive. The raise in ``_claim_for_advancing`` is the backstop for
        callers that do not.
        """
        return self._advancing_task is not None

    async def transition_state(self, new_state: type[State] | None) -> None:
        with self._claim_for_advancing():
            await self._advance(new_state)

    async def update(self) -> None:
        with self._claim_for_advancing():
            if self._current_state is None:
                new_state = None
            else:
                new_state = await self._current_state.on_update(self._context)
            await self._advance(new_state)

    async def handle_event(self, event: Any) -> None:
        with self._claim_for_advancing():
            if self._current_state is None:
                new_state = None
            else:
                new_state = await self._current_state.on_event(self._context, event)
            await self._advance(new_state)

    @contextmanager
    def _claim_for_advancing(self) -> Iterator[None]:
        """Claim sole right to advance this machine for the duration of the block.

        Claiming and releasing are paired here so that no entry point can take the
        claim and forget to release it: a stuck claim would leave the machine
        permanently un-advanceable.
        """
        if self._advancing_task is not None:
            msg = (
                "Attempted to run a workflow step while the workflow was already running. "
                "Failed because a workflow can only advance one step at a time. "
                "Wait for the current run to finish before starting another."
            )
            raise RuntimeError(msg)
        self._advancing_task = asyncio.current_task()
        try:
            yield
        finally:
            self._advancing_task = None

    async def _advance(self, new_state: type[State] | None) -> None:
        """Drive states until one settles.

        Every public entry point claims the machine before calling this, so that
        one drive holds it end to end. Reaching here without holding the claim
        means a new entry point skipped ``_claim_for_advancing``, and the
        single-driver guarantee is not in force. Comparing the task rather than
        just testing for any claim catches the dangerous case: an unclaimed
        caller arriving while another task is legitimately driving.
        """
        if self._advancing_task is not asyncio.current_task():
            msg = "Attempted to advance a state machine without claiming it. This is a programming error in the caller."
            raise RuntimeError(msg)
        while new_state is not None:
            # Exit the current state.
            if self._current_state is not None and new_state is self._current_state:
                new_state = await self._current_state.on_update(self._context)
                continue
            if self._current_state is not None:
                await self._current_state.on_exit(self._context)
            # Update current
            self._current_state = new_state
            # Enter the now-current state
            new_state = await self._current_state.on_enter(self._context)
