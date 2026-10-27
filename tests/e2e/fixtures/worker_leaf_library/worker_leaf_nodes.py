"""Fixture node for the worker-boundary e2e test: a node that always fails, in a library of its own."""

from __future__ import annotations

from griptape_nodes.exe_types.core_types import NodeError
from griptape_nodes.exe_types.node_types import ControlNode


class WorkerFailingNode(ControlNode):
    """Always fails with a ``NodeError``, so a workflow can fail on purpose."""

    FAILURE_MESSAGE = "The fixture node refused to run"

    def process(self) -> None:
        raise NodeError(self.FAILURE_MESSAGE)
