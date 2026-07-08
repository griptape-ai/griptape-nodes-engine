"""Undo/redo subsystem.

Public surface:
    - `UndoManager`: the mechanism (undo/redo stacks, replay, request handlers, recording API).
    - The vocabulary domains use to describe reversals: `UndoEntry`, `UndoBatch`,
      `RequestReplayUndoEntry`, `UndoRecorder`, `RecorderCapture`, `UndoEntryReplayError`, and the
      `dispatch_expecting` / `dispatch_expecting_success` replay helpers.

Per-domain reversal knowledge lives in the `undo.recorders` subpackage (e.g. `recorders.node`) and
is registered with the manager by the owning domain manager.
"""

from griptape_nodes.retained_mode.managers.undo.core import (
    RecorderCapture,
    RequestReplayUndoEntry,
    UndoBatch,
    UndoEntry,
    UndoEntryReplayError,
    UndoRecorder,
    dispatch_expecting,
    dispatch_expecting_success,
)
from griptape_nodes.retained_mode.managers.undo.manager import UndoManager

__all__ = [
    "RecorderCapture",
    "RequestReplayUndoEntry",
    "UndoBatch",
    "UndoEntry",
    "UndoEntryReplayError",
    "UndoManager",
    "UndoRecorder",
    "dispatch_expecting",
    "dispatch_expecting_success",
]
