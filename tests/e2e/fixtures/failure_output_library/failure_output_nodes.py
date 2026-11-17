"""Fixture nodes that fail in the ways the node-failure golden tests pin.

Every node here exists to produce one failure whose exact output a test records, so each is as
small as the failure allows. The loop group and its body nodes fail per iteration, with reasons
that depend on the iteration's index so a test chooses how many distinct reasons a loop reports.
"""

from __future__ import annotations

from typing import Any

from griptape_nodes.exe_types.core_types import NodeError, NodeErrorLink, Parameter, ParameterMode
from griptape_nodes.exe_types.node_groups.base_iterative_node_group import BaseIterativeNodeGroup
from griptape_nodes.exe_types.node_types import ControlNode

PROVIDER_RESPONSE = {"status": "ERRORED", "status_detail": {"details": "proxy client error"}}


class KeyErrorNode(ControlNode):
    """Raises a ``KeyError`` whose text starts with the node's own name, as many libraries write it."""

    def process(self) -> None:
        msg = f"{self.name}: some sentence"
        raise KeyError(msg)


class ProviderErrorNode(ControlNode):
    """Raises a ``NodeError`` carrying fields, a response body and links."""

    def process(self) -> None:
        msg = "The image service could not process the request: proxy client error."
        raise NodeError(
            msg,
            fields={"generation_id": "90dbcfa0-ac4b-4feb-b234-0badff151ee2", "status": "ERRORED"},
            response=PROVIDER_RESPONSE,
            links=[NodeErrorLink(label="Error handling guide", url="https://example.com/errors")],
        )


class DeclinesInExecutionEnvironmentNode(ControlNode):
    """Declines to run from ``validate_in_execution_environment``, so ``process`` never runs."""

    def validate_in_execution_environment(self) -> list[Exception] | None:
        return [ValueError("The model is not installed."), RuntimeError("The GPU is busy.")]

    def process(self) -> None:
        msg = "process must not run after the node declined"
        raise AssertionError(msg)


class ValidateBeforeRunNode(ControlNode):
    """Fails ``validate_before_node_run`` with two problems, so it never gets dispatched."""

    def validate_before_node_run(self) -> list[Exception] | None:
        return [ValueError("Connect an image to 'Input Image'."), NodeError("'Prompt' is empty.")]

    def process(self) -> None:
        msg = "process must not run after validation failed"
        raise AssertionError(msg)


class FailsWorkflowValidationNode(ControlNode):
    """Fails ``validate_before_workflow_run``, so the flow it is in refuses to start."""

    def validate_before_workflow_run(self) -> list[Exception] | None:
        return [ValueError("The prompt is missing."), ValueError("The seed is out of range.")]

    def process(self) -> None:
        msg = "process must not run after the flow failed validation"
        raise AssertionError(msg)


class PoisonSourceNode(ControlNode):
    """Produces the one value ``RefusesPoisonInputNode`` will not accept."""

    POISON = "poison"

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)
        self.add_parameter(
            Parameter(
                name="text",
                tooltip="A value the next node refuses",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        self.parameter_output_values["text"] = self.POISON


class RefusesPoisonInputNode(ControlNode):
    """Refuses the value its upstream node passes in, so the hand-over fails before the node runs."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)
        self.add_parameter(
            Parameter(
                name="text",
                tooltip="Text to accept",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )

    def before_value_set(self, parameter: Parameter, value: Any) -> Any:
        if parameter.name == "text" and value == PoisonSourceNode.POISON:
            msg = "The text cannot be 'poison'."
            raise ValueError(msg)
        return super().before_value_set(parameter, value)

    def process(self) -> None:
        return None


class _IndexedFailingNode(ControlNode):
    """A loop body node that always fails, with a reason chosen from the iteration's ``index``."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)
        self.add_parameter(
            Parameter(
                name="index",
                tooltip="Position of the item, wired from the loop",
                type="int",
                default_value=0,
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )

    def reason(self, index: int) -> str:
        raise NotImplementedError

    def process(self) -> None:
        raise RuntimeError(self.reason(int(self.get_parameter_value("index") or 0)))


class ConstantFailingBodyNode(_IndexedFailingNode):
    """Fails every iteration for the same reason."""

    def reason(self, index: int) -> str:  # noqa: ARG002
        return "The body refused the item."


class ParityFailingBodyNode(_IndexedFailingNode):
    """Fails every iteration, with one reason for even indexes and another for odd ones."""

    def reason(self, index: int) -> str:
        if index % 2 == 0:
            return "The body refused an even item."
        return "The body refused an odd item."


class DistinctFailingBodyNode(_IndexedFailingNode):
    """Fails every iteration, each for a reason of its own."""

    def reason(self, index: int) -> str:
        return f"The body refused item {index}."


class SomeItemsFailingBodyNode(_IndexedFailingNode):
    """Fails the iterations whose index is a multiple of five, and passes the rest."""

    def reason(self, index: int) -> str:
        return f"The body refused item {index}."

    def process(self) -> None:
        if int(self.get_parameter_value("index") or 0) % 5 == 0:
            super().process()


class FourteenItemForEachGroupNode(BaseIterativeNodeGroup):
    """Iterates fourteen items, enough for a reason that spans more iterations than a loop enumerates."""

    ITEMS = tuple(f"item {number}" for number in range(14))

    def _get_iteration_items(self) -> list[Any]:
        return list(self.ITEMS)

    def _get_current_item_value(self, iteration_index: int) -> Any:
        return self.ITEMS[iteration_index]
