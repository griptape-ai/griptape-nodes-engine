"""Running a node's pre-run checks at its turn, before it runs.

The resolution machine calls `validate_node`, in a worker thread, which runs the node's declared
validators and then its `validate_before_node_run` inside one `reading_once`, and returns
both methods' errors together.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from griptape_nodes.exe_types.core_types import ParameterList
from griptape_nodes.exe_types.validators._checks import input_subject
from griptape_nodes.exe_types.validators.base import holds_list, media_kind_of, needs_for_kind
from griptape_nodes.utils.media_probe import EMPTY_MEDIA, MediaInfo, read_media, reading_once

if TYPE_CHECKING:
    from collections.abc import Sequence

    from griptape_nodes.exe_types.core_types import Parameter
    from griptape_nodes.exe_types.node_error import NodeError
    from griptape_nodes.exe_types.node_types import BaseNode
    from griptape_nodes.exe_types.validators.base import PreRunValidator
    from griptape_nodes.utils.media_values import MediaKind

__all__ = ["validate_inputs", "validate_node"]

# What an input is called in a message, by the kind of media its parameter holds.
_NOUN_FOR_KIND: dict[MediaKind, str] = {"image": "image", "video": "video", "audio": "audio clip"}
_DEFAULT_NOUN = "input"


def validate_node(node: BaseNode) -> list[Exception]:
    """Run a node's declared validators, then its `validate_before_node_run`, and return their errors.

    Both run inside one `reading_once`, so each input is read once for the validators and
    the node's own checks together.
    """
    with reading_once():
        errors: list[Exception] = [*validate_inputs(node)]
        errors.extend(node.validate_before_node_run() or [])
    return errors


def validate_inputs(node: BaseNode) -> list[NodeError]:
    """Every error the node's inputs break its declared limits with, or an empty list.

    For each parameter with `pre_run_validators`, reads its value once, for every reading those
    validators need together, and runs each one. A list parameter's validators run on each item,
    named by its position, except those that check the whole list. A value that can't be read
    locally is skipped, as the checks underneath skip it.

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
    kind = media_kind_of(parameter)
    shown_name = parameter.display_name or parameter.name
    noun = _noun(kind)
    if not holds_list(parameter):
        value = _value_here(node, parameter.name)
        if value is None:
            return
        info = _read(value, validators, kind)
        subject = input_subject(shown_name, noun=noun)
        errors.extend(_collect(validators, value, subject=subject, media=info, docs_url=docs_url))
        return

    items = _list_items(node, parameter)
    whole_list = [validator for validator in validators if validator.checks_whole_list]
    per_item = [validator for validator in validators if not validator.checks_whole_list]
    errors.extend(_collect(whole_list, items, subject=shown_name, media=EMPTY_MEDIA, docs_url=docs_url))
    for position, item in enumerate(items, start=1):
        info = _read(item, per_item, kind)
        subject = input_subject(shown_name, noun=noun, position=position)
        errors.extend(_collect(per_item, item, subject=subject, media=info, docs_url=docs_url, position=position))


def _selected_validators(node: BaseNode, parameter: Parameter) -> list[PreRunValidator]:
    """The validators that apply to a parameter now, with each `IfParameter` replaced by its pick."""
    validators: list[PreRunValidator] = []
    for declared in parameter.pre_run_validators:
        validators.extend(declared.selected(node))
    return validators


def _read(value: Any, validators: Sequence[PreRunValidator], kind: MediaKind | None) -> MediaInfo:
    """One read of a value, for every reading these validators need together."""
    needs = needs_for_kind(validators, kind)
    if kind is None or not needs:
        return EMPTY_MEDIA
    return read_media(value, needs, kind=kind)


def _collect(  # noqa: PLR0913
    validators: Sequence[PreRunValidator],
    value: Any,
    *,
    subject: str,
    media: MediaInfo,
    docs_url: str | None,
    position: int | None = None,
) -> list[NodeError]:
    """Each error these validators report for one value."""
    errors: list[NodeError] = []
    for validator in validators:
        error = validator.check(value, subject=subject, docs_url=docs_url, media=media, position=position)
        if error is not None:
            errors.append(error)
    return errors


def _value_here(node: BaseNode, name: str) -> Any:
    """The parameter's value, or None when it's held in another process and can't be read here.

    A value a worker produced and kept (a `serializable=False` output) reaches this process only as a
    key, and reading it raises. It can't be read locally, so it's skipped like any other such value.
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


def _noun(kind: MediaKind | None) -> str:
    """What an input is called in a message, by the kind of media its parameter holds."""
    if kind is None:
        return _DEFAULT_NOUN
    return _NOUN_FOR_KIND[kind]
