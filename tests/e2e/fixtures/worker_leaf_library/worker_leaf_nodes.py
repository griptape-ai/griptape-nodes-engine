"""Fixture node for the worker-boundary e2e test: a node that always fails, in a library of its own."""

from __future__ import annotations

from griptape_nodes.exe_types.core_types import NodeError, Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import ControlNode


class WorkerFailingNode(ControlNode):
    """Always fails with a ``NodeError``, so a workflow can fail on purpose."""

    FAILURE_MESSAGE = "The fixture node refused to run"

    def process(self) -> None:
        raise NodeError(self.FAILURE_MESSAGE)


class WorkerValueErrorNode(ControlNode):
    """Always fails with a builtin ``ValueError``, so the worker's forwarded type is not a ``NodeError``."""

    FAILURE_MESSAGE = "The worker refused the input value"

    def process(self) -> None:
        raise ValueError(self.FAILURE_MESSAGE)


class WorkerDeclinesNode(ControlNode):
    """Declines to run from ``validate_in_execution_environment``, which runs in the worker."""

    DECLINE_MESSAGE = "The worker's model is not installed"

    def validate_in_execution_environment(self) -> list[Exception] | None:
        return [ValueError(self.DECLINE_MESSAGE)]

    def process(self) -> None:
        msg = "process must not run after the node declined"
        raise AssertionError(msg)


class WorkerUnsendableOutputNode(ControlNode):
    """Produces an output with no plain-data form, which the worker cannot send back."""

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata)
        self.add_parameter(
            Parameter(
                name="blob",
                tooltip="An output that cannot cross the process boundary",
                type="any",
                output_type="any",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        self.parameter_output_values["blob"] = object()
