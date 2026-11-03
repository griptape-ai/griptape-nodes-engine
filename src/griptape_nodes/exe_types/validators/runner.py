"""Running a node's pre-run checks at its turn, before it runs.

The resolution machine calls `validate_node`, in a worker thread, which runs the node's declared
validators and then its `validate_before_node_run` while each input is read at most once, and returns
both methods' errors together. Tests call it the same way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from griptape_nodes.exe_types.core_types import ParameterList
from griptape_nodes.exe_types.node_error import NodeError
from griptape_nodes.exe_types.validators._checks import input_noun, input_subject
from griptape_nodes.exe_types.validators.base import (
    IfParameter,
    PreRunRule,
    _holds_list,
    _pick_validators,
    _PickedValidator,
)
from griptape_nodes.exe_types.validators.context import CheckContext
from griptape_nodes.utils.media_probe import reading_once

if TYPE_CHECKING:
    from collections.abc import Sequence

    from griptape_nodes.exe_types.core_types import Parameter
    from griptape_nodes.exe_types.node_types import BaseNode

__all__ = ["validate_inputs", "validate_node"]


def validate_node(node: BaseNode) -> list[Exception]:
    """Run a node's declared validators, then its `validate_before_node_run`, and return their errors.

    Both run in one pass, so anything read for an input while checking it is shared between the
    validators and the node's own checks.
    """
    with reading_once():
        errors: list[Exception] = [*validate_inputs(node)]
        errors.extend(node.validate_before_node_run() or [])
    return errors


def validate_inputs(node: BaseNode) -> list[NodeError]:
    """Every error the node's inputs break its declared limits with, or an empty list.

    For each parameter with `pre_run_validators`, finds the validators that apply now and runs each
    on its value. A list parameter's validators run on each item, named by its position, except those
    that check the whole list. A single input with no value, an empty row, and a value held in
    another process are skipped, as are the checks an `IfParameter` picks by a setting held in
    another process. A rule that reads a parameter the node doesn't have is reported as an error.
    """
    declared = [parameter for parameter in node.parameters if parameter.pre_run_validators]
    if not declared:
        return []
    docs_url = node.input_limits_url()
    errors: list[NodeError] = []
    for parameter in declared:
        try:
            picked = _picked_now(node, parameter)
        except _UnknownSettingError as e:
            errors.append(NodeError(str(e), links=None))
            continue
        if not picked:
            continue
        errors.extend(_check_parameter(node, parameter, picked, docs_url=docs_url))
    return errors


def _picked_now(node: BaseNode, parameter: Parameter) -> list[_PickedValidator]:
    """The validators that apply to a parameter at the node's turn, each `IfParameter` resolved.

    An `IfParameter` whose setting is held in another process picks nothing.
    """

    def branches(rule: IfParameter) -> Sequence[PreRunRule]:
        try:
            current = _read_setting(node, rule.parameter, for_parameter=parameter.name)
        except _SettingUnavailableError:
            return ()
        return rule._listed_for(current)

    picked: list[_PickedValidator] = []
    for rule in parameter.pre_run_validators:
        picked.extend(_pick_validators(rule, branches))
    return picked


def _check_parameter(
    node: BaseNode, parameter: Parameter, picked: Sequence[_PickedValidator], *, docs_url: str | None
) -> list[NodeError]:
    """Run one parameter's validators on its value, or on each item and the whole list."""
    shown_name = parameter.display_name or parameter.name
    noun = input_noun(parameter)
    if not _holds_list(parameter):
        value = _value_here(node, parameter.name)
        if value is None:
            return []
        subject = input_subject(shown_name, noun=noun)
        ctx = CheckContext(parameter=parameter, value=value, subject=subject, docs_url=docs_url)
        return _run(picked, value, ctx)

    items = _list_items(node, parameter)
    whole_list = [entry for entry in picked if entry.validator.checks_whole_list]
    per_item = [entry for entry in picked if not entry.validator.checks_whole_list]
    ctx = CheckContext(parameter=parameter, value=items, subject=shown_name, docs_url=docs_url)
    errors = _run(whole_list, items, ctx)
    for position, item in enumerate(items, start=1):
        subject = input_subject(shown_name, noun=noun, position=position)
        ctx = CheckContext(parameter=parameter, value=item, subject=subject, docs_url=docs_url, position=position)
        errors.extend(_run(per_item, item, ctx))
    return errors


def _run(picked: Sequence[_PickedValidator], value: Any, ctx: CheckContext) -> list[NodeError]:
    """Each error the validators that apply to `value` report, sharing one context."""
    errors: list[NodeError] = []
    for entry in picked:
        if not entry.applies(value, ctx):
            continue
        error = entry.validator.check(value, ctx)
        if error is not None:
            errors.append(error)
    return errors


class _UnknownSettingError(ValueError):
    """An `IfParameter` names a parameter the node doesn't have, such as a misspelled one."""


class _SettingUnavailableError(Exception):
    """The parameter an `IfParameter` reads is held in another process and can't be read here."""


def _read_setting(node: BaseNode, name: str, *, for_parameter: str) -> Any:
    """The current value of the parameter an `IfParameter` on `for_parameter` picks its limits by.

    Raises:
        _UnknownSettingError: If the node has no parameter called `name`, so a misspelled name fails
            the node's checks instead of reading as None and quietly applying `default`.
        _SettingUnavailableError: If the value is held in another process and can't be read here.
    """
    if node.get_parameter_by_name(name) is None:
        msg = (
            f"Attempted to read parameter '{name}' to pick the limits for '{for_parameter}'. "
            f"Failed because the node has no parameter called '{name}'."
        )
        raise _UnknownSettingError(msg)
    try:
        return node.get_parameter_value(name)
    except RuntimeError as e:
        raise _SettingUnavailableError(name) from e


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
