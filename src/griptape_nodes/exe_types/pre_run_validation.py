"""Running a node's declared pre-run validators at its turn, before it runs.

The resolution machine calls `run_pre_run_validators` just before the node's
`validate_before_node_run`, inside one `input_check_budget` and one `input_check_readings`, and
reports both methods' errors together. It also names each input whose video or audio took too long to
read, so the machine can tell the user it wasn't checked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from griptape_nodes.exe_types.core_types import ParameterList
from griptape_nodes.exe_types.input_checks import input_subject, read_media
from griptape_nodes.exe_types.validators.base import MediaInfo, holds_list, media_kind_of, needs_for_kind

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from griptape_nodes.exe_types.core_types import Parameter
    from griptape_nodes.exe_types.node_error import NodeError
    from griptape_nodes.exe_types.node_types import BaseNode
    from griptape_nodes.exe_types.validators.base import PreRunValidator
    from griptape_nodes.utils.media_values import MediaKind

__all__ = ["run_pre_run_validators", "unchecked_inputs_message", "unchecked_locations_by_name"]

# What an input is called in a message, by the kind of media its parameter holds.
_NOUN_FOR_KIND: dict[MediaKind, str] = {"image": "image", "video": "video", "audio": "audio clip"}
_DEFAULT_NOUN = "input"


def run_pre_run_validators(node: BaseNode, *, unchecked: dict[str, str] | None = None) -> list[NodeError]:
    """Every error the node's inputs break its declared limits with, or an empty list.

    For each parameter with `pre_run_validators`, reads its value once, for every reading those
    validators need together, and runs each one. A list parameter's validators run on each item,
    named by its position, except those that check the whole list. Then the node's
    `node_validators` run with what was read. A value that can't be read locally is skipped, as the
    checks underneath skip it.

    Args:
        node: The node about to run.
        unchecked: If given, gets each input with declared validators that was skipped because
            its video or audio probe ran out of time: its location, mapped to its subject such as
            "The video in 'Video'", in the order checked. `unchecked_locations_by_name` names the
            rest.
    """
    declared = [parameter for parameter in node.parameters if parameter.pre_run_validators]
    if not declared and not node.node_validators:
        return []
    docs_url = node.input_limits_url()
    errors: list[NodeError] = []
    media: dict[str, list[MediaInfo]] = {}
    for parameter in declared:
        validators = _selected_validators(node, parameter)
        if not validators:
            continue
        readings = _check_parameter(node, parameter, validators, docs_url=docs_url, errors=errors)
        if readings is None:
            continue
        media[parameter.name] = readings
        if unchecked is not None:
            unchecked.update(_timed_out_subjects(parameter, readings))
    for node_validator in node.node_validators:
        errors.extend(node_validator.check(node, media=media, docs_url=docs_url))
    return errors


def unchecked_locations_by_name(timed_out: Collection[str], named: Collection[str]) -> list[str]:
    """Subjects for timed-out locations no declared input accounts for, named by their file names.

    A node's `validate_before_node_run` or a `NodeValidator` can probe a file directly; when that
    times out there is no parameter to name, so the file is named instead, such as "'clip.mp4'".

    Args:
        timed_out: Every location that timed out during the node's checks, from `timed_out_locations`.
        named: The locations `run_pre_run_validators` already named through `unchecked`.
    """
    return [f"'{_file_name(location)}'" for location in sorted(set(timed_out) - set(named))]


def unchecked_inputs_message(inputs: Sequence[str]) -> str:
    """One sentence telling the user these inputs weren't checked before the run, and why.

    Args:
        inputs: Each input's subject, as `run_pre_run_validators` gives them, such as
            "The video in 'Video'".
    """
    if len(inputs) == 1:
        return (
            f"{inputs[0]} wasn't checked before the run because reading it took too long. "
            "The provider may still reject it."
        )
    rest = [subject[0].lower() + subject[1:] for subject in inputs[1:]]
    listed = ", ".join([inputs[0], *rest[:-1]]) + f" and {rest[-1]}"
    return (
        f"{listed} weren't checked before the run because reading them took too long. "
        "The provider may still reject them."
    )


def _check_parameter(
    node: BaseNode,
    parameter: Parameter,
    validators: Sequence[PreRunValidator],
    *,
    docs_url: str | None,
    errors: list[NodeError],
) -> list[MediaInfo] | None:
    """Run one parameter's validators, appending their errors, and return what was read for it.

    None when the parameter holds no media, so nothing was read.
    """
    kind = media_kind_of(parameter)
    shown_name = parameter.display_name or parameter.name
    noun = _noun(parameter, kind)
    if not holds_list(parameter):
        value = node.get_parameter_value(parameter.name)
        if value is None:
            return None
        info = _read(value, validators, kind)
        subject = input_subject(shown_name, noun=noun)
        errors.extend(_collect(validators, value, subject=subject, media=info, docs_url=docs_url))
        return _readings_if_media([info], kind)

    items = _list_items(node, parameter)
    whole_list = [validator for validator in validators if validator.checks_whole_list]
    per_item = [validator for validator in validators if not validator.checks_whole_list]
    errors.extend(_collect(whole_list, items, subject=shown_name, media=MediaInfo(), docs_url=docs_url))
    readings: list[MediaInfo] = []
    for position, item in enumerate(items, start=1):
        info = _read(item, per_item, kind)
        readings.append(info)
        subject = input_subject(shown_name, noun=noun, position=position)
        errors.extend(_collect(per_item, item, subject=subject, media=info, docs_url=docs_url))
    return _readings_if_media(readings, kind)


def _selected_validators(node: BaseNode, parameter: Parameter) -> list[PreRunValidator]:
    """The validators that apply to a parameter now, with each `ByParameter` replaced by its pick."""
    validators: list[PreRunValidator] = []
    for declared in parameter.pre_run_validators:
        validators.extend(declared.selected(node))
    return validators


def _read(value: Any, validators: Sequence[PreRunValidator], kind: MediaKind | None) -> MediaInfo:
    """One read of a value, for every reading these validators need together."""
    needs = needs_for_kind(validators, kind)
    if kind is None or not needs:
        return MediaInfo()
    return read_media(value, needs, kind=kind)


def _collect(
    validators: Sequence[PreRunValidator], value: Any, *, subject: str, media: MediaInfo, docs_url: str | None
) -> list[NodeError]:
    """Each error these validators report for one value."""
    errors: list[NodeError] = []
    for validator in validators:
        error = validator.check(value, subject=subject, media=media, docs_url=docs_url)
        if error is not None:
            errors.append(error)
    return errors


def _list_items(node: BaseNode, parameter: Parameter) -> list[Any]:
    """A list parameter's items, flattened, without empty rows."""
    if isinstance(parameter, ParameterList):
        return node.get_parameter_list_value(parameter.name)
    value = node.get_parameter_value(parameter.name)
    if not isinstance(value, list):
        return []
    return [item for item in value if item is not None]


def _noun(parameter: Parameter, kind: MediaKind | None) -> str:
    """What the parameter's input is called in a message."""
    if parameter.pre_run_noun is not None:
        return parameter.pre_run_noun
    if kind is None:
        return _DEFAULT_NOUN
    return _NOUN_FOR_KIND[kind]


def _timed_out_subjects(parameter: Parameter, readings: Sequence[MediaInfo]) -> dict[str, str]:
    """Each of the parameter's inputs whose probe ran out of time: its location, mapped to its subject."""
    shown_name = parameter.display_name or parameter.name
    noun = _noun(parameter, media_kind_of(parameter))
    subjects: dict[str, str] = {}
    for position, reading in enumerate(readings, start=1):
        if not reading.timed_out or reading.location is None:
            continue
        if holds_list(parameter):
            subjects[reading.location] = input_subject(shown_name, noun=noun, position=position)
        else:
            subjects[reading.location] = input_subject(shown_name, noun=noun)
    return subjects


def _file_name(location: str) -> str:
    """The last part of a path or URL, which is how a user knows the file."""
    return location.rstrip("/").rsplit("/", 1)[-1] or location


def _readings_if_media(readings: list[MediaInfo], kind: MediaKind | None) -> list[MediaInfo] | None:
    """The readings for a parameter that holds media, or None for one that doesn't."""
    if kind is None:
        return None
    return readings
