"""Undo/redo subsystem.

Public surface: `UndoEntryReplayError`, the failure a reversal raises when it cannot complete and the
undo history can no longer be trusted.
"""

from griptape_nodes.retained_mode.managers.undo.core import UndoEntryReplayError

__all__ = ["UndoEntryReplayError"]
