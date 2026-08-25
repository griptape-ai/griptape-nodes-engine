"""The vocabulary domains use to describe reversible operations.

This module is domain-agnostic: it defines the failure mode a reversal reports when it can no longer
be trusted. The concrete reversal mechanism (currently whole-flow snapshots, in `flow_snapshot`)
builds on these types.
"""

from __future__ import annotations


class UndoEntryReplayError(RuntimeError):
    """Raised when replaying an undo/redo entry fails and the undo history can no longer be trusted."""
