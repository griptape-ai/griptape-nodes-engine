"""One open workflow's undo history: the workflow as it was at each step, and which one it is at now."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import assert_never

MAX_STEPS = 100


@dataclass(frozen=True)
class Checkpoint:
    """The workflow as a save would write it, at the end of a step.

    Args:
        commands: The workflow's serialized commands, compressed (see `UndoManager`).
        label: What the step that led here was called. None for the first checkpoint, which no step led to.
    """

    commands: bytes
    label: str | None


def _idle_event() -> asyncio.Event:
    event = asyncio.Event()
    event.set()
    return event


@dataclass(eq=False)
class OpenStep:
    """The step whose requests share an undo group, from its first request until the next step starts.

    A change counts toward the step only while one of its requests is being handled. What a task one
    of them started changes later is a change outside any step.

    Args:
        group: The undo group its requests share.
        label: What it is called: the last label one of its requests sent, or else its first request.
    """

    group: str
    label: str
    _running: int = field(default=0, init=False)
    _idle: asyncio.Event = field(default_factory=_idle_event, init=False)

    @property
    def running(self) -> bool:
        """Whether any of its requests is being handled."""
        return self._running > 0

    def request_started(self) -> None:
        self._running += 1
        self._idle.clear()

    def request_finished(self) -> None:
        self._running -= 1
        if self._running == 0:
            self._idle.set()

    async def wait_until_idle(self) -> None:
        """Wait until none of its requests is being handled."""
        await self._idle.wait()


class StepStillRunningError(Exception):
    """A request would start a step while a request of the step before is still being handled."""


@dataclass(frozen=True)
class Clean:
    """Nothing changed the workflow since the current checkpoint was taken."""


@dataclass(frozen=True)
class RunOutput:
    """Something other than a client's request changed the workflow: a run, or a task a request started, once the request finished.

    The next checkpoint replaces the current one, so an undo takes the change back with the step
    before it. A checkpoint holds no run results, so a redo loses nothing, and is kept.
    """


@dataclass(frozen=True)
class OutsideEdit:
    """A client changed the workflow outside any step.

    The next checkpoint replaces the current one, like `RunOutput`. A redo would bring the workflow
    back without the change, so it drops what could be redone, unless the workflow is as it was.
    """


@dataclass(frozen=True)
class StepPending:
    """A step changed the workflow. The next checkpoint adds one for it, and drops what could be redone."""

    step: OpenStep


# What changed since the current checkpoint was taken.
Drift = Clean | RunOutput | OutsideEdit | StepPending

# Changes of different kinds before one checkpoint count as the one whose checkpoint does the most.
# A pending step drops what could be redone anyway, so it takes in an outside edit.
_PRECEDENCE: dict[type[Drift], int] = {Clean: 0, RunOutput: 1, OutsideEdit: 2, StepPending: 3}


class UndoHistory:
    """The checkpoints one workflow went through, the one it is at, and what changed since."""

    def __init__(self) -> None:
        self._checkpoints: list[Checkpoint] = []
        self._current = -1
        self.drift: Drift = Clean()
        # The step a request in its undo group continues. A request in another group starts the next one.
        self.open_step: OpenStep | None = None
        # Set while an undo or redo rebuilds the workflow, whose changes are not steps.
        self.restoring = False

    @property
    def undo_label(self) -> str | None:
        """What an undo would take back, or None when there is nothing to undo."""
        if isinstance(self.drift, StepPending):
            return self.drift.step.label
        if self._current <= 0:
            return None
        return self._checkpoints[self._current].label

    @property
    def redo_label(self) -> str | None:
        """What a redo would put back, or None when there is nothing to redo."""
        if isinstance(self.drift, StepPending) or self._current + 1 >= len(self._checkpoints):
            return None
        return self._checkpoints[self._current + 1].label

    @property
    def step_running(self) -> bool:
        """Whether a request of the open step is being handled."""
        return self.open_step is not None and self.open_step.running

    @property
    def needs_checkpoint(self) -> bool:
        """Whether the workflow may differ from the current checkpoint, or there is none yet."""
        return self._current < 0 or not isinstance(self.drift, Clean)

    def note(self, change: Drift) -> None:
        """Note a change to the workflow since the current checkpoint."""
        if _PRECEDENCE[type(change)] >= _PRECEDENCE[type(self.drift)]:
            self.drift = change

    def record(self, commands: bytes) -> None:
        """Take a checkpoint of the workflow as it is now, ``commands``, according to what changed since the last."""
        if self._current < 0:
            self._checkpoints = [Checkpoint(commands=commands, label=None)]
            self._current = 0
            self.drift = Clean()
            return
        match self.drift:
            case StepPending(step=step):
                self._add_unless_unchanged(commands, step.label)
            case OutsideEdit():
                if commands != self._checkpoints[self._current].commands:
                    del self._checkpoints[self._current + 1 :]
                self.replace_current(commands)
            case Clean() | RunOutput():
                self.replace_current(commands)
            case _:
                assert_never(self.drift)
        self.drift = Clean()

    def previous(self) -> Checkpoint | None:
        """The checkpoint an undo goes back to. Take a checkpoint first, so the current one is up to date."""
        if self.needs_checkpoint or self._current <= 0:
            return None
        return self._checkpoints[self._current - 1]

    def next(self) -> Checkpoint | None:
        """The checkpoint a redo goes forward to. Take a checkpoint first, so the current one is up to date."""
        if self.needs_checkpoint or self._current + 1 >= len(self._checkpoints):
            return None
        return self._checkpoints[self._current + 1]

    def current(self) -> Checkpoint | None:
        if self._current < 0:
            return None
        return self._checkpoints[self._current]

    def replace_current(self, commands: bytes) -> None:
        """Replace the current checkpoint with ``commands``, keeping the label of the step that led to it."""
        label = self._checkpoints[self._current].label
        self._checkpoints[self._current] = Checkpoint(commands=commands, label=label)

    def step_back(self) -> None:
        """Make the previous checkpoint current, once an undo rebuilt the workflow from it."""
        self._current -= 1
        self.open_step = None

    def step_forward(self) -> None:
        """Make the next checkpoint current, once a redo rebuilt the workflow from it."""
        self._current += 1
        self.open_step = None

    def clear(self) -> None:
        """Forget every checkpoint, such as when the workflow could not be checkpointed or rebuilt."""
        self._checkpoints = []
        self._current = -1
        self.drift = Clean()
        self.open_step = None

    def _add_unless_unchanged(self, commands: bytes, label: str) -> None:
        """Add a checkpoint for a step, dropping what could be redone, unless the step left the workflow as it was."""
        if commands == self._checkpoints[self._current].commands:
            return
        del self._checkpoints[self._current + 1 :]
        self._checkpoints.append(Checkpoint(commands=commands, label=label))
        if len(self._checkpoints) > MAX_STEPS + 1:
            del self._checkpoints[0]
        self._current = len(self._checkpoints) - 1
