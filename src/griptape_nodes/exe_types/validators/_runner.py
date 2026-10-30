"""Running a node's pre-run checks at its turn, before it runs.

The resolution machine calls `validate_node`, in a worker thread, which runs the node's declared
validators and then its `validate_before_node_run` while each input is read at most once, and returns
both methods' errors together.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from griptape_nodes.exe_types.core_types import ParameterList
from griptape_nodes.exe_types.validators._checks import input_subject
from griptape_nodes.exe_types.validators.base import holds_list
from griptape_nodes.exe_types.validators.media_validator import files_read_once, input_noun, run_validators

if TYPE_CHECKING:
    from collections.abc import Sequence

    from griptape_nodes.exe_types.core_types import Parameter
    from griptape_nodes.exe_types.node_error import NodeError
    from griptape_nodes.exe_types.node_types import BaseNode
    from griptape_nodes.exe_types.validators.base import PreRunValidator

__all__ = ["validate_inputs", "validate_node"]


def validate_node(node: BaseNode) -> list[Exception]:
    """Run a node's declared validators, then its `validate_before_node_run`, and return their errors.

    Both run in one pass, so anything read for an input while checking it is shared between the
    validators and the node's own checks.
    """
    with files_read_once():
        errors: list[Exception] = [*validate_inputs(node)]
        errors.extend(node.validate_before_node_run() or [])
    return errors


def validate_inputs(node: BaseNode) -> list[NodeError]:
    """Every error the node's inputs break its declared limits with, or an empty list.

    For each parameter with `pre_run_validators`, gets its value and runs each validator that applies.
    A list parameter's validators run on each item, named by its position, except those that check
    the whole list. A single input with no value, an empty row, and a value held in another process
    are skipped.

    Args:
        node: The node about to run.
    """
    declared = [parameter for parameter in node.parameters if parameter.pre_run_validators]
    if not declared:
        return []
    docs_url = node.input_limits_url()
    errors: list[NodeError] = []
    for parameter in declared:
        validators = _selected_validators(node, parameter)
        if not validators:
            continue
        _check_parameter(node, parameter, validators, docs_url=docs_url, errors=errors)
    return errors


def _check_parameter(
    node: BaseNode,
    parameter: Parameter,
    validators: Sequence[PreRunValidator],
    *,
    docs_url: str | None,
    errors: list[NodeError],
) -> None:
    """Run one parameter's validators, appending their errors."""
    shown_name = parameter.display_name or parameter.name
    noun = input_noun(parameter)
    if not holds_list(parameter):
        value = _value_here(node, parameter.name)
        if value is None:
            return
        subject = input_subject(shown_name, noun=noun)
        errors.extend(run_validators(validators, value, parameter, subject=subject, docs_url=docs_url))
        return

    items = _list_items(node, parameter)
    whole_list = [validator for validator in validators if validator.checks_whole_list]
    per_item = [validator for validator in validators if not validator.checks_whole_list]
    errors.extend(run_validators(whole_list, items, parameter, subject=shown_name, docs_url=docs_url, whole_list=True))
    for position, item in enumerate(items, start=1):
        subject = input_subject(shown_name, noun=noun, position=position)
        errors.extend(run_validators(per_item, item, parameter, subject=subject, docs_url=docs_url, position=position))


def _selected_validators(node: BaseNode, parameter: Parameter) -> list[PreRunValidator]:
    """The validators that apply to a parameter now, with each `IfParameter` replaced by its pick."""
    validators: list[PreRunValidator] = []
    for declared in parameter.pre_run_validators:
        validators.extend(declared.selected(node))
    return validators


def _value_here(node: BaseNode, name: str) -> Any:
    """The parameter's value, or None when it's held in another process and can't be read here.

    A value a worker produced and kept (a `serializable=False` output) reaches this process only as a
    key, and reading it raises, so it's skipped.
    """
    try:
        return node.get_parameter_value(name)
    except RuntimeError:
        return None


def _list_items(node: BaseNode, parameter: Parameter) -> list[Any]:
    """A list parameter's items, flattened, without empty rows; empty when they're held in another process."""
    if isinstance(parameter, ParameterList):
        try:
            return node.get_parameter_list_value(parameter.name)
        except RuntimeError:
            return []
    value = _value_here(node, parameter.name)
    if not isinstance(value, list):
        return []
    return [item for item in value if item is not None]
