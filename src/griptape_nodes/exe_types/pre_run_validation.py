"""Running a node's pre-run checks at its turn, before it runs.

The resolution machine calls `validate_node`, which runs the node's declared validators and then
its `validate_before_node_run`, inside one `input_check_budget` and one `input_check_readings`. It
returns both methods' errors together, and names each input whose video or audio took too long to
read, so the machine can tell the user it wasn't checked.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import TYPE_CHECKING, Any, NamedTuple

from griptape_nodes.exe_types.core_types import ParameterList
from griptape_nodes.exe_types.input_check_budget import input_check_budget
from griptape_nodes.exe_types.input_checks import input_check_readings, input_subject, read_media, timed_out_locations
from griptape_nodes.exe_types.validators.base import EMPTY_MEDIA, MediaInfo, holds_list, media_kind_of, needs_for_kind
from griptape_nodes.files.path_utils import is_url

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from griptape_nodes.exe_types.core_types import Parameter
    from griptape_nodes.exe_types.node_error import NodeError
    from griptape_nodes.exe_types.node_types import BaseNode
    from griptape_nodes.exe_types.validators.base import PreRunValidator
    from griptape_nodes.utils.media_values import MediaKind

__all__ = [
    "UncheckedInput",
    "ValidationResult",
    "unchecked_inputs_message",
    "unchecked_locations_by_name",
    "validate_inputs",
    "validate_node",
]

# What an input is called in a message, by the kind of media its parameter holds.
_NOUN_FOR_KIND: dict[MediaKind, str] = {"image": "image", "video": "video", "audio": "audio clip"}
_DEFAULT_NOUN = "input"


class UncheckedInput(NamedTuple):
    """An input with declared validators whose video or audio probe ran out of time."""

    location: str
    """Where the input was read from."""
    subject: str
    """How a message names it, such as "The video in 'Video'"."""


@dataclass(frozen=True)
class ValidationResult:
    """What a node's pre-run checks found."""

    errors: list[Exception]
    """Every error, the declared validators' first and then `validate_before_node_run`'s, in order."""
    unchecked_inputs: list[str]
    """Each input whose video or audio took too long to read, such as "The video in 'Video'", or a
    file the node read itself, such as "'clip.mp4'". Empty when every input was read in time."""


def validate_node(node: BaseNode) -> ValidationResult:
    """Run a node's declared validators, then its `validate_before_node_run`, and collect what they found.

    Both run inside one `input_check_budget`, which stops their video and audio probes (they block
    the engine's event loop) after a few seconds, and one `input_check_readings`, so each input is
    read once for the validators and the node's own checks together.
    """
    unchecked: list[UncheckedInput] = []
    with input_check_budget(), input_check_readings():
        errors: list[Exception] = [*validate_inputs(node, unchecked=unchecked)]
        errors.extend(node.validate_before_node_run() or [])
        # The node's own checks can probe files directly; name any of those that timed out too.
        named = {entry.location for entry in unchecked}
        unchecked_inputs = [
            *(entry.subject for entry in unchecked),
            *unchecked_locations_by_name(timed_out_locations(), named),
        ]
    return ValidationResult(errors=errors, unchecked_inputs=unchecked_inputs)


def validate_inputs(node: BaseNode, *, unchecked: list[UncheckedInput] | None = None) -> list[NodeError]:
    """Every error the node's inputs break its declared limits with, or an empty list.

    For each parameter with `pre_run_validators`, reads its value once, for every reading those
    validators need together, and runs each one. A list parameter's validators run on each item,
    named by its position, except those that check the whole list. A value that can't be read
    locally is skipped, as the checks underneath skip it.

    Args:
        node: The node about to run.
        unchecked: If given, gets each input with declared validators that was skipped because
            its video or audio probe ran out of time, as its location and its subject such as
            "The video in 'Video'", in the order checked. Two inputs holding the same file are both
            listed. `unchecked_locations_by_name` names the rest.
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
        readings = _check_parameter(node, parameter, validators, docs_url=docs_url, errors=errors)
        if unchecked is not None:
            unchecked.extend(_timed_out_subjects(parameter, readings))
    return errors


def unchecked_locations_by_name(timed_out: Collection[str], named: Collection[str]) -> list[str]:
    """Subjects for timed-out locations no declared input accounts for, named by their file names.

    A node's `validate_before_node_run` can probe a file directly; when that times out there is no
    parameter to name, so the file is named instead, such as "'clip.mp4'".

    Args:
        timed_out: Every location that timed out during the node's checks, from `timed_out_locations`.
        named: The locations `validate_inputs` already named through `unchecked`.
    """
    return [f"'{_file_name(location)}'" for location in sorted(set(timed_out) - set(named))]


def unchecked_inputs_message(inputs: Sequence[str]) -> str:
    """One sentence telling the user these inputs weren't checked before the run, and why.

    Args:
        inputs: Each input's subject, as `ValidationResult.unchecked_inputs` gives them, such as
            "The video in 'Video'".
    """
    if len(inputs) == 1:
        return (
            f"{inputs[0]} wasn't checked before the run because reading it took too long. "
            "The provider may still reject it."
        )
    rest = [_mid_sentence(subject) for subject in inputs[1:]]
    listed = ", ".join([inputs[0], *rest[:-1]]) + f" and {rest[-1]}"
    return (
        f"{listed} weren't checked before the run because reading them took too long. "
        "The provider may still reject them."
    )


def _mid_sentence(subject: str) -> str:
    """A subject as it reads after the first in a list: "The video" and "Audio clip 2" lowercased.

    A quoted file name starts with its quote, so it stays as it is.
    """
    return subject[:1].lower() + subject[1:]


def _check_parameter(
    node: BaseNode,
    parameter: Parameter,
    validators: Sequence[PreRunValidator],
    *,
    docs_url: str | None,
    errors: list[NodeError],
) -> list[MediaInfo]:
    """Run one parameter's validators, appending their errors, and return what was read for each value."""
    kind = media_kind_of(parameter)
    shown_name = parameter.display_name or parameter.name
    noun = _noun(kind)
    if not holds_list(parameter):
        value = _value_here(node, parameter.name)
        if value is None:
            return []
        info = _read(value, validators, kind)
        subject = input_subject(shown_name, noun=noun)
        errors.extend(_collect(validators, value, subject=subject, media=info, docs_url=docs_url))
        return [info]

    items = _list_items(node, parameter)
    whole_list = [validator for validator in validators if validator.checks_whole_list]
    per_item = [validator for validator in validators if not validator.checks_whole_list]
    errors.extend(_collect(whole_list, items, subject=shown_name, media=EMPTY_MEDIA, docs_url=docs_url))
    readings: list[MediaInfo] = []
    for position, item in enumerate(items, start=1):
        info = _read(item, per_item, kind)
        readings.append(info)
        subject = input_subject(shown_name, noun=noun, position=position)
        errors.extend(_collect(per_item, item, subject=subject, media=info, docs_url=docs_url))
    return readings


def _selected_validators(node: BaseNode, parameter: Parameter) -> list[PreRunValidator]:
    """The validators that apply to a parameter now, with each `DependsOn` replaced by its pick."""
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


def _collect(
    validators: Sequence[PreRunValidator], value: Any, *, subject: str, media: MediaInfo, docs_url: str | None
) -> list[NodeError]:
    """Each error these validators report for one value."""
    errors: list[NodeError] = []
    for validator in validators:
        error = validator.check(value, subject=subject, docs_url=docs_url, media=media)
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


def _timed_out_subjects(parameter: Parameter, readings: Sequence[MediaInfo]) -> list[UncheckedInput]:
    """Each of the parameter's inputs whose probe ran out of time, as its location and its subject."""
    shown_name = parameter.display_name or parameter.name
    noun = _noun(media_kind_of(parameter))
    subjects: list[UncheckedInput] = []
    for position, reading in enumerate(readings, start=1):
        if not reading.timed_out or reading.location is None:
            continue
        if holds_list(parameter):
            subjects.append(UncheckedInput(reading.location, input_subject(shown_name, noun=noun, position=position)))
        else:
            subjects.append(UncheckedInput(reading.location, input_subject(shown_name, noun=noun)))
    return subjects


def _file_name(location: str) -> str:
    """The last part of a path or URL, which is how a user knows the file.

    Drops a URL's query, such as the static server's `?v=` cachebuster, and splits on either path
    separator, so a Windows path shows only its file name.
    """
    without_query = location.split("?", 1)[0] if is_url(location) else location
    return PureWindowsPath(without_query.rstrip("/\\")).name or location
