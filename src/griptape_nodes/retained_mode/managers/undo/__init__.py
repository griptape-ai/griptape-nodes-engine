"""Undo/redo subsystem.

Public surface:
    - `UndoManager`: the mechanism (undo/redo stacks, replay, request handlers, recording API).
    - The vocabulary domains use to describe reversals: `UndoEntry`, `UndoBatch`,
      `UndoEntryReplayError`, and the `RecordingStrategy` contract a recording strategy implements.

The concrete recording strategy (currently the whole-flow `SnapshotRecordingSession`) lives in its
own module and is selected by `UndoManager`. `RecordingStrategy` is the extension seam for layering
in finer-grained strategies later.
"""

from griptape_nodes.retained_mode.managers.undo.core import (
    RecordingStrategy,
    UndoBatch,
    UndoEntry,
    UndoEntryReplayError,
)
from griptape_nodes.retained_mode.managers.undo.manager import UndoManager

__all__ = [
    "RecordingStrategy",
    "UndoBatch",
    "UndoEntry",
    "UndoEntryReplayError",
    "UndoManager",
]
