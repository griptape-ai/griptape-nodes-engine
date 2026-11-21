"""One open workflow's undo history: the workflow as it was at each step, and which one it is at now."""

from __future__ import annotations

from dataclasses import dataclass

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


class UndoHistory:
    """The checkpoints one workflow went through, and the one it is at.

    The workflow matches the current checkpoint until something changes it. A request in an undo
    group makes a step: the change is pending until the next checkpoint is taken, which adds one.
    Any other change only makes the current checkpoint stale, and the next checkpoint replaces it,
    so an undo takes that change back with the step before it.
    """

    def __init__(self) -> None:
        self._checkpoints: list[Checkpoint] = []
        self._current = -1
        # What the step being recorded is called, once one of its requests changed the workflow.
        self.pending_label: str | None = None
        # Whether anything changed the workflow since the current checkpoint was taken.
        self.stale = False
        # Whether a client, rather than a run, changed the workflow outside any step since the current
        # checkpoint was taken.
        self.edited = False
        # The undo group whose step is being recorded. Its next request continues that step.
        self.open_group: str | None = None
        # Requests of the step being recorded that have not finished.
        self.in_flight = 0
        # Set while an undo or redo rebuilds the workflow, whose changes are not steps.
        self.restoring = False

    @property
    def undo_label(self) -> str | None:
        """What an undo would take back, or None when there is nothing to undo."""
        if self.pending_label is not None:
            return self.pending_label
        if self._current <= 0:
            return None
        return self._checkpoints[self._current].label

    @property
    def redo_label(self) -> str | None:
        """What a redo would put back, or None when there is nothing to redo."""
        if self.pending_label is not None or self._current + 1 >= len(self._checkpoints):
            return None
        return self._checkpoints[self._current + 1].label

    @property
    def has_checkpoint(self) -> bool:
        return self._current >= 0

    @property
    def needs_checkpoint(self) -> bool:
        """Whether the workflow may differ from the current checkpoint, or there is none yet."""
        return self._current < 0 or self.pending_label is not None or self.stale

    def record(self, commands: bytes) -> None:
        """Take a checkpoint of the workflow as it is now.

        Adds one for a pending step, dropping what could be redone. A step that left the workflow
        as it was, such as setting a value to the one it had, adds none. Replaces the current one
        when only changes outside any step were made, and starts the history when it is empty.
        """
        if self._current < 0:
            self._checkpoints = [Checkpoint(commands=commands, label=None)]
            self._current = 0
        elif self.pending_label is not None and commands == self._checkpoints[self._current].commands:
            pass
        elif self.pending_label is not None:
            del self._checkpoints[self._current + 1 :]
            self._checkpoints.append(Checkpoint(commands=commands, label=self.pending_label))
            if len(self._checkpoints) > MAX_STEPS + 1:
                del self._checkpoints[0]
            self._current = len(self._checkpoints) - 1
        else:
            label = self._checkpoints[self._current].label
            self._checkpoints[self._current] = Checkpoint(commands=commands, label=label)
        self.pending_label = None
        self.stale = False
        self.edited = False

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

    def drop_redo(self) -> None:
        """Forget the checkpoints a redo would go forward to, such as when a client changed the workflow since."""
        del self._checkpoints[self._current + 1 :]

    def step_back(self) -> None:
        """Make the previous checkpoint current, once an undo rebuilt the workflow from it."""
        self._current -= 1
        self.open_group = None

    def step_forward(self) -> None:
        """Make the next checkpoint current, once a redo rebuilt the workflow from it."""
        self._current += 1
        self.open_group = None

    def clear(self) -> None:
        """Forget every checkpoint, such as when the workflow could not be checkpointed or rebuilt."""
        self._checkpoints = []
        self._current = -1
        self.pending_label = None
        self.stale = False
        self.edited = False
        self.open_group = None
