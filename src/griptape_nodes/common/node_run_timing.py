"""Timing for the node_run_timing beta feature.

`NodeExecutor` records every node it runs while a run is being timed. When the run ends, the
timer logs a summary: how long the whole run took, which nodes ran in parallel, and how long each
one took. Nodes that ran inside a group or loop are listed under it, combined by name, with the
number of times they ran.
"""

from __future__ import annotations

import io
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum

from rich.console import Console
from rich.text import Text
from rich.tree import Tree

logger = logging.getLogger("griptape_nodes")

# Wide enough that deep trees with long node names stay on one line each.
_RENDER_WIDTH = 500


class NodeRunStatus(StrEnum):
    """How one node run ended."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunOutcome(StrEnum):
    """How a whole run ended."""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class NodeRunRecord:
    """One node run, timed with `time.perf_counter`.

    Attributes:
        node_name: Name of the node that ran.
        node_type: Class name of the node.
        parent_name: Name of the group or loop node this ran inside, or None at the top level.
        started_at: `time.perf_counter()` when the node started.
        finished_at: `time.perf_counter()` when the node finished.
        status: Whether the node succeeded, failed, or was cancelled.
    """

    node_name: str
    node_type: str
    parent_name: str | None
    started_at: float
    finished_at: float
    status: NodeRunStatus

    @property
    def seconds(self) -> float:
        return self.finished_at - self.started_at


@dataclass
class _Stage:
    """Top-level node runs whose times overlapped, so they ran in parallel."""

    records: list[NodeRunRecord]
    started_at: float
    finished_at: float


@dataclass
class _CombinedRuns:
    """Every run of one node inside a group, combined into a single summary line."""

    node_name: str
    node_type: str
    count: int
    seconds: float
    failures: int


class NodeRunTimer:
    """Collects node runs between the start and end of one workflow run."""

    def __init__(self) -> None:
        self._run_started_at: float | None = None
        self._records: list[NodeRunRecord] = []

    @property
    def is_timing(self) -> bool:
        return self._run_started_at is not None

    def start_run(self) -> None:
        """Start timing a run, dropping anything left over from an earlier one."""
        self._run_started_at = time.perf_counter()
        self._records = []

    def record(self, record: NodeRunRecord) -> None:
        """Keep a node run for the summary. Ignored when no run is being timed."""
        if not self.is_timing:
            return
        self._records.append(record)

    def finish_run(self, outcome: RunOutcome) -> None:
        """Log the run's summary and stop timing. Does nothing when no run is being timed.

        A run can end through more than one path, such as a failure followed by a cancel, so only
        the first call logs.
        """
        if self._run_started_at is None:
            return

        summary = build_run_summary(self._records, outcome, self._run_started_at, time.perf_counter())
        self._run_started_at = None
        self._records = []
        logger.info(summary)


def build_run_summary(
    records: list[NodeRunRecord], outcome: RunOutcome, run_started_at: float, run_finished_at: float
) -> str:
    """The summary logged when a timed run ends, drawn as a tree of stages and the nodes in them."""
    unique_node_count = len({record.node_name for record in records})
    tree = Tree(
        Text(f"RUN SUMMARY: {outcome} in {run_finished_at - run_started_at:.3f} s, {unique_node_count} nodes ran")
    )

    children_by_parent: dict[str, list[NodeRunRecord]] = defaultdict(list)
    top_level: list[NodeRunRecord] = []
    # A parent that was not recorded, such as a group still running when a cancelled run ends,
    # leaves its children at the top level rather than hidden.
    recorded_names = {record.node_name for record in records}
    for record in records:
        if record.parent_name is None or record.parent_name not in recorded_names:
            top_level.append(record)
        else:
            children_by_parent[record.parent_name].append(record)

    for index, stage in enumerate(_group_into_stages(top_level), start=1):
        header = (
            f"Stage {index} at {stage.started_at - run_started_at:.3f} s, {stage.finished_at - stage.started_at:.3f} s"
        )
        if len(stage.records) > 1:
            header = f"{header}, {len(stage.records)} nodes in parallel"
        stage_branch = tree.add(Text(header))
        for record in sorted(stage.records, key=lambda r: r.seconds, reverse=True):
            label = f"{record.seconds:.3f} s  '{record.node_name}' ({record.node_type}){_status_suffix(record)}"
            node_branch = stage_branch.add(Text(label))
            _add_children(node_branch, record.node_name, children_by_parent, ancestors=frozenset())
    return _render(tree)


def _group_into_stages(records: list[NodeRunRecord]) -> list[_Stage]:
    """Group runs whose start-to-finish times overlap. A new stage starts after a gap."""
    stages: list[_Stage] = []
    for record in sorted(records, key=lambda r: r.started_at):
        if stages and record.started_at < stages[-1].finished_at:
            stage = stages[-1]
            stage.records.append(record)
            stage.finished_at = max(stage.finished_at, record.finished_at)
            continue
        stages.append(_Stage(records=[record], started_at=record.started_at, finished_at=record.finished_at))
    return stages


def _add_children(
    branch: Tree, parent_name: str, children_by_parent: dict[str, list[NodeRunRecord]], ancestors: frozenset[str]
) -> None:
    """Add the nodes that ran inside `parent_name` under its branch, and the nodes inside those.

    Records are linked by name, so `ancestors` stops a child that shares a name with a node
    above it from being expanded forever.
    """
    if parent_name in ancestors:
        return
    ancestors = ancestors | {parent_name}

    combined: dict[str, _CombinedRuns] = {}
    for record in children_by_parent.get(parent_name, []):
        runs = combined.get(record.node_name)
        if runs is None:
            runs = _CombinedRuns(node_name=record.node_name, node_type=record.node_type, count=0, seconds=0, failures=0)
            combined[record.node_name] = runs
        runs.count += 1
        runs.seconds += record.seconds
        if record.status is not NodeRunStatus.SUCCEEDED:
            runs.failures += 1

    for runs in sorted(combined.values(), key=lambda r: r.seconds, reverse=True):
        label = f"{runs.seconds:.3f} s  '{runs.node_name}' ({runs.node_type})"
        if runs.count > 1:
            label = f"{label} x{runs.count}"
        if runs.failures:
            label = f"{label}  {runs.failures} did not finish"
        child_branch = branch.add(Text(label))
        _add_children(child_branch, runs.node_name, children_by_parent, ancestors)


def _render(tree: Tree) -> str:
    """Draw the tree as plain text for the log, without colour, and wide enough not to wrap."""
    buffer = io.StringIO()
    console = Console(file=buffer, width=_RENDER_WIDTH, color_system=None, force_terminal=False)
    console.print(tree)
    return "\n".join(line.rstrip() for line in buffer.getvalue().splitlines())


def _status_suffix(record: NodeRunRecord) -> str:
    if record.status is NodeRunStatus.SUCCEEDED:
        return ""
    return f"  {record.status.upper()}"
